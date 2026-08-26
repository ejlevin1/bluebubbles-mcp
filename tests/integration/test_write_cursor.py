"""Integration tests that SEND REAL MESSAGES.

Gated on ``TEST_WRITE_GUID``. Unset or empty — as in CI, which has no ``.env`` —
and every test here skips.

These sends are real and permanent: without the Private API there is no unsend, so
nothing here can clean up after itself. Point ``TEST_WRITE_GUID`` at a chat you own.

What they buy over the read-only suite: a message that did not exist when the cursor
was issued. That is the one thing a read-only walk cannot manufacture, and it is the
only way to prove the loop end to end — send, poll, arrive exactly once, and never
again.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncGenerator

import pytest
from fastmcp import Client

from bb_mcp.cursor import Cursor

pytestmark = [pytest.mark.integration, pytest.mark.write]

#: How long to wait for a sent message to land in chat.db. AppleScript sends are
#: asynchronous — the POST returns before Messages has committed the row.
DELIVERY_TIMEOUT_S = 45.0
POLL_INTERVAL_S = 1.5


@pytest.fixture
async def tools(bb_url: str, bb_password: str) -> AsyncGenerator[Client, None]:
    """The MCP tool layer, in process, against the live server."""
    from bb_mcp.server import mcp

    async with Client(mcp) as client:
        yield client


def _marker() -> str:
    """A body unique enough to identify one send unambiguously."""
    return f"[bb-mcp test {uuid.uuid4().hex[:12]}]"


async def _poll_until(
    tools: Client, cursor: str, predicate, deadline: float
) -> tuple[list[dict], str]:
    """Drain the cursor until `predicate` sees what it wants, or time runs out.

    Returns every message collected across the walk and the final cursor. Draining
    `has_more` fully matters: a backlog would otherwise be mistaken for the message
    never arriving.
    """
    collected: list[dict] = []
    while True:
        result = (await tools.call_tool("get_recent_messages", {"since": cursor})).data
        collected.extend(result["messages"])
        cursor = result["cursor"]
        if predicate(collected):
            return collected, cursor
        if result["has_more"]:
            continue  # backlog — keep going without sleeping
        if time.monotonic() > deadline:
            return collected, cursor
        await asyncio.sleep(POLL_INTERVAL_S)


async def test_sent_message_arrives_once_and_advances_the_cursor(
    tools: Client, test_write_guid: str
) -> None:
    """The whole feature in one test, using a message that did not exist before.

    A read-only walk can only ever replay history. Sending first is the only way to
    prove the three properties that matter together: a brand-new message is reachable
    from a cursor minted before it, it arrives exactly once, and the watermark moves
    past it rather than stalling and looping forever.

    Deliberately one test rather than three — every send is real and, without the
    Private API, permanent.
    """
    start = (
        await tools.call_tool("get_recent_messages", {"minutes": 1, "limit": 50})
    ).data
    cursor = start["cursor"]
    before = Cursor.parse(cursor, now_ms=int(time.time() * 1000))
    assert not any(
        (m.get("text") or "").startswith("[bb-mcp test ") for m in start["messages"]
    ), "a previous run's marker is still in the window; rerun in a moment"

    marker = _marker()
    await tools.call_tool(
        "send_message", {"chat_guid": test_write_guid, "message": marker}
    )

    deadline = time.monotonic() + DELIVERY_TIMEOUT_S
    collected, cursor = await _poll_until(
        tools,
        cursor,
        lambda rows: any(m.get("text") == marker for m in rows),
        deadline,
    )

    matches = [m for m in collected if m.get("text") == marker]
    assert matches, (
        f"sent message never arrived through the cursor within {DELIVERY_TIMEOUT_S}s"
    )
    assert any(m["isFromMe"] for m in matches), "no outgoing row for the sent message"

    # "Exactly once" is a property of GUIDs, not of text. A message sent to your own
    # number legitimately lands as TWO rows with the same body and different GUIDs —
    # one outgoing, one received — so asserting on the text would fail for a reason
    # that has nothing to do with the cursor.
    delivered = [m["guid"] for m in collected]
    repeats = {g for g in delivered if delivered.count(g) > 1}
    assert not repeats, f"cursor delivered {len(repeats)} message(s) more than once"

    after = Cursor.parse(cursor, now_ms=int(time.time() * 1000))
    assert after.created_ms > before.created_ms, "cursor did not advance past the send"
    assert after.changed_ms >= before.changed_ms, "changed watermark moved backwards"

    # Polling again must not hand it back.
    again = (await tools.call_tool("get_recent_messages", {"since": cursor})).data
    returned_again = {m["guid"] for m in again["messages"]} & {
        m["guid"] for m in matches
    }
    assert not returned_again, "cursor re-delivered a message it had already returned"


async def test_a_send_is_not_visible_to_a_cursor_minted_after_it(
    tools: Client, test_write_guid: str
) -> None:
    """The converse: a cursor taken after the send must not re-report it.

    Guards against a watermark that is too conservative — one that keeps handing back
    already-consumed history and floods the caller with duplicates on every poll.
    """
    marker = _marker()
    await tools.call_tool(
        "send_message", {"chat_guid": test_write_guid, "message": marker}
    )

    deadline = time.monotonic() + DELIVERY_TIMEOUT_S
    while time.monotonic() < deadline:
        window = (
            await tools.call_tool("get_recent_messages", {"minutes": 5, "limit": 200})
        ).data
        if any(m.get("text") == marker for m in window["messages"]):
            break
        await asyncio.sleep(POLL_INTERVAL_S)
    else:
        pytest.fail("sent message never landed in the recent window")

    # Everything up to now is consumed; the next poll should be empty of our marker.
    fresh = (
        await tools.call_tool("get_recent_messages", {"since": window["cursor"]})
    ).data
    assert not any(m.get("text") == marker for m in fresh["messages"])
