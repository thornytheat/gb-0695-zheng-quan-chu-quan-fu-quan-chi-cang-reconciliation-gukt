import io
import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('CONV_DB', str(tmp_path / 'conv.sqlite3'))
    monkeypatch.setenv('APP_DB', str(tmp_path / 'runs.sqlite3'))
    from app.main import app
    app.config['TESTING'] = True
    return app.test_client()


def test_pages_render_and_default_sample(client):
    for n in range(1, 6):
        assert client.get('/conv/work/%d' % n).status_code == 200


def test_run_json_and_idempotent_save(client):
    first = client.post('/conv/api/run', json={}).get_json()
    again = client.post('/conv/api/run', json={}).get_json()
    assert first['manifest']['run_id'] == again['manifest']['run_id']
    assert again['reused'] is True
    assert first['reconciliation']['balanced'] is True


def test_stage_endpoints_independent(client):
    for n in range(1, 6):
        resp = client.post('/conv/api/c%d' % n, json={})
        assert resp.status_code == 200
        assert resp.get_json()['stage'] == n


def test_publish_blocked_until_review_passed(client):
    run = client.post('/conv/api/run', json={}).get_json()
    rid = run['manifest']['run_id']
    # 未复核 → 禁止发布
    assert client.post('/conv/api/publish', json={'run_id': rid}).status_code == 409
    # 退回 → 仍禁止
    client.post('/conv/api/review', json={'run_id': rid, 'revision': 1, 'decision': '退回'})
    assert client.post('/conv/api/publish', json={'run_id': rid}).status_code == 409
    # 通过后可发布
    client.post('/conv/api/review', json={'run_id': rid, 'revision': 2, 'decision': '通过'})
    out = client.post('/conv/api/publish', json={'run_id': rid})
    assert out.status_code == 200 and out.get_json()['reused'] is False


def test_publish_is_idempotent_and_blocks_duplicate_conversion(client):
    run = client.post('/conv/api/run', json={}).get_json()
    rid = run['manifest']['run_id']
    client.post('/conv/api/review', json={'run_id': rid, 'revision': 1, 'decision': '通过'})
    first = client.post('/conv/api/publish', json={'run_id': rid}).get_json()
    second = client.post('/conv/api/publish', json={'run_id': rid}).get_json()
    assert second['reused'] is True and first['published_at'] == second['published_at']
    # 仅改处理日（转股价不变→指纹不变）形成新批次，发布须因已转股指纹而 409
    from app.config import CONV_SAMPLE
    text = CONV_SAMPLE.read_text(encoding='utf-8-sig').replace('2026-11-19', '2026-11-22', 1)
    other = client.post('/conv/api/run', json={'csv': text}).get_json()
    rid2 = other['manifest']['run_id']
    assert rid2 != rid
    client.post('/conv/api/review', json={'run_id': rid2, 'revision': 1, 'decision': '通过'})
    blocked = client.post('/conv/api/publish', json={'run_id': rid2})
    assert blocked.status_code == 409 and blocked.get_json()['code'] == 'DUPLICATE'


def test_stale_revision_conflict(client):
    run = client.post('/conv/api/run', json={}).get_json()
    rid = run['manifest']['run_id']
    body = {'run_id': rid, 'revision': 1, 'decision': '通过'}
    assert client.post('/conv/api/review', json=body).status_code == 200
    assert client.post('/conv/api/review', json=body).status_code == 409


def test_evidence_csv_contains_stages_and_detail(client):
    run = client.post('/conv/api/run', json={}).get_json()
    text = client.get('/conv/api/evidence.csv?run_id=' + run['manifest']['run_id']).data.decode('utf-8-sig')
    assert '转股明细' in text and '对账-债券维度' in text and '应转股票' in text


def test_csv_upload_field(client):
    from app.config import CONV_SAMPLE
    data = CONV_SAMPLE.read_bytes()
    resp = client.post('/conv/api/run', data={'file': (io.BytesIO(data), 'c.csv')},
                       content_type='multipart/form-data')
    assert resp.status_code == 200 and resp.get_json()['reconciliation']['balanced'] is True


def test_non_object_json_rejected(client):
    for value in ([], 7, 'x', None):
        resp = client.post('/conv/api/run', json=value) if value is not None \
            else client.post('/conv/api/run', data='null', content_type='application/json')
        assert resp.status_code == 400


def test_unknown_run_404(client):
    assert client.get('/conv/api/runs/nope').status_code == 404
    assert client.get('/conv/work/1?run_id=nope').status_code == 404


def test_structured_json_input_and_pre_announcement_flag(client):
    body = {
        'processing_date': '2026-11-19',
        'bonds': [{'bond_id': 'B9', 'initial_price': '10.0000'}],
        'price_events': [{'bond_id': 'B9', 'event_id': 'E', 'eff_date': '2026-11-18',
                          'new_price': '9.0000', 'version': 1}],
        'instructions': [{'instruction_id': 'I1', 'bond_id': 'B9', 'received_at': '2026-11-16',
                          'confirmed': False, 'face_amount': '1000', 'holding_face': '2000'}],
    }
    run = client.post('/conv/api/run', json=body).get_json()
    assert run['reconciliation']['balanced'] is True
    row = run['detail'][0]
    assert '公告前受理' in row['裁决'] and row['适用转股价'] == '9.0000'
    # 1000/9=111 股, 转股 999, 零头 1, 现金 0.09, 保留 1000
    assert row['应转股票'] == '111' and row['折现现金_CNY'] == '0.09'
    assert row['保留债券面值'] == '1000' and row['保留张数'] == '10'


def test_publish_blocked_when_reconciliation_unbalanced(client, monkeypatch):
    import json as _json
    from app.conv import store
    run = client.post('/conv/api/run', json={}).get_json()
    rid = run['manifest']['run_id']
    # 直接篡改持久化批次，制造“对账不符”
    with store.connection() as db:
        payload = _json.loads(db.execute(
            'SELECT payload FROM conv_runs WHERE run_id=?', (rid,)).fetchone()['payload'])
        payload['reconciliation']['balanced'] = False
        db.execute('UPDATE conv_runs SET payload=? WHERE run_id=?',
                   (_json.dumps(payload, ensure_ascii=False), rid))
    client.post('/conv/api/review', json={'run_id': rid, 'revision': 1, 'decision': '通过'})
    blocked = client.post('/conv/api/publish', json={'run_id': rid})
    assert blocked.status_code == 409 and blocked.get_json()['code'] == 'BALANCE'
