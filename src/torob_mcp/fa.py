"""Persian text helpers: normalization for comparing, digits, Jalali dates."""

from __future__ import annotations

import re

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫٬", "01234567890123456789,,")
_FOLD = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه", "\u200c": " ", "\u200f": "", "\u200e": ""})
_MONTHS = {
    "فروردین": 1, "اردیبهشت": 2, "خرداد": 3, "تیر": 4, "مرداد": 5, "شهریور": 6,
    "مهر": 7, "آبان": 8, "آذر": 9, "دی": 10, "بهمن": 11, "اسفند": 12,
}  # fmt: skip


def digits(text: str) -> str:
    """Persian and Arabic-Indic digits (and the Persian thousands/decimal marks) to ASCII."""
    return text.translate(_DIGITS)


def norm(text: object) -> str:
    """Fold a string for comparison only: ASCII digits, Persian yeh/kaf, ZWNJ to space, lowercase, one space."""
    s = digits(str(text or "")).translate(_FOLD).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip()


def first_int(text: object) -> int | None:
    """The first integer in a Persian string such as 'در ۱۱ فروشگاه' (None when there is none)."""
    m = re.search(r"\d[\d,]*", digits(str(text or "")))
    return int(m.group().replace(",", "")) if m else None


def price_from_text(text: object) -> int | None:
    """Toman from a display string such as 'از ۱۱۳٫۰۰۰٫۰۰۰ تومان' (the unit check for `price`)."""
    return first_int(text)


def jalali_to_iso(label: str) -> str | None:
    """'۴ آبان ۱۴۰۰' (or '1400/08/04') to '2021-10-26'; None when it is not a Jalali date."""
    s = digits(label).strip()
    m = re.match(r"(\d{1,2}) (\S+) (\d{4})$", s)
    if m and m.group(2) in _MONTHS:
        jd, jm, jy = int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3))
    elif m2 := re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})$", s):
        jy, jm, jd = (int(g) for g in m2.groups())
    else:
        return None
    # standard Jalali -> Gregorian conversion (jalaali algorithm, 33-year arithmetic form)
    jy += 1595
    days = -355668 + 365 * jy + (jy // 33) * 8 + ((jy % 33 + 3) // 4) + jd
    days += (jm - 1) * 31 if jm < 7 else (jm - 7) * 30 + 186
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd = days + 1
    mdays = [
        0,
        31,
        29 if (gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0 else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ]
    gm = 1
    while gm < 13 and gd > mdays[gm]:
        gd -= mdays[gm]
        gm += 1
    return f"{gy:04d}-{gm:02d}-{gd:02d}"
