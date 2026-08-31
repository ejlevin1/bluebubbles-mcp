"""Unit tests for the BlueBubbles MCP server layer."""

from __future__ import annotations

import base64
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client

from bb_mcp.cursor import MAX_PAGE, Cursor

BASE_URL = "http://bb.local:1234"
API = f"{BASE_URL}/api/v1"
PASSWORD = "test-secret"
MY_ADDRESS = "+15550000000"

#: Tests seed cursors well in the past, so any "now" newer than the fixtures works.
NOW_MS = 4_000_000_000_000


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def ok_json(data: Any = None) -> httpx.Response:
    return httpx.Response(200, json={"status": 200, "data": data})


def error_text(result: Any) -> str:
    """What an errored tool call actually put in front of the model.

    `is_error` alone would stay green if the check under test disappeared and some
    unrelated failure took its place, which is most of what these tests are for.
    """
    return " ".join(getattr(block, "text", "") for block in result.content)


#: Sentinel: `helper_connected` may legitimately be False or 0, so "not passed"
#: cannot be expressed as a falsy default.
_UNSET: Any = object()


def server_info_ok(
    private_api: bool = True,
    address: str = MY_ADDRESS,
    helper_connected: Any = _UNSET,
) -> httpx.Response:
    """A `/server/info` payload.

    `helper_connected` defaults to mirroring `private_api` but is independently
    settable: the helper drops whenever Messages.app restarts, and hard-wiring the
    two together would hide the combination that decides the send path.
    """
    return ok_json(
        {
            "private_api": private_api,
            "helper_connected": (
                private_api if helper_connected is _UNSET else helper_connected
            ),
            "detected_imessage": address,
            "detected_icloud": address,
            "server_version": "1.9.0",
            "os_version": "14.0",
        }
    )


def last_body(route: respx.Route) -> dict[str, Any]:
    """The JSON body of a mocked route's most recent request.

    Sends are asserted on the wire: the client is built inside `lifespan` and is
    unreachable from an in-process `Client(mcp)`, so its flags cannot be read.
    """
    import json as _json

    return dict(_json.loads(route.calls.last.request.content))


def icloud_account_ok(
    active: str = MY_ADDRESS,
    aliases: list[str] | None = None,
    name: str | None = "Test User",
) -> httpx.Response:
    return ok_json(
        {
            "account_name": name,
            "active_alias": active,
            "apple_id": "test@example.com",
            "aliases": [
                {"Alias": a, "Status": 3, "IsUserVisible": True}
                for a in (aliases if aliases is not None else [active])
            ],
        }
    )


# ---------------------------------------------------------------------------
# Pure function tests — no server needed
# ---------------------------------------------------------------------------


class TestSlimMessage:
    def test_drops_extended_fields(self) -> None:
        from bb_mcp.server import _slim_message

        msg = {"guid": "m1", "text": "hi", "originalROWID": 99, "itemType": 0}
        result = _slim_message(msg)
        assert result["guid"] == "m1"
        assert "originalROWID" not in result
        assert "itemType" not in result

    def test_trims_handle_to_slim_fields(self) -> None:
        from bb_mcp.server import _slim_message

        msg = {
            "guid": "m1",
            "handle": {"address": "+1555", "service": "iMessage", "color": "blue"},
        }
        result = _slim_message(msg)
        assert result["handle"] == {"address": "+1555", "service": "iMessage"}

    def test_trims_chats_list(self) -> None:
        from bb_mcp.server import _slim_message

        msg = {
            "guid": "m1",
            "chats": [
                {
                    "guid": "c1",
                    "displayName": "Group",
                    "isArchived": False,
                    "extra": "drop",
                }
            ],
        }
        result = _slim_message(msg)
        assert result["chats"] == [
            {"guid": "c1", "displayName": "Group", "isArchived": False}
        ]

    def test_null_handle_left_alone(self) -> None:
        from bb_mcp.server import _slim_message

        result = _slim_message({"guid": "m1", "handle": None})
        assert result["handle"] is None

    def test_empty_message_stays_empty(self) -> None:
        from bb_mcp.server import _slim_message

        assert _slim_message({}) == {}


class TestProject:
    def test_extended_false_slims_each_message(self) -> None:
        from bb_mcp.server import _project

        data = [{"guid": "m1", "text": "hi", "originalROWID": 1}]
        result = _project(data, extended=False)
        assert "originalROWID" not in result[0]
        assert result[0]["guid"] == "m1"

    def test_extended_true_passes_through_unchanged(self) -> None:
        from bb_mcp.server import _project

        data = [{"guid": "m1", "originalROWID": 1}]
        result = _project(data, extended=True)
        assert result is data

    def test_empty_list(self) -> None:
        from bb_mcp.server import _project

        assert _project([], extended=False) == []
        assert _project([], extended=True) == []


class TestSendCapability:
    """One source of truth for the send path: server info plus the override."""

    def test_unset_means_auto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bb_mcp.server import _read_send_method

        monkeypatch.delenv("BLUEBUBBLES_SEND_METHOD", raising=False)
        assert _read_send_method() == "auto"

    @pytest.mark.parametrize("value", ["auto", "apple-script", "private-api"])
    def test_accepts_each_documented_value(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        from bb_mcp.server import _read_send_method

        monkeypatch.setenv("BLUEBUBBLES_SEND_METHOD", value)
        assert _read_send_method() == value

    def test_typo_fails_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`applescript` must not quietly mean `auto`."""
        from bb_mcp.server import _read_send_method

        monkeypatch.setenv("BLUEBUBBLES_SEND_METHOD", "applescript")
        with pytest.raises(RuntimeError, match="BLUEBUBBLES_SEND_METHOD"):
            _read_send_method()

    def test_enabled_server_with_helper(self) -> None:
        from bb_mcp.server import _send_capable

        info = {"private_api": True, "helper_connected": True}
        assert _send_capable(info, "auto") is True

    def test_disconnected_helper_falls_back(self) -> None:
        """A `private_api:true / helper_connected:false` server sends fine over
        AppleScript; pinning on the toggle would fail every send there."""
        from bb_mcp.server import _send_capable

        info = {"private_api": True, "helper_connected": False}
        assert _send_capable(info, "auto") is False

    def test_helper_connected_zero_is_disconnected(self) -> None:
        """Truthiness, not identity: a Node serializer emits 0, and `0 is not False`."""
        from bb_mcp.server import _send_capable

        info = {"private_api": True, "helper_connected": 0}
        assert _send_capable(info, "auto") is False

    def test_absent_helper_is_unknown_and_allows(self) -> None:
        from bb_mcp.server import _send_capable

        assert _send_capable({"private_api": True}, "auto") is True

    def test_toggle_off_never_uses_private_api(self) -> None:
        from bb_mcp.server import _send_capable

        info = {"private_api": False, "helper_connected": True}
        assert _send_capable(info, "auto") is False

    def test_override_wins_in_both_directions(self) -> None:
        from bb_mcp.server import _send_capable

        enabled = {"private_api": True, "helper_connected": True}
        disabled = {"private_api": False, "helper_connected": False}
        assert _send_capable(enabled, "apple-script") is False
        assert _send_capable(disabled, "private-api") is True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bb_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLUEBUBBLES_URL", BASE_URL)
    monkeypatch.setenv("BLUEBUBBLES_PASSWORD", PASSWORD)
    # `just` loads .env (`set dotenv-load := true`) and .env.example invites operators
    # to pin a send method there. Every connection below is built from this
    # environment, so an unset var is as much a part of the fixture as the other two:
    # otherwise the developer's own config decides what these tests assert.
    monkeypatch.delenv("BLUEBUBBLES_SEND_METHOD", raising=False)


@pytest.fixture
async def mcp_client(bb_env: None) -> AsyncGenerator[tuple[Client, respx.Router], None]:
    """In-process MCP client; lifespan mocked with private_api=True."""
    from bb_mcp.server import mcp

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{API}/server/info").mock(
            return_value=server_info_ok(private_api=True)
        )
        router.get(f"{API}/icloud/account").mock(return_value=icloud_account_ok())
        async with Client(mcp) as client:
            yield client, router


@asynccontextmanager
async def bb_server(
    private_api: bool = True, helper_connected: Any = _UNSET
) -> AsyncGenerator[tuple[Client, respx.Router], None]:
    """`mcp_client` with the `/server/info` capability fields under test control.

    Callers must have applied `bb_env` (and any `BLUEBUBBLES_SEND_METHOD`) first.
    """
    from bb_mcp.server import mcp

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{API}/server/info").mock(
            return_value=server_info_ok(
                private_api=private_api, helper_connected=helper_connected
            )
        )
        router.get(f"{API}/icloud/account").mock(return_value=icloud_account_ok())
        async with Client(mcp) as client:
            yield client, router


# ---------------------------------------------------------------------------
# Server health tools
# ---------------------------------------------------------------------------


class TestGetMyAddress:
    async def test_returns_detected_address(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, _ = mcp_client
        result = await c.call_tool("get_my_address", {})
        assert result.data["address"] == MY_ADDRESS
        assert result.data["addresses"] == [MY_ADDRESS]
        assert result.data["source"] == "icloud_account"

    async def test_raises_when_no_address(self, bb_env: None) -> None:
        from bb_mcp.server import mcp

        with respx.mock(assert_all_called=False) as router:
            router.get(f"{API}/server/info").mock(
                return_value=ok_json(
                    {
                        "private_api": True,
                        "detected_imessage": None,
                        "detected_icloud": None,
                    }
                )
            )
            async with Client(mcp) as c:
                result = await c.call_tool("get_my_address", {}, raise_on_error=False)
                assert result.is_error


class TestIdentityResolution:
    """`_resolve_identity` enriches from the Private API but must never depend on it."""

    @staticmethod
    async def _resolve(
        router: respx.Router, private_api: bool = True, override: str | None = None
    ) -> dict[str, Any]:
        from bb_mcp.client import BlueBubblesClient
        from bb_mcp.server import _resolve_identity

        info = server_info_ok(private_api=private_api).json()["data"]
        client = BlueBubblesClient(BASE_URL, PASSWORD)
        try:
            return await _resolve_identity(client, info, override)
        finally:
            await client.close()

    async def test_aliases_lead_with_active_alias(self) -> None:
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{API}/icloud/account").mock(
                return_value=icloud_account_ok(
                    active="+15551110000",
                    aliases=["me@example.com", "+15551110000"],
                )
            )
            identity = await self._resolve(router)
        # active_alias is the address Apple sends from, so it is the primary.
        assert identity["address"] == "+15551110000"
        assert identity["addresses"][0] == "+15551110000"
        assert "me@example.com" in identity["addresses"]
        assert identity["name"] == "Test User"
        assert identity["source"] == "icloud_account"

    async def test_detected_address_is_kept_alongside_aliases(self) -> None:
        """The /server/info address must survive even if Apple omits it."""
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{API}/icloud/account").mock(
                return_value=icloud_account_ok(active="+15551110000", aliases=[])
            )
            identity = await self._resolve(router)
        assert MY_ADDRESS in identity["addresses"]

    async def test_hidden_aliases_are_dropped(self) -> None:
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{API}/icloud/account").mock(
                return_value=ok_json(
                    {
                        "account_name": "Test User",
                        "active_alias": "+15551110000",
                        "aliases": [
                            {"Alias": "+15551110000", "IsUserVisible": True},
                            {"Alias": "hidden@icloud.com", "IsUserVisible": False},
                        ],
                    }
                )
            )
            identity = await self._resolve(router)
        assert "hidden@icloud.com" not in identity["addresses"]

    async def test_no_duplicates_across_sources(self) -> None:
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{API}/icloud/account").mock(
                return_value=icloud_account_ok(active=MY_ADDRESS)
            )
            identity = await self._resolve(router)
        assert identity["addresses"] == [MY_ADDRESS]

    async def test_private_api_off_skips_the_call_entirely(self) -> None:
        with respx.mock(assert_all_called=False) as router:
            route = router.get(f"{API}/icloud/account").mock(
                return_value=icloud_account_ok()
            )
            identity = await self._resolve(router, private_api=False)
        assert not route.called, "must not hit a Private API route when PA is off"
        assert identity["address"] == MY_ADDRESS
        assert identity["source"] == "server_info"

    async def test_falls_back_when_icloud_route_errors(self) -> None:
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{API}/icloud/account").mock(
                return_value=httpx.Response(500, json={"status": 500})
            )
            identity = await self._resolve(router)
        assert identity["address"] == MY_ADDRESS
        assert identity["source"] == "server_info"

    async def test_falls_back_when_icloud_route_times_out(self) -> None:
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{API}/icloud/account").mock(
                side_effect=httpx.ReadTimeout("helper wedged")
            )
            identity = await self._resolve(router)
        assert identity["address"] == MY_ADDRESS
        assert identity["source"] == "server_info"

    async def test_survives_a_malformed_payload(self) -> None:
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{API}/icloud/account").mock(
                return_value=ok_json({"aliases": "not-a-list", "active_alias": 42})
            )
            identity = await self._resolve(router)
        assert identity["address"] == MY_ADDRESS

    async def test_override_leads_without_evicting_aliases(self) -> None:
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{API}/icloud/account").mock(
                return_value=icloud_account_ok(
                    active="+15551110000", aliases=["me@example.com", "+15551110000"]
                )
            )
            identity = await self._resolve(router, override="me@example.com")
        assert identity["address"] == "me@example.com"
        assert identity["source"] == "override"
        assert "+15551110000" in identity["addresses"]


class TestGetServerInfo:
    async def test_returns_dict_with_version(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        router.get(f"{API}/server/info").mock(return_value=server_info_ok())
        result = await c.call_tool("get_server_info", {})
        assert isinstance(result.data, dict)
        assert "server_version" in result.data


class TestPing:
    async def test_returns_result(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        router.get(f"{API}/ping").mock(return_value=ok_json("pong"))
        result = await c.call_tool("ping", {})
        assert not result.is_error


# ---------------------------------------------------------------------------
# Chat tools
# ---------------------------------------------------------------------------


class TestListChats:
    async def test_returns_list(self, mcp_client: tuple[Client, respx.Router]) -> None:
        c, router = mcp_client
        router.post(f"{API}/chat/query").mock(
            return_value=ok_json([{"guid": "iMessage;-;+1555", "displayName": ""}])
        )
        result = await c.call_tool("list_chats", {"limit": 5})
        assert isinstance(result.data, list)
        assert result.data[0]["guid"] == "iMessage;-;+1555"


class TestGetChat:
    async def test_returns_chat_dict(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        router.get(f"{API}/chat/any;-;+1555").mock(
            return_value=ok_json({"guid": "iMessage;-;+1555", "participants": []})
        )
        result = await c.call_tool("get_chat", {"chat_guid": "iMessage;-;+1555"})
        assert isinstance(result.data, dict)
        assert not result.is_error


class TestGetChatMessages:
    async def test_returns_slim_messages(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        router.get(f"{API}/chat/g1/message").mock(
            return_value=ok_json(
                [{"guid": "m1", "text": "hi", "isFromMe": True, "originalROWID": 1}]
            )
        )
        result = await c.call_tool("get_chat_messages", {"chat_guid": "g1"})
        assert isinstance(result.data, list)
        assert "originalROWID" not in result.data[0]

    async def test_extended_true_passes_through(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        router.get(f"{API}/chat/g1/message").mock(
            return_value=ok_json([{"guid": "m1", "originalROWID": 1}])
        )
        result = await c.call_tool(
            "get_chat_messages", {"chat_guid": "g1", "extended": True}
        )
        assert result.data[0]["originalROWID"] == 1

    async def test_from_address_me_filters_on_is_from_me(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        # `handle` names the OTHER party even on outgoing messages, so matching it
        # against the owner's own address finds nothing. Verified against a live
        # server: 0 rows returned where 85 of 200 messages were in fact outgoing.
        c, router = mcp_client
        router.get(f"{API}/chat/g1/message").mock(
            return_value=ok_json(
                [
                    {
                        "guid": "mine",
                        "isFromMe": True,
                        "handle": {"address": "+15559999999", "service": "iMessage"},
                    },
                    {
                        "guid": "theirs",
                        "isFromMe": False,
                        "handle": {"address": "+15559999999", "service": "iMessage"},
                    },
                ]
            )
        )
        result = await c.call_tool(
            "get_chat_messages", {"chat_guid": "g1", "from_address": "me"}
        )
        assert [m["guid"] for m in result.data] == ["mine"]

    async def test_from_address_other_still_filters_by_handle(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        router.get(f"{API}/chat/g1/message").mock(
            return_value=ok_json(
                [
                    {"guid": "a", "handle": {"address": "+15551112222"}},
                    {"guid": "b", "handle": {"address": "+15559999999"}},
                ]
            )
        )
        result = await c.call_tool(
            "get_chat_messages", {"chat_guid": "g1", "from_address": "+15551112222"}
        )
        assert [m["guid"] for m in result.data] == ["a"]


class TestVersion:
    async def test_server_reports_its_own_version_not_fastmcps(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        # A client needs this to tell which build it is talking to: uvx caches by URL
        # and will happily serve a stale one from a branch that has moved on.
        from bb_mcp import __version__
        from bb_mcp.server import mcp

        assert mcp.version == __version__

    def test_version_matches_pyproject(self) -> None:
        """The declared version and the importable one must not drift."""
        import tomllib
        from pathlib import Path

        from bb_mcp import __version__

        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        declared = tomllib.loads(pyproject.read_text())["project"]["version"]
        assert __version__ == declared, (
            f"pyproject declares {declared} but the installed package is "
            f"{__version__}; run `uv sync` or bump one to match"
        )


class TestGetRecentMessages:
    """The incremental polling envelope."""

    def _routes(
        self, router: respx.Router, created: list[dict], changed: list[dict]
    ) -> None:
        """Route the two axes apart by inspecting the outgoing where-clause."""
        import json as _json

        def dispatch(request: httpx.Request) -> httpx.Response:
            body = _json.loads(request.content)
            statements = " ".join(w["statement"] for w in body.get("where", []))
            if "message.guid IN" in statements:
                return ok_json([])  # chat resolution
            if "date_edited" in statements:
                return ok_json(changed)
            return ok_json(created)

        router.post(f"{API}/message/query").mock(side_effect=dispatch)

    async def test_returns_an_envelope_not_a_bare_list(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        self._routes(router, [{"guid": "m1", "text": "hey", "dateCreated": 1000}], [])
        result = await c.call_tool("get_recent_messages", {"minutes": 60})
        assert set(result.data) >= {
            "messages",
            "changed",
            "reactions",
            "cursor",
            "has_more",
            "cursor_advanced",
            "counts",
            "notes",
        }
        assert [m["guid"] for m in result.data["messages"]] == ["m1"]

    async def test_messages_are_slim_by_default(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        self._routes(
            router, [{"guid": "m1", "dateCreated": 1000, "originalROWID": 5}], []
        )
        result = await c.call_tool("get_recent_messages", {"minutes": 60})
        assert "originalROWID" not in result.data["messages"][0]

    async def test_extended_passes_raw_rows_through(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        self._routes(
            router, [{"guid": "m1", "dateCreated": 1000, "originalROWID": 5}], []
        )
        result = await c.call_tool(
            "get_recent_messages", {"minutes": 60, "extended": True}
        )
        assert result.data["messages"][0]["originalROWID"] == 5

    async def test_cursor_round_trips_through_the_tool(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        self._routes(router, [{"guid": "m1", "dateCreated": 1_700_000_000_000}], [])
        seed = Cursor.seed(1).encode()
        first = (await c.call_tool("get_recent_messages", {"since": seed})).data
        second = await c.call_tool(
            "get_recent_messages", {"since": first["cursor"]}, raise_on_error=False
        )
        assert not second.is_error
        assert (
            Cursor.parse(first["cursor"], now_ms=NOW_MS).created_ms == 1_700_000_000_000
        )

    async def test_since_wins_over_minutes_and_says_so(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        self._routes(router, [], [])
        token = Cursor.seed(1_700_000_000_000).encode()
        result = await c.call_tool(
            "get_recent_messages", {"since": token, "minutes": 5}
        )
        assert any("minutes" in n for n in result.data["notes"])

    async def test_malformed_since_errors_instead_of_scanning_everything(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        self._routes(router, [], [])
        result = await c.call_tool(
            "get_recent_messages", {"since": "made-up"}, raise_on_error=False
        )
        assert result.is_error

    async def test_from_address_me_uses_is_from_me_clause(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        import json as _json

        c, router = mcp_client
        self._routes(router, [], [])
        await c.call_tool("get_recent_messages", {"minutes": 60, "from_address": "me"})
        statements = [
            w["statement"]
            for call in router.calls
            if call.request.method == "POST"
            for w in _json.loads(call.request.content).get("where", [])
        ]
        assert any("message.is_from_me" in s for s in statements)
        assert not any("handle.id" in s for s in statements)

    async def test_old_edits_do_not_hide_a_new_message(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        """The reason there are two watermarks rather than one.

        Ten years-old messages edited moments ago sort near the FRONT by
        message.date. Under a single shared cursor the frontier trim discards the one
        genuinely new message and the cursor lands behind where the poll started, so
        the next poll returns the identical batch and the new message is unreachable
        forever.
        """
        c, router = mcp_client
        old_edited = [
            {
                "guid": f"old{i}",
                "dateCreated": 1_000 + i,
                "dateEdited": 1_700_000_000_000,
            }
            for i in range(10)
        ]
        brand_new = [{"guid": "new", "dateCreated": 1_700_000_000_500}]
        self._routes(router, brand_new, old_edited)

        seed = Cursor.seed(999).encode()
        result = (
            await c.call_tool("get_recent_messages", {"since": seed, "limit": 10})
        ).data

        assert "new" in [m["guid"] for m in result["messages"]]
        assert result["cursor_advanced"]
        assert (
            Cursor.parse(result["cursor"], now_ms=NOW_MS).created_ms
            == 1_700_000_000_500
        )
        assert len(result["changed"]) == 10

    async def test_shared_millisecond_truncation_skips_nothing(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        rows = [
            {"guid": "a", "dateCreated": 100},
            {"guid": "b", "dateCreated": 200},
            {"guid": "c", "dateCreated": 200},
        ]
        self._routes(router, rows, [])
        seed = Cursor.seed(1).encode()
        result = (
            await c.call_tool("get_recent_messages", {"since": seed, "limit": 2})
        ).data
        # The 200-block is held back rather than half-delivered and then excluded.
        assert [m["guid"] for m in result["messages"]] == ["a"]
        assert result["has_more"]
        assert Cursor.parse(result["cursor"], now_ms=NOW_MS).created_ms == 100

    async def test_batch_inside_one_millisecond_escalates_and_advances(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        # A truncated page entirely inside one millisecond cannot advance without
        # either skipping rows or re-fetching. Retrying at the largest page the server
        # allows resolves it: we then hold the whole millisecond, so excluding it is
        # safe.
        c, router = mcp_client
        rows = [{"guid": g, "dateCreated": 500} for g in ("a", "b", "c")]
        self._routes(router, rows, [])
        seed = Cursor.seed(1).encode()
        result = (
            await c.call_tool("get_recent_messages", {"since": seed, "limit": 2})
        ).data
        assert len(result["messages"]) == 3, "the batch must not come back empty"
        assert result["stalled_ms"] is None
        assert Cursor.parse(result["cursor"], now_ms=NOW_MS).created_ms == 500

    async def test_unresolvable_millisecond_stall_is_reported_not_hidden(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        # More rows share one millisecond than even the maximum page can hold. The
        # cursor steps back so nothing is lost, and the stall is stated outright
        # rather than returned as a silently non-advancing cursor.
        c, router = mcp_client
        rows = [{"guid": f"m{i}", "dateCreated": 500} for i in range(MAX_PAGE + 1)]
        self._routes(router, rows, [])
        seed = Cursor.seed(1).encode()
        result = (
            await c.call_tool("get_recent_messages", {"since": seed, "limit": 5})
        ).data
        assert result["stalled_ms"] == 500
        assert result["has_more"]
        assert any("share millisecond" in n for n in result["notes"])
        assert Cursor.parse(result["cursor"], now_ms=NOW_MS).created_ms == 499

    async def test_reactions_surface_with_stripped_targets(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        self._routes(
            router,
            [
                {
                    "guid": "r1",
                    "dateCreated": 100,
                    "associatedMessageGuid": "p:3/TARGET",
                    "associatedMessageType": "love",
                }
            ],
            [],
        )
        result = (await c.call_tool("get_recent_messages", {"minutes": 60})).data
        assert result["reactions"][0]["target_guid"] == "TARGET"
        assert result["reactions"][0]["type"] == "love"

    async def test_unattributed_rows_get_an_empty_chat_list(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        # The INNER join drops 2FA senders and shortcodes; they must arrive with an
        # explicit empty bucket rather than vanishing from the results.
        c, router = mcp_client
        self._routes(router, [{"guid": "m1", "dateCreated": 100}], [])
        result = (await c.call_tool("get_recent_messages", {"minutes": 60})).data
        assert result["messages"][0]["chats"] == []
        assert result["counts"]["no_chat"] == 1

    async def test_chat_resolution_failure_still_returns_a_cursor(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        import json as _json

        c, router = mcp_client

        def dispatch(request: httpx.Request) -> httpx.Response:
            body = _json.loads(request.content)
            statements = " ".join(w["statement"] for w in body.get("where", []))
            if "message.guid IN" in statements:
                return httpx.Response(500, json={"status": 500, "message": "boom"})
            if "date_edited" in statements:
                return ok_json([])
            return ok_json([{"guid": "m1", "dateCreated": 100}])

        router.post(f"{API}/message/query").mock(side_effect=dispatch)
        result = (await c.call_tool("get_recent_messages", {"minutes": 60})).data
        # Losing the cursor would make the caller replay the same window forever.
        assert result["cursor"]
        assert result["messages"][0]["guid"] == "m1"
        assert any("attribution" in n for n in result["notes"])


class TestGetRecentMessagesScoped:
    """`chat_guid` scoping: the wire, the cursor scope rule, and the GUID check."""

    CHAT = "any;-;+15551230000"
    OTHER = "any;-;+15559990000"

    def _routes(
        self,
        router: respx.Router,
        created: list[dict] | None = None,
        changed: list[dict] | None = None,
        *,
        chat_exists: bool = True,
    ) -> None:
        import json as _json

        def dispatch(request: httpx.Request) -> httpx.Response:
            body = _json.loads(request.content)
            statements = " ".join(w["statement"] for w in body.get("where", []))
            if "message.guid IN" in statements:
                return ok_json([])  # chat resolution
            if "date_edited" in statements:
                return ok_json(changed or [])
            return ok_json(created or [])

        router.post(f"{API}/message/query").mock(side_effect=dispatch)
        for guid in (self.CHAT, self.OTHER):
            router.get(f"{API}/chat/{guid}").mock(
                return_value=ok_json(
                    {"guid": guid, "displayName": "Family", "participants": ["x"]}
                )
                if chat_exists
                else httpx.Response(404, json={"status": 404, "message": "not found"})
            )

    @staticmethod
    def _query_bodies(router: respx.Router) -> list[dict[str, Any]]:
        import json as _json

        return [
            _json.loads(call.request.content)
            for call in router.calls
            if call.request.method == "POST"
        ]

    async def test_scope_reaches_both_axes_on_the_wire(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        self._routes(router)
        await c.call_tool(
            "get_recent_messages",
            {"minutes": 60, "chat_guid": "iMessage;-;+15551230000"},
        )
        bodies = self._query_bodies(router)
        assert len(bodies) == 2
        assert [b.get("chatGuid") for b in bodies] == [self.CHAT, self.CHAT]
        statements = [" ".join(w["statement"] for w in b["where"]) for b in bodies]
        assert any("date_edited" in s for s in statements)
        assert any("date_edited" not in s for s in statements)

    async def test_global_poll_sends_no_chat_guid(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        self._routes(router)
        await c.call_tool("get_recent_messages", {"minutes": 60})
        assert all("chatGuid" not in b for b in self._query_bodies(router))

    async def test_scoped_cursor_is_refused_on_a_global_poll(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        self._routes(router)
        scoped = (
            await c.call_tool(
                "get_recent_messages", {"minutes": 60, "chat_guid": self.CHAT}
            )
        ).data["cursor"]
        assert scoped.startswith("v2|")
        result = await c.call_tool(
            "get_recent_messages", {"since": scoped}, raise_on_error=False
        )
        assert result.is_error
        assert "one cursor per scope" in error_text(result)

    async def test_global_cursor_is_refused_on_a_scoped_poll(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        self._routes(router)
        unscoped = (await c.call_tool("get_recent_messages", {"minutes": 60})).data[
            "cursor"
        ]
        assert unscoped.startswith("v1|")
        result = await c.call_tool(
            "get_recent_messages",
            {"since": unscoped, "chat_guid": self.CHAT},
            raise_on_error=False,
        )
        assert result.is_error
        assert "one cursor per scope" in error_text(result)

    async def test_another_chats_cursor_is_refused(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        self._routes(router)
        mine = (
            await c.call_tool(
                "get_recent_messages", {"minutes": 60, "chat_guid": self.CHAT}
            )
        ).data["cursor"]
        result = await c.call_tool(
            "get_recent_messages",
            {"since": mine, "chat_guid": self.OTHER},
            raise_on_error=False,
        )
        assert result.is_error
        message = error_text(result)
        assert "one cursor per scope" in message
        # Naming both scopes is the whole point: "some cursor is wrong" is not
        # actionable when an agent is holding one per chat.
        assert self.CHAT in message and self.OTHER in message

    @pytest.mark.parametrize(
        "args",
        [
            {"minutes": 60},
            {"since": "1700000000000"},
            {"since": "1700000000"},
            {"since": "2023-11-14T22:13:20Z"},
            {"since": "2023-11-14"},
        ],
    )
    async def test_seeds_are_scope_neutral(
        self, mcp_client: tuple[Client, respx.Router], args: dict[str, Any]
    ) -> None:
        """A seed adopts the requested scope; raising here breaks every first poll."""
        c, router = mcp_client
        self._routes(router)
        result = await c.call_tool(
            "get_recent_messages",
            {**args, "chat_guid": self.CHAT},
            raise_on_error=False,
        )
        assert not result.is_error
        assert result.data["cursor"].startswith("v2|")

    async def test_service_prefix_variants_are_one_scope(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        self._routes(router)
        minted = (
            await c.call_tool(
                "get_recent_messages",
                {"minutes": 60, "chat_guid": "iMessage;-;+15551230000"},
            )
        ).data["cursor"]
        replayed = await c.call_tool(
            "get_recent_messages",
            {"since": minted, "chat_guid": "any;-;+15551230000"},
            raise_on_error=False,
        )
        assert not replayed.is_error
        assert self.CHAT in minted

    async def test_unknown_chat_guid_is_rejected_before_it_polls_empty_forever(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        self._routes(router, chat_exists=False)
        result = await c.call_tool(
            "get_recent_messages",
            {"minutes": 60, "chat_guid": self.CHAT},
            raise_on_error=False,
        )
        assert result.is_error
        assert "list_chats" in error_text(result)
        # An empty result would look healthy forever, so nothing is even queried.
        assert self._query_bodies(router) == []

    async def test_scoped_rows_skip_the_chat_resolution_pass(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        self._routes(router, [{"guid": "m1", "dateCreated": 100}], [])
        result = (
            await c.call_tool(
                "get_recent_messages", {"minutes": 60, "chat_guid": self.CHAT}
            )
        ).data
        # The label carries the same fields a global poll's does — losing
        # `displayName` would leave a caller unable to name the chat it just polled —
        # and only those: `participants` is dropped exactly as `_slim_message` would.
        assert result["messages"][0]["chats"] == [
            {"guid": self.CHAT, "displayName": "Family"}
        ]
        # `no_chat` is 0 by construction when scoped: a chat-less message cannot
        # match a chat GUID filter.
        assert result["counts"]["no_chat"] == 0
        statements = [
            " ".join(w["statement"] for w in b["where"])
            for b in self._query_bodies(router)
        ]
        assert not any("message.guid IN" in s for s in statements)

    async def test_the_scope_check_happens_once_per_session(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        """A resumed scoped cursor was minted by a call that already checked."""
        c, router = mcp_client
        self._routes(router)
        first = (
            await c.call_tool(
                "get_recent_messages", {"minutes": 60, "chat_guid": self.CHAT}
            )
        ).data["cursor"]
        await c.call_tool(
            "get_recent_messages", {"since": first, "chat_guid": self.CHAT}
        )
        chat_gets = [
            call
            for call in router.calls
            if call.request.method == "GET" and "/chat/" in str(call.request.url)
        ]
        assert len(chat_gets) == 1

    async def test_a_resumed_scoped_poll_still_labels_its_rows(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        """The cursor outlives the chat lookup, so a resumed poll repeats it.

        Only when there is something to label, though: an empty poll — what a loop
        does most of the time — still costs nothing beyond the two queries.
        """
        c, router = mcp_client
        created: list[dict] = []
        self._routes(router, created, [])
        first = (
            await c.call_tool(
                "get_recent_messages", {"minutes": 60, "chat_guid": self.CHAT}
            )
        ).data["cursor"]
        created.append({"guid": "m1", "dateCreated": 100})
        result = (
            await c.call_tool(
                "get_recent_messages", {"since": first, "chat_guid": self.CHAT}
            )
        ).data
        assert result["messages"][0]["chats"] == [
            {"guid": self.CHAT, "displayName": "Family"}
        ]
        chat_gets = [
            call
            for call in router.calls
            if call.request.method == "GET" and "/chat/" in str(call.request.url)
        ]
        # One for the empty first poll's scope check, one for the second poll's label.
        assert len(chat_gets) == 2


class TestScopeExistenceCheck:
    """Only a missing chat may be reported as a missing chat.

    The check runs on the first poll of a scope, which is exactly when the Mac has
    just woken or BlueBubbles has just restarted. Answering a connection failure with
    "get the chat GUID from list_chats" sends the model off to re-resolve a GUID that
    was right all along — the misdirection this tool's advice exists to remove.
    """

    CHAT = "any;-;+15551230000"

    async def _check(self, **mock: Any) -> dict[str, Any]:
        from bb_mcp.client import BlueBubblesClient
        from bb_mcp.server import _require_scope_chat

        with respx.mock() as router:
            router.get(f"{API}/chat/{self.CHAT}").mock(**mock)
            client = BlueBubblesClient(BASE_URL, PASSWORD)
            try:
                return await _require_scope_chat(client, self.CHAT)
            finally:
                await client.close()

    async def test_unknown_chat_is_reported_as_a_bad_guid(self) -> None:
        with pytest.raises(ValueError, match="list_chats"):
            await self._check(
                return_value=httpx.Response(
                    404, json={"status": 404, "message": "Chat does not exist!"}
                )
            )

    async def test_transport_failure_is_not_relabelled_as_a_bad_guid(self) -> None:
        from bb_mcp.client import BlueBubblesError

        with pytest.raises(httpx.ConnectError) as excinfo:
            await self._check(side_effect=httpx.ConnectError("connection refused"))
        assert not isinstance(excinfo.value, (ValueError, BlueBubblesError))

    async def test_gateway_timeout_is_not_relabelled_as_a_bad_guid(self) -> None:
        from bb_mcp.client import BlueBubblesError

        # A sleeping Mac behind a proxy: the GUID is fine, the server is not.
        with pytest.raises(BlueBubblesError) as excinfo:
            await self._check(return_value=httpx.Response(504, text="gateway timeout"))
        assert "list_chats" not in str(excinfo.value)
        assert excinfo.value.status_code == 504

    async def test_returns_the_chat_it_read(self) -> None:
        chat = await self._check(
            return_value=ok_json(
                {"guid": self.CHAT, "displayName": "Family", "participants": ["x"]}
            )
        )
        assert chat == {"guid": self.CHAT, "displayName": "Family"}


class TestGetUnreadChats:
    async def test_returns_list_with_expected_shape(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        router.post(f"{API}/chat/query").mock(
            return_value=ok_json(
                [
                    {"guid": "g1", "hasUnreadMessages": True},
                    {"guid": "g2", "hasUnreadMessages": False},
                ]
            )
        )
        router.get(f"{API}/chat/g1/message").mock(return_value=ok_json([]))
        result = await c.call_tool("get_unread_chats", {})
        assert isinstance(result.data, list)
        assert len(result.data) == 1
        item = result.data[0]
        assert "chat" in item
        assert "recent_messages" in item
        assert item["chat"]["guid"] == "g1"

    async def test_message_limit_respected(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        router.post(f"{API}/chat/query").mock(
            return_value=ok_json([{"guid": "g1", "hasUnreadMessages": True}])
        )
        msgs = [{"guid": f"m{i}"} for i in range(10)]
        router.get(f"{API}/chat/g1/message").mock(return_value=ok_json(msgs[:2]))
        result = await c.call_tool("get_unread_chats", {"message_limit": 2})
        assert len(result.data[0]["recent_messages"]) <= 2


class TestMarkChatRead:
    async def test_returns_confirmation(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        router.post(f"{API}/chat/g1/read").mock(return_value=ok_json(None))
        result = await c.call_tool("mark_chat_read", {"chat_guid": "g1"})
        assert "read" in result.data.lower()


class TestMarkChatUnread:
    async def test_returns_confirmation(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        router.post(f"{API}/chat/g1/unread").mock(return_value=ok_json(None))
        result = await c.call_tool("mark_chat_unread", {"chat_guid": "g1"})
        assert "unread" in result.data.lower()


class TestDeleteChat:
    async def test_returns_confirmation(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        router.delete(f"{API}/chat/g1").mock(return_value=ok_json(None))
        result = await c.call_tool("delete_chat", {"chat_guid": "g1"})
        assert "deleted" in result.data.lower()


# ---------------------------------------------------------------------------
# Message tools
# ---------------------------------------------------------------------------


class TestSearchMessages:
    async def test_returns_slim_list(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        router.post(f"{API}/message/query").mock(
            return_value=ok_json([{"guid": "m1", "text": "hello", "originalROWID": 3}])
        )
        result = await c.call_tool("search_messages", {"query": "hello"})
        assert isinstance(result.data, list)
        assert "originalROWID" not in result.data[0]

    async def test_from_address_me_resolves(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        router.post(f"{API}/message/query").mock(return_value=ok_json([]))
        result = await c.call_tool("search_messages", {"from_address": "me"})
        assert isinstance(result.data, list)

    async def test_extended_passes_through(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        router.post(f"{API}/message/query").mock(
            return_value=ok_json([{"guid": "m1", "originalROWID": 9}])
        )
        result = await c.call_tool("search_messages", {"extended": True})
        assert result.data[0]["originalROWID"] == 9


class TestGetMessage:
    async def test_slim_by_default(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        router.get(f"{API}/message/m1").mock(
            return_value=ok_json({"guid": "m1", "text": "hi", "originalROWID": 7})
        )
        result = await c.call_tool("get_message", {"message_guid": "m1"})
        assert result.data["guid"] == "m1"
        assert "originalROWID" not in result.data

    async def test_extended_true_passes_through(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        router.get(f"{API}/message/m1").mock(
            return_value=ok_json({"guid": "m1", "originalROWID": 7})
        )
        result = await c.call_tool(
            "get_message", {"message_guid": "m1", "extended": True}
        )
        assert result.data["originalROWID"] == 7


class TestSendMessage:
    async def test_returns_sent_message(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        route = router.post(f"{API}/message/text").mock(
            return_value=ok_json({"guid": "m1"})
        )
        result = await c.call_tool(
            "send_message", {"chat_guid": "g1", "message": "Hello"}
        )
        assert result.data["guid"] == "m1"
        assert last_body(route)["method"] == "private-api"

    async def test_reply_to_guid_reaches_the_wire(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        """The field the AppleScript path cannot express, and never used to send."""
        c, router = mcp_client
        route = router.post(f"{API}/message/text").mock(
            return_value=ok_json({"guid": "m1"})
        )
        await c.call_tool(
            "send_message",
            {"chat_guid": "g1", "message": "Hello", "reply_to_guid": "parent"},
        )
        assert last_body(route)["selectedMessageGuid"] == "parent"


class TestSendMessageToAddress:
    async def test_routes_through_message_text(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        route = router.post(f"{API}/message/text").mock(
            return_value=ok_json({"guid": "m1"})
        )
        result = await c.call_tool(
            "send_message_to_address",
            {"address": "+15551234567", "message": "hi"},
        )
        assert result.data["guid"] == "m1"
        assert route.called
        assert last_body(route)["method"] == "private-api"

    async def test_default_service_never_touches_chat_new(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        """`/chat/new` returns a chat, not a message, and would force iMessage on
        SMS-only recipients — so only an explicit SMS request may take it."""
        c, router = mcp_client
        text = router.post(f"{API}/message/text").mock(
            return_value=ok_json({"guid": "m1"})
        )
        new_chat = router.post(f"{API}/chat/new").mock(return_value=ok_json({}))
        await c.call_tool(
            "send_message_to_address",
            {"address": "+15551234567", "message": "hi"},
        )
        assert text.called
        assert not new_chat.called
        assert last_body(text)["chatGuid"] == "any;-;+15551234567"

    async def test_service_is_an_enum_in_the_tool_schema(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        """A bare `str` let "SMS/MMS" or "sms text" through, and everything
        unrecognized was coerced to iMessage — the wrong transport, silently."""
        c, _ = mcp_client
        tool = next(
            t for t in await c.list_tools() if t.name == "send_message_to_address"
        )
        assert tool.inputSchema["properties"]["service"]["enum"] == ["iMessage", "SMS"]

    async def test_a_service_outside_the_enum_never_reaches_the_wire(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        text = router.post(f"{API}/message/text").mock(
            return_value=ok_json({"guid": "m1"})
        )
        new_chat = router.post(f"{API}/chat/new").mock(return_value=ok_json({}))
        for service in ("SMS/MMS", "sms text", "sms"):
            result = await c.call_tool(
                "send_message_to_address",
                {"address": "+15551234567", "message": "hi", "service": service},
                raise_on_error=False,
            )
            assert result.is_error, service
        assert not text.called
        assert not new_chat.called

    async def test_sms_reaches_chat_new(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        """SMS is the one service the tool forwards; it takes the `/chat/new` route,
        which is the only one that can pin the service."""
        c, router = mcp_client
        text = router.post(f"{API}/message/text").mock(
            return_value=ok_json({"guid": "m1"})
        )
        new_chat = router.post(f"{API}/chat/new").mock(
            return_value=ok_json({"guid": "any;-;+15551234567", "text": None})
        )
        result = await c.call_tool(
            "send_message_to_address",
            {"address": "+15551234567", "message": "hi", "service": "SMS"},
        )
        assert new_chat.called
        assert not text.called
        # The SMS branch returns a chat, which the docstring promises explicitly.
        assert result.data["guid"] == "any;-;+15551234567"
        body = last_body(new_chat)
        assert body["service"] == "SMS"
        assert body["method"] == "private-api"


class TestSendMethod:
    """The effective send method, asserted on the wire.

    None of these cases mutate the tool set — every one reports `private_api: true`
    — so they belong above `TestPrivateApiDisabled` rather than beside it.
    """

    @staticmethod
    async def _method(c: Client, router: respx.Router) -> str:
        route = router.post(f"{API}/message/text").mock(
            return_value=ok_json({"guid": "m1"})
        )
        await c.call_tool("send_message", {"chat_guid": "g1", "message": "hi"})
        return str(last_body(route)["method"])

    async def test_auto_follows_an_enabled_server(self, bb_env: None) -> None:
        async with bb_server(private_api=True) as (c, router):
            assert await self._method(c, router) == "private-api"

    async def test_auto_falls_back_when_the_helper_dropped(self, bb_env: None) -> None:
        async with bb_server(private_api=True, helper_connected=False) as (c, router):
            assert await self._method(c, router) == "apple-script"

    async def test_auto_falls_back_on_integer_zero(self, bb_env: None) -> None:
        async with bb_server(private_api=True, helper_connected=0) as (c, router):
            assert await self._method(c, router) == "apple-script"

    async def test_apple_script_override(
        self, bb_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BLUEBUBBLES_SEND_METHOD", "apple-script")
        async with bb_server(private_api=True) as (c, router):
            assert await self._method(c, router) == "apple-script"

    async def test_private_api_override(
        self, bb_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BLUEBUBBLES_SEND_METHOD", "private-api")
        async with bb_server(private_api=True, helper_connected=False) as (c, router):
            assert await self._method(c, router) == "private-api"

    async def test_invalid_value_fails_startup(
        self, bb_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BLUEBUBBLES_SEND_METHOD", "applescript")
        with pytest.raises(RuntimeError, match="BLUEBUBBLES_SEND_METHOD"):
            async with bb_server(private_api=True):
                pass

    async def test_apple_script_override_does_not_drop_reply_to_guid(
        self, bb_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The failure mode the shared capability exists to prevent.

        Forcing AppleScript on a Private-API server used to leave the guard reading
        the raw toggle while the client dropped `selectedMessageGuid` — the reply
        would send, unthreaded, with nothing to show for it. It must refuse instead.
        """
        monkeypatch.setenv("BLUEBUBBLES_SEND_METHOD", "apple-script")
        async with bb_server(private_api=True) as (c, router):
            route = router.post(f"{API}/message/text").mock(
                return_value=ok_json({"guid": "m1"})
            )
            result = await c.call_tool(
                "send_message",
                {"chat_guid": "g1", "message": "hi", "reply_to_guid": "parent"},
                raise_on_error=False,
            )
            assert result.is_error
            assert not route.called

    async def test_apple_script_override_refuses_sms(
        self, bb_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BLUEBUBBLES_SEND_METHOD", "apple-script")
        async with bb_server(private_api=True) as (c, router):
            new_chat = router.post(f"{API}/chat/new").mock(return_value=ok_json({}))
            result = await c.call_tool(
                "send_message_to_address",
                {"address": "+15551234567", "message": "hi", "service": "SMS"},
                raise_on_error=False,
            )
            assert result.is_error
            assert not new_chat.called


# ---------------------------------------------------------------------------
# Contact tools
# ---------------------------------------------------------------------------


class TestGetContacts:
    async def test_no_query_returns_all(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        contacts = [
            {"displayName": "Alice", "phoneNumbers": [], "emails": []},
            {"displayName": "Bob", "phoneNumbers": [], "emails": []},
        ]
        router.get(f"{API}/contact").mock(return_value=ok_json(contacts))
        result = await c.call_tool("get_contacts", {})
        assert len(result.data) == 2

    async def test_query_filters_by_display_name(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        contacts = [
            {"displayName": "Alice", "phoneNumbers": [], "emails": []},
            {"displayName": "Bob", "phoneNumbers": [], "emails": []},
        ]
        router.get(f"{API}/contact").mock(return_value=ok_json(contacts))
        result = await c.call_tool("get_contacts", {"query": "alice"})
        assert len(result.data) == 1
        assert result.data[0]["displayName"] == "Alice"

    async def test_query_filters_by_phone_number(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        contacts = [
            {
                "displayName": "Alice",
                "phoneNumbers": [{"address": "+15551112222"}],
                "emails": [],
            },
            {
                "displayName": "Bob",
                "phoneNumbers": [{"address": "+15559999999"}],
                "emails": [],
            },
        ]
        router.get(f"{API}/contact").mock(return_value=ok_json(contacts))
        result = await c.call_tool("get_contacts", {"query": "1112222"})
        assert len(result.data) == 1
        assert result.data[0]["displayName"] == "Alice"

    async def test_query_filters_by_email(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        contacts = [
            {
                "displayName": "Alice",
                "phoneNumbers": [],
                "emails": [{"address": "alice@example.com"}],
            },
            {"displayName": "Bob", "phoneNumbers": [], "emails": []},
        ]
        router.get(f"{API}/contact").mock(return_value=ok_json(contacts))
        result = await c.call_tool("get_contacts", {"query": "alice@"})
        assert len(result.data) == 1

    async def test_nonsense_query_returns_empty(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        router.get(f"{API}/contact").mock(
            return_value=ok_json(
                [{"displayName": "Alice", "phoneNumbers": [], "emails": []}]
            )
        )
        result = await c.call_tool("get_contacts", {"query": "zzz_no_match"})
        assert result.data == []


# ---------------------------------------------------------------------------
# Attachment tools
# ---------------------------------------------------------------------------


class TestDownloadAttachment:
    async def test_image_returned_as_image_content(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        router.get(f"{API}/attachment/att1").mock(
            return_value=ok_json(
                {"guid": "att1", "mimeType": "image/png", "transferName": "photo.png"}
            )
        )
        router.get(f"{API}/attachment/att1/download").mock(
            return_value=httpx.Response(200, content=b"\x89PNG\r\nfake")
        )
        result = await c.call_tool("download_attachment", {"attachment_guid": "att1"})
        assert not result.is_error
        assert result.content

    async def test_non_image_returned_as_base64_dict(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        raw = b"pdf bytes here"
        router.get(f"{API}/attachment/att1").mock(
            return_value=ok_json(
                {
                    "guid": "att1",
                    "mimeType": "application/pdf",
                    "transferName": "doc.pdf",
                }
            )
        )
        router.get(f"{API}/attachment/att1/download").mock(
            return_value=httpx.Response(200, content=raw)
        )
        result = await c.call_tool("download_attachment", {"attachment_guid": "att1"})
        assert not result.is_error
        assert result.data["mime_type"] == "application/pdf"
        assert result.data["filename"] == "doc.pdf"
        assert result.data["size_bytes"] == len(raw)
        assert result.data["data_base64"] == base64.b64encode(raw).decode()

    async def test_unknown_mime_type_treated_as_binary(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        router.get(f"{API}/attachment/att1").mock(
            return_value=ok_json(
                {"guid": "att1", "mimeType": None, "transferName": "file.bin"}
            )
        )
        router.get(f"{API}/attachment/att1/download").mock(
            return_value=httpx.Response(200, content=b"\x00\x01\x02")
        )
        result = await c.call_tool("download_attachment", {"attachment_guid": "att1"})
        assert not result.is_error
        assert result.data["mime_type"] == "application/octet-stream"


# ---------------------------------------------------------------------------
# Scheduled message tools
# ---------------------------------------------------------------------------


class TestScheduledMessageDocs:
    """The consequence of scheduling has to be in the tool docs, not only the skill.

    A scheduled send freezes its method and fails hard, at a time nobody is watching,
    if the Private API helper has dropped by then — and the listing tool is the only
    thing that ever shows it. "Queued messages waiting to be sent" described a record
    that may in fact be a send that already failed.
    """

    async def test_listing_documents_status_and_failure(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, _ = mcp_client
        tools = {t.name: t.description or "" for t in await c.list_tools()}
        listing = tools["list_scheduled_messages"]
        assert "status" in listing
        assert "'error'" in listing and "FAILED" in listing

    async def test_scheduling_documents_the_frozen_method(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, _ = mcp_client
        tools = {t.name: t.description or "" for t in await c.list_tools()}
        scheduling = tools["schedule_message"]
        assert "frozen" in scheduling
        # Scheduling successfully is not delivery, and the docstring must say where
        # the failure turns up rather than leaving the model to assume it went out.
        assert "list_scheduled_messages" in scheduling


class TestListScheduledMessages:
    async def test_returns_list(self, mcp_client: tuple[Client, respx.Router]) -> None:
        c, router = mcp_client
        router.get(f"{API}/message/schedule").mock(
            return_value=ok_json([{"id": 1, "message": "later"}])
        )
        result = await c.call_tool("list_scheduled_messages", {})
        assert result.data == [{"id": 1, "message": "later"}]


class TestScheduleMessage:
    async def test_returns_scheduled_message(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        router.post(f"{API}/message/schedule").mock(return_value=ok_json({"id": 5}))
        result = await c.call_tool(
            "schedule_message",
            {"chat_guid": "g1", "message": "later", "scheduled_for": 9999999999},
        )
        assert result.data["id"] == 5


class TestDeleteScheduledMessage:
    async def test_returns_confirmation(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        router.delete(f"{API}/message/schedule/42").mock(return_value=ok_json(None))
        result = await c.call_tool("delete_scheduled_message", {"schedule_id": 42})
        assert "deleted" in result.data.lower()


# ---------------------------------------------------------------------------
# Group chat tools
# ---------------------------------------------------------------------------


class TestRenameGroup:
    async def test_calls_api(self, mcp_client: tuple[Client, respx.Router]) -> None:
        c, router = mcp_client
        router.put(f"{API}/chat/g1").mock(return_value=ok_json(None))
        result = await c.call_tool(
            "rename_group", {"chat_guid": "g1", "name": "New Name"}
        )
        assert not result.is_error


class TestLeaveChat:
    async def test_returns_confirmation(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        router.post(f"{API}/chat/g1/leave").mock(return_value=ok_json(None))
        result = await c.call_tool("leave_chat", {"chat_guid": "g1"})
        assert "left" in result.data.lower()


# ---------------------------------------------------------------------------
# Private API guard tests
#
# NOTE: These tests modify the module-level `mcp` instance (tool removal in
# lifespan). Place this class last in the file so it runs after all tests
# that depend on the full tool set.
# ---------------------------------------------------------------------------


class TestReadReceiptsNeedPrivateApi:
    """Read receipts go out over the Private API.

    Without it the server answers `POST /chat/:guid/read` with a 500 reading
    "iMessage Private API is not enabled!" — on every chat, not just some. Offering
    the tool anyway hands an agent a call that cannot possibly succeed.
    """

    def test_listed_as_private_api_tools(self) -> None:
        from bb_mcp.server import PRIVATE_API_TOOLS

        assert "mark_chat_read" in PRIVATE_API_TOOLS
        assert "mark_chat_unread" in PRIVATE_API_TOOLS


class TestPrivateApiDisabled:
    @pytest.fixture
    async def no_api_client(self, bb_env: None) -> AsyncGenerator[Client, None]:
        from bb_mcp.server import mcp

        with respx.mock(assert_all_called=False) as router:
            router.get(f"{API}/server/info").mock(
                return_value=server_info_ok(private_api=False)
            )
            async with Client(mcp) as c:
                yield c

    async def test_send_reaction_removed(self, no_api_client: Client) -> None:
        result = await no_api_client.call_tool(
            "send_reaction",
            {"chat_guid": "g1", "message_guid": "m1", "reaction": "love"},
            raise_on_error=False,
        )
        assert result.is_error

    async def test_edit_message_removed(self, no_api_client: Client) -> None:
        result = await no_api_client.call_tool(
            "edit_message",
            {"message_guid": "m1", "new_text": "edited"},
            raise_on_error=False,
        )
        assert result.is_error

    async def test_unsend_message_removed(self, no_api_client: Client) -> None:
        result = await no_api_client.call_tool(
            "unsend_message",
            {"message_guid": "m1"},
            raise_on_error=False,
        )
        assert result.is_error

    async def test_mark_chat_read_removed(self, no_api_client: Client) -> None:
        result = await no_api_client.call_tool(
            "mark_chat_read", {"chat_guid": "g1"}, raise_on_error=False
        )
        assert result.is_error

    async def test_mark_chat_unread_removed(self, no_api_client: Client) -> None:
        result = await no_api_client.call_tool(
            "mark_chat_unread", {"chat_guid": "g1"}, raise_on_error=False
        )
        assert result.is_error

    async def test_send_message_reply_to_guid_raises(
        self, no_api_client: Client
    ) -> None:
        with respx.mock(assert_all_called=False) as router:
            router.post(f"{API}/message/text").mock(return_value=ok_json({"guid": "x"}))
            result = await no_api_client.call_tool(
                "send_message",
                {"chat_guid": "g1", "message": "hi", "reply_to_guid": "parent"},
                raise_on_error=False,
            )
            assert result.is_error

    async def test_send_message_to_address_sms_raises(
        self, no_api_client: Client
    ) -> None:
        result = await no_api_client.call_tool(
            "send_message_to_address",
            {"address": "+15551234567", "message": "hi", "service": "SMS"},
            raise_on_error=False,
        )
        assert result.is_error

    async def test_send_message_to_address_lowercase_sms_raises(
        self, no_api_client: Client
    ) -> None:
        """Casing cannot slip past: the schema refuses anything but the two literals,
        and the guard behind it compares the canonical value."""
        result = await no_api_client.call_tool(
            "send_message_to_address",
            {"address": "+15551234567", "message": "hi", "service": "sms"},
            raise_on_error=False,
        )
        assert result.is_error

    async def test_sends_go_out_over_apple_script(self, no_api_client: Client) -> None:
        with respx.mock(assert_all_called=False) as router:
            route = router.post(f"{API}/message/text").mock(
                return_value=ok_json({"guid": "m1"})
            )
            await no_api_client.call_tool(
                "send_message", {"chat_guid": "g1", "message": "hi"}
            )
            assert last_body(route)["method"] == "apple-script"

    async def test_send_message_to_address_stays_on_message_text(
        self, no_api_client: Client
    ) -> None:
        with respx.mock(assert_all_called=False) as router:
            text = router.post(f"{API}/message/text").mock(
                return_value=ok_json({"guid": "m1"})
            )
            new_chat = router.post(f"{API}/chat/new").mock(return_value=ok_json({}))
            await no_api_client.call_tool(
                "send_message_to_address",
                {"address": "+15551234567", "message": "hi"},
            )
            assert text.called
            assert not new_chat.called
            assert last_body(text)["method"] == "apple-script"


# ---------------------------------------------------------------------------
# Bundled skill resources
# ---------------------------------------------------------------------------


class TestSkillResources:
    #: Every skill doc is comfortably over 3KB; a stub would fall well under.
    MIN_SKILL_FILE_BYTES = 1_000

    async def test_skill_dir_ships_with_the_package(self) -> None:
        from bb_mcp.server import SKILL_DIR

        assert (SKILL_DIR / "SKILL.md").is_file()
        assert (SKILL_DIR / "references" / "best-practices.md").is_file()
        assert (SKILL_DIR / "references" / "tools.md").is_file()

    async def test_lists_main_file_and_manifest(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        client, _ = mcp_client
        uris = {str(r.uri) for r in await client.list_resources()}
        assert "skill://bluebubbles/SKILL.md" in uris
        assert "skill://bluebubbles/_manifest" in uris

    async def test_supporting_files_exposed_via_template(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        client, _ = mcp_client
        templates = {t.uriTemplate for t in await client.list_resource_templates()}
        assert "skill://bluebubbles/{path*}" in templates
        # "template" mode: reference files are not listed individually.
        uris = {str(r.uri) for r in await client.list_resources()}
        assert not any(u.startswith("skill://bluebubbles/references/") for u in uris)

    async def test_reads_skill_md(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        client, _ = mcp_client
        contents = await client.read_resource("skill://bluebubbles/SKILL.md")
        text = contents[0].text  # type: ignore[union-attr]
        assert text.startswith("---")
        assert "name: bluebubbles" in text

    async def test_manifest_lists_every_file(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        import json

        client, _ = mcp_client
        contents = await client.read_resource("skill://bluebubbles/_manifest")
        manifest = json.loads(contents[0].text)  # type: ignore[union-attr]
        assert manifest["skill"] == "bluebubbles"
        assert {f["path"] for f in manifest["files"]} == {
            "SKILL.md",
            "references/best-practices.md",
            "references/tools.md",
        }

    async def test_every_file_served_byte_identical_to_disk(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        """Each advertised file must arrive intact.

        Asserting merely "non-empty" would still pass on a truncated or stale
        file shipped by a future packaging change, so compare the served bytes
        against both the manifest's own hash and the file on disk.
        """
        import hashlib
        import json

        from bb_mcp.server import SKILL_DIR

        client, _ = mcp_client
        manifest = json.loads(
            (await client.read_resource("skill://bluebubbles/_manifest"))[0].text  # type: ignore[union-attr]
        )
        assert manifest["files"], "manifest advertised no files"

        for entry in manifest["files"]:
            path = entry["path"]
            contents = await client.read_resource(f"skill://bluebubbles/{path}")
            served = contents[0].text.encode("utf-8")  # type: ignore[union-attr]
            digest = "sha256:" + hashlib.sha256(served).hexdigest()

            assert digest == entry["hash"], f"{path}: served bytes != manifest hash"
            assert len(served) == entry["size"], f"{path}: served size != manifest size"
            assert served == (SKILL_DIR / path).read_bytes(), (
                f"{path}: served bytes != file on disk"
            )
            # The manifest is generated from the files on disk, so hashes alone
            # are self-consistent even for a truncated file. A size floor is what
            # actually catches a packaging change that ships a stub.
            assert len(served) > self.MIN_SKILL_FILE_BYTES, (
                f"{path}: only {len(served)} bytes — looks truncated"
            )

    async def test_supporting_files_carry_real_content(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        """Hashes prove integrity; these prove we shipped the right documents."""
        client, _ = mcp_client

        tools = (await client.read_resource("skill://bluebubbles/references/tools.md"))[
            0
        ].text  # type: ignore[union-attr]
        assert "Private API" in tools

        practices = (
            await client.read_resource(
                "skill://bluebubbles/references/best-practices.md"
            )
        )[0].text  # type: ignore[union-attr]
        assert practices.lstrip().startswith("#")

    async def test_escaping_the_skill_dir_is_rejected(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        client, _ = mcp_client
        with pytest.raises(Exception):
            await client.read_resource("skill://bluebubbles/../../server.py")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


class TestCli:
    def _run(self, monkeypatch: pytest.MonkeyPatch, *args: str) -> dict[str, Any]:
        """Invoke the CLI with mcp.run stubbed; return the kwargs it was called with."""
        from typer.testing import CliRunner

        from bb_mcp import server

        captured: dict[str, Any] = {}

        def fake_run(**kwargs: Any) -> None:
            captured.update(kwargs)

        monkeypatch.setattr(server.mcp, "run", fake_run)
        result = CliRunner().invoke(server.app, list(args))
        assert result.exit_code == 0, result.output
        return captured

    def test_banner_is_suppressed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The banner is an ASCII box + PyPI update nag on stderr — noise for a
        stdio client, and it costs an HTTP call on every startup."""
        assert self._run(monkeypatch)["show_banner"] is False

    def test_runs_over_stdio(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._run(monkeypatch)["transport"] == "stdio"
