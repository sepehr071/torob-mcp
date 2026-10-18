import copy

import httpx
import pytest
from conftest import PRK, PRK2, fixture

pytestmark = pytest.mark.anyio

SEARCH = "/v4/base-product/search/"
DETAILS = "/v4/base-product/details/"
SELLERS = "/v4/base-product/sellers/"
CHART = "/v4/base-product/price-chart/"


async def run(client, name, args=None):
    return (await client.call_tool(name, args or {})).structured_content


def offers_by_product(prices):
    """sellers reply per prk: {prk: [(shop_id, shop, price)]}."""

    def reply(request):
        data = copy.deepcopy(fixture("sellers.json"))
        rows = data["results"][:1]
        rows = [copy.deepcopy(rows[0]) for _ in prices[request.url.params["prk"]]]
        for row, (sid, name, price) in zip(rows, prices[request.url.params["prk"]], strict=True):
            row.update(shop_id=sid, shop_name=name, price=price)
        return httpx.Response(200, json={"results": rows, "count": len(rows)})

    return reply


async def test_tb_cheapest_checks_every_shop_and_skips_unreliable(client, api):
    api[SEARCH] = fixture("search.json")
    sellers = copy.deepcopy(fixture("sellers.json"))
    sellers["results"][0].update(price=40_000_000, is_price_unreliable=True)
    api[SELLERS] = sellers
    out = await run(client, "tb_cheapest", {"query": "iphone 13 pro", "candidates": 2})
    p = api.params(0)
    assert p["stock_status"] == "new" and p["q"] == "iphone 13 pro"
    assert len(out["picks"]) == 2 and out["picks"][0]["product"]["id"] == PRK
    assert out["picks"][0]["best_offer"]["price"] == 113_000_000  # the 40M bait offer is skipped
    assert out["cheapest"]["best_offer"]["price"] == 113_000_000
    assert sum(1 for c in api.calls if c.url.path == SELLERS) == 2


async def test_tb_cheapest_installment_and_no_match(client, api):
    api[SEARCH] = fixture("search.json")
    api[SELLERS] = fixture("sellers.json")
    out = await run(client, "tb_cheapest", {"query": "iphone", "installment_only": True, "candidates": 1})
    assert out["picks"][0]["best_offer"] is None and "No usable offer" in out["note"]
    api[SEARCH] = {"results": [], "count": 0}
    result = await client.call_tool("tb_cheapest", {"query": "zzz"})
    assert result.is_error and "No priced product" in result.content[0].text


async def test_tb_compare_shows_only_differences(client, api):
    def reply(request):
        d = copy.deepcopy(fixture("details.json"))
        if request.url.params["prk"] == PRK2:
            d.update(random_key=PRK2, min_price=99_000_000, name1="گوشی دیگر")
            d["key_specs"][0]["items"][0]["value"] = ["128 گیگابایت"]
            d["key_specs"][0]["items"].pop()  # a spec only the first product has
        return httpx.Response(200, json=d)

    api[DETAILS] = reply
    out = await run(client, "tb_compare", {"products": [PRK, PRK2]})
    assert out["cheapest"] == PRK2 and out["products"][1]["cheapest"] is True and "cheapest" not in out["products"][0]
    assert out["differences"]["حافظه داخلی"] == ["256 گیگابایت", "128 گیگابایت"]
    assert list(out["differences"].values()).count(None) == 0 and out["same_specs"] > 5
    assert "حافظه RAM" not in out["differences"]  # identical in both
    assert out["price_gap_pct"] == 14
    same = await client.call_tool("tb_compare", {"products": [PRK, PRK]})
    assert same.is_error and "two different" in same.content[0].text


async def test_tb_best_value_budget(client, api):
    api[SEARCH] = fixture("search.json")  # prices 113M, 76.3M, 140M, 169M, 88.8M
    out = await run(client, "tb_best_value", {"budget": 120_000_000, "query": "iphone", "min_shops": 5})
    p = api.params()
    assert (
        p["price__lt"] == "120000000"
        and p["sort"] == "-price"
        and p["available"] == "true"
        and p["stock_status"] == "new"
    )
    assert all(x["price"] <= 120_000_000 and x["shops"] >= 5 for x in out["products"])
    assert [x["price"] for x in out["products"]] == [113_000_000, 76_300_000, 88_800_000]
    assert out["products"][0]["budget_used_pct"] == 94
    await run(client, "tb_best_value", {"budget": 120_000_000, "category": 94, "rank_by": "cheapest"})
    assert api.params()["sort"] == "price" and api.params()["category"] == "94"
    assert (await client.call_tool("tb_best_value", {"budget": 5000})).is_error
    none = await run(client, "tb_best_value", {"budget": 5_000_000, "query": "iphone", "min_shops": 5})
    assert none["products"] == [] and "No in-stock product" in none["note"]
    assert "note" not in out


async def test_tb_deal_check_uses_history_and_market(client, api):
    api[CHART] = {
        "labels": [f"{d} مهر ۱۴۰۵" for d in range(1, 11)],
        "dataSets": [
            {
                "label": "کمترین قیمت",
                "entries": [
                    {"val": v, "i": i} for i, v in enumerate([130, 125, 120, 118, 116, 115, 114, 113, 113, 113])
                ],
            }
        ],
    }
    api[SELLERS] = fixture("sellers.json")  # cheapest usable offer 113,000,000
    api[CHART]["dataSets"][0]["entries"] = [
        {"val": v * 1_000_000, "i": e["i"]}
        for v, e in zip(
            [130, 125, 120, 118, 116, 115, 114, 113, 113, 113], api[CHART]["dataSets"][0]["entries"], strict=True
        )
    ]
    good = await run(client, "tb_deal_check", {"product": PRK})
    assert good["price"] == 113_000_000 and good["score"] == 100 and good["verdict"] == "good deal"
    assert good["cheapest_offer"]["shop"] == "باوان موبایل" and good["history"]["verdict"] == "great"
    assert any(r.startswith("Judged price is today's cheapest") for r in good["reasons"])
    pricey = await run(client, "tb_deal_check", {"product": PRK, "price": 128_000_000})
    assert pricey["score"] < 40 and pricey["verdict"] == "wait or shop around"
    assert any("above today's cheapest" in r for r in pricey["reasons"])


async def test_tb_deal_check_without_history_is_neutral(client, api):
    api[CHART] = lambda r: httpx.Response(404, json={"message": "no chart"})
    api[SELLERS] = fixture("sellers.json")
    out = await run(client, "tb_deal_check", {"product": PRK})
    assert out["history"] is None and out["score"] == 60 and "No price history" in out["reasons"][0]


async def test_tb_basket_single_shop_and_split_cart(client, api):
    api[SEARCH] = lambda r: httpx.Response(
        200,
        json={
            "results": [
                c
                for c in fixture("search.json")["results"]
                if c["random_key"] == (PRK if "phone" in r.url.params["q"] else PRK2)
            ]
        },
    )
    api[SELLERS] = offers_by_product(
        {
            PRK: [(1, "الف", 100), (2, "ب", 105), (3, "ج", 90)],
            PRK2: [(1, "الف", 50), (2, "ب", 40), (4, "د", 30)],
        }
    )
    out = await run(client, "tb_basket", {"items": ["a phone", "x case"]})
    assert [i["id"] for i in out["items"]] == [PRK, PRK2]
    assert out["single_shop"][0]["shop"] == "ب" and out["single_shop"][0]["total"] == 145  # 105 + 40
    assert [s["total"] for s in out["single_shop"]] == [145, 150]
    assert out["split_cart"]["total"] == 120 and out["split_saves"] == 25  # 90 at ج + 30 at د
    assert {p["shop"] for p in out["split_cart"]["picks"]} == {"ج", "د"}


async def test_tb_basket_accepts_ids_reports_unresolved_and_no_common_shop(client, api):
    api[SEARCH] = {"results": [], "count": 0}
    api[SELLERS] = offers_by_product({PRK: [(1, "الف", 100)], PRK2: [(2, "ب", 50)]})
    out = await run(client, "tb_basket", {"items": [PRK, f"https://torob.com/p/{PRK2}/x/", "ناشناخته"]})
    assert out["unresolved"] == ["ناشناخته"] and out["single_shop"] == [] and "No single shop" in out["note"]
    assert out["split_cart"]["total"] == 150
    assert (await client.call_tool("tb_basket", {"items": ["one"]})).is_error
