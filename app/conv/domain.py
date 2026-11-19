"""可转债转股：纯函数领域规则（Decimal 精确计算，无浮点）。

核心规则
--------
转换比率 = 债券面值 / 转股价（按高精度除法，最终股数向下取整）。
不足一股部分折算现金 = 不足一股的面值零头 × 转股价 / 债券面值，四舍五入到分。
剩余债券（未转股的整张，按面值 100/张）继续保留，整张不注销。
转股价随分红等公司行动调整，按 *处理日* 生效的转股价裁决；公告前受理、
处理日尚未确认的申报，在处理日按新价执行并显式标注“公告前受理”。
"""
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP, getcontext
from datetime import date

from app.errors import DomainError
from app.numeric import number

getcontext().prec = 50

FACE_VALUE = Decimal('100')          # 每张债券面值（CNY）
CASH_QUANT = Decimal('0.01')         # 现金最小单位：分
PRICE_QUANT = Decimal('0.0001')      # 转股价保留四位小数


def as_day(text):
    try:
        return date.fromisoformat(str(text).strip())
    except (ValueError, TypeError):
        raise DomainError('DATE', '日期格式无效，应为 YYYY-MM-DD')


def quantize_price(value):
    return Decimal(value).quantize(PRICE_QUANT, rounding=ROUND_HALF_UP)


def effective_price(events, bond, processing_day):
    """返回处理日生效的转股价及命中的价格事件。

    价格事件自生效日起适用；取生效日 <= 处理日 的最新一条（按生效日、版本）。
    无任何已生效事件时回退到债券初始转股价。
    """
    initial = number(bond['initial_price'])
    if initial <= 0:
        raise DomainError('PRICE', '初始转股价必须为正数')
    applicable = [e for e in events
                  if e['bond_id'] == bond['bond_id'] and e['eff_date'] <= processing_day]
    if not applicable:
        return initial, None
    chosen = max(applicable, key=lambda e: (e['eff_date'], e['version_n']))
    price = number(chosen['new_price'])
    if price <= 0:
        raise DomainError('PRICE', '调整后转股价必须为正数')
    return quantize_price(price), chosen


def convert(face_amount, price):
    """对一笔申报的转股面值做精确拆分。

    face_amount 为申请转股的债券面值（= 张数 × 100），必须为整张（100 的整数倍）。
    返回：应转股票（整数股）、转股面值、零头面值、折现现金（0.01）、保留面值。
    """
    amount = number(face_amount)
    if amount < 0 or amount % FACE_VALUE != 0:
        raise DomainError('FACE', '转股面值须为 100 的非负整数倍（整张债券）')
    price = quantize_price(price)
    if price <= 0:
        raise DomainError('PRICE', '转股价必须为正数')

    ratio = amount / price                       # 高精度转换比率（含小数股）
    whole_shares = ratio.to_integral_value(rounding=ROUND_FLOOR)
    converted_face = whole_shares * price        # 整数股对应的面值
    fraction_face = amount - converted_face      # 不足一股的面值零头（折现金，不退券）
    # 零头折现金 = 零头面值 / 面值 × 转股价；四舍五入到分
    cash = (fraction_face * price / FACE_VALUE).quantize(CASH_QUANT, rounding=ROUND_HALF_UP)
    # 守恒恒等式：申请面值 = 转股面值 + 零头面值；提交的整张全部被处理，不注销未提交的整张
    return {
        'whole_shares': whole_shares,
        'converted_face': converted_face,
        'fraction_face': fraction_face,
        'cash': cash,
    }


def holding_conservation(holding_face, submitted_face, retained_face):
    """持仓守恒：处理前持仓 = 本次申请转股面值 + 未申请而继续保留的整张面值。"""
    return number(holding_face) == number(submitted_face) + number(retained_face)


def submitted_conservation(submitted_face, converted_face, fraction_face):
    """申报守恒：申请面值 = 整数股转股面值 + 不足一股的零头面值（零头折现金）。"""
    return number(submitted_face) == number(converted_face) + number(fraction_face)
