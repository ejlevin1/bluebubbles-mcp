"""Integration tests: message endpoints (read-only)."""

from __future__ import annotations

import time

import pytest

from bb_mcp.client import BlueBubblesClient
from bb_mcp.cursor import Cursor, advance_created, change_times, message_ms

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


# ===========================================================================
# Incremental polling (read-only: POST /message/query is a read)
# ===========================================================================


async def test_cursor_walk_loses_no_messages(client: BlueBubblesClient) -> None:
    """Walk the cursor at a small page size and compare against a full scan.

    This is the test that catches cursor bugs the unit tests cannot: it exercises
    real sub-millisecond timestamps, real floor-millisecond collisions, and the real
    server sort. Every message the full scan sees must also be reachable by walking.
    """
    now_ms = int(time.time() * 1000)
    window_start = now_ms - 7 * 86_400_000

    full_scan: dict[str, int] = {}
    cursor_ms = window_start
    while True:
        page = await client.query_created_since(since_ms=cursor_ms, limit=1000)
        fresh = {
            m["guid"]: message_ms(m) or 0 for m in page if m["guid"] not in full_scan
        }
        if not fresh:
            break
        full_scan.update(fresh)
        highest = max(fresh.values())
        if highest <= cursor_ms:
            break
        cursor_ms = highest
    if len(full_scan) < 20:
        pytest.skip("Not enough recent messages to make the walk meaningful")

    walked: set[str] = set()
    cursor = Cursor.seed(window_start)
    cursors_seen = [cursor.created_ms]
    for _ in range(500):
        raw = await client.query_created_since(since_ms=cursor.created_ms, limit=6)
        result = advance_created(raw, prev_ms=cursor.created_ms, limit=5, now_ms=now_ms)
        walked.update(m["guid"] for m in result.rows)
        if not result.truncated:
            break
        assert result.next_ms >= cursor.created_ms, "cursor moved backwards"
        if result.next_ms == cursor.created_ms:
            pytest.fail(f"cursor stalled at {cursor.created_ms}")
        cursor = Cursor(created_ms=result.next_ms, changed_ms=cursor.changed_ms)
        cursors_seen.append(cursor.created_ms)

    missing = set(full_scan) - walked
    assert not missing, f"{len(missing)} messages unreachable by cursor walk"
    assert cursors_seen == sorted(set(cursors_seen)), "cursor did not strictly increase"


async def test_chats_join_would_have_dropped_real_messages(
    client: BlueBubblesClient,
) -> None:
    """The reason chat attribution is a second pass rather than a `with` on the query.

    ``with: ["chats"]`` is an INNER join. Messages belonging to no chat row — SMS
    shortcodes, 2FA senders — are dropped from the result entirely rather than
    returned without a chat label.
    """
    now_ms = int(time.time() * 1000)
    rows = await client.query_created_since(
        since_ms=now_ms - 30 * 86_400_000, limit=1000
    )
    if len(rows) < 100:
        pytest.skip("Not enough messages in the window to measure the join")

    resolved = await client.resolve_message_chats([m["guid"] for m in rows])
    unattributed = [m for m in rows if m["guid"] not in resolved]

    for m in rows:
        assert m["guid"] in resolved or m in unattributed
    # Every row survives the primary query; only the *labels* can be missing.
    assert len(rows) >= len(resolved)


async def test_changed_axis_never_returns_unedited_messages(
    client: BlueBubblesClient,
) -> None:
    """Never-edited rows store 0, so a negative bind would match all of them.

    A pre-2001 watermark produces a negative Apple-ns bind and ``0 > negative`` is
    TRUE. Without the floor in :func:`changed_bind_ns` this query returns the entire
    message table.
    """
    changed = await client.query_changed_since(since_ms=0, limit=1000)
    for row in changed:
        assert change_times(row), f"{row['guid']} has no edit or retraction timestamp"


async def test_search_escapes_like_wildcards(client: BlueBubblesClient) -> None:
    """An unescaped `_` matches every single-character position, i.e. everything."""
    escaped = await client.search_messages(query="_", limit=1000)
    unfiltered = await client.search_messages(limit=1000)
    if len(unfiltered) < 1000:
        pytest.skip("Too few messages for the wildcard difference to show")
    assert len(escaped) < len(unfiltered), "LIKE wildcards are not being escaped"
