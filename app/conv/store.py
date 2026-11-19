"""可转债转股的持久化：跨批次的已转股记录、发布状态与证据留存。

与除权工具的 runs.sqlite3 分离（conv.sqlite3），避免污染原工具的批次表。
- processed：以申报指纹去重，保证同一笔申报重试不重复转股（跨批次有效）。
- conv_runs：留存每个计算批次的完整结果，供复核页面与证据导出。
- publish：每个批次至多发布一次（幂等），发布需复核对账相符。
"""
import json
import os
import sqlite3
from pathlib import Path

from app.errors import DomainError


def connection():
    path = Path(os.environ.get('CONV_DB', 'data/conv.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute(
        'CREATE TABLE IF NOT EXISTS processed('
        'fingerprint TEXT PRIMARY KEY, bond_id TEXT NOT NULL, run_id TEXT NOT NULL, '
        'instruction_id TEXT NOT NULL, face TEXT NOT NULL, shares TEXT NOT NULL, '
        'cash TEXT NOT NULL, applied_price TEXT NOT NULL, created_at TEXT NOT NULL)')
    db.execute(
        'CREATE TABLE IF NOT EXISTS conv_runs('
        'run_id TEXT PRIMARY KEY, payload TEXT NOT NULL, decision TEXT NOT NULL DEFAULT "待复核", '
        'revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)')
    db.execute(
        'CREATE TABLE IF NOT EXISTS publish('
        'run_id TEXT PRIMARY KEY, published_at TEXT NOT NULL)')
    return db


def known_fingerprints(db, fingerprints):
    if not fingerprints:
        return set()
    found = set()
    for chunk in range(0, len(fingerprints), 500):
        marks = ','.join('?' * min(500, len(fingerprints) - chunk))
        rows = db.execute(
            'SELECT fingerprint FROM processed WHERE fingerprint IN (%s)' % marks,
            fingerprints[chunk:chunk + 500]).fetchall()
        found.update(r['fingerprint'] for r in rows)
    return found


def save_run(result):
    """保存计算批次；同 run_id 复用，不覆盖既有复核结论。"""
    with connection() as db:
        row = db.execute('SELECT payload,decision,revision FROM conv_runs WHERE run_id=?',
                         (result['manifest']['run_id'],)).fetchone()
        if row:
            return {**json.loads(row['payload']), 'decision': row['decision'],
                    'revision': row['revision'], 'reused': True}
        db.execute('INSERT INTO conv_runs(run_id,payload,created_at) VALUES(?,?,?)',
                   (result['manifest']['run_id'], json.dumps(result, ensure_ascii=False),
                    result['manifest']['created_at']))
    return {**result, 'decision': '待复核', 'revision': 1, 'reused': False}


def get_run(run_id):
    with connection() as db:
        row = db.execute('SELECT payload,decision,revision FROM conv_runs WHERE run_id=?',
                         (run_id,)).fetchone()
        if row is None:
            raise DomainError('NOT_FOUND', '转股批次不存在', 404)
        return {**json.loads(row['payload']), 'decision': row['decision'],
                'revision': row['revision']}


def latest_run():
    with connection() as db:
        row = db.execute('SELECT payload,decision,revision FROM conv_runs '
                         'ORDER BY rowid DESC LIMIT 1').fetchone()
        if row is None:
            return None
        return {**json.loads(row['payload']), 'decision': row['decision'],
                'revision': row['revision']}


def history():
    with connection() as db:
        return [dict(r) for r in db.execute(
            'SELECT run_id,decision,revision,created_at FROM conv_runs ORDER BY rowid DESC')]


def review(run_id, revision, decision):
    if decision not in ('通过', '退回'):
        raise DomainError('DECISION', '复核动作无效')
    with connection() as db:
        changed = db.execute(
            'UPDATE conv_runs SET revision=revision+1,decision=? '
            'WHERE run_id=? AND revision=?', (decision, run_id, revision)).rowcount
        if not changed:
            raise DomainError('VERSION', '版本已变化，请重新载入', 409)
    return {'revision': revision + 1, 'decision': decision}


def is_published(run_id):
    with connection() as db:
        return db.execute('SELECT 1 FROM publish WHERE run_id=?', (run_id,)).fetchone() is not None


def publish(run_id):
    """发布（登记转股结果）。要求批次复核通过且整批对账相符；幂等。"""
    run = get_run(run_id)
    if run['decision'] != '通过':
        raise DomainError('REVIEW', '复核未通过，禁止发布', 409)
    if not run['reconciliation']['balanced']:
        raise DomainError('BALANCE', '整批对账不符，禁止发布', 409)
    with connection() as db:
        existing = db.execute('SELECT published_at FROM publish WHERE run_id=?',
                              (run_id,)).fetchone()
        if existing:
            return {'run_id': run_id, 'published_at': existing['published_at'], 'reused': True}
        import time
        published_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        converted = [d for d in run['detail'] if d['处置'] == '转股']
        # 整批申报都已被历史批次转股（重复跳过）→ 无新增转股，禁止再次发布
        if run['detail'] and not converted:
            raise DomainError('DUPLICATE', '本批申报均已转股，重复发布被拒绝', 409)
        # 登记前再查一次指纹，杜绝并发/跨批次重复转股
        fps = [d['fingerprint'] for d in converted]
        clash = known_fingerprints(db, fps)
        if clash:
            raise DomainError('DUPLICATE', '存在已转股申报，禁止重复发布：%s'
                              % ','.join(sorted(clash)[:5]), 409)
        for d in converted:
            db.execute(
                'INSERT OR IGNORE INTO processed(fingerprint,bond_id,run_id,instruction_id,'
                'face,shares,cash,applied_price,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                (d['fingerprint'], d['债券'], run_id, d['申报号'], d['申请转股面值'],
                 d['应转股票'], d['折现现金_CNY'], d['适用转股价'], published_at))
        db.execute('INSERT INTO publish(run_id,published_at) VALUES(?,?)',
                   (run_id, published_at))
    return {'run_id': run_id, 'published_at': published_at, 'reused': False}
