"""Helpers for the live tests (run with -m live).

Torob blocks IPs after a burst (HTTP 490). A blocked call skips the test instead of failing it: the block is the
network's state, not a bug. The cooldown in torob_mcp.http keeps later tests from sending anything while blocked.
"""

import pytest


async def call(client, name, args=None):
    result = await client.call_tool(name, args or {})
    text = result.content[0].text
    if result.is_error and "bot protection" in text:
        pytest.skip(f"Torob blocked this IP (HTTP 490): {text[:90]}")
    assert not result.is_error, text
    return result.structured_content
