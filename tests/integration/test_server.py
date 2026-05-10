"""Integration tests: server connectivity (read-only)."""

from __future__ import annotations

import pytest

from bb_mcp.client import BlueBubblesClient

pytestmark = pytest.mark.integration


async def test_ping(client: BlueBubblesClient) -> None:
    result = await client.ping()
    assert result is not None


async def test_server_info(client: BlueBubblesClient) -> None:
    info = await client.server_info()
    assert isinstance(info, dict)
    assert "os_version" in info or "server_version" in info or len(info) > 0
