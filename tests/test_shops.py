import json

import pytest
from conftest import fixture

pytestmark = pytest.mark.anyio


async def run(client, name, args=None):
    return (await client.call_tool(name, args or {})).structured_content


async def test_tb_shop_is_an_allowlist(client, api):
    api["/v4/internet-shop/details/"] = fixture("shop.json")
    out = await run(client, "tb_shop", {"shop_id": 299463})
    assert api.params() == {"id": "299463"}
    assert out["name"] == "باوان موبایل" and out["city"] == "مهاباد" and out["score"] == 5
    assert out["enamad"]["level"] == "نماد ۱ ستاره" and out["on_torob"] == "۱۰ ماه و ۱ هفته"
    assert out["website"] == "https://bavanmobile.ir" and out["url"] == "https://torob.com/shop/299463"
    assert out["delivery"] and out["payment"] and out["status"] == "فعال"
    assert not {"phone", "address", "billing_info", "customer_support_info", "users"} & out.keys()


async def test_tb_shop_does_not_leak_contact_details(client, api):
    shop = dict(
        fixture("shop.json"), phone="0912", address="street", customer_support_info={"emails": ["a@b.c"]}, credit=5
    )
    api["/v4/internet-shop/details/"] = shop
    text = (await client.call_tool("tb_shop", {"shop_id": 299463})).content[0].text
    assert "0912" not in text and "street" not in text and "a@b.c" not in text


async def test_tb_shop_not_found(client, api):
    api["/v4/internet-shop/details/"] = {"detail": "x"}
    result = await client.call_tool("tb_shop", {"shop_id": 1})
    assert result.is_error and "not found" in result.content[0].text


async def test_tb_find_shops(client, api):
    api["/v4/internet-shop/list/"] = fixture("shop_list.json")
    out = await run(client, "tb_find_shops", {"query": "موبایل"})
    p = api.params()
    assert p["q"] == "موبایل" and p["page"] == "0" and p["shop_type"] == "all"
    assert out["shops"][0] == {
        "id": 299463, "name": "باوان موبایل", "city": "مهاباد", "province": "آذربایجان غربی",
        "domain": "bavanmobile.ir", "score": 5, "url": "https://torob.com/shop/299463",
    }  # fmt: skip
    lar = await run(client, "tb_find_shops", {"query": "موبایل", "city": "لار", "kind": "offline"})
    assert [s["id"] for s in lar["shops"]] == [383332] and api.params()["shop_type"] == "offline"


async def test_tb_shop_products(client, api):
    api["/v4/internet-shop/base-product/list/"] = fixture("shop_products.json")
    out = await run(client, "tb_shop_products", {"shop_id": 299463, "query": "iphone", "sort": "cheapest", "limit": 3})
    p = api.params()
    assert p["shop_id"] == "299463" and p["q"] == "iphone" and p["sort"] == "price" and p["size"] == "3"
    assert len(out["products"]) == 3 and out["price_range"] == [76_300_000, 140_000_000]
    assert out["next_page"] == 1 and out["count"] == 120


async def test_every_tool_output_is_one_compact_json_text(client, api):
    api["/suggestion2/"] = fixture("suggest.json")
    text = (await client.call_tool("tb_suggest", {"text": "x"})).content[0].text
    assert json.loads(text) and "\n" not in text and ": " not in text
