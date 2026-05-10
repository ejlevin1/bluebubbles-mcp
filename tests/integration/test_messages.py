"""Integration tests: message endpoints (read-only)."""

from __future__ import annotations

import pytest

from bb_mcp.client import BlueBubblesClient

pytestmark = pytest.mark.integration


async def test_search_messages_no_filter(client: BlueBubblesClient) -> None:
    messages = await client.search_messages(limit=10)
    assert isinstance(messages, list)


async def test_search_messages_with_query(client: BlueBubblesClient) -> None:
    messages = await client.search_messages(query="the", limit=5)
    assert isinstance(messages, list)


async def test_search_messages_in_chat(
    client: BlueBubblesClient, first_chat_guid: str
) -> None:
    messages = await client.search_messages(chat_guid=first_chat_guid, limit=5)
    assert isinstance(messages, list)


async def test_get_message(client: BlueBubblesClient, first_chat_guid: str) -> None:
    messages = await client.get_chat_messages(first_chat_guid, limit=1)
    if not messages:
        pytest.skip("No messages in first chat")
    msg = await client.get_message(messages[0]["guid"])
    assert msg["guid"] == messages[0]["guid"]


async def test_list_scheduled_messages(client: BlueBubblesClient) -> None:
    scheduled = await client.list_scheduled_messages()
    assert isinstance(scheduled, list)
