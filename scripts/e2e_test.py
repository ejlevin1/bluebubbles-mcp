#!/usr/bin/env python3
"""End-to-end tests for all BlueBubbles MCP server tools.

Uses the fastmcp in-memory Client against a live BlueBubbles server.
Loads credentials from .env automatically.

Usage:
    uv run scripts/e2e_test.py
    uv run scripts/e2e_test.py --send   # also sends real messages to BLUEBUBBLES_MY_ADDRESS
    uv run scripts/e2e_test.py --no-send
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

import typer
from fastmcp import Client

# ---------------------------------------------------------------------------
# .env loader — must run before the server module is imported
# ---------------------------------------------------------------------------


def _load_dotenv() -> None:
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

from bb_mcp.server import mcp  # noqa: E402 — must come after env load

# ---------------------------------------------------------------------------
# Test reporter
# ---------------------------------------------------------------------------

_SLIM_FIELDS = {
    "guid",
    "text",
    "handle",
    "isFromMe",
    "dateCreated",
    "attachments",
    "replyToGuid",
    "associatedMessageGuid",
    "associatedMessageType",
    "dateEdited",
    "dateRetracted",
    "isAudioMessage",
    "chats",
    "error",
}
_EXTENDED_SENTINEL = "originalROWID"  # present in extended, absent in slim


class Suite:
    def __init__(self, name: str) -> None:
        self.name = name
        self._passed = 0
        self._failed = 0
        self._skipped = 0
        print(f"\n{'─' * 60}")
        print(f"  {name}")
        print(f"{'─' * 60}")

    def ok(self, label: str) -> None:
        self._passed += 1
        print(f"  [PASS] {label}")

    def fail(self, label: str, detail: str = "") -> None:
        self._failed += 1
        msg = f"  [FAIL] {label}"
        if detail:
            msg += f"\n         {detail}"
        print(msg)

    def skip(self, label: str, reason: str = "") -> None:
        self._skipped += 1
        msg = f"  [SKIP] {label}"
        if reason:
            msg += f" — {reason}"
        print(msg)

    def check(self, condition: bool, label: str, detail: str = "") -> bool:
        if condition:
            self.ok(label)
        else:
            self.fail(label, detail)
        return condition

    @property
    def totals(self) -> tuple[int, int, int]:
        return self._passed, self._failed, self._skipped


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _data(result: Any) -> Any:
    return result.data


def _first_chat_guid(chats: list[dict]) -> str | None:
    return chats[0]["guid"] if chats else None


def _first_message_guid(messages: list[dict]) -> str | None:
    return messages[0]["guid"] if messages else None


def _first_attachment_guid(messages: list[dict]) -> str | None:
    for msg in messages:
        for att in msg.get("attachments") or []:
            if att.get("guid"):
                return att["guid"]
    return None


# ---------------------------------------------------------------------------
# Test sections
# ---------------------------------------------------------------------------


async def test_server_health(c: Client) -> Suite:
    s = Suite("1. Server health")

    result = await c.call_tool("ping", {})
    s.check(not result.is_error and bool(result.content), "ping returns a result")

    info = _data(await c.call_tool("get_server_info", {}))
    s.check(isinstance(info, dict), "get_server_info returns dict")
    s.check("server_version" in info, "get_server_info has server_version")
    s.check("private_api" in info, "get_server_info has private_api flag")

    addr = _data(await c.call_tool("get_my_address", {}))
    s.check(
        isinstance(addr, str) and len(addr) > 0,
        f"get_my_address returns non-empty string: {addr!r}",
    )

    return s


async def test_contacts(c: Client, my_address: str) -> Suite:
    s = Suite("2. Contacts")

    all_contacts = _data(await c.call_tool("get_contacts", {}))
    s.check(
        isinstance(all_contacts, list),
        f"get_contacts returns list ({len(all_contacts)} contacts)",
    )

    filtered = _data(await c.call_tool("get_contacts", {"query": "a"}))
    s.check(isinstance(filtered, list), "get_contacts(query='a') returns list")
    s.check(len(filtered) <= len(all_contacts), "get_contacts query narrows results")

    empty = _data(await c.call_tool("get_contacts", {"query": "zzz_no_match_xqq"}))
    s.check(empty == [], "get_contacts nonsense query returns empty list")

    lookup = _data(await c.call_tool("lookup_contact", {"addresses": [my_address]}))
    s.check(
        isinstance(lookup, list),
        f"lookup_contact([my_address]) returns list ({len(lookup)} results)",
    )

    return s


async def test_chats(c: Client) -> tuple[Suite, str | None]:
    s = Suite("3. Chats")

    chats = _data(await c.call_tool("list_chats", {"limit": 10}))
    s.check(isinstance(chats, list), f"list_chats returns list ({len(chats)} chats)")

    first_guid = _first_chat_guid(chats)
    if not first_guid:
        s.skip("get_chat", "no chats available")
    else:
        chat = _data(await c.call_tool("get_chat", {"chat_guid": first_guid}))
        s.check(isinstance(chat, dict), "get_chat returns dict")
        s.check("guid" in chat, "get_chat result has guid")

    unread = _data(await c.call_tool("get_unread_chats", {"message_limit": 3}))
    s.check(
        isinstance(unread, list),
        f"get_unread_chats returns list ({len(unread)} unread)",
    )
    for item in unread:
        if not s.check(
            "chat" in item and "recent_messages" in item,
            "get_unread_chats item has chat + recent_messages",
        ):
            break
    if unread:
        s.check(
            all(len(item["recent_messages"]) <= 3 for item in unread),
            "get_unread_chats message_limit respected",
        )

    return s, first_guid


async def test_messages(
    c: Client, chat_guid: str | None, my_address: str
) -> tuple[Suite, str | None]:
    s = Suite("4. Messages — read")

    first_msg_guid: str | None = None
    first_att_guid: str | None = None

    # --- get_chat_messages ---
    if not chat_guid:
        s.skip("get_chat_messages", "no chat_guid available")
    else:
        msgs = _data(
            await c.call_tool(
                "get_chat_messages", {"chat_guid": chat_guid, "limit": 10}
            )
        )
        s.check(
            isinstance(msgs, list),
            f"get_chat_messages returns list ({len(msgs)} messages)",
        )
        first_msg_guid = _first_message_guid(msgs)
        first_att_guid = _first_attachment_guid(msgs)

        if msgs:
            extra_keys = set(msgs[0].keys()) - _SLIM_FIELDS
            s.check(
                _EXTENDED_SENTINEL not in msgs[0],
                "get_chat_messages compact: originalROWID absent",
                f"unexpected extended fields: {extra_keys}" if extra_keys else "",
            )

        msgs_ext = _data(
            await c.call_tool(
                "get_chat_messages",
                {"chat_guid": chat_guid, "limit": 5, "extended": True},
            )
        )
        if msgs_ext:
            s.check(
                _EXTENDED_SENTINEL in msgs_ext[0],
                "get_chat_messages extended=True: originalROWID present",
            )
        else:
            s.skip("get_chat_messages extended check", "no messages to inspect")

        msgs_me = _data(
            await c.call_tool(
                "get_chat_messages",
                {"chat_guid": chat_guid, "limit": 25, "from_address": "me"},
            )
        )
        s.check(
            isinstance(msgs_me, list),
            f"get_chat_messages(from_address='me') returns list ({len(msgs_me)} messages)",
        )
        bad = [
            m for m in msgs_me if (m.get("handle") or {}).get("address") != my_address
        ]
        s.check(
            len(bad) == 0,
            "get_chat_messages(from_address='me') all messages match user address",
            f"{len(bad)} messages with unexpected handle",
        )

        msgs_asc = _data(
            await c.call_tool(
                "get_chat_messages", {"chat_guid": chat_guid, "limit": 5, "sort": "ASC"}
            )
        )
        s.check(isinstance(msgs_asc, list), "get_chat_messages sort=ASC returns list")
        if len(msgs_asc) >= 2 and len(msgs) >= 2:
            s.check(
                msgs_asc[0]["dateCreated"] <= msgs_asc[-1]["dateCreated"],
                "get_chat_messages ASC sort is oldest-first",
            )

    # --- get_recent_messages ---
    recent = _data(
        await c.call_tool("get_recent_messages", {"minutes": 10080, "limit": 20})
    )
    s.check(
        isinstance(recent, list),
        f"get_recent_messages returns list ({len(recent)} messages)",
    )
    if not first_msg_guid and recent:
        first_msg_guid = _first_message_guid(recent)
    if not first_att_guid and recent:
        first_att_guid = _first_attachment_guid(recent)

    recent_me = _data(
        await c.call_tool(
            "get_recent_messages", {"minutes": 10080, "limit": 20, "from_address": "me"}
        )
    )
    s.check(
        isinstance(recent_me, list),
        f"get_recent_messages(from_address='me') returns list ({len(recent_me)} messages)",
    )

    # --- search_messages ---
    search_all = _data(await c.call_tool("search_messages", {"limit": 10}))
    s.check(
        isinstance(search_all, list),
        f"search_messages() returns list ({len(search_all)} messages)",
    )
    if not first_msg_guid and search_all:
        first_msg_guid = _first_message_guid(search_all)

    search_text = _data(
        await c.call_tool("search_messages", {"query": "the", "limit": 10})
    )
    s.check(
        isinstance(search_text, list),
        f"search_messages(query='the') returns list ({len(search_text)} messages)",
    )
    bad_text = [m for m in search_text if "the" not in (m.get("text") or "").lower()]
    s.check(
        len(bad_text) == 0,
        "search_messages text query results contain query string",
        f"{len(bad_text)} messages missing 'the'",
    )

    search_me = _data(
        await c.call_tool("search_messages", {"limit": 10, "from_address": "me"})
    )
    s.check(
        isinstance(search_me, list),
        f"search_messages(from_address='me') returns list ({len(search_me)} messages)",
    )

    search_none = _data(
        await c.call_tool("search_messages", {"query": "zzz_no_match_xqq_e2e"})
    )
    s.check(search_none == [], "search_messages nonsense query returns empty list")

    # --- get_message ---
    if not first_msg_guid:
        s.skip("get_message", "no message_guid available from any query")
    else:
        msg = _data(await c.call_tool("get_message", {"message_guid": first_msg_guid}))
        s.check(isinstance(msg, dict), "get_message returns dict")
        s.check(msg.get("guid") == first_msg_guid, "get_message guid matches request")
        s.check(
            _EXTENDED_SENTINEL not in msg, "get_message compact: originalROWID absent"
        )

        msg_ext = _data(
            await c.call_tool(
                "get_message", {"message_guid": first_msg_guid, "extended": True}
            )
        )
        s.check(
            _EXTENDED_SENTINEL in msg_ext,
            "get_message extended=True: originalROWID present",
        )

    return s, first_att_guid


async def test_scheduled_messages(c: Client) -> Suite:
    s = Suite("5. Scheduled messages")

    scheduled = _data(await c.call_tool("list_scheduled_messages", {}))
    s.check(
        isinstance(scheduled, list),
        f"list_scheduled_messages returns list ({len(scheduled)} scheduled)",
    )

    return s


async def test_attachments(c: Client, att_guid: str | None) -> Suite:
    s = Suite("6. Attachments")

    if not att_guid:
        s.skip("get_attachment_info", "no attachment found in recent messages")
        s.skip("download_attachment", "no attachment found in recent messages")
        return s

    info = _data(
        await c.call_tool("get_attachment_info", {"attachment_guid": att_guid})
    )
    s.check(isinstance(info, dict), "get_attachment_info returns dict")
    s.check(
        "mimeType" in info or "transferName" in info,
        "get_attachment_info has mimeType or transferName",
    )

    dl = await c.call_tool("download_attachment", {"attachment_guid": att_guid})
    s.check(not dl.is_error and bool(dl.content), "download_attachment returns data")

    return s


async def test_send(c: Client, my_address: str) -> Suite:
    s = Suite("7. Send (live — messages will be delivered)")

    # Send to self via address — this also returns the chat GUID we can reuse.
    ta = _data(
        await c.call_tool(
            "send_message_to_address",
            {
                "address": my_address,
                "message": "[bb-mcp e2e] send_message_to_address",
            },
        )
    )
    s.check(
        isinstance(ta, dict) and "guid" in ta,
        "send_message_to_address returns dict with guid",
    )

    # Derive the self-chat GUID from the sent message so send_message also goes
    # to self — not to whatever arbitrary chat happened to be most recent.
    self_chat_guid: str | None = None
    if isinstance(ta, dict):
        chats = ta.get("chats") or []
        if chats and isinstance(chats[0], dict):
            self_chat_guid = chats[0].get("guid")

    if not self_chat_guid:
        s.skip("send_message", "could not derive self-chat GUID from sent message")
    else:
        sm = _data(
            await c.call_tool(
                "send_message",
                {
                    "chat_guid": self_chat_guid,
                    "message": "[bb-mcp e2e] send_message",
                },
            )
        )
        s.check(
            isinstance(sm, dict) and "guid" in sm, "send_message returns dict with guid"
        )

    return s


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

app = typer.Typer()


@app.command()
def main(
    send: bool = typer.Option(
        False,
        "--send/--no-send",
        help="Also run send tests (sends real messages to BLUEBUBBLES_MY_ADDRESS).",
    ),
    url: Optional[str] = typer.Option(
        None,
        "--url",
        envvar="BLUEBUBBLES_URL",
        help="BlueBubbles server URL.",
    ),
    password: Optional[str] = typer.Option(
        None,
        "--password",
        envvar="BLUEBUBBLES_PASSWORD",
        help="BlueBubbles server password.",
    ),
    my_address: Optional[str] = typer.Option(
        None,
        "--my-address",
        envvar="BLUEBUBBLES_MY_ADDRESS",
        help="Override detected user iMessage address.",
    ),
) -> None:
    """Run end-to-end tests against a live BlueBubbles server."""
    if not url or not password:
        typer.echo(
            "ERROR: BLUEBUBBLES_URL and BLUEBUBBLES_PASSWORD must be set (or present in .env)",
            err=True,
        )
        raise typer.Exit(1)

    os.environ["BLUEBUBBLES_URL"] = url
    os.environ["BLUEBUBBLES_PASSWORD"] = password
    if my_address:
        mcp._my_address_override = my_address  # type: ignore[attr-defined]

    asyncio.run(_run(send=send))


async def _run(send: bool) -> None:
    suites: list[Suite] = []

    async with Client(mcp) as c:
        # Always need my_address for filter tests
        my_address: str = _data(await c.call_tool("get_my_address", {})) or ""

        s_health = await test_server_health(c)
        suites.append(s_health)

        s_contacts = await test_contacts(c, my_address)
        suites.append(s_contacts)

        s_chats, first_guid = await test_chats(c)
        suites.append(s_chats)

        s_messages, att_guid = await test_messages(c, first_guid, my_address)
        suites.append(s_messages)

        s_scheduled = await test_scheduled_messages(c)
        suites.append(s_scheduled)

        s_attachments = await test_attachments(c, att_guid)
        suites.append(s_attachments)

        if send:
            if not my_address:
                typer.echo(
                    "WARNING: --send requires BLUEBUBBLES_MY_ADDRESS; skipping send tests.",
                    err=True,
                )
            else:
                s_send = await test_send(c, my_address)
                suites.append(s_send)
        else:
            print("\n[SKIP] Send tests — pass --send to enable")

    # Summary
    total_pass = sum(s.totals[0] for s in suites)
    total_fail = sum(s.totals[1] for s in suites)
    total_skip = sum(s.totals[2] for s in suites)

    print(f"\n{'═' * 60}")
    print(f"  RESULTS: {total_pass} passed, {total_fail} failed, {total_skip} skipped")
    print(f"{'═' * 60}")

    if total_fail:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
