"""Integration tests: MCP server tool behaviour (requires live BlueBubbles server).

Tests in this module use the fastmcp in-memory Client to exercise the full MCP
tool layer (lifespan, parameter stripping, return types) rather than calling
the BlueBubbles HTTP client directly.

Each test that needs a live server creates a fresh FastMCP instance so that
lifespan side-effects (tool removal, schema patching) don't leak between tests.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

import pytest
from fastmcp import Client

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_env() -> tuple[str, str]:
    """Return (url, password) or skip if env vars not set."""
    url = os.environ.get("BLUEBUBBLES_URL")
    pw = os.environ.get("BLUEBUBBLES_PASSWORD")
    if not url or not pw:
        pytest.skip("BLUEBUBBLES_URL / BLUEBUBBLES_PASSWORD not set")
    return url, pw  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# mask_error_details flag / env var
# ---------------------------------------------------------------------------


class TestMaskErrors:
    """Test --mask-errors / --no-mask-errors CLI flag and BLUEBUBBLES_MASK_ERRORS env var."""

    def _invoke(self, args: list[str], env: dict[str, str] | None = None) -> bool:
        """Invoke the Typer app and return the resolved mask_error_details value."""
        from typer.testing import CliRunner
        from bb_mcp.server import app, mcp

        captured: list[bool] = []
        original_run = mcp.run

        def fake_run(**kwargs) -> None:  # type: ignore[no-untyped-def]
            captured.append(mcp.mask_error_details)

        mcp.run = fake_run  # type: ignore[method-assign]
        try:
            runner = CliRunner(env=env)
            runner.invoke(app, args, catch_exceptions=False)
        finally:
            mcp.run = original_run  # type: ignore[method-assign]

        assert captured, "main() did not call mcp.run()"
        return captured[0]

    def test_default_is_true(self) -> None:
        """mask_error_details defaults to True (production-safe)."""
        _require_env()
        assert self._invoke([]) is True

    def test_no_mask_errors_flag(self) -> None:
        """--no-mask-errors sets mask_error_details to False."""
        _require_env()
        assert self._invoke(["--no-mask-errors"]) is False

    def test_mask_errors_flag_explicit(self) -> None:
        """--mask-errors sets mask_error_details to True."""
        _require_env()
        assert self._invoke(["--mask-errors"]) is True

    def test_env_var_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """BLUEBUBBLES_MASK_ERRORS=false disables masking."""
        _require_env()
        assert self._invoke([], env={"BLUEBUBBLES_MASK_ERRORS": "false"}) is False

    def test_env_var_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """BLUEBUBBLES_MASK_ERRORS=true enables masking."""
        _require_env()
        assert self._invoke([], env={"BLUEBUBBLES_MASK_ERRORS": "true"}) is True

    def test_flag_overrides_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CLI flag takes precedence over env var."""
        _require_env()
        # env says false but flag says mask-errors → True wins
        assert (
            self._invoke(["--mask-errors"], env={"BLUEBUBBLES_MASK_ERRORS": "false"})
            is True
        )


# ---------------------------------------------------------------------------
# get_contacts query filter (live server via in-memory Client)
# ---------------------------------------------------------------------------


@pytest.fixture
async def live_client() -> AsyncGenerator[Client, None]:
    """In-memory MCP client connected to a fresh server instance."""
    _require_env()
    # Use the module-level mcp but wrap in a client — each test gets its own
    # connection lifecycle (lifespan runs fresh per Client context).
    from bb_mcp.server import mcp

    async with Client(mcp) as client:
        yield client


class TestGetContactsFilter:
    async def test_no_query_returns_list(self, live_client: Client) -> None:
        """get_contacts with no query returns a list."""
        result = await live_client.call_tool("get_contacts", {})
        assert isinstance(result.data, list)

    async def test_query_returns_subset(self, live_client: Client) -> None:
        """get_contacts with a query returns fewer or equal results than unfiltered."""
        all_contacts = (await live_client.call_tool("get_contacts", {})).data
        filtered = (await live_client.call_tool("get_contacts", {"query": "a"})).data
        assert isinstance(filtered, list)
        assert len(filtered) <= len(all_contacts)

    async def test_query_results_match_filter(self, live_client: Client) -> None:
        """Every contact returned by a query actually contains the query string."""
        query = "a"
        filtered = (await live_client.call_tool("get_contacts", {"query": query})).data
        assert isinstance(filtered, list)
        for contact in filtered:
            name = (contact.get("displayName") or "").lower()
            phones = [
                (p.get("address") or "").lower()
                for p in (contact.get("phoneNumbers") or [])
            ]
            emails = [
                (e.get("address") or "").lower() for e in (contact.get("emails") or [])
            ]
            all_fields = [name] + phones + emails
            assert any(query in f for f in all_fields), (
                f"Contact {contact.get('displayName')!r} does not match query {query!r}"
            )

    async def test_nonsense_query_returns_empty(self, live_client: Client) -> None:
        """A query that matches nothing returns an empty list."""
        result = await live_client.call_tool(
            "get_contacts", {"query": "zzz_no_match_xqq"}
        )
        assert result.data == []


# ---------------------------------------------------------------------------
# get_unread_chats parallel fetch
# ---------------------------------------------------------------------------


class TestGetUnreadChats:
    async def test_returns_list(self, live_client: Client) -> None:
        """get_unread_chats always returns a list (even when empty)."""
        result = await live_client.call_tool("get_unread_chats", {})
        assert isinstance(result.data, list)

    async def test_each_item_has_chat_and_messages(self, live_client: Client) -> None:
        """Each entry in get_unread_chats has 'chat' and 'recent_messages' keys."""
        result = (await live_client.call_tool("get_unread_chats", {})).data
        for item in result:
            assert "chat" in item, f"Missing 'chat' key in {item}"
            assert "recent_messages" in item, f"Missing 'recent_messages' key in {item}"
            assert isinstance(item["recent_messages"], list)

    async def test_message_limit_respected(self, live_client: Client) -> None:
        """message_limit parameter caps the number of messages per chat."""
        result = (
            await live_client.call_tool("get_unread_chats", {"message_limit": 2})
        ).data
        for item in result:
            assert len(item["recent_messages"]) <= 2, (
                f"Expected at most 2 messages, got {len(item['recent_messages'])}"
            )

    async def test_completes_in_reasonable_time(self, live_client: Client) -> None:
        """get_unread_chats completes within 30 seconds (smoke test for asyncio.gather)."""
        import time

        start = time.monotonic()
        result = (
            await live_client.call_tool("get_unread_chats", {"message_limit": 5})
        ).data
        elapsed = time.monotonic() - start
        assert isinstance(result, list)
        assert elapsed < 30, (
            f"get_unread_chats took {elapsed:.1f}s — possible sequential bottleneck"
        )
