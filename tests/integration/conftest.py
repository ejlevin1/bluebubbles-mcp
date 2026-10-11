from __future__ import annotations

import asyncio
import os
import pathlib
from typing import Any

import pytest

from bb_mcp.client import BlueBubblesClient
from bb_mcp.server import _read_send_method, _send_capable


def _load_dotenv() -> None:
    """Populate os.environ from the repo's .env, if there is one.

    `just` loads .env itself (`set dotenv-load := true`), but a bare
    `pytest tests/integration` does not — without this the write tests would skip
    for the wrong reason. Existing environment variables win.
    """
    env_path = pathlib.Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            value = value.strip()
            # Values may be quoted — a chat GUID contains `;`, which has to be
            # quoted for POSIX `. ./.env` sourcing to survive it.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


_load_dotenv()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "integration: requires a live BlueBubbles server"
    )
    config.addinivalue_line(
        "markers",
        "write: sends real, permanent messages; needs TEST_WRITE_GUID (see conftest)",
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


@pytest.fixture(scope="session")
def server_info(bb_url: str, bb_password: str) -> dict[str, Any]:
    """One live ``GET /server/info`` for the whole session.

    Probed independently of the `client` fixture: `client` has to know the send
    capability before it is handed to a test, so it cannot depend on a fixture that
    itself depends on a client. Session-scoped because this is a real HTTP call and
    the answer does not change under a test run.

    Synchronous, with its own ``asyncio.run``, so that a session-scoped fixture
    never has to borrow pytest-asyncio's function-scoped event loop.
    """

    async def _probe() -> dict[str, Any]:
        c = BlueBubblesClient(bb_url, bb_password)
        try:
            return await c.server_info()
        finally:
            await c.close()

    return asyncio.run(_probe())


@pytest.fixture(scope="session")
def send_capable(server_info: dict[str, Any]) -> bool:
    """Whether sends on this server may go out over the Private API.

    Deliberately `bb_mcp.server`'s own predicate rather than a second copy: it folds
    in ``helper_connected`` (truthiness, so a Node-serialised ``0`` counts as
    disconnected) and the ``BLUEBUBBLES_SEND_METHOD`` override, and a copy here
    would drift from the thing under test.
    """
    return _send_capable(server_info, _read_send_method())


@pytest.fixture
async def client(bb_url: str, bb_password: str, send_capable: bool):  # type: ignore[return]
    c = BlueBubblesClient(bb_url, bb_password)
    # `lifespan` assigns this after probing /server/info; without it every
    # client-level integration test would silently exercise only the AppleScript
    # fallback, which is the blind spot the capability check exists to remove.
    c.private_api = send_capable
    yield c
    await c.close()


@pytest.fixture
async def first_chat_guid(client: BlueBubblesClient) -> str:
    chats = await client.list_chats(limit=1)
    if not chats:
        pytest.skip("No chats available on the server")
    return chats[0]["guid"]


@pytest.fixture(scope="session")
def private_api(server_info: dict[str, Any]) -> bool:
    """Whether the server has the Private API enabled.

    Several routes (handle availability, reactions, edits) 500 outright without it,
    so tests that need them should skip rather than fail on a server that simply is
    not configured for them.

    This is the raw toggle, matching `lifespan`'s use of it for the Private-API
    *routes*. Sends are a different question — see `send_capable`.
    """
    return bool(server_info.get("private_api"))


@pytest.fixture
def test_write_guid() -> str:
    """Chat GUID that write tests are allowed to send into, or skip.

    Set TEST_WRITE_GUID in .env to a chat GUID you own — a self-chat, e.g.
    ``any;-;you@example.com``. Leave it unset and every write test skips, which is
    what happens in CI.

    These sends are REAL and, without the Private API, cannot be unsent. Never point
    this at someone else's conversation.
    """
    guid = (os.environ.get("TEST_WRITE_GUID") or "").strip()
    if not guid:
        pytest.skip("TEST_WRITE_GUID not set — skipping write tests")
    return guid
