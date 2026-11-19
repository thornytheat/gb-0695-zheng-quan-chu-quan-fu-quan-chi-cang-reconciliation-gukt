"""可转债转股的 HTTP 入口：JSON / 组合CSV 计算、复核、发布、证据导出与页面。"""
import csv
import io

from flask import Blueprint, Response, jsonify, render_template, request

from app.config import CONV_SAMPLE
from app.conv import store
from app.conv.pipeline import calculate
from app.errors import DomainError
from app.numeric import serial

bp = Blueprint('conv', __name__, url_prefix='/conv')

STAGE_NAMES = {1: '数据接收', 2: '转股价裁决', 3: '转股拆分', 4: '整批对账', 5: '复核发布'}


def _payload():
    if request.files and 'file' in request.files:
        from app.reader import decode
        return decode(request.files['file'].read())
    body = request.get_json(silent=True)
    if not body:
        return None                       # 空对象 → 运行样例
    if 'csv' in body:
        return body['csv']
    return body  # 结构化 JSON


@bp.route('/health')
def health():
    return {'status': 'ok'}


@bp.route('/api/run', methods=['POST'])
def run():
    payload = _payload()
    result = calculate() if payload is None else calculate(payload)
    return jsonify(store.save_run(result))


@bp.route('/api/c<int:stage>', methods=['GET', 'POST'])
def stage_only(stage):
    if stage < 1 or stage > 5:
        raise DomainError('STAGE', '模块编号须在1-5之间', 404)
    if request.method == 'GET':
        run = store.latest_run()
        if run is None:
            run = store.save_run(calculate())
        return jsonify(serial(run['stages'][str(stage)]))
    payload = _payload()
    result = calculate() if payload is None else calculate(payload, stop=stage)
    return jsonify(serial(result['stages'][str(stage)]))


@bp.route('/api/runs/<run_id>')
def run_detail(run_id):
    return jsonify(store.get_run(run_id))


@bp.route('/api/history')
def history():
    return jsonify(store.history())


@bp.route('/api/review', methods=['POST'])
def decide():
    body = request.get_json(silent=True) or {}
    try:
        revision = int(body.get('revision', 0))
    except (ValueError, TypeError):
        raise DomainError('VERSION', '版本必须为整数')
    return jsonify(store.review(body.get('run_id'), revision, body.get('decision')))


@bp.route('/api/publish', methods=['POST'])
def publish():
    body = request.get_json(silent=True) or {}
    return jsonify(store.publish(body.get('run_id')))


@bp.route('/api/evidence.csv')
def evidence():
    run_id = request.args.get('run_id')
    run = store.get_run(run_id) if run_id else (store.latest_run() or store.save_run(calculate()))
    out = io.StringIO(newline='')
    rows = []
    for index, stage in run['stages'].items():
        rows.extend({'阶段': 'C%s %s' % (index, stage['name']), **r} for r in stage['rows'])
    rows.extend({'阶段': '转股明细', **r} for r in run['detail'])
    rows.extend({'阶段': '对账-债券维度', **r} for r in run['reconciliation'].get('bond_lines', []))
    fields = list(dict.fromkeys(['阶段', 'run_id', '规则版本'] + [k for r in rows for k in r]))
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    for r in rows:
        writer.writerow({'run_id': run['manifest']['run_id'],
                         '规则版本': run['manifest']['rule_version'], **r})
    return Response('\ufeff' + out.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=conv_evidence.csv'})


@bp.route('/')
@bp.route('/work/<int:stage>')
def work(stage=1):
    if stage < 1 or stage > 5:
        from flask import abort
        abort(404)
    run_id = request.args.get('run_id')
    run = store.get_run(run_id) if run_id else store.latest_run()
    if run is None:
        run = store.save_run(calculate())
    current = run['stages'][str(stage)]
    published = store.is_published(run['manifest']['run_id'])
    sample = CONV_SAMPLE.read_text(encoding='utf-8-sig')
    return render_template('conv/work.html', run=run, current=current, stage=stage,
                           stage_names=STAGE_NAMES, sample=sample, published=published)
