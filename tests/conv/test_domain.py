from decimal import Decimal
from datetime import date

from app.conv.domain import convert, effective_price, as_day, holding_conservation, submitted_conservation


def test_convert_whole_and_fraction_split():
    # 2000 面值 / 9.5 = 210.526… → 210 股，零头 2000-1995=5 折现金
    r = convert('2000', '9.5')
    assert r['whole_shares'] == Decimal('210')
    assert r['converted_face'] == Decimal('1995.0')
    assert r['fraction_face'] == Decimal('5.0')
    # 5 * 9.5 / 100 = 0.475 → 0.48
    assert r['cash'] == Decimal('0.48')
    assert submitted_conservation('2000', r['converted_face'], r['fraction_face'])


def test_convert_exact_multiple_no_cash():
    r = convert('1000', '10')
    assert r['whole_shares'] == Decimal('100')
    assert r['fraction_face'] == Decimal('0')
    assert r['cash'] == Decimal('0.00')


def test_cash_rounds_half_up_to_cent():
    # 3000/12.5 = 240 整股 → 无零头；构造有零头场景 1700/12.5=136 整
    r = convert('1500', '9.0')   # 166.666… →166 股, 转股1494, 零头6
    assert r['whole_shares'] == Decimal('166')
    assert r['fraction_face'] == Decimal('6.0')
    # 6*9/100 = 0.54
    assert r['cash'] == Decimal('0.54')


def test_reject_non_whole_bond_face():
    from app.errors import DomainError
    import pytest
    with pytest.raises(DomainError):
        convert('150', '10')   # 非整张
    with pytest.raises(DomainError):
        convert('-100', '10')


def test_no_float_for_large_face():
    r = convert('9007199254740900', '10')   # 100 的整数倍大整数
    assert r['whole_shares'] == Decimal('900719925474090')
    assert isinstance(r['whole_shares'], Decimal)


def test_effective_price_chooses_latest_applied_event():
    bond = {'bond_id': 'B1', 'initial_price': Decimal('10')}
    events = [
        {'bond_id': 'B1', 'eff_date': date(2026, 11, 10), 'new_price': Decimal('9.5'), 'version_n': 1},
        {'bond_id': 'B1', 'eff_date': date(2026, 11, 18), 'new_price': Decimal('9.0'), 'version_n': 2},
    ]
    price, hit = effective_price(events, bond, date(2026, 11, 19))
    assert price == Decimal('9.0000') and hit['version_n'] == 2
    price_before, hit_before = effective_price(events, bond, date(2026, 11, 11))
    assert price_before == Decimal('9.5000') and hit_before['version_n'] == 1
    price_initial, none = effective_price([], bond, date(2026, 11, 1))
    assert price_initial == Decimal('10') and none is None


def test_holding_conservation_keeps_unsubmitted_whole_bonds():
    # 持仓 5000，申请 2000，保留 3000（整张不注销）
    assert holding_conservation('5000', '2000', '3000')
    assert not holding_conservation('5000', '2000', '2900')
