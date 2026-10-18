import copy

import httpx
import pytest
from conftest import fixture

from torob_mcp import http

pytestmark = pytest.mark.anyio

SEARCH = "/v4/base-product/search/"
BRANDS = "/v4/brand/list/"


async def run(client, name, args=None):
    return (await client.call_tool(name, args or {})).structured_content


async def test_tb_search_basic(client, api):
    api[SEARCH] = fixture("search.json")
    out = await run(client, "tb_search", {"query": "iphone", "limit": 5})
    p = api.params()
    assert p["q"] == "iphone" and p["query"] == "iphone" and p["size"] == "5" and p["page"] == "0" and p["sort"] == ""
    assert p["source"] == "next_desktop"
    assert out["count"] == 1200 and out["count_capped"] is True and len(out["products"]) == 5
    assert out["products"][0]["price"] == 113_000_000 and out["products"][0]["shops"] == 11
    assert out["price_span"][1] > out["price_span"][0] and "next_page" not in out
    assert "low_confidence" not in out


async def test_every_filter_is_sent_under_the_name_torob_understands(client, api):
    """Torob ignores unknown parameter names silently, so the exact names matter."""
    api[SEARCH] = fixture("search_category.json")
    await run(
        client,
        "tb_search",
        {
            "query": "iphone", "category": 94, "sort": "cheapest", "min_price": 1_000_000, "max_price": 30_000_000,
            "available_only": True, "condition": "used", "in_person": True, "installment": True, "page": 2,
        },
    )  # fmt: skip
    p = api.params()
    assert (
        p["category"] == "94" and p["sort"] == "price" and p["price__gt"] == "1000000" and p["price__lt"] == "30000000"
    )
    assert p["available"] == "true" and p["stock_status"] == "stock" and p["shop_type"] == "offline"
    assert p["torobpay"] == "true" and p["page"] == "2"
    for name, value in {"priciest": "-price", "newest": "-date", "most_sellers": "-supply", "popular": ""}.items():
        await run(client, "tb_search", {"query": "iphone", "sort": name})
        assert api.params()["sort"] == value
    await run(client, "tb_search", {"query": "iphone", "condition": "new"})
    assert api.params()["stock_status"] == "new"
    http._cache.clear()  # an identical earlier request would be answered from the cache
    await run(client, "tb_search", {"query": "iphone", "condition": "any"})
    assert "stock_status" not in api.params() and "available" not in api.params() and "price__lt" not in api.params()


async def test_price_filter_guard_drops_cards_torob_did_not_filter(client, api):
    api[SEARCH] = fixture("search.json")  # prices 113M, 76.3M, 140M, 169M, 88.8M
    out = await run(client, "tb_search", {"query": "iphone", "max_price": 100_000_000})
    assert [p["price"] for p in out["products"]] == [76_300_000, 88_800_000]
    assert "Dropped 3 cards" in out["notes"][0]


async def test_search_needs_a_query_or_category(client, api):
    result = await client.call_tool("tb_search", {})
    assert result.is_error and "query, a category" in result.content[0].text


async def test_brand_name_is_mapped_to_an_id(client, api):
    api[SEARCH] = fixture("search_category.json")
    api[BRANDS] = fixture("brands.json")
    await run(client, "tb_search", {"category": 94, "brand": "Apple"})
    assert api.params()["brand"] == "14"
    assert api.params(0)["size"] == "1"  # the probe
    brand_calls = [c for c in api.calls if c.url.path == BRANDS]
    assert brand_calls[0].url.params["cat_list"] == "94"
    await run(client, "tb_search", {"category": 94, "brand": "سامسونگ"})
    assert api.params()["brand"] == "5"
    await run(client, "tb_search", {"category": 94, "brand": 102})  # an id needs no lookup
    assert api.params()["brand"] == "102"


async def test_unknown_brand_is_refused_with_the_real_list(client, api):
    api[SEARCH] = fixture("search_category.json")
    api[BRANDS] = fixture("brands.json")
    result = await client.call_tool("tb_search", {"category": 94, "brand": "Nonexistent"})
    assert (
        result.is_error
        and "Unknown brand 'Nonexistent'" in result.content[0].text
        and "اپل (14)" in result.content[0].text
    )
    assert not [c for c in api.calls if c.url.params.get("brand")]  # the filtered search was never sent


async def test_attribute_filters_are_validated(client, api):
    api[SEARCH] = fixture("search_category.json")
    await run(client, "tb_search", {"category": 94, "filters": {"storage": "256 gb"}})
    assert api.params()["storage"] == "256 gb"
    await run(client, "tb_search", {"category": 94, "filters": {"storage": "1 ترابایت"}})  # display name -> value
    assert api.params()["storage"] == "1 tb"
    bad_slug = await client.call_tool("tb_search", {"category": 94, "filters": {"storge": "256 gb"}})
    assert (
        bad_slug.is_error
        and "Unknown filter 'storge'" in bad_slug.content[0].text
        and "storage" in bad_slug.content[0].text
    )
    bad_value = await client.call_tool("tb_search", {"category": 94, "filters": {"storage": "3 gb"}})
    assert bad_value.is_error and "not a value of filter 'storage'" in bad_value.content[0].text
    reserved = await client.call_tool("tb_search", {"category": 94, "filters": {"sort": "price"}})
    assert reserved.is_error and "own parameter" in reserved.content[0].text


async def test_weak_results_set_low_confidence_unmatched_terms_and_suggestions(client, api):
    api[SEARCH] = fixture("search.json")  # iPhone 13 Pro cards
    api["/suggestion2/"] = fixture("suggest.json")
    out = await run(client, "tb_search", {"query": "zzzzz qqqqq iphone"})
    assert out["low_confidence"] is True and out["unmatched_terms"] == ["zzzzz", "qqqqq"]
    assert out["suggested_queries"][0] == "ayfon 13" and len(out["suggested_queries"]) == 5


async def test_empty_result_notes_and_suggestions(client, api):
    api[SEARCH] = {"results": [], "count": 0, "next": None, "spellcheck": {}}
    api["/suggestion2/"] = fixture("suggest.json")
    out = await run(client, "tb_search", {"query": "qwertyuiop"})
    assert out["products"] == [] and out["low_confidence"] is True and "No results" in out["notes"][0]
    assert out["suggested_queries"]


async def test_spellcheck_is_reported(client, api):
    data = copy.deepcopy(fixture("search.json"))
    data["spellcheck"] = {"is_spellchecked": True, "initial_query": "ayfon", "corrected_query": "iphone"}
    api[SEARCH] = data
    out = await run(client, "tb_search", {"query": "ayfon"})
    assert out["spellcheck"] == "iphone" and "instead of 'ayfon'" in out["notes"][0]


async def test_csv_output(client, api):
    api[SEARCH] = fixture("search.json")
    out = await run(client, "tb_search", {"query": "iphone", "output": "csv"})
    lines = out["csv"].splitlines()
    assert lines[0] == "id,name,price,shops,condition,url" and len(lines) == 6 and "113000000" in lines[1]


async def test_city_name_is_resolved_and_sent(client, api):
    api[SEARCH] = fixture("search.json")
    api["/v4/city/list/"] = {"count": 1, "results": [{"id": 468, "name": "مشهد"}]}
    await run(client, "tb_search", {"query": "iphone", "city": "مشهد"})
    assert api.params(0)["search"] == "مشهد"
    assert api.params()["cities"] == "468" and api.calls[-1].headers["cookie"] == "deliver_city=468"


async def test_city_name_unknown(client, api):
    api["/v4/city/list/"] = {"count": 0, "results": []}
    result = await client.call_tool("tb_search", {"query": "iphone", "city": "Atlantis"})
    assert result.is_error and "tb_cities" in result.content[0].text


async def test_tb_suggest(client, api):
    api["/suggestion2/"] = fixture("suggest.json")
    out = await run(client, "tb_suggest", {"text": "ayfon"})
    assert out["suggestions"][0] == "ayfon 13" and api.params() == {"q": "ayfon", "source": "next_desktop"}


async def test_tb_filters(client, api):
    api[SEARCH] = fixture("search_category.json")
    api[BRANDS] = fixture("brands.json")
    out = await run(client, "tb_filters", {"category": 94})
    assert (
        out["price_span"][1] == 3730000000.0
        and len(out["brands"]) == 7
        and out["brands"][0] == {"id": 14, "name": "اپل", "name_en": "Apple"}
    )
    slugs = {a["slug"] for a in out["attributes"]}
    assert "storage" in slugs
    assert "sort" not in slugs and out["sort"][0] == "popular" and "torobpay" in out["toggles"]
    assert out["categories"] and out["parents"][0]["id"] == 175
    assert (await client.call_tool("tb_filters", {})).is_error


async def test_tb_category(client, api):
    api[SEARCH] = fixture("search_category.json")
    out = await run(client, "tb_category", {"category": 94, "sort": "cheapest", "max_price": 25_000_000, "limit": 5})
    p = api.params()
    assert p["category"] == "94" and p["sort"] == "price" and p["price__lt"] == "25000000" and "q" not in p
    assert out["children"][0]["id"] == 95 and out["parents"][0]["id"] == 175 and len(out["products"]) == 5
    assert out["products"][0]["shop"] == "شیک موبایل تهران"


async def test_tb_brands(client, api):
    api[BRANDS] = fixture("brands.json")
    out = await run(client, "tb_brands", {"category": 94, "search": "sam"})
    assert out["brands"] == [{"id": 5, "name": "سامسونگ", "name_en": "Samsung"}] and api.params() == {"cat_list": "94"}


async def test_tb_trends(client, api):
    api["/v4/search-trends/"] = fixture("trends.json")
    out = await run(client, "tb_trends", {"limit": 5})
    assert out["trends"][0]["query"] == "ایفون 18" and out["trends"][0]["category"] == 94
    assert out["trends"][0]["example"]["id"] == "a4e79e9c-1a8a-4ac7-8059-939009eb15d8"


async def test_tb_offers(client, api):
    api["/v4/special-offers/"] = fixture("offers.json")
    out = await run(client, "tb_offers", {})
    p = api.params()
    assert p["size"] == "1" and p["flavor_name"] == "default" and p["page"] == "0"
    row = out["rows"][0]
    assert (
        row["themes"][0]["title"] == "ترندهای لوازم خانگی"
        and row["products"][0]["id"] == "e462957f-e078-4bca-8d53-e72f1acb338c"
    )
    assert row["products"][0]["price"] is None  # the recorded fragment has no numeric price


async def test_tb_cities(client, api):
    api["/v4/city/most-visited/list/"] = fixture("cities_top.json")
    api["/v4/city/list/"] = fixture("cities.json")
    top = await run(client, "tb_cities", {"limit": 3})
    assert top["cities"][0] == {"id": 392, "name": "تهران"} and api.params() == {"count": "3"}
    found = await run(client, "tb_cities", {"search": "لار"})
    assert found["cities"][0] == {"id": 712, "name": "لار"} and api.params()["search"] == "لار"


async def test_server_error_on_search_is_an_error_result(client, api):
    api[SEARCH] = lambda r: httpx.Response(500)
    result = await client.call_tool("tb_search", {"query": "x"})
    assert result.is_error and "server error" in result.content[0].text
