"""Compact projections of Torob's raw objects: product cards, seller rows, store rows, seller statistics.

Everything here is pure (no HTTP). Prices are Toman integers, `None` when Torob gives none (never 0). Text that
comes from shops (titles, notes) is clipped and flattened: it is data, never instructions.
"""

from __future__ import annotations

import csv
import io
import re
import statistics
from typing import Any

from .fa import first_int
from .http import SITE

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def clip(text: object, n: int = 160) -> str | None:
    """One-line text cut at `n` characters; None when empty."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if not s:
        return None
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def toman(value: object) -> int | None:
    """A positive price as int Toman; None for 0, missing or non-numeric values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value)


def product_url(prk: str) -> str:
    """The product page; Torob redirects it to the canonical slug URL."""
    return f"{SITE}/p/{prk}/"


def slim(d: dict[str, Any], keep: tuple[str, ...] = ()) -> dict[str, Any]:
    """Drop None, False, 0, empty values except the keys in `keep`."""
    return {k: v for k, v in d.items() if k in keep or v not in (None, False, 0, "", [], {})}


def card(c: dict[str, Any]) -> dict[str, Any]:
    """A search / similar / trend / offer card -> {id, name, price, shops, url, ...}.

    `price` is the cheapest shop price in Toman or None. `shops` is the number of shops. A card that lists one
    shop by name ('در دیجی اپال') has `shops` 1 and `shop` set. `ad` marks a sponsored slot.
    """
    prk = c.get("random_key") or c.get("prk") or ""
    text = c.get("shop_text") or ""
    n = first_int(text)
    badges = [b.get("text") for b in c.get("badges") or [] if isinstance(b, dict) and b.get("text")]
    out = {
        "id": prk,
        "name": clip(c.get("name1"), 200),
        "name_en": clip(c.get("name2"), 200),
        "price": toman(c.get("price")),
        "shops": n if n is not None else (1 if text else None),
        "shop": clip(text.removeprefix("در "), 60) if text and n is None else None,
        "condition": c.get("stock_status") or None,
        "ad": c.get("is_adv"),
        "local_seller": c.get("has_nearby_shop"),
        "badges": badges,
        "discount": c.get("discount_info"),
        "image": c.get("image_url") or None,
        "url": product_url(prk) if prk else None,
    }
    return slim(out, keep=("id", "name", "price", "url"))


def cards(items: object, limit: int | None = None) -> list[dict[str, Any]]:
    rows = [card(c) for c in items or [] if isinstance(c, dict) and (c.get("random_key") or c.get("prk"))]
    return rows[:limit] if limit else rows


def _installment(v: object) -> Any:
    if isinstance(v, dict):
        return clip(v.get("text") or v.get("title") or v.get("description"), 80) or True
    if isinstance(v, str):
        return clip(v, 80)
    return True if v else None


def seller_row(r: dict[str, Any]) -> dict[str, Any]:
    """One online seller offer of a product. `buyer_notes` are Torob's trust sentences (order volume, complaints)."""
    more = r.get("more_info") or {}
    info = (r.get("score_info") or {}).get("complaints_info") or {}
    price = toman(r.get("price"))
    if r.get("availability") is False:
        price = None  # an unavailable offer has no usable price
    shipping = slim(
        {
            "free": bool(more.get("free_shipping") or more.get("same_day_free_shipping")),
            "cod": bool(more.get("payment_on_delivery")),
            "same_day": clip(more.get("same_day_delivery"), 80),
            "types": [clip(t, 40) for t in more.get("shipping_types") or []],
        }
    )
    out = {
        "shop_id": r.get("shop_id"),
        "shop": clip(r.get("shop_name"), 60),
        "city": clip(r.get("shop_name2"), 40),
        "price": price,
        "in_stock": r.get("availability", True),
        "unreliable": r.get("is_price_unreliable"),
        "score": r.get("shop_score"),
        "votes": r.get("shop_votes_count"),
        "torob_warranty": (r.get("guarantee_info") or {}).get("status") == "enabled",
        "installment": _installment(r.get("installment")),
        "shipping": shipping,
        "updated": r.get("last_price_change_date"),
        "buyer_notes": [clip(s, 120) for s in info.get("summary") or [] if isinstance(s, str)],
        "title": clip(r.get("name1"), 120),
        "note": clip(r.get("name2"), 120),
        "profile": r.get("has_public_torob_profile"),
    }
    return slim(out, keep=("shop_id", "shop", "price", "in_stock", "score"))


def store_row(r: dict[str, Any]) -> dict[str, Any]:
    """One in-person store selling a product: address, hours, shelf price and its age."""
    wh = r.get("working_hours") or {}
    title = wh.get("title") or {}
    loc = r.get("location") or {}
    out = {
        "shop_id": r.get("shop_id"),
        "shop": clip(r.get("shop_name"), 60),
        "city": clip(r.get("shop_name2"), 40),
        "address": clip(r.get("name1"), 120),
        "note": clip(r.get("name2"), 120),
        "open": r.get("is_open"),
        "hours": clip(f"{title.get('status') or ''} {title.get('text') or ''}", 60),
        "schedule": [
            clip(f"{d.get('title')}: {d.get('text')}", 40) for d in wh.get("schedule") or [] if isinstance(d, dict)
        ],
        "price": toman(r.get("price")),
        "unreliable": r.get("is_price_unreliable"),
        "price_updated": r.get("last_price_change_date"),
        "fast_delivery": r.get("supports_fast_delivery"),
        "distance": r.get("distance"),
        "location": {"lat": loc["lat"], "lon": loc["lon"]} if "lat" in loc and "lon" in loc else None,
    }
    return slim(out, keep=("shop_id", "shop", "price", "open"))


def usable(rows: list[dict[str, Any]], min_score: float = 0) -> list[dict[str, Any]]:
    """Offers worth buying from: a price, in stock, not flagged unreliable, shop score at least `min_score`."""
    return [
        r
        for r in rows
        if r.get("price") is not None
        and r.get("in_stock", True)
        and not r.get("unreliable")
        and (r.get("score") or 0) >= min_score
    ]


def seller_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """min / median / max over usable offers, the spread in percent of the min, and counts."""
    ok = sorted(r["price"] for r in usable(rows))
    out: dict[str, Any] = {
        "offers": len(rows),
        "usable": len(ok),
        "unreliable": sum(1 for r in rows if r.get("unreliable")),
        "installment": sum(1 for r in rows if r.get("installment")),
        "torob_warranty": sum(1 for r in rows if r.get("torob_warranty")),
    }
    if ok:
        out |= {
            "min": ok[0],
            "median": int(statistics.median(ok)),
            "max": ok[-1],
            "price_spread_pct": round((ok[-1] - ok[0]) / ok[0] * 100),
        }
    return out


def to_csv(rows: list[dict[str, Any]], columns: list[str]) -> str:
    """Rows as CSV text (header + one line per row; lists joined with ' | ')."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(columns)
    for r in rows:
        w.writerow(
            [" | ".join(map(str, v)) if isinstance(v := r.get(c), list) else ("" if v is None else v) for c in columns]
        )
    return buf.getvalue()
