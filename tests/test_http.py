import json
import re
from pathlib import Path

import httpx
import pytest
from conftest import PRK, fixture

from torob_mcp import http

pytestmark = pytest.mark.anyio

SEARCH = "/v4/base-product/search/"
BLOCK_HTML = "<html><body>آیا شما یک ربات هستید؟ captcha</body></html>"


async def test_all_tools_are_read_only(client):
    tools = (await client.list_tools()).tools
    assert len(tools) == 26
    for t in tools:
        assert t.name.startswith("tb_"), t.name
        assert t.annotations.read_only_hint is True and t.annotations.destructive_hint is False, t.name
        assert t.description and t.title, t.name


async def test_first_docstring_line_is_a_complete_sentence(client):
    for t in (await client.list_tools()).tools:
        first = t.description.splitlines()[0]
        assert first.endswith((".", ")")) and len(first) < 130, (t.name, first)


async def test_readme_tool_tables_match_the_server(client):
    readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    listed = re.findall(r"^\| `(tb_\w+)` \|", readme, re.M)
    assert sorted(listed) == sorted(t.name for t in (await client.list_tools()).tools)


async def test_dropped_connection_is_retried_once(client, api):
    attempts = []

    def flaky(request):
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)
        return httpx.Response(200, json=fixture("suggest.json"))

    api["/suggestion2/"] = flaky
    result = await client.call_tool("tb_suggest", {"text": "ayfon"})
    assert not result.is_error and len(attempts) == 2


async def test_network_error_after_retry(client, api):
    def down(request):
        raise httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING]", request=request)

    api["/suggestion2/"] = down
    result = await client.call_tool("tb_suggest", {"text": "x"})
    assert result.is_error and "Could not reach torob.com (ConnectError)" in result.content[0].text
    assert len(api.calls) == 3


async def test_timeout_is_retried_twice(client, api):
    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    api["/suggestion2/"] = slow
    result = await client.call_tool("tb_suggest", {"text": "x"})
    assert result.is_error and "did not answer in time" in result.content[0].text and len(api.calls) == 3


async def test_503_is_retried_once(client, api):
    replies = iter([httpx.Response(503), httpx.Response(200, json=fixture("suggest.json"))])
    api["/suggestion2/"] = lambda r: next(replies)
    result = await client.call_tool("tb_suggest", {"text": "x"})
    assert not result.is_error and len(api.calls) == 2


@pytest.mark.parametrize(
    ("status", "text"),
    [(404, "HTTP 404"), (429, "rate limiting"), (500, "server error (HTTP 500)"), (403, "refused"), (400, "rejected")],
)
async def test_http_errors_are_actionable(client, api, status, text):
    api["/suggestion2/"] = lambda r: httpx.Response(status)
    result = await client.call_tool("tb_suggest", {"text": "x"})
    assert result.is_error and text in result.content[0].text


async def test_upstream_error_message_is_passed_on(client, api):
    api["/v4/internet-shop/details/"] = lambda r: httpx.Response(
        400, json={"error": {"message": "شناسه‌ی فروشگاه معتبر نیست."}}
    )
    result = await client.call_tool("tb_shop", {"shop_id": 5})
    assert result.is_error and "HTTP 400" in result.content[0].text and "معتبر نیست" in result.content[0].text


async def test_non_json_reply(client, api):
    api["/suggestion2/"] = lambda r: httpx.Response(
        200, text="<html>oops</html>", headers={"content-type": "text/html"}
    )
    result = await client.call_tool("tb_suggest", {"text": "x"})
    assert result.is_error and "non-JSON" in result.content[0].text


async def test_490_starts_a_cooldown_and_no_more_requests_are_sent(client, api, monkeypatch):
    api[SEARCH] = lambda r: httpx.Response(490, text=BLOCK_HTML, headers={"content-type": "text/html"})
    first = await client.call_tool("tb_search", {"query": "x"})
    assert first.is_error and "HTTP 490" in first.content[0].text and "30 more minute" in first.content[0].text
    assert "does not solve CAPTCHAs" in first.content[0].text
    sent = len(api.calls)
    again = await client.call_tool("tb_search", {"query": "y"})
    assert again.is_error and "30 more minute" in again.content[0].text and len(api.calls) == sent  # nothing sent
    # time passes: 10 minutes in, 20 are left
    monkeypatch.setattr(http, "_now", lambda: http._blocked_until - 20 * 60 + 5)
    later = await client.call_tool("tb_search", {"query": "y"})
    assert "20 more minute" in later.content[0].text and len(api.calls) == sent
    # cooldown over: requests go out again
    monkeypatch.setattr(http, "_now", lambda: http._blocked_until + 1)
    api[SEARCH] = fixture("search.json")
    ok = await client.call_tool("tb_search", {"query": "x"})
    assert not ok.is_error and len(api.calls) > sent


async def test_captcha_page_with_status_200_is_also_a_block(client, api):
    api[SEARCH] = lambda r: httpx.Response(200, text=BLOCK_HTML, headers={"content-type": "text/html"})
    result = await client.call_tool("tb_search", {"query": "x"})
    assert result.is_error and "bot protection" in result.content[0].text


async def test_suggest_still_works_during_a_cooldown(client, api):
    api[SEARCH] = lambda r: httpx.Response(490, text=BLOCK_HTML, headers={"content-type": "text/html"})
    api["/suggestion2/"] = fixture("suggest.json")
    await client.call_tool("tb_search", {"query": "x"})
    ok = await client.call_tool("tb_suggest", {"text": "ayfon"})
    assert not ok.is_error and ok.structured_content["suggestions"]


async def test_cookies_are_never_kept_and_city_cookie_is_per_request(client, api):
    def reply(request):
        return httpx.Response(200, json=fixture("sellers.json"), headers={"set-cookie": "deliver_city=392; Path=/"})

    api["/v4/base-product/sellers/"] = reply
    api["/v4/city/list/"] = fixture("cities.json")
    await client.call_tool("tb_sellers", {"product": PRK})
    await client.call_tool("tb_sellers", {"product": PRK, "city": 712})
    await client.call_tool("tb_sellers", {"product": PRK, "city": 468})
    sellers = [c for c in api.calls if c.url.path.endswith("/sellers/")]
    assert "cookie" not in sellers[0].headers  # nothing carried over from Torob's set-cookie
    assert sellers[1].headers["cookie"] == "deliver_city=712" and sellers[2].headers["cookie"] == "deliver_city=468"


async def test_responses_are_cached(client, api):
    api["/suggestion2/"] = fixture("suggest.json")
    await client.call_tool("tb_suggest", {"text": "ayfon"})
    await client.call_tool("tb_suggest", {"text": "ayfon"})
    await client.call_tool("tb_suggest", {"text": "other"})
    assert len(api.calls) == 2


async def test_requests_are_spaced(client, api, monkeypatch):
    waits = []

    async def fake_sleep(s):
        waits.append(s)

    monkeypatch.setattr(http, "MIN_INTERVAL", 10)
    monkeypatch.setattr(http.asyncio, "sleep", fake_sleep)
    api["/v4/search-trends/"] = fixture("trends.json")
    api["/v4/city/most-visited/list/"] = fixture("cities_top.json")
    await client.call_tool("tb_trends", {})
    await client.call_tool("tb_cities", {})
    assert len(waits) == 1 and 9 < waits[0] <= 10  # the second call waited for its slot


async def test_browser_user_agent_and_no_env_proxy(client, api, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")  # must be ignored (trust_env=False)
    api["/suggestion2/"] = fixture("suggest.json")
    result = await client.call_tool("tb_suggest", {"text": "x"})
    assert not result.is_error and "User-Agent" not in http.HEADERS  # the impersonated Chrome profile sends it
    assert api.calls[0].headers["Origin"] == "https://torob.com"
    assert api.calls[0].method == "GET" and api.calls[0].url.host == "api.torob.com"


async def test_redirects_are_not_followed(client, api):
    api["/suggestion2/"] = lambda r: httpx.Response(302, headers={"location": "https://evil.example/x"})
    result = await client.call_tool("tb_suggest", {"text": "x"})
    assert result.is_error and len(api.calls) == 1


async def test_raw_client_one_text_block_no_structured_content_no_output_schema():
    from mcp import Client

    from torob_mcp.server import mcp

    http.set_transport(httpx.MockTransport(lambda r: httpx.Response(200, json=fixture("suggest.json"))))
    async with Client(mcp) as raw:  # the `client` fixture patches call_tool; this one does not
        tools = (await raw.list_tools()).tools
        assert all(t.output_schema is None for t in tools)
        assert all('"title"' not in json.dumps(t.input_schema) for t in tools)
        result = await raw.call_tool("tb_suggest", {"text": "ayfon"})
    assert len(result.content) == 1 and result.content[0].type == "text" and result.structured_content is None


async def test_navigation_only_headers_are_dropped_from_the_curl_request():
    transport = http._CurlTransport(impersonate="chrome", default_headers=True)
    req = httpx.Request(
        "GET", "https://api.torob.com/x", headers=http.HEADERS, extensions={"timeout": {"connect": 1, "read": 1}}
    )
    sent = dict(transport._create_request_params(req)["headers"])
    assert sent[b"upgrade-insecure-requests"] is None and sent[b"sec-fetch-user"] is None
    assert sent[b"sec-fetch-mode"] == b"cors"


async def test_429_waits_for_retry_after_and_403_is_not_retried(client, api, monkeypatch):
    waits = []

    async def fake_sleep(s):
        waits.append(s)

    monkeypatch.setattr(http.asyncio, "sleep", fake_sleep)
    replies = iter(
        [httpx.Response(429, headers={"retry-after": "3"}), httpx.Response(200, json=fixture("suggest.json"))]
    )
    api["/suggestion2/"] = lambda r: next(replies)
    assert not (await client.call_tool("tb_suggest", {"text": "x"})).is_error and waits == [3.0]
    api["/suggestion2/"] = lambda r: httpx.Response(403)
    calls = len(api.calls)
    assert (await client.call_tool("tb_suggest", {"text": "y"})).is_error and len(api.calls) == calls + 1


async def test_proxy_pool_moves_on_after_a_challenge(client, api, monkeypatch):
    monkeypatch.setenv("TOROB_MCP_PROXY", "http://a:1, http://b:2")
    clients = []
    replies = iter([httpx.Response(490, text=BLOCK_HTML), httpx.Response(200, json=fixture("suggest.json"))])

    def reply(request):
        clients.append(http._client)
        return next(replies)

    api["/suggestion2/"] = reply
    result = await client.call_tool("tb_suggest", {"text": "x"})
    assert not result.is_error and clients[0] is not clients[1] and http._proxy_index == 1
    assert http._cooldown_left() == 0


async def test_browser_cookies_are_sent_and_retried_once(client, api, monkeypatch):
    async def solved():
        return {"trb_clearance": "abc"}, ""

    monkeypatch.setattr(http, "BROWSER", True)
    monkeypatch.setattr(http, "_clear_challenge", solved)
    replies = iter([httpx.Response(490, text=BLOCK_HTML), httpx.Response(200, json=fixture("suggest.json"))])
    api["/suggestion2/"] = lambda r: next(replies)
    result = await client.call_tool("tb_suggest", {"text": "x"})
    assert not result.is_error and len(api.calls) == 2
    assert api.calls[1].headers["cookie"] == "trb_clearance=abc" and "cookie" not in api.calls[0].headers


async def test_browser_that_cannot_pass_the_captcha_ends_in_a_cooldown(client, api, monkeypatch):
    runs = []

    async def stuck():
        runs.append(1)
        return {}, " A headless browser was tried; the CAPTCHA did not clear."

    monkeypatch.setattr(http, "BROWSER", True)
    monkeypatch.setattr(http, "_clear_challenge", stuck)
    api[SEARCH] = lambda r: httpx.Response(490, text=BLOCK_HTML)
    first = await client.call_tool("tb_search", {"query": "x"})
    assert first.is_error and "CAPTCHA did not clear" in first.content[0].text and len(api.calls) == 1
    await client.call_tool("tb_search", {"query": "y"})
    assert len(runs) == 1 and len(api.calls) == 1  # no loop: the cooldown stops the browser and the requests


async def test_queued_callers_reuse_a_fresh_clearance_and_launch_no_second_browser(api, monkeypatch):
    import asyncio
    import sys
    import types

    launches = []

    class Page:
        async def goto(self, *a, **k):
            return types.SimpleNamespace(status=200)

        async def content(self):
            return "<html>ok</html>"

    class Ctx:
        async def new_page(self):
            page = Page()
            page.context = self
            return page

        async def cookies(self):
            return [{"name": "trb_clearance", "value": "abc"}]

    class Browser:
        async def new_context(self, **k):
            return Ctx()

        async def close(self):
            pass

    class Playwright:
        async def __aenter__(self):
            return types.SimpleNamespace(chromium=self)

        async def __aexit__(self, *a):
            pass

        async def launch(self, **k):
            launches.append(1)
            await asyncio.sleep(0.05)  # let the second caller reach the lock
            return Browser()

    fake = types.ModuleType("patchright.async_api")
    fake.async_playwright = Playwright
    monkeypatch.setitem(sys.modules, "patchright", types.ModuleType("patchright"))
    monkeypatch.setitem(sys.modules, "patchright.async_api", fake)
    monkeypatch.setattr(http, "BROWSER", True)

    def ok(r):
        has = "trb_clearance" in r.headers.get("cookie", "")
        return httpx.Response(200, json={}) if has else httpx.Response(490, text=BLOCK_HTML)

    api["/v4/a/"] = api["/v4/b/"] = ok
    await asyncio.gather(http.api("/v4/a/"), http.api("/v4/b/"))
    assert len(launches) == 1
