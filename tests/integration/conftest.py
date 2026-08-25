from __future__ import annotations

import os

import pytest

from bb_mcp.client import BlueBubblesClient


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "integration: requires a live BlueBubbles server"
    )


@pytest.fixture(scope="session")
def bb_url() -> str:
    url = os.environ.get("BLUEBUBBLES_URL")
    if not url:
        pytest.skip("BLUEBUBBLES_URL not set")
    return url  # type: ignore[return-value]


@pytest.fixture(scope="session")
def bb_password() -> str:
    pw = os.environ.get("BLUEBUBBLES_PASSWORD")
    if not pw:
        pytest.skip("BLUEBUBBLES_PASSWORD not set")
    return pw  # type: ignore[return-value]


@pytest.fixture
async def client(bb_url: str, bb_password: str):  # type: ignore[return]
    c = BlueBubblesClient(bb_url, bb_password)
    yield c
    await c.close()


@pytest.fixture
async def first_chat_guid(client: BlueBubblesClient) -> str:
    chats = await client.list_chats(limit=1)
    if not chats:
        pytest.skip("No chats available on the server")
    return chats[0]["guid"]


@pytest.fixture
async def private_api(client: BlueBubblesClient) -> bool:
    """Whether the server has the Private API enabled.

    Several routes (handle availability, reactions, edits) 500 outright without it,
    so tests that need them should skip rather than fail on a server that simply is
    not configured for them.
    """
    info = await client.server_info()
    return bool(info.get("private_api"))
