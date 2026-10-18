import pytest
from live import call

pytestmark = [pytest.mark.anyio, pytest.mark.live]


async def test_tb_cheapest(client):
    out = await call(client, "tb_cheapest", {"query": "گوشی سامسونگ A16", "candidates": 2})
    assert out["picks"] and ("cheapest" in out or "note" in out)
    if "cheapest" in out:
        assert out["cheapest"]["best_offer"]["price"] > 0


async def test_tb_compare_and_best_value(client):
    top = await call(client, "tb_best_value", {"budget": 30_000_000, "category": 94, "limit": 3})
    assert top["products"] and all(p["price"] <= 30_000_000 for p in top["products"])
    ids = [p["id"] for p in top["products"][:2]]
    if len(ids) == 2:
        cmp = await call(client, "tb_compare", {"products": ids})
        assert len(cmp["products"]) == 2 and cmp["cheapest"] in ids


async def test_tb_basket(client):
    out = await call(client, "tb_basket", {"items": ["گوشی سامسونگ A16", "گارد گوشی سامسونگ A16"]})
    assert out["items"] and ("split_cart" in out or "no_offer" in out)
