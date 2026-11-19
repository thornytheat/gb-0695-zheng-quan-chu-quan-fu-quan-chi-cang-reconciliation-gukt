import pytest
from decimal import Decimal

from app.conv.pipeline import calculate
from app.config import CONV_SAMPLE


def payload(**over):
    base = {
        'processing_date': '2026-11-19',
        'bonds': [
            {'bond_id': '113001', 'name': '示例转债', 'initial_price': '10.0000'},
            {'bond_id': '113002', 'name': '二号转债', 'initial_price': '12.5000'},
        ],
        'price_events': [
            {'bond_id': '113001', 'event_id': 'E1', 'eff_date': '2026-11-10',
             'new_price': '9.5000', 'reason': '分红', 'version': 1},
            {'bond_id': '113001', 'event_id': 'E2', 'eff_date': '2026-11-18',
             'new_price': '9.0000', 'reason': '年度分红', 'version': 2},
        ],
        'instructions': [
            {'instruction_id': 'C001', 'bond_id': '113001', 'account': 'A',
             'received_at': '2026-11-12', 'confirmed': True, 'face_amount': '2000', 'holding_face': '5000'},
            # 公告(E2 11-18)前受理、未确认 → 处理日按新价 9.0 执行并标注
            {'instruction_id': 'C002', 'bond_id': '113001', 'account': 'B',
             'received_at': '2026-11-16', 'confirmed': False, 'face_amount': '3000', 'holding_face': '8000'},
            {'instruction_id': 'C005', 'bond_id': '113002', 'account': 'E',
             'received_at': '2026-11-17', 'confirmed': True, 'face_amount': '2500', 'holding_face': '10000'},
        ],
    }
    base.update(over)
    return base


def test_sample_runs_and_balances():
    result = calculate(CONV_SAMPLE.read_text(encoding='utf-8-sig'))
    assert result['reconciliation']['balanced'] is True
    assert len(result['stages']) == 5


def test_pre_announcement_uses_new_price_and_flags():
    result = calculate(payload())
    adj = {r['申报号']: r for r in result['stages']['2']['rows']}
    # C002 受理 11-16 早于 E2 生效 11-18，未确认 → 新价 9.0 + 公告前受理
    assert adj['C002']['适用转股价'] == '9.0000'
    assert '公告前受理' in adj['C002']['裁决'] and '新价执行' in adj['C002']['裁决']
    # C001 已确认，同样命中新价但不带公告前受理
    assert adj['C001']['适用转股价'] == '9.0000'
    assert '公告前受理' not in adj['C001']['裁决']
    # 113002 无价格事件 → 原价
    assert adj['C005']['适用转股价'] == '12.5000' and '原价执行' in adj['C005']['裁决']


def test_conversion_detail_conservation_and_retained_whole_bonds():
    result = calculate(payload())
    d = {x['申报号']: x for x in result['detail']}
    # C001: 2000/9 = 222 股, 转股1998, 零头2, 现金 2*9/100=0.18, 保留 5000-2000=3000
    assert Decimal(d['C001']['应转股票']) == Decimal('222')
    assert Decimal(d['C001']['转股面值']) == Decimal('1998')
    assert Decimal(d['C001']['零头面值']) == Decimal('2')
    assert Decimal(d['C001']['折现现金_CNY']) == Decimal('0.18')
    assert Decimal(d['C001']['保留债券面值']) == Decimal('3000') and Decimal(d['C001']['保留张数']) == Decimal('30')
    # 整张保留：保留均为100整数倍
    assert all(Decimal(x['保留张数']) * 100 == Decimal(x['保留债券面值']) for x in result['detail'])


def test_retry_same_instruction_is_idempotent(tmp_path, monkeypatch):
    db = str(tmp_path / 'conv.sqlite3')
    monkeypatch.setenv('CONV_DB', db)
    from app.conv import store
    first = calculate(payload())
    store.save_run(first)
    store.review(first['manifest']['run_id'], 1, '通过')
    store.publish(first['manifest']['run_id'])
    # 同内容重算：指纹命中 → 重复跳过，不产生新股/现金
    again = calculate(payload())
    d = {x['申报号']: x for x in again['detail']}
    assert all(x['处置'] == '重复跳过' for x in again['detail'])
    assert d['C001']['应转股票'] == '0' and d['C001']['折现现金_CNY'] == '0.00'
    # 同文复用同批次：发布已登记，再次发布幂等返回 reused
    again_saved = store.save_run(again)
    assert again_saved['manifest']['run_id'] == first['manifest']['run_id']
    out = store.publish(again_saved['manifest']['run_id'])
    assert out['reused'] is True


def test_price_change_changes_fingerprint_so_not_duplicate(tmp_path, monkeypatch):
    db = str(tmp_path / 'conv.sqlite3')
    monkeypatch.setenv('CONV_DB', db)
    from app.conv import store
    first = calculate(payload())
    store.save_run(first)
    store.review(first['manifest']['run_id'], 1, '通过')
    store.publish(first['manifest']['run_id'])
    # 新一轮分红下调转股价，同申报号视为新指令（指纹含价），可再次转股
    new_events = payload()['price_events'] + [
        {'bond_id': '113001', 'event_id': 'E3', 'eff_date': '2026-11-20',
         'new_price': '8.0000', 'reason': '再分红', 'version': 3}]
    second = calculate(payload(processing_date='2026-11-21', price_events=new_events))
    c001 = next(x for x in second['detail'] if x['申报号'] == 'C001')
    assert c001['处置'] == '转股' and c001['适用转股价'] == '8.0000'


def test_invalid_face_quarantined_or_rejected():
    from app.errors import DomainError
    bad = payload()
    bad['instructions'][0]['face_amount'] = '150'   # 非整张
    with pytest.raises(DomainError):
        calculate(bad)
    over = payload()
    over['instructions'][0]['face_amount'] = '9000'  # 超持仓
    with pytest.raises(DomainError):
        calculate(over)


def test_manifest_identity_stable_for_same_input():
    a = calculate(payload())
    b = calculate(payload())
    assert a['manifest']['run_id'] == b['manifest']['run_id']
