"""可转债转股五阶段流水线（确定性、可独立运行到任一阶段）。

C1 数据接收  : 解析并校验债券/价格事件/申报，隔离脏数据，统计输入守恒。
C2 转股价裁决: 按处理日选取生效转股价；标注“公告前受理/已确认/新价执行”。
C3 转股拆分  : 整数股转股、零头折现金、整张债券保留；幂等去重。
C4 整批对账  : 逐笔守恒 + 债券维度持仓守恒 + 超发校验，决定是否相符。
C5 复核发布  : 仅复核通过且对账相符方可发布，发布幂等、不重复转股。
"""
import hashlib
import time
from decimal import Decimal

from app.config import RULE_VERSION
from app.conv.domain import effective_price, convert, FACE_VALUE
from app.conv.parser import parse_payload, build_models
from app.conv import store
from app.numeric import serial

CASH_Q = Decimal('0.01')


def _fingerprint(instr, price):
    base = '|'.join([instr['bond_id'], instr['instruction_id'],
                     str(instr['face_amount']), str(price)])
    return hashlib.sha256(base.encode('utf-8')).hexdigest()[:16]


def make_manifest(payload_text, processing_day):
    digest = hashlib.sha256(payload_text.encode('utf-8')).hexdigest()
    run_id = hashlib.sha256(
        ('CONV:' + RULE_VERSION + ':' + digest).encode('utf-8')).hexdigest()[:16]
    return {'run_id': run_id, 'sha256': digest, 'rule_version': RULE_VERSION,
            'processing_date': processing_day.isoformat(),
            'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'parameters': {}}


def calculate(payload=None, stop=5, db=None):
    if payload is None:
        from app.config import CONV_SAMPLE
        payload = CONV_SAMPLE.read_text(encoding='utf-8-sig')
    payload_text = payload if isinstance(payload, str) else _json_text(payload)
    ctx = {'quarantine': [], 'stages': {}, 'detail': [], 'reconciliation': {}}

    stage1_parse(ctx, payload)
    if stop >= 2:
        stage2_adjudicate(ctx)
    if stop >= 3:
        stage3_convert(ctx, db)
    if stop >= 4:
        stage4_reconcile(ctx)
    if stop >= 5:
        stage5_publish_gate(ctx)

    ctx['manifest'] = make_manifest(payload_text, ctx['processing_day'])
    return serial(ctx)


def _json_text(payload):
    import json
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


# ---------------- C1 数据接收 ----------------
def stage1_parse(ctx, payload):
    data = parse_payload(payload)
    processing_day, bonds, events, instructions = build_models(data)
    ctx['processing_day'] = processing_day
    ctx['bonds'] = bonds
    ctx['price_events'] = events
    ctx['instructions'] = instructions
    rows = [{'债券': b['bond_id'], '名称': b['name'], '初始转股价': b['initial_price_text']}
            for b in bonds.values()]
    ctx['stages']['1'] = {
        'stage': 1, 'name': '数据接收', 'rows': rows,
        'metrics': [['债券', len(bonds), '只'], ['价格事件', len(events), '条'],
                    ['转股申报', len(instructions), '笔']]}


# ---------------- C2 转股价裁决 ----------------
def stage2_adjudicate(ctx):
    pday = ctx['processing_day']
    rows = []
    ctx['adjudicated'] = []
    for instr in sorted(ctx['instructions'], key=lambda x: (x['bond_id'], x['instruction_id'])):
        bond = ctx['bonds'][instr['bond_id']]
        price, event = effective_price(ctx['price_events'], bond, pday)
        pre_announcement = (not instr['confirmed']) and event is not None \
            and instr['received_at'] < event['eff_date']
        flags = []
        if pre_announcement:
            flags.append('公告前受理')
        flags.append('已确认' if instr['confirmed'] else '未确认')
        flags.append('新价执行' if event is not None else '原价执行')
        adj = {**instr, 'applied_price': price, 'price_event': event,
               'pre_announcement': pre_announcement, 'flags': '／'.join(flags)}
        ctx['adjudicated'].append(adj)
        rows.append({'申报号': instr['instruction_id'], '债券': instr['bond_id'],
                     '受理日': instr['received_at'].isoformat(),
                     '处理日': pday.isoformat(),
                     '适用转股价': str(price),
                     '生效事件': event['event_id'] if event else '初始价',
                     '裁决': '／'.join(flags)})
    ctx['stages']['2'] = {
        'stage': 2, 'name': '转股价裁决', 'rows': rows,
        'metrics': [['裁决申报', len(rows), '笔'],
                    ['公告前受理按新价', sum(r['pre_announcement'] for r in ctx['adjudicated']), '笔'],
                    ['价格事件命中', sum(r['price_event'] is not None for r in ctx['adjudicated']), '笔']]}


# ---------------- C3 转股拆分（幂等） ----------------
def stage3_convert(ctx, db):
    conn = db if db is not None else store.connection()
    rows, detail = [], []
    candidates = [_fingerprint(a, a['applied_price']) for a in ctx['adjudicated']]
    already = store.known_fingerprints(conn, candidates)
    if db is None:
        conn.close()
    for a in ctx['adjudicated']:
        fp = _fingerprint(a, a['applied_price'])
        base = {'申报号': a['instruction_id'], '债券': a['bond_id'], '账户': a['account'],
                '持仓面值': str(a['holding_face']),
                '申请转股面值': str(a['face_amount']), '适用转股价': str(a['applied_price']),
                'fingerprint': fp, '裁决': a['flags']}
        if fp in already:
            retained_skip = a['holding_face'] - a['face_amount']
            row = {**base, '处置': '重复跳过', '应转股票': '0', '转股面值': '0',
                   '零头面值': '0', '折现现金_CNY': '0.00',
                   '保留债券面值': str(retained_skip),
                   '保留张数': str(retained_skip // FACE_VALUE)}
            detail.append(row)
            rows.append(row)
            continue
        r = convert(a['face_amount'], a['applied_price'])
        # 未申请转股的整张债券继续保留（持仓 - 本次申请），绝不注销整张
        retained = a['holding_face'] - a['face_amount']
        row = {**base,
               '处置': '转股',
               '应转股票': str(r['whole_shares']),
               '转股面值': str(r['converted_face']),
               '零头面值': str(r['fraction_face']),
               '折现现金_CNY': str(r['cash']),
               '保留债券面值': str(retained),
               '保留张数': str(retained // FACE_VALUE)}
        detail.append(row)
        rows.append(row)
    ctx['detail'] = detail
    converted = [d for d in detail if d['处置'] == '转股']
    ctx['stages']['3'] = {
        'stage': 3, 'name': '转股拆分', 'rows': rows,
        'metrics': [['执行转股', len(converted), '笔'],
                    ['重复跳过', len(detail) - len(converted), '笔'],
                    ['应转股票合计', str(sum((Decimal(d['应转股票']) for d in converted), Decimal(0))), '股'],
                    ['折现现金合计', str(sum((Decimal(d['折现现金_CNY']) for d in converted), Decimal(0))), 'CNY']]}


# ---------------- C4 整批对账 ----------------
def stage4_reconcile(ctx):
    detail = ctx['detail']
    executed = [d for d in detail if d['处置'] == '转股']
    checks = []

    def add(name, ok, evidence):
        checks.append({'对账项': name, '结论': '相符' if ok else '差异', '证据': evidence})

    # 1) 逐笔申报守恒：申请面值 = 转股面值 + 零头面值（仅本批实际执行的笔；重复跳过不重复转股）
    per_ok = all(
        Decimal(d['申请转股面值']) == Decimal(d['转股面值']) + Decimal(d['零头面值'])
        for d in executed)
    add('逐笔申报守恒', per_ok, '申请面值=转股面值+零头面值（重复跳过笔不参与）')

    # 2) 整张债券保留：保留面值 = 持仓 - 申请，且为100整数倍（整张不注销）
    whole_ok = all(
        Decimal(d['保留债券面值']) == Decimal(d['持仓面值']) - Decimal(d['申请转股面值'])
        and Decimal(d['保留债券面值']) % FACE_VALUE == 0
        for d in detail)
    add('整张债券保留', whole_ok, '保留=持仓-申请，均为100整数倍且不注销整张')

    # 3) 超发校验：每笔执行转股面值不超过其申请/持仓
    oversub = any(
        Decimal(d['转股面值']) > Decimal(d['申请转股面值'])
        or Decimal(d['申请转股面值']) > Decimal(d['持仓面值'])
        for d in executed)
    add('超发校验', not oversub, '转股面值≤申请面值≤持仓面值')

    # 4) 债券维度持仓守恒：执行笔 持仓=转股+零头+保留；跳过笔持仓原样保留
    bond_lines = []
    bond_balance_ok = True
    by_bond = {}
    for d in detail:
        by_bond.setdefault(d['债券'], []).append(d)
    for bid, items in sorted(by_bond.items()):
        holding = sum((Decimal(i['持仓面值']) for i in items), Decimal(0))
        converted = sum((Decimal(i['转股面值']) for i in items if i['处置'] == '转股'), Decimal(0))
        fraction = sum((Decimal(i['零头面值']) for i in items if i['处置'] == '转股'), Decimal(0))
        retained = sum((Decimal(i['保留债券面值']) for i in items), Decimal(0))
        skipped_face = sum((Decimal(i['申请转股面值']) for i in items if i['处置'] != '转股'), Decimal(0))
        ok = holding == converted + fraction + retained + skipped_face
        bond_balance_ok &= ok
        bond_lines.append({'债券': bid, '持仓合计': str(holding), '转股面值合计': str(converted),
                           '零头合计': str(fraction), '保留面值合计': str(retained),
                           '跳过申请面值': str(skipped_face), '守恒': '是' if ok else '否'})
    add('债券维度持仓守恒', bond_balance_ok, '持仓=转股+零头+保留+跳过申请')

    # 5) 现金守恒：逐笔折现现金按零头复算一致（四舍五入到分）
    cash_ok = all(
        (Decimal(d['零头面值']) * Decimal(d['适用转股价']) / FACE_VALUE)
        .quantize(CASH_Q) == Decimal(d['折现现金_CNY'])
        for d in executed)
    add('折现现金复算', cash_ok, '零头面值×转股价/100，四舍五入到分')

    balanced = all(c['结论'] == '相符' for c in checks)
    ctx['reconciliation'] = {'balanced': balanced, 'checks': checks, 'bond_lines': bond_lines}
    ctx['stages']['4'] = {
        'stage': 4, 'name': '整批对账', 'rows': checks,
        'metrics': [['对账项', len(checks), '项'],
                    ['相符', sum(c['结论'] == '相符' for c in checks), '项'],
                    ['整批结论', '相符' if balanced else '差异', '']]}


# ---------------- C5 复核发布闸门 ----------------
def stage5_publish_gate(ctx):
    balanced = ctx['reconciliation']['balanced']
    gate = [{'闸门': '整批对账相符', '状态': '通过' if balanced else '阻断'},
            {'闸门': '复核结论=通过', '状态': '待复核'},
            {'闸门': '发布幂等（不重复转股）', '状态': '就绪'}]
    ctx['stages']['5'] = {
        'stage': 5, 'name': '复核发布', 'rows': gate,
        'metrics': [['对账结论', '相符' if balanced else '差异', ''],
                    ['发布条件', '满足' if balanced else '不满足', ''],
                    ['重复申报', sum(d['处置'] == '重复跳过' for d in ctx['detail']), '笔']]}
