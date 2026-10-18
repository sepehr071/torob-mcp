from torob_mcp.fa import digits, first_int, jalali_to_iso, norm


def test_norm_folds_persian_variants_for_comparison_only():
    assert norm("كتاب  گوشي‌ ۱۲") == "کتاب گوشی 12"
    assert norm("Apple-iPhone 13!") == "apple iphone 13"


def test_digits_and_first_int():
    assert digits("۱۲۳٫۴۵") == "123,45"
    assert first_int("از ۱۱۳٫۰۰۰٫۰۰۰ تومان") == 113_000_000
    assert first_int("در ۱۱ فروشگاه") == 11
    assert first_int("در دیجی اپال") is None


def test_jalali_to_iso():
    assert jalali_to_iso("1405/1/1") == "2026-03-21"  # Nowruz 1405
    assert jalali_to_iso("۴ آبان ۱۴۰۰") == "2021-10-26"
    assert jalali_to_iso("۲۷ شهریور ۱۴۰۵") == "2026-09-18"
    assert jalali_to_iso("۱ اسفند ۱۴۰۳") == "2025-02-19"
    assert jalali_to_iso("not a date") is None
