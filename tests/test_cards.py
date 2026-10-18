import pytest
from conftest import fixture

from torob_mcp.cards import card, cards, seller_row, seller_stats, store_row, to_csv, toman, usable
from torob_mcp.fa import price_from_text


@pytest.mark.parametrize("name", ["search.json", "search_category.json", "search_mapper.json", "similar.json"])
def test_card_price_is_toman_and_matches_price_text(name):
    """Contract: `price` is already Toman. The digits of `price_text` must equal it (no x10, no /10)."""
    raw = fixture(name)["results"]
    assert raw
    for c in raw:
        assert c["price"] == price_from_text(c["price_text"]), c["name1"]
        assert card(c)["price"] == c["price"]


def test_card_fields_and_nulls():
    c = card(fixture("search.json")["results"][0])
    assert c["id"] == "57ea65ae-0798-4cd0-96a7-38d8af180345" and c["shops"] == 11 and c["condition"] == "کارکرده"
    assert c["url"] == "https://torob.com/p/57ea65ae-0798-4cd0-96a7-38d8af180345/" and "ad" not in c
    assert c["badges"] == ["کارکرده"]
    assert card({"random_key": "x", "name1": "n", "price": 0})["price"] is None  # never 0
    assert toman(None) is None and toman("5") is None and toman(12.0) == 12


def test_card_shop_named_instead_of_counted():
    c = card(fixture("search_category.json")["results"][0])
    assert c["shops"] == 1 and c["shop"] == "شیک موبایل تهران"


def test_card_marks_ads_and_skips_junk():
    rows = cards([{"random_key": "a", "name1": "x", "price": 5, "is_adv": True}, {"name1": "no id"}, "junk"])
    assert len(rows) == 1 and rows[0]["ad"] is True


def test_seller_row_and_stats():
    rows = [seller_row(r) for r in fixture("sellers.json")["results"]]
    first = rows[0]
    assert first["shop"] == "باوان موبایل" and first["city"] == "مهاباد" and first["price"] == 113_000_000
    assert first["score"] == 5 and first["buyer_notes"] and first["shipping"]["types"]
    assert "unreliable" not in first and "torob_warranty" not in first
    stats = seller_stats(rows)
    assert stats["min"] == 113_000_000 and stats["max"] == 117_810_000 and stats["usable"] == 3
    assert stats["price_spread_pct"] == 4


def test_unavailable_offer_has_null_price_and_is_not_usable():
    raw = dict(fixture("sellers.json")["results"][0], availability=False)
    row = seller_row(raw)
    assert row["price"] is None and row["in_stock"] is False and usable([row]) == []


def test_unreliable_offers_are_excluded_from_stats():
    good = seller_row(fixture("sellers.json")["results"][0])
    bait = dict(good, price=1_000_000, unreliable=True)
    stats = seller_stats([good, bait])
    assert stats["min"] == good["price"] and stats["unreliable"] == 1 and stats["usable"] == 1


def test_installment_shapes():
    base = fixture("sellers.json")["results"][0]
    assert seller_row(dict(base, installment={"text": "قسطی ۱۲ ماهه"}))["installment"] == "قسطی ۱۲ ماهه"
    assert seller_row(dict(base, installment=True))["installment"] is True
    assert "installment" not in seller_row(base)


def test_store_row():
    row = store_row(fixture("stores.json")["results"][0])
    assert row["shop"] == "لارفون" and row["open"] is False and row["price"] == 55_000_000
    assert row["hours"].startswith("بسته") and len(row["schedule"]) == 7 and row["address"]
    located = store_row(fixture("map_sellers.json")["result"][0])
    assert located["location"].keys() == {"lat", "lon"}


def test_to_csv():
    text = to_csv([{"a": 1, "b": ["x", "y"], "c": None}], ["a", "b", "c"])
    assert text == "a,b,c\n1,x | y,\n"
