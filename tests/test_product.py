import copy

import httpx
import pytest
from conftest import PRK, PRK2, fixture

pytestmark = pytest.mark.anyio

DETAILS = "/v4/base-product/details/"
SELLERS = "/v4/base-product/sellers/"
CHART = "/v4/base-product/price-chart/"


async def run(client, name, args=None):
    return (await client.call_tool(name, args or {})).structured_content


def sellers_reply(request):
    rows = copy.deepcopy(fixture("sellers.json"))
    if request.url.params.get("list_type") == "products_in_store_info":
        return httpx.Response(200, json=fixture("stores.json"))
    return httpx.Response(200, json=rows)


async def test_tb_product(client, api):
    api[DETAILS] = fixture("details.json")
    out = await run(client, "tb_product", {"product": PRK})
    assert api.params()["prk"] == PRK
    assert (
        out["id"] == PRK
        and out["name_en"].startswith("Apple iPhone 13 Pro")
        and out["url"] == f"https://torob.com/p/{PRK}/"
    )
    assert out["price_min"] == 113_000_000 and out["price_max"] == 212_056_000 and out["shops"] == 11
    assert out["online_offers"] == 69 and out["in_person_stores"] == 22 and out["condition"] == "کارکرده"
    assert out["category"] == {"id": 94, "path": ["موبایل و کالای دیجیتال", "گوشی موبایل"]}
    assert out["brand"] == {"id": 14, "name": "اپل (Apple)"}
    assert out["key_specs"]["حافظه داخلی"] == "256 گیگابایت" and "specs" not in out
    assert out["cheapest_offer"]["shop"] == "باوان موبایل" and out["cheapest_offer"]["price"] == 113_000_000
    assert out["variant_groups"] == ["حافظه و رم"] and out["description"]
    full = await run(client, "tb_product", {"product": PRK, "full_specs": True})
    assert "مشخصات کلی" in full["specs"]


async def test_product_reference_forms(client, api):
    api[DETAILS] = fixture("details.json")
    for ref in (PRK, PRK.upper(), f"/p/{PRK}/slug/", f"https://torob.com/p/{PRK}/%DA%AF/?x=1"):
        assert (await run(client, "tb_product", {"product": ref}))["id"] == PRK
    bad = await client.call_tool("tb_product", {"product": "not-a-product-id"})
    assert bad.is_error and "no product id" in bad.content[0].text


async def test_product_not_found(client, api):
    api[DETAILS] = lambda r: httpx.Response(404, json={"message": f"Base product not found: {PRK}"})
    result = await client.call_tool("tb_product", {"product": PRK})
    assert result.is_error and "Base product not found" in result.content[0].text


async def test_tb_product_url_sends_no_request(client, api):
    out = await run(client, "tb_product_url", {"product": f"https://torob.com/p/{PRK}/some-slug/"})
    assert out["url"] == f"https://torob.com/p/{PRK}/" and api.calls == []


async def test_tb_sellers(client, api):
    api[SELLERS] = sellers_reply
    out = await run(client, "tb_sellers", {"product": PRK})
    p = api.params()
    assert p["prk"] == PRK and p["list_type"] == "products_info"
    assert [o["price"] for o in out["offers"]] == [113_000_000, 113_000_000, 117_810_000]
    assert out["stats"]["min"] == 113_000_000 and out["stats"]["median"] == 113_000_000 and out["stats"]["usable"] == 3
    assert out["offers"][0]["buyer_notes"] and out["url"] == f"https://torob.com/p/{PRK}/"


async def test_sellers_filters_and_unreliable_hidden_by_default(client, api):
    rows = copy.deepcopy(fixture("sellers.json"))
    rows["results"][0].update(is_price_unreliable=True, price=50_000_000)
    rows["results"][1].update(installment={"text": "قسطی"}, guarantee_info={"status": "enabled"})
    rows["results"][2].update(shop_score=3.0)
    api[SELLERS] = rows
    out = await run(client, "tb_sellers", {"product": PRK})
    assert (
        out["hidden_unreliable"] == 1
        and all("unreliable" not in o for o in out["offers"])
        and out["stats"]["min"] == 113_000_000
    )
    shown = await run(client, "tb_sellers", {"product": PRK, "include_unreliable": True})
    assert shown["offers"][0]["price"] == 50_000_000 and shown["offers"][0]["unreliable"] is True
    inst = await run(client, "tb_sellers", {"product": PRK, "installment_only": True})
    assert inst["matching"] == 1 and inst["offers"][0]["installment"] == "قسطی"
    war = await run(client, "tb_sellers", {"product": PRK, "warranty_only": True})
    assert war["matching"] == 1 and war["offers"][0]["torob_warranty"] is True
    score = await run(client, "tb_sellers", {"product": PRK, "min_score": 4})
    assert score["matching"] == 1  # the unreliable 5-star is hidden, the 3.0 shop is cut
    limited = await run(client, "tb_sellers", {"product": PRK, "limit": 1})
    assert len(limited["offers"]) == 1


async def test_sellers_csv_and_out_of_stock_null_price(client, api):
    rows = copy.deepcopy(fixture("sellers.json"))
    rows["results"][0]["availability"] = False
    api[SELLERS] = rows
    out = await run(client, "tb_sellers", {"product": PRK, "output": "csv"})
    lines = out["csv"].splitlines()
    assert lines[0].startswith("shop,city,price") and len(lines) == 4
    assert lines[-1].split(",")[2] == ""  # the unavailable offer sorts last with an empty price, not 0


async def test_sellers_city_by_name(client, api):
    api[SELLERS] = fixture("sellers.json")
    api["/v4/city/list/"] = fixture("cities.json")
    await run(client, "tb_sellers", {"product": PRK, "city": "لار"})
    assert api.calls[-1].headers["cookie"] == "deliver_city=712"


async def test_tb_stores(client, api):
    api[SELLERS] = sellers_reply
    out = await run(client, "tb_stores", {"product": PRK})
    assert api.params()["list_type"] == "products_in_store_info"
    assert out["count"] == 22 and out["open_now"] == 0 and out["stores"][0]["shop"] == "لارفون"
    assert out["stores"][0]["address"] and out["stores"][0]["price"] == 55_000_000


async def test_tb_stores_with_location_uses_the_map_endpoint(client, api):
    api["/v4/base-product/map/sellers/"] = fixture("map_sellers.json")
    out = await run(client, "tb_stores", {"product": PRK, "with_location": True})
    assert out["stores"][0]["location"].keys() == {"lat", "lon"} and api.params()["prk"] == PRK


async def test_tb_presence(client, api):
    api[DETAILS] = fixture("details.json")
    out = await run(client, "tb_presence", {"product": PRK})
    assert out["online_shops"] == 69 and out["in_person_stores"] == 22
    assert out["installment_shops"] == 2 and out["torob_warranty_shops"] == 3
    assert out["price_min"] == 113_000_000 and out["price_max"] == 212_056_000


async def test_tb_variants(client, api):
    api[DETAILS] = fixture("details.json")
    out = await run(client, "tb_variants", {"product": PRK})
    group = out["variants"][0]
    assert group["group"] == "حافظه و رم" and group["options"] and group["options"][0]["id"]
    assert all(o["price"] is None or isinstance(o["price"], int) for o in group["options"])
    assert any(o.get("selected") for o in group["options"])


async def test_tb_price_chart(client, api):
    api[CHART] = fixture("price_chart.json")
    out = await run(client, "tb_price_chart", {"product": PRK})
    s = out["summary"]
    assert s["points"] >= 40 and s["current"] > 0 and s["verdict"] in ("great", "fair", "high")
    assert s["period_low"] <= s["current"] <= s["period_high"] and 0 <= s["percentile"] <= 100
    assert s["since"] and s["since"].startswith("202")
    assert len(out["series"]) == 26 and out["series"][-1]["date"] == "2026-09-18"
    assert out["series"][-1]["min"] == s["current"]
    short = await run(client, "tb_price_chart", {"product": PRK, "points": 4})
    assert len(short["series"]) == 4


def chart_with(values):
    labels = [f"{d} مهر ۱۴۰۵" for d in range(1, len(values) + 1)]
    entries = [{"val": v, "i": i} for i, v in enumerate(values)]
    return {
        "labels": labels,
        "dataSets": [{"label": "میانگین قیمت", "entries": entries}, {"label": "کمترین قیمت", "entries": entries}],
    }


@pytest.mark.parametrize(
    ("values", "verdict", "pct"),
    [
        ([100, 110, 120, 130, 140, 100], "great", 0),
        ([100, 90, 95, 105, 110, 100], "fair", 33),
        ([100, 90, 80, 85, 95, 140], "high", 83),
    ],
)
async def test_verdict_by_percentile(client, api, values, verdict, pct):
    api[CHART] = chart_with(values)
    s = (await run(client, "tb_price_chart", {"product": PRK}))["summary"]
    assert s["verdict"] == verdict and s["percentile"] == pct


async def test_no_chart(client, api):
    api[CHART] = {"labels": [], "dataSets": []}
    result = await client.call_tool("tb_price_chart", {"product": PRK})
    assert result.is_error and "no price chart" in result.content[0].text


async def test_tb_price_history(client, api):
    api[CHART] = chart_with([100, 100, 90, 91, 120, 120])
    out = await run(client, "tb_price_history", {"product": PRK, "min_change_pct": 5})
    assert out["current"] == 120 and out["lowest"]["price"] == 90 and out["highest"]["price"] == 120
    assert [e["kind"] for e in out["events"]] == ["rise", "drop"] and out["events"][0]["change_pct"] == 32
    assert out["last_change"] == out["events"][0] and out["lowest"]["date"] == "2026-09-25"
    quiet = await run(client, "tb_price_history", {"product": PRK, "min_change_pct": 90})
    assert quiet["events"] == [] and quiet["last_change"] is None


async def test_tb_similar(client, api):
    api["/v4/base-product/similar-base-product/"] = fixture("similar.json")
    out = await run(client, "tb_similar", {"product": PRK, "limit": 2})
    assert api.params()["prk"] == PRK and len(out["products"]) == 2 and out["products"][0]["ad"] is True
    assert out["products"][0]["price"] == 197_000_000


async def test_tb_products_batch(client, api):
    def reply(request):
        if request.url.params["prk"] == PRK2:
            return httpx.Response(404, json={"message": "Base product not found"})
        return httpx.Response(200, json=fixture("details.json"))

    api[DETAILS] = reply
    out = await run(client, "tb_products_batch", {"products": [PRK, PRK, PRK2]})
    assert [p["id"] for p in out["products"]] == [PRK] and out["errors"][0]["id"] == PRK2
    assert len(api.calls) == 2  # duplicate ids are fetched once
    too_many = await client.call_tool("tb_products_batch", {"products": [PRK] * 11})
    assert too_many.is_error
