import pytest
from live import call

pytestmark = [pytest.mark.anyio, pytest.mark.live]


async def a_shop_id(client):
    search = await call(client, "tb_search", {"query": "گوشی سامسونگ", "category": 94, "limit": 5})
    prk = next(p["id"] for p in search["products"] if p["price"] and not p.get("ad"))
    sellers = await call(client, "tb_sellers", {"product": prk, "limit": 10})
    return next(o["shop_id"] for o in sellers["offers"] if o.get("shop_id") and o.get("profile"))


async def test_tb_shop(client):
    out = await call(client, "tb_shop", {"shop_id": await a_shop_id(client)})
    assert out["name"] and out["url"].startswith("https://torob.com/shop/")
    assert not {"phone", "address", "billing_info", "customer_support_info"} & out.keys()


async def test_tb_find_shops_and_catalogue(client):
    shop_id = await a_shop_id(client)
    name = (await call(client, "tb_shop", {"shop_id": shop_id}))["name"]
    found = await call(client, "tb_find_shops", {"query": name})
    assert found["shops"]
    catalogue = await call(client, "tb_shop_products", {"shop_id": shop_id, "limit": 5})
    assert catalogue["products"]
