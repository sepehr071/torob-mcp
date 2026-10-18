"""Shared async HTTP client for the Torob API.

Torob serves everything from one JSON gateway, `https://api.torob.com` (paths `/v4/...`, plus `/suggestion2/`).
No token or header is needed. Every tool goes through `api`. `_send` spaces requests (`MIN_INTERVAL`), keeps a
small TTL cache, retries once on dropped connections and 502/503/504, and turns failures into `ToolError` messages
the model can act on.

Requests look like one Chrome user: curl_cffi sends Chrome's TLS/HTTP2 fingerprint and default headers.

Torob's WAF answers HTTP 490 (an ArCaptcha page) when an IP sends a burst of requests. A challenge is met in order
with: the next proxy of `TOROB_MCP_PROXY` (a comma-separated pool), then one headless patchright visit of the home
page whose cookies are reused (optional extra `torob-mcp[browser]`; it cannot pass a real CAPTCHA). If both fail the
call starts a cooldown (`COOLDOWN` seconds) during which every call fails at once with the minutes left.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from typing import Any

import httpx
from httpx_curl_cffi import AsyncCurlTransport
from mcp.server.mcpserver.exceptions import ToolError

API = "https://api.torob.com"
SITE = "https://torob.com"

HEADERS = {
    # User-Agent, sec-ch-ua and TLS come from the impersonated Chrome profile (see _get_client).
    "Accept": "application/json",
    "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
    "Origin": SITE,
    "Referer": f"{SITE}/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",  # torob.com page calling api.torob.com
    "priority": "u=1, i",  # the profile's u=0 is navigation-style
    # navigation-only headers of the Chrome profile; an XHR never sends them (see _CurlTransport)
    "upgrade-insecure-requests": "",
    "sec-fetch-user": "",
}
_NAV_ONLY = (b"upgrade-insecure-requests", b"sec-fetch-user")

# The block is rate based (about 6-10 /v4 calls in a few minutes), so one call at a time with a long gap.
MAX_CONCURRENCY = 1
MIN_INTERVAL = float(os.environ.get("TOROB_MCP_MIN_INTERVAL") or 2.0)  # seconds between upstream calls
JITTER = (0.5, 2.0)  # seconds added before each upstream call, on top of MIN_INTERVAL
COOLDOWN = 30 * 60  # seconds without any call after an HTTP 490
RETRY_DELAY = 0.5  # seconds; doubled per retry, with jitter
MAX_RETRIES = 2
MAX_RETRY_WAIT = 10  # cap for Retry-After and backoff, seconds
RETRY_STATUS = (429, 502, 503, 504)
BROWSER = os.environ.get("TOROB_MCP_BROWSER", "1") != "0"  # rung 2 switch (tests turn it off)
BROWSER_WAIT = 20  # seconds the headless browser waits for the challenge to clear
CACHE_MAX = 256
# cache lifetime in seconds by path prefix; the first match wins, DEFAULT_TTL otherwise
TTLS = (("/v4/base-product/price-chart/", 6 * 3600), ("/v4/base-product/details/", 900), ("/v4/city/", 3600))
DEFAULT_TTL = 300

_transport: httpx.AsyncBaseTransport | None = None
_client: httpx.AsyncClient | None = None
_limit: asyncio.Semaphore | None = None
_proxy_index = 0
_clearance: dict[str, str] = {}  # cookies a headless browser got from torob.com, sent on every call
_browser_lock: asyncio.Lock | None = None
_cache: dict[str, tuple[float, Any]] = {}
_blocked_until = 0.0
_next_slot = 0.0
_cleared_at = -1e9  # when a browser last got cookies; callers queued on the lock reuse them
RECENT_CLEAR = 30  # seconds


class ApiError(ToolError):
    """A failed upstream call."""


class _CurlTransport(AsyncCurlTransport):
    """curl_cffi transport that drops the Chrome profile's navigation-only headers (None disables a header)."""

    def _create_request_params(self, req: httpx.Request) -> dict[str, Any]:
        params = super()._create_request_params(req)
        params["headers"] = [(k, None if k.lower() in _NAV_ONLY else v) for k, v in params["headers"]]
        return params


class Blocked(ApiError):
    """Torob's bot protection (HTTP 490) is refusing this IP."""


def _now() -> float:
    return time.monotonic()


def reset() -> None:
    """Forget the cache, the cooldown and the pacing slot (tests)."""
    global _blocked_until, _next_slot, _cleared_at
    _cache.clear()
    _clearance.clear()
    _blocked_until = _next_slot = 0.0
    _cleared_at = -1e9


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Swap the transport (tests use httpx.MockTransport). Drops the current client."""
    global _transport, _client, _limit, _proxy_index, _browser_lock
    _transport, _client, _limit, _proxy_index, _browser_lock = transport, None, None, 0, None


def _proxies() -> list[str]:
    """TOROB_MCP_PROXY: one proxy URL, or a comma-separated list used one at a time."""
    return [p.strip() for p in os.environ.get("TOROB_MCP_PROXY", "").split(",") if p.strip()]


def _next_proxy(client: httpx.AsyncClient) -> bool:
    """Sticky pool: after a block or connect error move on and rebuild the client. False when there is no other proxy.

    Only the client that failed may rotate, so concurrent failures do not skip proxies. The old client is not
    closed: other requests may still be using it.
    """
    global _client, _limit, _proxy_index
    proxies = _proxies()
    if len(proxies) < 2 or client is not _client:
        return client is not _client  # someone else already rotated: just retry on the new client
    _client, _limit, _proxy_index = None, None, (_proxy_index + 1) % len(proxies)
    return True


def _get_client() -> tuple[httpx.AsyncClient, asyncio.Semaphore]:
    global _client, _limit
    if _client is None:
        # a proxy is only used when the user opts in with TOROB_MCP_PROXY; curl_cffi presents Chrome's TLS/HTTP2
        # fingerprint and default headers, and takes the proxy itself (a client-level proxy= is ignored once a
        # transport is given). Cookies stay with httpx (cleared after each call, see _send).
        proxies = _proxies()
        _client = httpx.AsyncClient(
            transport=_transport
            or _CurlTransport(
                impersonate="chrome",
                default_headers=True,
                proxy=proxies[_proxy_index % len(proxies)] if proxies else None,
                max_connections=2 * MAX_CONCURRENCY,
            ),
            headers=HEADERS,
            timeout=30,
            follow_redirects=False,  # the API never redirects; do not follow one to another host
            trust_env=False,
        )
        _limit = asyncio.Semaphore(MAX_CONCURRENCY)
    assert _limit is not None
    return _client, _limit


def _ttl(path: str) -> int:
    return next((t for prefix, t in TTLS if path.startswith(prefix)), DEFAULT_TTL)


def _cooldown_left() -> int:
    """Whole minutes (rounded up) until calls are allowed again, 0 when not blocked."""
    return max(0, -int(-(_blocked_until - _now()) // 60)) if _blocked_until > _now() else 0


def _blocked_message(minutes: int, hint: str = "") -> str:
    return (
        f"Torob's bot protection (HTTP 490) is asking for a CAPTCHA and blocks this IP. No requests will be sent for "
        f"{minutes} more minute(s); wait that long or set TOROB_MCP_PROXY to another proxy (ideally an Iranian "
        f"residential exit). This server does not solve CAPTCHAs.{hint}"
    )


def _is_challenge(r: httpx.Response) -> bool:
    """Torob's WAF page: HTTP 490, a captcha page with another status, or an HTML 403/429/503 (API errors are JSON)."""
    if r.status_code == 490:
        return True
    if r.status_code in (403, 429, 503):
        return r.content.lstrip()[:1] == b"<"
    return (
        r.status_code < 400
        and "json" not in r.headers.get("content-type", "")
        and bool(re.search(rb"captcha", r.content[:4000], re.I))
    )


async def _clear_challenge() -> tuple[dict[str, str], str]:
    """Rung 2: open torob.com once in headless patchright. Returns (cookies, hint); no cookies when it failed.

    Only a challenge that clears by itself (JS check) can pass; an interactive ArCaptcha cannot.
    """
    global _browser_lock, _cleared_at
    try:
        from patchright.async_api import async_playwright
    except ImportError:
        return {}, " Install torob-mcp[browser] and run `patchright install chromium` to let a headless browser try."
    _browser_lock = _browser_lock or asyncio.Lock()
    proxies = _proxies()
    proxy = None
    if proxies:
        u = httpx.URL(proxies[_proxy_index % len(proxies)])
        proxy = {"server": f"{u.scheme}://{u.host}:{u.port}", "username": u.username, "password": u.password}
    async with _browser_lock:
        # callers queued on the lock: a cooldown or a fresh clear by the previous holder means no second browser
        if _cooldown_left():
            return {}, ""
        if _now() - _cleared_at < RECENT_CLEAR and _clearance:
            return dict(_clearance), ""
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, proxy=proxy)
                try:
                    page = await (await browser.new_context(locale="fa-IR")).new_page()
                    resp = await page.goto(f"{SITE}/", wait_until="domcontentloaded", timeout=BROWSER_WAIT * 1000)
                    deadline = _now() + BROWSER_WAIT
                    while _now() < deadline and (
                        (resp and resp.status == 490) or "captcha" in (await page.content())[:200_000].lower()
                    ):
                        await asyncio.sleep(1)
                        resp = None  # the first answer is stale once the page has had time to reload
                    cleared = "captcha" not in (await page.content())[:200_000].lower()
                    cookies = {c["name"]: c["value"] for c in await page.context.cookies() if c["value"]}
                finally:
                    await browser.close()
        except Exception as e:  # patchright raises its own Error types, e.g. when chromium is not installed
            return {}, f" The headless browser failed ({type(e).__name__}); run `patchright install chromium`."
    if not cleared:
        return {}, " A headless browser was tried; the CAPTCHA did not clear."
    _cleared_at = _now()
    _clearance.update(cookies)  # set before the lock is released so queued callers see them
    return cookies, ""


async def api(
    path: str,
    params: dict[str, Any] | None = None,
    *,
    city: int | None = None,
    guard: bool = True,
) -> Any:
    """GET `path` on api.torob.com and return the parsed JSON (dict or list).

    `city` sends the `deliver_city` cookie for this one request (Torob filters sellers by it; no cookie jar is kept).
    `guard=False` is for /suggestion2/, which Torob does not rate limit: no pacing and it works during a cooldown.
    """
    global _blocked_until, _next_slot
    query = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in (params or {}).items() if v is not None}
    key = f"{path}?{json.dumps(query, sort_keys=True, ensure_ascii=False)}#{city}"
    hit = _cache.get(key)
    if hit and hit[0] > _now():
        return hit[1]
    if guard and (left := _cooldown_left()):
        raise Blocked(_blocked_message(left))
    if guard and MIN_INTERVAL > 0:
        now = _now()
        wait = _next_slot - now
        _next_slot = max(now, _next_slot) + MIN_INTERVAL
        if wait > 0:
            await asyncio.sleep(wait)

    def send() -> Any:
        cookies = {**_clearance, **({"deliver_city": str(city)} if city else {})}
        return _send(
            f"{API}{path}", query, {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())} if cookies else None
        )

    r, client = await send()
    hint = ""
    if _is_challenge(r):
        if _next_proxy(client):  # a fresh IP first
            r, client = await send()
        if _is_challenge(r) and BROWSER:
            cookies, hint = await _clear_challenge()
            if cookies:
                _clearance.update(cookies)
                r, client = await send()
    if _is_challenge(r):
        _blocked_until = _now() + COOLDOWN
        raise Blocked(_blocked_message(COOLDOWN // 60, hint))
    if r.status_code >= 400:
        raise ApiError(_status_message(r.status_code, r))
    try:
        data = r.json()
    except ValueError as e:
        raise ApiError(f"torob.com returned a non-JSON response for {path} (HTTP {r.status_code}).") from e
    if len(_cache) >= CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (_now() + _ttl(path), data)
    return data


async def _send(
    url: str, params: dict[str, Any], headers: dict[str, str] | None
) -> tuple[httpx.Response, httpx.AsyncClient]:
    """GET with up to MAX_RETRIES retries on timeouts, dropped connections, 429 and 502/503/504; 403/490 never."""
    for attempt in range(MAX_RETRIES + 1):
        client, limit = _get_client()
        last = attempt == MAX_RETRIES
        wait = min(RETRY_DELAY * 2**attempt * random.uniform(1, 1.5), MAX_RETRY_WAIT)
        try:
            async with limit:
                if delay := random.uniform(*JITTER):
                    await asyncio.sleep(delay)
                r = await client.get(url, params=params, headers=headers)
        except httpx.TimeoutException as e:
            if last:
                raise ApiError("torob.com did not answer in time. Try again in a moment.") from e
            await asyncio.sleep(wait)
            continue
        except httpx.TransportError as e:
            if isinstance(e, httpx.ConnectError | httpx.ProxyError):
                _next_proxy(client)
            if last:
                raise ApiError(
                    f"Could not reach torob.com ({type(e).__name__}). Check the internet connection, "
                    "or set TOROB_MCP_PROXY."
                ) from e
            await asyncio.sleep(wait)
            continue
        finally:
            client.cookies.clear()  # never carry Torob's cookies (search_session, deliver_city) between calls
        if not last and r.status_code in RETRY_STATUS and not _is_challenge(r):
            ra = r.headers.get("retry-after", "")
            await asyncio.sleep(min(float(ra), MAX_RETRY_WAIT) if ra.replace(".", "", 1).isdigit() else wait)
            continue
        break
    return r, client


def _upstream_message(r: httpx.Response) -> str:
    """Torob error bodies come in three shapes: {message}, {error: {message}} and an echo of the params."""
    try:
        body = r.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        err = body.get("error")
        msg = err.get("message") if isinstance(err, dict) else body.get("message")
        return msg if isinstance(msg, str) else ""
    return ""


def _status_message(code: int, r: httpx.Response) -> str:
    said = _upstream_message(r)
    tail = f" Torob said: {said}" if said else ""
    if code == 400:
        return f"torob.com rejected the parameters (HTTP 400).{tail}"
    if code in (401, 403):
        return f"torob.com refused the request (HTTP {code}). Turn off VPN/proxy or set TOROB_MCP_PROXY.{tail}"
    if code == 404:
        return f"Not found on torob.com (HTTP 404). The product id, shop id or path may be wrong.{tail}"
    if code == 429:
        return "torob.com is rate limiting requests (HTTP 429). Wait a minute before retrying."
    if code >= 500:
        return f"torob.com had a server error (HTTP {code}). Try again later."
    return f"torob.com rejected the request (HTTP {code}).{tail}"
