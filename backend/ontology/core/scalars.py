"""Exact scalar comparison with the same 1,000-digit budget as integer fields."""

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation


def bounded_decimal(value):
    text = str(value)
    if len(text) > 2016:
        raise ValueError("数值文本过长；整数位、小数位及有效数字均不得超过 1000 位")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("无效十进制数") from exc
    if not number.is_finite():
        raise ValueError("数值必须有限")
    if number.is_zero():
        return Decimal(0)
    parts = number.as_tuple()
    if len(parts.digits) > 1000 or number.adjusted() >= 1000 or parts.exponent < -1000:
        raise ValueError("整数位、小数位及有效数字均不得超过 1000 位")
    return number


def canonical_time(kind, value):
    if kind == "date":
        return date.fromisoformat(value).isoformat()
    if re.search(r"[.,]\d{7,}", value):
        raise ValueError("时间精度最多为 6 位小数（微秒），不能截断后用作业务键")
    value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        raise ValueError("时间必须包含时区")
    # Integer arithmetic preserves microseconds and handles timezone offsets
    # at year 1/9999 without overflowing datetime's representable UTC range.
    elapsed = value.replace(tzinfo=None) - datetime(1, 1, 1) - value.utcoffset()
    return str((elapsed.days * 86400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds)
