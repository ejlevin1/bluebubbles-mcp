"""Unit tests for the BlueBubbles MCP server layer."""

from __future__ import annotations

import base64
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client

BASE_URL = "http://bb.local:1234"
API = f"{BASE_URL}/api/v1"
PASSWORD = "test-secret"
MY_ADDRESS = "+15550000000"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def ok_json(data: Any = None) -> httpx.Response:
    return httpx.Response(200, json={"status": 200, "data": data})


def server_info_ok(private_api: bool = True, address: str = MY_ADDRESS) -> httpx.Response:
    return ok_json(
        {
            "private_api": private_api,
            "helper_connected": private_api,
            "detected_imessage": address,
            "detected_icloud": address,
            "server_version": "1.9.0",
            "os_version": "14.0",
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bb_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLUEBUBBLES_URL", BASE_URL)
    monkeypatch.setenv("BLUEBUBBLES_PASSWORD", PASSWORD)


@pytest.fixture
async def mcp_client(bb_env: None) -> AsyncGenerator[tuple[Client, respx.Router], None]:
    """In-process MCP client; lifespan mocked with private_api=True."""
    from bb_mcp.server import mcp

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{API}/server/info").mock(return_value=server_info_ok(private_api=True))
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
        assert result.data == MY_ADDRESS

    async def test_raises_when_no_address(self, bb_env: None) -> None:
        from bb_mcp.server import mcp

        with respx.mock(assert_all_called=False) as router:
            router.get(f"{API}/server/info").mock(
                return_value=ok_json(
                    {"private_api": True, "detected_imessage": None, "detected_icloud": None}
                )
            )
            async with Client(mcp) as c:
                result = await c.call_tool("get_my_address", {}, raise_on_error=False)
                assert result.is_error


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
    async def test_returns_result(self, mcp_client: tuple[Client, respx.Router]) -> None:
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
    async def test_returns_chat_dict(self, mcp_client: tuple[Client, respx.Router]) -> None:
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

    async def test_from_address_me_filters_client_side(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        router.get(f"{API}/chat/g1/message").mock(
            return_value=ok_json(
                [
                    {
                        "guid": "m1",
                        "handle": {"address": MY_ADDRESS, "service": "iMessage"},
                    },
                    {
                        "guid": "m2",
                        "handle": {"address": "+15559999999", "service": "iMessage"},
                    },
                ]
            )
        )
        result = await c.call_tool(
            "get_chat_messages", {"chat_guid": "g1", "from_address": "me"}
        )
        assert len(result.data) == 1
        assert result.data[0]["guid"] == "m1"


class TestGetRecentMessages:
    async def test_returns_slim_list(self, mcp_client: tuple[Client, respx.Router]) -> None:
        c, router = mcp_client
        router.post(f"{API}/message/query").mock(
            return_value=ok_json([{"guid": "m1", "text": "hey", "originalROWID": 5}])
        )
        result = await c.call_tool("get_recent_messages", {"minutes": 60})
        assert isinstance(result.data, list)
        assert "originalROWID" not in result.data[0]

    async def test_extended_passes_through(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        router.post(f"{API}/message/query").mock(
            return_value=ok_json([{"guid": "m1", "originalROWID": 5}])
        )
        result = await c.call_tool(
            "get_recent_messages", {"minutes": 60, "extended": True}
        )
        assert result.data[0]["originalROWID"] == 5

    async def test_from_address_me_resolves(
        self, mcp_client: tuple[Client, respx.Router]
    ) -> None:
        c, router = mcp_client
        router.post(f"{API}/message/query").mock(return_value=ok_json([]))
        result = await c.call_tool(
            "get_recent_messages", {"minutes": 60, "from_address": "me"}
        )
        assert isinstance(result.data, list)


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
    async def test_returns_slim_list(self, mcp_client: tuple[Client, respx.Router]) -> None:
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
    async def test_slim_by_default(self, mcp_client: tuple[Client, respx.Router]) -> None:
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
        router.post(f"{API}/message/text").mock(return_value=ok_json({"guid": "m1"}))
        result = await c.call_tool(
            "send_message", {"chat_guid": "g1", "message": "Hello"}
        )
        assert result.data["guid"] == "m1"


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
            "edit_message", {"message_guid": "m1", "new_text": "edited"},
            raise_on_error=False,
        )
        assert result.is_error

    async def test_unsend_message_removed(self, no_api_client: Client) -> None:
        result = await no_api_client.call_tool(
            "unsend_message", {"message_guid": "m1"},
            raise_on_error=False,
        )
        assert result.is_error

    async def test_send_message_reply_to_guid_raises(self, no_api_client: Client) -> None:
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
