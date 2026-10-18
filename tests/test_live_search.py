import pytest
from live import call

pytestmark = [pytest.mark.anyio, pytest.mark.live]


async def test_tb_suggest(client):
    out = await call(client, "tb_suggest", {"text": "ayfon"})
    assert out["suggestions"]


async def test_tb_search_and_every_filter_narrows(client):
    """Torob ignores a misspelled parameter silently, so each filter must visibly narrow the result."""
    base = await call(client, "tb_search", {"query": "گوشی اپل", "limit": 24})
    assert (
        base["products"] and base["count"] > 0 and all(p["price"] is None or p["price"] > 0 for p in base["products"])
    )
    capped = await call(client, "tb_search", {"query": "گوشی اپل", "max_price": 30_000_000, "limit": 24})
    # Torob stops counting at 1200, so a broad query can be capped on both sides
    assert capped["count"] < base["count"] or base.get("count_capped")
    assert all(p["price"] <= 30_000_000 for p in capped["products"] if p["price"])
    assert "notes" not in capped or "Dropped" not in capped["notes"][0]  # Torob itself applied the bound
    cheap = await call(client, "tb_search", {"query": "گوشی اپل", "sort": "cheapest", "limit": 10})
    prices = [p["price"] for p in cheap["products"] if p["price"]]
    assert prices == sorted(prices)
    used = await call(client, "tb_search", {"query": "گوشی اپل", "condition": "used", "limit": 10})
    assert used["count"] != base["count"] or {p["id"] for p in used["products"]} != {
        p["id"] for p in base["products"][:10]
    }
    page2 = await call(client, "tb_search", {"query": "گوشی اپل", "page": 1, "limit": 10})
    first = await call(client, "tb_search", {"query": "گوشی اپل", "page": 0, "limit": 10})
    assert {p["id"] for p in page2["products"]}.isdisjoint({p["id"] for p in first["products"]})


async def test_tb_search_brand_by_name(client):
    out = await call(client, "tb_search", {"category": 94, "brand": "Apple", "limit": 5})
    assert out["products"] and all(
        "اپل" in (p["name"] or "") or "Apple" in (p["name_en"] or "") for p in out["products"][:3]
    )


async def test_tb_filters(client):
    out = await call(client, "tb_filters", {"category": 94})
    assert out["brands"] and out["attributes"] and out["price_span"][1] > 0


async def test_tb_category(client):
    out = await call(client, "tb_category", {"category": 94, "limit": 5})
    assert out["products"] and out["parents"]


async def test_tb_brands(client):
    out = await call(client, "tb_brands", {"category": 94, "search": "apple"})
    assert out["brands"][0]["id"] == 14


async def test_tb_trends(client):
    assert (await call(client, "tb_trends", {"limit": 5}))["trends"]


async def test_tb_offers(client):
    assert (await call(client, "tb_offers", {}))["rows"]


async def test_tb_cities(client):
    top = await call(client, "tb_cities", {})
    assert 392 in {c["id"] for c in top["cities"]}
    found = await call(client, "tb_cities", {"search": "مشهد"})
    assert any(c["name"] == "مشهد" for c in found["cities"])
