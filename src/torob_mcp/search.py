"""Finding products: suggestions, search with filters, filter discovery, categories, brands, trends, offers, cities."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from .cards import card, cards, clip, to_csv
from .fa import norm
from .http import ApiError, api
from .registry import tool

SOURCE = "next_desktop"
SORTS = {"popular": "", "cheapest": "price", "priciest": "-price", "newest": "-date", "most_sellers": "-supply"}
COUNT_CAP = 1200  # Torob stops counting here; a count at the cap is an estimate
RESERVED = {"q", "query", "category", "brand", "price", "price__gt", "price__lt", "sort", "available", "stock_status",
            "shop_type", "torobpay", "page", "size"}  # fmt: skip

Sort = Annotated[
    Literal["popular", "cheapest", "priciest", "newest", "most_sellers"],
    Field(description="Order: popular (Torob default), cheapest, priciest, newest, most_sellers (most shops)."),
]
Category = Annotated[
    int,
    Field(
        ge=1,
        le=10_000_000,
        description="Torob category id, e.g. 94 (mobile phones). Find ids with tb_filters or tb_category.",
    ),
]
Limit = Annotated[int, Field(ge=1, le=50, description="Products to return (default 10, max 50).")]


def _price(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) else None


async def city_id(city: int | str) -> int:
    """A city id, or a name looked up through /v4/city/list/ (exact match first, else the first hit)."""
    if isinstance(city, int) or str(city).strip().isdigit():
        return int(city)
    data = await api("/v4/city/list/", {"search": str(city).strip(), "size": 10})
    rows = [
        r
        for r in (data.get("results") if isinstance(data, dict) else data) or []
        if isinstance(r, dict) and r.get("id")
    ]
    if not rows:
        raise ApiError(f"No Torob city matches '{city}'. List cities with tb_cities.")
    exact = [r for r in rows if norm(r.get("name")) == norm(city)]
    return int((exact or rows)[0]["id"])


def _facets(data: dict[str, Any]) -> dict[str, Any]:
    """Filter definitions of a search reply: brands, attribute slugs with their allowed values, toggles."""
    attrs: dict[str, dict[str, Any]] = {}
    brand_url = None
    brands: list[dict[str, Any]] = []
    for f in (data.get("filters1") or []) + (data.get("filters2") or []) + (data.get("attributes") or []):
        if not isinstance(f, dict) or not f.get("slug"):
            continue
        slug = f["slug"]
        if f.get("type") == "brand":
            brand_url = brand_url or f.get("url")
            brands = brands or [b for b in f.get("items") or [] if isinstance(b, dict)]
            continue
        if slug in attrs or f.get("type") == "price":
            continue
        values = {
            str(i["value"]): i.get("name") or str(i["value"])
            for i in f.get("items") or []
            if isinstance(i, dict) and "value" in i
        }
        attrs[slug] = {"title": f.get("title"), "type": f.get("type"), "values": values}
    return {"attrs": attrs, "brands": brands, "brand_url": brand_url}


async def _probe(query: str | None, category: int | None) -> dict[str, Any]:
    """A size-1 search whose reply carries the filter definitions for this query/category (cached upstream)."""
    return await api(
        "/v4/base-product/search/",
        {"q": query, "query": query, "category": category, "size": 1, "page": 0, "source": SOURCE},
    )


async def _all_brands(facets: dict[str, Any], category: int | None) -> list[dict[str, Any]]:
    """Every brand of the category (the search reply only lists the top few)."""
    cat = category
    if cat is None and facets["brand_url"] and "cat_list=" in facets["brand_url"]:
        cat = int(facets["brand_url"].split("cat_list=")[1].split("&")[0])
    if cat is not None:
        data = await api("/v4/brand/list/", {"cat_list": cat})
        rows = data if isinstance(data, list) else (data or {}).get("results") or []
        if rows:
            return [b for b in rows if isinstance(b, dict) and b.get("id")]
    return facets["brands"]


def _brand_row(b: dict[str, Any]) -> dict[str, Any]:
    return {"id": b["id"], "name": b.get("name1"), "name_en": b.get("name2")}


async def _resolve_brand(brand: int | str, query: str | None, category: int | None, facets: dict[str, Any]) -> int:
    if isinstance(brand, int) or brand.strip().isdigit():
        return int(brand)
    brands = await _all_brands(facets, category)
    want = norm(brand)
    for b in brands:
        if want in (norm(b.get("name1")), norm(b.get("name2"))):
            return int(b["id"])
    close = [b for b in brands if want and (want in norm(b.get("name1")) or want in norm(b.get("name2")))]
    if len(close) == 1:
        return int(close[0]["id"])
    shown = ", ".join(f"{b.get('name1') or b.get('name2')} ({b['id']})" for b in (close or brands)[:15])
    raise ApiError(
        f"Unknown brand '{brand}' here. "
        + (f"Known brands: {shown}." if shown else "Pass a category so brands can be listed, or a brand id.")
    )


def _check_filters(filters: dict[str, str], facets: dict[str, Any]) -> dict[str, str]:
    """Validate extra attribute filters against the reply's definitions; a wrong slug is silently ignored by Torob."""
    out: dict[str, str] = {}
    for slug, value in filters.items():
        if slug in RESERVED:
            raise ApiError(f"'{slug}' has its own parameter on tb_search; do not pass it in filters.")
        spec = facets["attrs"].get(slug)
        if not spec:
            raise ApiError(
                f"Unknown filter '{slug}'. Available for this search: {', '.join(sorted(facets['attrs'])) or 'none'}. See tb_filters."
            )
        by_name = {norm(n): v for v, n in spec["values"].items()}
        if spec["values"] and str(value) not in spec["values"]:
            v = by_name.get(norm(value))
            if v is None:
                shown = "; ".join(f"{v} ({n})" for v, n in list(spec["values"].items())[:15])
                raise ApiError(f"'{value}' is not a value of filter '{slug}'. Allowed: {shown}.")
            value = v
        out[slug] = str(value)
    return out


def _confidence(query: str, items: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """Query words that appear in none of the first 10 result names; low confidence when half or more are missing."""
    hay = " ".join(norm(f"{c.get('name1')} {c.get('name2')}") for c in items[:10])
    words = [w for w in norm(query).split() if len(w) > 1]
    missing = [w for w in words if w not in hay]
    return bool(words) and (not items or len(missing) * 2 >= len(words)), missing


@tool("Search products")
async def tb_search(
    query: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=120,
            description="Search text, Persian or Finglish, e.g. 'گوشی آیفون 13' or 'ayfon 13'. Optional when category is given.",
        ),
    ] = None,
    sort: Sort = "popular",
    category: Annotated[
        int | None, Field(ge=1, le=10_000_000, description="Limit to a category id (e.g. 94 = mobile phones).")
    ] = None,
    brand: Annotated[
        int | str | None,
        Field(
            description="Brand id (e.g. 14) or name (e.g. 'Apple', 'اپل'); needs query or category. Unknown names are refused with the real brand list."
        ),
    ] = None,
    min_price: Annotated[int | None, Field(ge=0, description="Lowest price in Toman.")] = None,
    max_price: Annotated[int | None, Field(ge=1, description="Highest price in Toman, e.g. 30000000.")] = None,
    available_only: Annotated[bool, Field(description="Only products in stock.")] = False,
    condition: Annotated[
        Literal["any", "new", "used"], Field(description="new, used (کارکرده / stock) or any.")
    ] = "any",
    in_person: Annotated[bool, Field(description="Only products with an in-person (offline) shop.")] = False,
    installment: Annotated[bool, Field(description="Only products sold in installments (Torob Pay).")] = False,
    city: Annotated[
        int | str | None,
        Field(
            description="City id or name; sellers near it are preferred. Effect on search is Torob-side and unverified, use tb_sellers(city=) for exact local offers."
        ),
    ] = None,
    filters: Annotated[
        dict[str, str] | None,
        Field(
            description="Extra attribute filters {slug: value} from tb_filters, e.g. {'storage': '256 gb', 'ram': '8 gb'}. Validated: unknown slugs or values are refused."
        ),
    ] = None,
    page: Annotated[int, Field(ge=0, le=100, description="0-based page (default 0).")] = 0,
    limit: Limit = 10,
    output: Annotated[
        Literal["json", "csv"], Field(description="json (default) or csv text of the cards for export.")
    ] = "json",
) -> dict[str, Any]:
    """Search Torob for products; each card has the cheapest shop price (Toman), shop count, image and URL.

    Use first for any "find / how much is X" question. Cards are products (one per model/variant), not shop offers:
    the price is the cheapest across shops. Next: tb_sellers (all offers), tb_cheapest (best offer for a query),
    tb_product (specs). Prices are Toman; `price` is null when Torob lists none. Unknown query words and weak
    matches set `low_confidence` and `unmatched_terms`; `spellcheck` / `suggested_queries` help rephrase.
    `count` stops at 1200 (`count_capped`). `ad` marks sponsored cards. `min_price`/`max_price` in the reply are
    the span of the unfiltered result.
    """
    if not query and category is None:
        raise ApiError("Give a query, a category, or both.")
    facets = None
    if brand is not None or filters:
        facets = _facets(await _probe(query, category))
    params: dict[str, Any] = {
        "q": query,
        "query": query,
        "category": category,
        "sort": SORTS[sort],
        "page": page,
        "size": limit,
        "source": SOURCE,
    }
    if brand is not None:
        assert facets is not None
        params["brand"] = await _resolve_brand(brand, query, category, facets)
    if filters:
        assert facets is not None
        params |= _check_filters(filters, facets)
    params |= {
        "price__gt": min_price or None,
        "price__lt": max_price,
        "available": True if available_only else None,
        "stock_status": {"new": "new", "used": "stock"}.get(condition),
        "shop_type": "offline" if in_person else None,
        "torobpay": True if installment else None,
    }
    if city is not None:
        cid = await city_id(city)
        params["cities"] = cid
    data = await api("/v4/base-product/search/", params, city=params.get("cities"))
    raw = [c for c in data.get("results") or [] if isinstance(c, dict)]
    rows = [card(c) for c in raw]
    notes: list[str] = []
    # Torob ignores filters it does not understand; check the bounds ourselves and say so
    if min_price or max_price:
        inside = [
            r
            for r in rows
            if r["price"] is None
            or ((not min_price or r["price"] >= min_price) and (not max_price or r["price"] <= max_price))
        ]
        if len(inside) < len(rows):
            notes.append(
                f"Dropped {len(rows) - len(inside)} cards outside the price range (Torob did not apply it to them)."
            )
            rows = inside
    count = data.get("count") if isinstance(data.get("count"), int) else len(rows)
    out: dict[str, Any] = {
        "query": query,
        "count": count,
        "count_capped": count >= COUNT_CAP or None,
        "page": page,
        "next_page": page + 1 if data.get("next") else None,
        "price_span": [_price(data.get("min_price")), _price(data.get("max_price"))] if data.get("max_price") else None,
        "products": rows,
    }
    spell = data.get("spellcheck") or {}
    if spell.get("is_spellchecked") and spell.get("corrected_query"):
        out["spellcheck"] = spell["corrected_query"]
        notes.append(f"Torob searched '{spell['corrected_query']}' instead of '{query}'.")
    if query:
        low, missing = _confidence(query, raw)
        if low:
            out["low_confidence"] = True
            out["unmatched_terms"] = missing
            if not rows:
                notes.append("No results. Try fewer words, a different spelling, or drop a filter.")
            try:
                sug = await api("/suggestion2/", {"q": query, "source": SOURCE}, guard=False)
                out["suggested_queries"] = [s["text"] for s in sug if isinstance(s, dict) and s.get("text")][:5]
            except ApiError:
                pass
    if notes:
        out["notes"] = notes
    if output == "csv":
        return {
            "csv": to_csv(rows, ["id", "name", "price", "shops", "condition", "url"]),
            "count": count,
            "next_page": out["next_page"],
        }
    return {k: v for k, v in out.items() if v is not None}


@tool("Suggestions")
async def tb_suggest(
    text: Annotated[
        str, Field(min_length=1, max_length=80, description="Typed text, Persian or Finglish (e.g. 'ayfon', 'لپ').")
    ],
) -> dict[str, Any]:
    """Torob's own autocomplete: up to 8 search phrases for the typed text (also fixes Finglish and colloquial wording).

    Use to find how shoppers phrase a product before tb_search. Not rate limited by Torob, so it also works while
    other calls are blocked.
    """
    data = await api("/suggestion2/", {"q": text, "source": SOURCE}, guard=False)
    phrases = [s["text"] for s in data if isinstance(s, dict) and s.get("text")] if isinstance(data, list) else []
    return {"text": text, "suggestions": phrases}


@tool("Search filters")
async def tb_filters(
    query: Annotated[
        str | None,
        Field(min_length=1, max_length=120, description="Search text whose filters you want, e.g. 'لپ تاپ'."),
    ] = None,
    category: Annotated[int | None, Field(ge=1, le=10_000_000, description="Category id (e.g. 94).")] = None,
) -> dict[str, Any]:
    """Which filters exist for a query/category: category children, brands, price span, sort options, attributes.

    Use before tb_search when you need a category id, a brand id, or the exact attribute slugs and values
    (storage, ram, ...) for `filters`. Needs a query or a category. Brands list is complete when a category is known.
    """
    if not query and category is None:
        raise ApiError("Give a query, a category, or both.")
    data = await _probe(query, category)
    facets = _facets(data)
    brands = await _all_brands(facets, category)
    attrs = [
        {"slug": s, "title": a["title"], "values": [{"value": v, "name": n} for v, n in list(a["values"].items())[:12]]}
        for s, a in facets["attrs"].items()
        if s not in ("sort",) and a["values"]
    ]
    return {
        "count": data.get("count"),
        "price_span": [_price(data.get("min_price")), _price(data.get("max_price"))],
        "is_leaf_category": data.get("category_is_leaf"),
        "parents": _cats(data.get("parent_categories")),
        "categories": _cats(data.get("categories")),
        "brands": [_brand_row(b) for b in brands[:60]],
        "sort": list(SORTS),
        "toggles": [s for s in ("available", "torobpay", "shop_type", "stock_status") if s in facets["attrs"]] or None,
        "attributes": attrs,
    }


def _cats(rows: Any) -> list[dict[str, Any]]:
    return [
        {"id": c.get("cat_id"), "title": c.get("title"), **({"brand_id": c["brand_id"]} if c.get("brand_id") else {})}
        for c in rows or []
        if isinstance(c, dict)
    ]


@tool("Browse category")
async def tb_category(
    category: Category,
    sort: Sort = "popular",
    brand: Annotated[int | str | None, Field(description="Brand id or name inside this category.")] = None,
    max_price: Annotated[int | None, Field(ge=1, description="Highest price in Toman.")] = None,
    page: Annotated[int, Field(ge=0, le=100)] = 0,
    limit: Limit = 10,
) -> dict[str, Any]:
    """A category page: where it sits in the tree, its sub-categories, and its top products.

    Use to walk the category tree (parents and children come with each reply; Torob has no tree endpoint, so
    counts per sub-category are not available) and to list a category's products. For text search inside a
    category use tb_search(category=).
    """
    data = await api(
        "/v4/base-product/search/",
        {
            "category": category,
            "sort": SORTS[sort],
            "page": page,
            "size": limit,
            "source": SOURCE,
            "price__lt": max_price,
        }
        | (
            {"brand": await _resolve_brand(brand, None, category, _facets(await _probe(None, category)))}
            if brand is not None
            else {}
        ),
    )
    rows = cards(data.get("results"))
    out = {
        "category": category,
        "parents": _cats(data.get("parent_categories")),
        "children": _cats(data.get("categories")),
        "is_leaf": data.get("category_is_leaf"),
        "count": data.get("count"),
        "page": page,
        "next_page": page + 1 if data.get("next") else None,
        "products": rows,
    }
    return {k: v for k, v in out.items() if v is not None}


@tool("Brands")
async def tb_brands(
    category: Category,
    search: Annotated[
        str | None, Field(min_length=1, max_length=40, description="Keep brands whose name contains this text.")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 60,
) -> dict[str, Any]:
    """All brands of a category with their ids (for tb_search brand=).

    Use when you know the category and need a brand id; tb_search also accepts the brand name directly.
    """
    data = await api("/v4/brand/list/", {"cat_list": category})
    rows = data if isinstance(data, list) else (data or {}).get("results") or []
    brands = [_brand_row(b) for b in rows if isinstance(b, dict) and b.get("id")]
    if search:
        s = norm(search)
        brands = [b for b in brands if s in norm(b["name"]) or s in norm(b["name_en"])]
    return {"category": category, "count": len(brands), "brands": brands[:limit]}


@tool("Search trends")
async def tb_trends(
    limit: Annotated[int, Field(ge=1, le=30, description="Trends to return (default 10).")] = 10,
) -> dict[str, Any]:
    """What people search on Torob right now, each with the category it lands in and one sample product.

    Use for "what is trending" or to pick a query. Follow with tb_search(query=...).
    """
    data = await api("/v4/search-trends/")
    items = data if isinstance(data, list) else (data or {}).get("results") or []
    trends = []
    for t in items[:limit]:
        if not isinstance(t, dict):
            continue
        sample = t.get("partial_info")
        trends.append(
            {"query": t.get("query"), "category": t.get("category_id")}
            | ({"example": card(sample)} if isinstance(sample, dict) and sample.get("random_key") else {})
        )
    return {"count": len(trends), "trends": trends}


@tool("Special offers")
async def tb_offers(
    page: Annotated[int, Field(ge=0, le=50, description="0-based page.")] = 0,
    size: Annotated[
        int,
        Field(
            ge=1,
            le=3,
            description="Rows per page (default 1). Torob replies are huge (about 900 KB for one row), so keep it small.",
        ),
    ] = 1,
    limit: Annotated[int, Field(ge=1, le=30, description="Products per row to return (default 10).")] = 10,
) -> dict[str, Any]:
    """Torob's special-offers feed ("پیشنهاد ویژه"): themed rows with discounted or promoted products.

    Use for "what is on sale". Each row has a theme title and product cards. Follow with tb_product / tb_sellers.
    """
    data = await api("/v4/special-offers/", {"page": page, "size": size, "flavor_name": "default", "source": SOURCE})
    rows = []
    for r in (data.get("results") if isinstance(data, dict) else None) or []:
        if not isinstance(r, dict):
            continue
        themes = [
            {
                "title": clip(t.get("title"), 80),
                "description": clip(t.get("description"), 120),
                "shop_id": t.get("shop_id"),
            }
            for t in r.get("data") or []
            if isinstance(t, dict) and t.get("title")
        ]
        rows.append(
            {
                "themes": [{k: v for k, v in t.items() if v} for t in themes],
                "products": cards(r.get("base_products"), limit),
            }
        )
    return {"page": page, "next_page": page + 1 if isinstance(data, dict) and data.get("next") else None, "rows": rows}


@tool("Cities")
async def tb_cities(
    search: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=40,
            description="City name or part of it (Persian), e.g. 'مشهد'. Omit for the most visited cities.",
        ),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """City ids for the `city` filter of tb_search, tb_sellers and tb_stores (392 Tehran, 468 Mashhad, 240 Isfahan).

    With `search` finds a city by name; without it lists the most visited cities.
    """
    if search:
        data = await api("/v4/city/list/", {"search": search, "size": limit})
    else:
        data = await api("/v4/city/most-visited/list/", {"count": limit})
    rows = data.get("results") if isinstance(data, dict) else data
    cities = [{"id": c["id"], "name": c.get("name")} for c in rows or [] if isinstance(c, dict) and c.get("id")]
    return {"count": len(cities), "cities": cities[:limit]}
