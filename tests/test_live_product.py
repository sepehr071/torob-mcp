import pytest
from live import call

pytestmark = [pytest.mark.anyio, pytest.mark.live]


async def a_product(client):
    out = await call(client, "tb_search", {"query": "گوشی سامسونگ", "category": 94, "limit": 10})
    card = next(p for p in out["products"] if p["price"] and not p.get("ad") and (p.get("shops") or 0) >= 5)
    return card["id"]


async def test_tb_product(client):
    prk = await a_product(client)
    out = await call(client, "tb_product", {"product": prk})
    assert out["id"] == prk and out["price_min"] and out["key_specs"] and out["category"]["path"]


async def test_tb_sellers_and_derived_tools(client):
    prk = await a_product(client)
    out = await call(client, "tb_sellers", {"product": prk, "limit": 5})
    assert out["offers"] and out["stats"]["min"] <= out["stats"]["median"] <= out["stats"]["max"]
    assert all(o["price"] is None or o["price"] > 0 for o in out["offers"])
    inst = await call(client, "tb_sellers", {"product": prk, "installment_only": True})
    assert inst["matching"] <= out["matching"] or inst["matching"] <= out["stats"]["offers"]
    check = await call(client, "tb_deal_check", {"product": prk})
    assert 0 <= check["score"] <= 100 and check["reasons"]
    chart = await call(client, "tb_price_chart", {"product": prk})
    assert chart["summary"]["verdict"] in ("great", "fair", "high") and chart["series"][-1]["date"].startswith("202")
    history = await call(client, "tb_price_history", {"product": prk})
    assert history["lowest"]["price"] <= history["highest"]["price"]


async def test_tb_stores_presence_variants_similar(client):
    prk = await a_product(client)
    assert (await call(client, "tb_stores", {"product": prk}))["count"] >= 0
    assert (await call(client, "tb_presence", {"product": prk}))["online_shops"] >= 1
    assert "variants" in await call(client, "tb_variants", {"product": prk})
    assert (await call(client, "tb_similar", {"product": prk, "limit": 5}))["products"]


async def test_tb_product_url_and_batch(client):
    prk = await a_product(client)
    assert (await call(client, "tb_product_url", {"product": f"/p/{prk}/x/"}))["id"] == prk
    assert (await call(client, "tb_products_batch", {"products": [prk]}))["products"][0]["id"] == prk
