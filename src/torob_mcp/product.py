"""One product: details, sellers, in-person stores, presence, variants, price chart and history, similar, URLs."""

from __future__ import annotations

import statistics
from datetime import date, timedelta
from typing import Annotated, Any, Literal

from pydantic import Field

from .cards import UUID_RE, cards, clip, product_url, seller_row, seller_stats, slim, store_row, to_csv, toman, usable
from .fa import first_int, jalali_to_iso
from .http import SITE, ApiError, Blocked, api
from .registry import tool
from .search import SOURCE, city_id

Ref = Annotated[
    str,
    Field(
        min_length=8,
        max_length=400,
        description="Product id (the UUID from search cards, e.g. '57ea65ae-0798-4cd0-96a7-38d8af180345'), a '/p/<id>/...' path or a full torob.com product URL.",
    ),
]
City = Annotated[
    int | str | None,
    Field(description="City id or name (e.g. 'مشهد' or 468): only offers that deliver to / sit in that city."),
]
Limit = Annotated[int, Field(ge=1, le=50, description="Rows to return (default 15, max 50).")]


def prk_of(ref: str) -> str:
    """The product UUID inside a bare id, a /p/ path or a full URL."""
    m = UUID_RE.search(ref or "")
    if not m:
        raise ApiError(
            f"'{ref}' has no product id (a UUID like 57ea65ae-0798-4cd0-96a7-38d8af180345). Get ids from tb_search."
        )
    return m.group().lower()


async def details(prk: str) -> dict[str, Any]:
    d = await api("/v4/base-product/details/", {"prk": prk, "source": SOURCE})
    if not isinstance(d, dict) or not d.get("random_key"):
        raise ApiError(f"Torob returned no product for {prk}.")
    return d


async def offers(prk: str, city: int | str | None = None) -> list[dict[str, Any]]:
    """All online seller offers of a product as compact rows (cheapest first, unavailable last)."""
    cid = await city_id(city) if city is not None else None
    data = await api("/v4/base-product/sellers/", {"prk": prk, "list_type": "products_info"}, city=cid)
    rows = [
        seller_row(r) for r in (data.get("results") if isinstance(data, dict) else None) or [] if isinstance(r, dict)
    ]
    return sorted(rows, key=lambda r: (r["price"] is None, r["price"] or 0))


def _join(v: Any) -> str:
    return ", ".join(map(str, v)) if isinstance(v, list) else str(v)


def spec_map(d: dict[str, Any]) -> dict[str, str]:
    """Every spec of a details reply as {name: value} (key specs first, then the full table)."""
    out: dict[str, str] = {}
    for sec in d.get("key_specs") or []:
        for it in sec.get("items") or []:
            out[str(it.get("key"))] = _join(it.get("value"))
    for h in (d.get("structural_specs") or {}).get("headers") or []:
        for k, v in (h.get("specs") or {}).items():
            out.setdefault(str(k), _join(v))
    return out


def _image(d: dict[str, Any]) -> str | None:
    if d.get("image_url"):
        return d["image_url"]
    for key in ("media_urls", "image_urls"):
        first = (d.get(key) or [None])[0]
        url = first.get("url") if isinstance(first, dict) else first
        if isinstance(url, str) and url.startswith("https://"):
            return url
    return None


def summary(d: dict[str, Any]) -> dict[str, Any]:
    """Short product record shared by tb_product, tb_products_batch and tb_compare."""
    prk = d["random_key"]
    crumbs = [b for b in d.get("breadcrumbs") or [] if isinstance(b, dict) and b.get("cat_id")]
    brand = next((b for b in crumbs if b.get("brand_id")), None)
    out = {
        "id": prk,
        "name": clip(d.get("name1"), 200),
        "name_en": clip(d.get("name2"), 200),
        "url": product_url(prk),
        "price_min": toman(d.get("min_price") or d.get("price")),
        "price_max": toman(d.get("max_price")),
        "shops": first_int(d.get("shop_text")),
        "online_offers": (d.get("products_info") or {}).get("count"),
        "in_person_stores": (d.get("products_in_store_info") or {}).get("count"),
        "condition": d.get("stock_status") or None,
        "available": d.get("availability"),
        "category": {"id": d.get("torob_category"), "path": [b["title"] for b in crumbs if not b.get("brand_id")]},
        "brand": {"id": brand["brand_id"], "name": brand["title"]} if brand else None,
        "image": _image(d),
    }
    return slim(out, keep=("id", "name", "url", "price_min", "available"))


@tool("Product details")
async def tb_product(
    product: Ref,
    full_specs: Annotated[
        bool, Field(description="Also return the complete spec table grouped by section (long).")
    ] = False,
) -> dict[str, Any]:
    """Full record of one product: name, price range across shops (Toman), category path, brand, key specs, cheapest offer.

    Use after tb_search for specs and the price window. For every shop's price use tb_sellers; for a buying
    decision tb_deal_check; to compare models tb_compare. `price_min`/`price_max` span all offers (including
    unreliable ones); `cheapest_offer` is the cheapest usable one among the offers embedded in the page.
    """
    prk = prk_of(product)
    d = await details(prk)
    out = summary(d)
    out["key_specs"] = {k: v for k, v in list(spec_map({"key_specs": d.get("key_specs")}).items())}
    embedded = [seller_row(r) for r in (d.get("products_info") or {}).get("result") or [] if isinstance(r, dict)]
    best = usable(sorted(embedded, key=lambda r: (r["price"] is None, r["price"] or 0)))
    if best:
        out["cheapest_offer"] = best[0]
    if d.get("variants"):
        out["variant_groups"] = [v.get("title") for v in d["variants"] if isinstance(v, dict)]
    if desc := clip(d.get("seo_description"), 300):
        out["description"] = desc
    if full_specs:
        out["specs"] = {
            h.get("header"): {k: _join(v) for k, v in (h.get("specs") or {}).items()}
            for h in (d.get("structural_specs") or {}).get("headers") or []
            if isinstance(h, dict)
        }
    return out


@tool("Sellers")
async def tb_sellers(
    product: Ref,
    city: City = None,
    installment_only: Annotated[bool, Field(description="Only shops that sell it in installments (قسطی).")] = False,
    warranty_only: Annotated[
        bool, Field(description="Only offers with Torob's buyer-protection warranty (گارانتی ترب).")
    ] = False,
    min_score: Annotated[float, Field(ge=0, le=5, description="Only shops with at least this Torob score (0-5).")] = 0,
    include_unreliable: Annotated[
        bool, Field(description="Keep offers Torob flags as unreliable (bait or stale prices). Hidden by default.")
    ] = False,
    limit: Limit = 15,
    output: Annotated[Literal["json", "csv"], Field(description="json (default) or csv text for export.")] = "json",
) -> dict[str, Any]:
    """Every online shop offer for a product, cheapest first, with trust, warranty, installment and delivery info.

    Use to pick where to buy. Row fields: price (Toman, null when out of stock), score/votes (shop rating 0-5),
    `unreliable` (Torob flags the price as bait or stale), `torob_warranty`, `installment`, `shipping`,
    `buyer_notes` (Torob's order-volume and complaint sentences), `updated` (age of the price). `stats` are over
    usable offers only (in stock, not unreliable): min / median / max and `price_spread_pct`. `city` filters to
    shops serving that city. Torob returns all offers at once; `limit` slices them. Buy at `url`.
    """
    prk = prk_of(product)
    rows = await offers(prk, city)
    stats = seller_stats(rows)
    shown = [
        r
        for r in rows
        if (include_unreliable or not r.get("unreliable"))
        and (not installment_only or r.get("installment"))
        and (not warranty_only or r.get("torob_warranty"))
        and (r.get("score") or 0) >= min_score
    ]
    out = {
        "id": prk,
        "stats": stats,
        "matching": len(shown),
        "offers": shown[:limit],
        "hidden_unreliable": sum(1 for r in rows if r.get("unreliable")) if not include_unreliable else None,
        "url": product_url(prk),
    }
    if output == "csv":
        cols = [
            "shop",
            "city",
            "price",
            "score",
            "votes",
            "unreliable",
            "torob_warranty",
            "installment",
            "updated",
            "shop_id",
        ]
        return {"csv": to_csv(shown[:limit], cols), "matching": len(shown), "url": out["url"]}
    return slim(out, keep=("id", "stats", "offers", "url"))


@tool("In-person stores")
async def tb_stores(
    product: Ref,
    city: City = None,
    with_location: Annotated[bool, Field(description="Add map coordinates (lat, lon) of each store.")] = False,
    limit: Limit = 15,
) -> dict[str, Any]:
    """In-person (offline) shops that stock a product: address, opening hours, open now, shelf price and its age.

    Use for "can I buy it today near me". Prices on shelves are set by the shop and can be stale (`price_updated`).
    `city` narrows to one city (Torob filters by it server-side). Online offers are in tb_sellers.
    """
    prk = prk_of(product)
    cid = await city_id(city) if city is not None else None
    if with_location:
        data = await api("/v4/base-product/map/sellers/", {"prk": prk}, city=cid)
        raw = data.get("result") or data.get("results") or []
    else:
        data = await api("/v4/base-product/sellers/", {"prk": prk, "list_type": "products_in_store_info"}, city=cid)
        raw = data.get("results") or []
    rows = sorted(
        (store_row(r) for r in raw if isinstance(r, dict)), key=lambda r: (r["price"] is None, r["price"] or 0)
    )
    return slim(
        {
            "id": prk,
            "count": data.get("count", len(rows)),
            "open_now": sum(1 for r in rows if r.get("open")),
            "stores": rows[:limit],
            "url": product_url(prk),
        },
        keep=("id", "count", "open_now", "stores"),
    )


@tool("Shop presence")
async def tb_presence(product: Ref) -> dict[str, Any]:
    """How widely a product is sold: online shops, in-person stores, installment shops, Torob-warranty shops, price window.

    One cheap call to judge availability before digging into tb_sellers or tb_stores.
    """
    d = await details(prk_of(product))
    flt = {
        f.get("action"): f
        for f in (d.get("filters") or {}).get("items") or []
        if isinstance(f, dict) and f.get("action")
    }
    out = {
        "id": d["random_key"],
        "name": clip(d.get("name1"), 200),
        "online_shops": (d.get("products_info") or {}).get("count"),
        "in_person_stores": (d.get("products_in_store_info") or {}).get("count"),
        "installment_shops": (flt.get("is_filtered_by_bnpl") or {}).get("online_shop_display_count"),
        "torob_warranty_shops": (flt.get("is_filtered_by_warranty") or {}).get("online_shop_display_count"),
        "price_min": toman(d.get("min_price")),
        "price_max": toman(d.get("max_price")),
        "available": d.get("availability"),
    }
    return {k: v for k, v in out.items() if v is not None}


@tool("Variants")
async def tb_variants(product: Ref) -> dict[str, Any]:
    """Sibling variants of a product (storage + RAM, colour, region ...) each with its own cheapest price.

    Use to answer "what does the 512 GB version cost". Pass an option's `id` to the other tools.
    """
    d = await details(prk_of(product))
    groups = []
    for g in d.get("variants") or []:
        if not isinstance(g, dict):
            continue
        options = []
        for it in g.get("items") or []:
            if not isinstance(it, dict) or not it.get("random_key"):
                continue
            c = cards([it])[0]
            options.append(
                slim(
                    {
                        "id": c["id"],
                        "title": clip(it.get("title"), 80),
                        "price": c["price"],
                        "shops": c.get("shops"),
                        "selected": it.get("selected"),
                        "url": c["url"],
                    },
                    keep=("id", "price"),
                )
            )
        groups.append({"group": g.get("title"), "options": options})
    return {"id": d["random_key"], "name": clip(d.get("name1"), 200), "variants": groups}


def series_of(chart: dict[str, Any]) -> tuple[list[str], dict[str, dict[int, int]]]:
    """Chart labels and the average / minimum series as {index: Toman} (series are sparse: join on `i`)."""
    labels = chart.get("labels") or []
    sets: dict[str, dict[int, int]] = {}
    for n, s in enumerate(chart.get("dataSets") or []):
        name = (
            "min"
            if "کمترین" in str(s.get("label"))
            else "avg"
            if "میانگین" in str(s.get("label"))
            else ("avg", "min")[n % 2]
        )
        sets[name] = {
            int(e["i"]): int(e["val"])
            for e in s.get("entries") or []
            if isinstance(e, dict) and toman(e.get("val")) and "i" in e
        }
    return labels, sets


async def chart_data(prk: str) -> dict[str, Any]:
    data = await api("/v4/base-product/price-chart/", {"prk": prk})
    if not isinstance(data, dict) or not data.get("labels"):
        raise ApiError("Torob has no price chart for this product.")
    return data


def chart_summary(chart: dict[str, Any]) -> dict[str, Any]:
    """Stats of the minimum-price series: current, year low / high / median, percentile, verdict, 30 / 90 day change.

    `percentile` is the share of weeks in which the price was lower than now (0 = cheapest in the period).
    Verdict: great (percentile <= 20), fair (<= 60), high (above).
    """
    labels, sets = series_of(chart)
    series = sets.get("min") or sets.get("avg") or {}
    if not series:
        raise ApiError("The price chart has no data points.")
    idx = sorted(series)
    vals = [series[i] for i in idx]
    now = vals[-1]
    pct = round(sum(1 for v in vals if v < now) / len(vals) * 100)

    def ago(days: int) -> int | None:
        dates = [jalali_to_iso(labels[i]) if i < len(labels) else None for i in idx]
        if not dates[-1]:
            return None
        target = date.fromisoformat(dates[-1]) - timedelta(days=days)
        past = [
            (abs((date.fromisoformat(d) - target).days), v)
            for d, v in zip(dates, vals, strict=True)
            if d and date.fromisoformat(d) <= target + timedelta(days=3)
        ]
        return min(past)[1] if past else None

    def change(days: int) -> int | None:
        old = ago(days)
        return round((now - old) / old * 100) if old else None

    first = labels[idx[0]] if idx[0] < len(labels) else None
    return {
        "current": now,
        "period_low": min(vals),
        "period_high": max(vals),
        "period_median": int(statistics.median(vals)),
        "percentile": pct,
        "verdict": "great" if pct <= 20 else "fair" if pct <= 60 else "high",
        "vs_low_pct": round((now - min(vals)) / min(vals) * 100),
        "change_30d_pct": change(30),
        "change_90d_pct": change(90),
        "points": len(vals),
        "since": jalali_to_iso(first) if first else None,
    }


@tool("Price chart")
async def tb_price_chart(
    product: Ref,
    points: Annotated[
        int,
        Field(
            ge=4,
            le=60,
            description="Latest chart points to return (default 26, about 6 months; Torob has about 52 weekly points).",
        ),
    ] = 26,
) -> dict[str, Any]:
    """Price chart of a product (about a year, weekly): minimum and average price in Toman, with a buy-now verdict.

    Use to see if now is a good time. `summary.verdict` is great / fair / high from where today's minimum price sits
    among the period's weeks (`percentile` 0 = cheapest of the period). Dates are ISO (converted from Jalali).
    For notable moves only see tb_price_history; for a full buying decision tb_deal_check.
    """
    prk = prk_of(product)
    chart = await chart_data(prk)
    labels, sets = series_of(chart)
    idx = sorted(set(sets.get("min", {})) | set(sets.get("avg", {})))[-points:]
    rows = [
        slim(
            {
                "date": jalali_to_iso(labels[i]) or labels[i],
                "min": sets.get("min", {}).get(i),
                "avg": sets.get("avg", {}).get(i),
            },
            keep=("date",),
        )
        for i in idx
        if i < len(labels)
    ]
    return {"id": prk, "summary": chart_summary(chart), "series": rows, "url": product_url(prk)}


@tool("Price history events")
async def tb_price_history(
    product: Ref,
    min_change_pct: Annotated[
        int, Field(ge=1, le=90, description="Ignore moves smaller than this percent (default 3).")
    ] = 3,
    limit: Annotated[int, Field(ge=1, le=30)] = 10,
) -> dict[str, Any]:
    """Notable price moves of a product, newest first: drops, rises, lowest and highest point, last change.

    Derived from the price chart's weekly minimum series (Torob's own change-event feed is not used), so moves are
    per week, not per day. Use for "did the price just drop". Prices in Toman.
    """
    prk = prk_of(product)
    chart = await chart_data(prk)
    labels, sets = series_of(chart)
    series = sets.get("min") or sets.get("avg") or {}
    idx = sorted(series)

    def when(i: int) -> str | None:
        return (jalali_to_iso(labels[i]) or labels[i]) if i < len(labels) else None

    events = []
    for a, b in zip(idx, idx[1:], strict=False):
        pct = round((series[b] - series[a]) / series[a] * 100)
        if abs(pct) >= min_change_pct:
            events.append(
                {
                    "date": when(b),
                    "from": series[a],
                    "to": series[b],
                    "change_pct": pct,
                    "kind": "drop" if pct < 0 else "rise",
                }
            )
    lo, hi = min(idx, key=lambda i: series[i]), max(idx, key=lambda i: series[i])
    return {
        "id": prk,
        "current": series[idx[-1]],
        "lowest": {"date": when(lo), "price": series[lo]},
        "highest": {"date": when(hi), "price": series[hi]},
        "last_change": events[-1] if events else None,
        "events": events[::-1][:limit],
    }


@tool("Similar products")
async def tb_similar(product: Ref, limit: Limit = 10) -> dict[str, Any]:
    """Products Torob shows as similar (alternatives in the same class), with cheapest price and shop count.

    Use for "what else is like this". Torob ignores a page size, so the list is cut here. Compare candidates
    with tb_compare.
    """
    prk = prk_of(product)
    data = await api("/v4/base-product/similar-base-product/", {"prk": prk, "source": SOURCE})
    rows = cards(data.get("results") if isinstance(data, dict) else None)
    return {"id": prk, "count": len(rows), "products": rows[:limit]}


@tool("Product URL")
async def tb_product_url(product: Ref) -> dict[str, Any]:
    """Clean torob.com URL for a product id, '/p/<id>/...' path or any torob.com product link (no request is sent).

    Use to hand the user a link. Torob redirects it to the canonical page with the product name.
    """
    prk = prk_of(product)
    return {"id": prk, "url": product_url(prk), "site": SITE}


@tool("Product cards")
async def tb_products_batch(
    products: Annotated[
        list[Ref], Field(min_length=1, max_length=10, description="1 to 10 product ids, paths or URLs.")
    ],
) -> dict[str, Any]:
    """Short records (name, price window, shop counts, category, brand) for up to 10 products at once.

    Use to refresh a list of known ids. Torob has no batch call, so this makes one request per product
    (spaced out); failed ones are reported in `errors` and do not stop the rest.
    """
    out, errors = [], []
    for ref in dict.fromkeys(prk_of(p) for p in products):
        try:
            out.append(summary(await details(ref)))
        except Blocked:
            raise
        except ApiError as e:
            errors.append({"id": ref, "error": str(e)})
    return slim({"products": out, "errors": errors}, keep=("products",))
