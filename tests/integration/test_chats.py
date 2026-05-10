"""Integration tests: chat endpoints (read-only)."""

from __future__ import annotations

import pytest

from bb_mcp.client import BlueBubblesClient

pytestmark = pytest.mark.integration


async def test_list_chats_returns_list(client: BlueBubblesClient) -> None:
    chats = await client.list_chats(limit=10)
    assert isinstance(chats, list)


async def test_list_chats_have_guid(client: BlueBubblesClient) -> None:
    chats = await client.list_chats(limit=10)
    for chat in chats:
        assert "guid" in chat


async def test_list_chats_pagination(client: BlueBubblesClient) -> None:
    page1 = await client.list_chats(limit=5, offset=0)
    page2 = await client.list_chats(limit=5, offset=5)
    if len(page1) == 5 and len(page2) > 0:
        assert page1[0]["guid"] != page2[0]["guid"]


async def test_get_chat(client: BlueBubblesClient, first_chat_guid: str) -> None:
    chat = await client.get_chat(
        first_chat_guid, with_fields=["participants", "lastmessage"]
    )
    assert chat["guid"] == first_chat_guid


async def test_get_chat_messages(
    client: BlueBubblesClient, first_chat_guid: str
) -> None:
    messages = await client.get_chat_messages(first_chat_guid, limit=10)
    assert isinstance(messages, list)


async def test_get_chat_messages_have_guid(
    client: BlueBubblesClient, first_chat_guid: str
) -> None:
    messages = await client.get_chat_messages(first_chat_guid, limit=5)
    for msg in messages:
        assert "guid" in msg


async def test_get_chat_messages_sort_asc(
    client: BlueBubblesClient, first_chat_guid: str
) -> None:
    messages = await client.get_chat_messages(first_chat_guid, limit=5, sort="ASC")
    assert isinstance(messages, list)


async def test_list_chats_with_lastmessage(client: BlueBubblesClient) -> None:
    chats = await client.list_chats(limit=5, with_fields=["lastmessage"])
    assert isinstance(chats, list)
