"""Decision tools that combine several calls: cheapest offer, compare, best value, deal check, basket."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from .cards import UUID_RE, card, dedupe, product_url, seller_stats, slim, usable
from .fa import norm
from .http import ApiError, Blocked, OutOfTime, api
from .product import City, Ref, chart_data, chart_summary, details, offers, prk_of, series_of, spec_map, summary
from .registry import tool
from .search import SORTS, SOURCE

Condition = Annotated[Literal["new", "used", "any"], Field(description="used = کارکرده / stock.")]


async def _top_cards(query: str, condition: str, size: int, **extra: Any) -> list[dict[str, Any]]:
    """Search cards that have a price, sponsored ones last (they are paid placements, not best matches)."""
    data = await api(
        "/v4/base-product/search/",
        {
            "q": query,
            "query": query,
            "size": size,
            "page": 0,
            "source": SOURCE,
            "stock_status": {"new": "new", "used": "stock"}.get(condition),
        }
        | extra,
    )
    rows = dedupe([card(c) for c in data.get("results") or [] if isinstance(c, dict) and c.get("random_key")])
    rows = [r for r in rows if r["price"] is not None]
    return sorted(rows, key=lambda r: bool(r.get("ad")))


def _pick(rows: list[dict[str, Any]], installment_only: bool, min_score: float) -> list[dict[str, Any]]:
    return [r for r in usable(rows, min_score) if not installment_only or r.get("installment")]


@tool("Cheapest offer")
async def tb_cheapest(
    query: Annotated[str, Field(min_length=1, max_length=120, description="Product, e.g. 'ps5 slim'.")],
    candidates: Annotated[int, Field(ge=1, le=5, description="Top search matches to check (one request each).")] = 3,
    condition: Condition = "new",
    city: City = None,
    installment_only: Annotated[bool, Field(description="Installment (قسطی) shops only.")] = False,
    min_score: Annotated[float, Field(ge=0, le=5, description="Minimum shop score.")] = 0,
) -> dict[str, Any]:
    """Cheapest trustworthy offer for a product named in words: searches, then looks at every shop's offer.

    Gives the best usable offer per top match and the overall `cheapest`; check the matched name.
    """
    found = (await _top_cards(query, condition, max(candidates, 5)))[:candidates]
    if not found:
        raise ApiError(f"No priced product found for '{query}'. Try tb_suggest for wording.")
    picks = []
    for c in found:
        try:
            rows = await offers(c["id"], city)
        except OutOfTime:
            if not picks:
                raise
            break  # partial: the matches checked so far
        ok = _pick(rows, installment_only, min_score)
        picks.append(
            {
                "product": {"id": c["id"], "name": c["name"], "url": c["url"]},
                "best_offer": ok[0] if ok else None,
                "stats": seller_stats(rows),
            }
        )
    priced = [p for p in picks if p["best_offer"]]
    out: dict[str, Any] = {"query": query, "picks": picks}
    if len(picks) < len(found):
        out["partial"] = f"Checked {len(picks)} of {len(found)} matches before the time limit."
    if priced:
        best = min(priced, key=lambda p: p["best_offer"]["price"])
        offer = best["best_offer"]  # the full row is already in `picks`
        out["cheapest"] = {
            "product": best["product"],
            "best_offer": {"shop_id": offer.get("shop_id"), "shop": offer.get("shop"), "price": offer["price"]},
        }
    else:
        out["note"] = "No usable offer matched the filters (in stock, not unreliable, installment / score / city)."
    return out


@tool("Compare products")
async def tb_compare(
    products: Annotated[list[Ref], Field(min_length=2, max_length=5)],
) -> dict[str, Any]:
    """Side-by-side comparison of 2-5 products: only the specs that differ, the price window, and which is cheapest.

    `differences` maps a spec to its values in `products` order (null = not listed).
    """
    ids = list(dict.fromkeys(prk_of(p) for p in products))
    if len(ids) < 2:
        raise ApiError("Give at least two different products.")
    recs, specs = [], []
    for prk in ids:
        d = await details(prk)
        recs.append(summary(d))
        specs.append(spec_map(d))
    keys = list(dict.fromkeys(k for s in specs for k in s))
    diff = {}
    for k in keys:
        vals = [s.get(k) for s in specs]
        if len({norm(v) for v in vals}) > 1:
            diff[k] = vals
    priced = [r for r in recs if r.get("price_min")]
    best = min(priced, key=lambda r: r["price_min"])["id"] if priced else None
    for r in recs:
        r["cheapest"] = r["id"] == best or None
    return {
        "products": [slim(r, keep=("id", "name", "price_min")) for r in recs],
        "differences": diff,
        "same_specs": len(keys) - len(diff),
        "cheapest": best,
        "price_gap_pct": round(
            (max(r["price_min"] for r in priced) - min(r["price_min"] for r in priced))
            / min(r["price_min"] for r in priced)
            * 100
        )
        if len(priced) > 1
        else None,
    }


@tool("Best value under budget")
async def tb_best_value(
    budget: Annotated[int, Field(ge=1000, description="Max price, Toman.")],
    query: Annotated[
        str | None,
        Field(min_length=1, max_length=120, description="What to buy, e.g. 'لپ تاپ'. Optional with category."),
    ] = None,
    category: Annotated[int | None, Field(ge=1, le=10_000_000)] = None,
    rank_by: Annotated[
        Literal["budget_reach", "popular", "cheapest"],
        Field(description="budget_reach: priciest models that fit first."),
    ] = "budget_reach",
    min_shops: Annotated[int, Field(ge=1, le=100, description="Skip products with fewer shops.")] = 3,
    condition: Condition = "new",
    limit: Annotated[int, Field(ge=1, le=30)] = 10,
) -> dict[str, Any]:
    """Ranked product picks that fit a budget (Toman), for "best phone under 30 million".

    No quality score: check specs with tb_compare. In-stock products only.
    """
    if not query and category is None:
        raise ApiError("Give a query, a category, or both.")
    sort = {"budget_reach": "priciest", "popular": "popular", "cheapest": "cheapest"}[rank_by]
    data = await api(
        "/v4/base-product/search/",
        {"q": query, "query": query, "category": category, "price__lt": budget, "sort": SORTS[sort], "available": True, "page": 0, "size": 50, "source": SOURCE,
         "stock_status": {"new": "new", "used": "stock"}.get(condition)},
    )  # fmt: skip
    rows = dedupe([card(c) for c in data.get("results") or [] if isinstance(c, dict) and c.get("random_key")])
    keep = [
        r
        for r in rows
        if r["price"] and r["price"] <= budget and (r.get("shops") or 0) >= min_shops and not r.get("ad")
    ]
    for r in keep:
        r["budget_used_pct"] = round(r["price"] / budget * 100)
    out: dict[str, Any] = {"budget": budget, "rank_by": rank_by, "matches": data.get("count"), "products": keep[:limit]}
    if not keep:
        out["note"] = (
            f"No in-stock product with at least {min_shops} shops fits {budget:,} Toman"
            f" ({len(rows)} cards checked). Raise the budget, lower min_shops, or broaden the query / category."
        )
    return out


@tool("Deal check")
async def tb_deal_check(
    product: Ref,
    price: Annotated[
        int | None,
        Field(ge=1000, description="Offered price, Toman; default today's cheapest usable offer."),
    ] = None,
) -> dict[str, Any]:
    """Is this a good price? Score 0-100 from the product's price history and today's competing offers.

    100 minus the price's percentile among past weekly minimums, minus up to 20 when above today's cheapest usable
    offer. `reasons` explains it.
    """
    prk = prk_of(product)
    rows = await offers(prk)
    stats = seller_stats(rows)
    best = (usable(rows) or [None])[0]
    mine = price or (best["price"] if best else None)
    if mine is None:
        raise ApiError("No usable offer to judge (everything is out of stock or flagged unreliable). Pass `price`.")
    reasons: list[str] = []
    try:
        raw = await chart_data(prk)
        chart = chart_summary(raw)
        if price is None:
            hist_pct = chart["percentile"]
        else:
            _, sets = series_of(raw)
            series = list((sets.get("min") or sets.get("avg") or {}).values())
            hist_pct = round(sum(1 for v in series if v < price) / len(series) * 100)
        base = 100 - hist_pct
        reasons.append(
            f"{hist_pct}% of the weeks in the period had a lower minimum price (period low {chart['period_low']:,}, median {chart['period_median']:,})."
        )
    except Blocked:
        raise
    except ApiError as e:
        chart, base = None, 60
        reasons.append(f"No price history ({e}); history part assumed neutral.")
    penalty = 0
    if best and mine > best["price"]:
        gap = round((mine - best["price"]) / best["price"] * 100)
        penalty = min(20, gap)
        reasons.append(f"{gap}% above today's cheapest usable offer ({best['price']:,} at {best.get('shop')}).")
    elif best and price is None:
        reasons.append(f"Judged price is today's cheapest usable offer ({best['price']:,} at {best.get('shop')}).")
    elif best:
        reasons.append(f"Below today's cheapest usable offer ({best['price']:,}), or equal to it.")
    if stats.get("unreliable"):
        reasons.append(f"{stats['unreliable']} offer(s) flagged unreliable are ignored.")
    score = max(0, min(100, base - penalty))
    return {
        "id": prk,
        "price": mine,
        "score": score,
        "verdict": "good deal" if score >= 70 else "fair" if score >= 40 else "wait or shop around",
        "reasons": reasons,
        "cheapest_offer": best,
        "history": chart,
        "market": {k: stats.get(k) for k in ("usable", "min", "median", "max", "price_spread_pct")},
        "url": product_url(prk),
    }


@tool("Shopping basket")
async def tb_basket(
    items: Annotated[
        list[str],
        Field(
            min_length=2,
            max_length=8,
            description="Product names ('آیفون 13 128') or ids / URLs.",
        ),
    ],
    condition: Condition = "new",
    city: City = None,
    installment_only: Annotated[bool, Field(description="Installment shops only.")] = False,
    min_score: Annotated[float, Field(ge=0, le=5)] = 0,
) -> dict[str, Any]:
    """Cheapest shop for a whole shopping list: best single shop that sells everything, and the split-cart total.

    Names resolve to the top search match (check `items[].name`; pass ids to be exact).
    """
    resolved, missing = [], []
    for text in dict.fromkeys(items):
        if m := UUID_RE.search(text):
            resolved.append({"input": text, "id": m.group().lower(), "name": None})
            continue
        top = await _top_cards(text, condition, 5)
        if top:
            resolved.append({"input": text, "id": top[0]["id"], "name": top[0]["name"], "url": top[0]["url"]})
        else:
            missing.append(text)
    per_item: list[dict[Any, dict[str, Any]]] = []
    for it in resolved:
        ok = _pick(await offers(it["id"], city), installment_only, min_score)
        by_shop: dict[Any, dict[str, Any]] = {}
        for r in ok:  # rows are cheapest first, so the first per shop is its best
            by_shop.setdefault(r.get("shop_id") or r.get("shop"), r)
        per_item.append(by_shop)
        it["best_price"] = ok[0]["price"] if ok else None
        it["cheapest_shop"] = ok[0].get("shop") if ok else None
        it["shops"] = len(by_shop)
    covered = [i for i, s in enumerate(per_item) if s]
    shops = set.intersection(*(set(per_item[i]) for i in covered)) if covered else set()
    singles = []
    for sid in shops:
        rows = [per_item[i][sid] for i in covered]
        singles.append(
            {
                "shop": rows[0].get("shop"),
                "shop_id": rows[0].get("shop_id"),
                "city": rows[0].get("city"),
                "score": rows[0].get("score"),
                "total": sum(r["price"] for r in rows),
                "prices": [r["price"] for r in rows],
            }
        )
    singles.sort(key=lambda s: s["total"])
    split = [(resolved[i], min(per_item[i].values(), key=lambda r: r["price"])) for i in covered]
    split_total = sum(r["price"] for _, r in split)
    out: dict[str, Any] = {
        "items": [slim(i, keep=("input", "id", "best_price")) for i in resolved],
        "unresolved": missing or None,
        "no_offer": [resolved[i]["input"] for i in range(len(resolved)) if i not in covered] or None,
        "single_shop": singles[:5],
        "split_cart": {
            "total": split_total,
            "picks": [
                {"item": it["name"] or it["input"], "shop": r.get("shop"), "price": r["price"]} for it, r in split
            ],
        }
        if split
        else None,
    }
    if singles and split:
        out["split_saves"] = singles[0]["total"] - split_total
    if covered and not singles:
        out["note"] = "No single shop sells all items; use split_cart."
    return {k: v for k, v in out.items() if v is not None}
