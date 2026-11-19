"""可转债转股输入解析。

支持两种输入（均离线、确定性）：
1. JSON 对象：{"processing_date": "...", "bonds": [...], "price_events": [...], "instructions": [...]}
2. 组合 CSV：首列为 section（meta / bond / price_event / instruction），其后按各实体列对齐。
   meta 行的处理日写在 event_id 列。

列定义（组合 CSV 表头，顺序固定，允许缺省列以空值处理）：
section, bond_id, name, initial_price, event_id, eff_date, new_price, reason, version,
instruction_id, account, received_at, confirmed, face_amount, holding_face
"""
import csv
import io
import unicodedata
from decimal import Decimal

from app.errors import DomainError
from app.numeric import number
from app.conv.domain import as_day

BOND_COLS = ['bond_id', 'name', 'initial_price']
EVENT_COLS = ['event_id', 'eff_date', 'new_price', 'reason', 'version']
INSTR_COLS = ['instruction_id', 'account', 'received_at', 'confirmed',
              'face_amount', 'holding_face']
CSV_HEAD = ['section'] + BOND_COLS + EVENT_COLS + INSTR_COLS


def _clean(value):
    return unicodedata.normalize('NFKC', str(value if value is not None else '')).strip()


def parse_payload(payload):
    """归一为 dict 结构。payload 可为 dict（JSON）或 str（组合 CSV）。"""
    if payload is None:
        raise DomainError('EMPTY', '输入为空')
    if isinstance(payload, dict):
        return {
            'processing_date': _clean(payload.get('processing_date')),
            'bonds': list(payload.get('bonds') or []),
            'price_events': list(payload.get('price_events') or []),
            'instructions': list(payload.get('instructions') or []),
        }
    if isinstance(payload, str):
        return _parse_csv(payload)
    raise DomainError('TYPE', '输入须为 JSON 对象或组合 CSV 文本')


def _parse_csv(text):
    text = text.lstrip('\ufeff')
    if not text.strip():
        raise DomainError('EMPTY', '输入为空')
    reader = csv.reader(io.StringIO(text, newline=''), strict=True)
    try:
        head = next(reader, None)
    except csv.Error:
        raise DomainError('STRUCTURE', '表头损坏')
    if head is None:
        raise DomainError('BLANK', '文件仅有空行')
    head = [h.strip() for h in head]
    if head != CSV_HEAD:
        raise DomainError('HEADER', '组合CSV字段缺失或顺序不符，应为：' + ','.join(CSV_HEAD))
    bonds, events, instructions = [], [], []
    processing_date = ''
    for cells in reader:
        if not cells or all(not c.strip() for c in cells):
            continue
        if len(cells) != len(head):
            raise DomainError('COLUMNS', '列数不符（行 %d）' % reader.line_num)
        row = dict(zip(head, [c.strip() for c in cells]))
        section = row.pop('section')
        entity = {k: row[k] for k in row if row[k] != ''}
        if section == 'meta':
            # 处理日写在 event_id 列，避免新增列
            processing_date = row.get('event_id', '')
        elif section == 'bond':
            bonds.append({k: entity.get(k, '') for k in BOND_COLS})
        elif section == 'price_event':
            events.append({k: entity.get(k, '') for k in EVENT_COLS if k in entity}
                          | {'bond_id': row.get('bond_id', '')})
        elif section == 'instruction':
            instructions.append({k: entity.get(k, '') for k in INSTR_COLS if k in entity}
                                | {'bond_id': row.get('bond_id', '')})
        else:
            raise DomainError('SECTION', '未知 section：%s（行 %d）' % (section, reader.line_num))
    return {'processing_date': processing_date, 'bonds': bonds,
            'price_events': events, 'instructions': instructions}


def build_models(data):
    """校验并构建债券、价格事件、申报三类模型；返回 (bonds, events, instructions)。"""
    processing_day = as_day(data.get('processing_date') or '')
    bonds = {}
    for raw in data['bonds']:
        bid = _clean(raw.get('bond_id'))
        if not bid:
            raise DomainError('MISSING', '债券代码为空')
        if bid in bonds:
            raise DomainError('CONFLICT', '债券重复定义：%s' % bid)
        price = number(raw.get('initial_price'))
        if price <= 0:
            raise DomainError('PRICE', '初始转股价必须为正数')
        bonds[bid] = {'bond_id': bid, 'name': _clean(raw.get('name')) or bid,
                      'initial_price': price, 'initial_price_text': str(price)}

    events = []
    for raw in data['price_events']:
        bid = _clean(raw.get('bond_id'))
        if bid not in bonds:
            raise DomainError('BOND', '价格事件引用未知债券：%s' % bid)
        try:
            version = int(_clean(raw.get('version')) or '1')
        except ValueError:
            raise DomainError('VERSION', '价格事件版本须为整数')
        new_price = number(raw.get('new_price'))
        if new_price <= 0:
            raise DomainError('PRICE', '调整后转股价必须为正数')
        events.append({'bond_id': bid, 'event_id': _clean(raw.get('event_id')),
                       'eff_date': as_day(raw.get('eff_date')),
                       'new_price': new_price, 'reason': _clean(raw.get('reason')),
                       'version_n': version})

    instructions = []
    seen = set()
    for raw in data['instructions']:
        instr_id = _clean(raw.get('instruction_id'))
        bid = _clean(raw.get('bond_id'))
        if not instr_id:
            raise DomainError('MISSING', '申报号为空')
        if not bid or bid not in bonds:
            raise DomainError('BOND', '申报 %s 引用未知债券：%s' % (instr_id, bid))
        if instr_id in seen:
            raise DomainError('DUPLICATE', '同批次申报号重复：%s' % instr_id)
        seen.add(instr_id)
        face = number(raw.get('face_amount'))
        if face < 0 or face % Decimal('100') != 0:
            raise DomainError('FACE', '申报 %s 转股面值须为100的非负整数倍' % instr_id)
        holding = number(raw.get('holding_face'))
        if holding < 0 or holding % Decimal('100') != 0:
            raise DomainError('FACE', '申报 %s 持仓面值须为100的非负整数倍' % instr_id)
        if face > holding:
            raise DomainError('RANGE', '申报 %s 转股面值超过持仓' % instr_id)
        confirmed = _clean(raw.get('confirmed')).lower() in ('1', 'true', 'yes', 'y', '是', '已确认')
        instructions.append({
            'instruction_id': instr_id, 'bond_id': bid,
            'account': _clean(raw.get('account')),
            'received_at': as_day(raw.get('received_at')),
            'confirmed': confirmed,
            'face_amount': face, 'holding_face': holding,
        })
    return processing_day, bonds, events, instructions
