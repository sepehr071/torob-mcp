import json
from pathlib import Path

import httpx
import pytest
from mcp import Client

from torob_mcp import http
from torob_mcp.server import mcp

FIXTURES = Path(__file__).parent / "fixtures"
LIVE_MIN_INTERVAL = http.MIN_INTERVAL

PRK = "57ea65ae-0798-4cd0-96a7-38d8af180345"  # the product of details.json
PRK2 = "158600cc-5178-4bcc-87f0-1688ce3e5847"  # second card of search.json


def fixture(name: str):
    """A recorded (trimmed) response, parsed JSON."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def fresh_http_client(request):
    """The shared httpx client is bound to one event loop; each test gets a new loop.

    Offline tests start with an empty cache, no cooldown and no pacing. Live tests keep the cache, the cooldown and
    the real pacing across tests, so a Torob block (HTTP 490) is not hammered again by every test.
    """
    live = request.node.get_closest_marker("live") is not None
    http.set_transport(None)
    http.RETRY_DELAY = 0
    http.JITTER = (0, 0)
    http.BROWSER = False
    http.MIN_INTERVAL = LIVE_MIN_INTERVAL if live else 0
    if not live:
        http.reset()
    yield
    http.set_transport(None)
    http.MIN_INTERVAL = LIVE_MIN_INTERVAL


@pytest.fixture
def api():
    """Route table for a fake Torob API.

    Keys are URL paths (api["/v4/base-product/details/"]). Values: a dict/list (JSON) or a callable(request) ->
    httpx.Response. Unknown paths return 404 with an empty body. Every request is appended to api.calls.
    """

    class Routes(dict):
        calls: list[httpx.Request]

        def params(self, i: int = -1) -> dict[str, str]:
            return dict(self.calls[i].url.params)

        def paths(self) -> list[str]:
            return [c.url.path for c in self.calls]

    routes = Routes()
    routes.calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        routes.calls.append(request)
        target = routes.get(request.url.path)
        if target is None:
            return httpx.Response(404)
        if callable(target):
            return target(request)
        return httpx.Response(200, json=target)

    http.set_transport(httpx.MockTransport(handler))
    return routes


@pytest.fixture
async def client():
    async with Client(mcp, raise_exceptions=True) as c:
        call_tool = c.call_tool

        async def parsed(*args, **kwargs):
            result = await call_tool(*args, **kwargs)
            if not result.is_error:
                result.structured_content = json.loads(result.content[0].text)
            return result

        c.call_tool = parsed
        yield c
