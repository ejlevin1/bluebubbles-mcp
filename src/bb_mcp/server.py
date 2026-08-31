"""MCP server for BlueBubbles iMessage bridge."""

from __future__ import annotations

import asyncio
import base64
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import typer
from fastmcp import Context, FastMCP
from fastmcp.server.providers.skills import SkillProvider
from fastmcp.utilities.logging import get_logger
from fastmcp.utilities.types import Image
from mcp.types import ToolAnnotations

from bb_mcp import __version__
from bb_mcp.client import BlueBubblesClient, BlueBubblesError
from bb_mcp.cursor import (
    CURSOR_VERSIONS,
    MAX_PAGE,
    Cursor,
    advance_changed,
    advance_created,
    merge_monotonic,
    split_overfetch,
    summarize_reactions,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

IDEMPOTENT_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

SEND = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)

DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)

# ---------------------------------------------------------------------------
# Lifespan: create/destroy the shared BlueBubbles client
# ---------------------------------------------------------------------------


PRIVATE_API_TOOLS = [
    "send_reaction",
    "edit_message",
    "unsend_message",
    "start_typing",
    "stop_typing",
    "send_attachment",
    "check_imessage",
    "check_facetime",
    # Read receipts go out over the Private API too. Without it the server answers
    # `POST /chat/:guid/read` with a 500 whose body reads "iMessage Private API is
    # not enabled!" — so advertising these would offer an agent a tool that cannot
    # work, on any chat.
    "mark_chat_read",
    "mark_chat_unread",
]


#: Accepted values for ``BLUEBUBBLES_SEND_METHOD``. ``auto`` follows the server.
SEND_METHODS = ("auto", "apple-script", "private-api")


def _read_send_method() -> str:
    """Read and validate ``BLUEBUBBLES_SEND_METHOD``.

    Validated at startup rather than at send time: a typo'd ``applescript`` that
    quietly meant ``auto`` would be indistinguishable from working configuration
    until a send behaved unexpectedly, which is the class of bug this whole knob
    exists to make visible.
    """
    raw = (os.environ.get("BLUEBUBBLES_SEND_METHOD") or "auto").strip().lower()
    if raw not in SEND_METHODS:
        raise RuntimeError(
            "BLUEBUBBLES_SEND_METHOD must be one of "
            f"{', '.join(SEND_METHODS)}; got {raw!r}."
        )
    return raw


def _send_capable(info: dict[str, Any], send_method: str) -> bool:
    """Whether sends on this connection may go out over the Private API.

    ``private_api`` is the config toggle; ``helper_connected`` reports whether the
    injected helper is actually attached, and it drops whenever Messages.app
    restarts. A ``private_api: true / helper_connected: false`` server still sends
    perfectly well over AppleScript, so pinning on the toggle alone would fail
    every send there with the very error this capability check removes.

    Truthiness, not identity: a Node JSON serializer is free to emit
    ``helper_connected: 0``, which ``is not False`` and would slip through an
    identity test. An absent field is unknown rather than disconnected — older
    servers do not report it at all — so it allows.
    """
    if send_method != "auto":
        return send_method == "private-api"
    helper = info.get("helper_connected")
    connected = True if helper is None else bool(helper)
    return bool(info.get("private_api")) and connected


def _account_aliases(account: Any) -> list[str]:
    """Ordered, de-duplicated alias list from an ``/icloud/account`` payload.

    ``active_alias`` leads: it is the address Apple currently sends from, which
    makes it the right thing to report as *the* user's address. Hidden aliases are
    dropped — ``IsUserVisible`` is false for addresses Apple keeps internally, and
    surfacing those would be noise. ``Status`` is deliberately not filtered on: the
    codes are undocumented and guessing at them risks discarding a live alias.
    """
    if not isinstance(account, dict):
        return []
    ordered: list[str] = []
    active = account.get("active_alias")
    if isinstance(active, str) and active.strip():
        ordered.append(active.strip())
    for entry in account.get("aliases") or []:
        if not isinstance(entry, dict) or entry.get("IsUserVisible") is False:
            continue
        alias = entry.get("Alias")
        if isinstance(alias, str) and alias.strip():
            ordered.append(alias.strip())
    seen: set[str] = set()
    out: list[str] = []
    for alias in ordered:
        key = alias.lower()
        if key not in seen:
            seen.add(key)
            out.append(alias)
    return out


async def _resolve_identity(
    client: BlueBubblesClient, info: dict[str, Any], override: str | None
) -> dict[str, Any]:
    """Work out who the local user is, best source first.

    Enriching from the Private API is strictly optional: any failure falls back to
    the ``/server/info`` fields, which is exactly the behaviour on a server without
    the Private API. Startup must not depend on it.
    """
    detected = [
        a
        for a in (info.get("detected_imessage"), info.get("detected_icloud"))
        if isinstance(a, str) and a.strip()
    ]
    aliases: list[str] = []
    name: str | None = None
    source = "server_info"

    if info.get("private_api"):
        try:
            account = await client.icloud_account()
        except Exception as exc:  # noqa: BLE001 - enrichment is best-effort
            logger.warning(
                "Could not read /icloud/account (%s); falling back to the address "
                "detected by /server/info.",
                exc,
            )
        else:
            aliases = _account_aliases(account)
            if isinstance(account, dict):
                raw_name = account.get("account_name")
                name = raw_name.strip() if isinstance(raw_name, str) else None
            if aliases:
                source = "icloud_account"

    # Detected addresses stay in the set even when aliases came back — they are
    # what older code reported, and dropping one would silently narrow matching.
    addresses = list(aliases)
    seen = {a.lower() for a in addresses}
    for addr in detected:
        if addr.lower() not in seen:
            seen.add(addr.lower())
            addresses.append(addr)

    # An explicit override wins the default slot without evicting anything.
    if override and override.strip():
        override = override.strip()
        addresses = [override] + [a for a in addresses if a.lower() != override.lower()]
        source = "override"

    return {
        "address": addresses[0] if addresses else None,
        "addresses": addresses,
        "name": name,
        "source": source,
    }


@asynccontextmanager
async def lifespan(server: FastMCP):
    url = os.environ.get("BLUEBUBBLES_URL")
    password = os.environ.get("BLUEBUBBLES_PASSWORD")
    if not url or not password:
        raise RuntimeError(
            "BLUEBUBBLES_URL and BLUEBUBBLES_PASSWORD environment variables are required"
        )
    send_method = _read_send_method()
    client = BlueBubblesClient(url, password)
    info = await client.server_info()
    # The send path and the Private-API *routes* are separate questions: the
    # routes below follow the raw toggle, sends follow the capability, which also
    # accounts for the helper and for the operator's override.
    send_private_api = _send_capable(info, send_method)
    client.private_api = send_private_api
    if send_private_api != bool(info.get("private_api")):
        logger.warning(
            "Sends will use %s (BLUEBUBBLES_SEND_METHOD=%s, private_api=%s, "
            "helper_connected=%s).",
            "the Private API" if send_private_api else "AppleScript",
            send_method,
            info.get("private_api"),
            info.get("helper_connected"),
        )
    if not info.get("private_api"):
        logger.warning(
            "BlueBubbles Private API is not enabled (helper_connected=%s). "
            "Disabling tools: %s",
            info.get("helper_connected"),
            ", ".join(PRIVATE_API_TOOLS),
        )
        for tool_name in PRIVATE_API_TOOLS:
            try:
                server.local_provider.remove_tool(tool_name)
            except KeyError:
                pass  # already removed (e.g. second lifespan run in tests)
        # Strip reply_to_guid from send_message — it requires Private API
        send_msg_tool = await server.local_provider.get_tool("send_message")
        if send_msg_tool:
            send_msg_tool.parameters.get("properties", {}).pop("reply_to_guid", None)
            required = send_msg_tool.parameters.get("required", [])
            if "reply_to_guid" in required:
                required.remove("reply_to_guid")
    else:
        logger.info("BlueBubbles Private API is enabled — all tools available.")
    override = getattr(server, "_my_address_override", None)
    identity = await _resolve_identity(client, info, override)
    try:
        yield {
            "bb": client,
            "private_api": bool(info.get("private_api")),
            "send_private_api": send_private_api,
            "me": identity["address"],
            "identity": identity,
        }
    finally:
        await client.close()


mcp = FastMCP(
    "BlueBubbles",
    version=__version__,
    instructions=(
        "Use this server for anything involving iMessage or SMS text messages on the user's "
        "Apple iPhone or Mac. It bridges the user's real personal messaging via BlueBubbles.\n\n"
        "Covers: reading and sending iMessage/SMS, searching message history, managing group "
        "chats, sending attachments/photos/files, scheduling future messages, tapback reactions, "
        "editing/unsending messages, marking chats read/unread, looking up contacts, and checking "
        "iMessage or FaceTime availability for a phone number or email.\n\n"
        "To follow conversations over time, poll get_recent_messages and pass the cursor "
        "it returns back as `since` — that is the only way to see new messages, edits, and "
        "unsends without re-reading what you already have. To follow one thread, pass that "
        "thread's `chat_guid` to the same tool rather than re-reading it with "
        "get_chat_messages. A cursor belongs to the scope it was minted in: keep one cursor "
        "per chat plus one for the unscoped poll, and never move a cursor between scopes — "
        "doing so raises rather than silently skipping messages.\n\n"
        "Not for: email, phone/FaceTime calls, or other platforms (Slack, WhatsApp, Telegram).\n\n"
        "All sends, reactions, and read receipts are real and visible to the other person. "
        "Always confirm with the user before destructive actions (delete chat, unsend message, "
        "remove participant).\n\n"
        "This server publishes its own usage skill as an MCP resource. Read "
        "skill://bluebubbles/SKILL.md for workflows, the full tool catalog, and best "
        "practices before proceeding; skill://bluebubbles/_manifest lists its supporting "
        "reference files."
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Bundled skill
# ---------------------------------------------------------------------------

#: Ships inside the package so it travels with every install (uvx, wheel, Docker).
SKILL_DIR = Path(__file__).parent / "skills" / "bluebubbles"

try:
    mcp.add_provider(SkillProvider(SKILL_DIR))
except FileNotFoundError:  # pragma: no cover - broken/partial install
    logger.warning("Bundled bluebubbles skill not found at %s; skipping.", SKILL_DIR)


def _bb(ctx: Context) -> BlueBubblesClient:
    return ctx.lifespan_context["bb"]


def _send_private_api(ctx: Context) -> bool:
    """Whether *sends* go out over the Private API on this connection.

    Distinct from the raw ``private_api`` toggle that gates Private-API *routes*,
    which ``lifespan`` reads straight off ``/server/info``: the send path
    additionally depends on the injected helper being attached and on
    ``BLUEBUBBLES_SEND_METHOD``. Anything
    whose behaviour changes with the send method must read this one — it is the
    same value the client's ``private_api`` flag was assigned in ``lifespan``, so
    a guard here can never disagree with what the client puts on the wire.
    """
    return ctx.lifespan_context["send_private_api"]


def _canonical_service(service: str) -> Literal["iMessage", "SMS"]:
    """Canonicalize a service name to exactly ``"SMS"`` or ``"iMessage"``.

    The client compares case-sensitively against ``"SMS"`` and puts the value on
    the wire, so the casing is settled once, here. The tool parameter is a
    ``Literal``, so the model cannot express anything else in the first place —
    this stays as the single canonical point for every other caller, and because a
    coercing fallback is what once turned an unrecognized service into a silent
    iMessage send.
    """
    return "SMS" if service.strip().upper() == "SMS" else "iMessage"


def _sender_filter(ctx: Context, address: str | None) -> tuple[str | None, bool]:
    """Split a `from_address` into (handle_address, from_me).

    'me' cannot be expressed as a handle: `handle` names the OTHER party even on
    outgoing messages. Filtering on the user's own address either matches nothing
    (an email address: 0 rows against 85 outgoing messages in the same window) or
    matches rows the user did not send, because their own phone number *does*
    appear as a handle (540 rows live, 501 of them `isFromMe=false`). Either way
    it is the wrong column, so 'me' routes to `message.is_from_me` instead.
    """
    if address is None:
        return None, False
    if address.strip().lower() == "me":
        return None, True
    return _resolve_address(ctx, address), False


def _resolve_address(ctx: Context, address: str) -> str:
    """Translate 'me' to the server's detected iMessage address."""
    if address.lower() == "me":
        me = ctx.lifespan_context.get("me")
        if not me:
            raise ValueError(
                "Cannot resolve 'me': server did not return a detected_imessage address."
            )
        return me
    return address


# ---------------------------------------------------------------------------
# Message projection
# ---------------------------------------------------------------------------

_SLIM_MSG_FIELDS = frozenset(
    {
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
)
_SLIM_HANDLE_FIELDS = frozenset({"address", "service"})
_SLIM_CHAT_FIELDS = frozenset({"guid", "displayName", "isArchived"})


def _slim_message(msg: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in msg.items() if k in _SLIM_MSG_FIELDS}
    if isinstance(out.get("handle"), dict):
        out["handle"] = {
            k: v for k, v in out["handle"].items() if k in _SLIM_HANDLE_FIELDS
        }
    if isinstance(out.get("chats"), list):
        out["chats"] = [
            {k: v for k, v in c.items() if k in _SLIM_CHAT_FIELDS} for c in out["chats"]
        ]
    return out


def _project(data: list[dict[str, Any]], extended: bool) -> list[dict[str, Any]]:
    return data if extended else [_slim_message(m) for m in data]


def _scope_guid(chat_guid: str | None) -> str | None:
    """The one normalised chat scope a call uses everywhere, or ``None`` if global.

    Normalising once matters: the cursor scope key, the two ``/message/query`` bodies
    and the chat stamped onto each row all have to agree, and ``iMessage;-;X`` and
    ``any;-;X`` are the same chat.
    """
    if not chat_guid or not chat_guid.strip():
        return None
    return BlueBubblesClient._normalize_guid(chat_guid.strip())


def _is_encoded_cursor(since: str | None) -> bool:
    """True when ``since`` is a token this server minted, not a human-typed seed."""
    if not isinstance(since, str):
        return False
    return since.strip().partition("|")[0] in CURSOR_VERSIONS


async def _scope_chat(bb: BlueBubblesClient, chat_guid: str) -> dict[str, Any]:
    """Read the scoped chat, slimmed to the fields a row's ``chats`` entry carries.

    One read serves two purposes: it proves the scope exists (see
    :func:`_require_scope_chat`) and it is the label every row of that poll gets,
    which is why it keeps ``displayName`` rather than just the GUID.
    """
    chat = await bb.get_chat(chat_guid)
    slim = {k: v for k, v in chat.items() if k in _SLIM_CHAT_FIELDS} if chat else {}
    slim.setdefault("guid", chat_guid)
    return slim


async def _require_scope_chat(bb: BlueBubblesClient, chat_guid: str) -> dict[str, Any]:
    """Fail loudly on a chat scope that would otherwise poll empty forever.

    Unlike ``GET /chat/{guid}``, ``/message/query`` answers an unknown ``chatGuid``
    with ``[]`` rather than a 404 — a healthy-looking poll that returns nothing for
    the rest of the session. That is exactly the plausible empty result
    :meth:`Cursor.parse` refuses to invent, so the scope gets the same treatment.

    Only a *not found* answer means the GUID is wrong, and 404 is the whole of that
    answer: verified live, an unknown-but-well-formed GUID and outright garbage both
    come back 404 "Chat does not exist!". A connection failure or a gateway timeout —
    the Mac asleep, BlueBubbles restarting — says nothing about the GUID, so it
    propagates unchanged rather than being relabelled as one: telling the model to go
    re-resolve a GUID that was correct all along is the exact misdirection this tool's
    advice exists to remove.

    Called once per polling session (a resumed scoped cursor was minted by a call that
    already checked), so a loop pays for this at most once.
    """
    try:
        return await _scope_chat(bb, chat_guid)
    except BlueBubblesError as exc:
        if exc.status_code != 404:
            raise
        raise ValueError(
            f"Could not read chat {chat_guid!r} ({exc}). A scoped poll cannot detect "
            "this itself: the message query answers an unknown chat with an empty "
            "result rather than an error, so it would keep looking healthy and return "
            "nothing. Get the chat GUID from list_chats or search_messages and pass "
            "the `guid` they report."
        ) from exc


async def _attach_chats(
    bb: BlueBubblesClient, rows: list[dict[str, Any]], notes: list[str]
) -> int:
    """Fill in each row's ``chats`` with a second pass. Returns the "no chat" count.

    The primary query cannot ask for chats: ``with: ["chats"]`` is an INNER join that
    silently drops every message belonging to no chat row — 4.70% over a 30-day window
    on a live server, and the dropped rows are exactly the SMS shortcodes and 2FA
    senders someone polling would most want to see. Resolving separately means those
    rows arrive with ``chats: []`` instead of vanishing.
    """
    if not rows:
        return 0
    try:
        resolved = await bb.resolve_message_chats([r.get("guid") or "" for r in rows])
    except Exception as exc:  # noqa: BLE001 - a failed lookup must not cost the cursor
        # Returning no cursor would make the caller replay the same window forever,
        # which is far worse than missing chat labels for one poll.
        for row in rows:
            row["chats"] = row.get("chats") or []
        notes.append(
            f"Chat attribution unavailable this poll ({type(exc).__name__}); "
            "messages are complete but unlabelled."
        )
        return 0
    missing = 0
    for row in rows:
        chats = resolved.get(row.get("guid") or "")
        row["chats"] = chats or []
        if not chats:
            missing += 1
    return missing


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def get_my_address(ctx: Context) -> dict[str, Any]:
    """Get the iMessage addresses that identify the user who owns this device.

    An Apple account can receive on several addresses at once — typically a phone
    number plus one or more emails. All of them are the same person.

    Do NOT pass these values as `from_address` to search/fetch tools. That filter
    matches the OTHER party in a conversation, so filtering on your own address
    returns nothing (or, if you have a chat with yourself, only that thread).
    To find messages you sent, pass the literal string 'me' instead.

    Returns:
        address: The primary address — Apple's currently active sending alias.
        addresses: Every address belonging to the user, primary first.
        name: The account holder's display name, when the server reports one.
        source: Where this came from — 'icloud_account' (Private API, complete),
                'server_info' (detected address only, may be missing aliases), or
                'override' (BLUEBUBBLES_MY_ADDRESS).
    """
    identity = ctx.lifespan_context.get("identity") or {}
    if not identity.get("address"):
        raise ValueError(
            "Could not determine user address: server did not return detected_imessage."
        )
    return identity


@mcp.tool(annotations=READ_ONLY)
async def get_server_info(ctx: Context) -> dict[str, Any]:
    """Get BlueBubbles server info and health status.

    Returns version, OS, and configuration details for the BlueBubbles iMessage/SMS bridge server.
    """
    return await _bb(ctx).server_info()


@mcp.tool(annotations=READ_ONLY)
async def ping(ctx: Context) -> Any:
    """Ping the BlueBubbles server to check connectivity.

    Verifies the iMessage/SMS bridge is reachable and responding.
    """
    return await _bb(ctx).ping()


# ---------------------------------------------------------------------------
# Chats
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def list_chats(
    ctx: Context,
    limit: int = 25,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List iMessage and SMS text message conversations, sorted by most recent activity.

    Returns Apple iMessage and SMS chat threads from the bridged iPhone/Mac, including
    direct messages and group chats. Each chat includes the last message preview.

    Args:
        limit: Max number of chats to return (default 25).
        offset: Pagination offset.
    """
    return await _bb(ctx).list_chats(
        limit=limit, offset=offset, with_fields=["lastmessage"]
    )


@mcp.tool(annotations=READ_ONLY)
async def get_chat(ctx: Context, chat_guid: str) -> dict[str, Any]:
    """Get details for a specific iMessage or SMS chat, including participants.

    Returns full chat metadata, participant phone numbers or emails, and the last message
    for an Apple iMessage or SMS text message conversation.

    Args:
        chat_guid: The chat GUID (e.g. 'iMessage;-;+15551234567' or 'iMessage;+;chat123').
    """
    return await _bb(ctx).get_chat(
        chat_guid, with_fields=["participants", "lastmessage"]
    )


@mcp.tool(annotations=READ_ONLY)
async def get_chat_messages(
    ctx: Context,
    chat_guid: str,
    limit: int = 25,
    offset: int = 0,
    sort: str = "DESC",
    after: int | None = None,
    before: int | None = None,
    from_address: str | None = None,
    extended: bool = False,
) -> list[dict[str, Any]]:
    """Get iMessage or SMS text messages from a specific chat conversation.

    Retrieves the message history for an Apple iMessage or SMS thread, with optional
    time range filtering. Messages include sender, timestamp, text body, and attachments.

    `after` is a fixed time window, not a cursor — repeated calls re-read the same
    messages and never show edits or unsends. To follow a conversation over time, use
    get_recent_messages with its cursor, passing this `chat_guid` to scope it to this
    one thread. Use this tool for browsing or backfilling history instead.

    Args:
        chat_guid: The chat GUID.
        limit: Max messages to return (default 25).
        offset: Pagination offset.
        sort: 'ASC' or 'DESC' (default DESC = newest first).
        after: Only messages after this epoch-ms timestamp.
        before: Only messages before this epoch-ms timestamp.
        from_address: Only return messages from this sender. Pass an E.164 phone
                      number or email (e.g. '+15551234567'), or the special value
                      'me' to filter to messages sent by the user. Filters client-side
                      after fetch.
        extended: KEEP FALSE. Only set True if you have verified the compact
                  subset is missing a field you need AND get_message(guid,
                  extended=True) cannot serve the specific message instead.
                  Compact fields: guid, text, handle, isFromMe, dateCreated,
                  attachments, replyToGuid, associatedMessageGuid/Type, chats, error.
    """
    handle_address, from_me = _sender_filter(ctx, from_address)
    data = await _bb(ctx).get_chat_messages(
        chat_guid,
        limit=limit,
        offset=offset,
        sort=sort,
        after=after,
        before=before,
        handle_address=handle_address,
        from_me=from_me,
    )
    return _project(data, extended)


@mcp.tool(annotations=READ_ONLY)
async def get_recent_messages(
    ctx: Context,
    since: str | None = None,
    minutes: int | None = None,
    limit: int = 50,
    chat_guid: str | None = None,
    from_address: str | None = None,
    extended: bool = False,
) -> dict[str, Any]:
    """Get NEW and recently-changed iMessage and SMS text messages since your last check.

    This is the tool for following Apple iMessage and SMS conversations over time on the
    bridged iPhone/Mac. Call it repeatedly, passing the `cursor` from the previous
    response back as `since`, and each call returns only what is new or changed since
    then — including edits and unsends of older messages, which a plain time window
    cannot see at all. Omit `since` for a first look at a recent time window.

    To follow ONE conversation, pass its `chat_guid` here rather than re-reading the
    thread with get_chat_messages. A cursor is a time watermark for the scope it was
    minted in, so each scope keeps its own: a scoped cursor replayed on an unscoped
    call (or on a different chat) raises rather than silently skipping everything the
    other scope saw meanwhile. Start a new scope by calling without `since`.

    Do NOT poll by calling this again with `minutes`. That re-reads the same messages
    every time, wastes context on duplicates, and never surfaces edits or unsends.

    Returns a dict with:
        messages: New messages, oldest first.
        changed: Messages edited or unsent since the last check (these may be old).
        reactions: Tapbacks among the new messages, with the GUID they target.
        cursor: Pass back as `since` on the next call.
        has_more: True means there is a backlog — call again IMMEDIATELY with the new
                  cursor rather than waiting for your next poll interval.
        cursor_advanced: False means nothing was consumed; check `notes` and back off.
        counts, notes: Totals and any warnings worth surfacing. `counts.no_chat` is
                  the number of messages belonging to no chat row — SMS shortcodes and
                  2FA senders. It is structurally 0 when `chat_guid` is set, since
                  filtering on a chat GUID can only match messages that have one, so
                  do not read it as evidence there were none.

    Args:
        since: Resume token. Pass the `cursor` string from a previous response
               VERBATIM — do not parse, construct, or edit it. An ISO-8601 timestamp
               or epoch milliseconds also works to start from a specific point.
               When set, `minutes` is ignored.
        minutes: How far back to look when `since` is omitted (default 60).
        limit: Max messages per axis per call (default 50, max 999).
        chat_guid: Restrict the poll to one conversation. A cursor minted with this
                   set only works with this same chat; mixing scopes raises. An
                   unknown GUID is rejected on the first call of a scope, because the
                   underlying query answers one with an empty result, not an error.
        from_address: Only messages from this sender. Pass an E.164 phone number or
                      email (e.g. '+15551234567'), or 'me' for the user's own messages.
                      Server-side filter.
        extended: KEEP FALSE. Only set True if you have verified the compact
                  subset is missing a field you need AND get_message(guid,
                  extended=True) cannot serve the specific message instead.
    """
    bb = _bb(ctx)
    now_ms = int(time.time() * 1000)
    page = max(1, min(int(limit), MAX_PAGE))
    notes: list[str] = []

    scope = _scope_guid(chat_guid)

    if since is not None:
        cursor = Cursor.parse(since, now_ms=now_ms, expect_scope=scope)
        if minutes is not None:
            notes.append("`minutes` was ignored because `since` was provided.")
    else:
        window = 60 if minutes is None else max(1, int(minutes))
        cursor = Cursor.seed(now_ms - window * 60_000)

    # Seeds are scope-neutral, so this is the first call of a scope whenever `since`
    # is not one of our own tokens — the only moment the scope has not been checked.
    scope_chat: dict[str, Any] | None = None
    if scope is not None and not _is_encoded_cursor(since):
        scope_chat = await _require_scope_chat(bb, scope)

    handle_address, from_me = _sender_filter(ctx, from_address)

    async def fetch_created(page_size: int) -> list[dict[str, Any]]:
        return await bb.query_created_since(
            since_ms=cursor.created_ms,
            limit=page_size,
            chat_guid=scope,
            handle_address=handle_address,
            from_me=from_me,
        )

    async def fetch_changed(page_size: int) -> list[dict[str, Any]]:
        return await bb.query_changed_since(
            since_ms=cursor.changed_ms,
            limit=page_size,
            chat_guid=scope,
            handle_address=handle_address,
            from_me=from_me,
        )

    created_raw, changed_raw = await asyncio.gather(
        fetch_created(page + 1), fetch_changed(page + 1)
    )

    created = advance_created(
        created_raw, prev_ms=cursor.created_ms, limit=page, now_ms=now_ms
    )

    # A truncated page sitting entirely inside one millisecond cannot advance without
    # either skipping rows or re-fetching the millisecond. Retry once at the largest
    # page the server allows so the walk keeps moving.
    if created.stalled_ms is not None and page < MAX_PAGE:
        created = advance_created(
            await fetch_created(MAX_PAGE + 1),
            prev_ms=cursor.created_ms,
            limit=MAX_PAGE,
            now_ms=now_ms,
        )
    notes.extend(created.notes)
    if created.stalled_ms is not None:
        notes.append(
            f"More than {MAX_PAGE} messages share millisecond {created.stalled_ms}; "
            "the cursor is holding there so none are lost. Expect repeats."
        )

    changed_rows, changed_truncated = split_overfetch(changed_raw, page)
    if changed_truncated and page < MAX_PAGE:
        changed_rows, changed_truncated = split_overfetch(
            await fetch_changed(MAX_PAGE + 1), MAX_PAGE
        )
    if changed_truncated:
        notes.append(
            "More edits and unsends than fit in one page. The changed cursor is "
            "holding position so none are lost — expect repeats until it drains."
        )

    next_cursor, monotonic_notes = merge_monotonic(
        cursor,
        Cursor(
            created_ms=created.next_ms,
            changed_ms=advance_changed(
                changed_rows,
                prev_ms=cursor.changed_ms,
                truncated=changed_truncated,
                now_ms=now_ms,
            ),
        ),
    )
    notes.extend(monotonic_notes)

    reactions = summarize_reactions(created.rows)
    rows = [*created.rows, *changed_rows]
    if scope is None:
        no_chat = await _attach_chats(bb, rows, notes)
    else:
        # Scoped rows came back through a join on chat.guid, so the chat is already
        # known and the per-row resolve pass could only re-derive it. Skipping it
        # matters most here: this is the tool designed to run in a loop, and an empty
        # poll — the common case in one — now costs no extra request at all. It also
        # makes `no_chat` structurally 0: a chat-less message cannot match a chat GUID.
        #
        # The label still carries what a global poll's does (`displayName` included),
        # so a caller can say "new message in Family" without another lookup. The
        # scope check already fetched it on the first call of a scope; a resumed
        # cursor pays one read, and only when there is something to label.
        if rows and scope_chat is None:
            try:
                scope_chat = await _scope_chat(bb, scope)
            except Exception as exc:  # noqa: BLE001 - a label must not cost the cursor
                notes.append(
                    f"Chat details unavailable this poll ({type(exc).__name__}); "
                    "messages are complete but labelled with the GUID only."
                )
        for row in rows:
            row["chats"] = [dict(scope_chat or {"guid": scope})]
        no_chat = 0

    return {
        "messages": _project(created.rows, extended),
        "changed": _project(changed_rows, extended),
        "reactions": reactions,
        "cursor": next_cursor.encode(scope),
        "has_more": created.truncated or changed_truncated,
        "cursor_advanced": next_cursor != cursor,
        "counts": {
            "new": len(created.rows),
            "changed": len(changed_rows),
            "reactions": len(reactions),
            "no_chat": no_chat,
        },
        "stalled_ms": created.stalled_ms,
        "notes": notes,
    }


@mcp.tool(annotations=READ_ONLY)
async def get_unread_chats(
    ctx: Context,
    message_limit: int = 5,
    extended: bool = False,
) -> list[dict[str, Any]]:
    """Get all iMessage and SMS conversations with unread text messages.

    Returns Apple iMessage and SMS chats that have unread messages on the bridged
    iPhone/Mac, along with their most recent messages.

    This reflects the USER's read/unread flag, which flips when they read a message on
    their phone. It is not a record of what you have already seen: a message you
    already handled can still be unread, and one you have never seen can already be
    read. To track what is new to YOU across calls, use get_recent_messages and its
    cursor instead — with `chat_guid` once you are following one of these threads.

    Args:
        message_limit: Number of recent messages to include per unread chat (default 5).
        extended: KEEP FALSE. Only set True if you have verified the compact
                  subset is missing a field you need AND get_message(guid,
                  extended=True) cannot serve the specific message instead.
    """
    bb = _bb(ctx)
    chats = await bb.list_chats(limit=100, with_fields=["lastmessage"])
    unread = [c for c in chats if c.get("hasUnreadMessages")]

    async def fetch_chat_with_messages(chat: dict[str, Any]) -> dict[str, Any]:
        messages = await bb.get_chat_messages(chat["guid"], limit=message_limit)
        return {"chat": chat, "recent_messages": _project(messages, extended)}

    results = await asyncio.gather(*[fetch_chat_with_messages(c) for c in unread])
    return list(results)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def mark_chat_read(ctx: Context, chat_guid: str) -> str:
    """Mark an iMessage or SMS conversation as read, sending a read receipt.

    Marks the Apple iMessage or SMS thread as read on the bridged iPhone/Mac.
    The read receipt is visible to the other person in iMessage threads.

    Args:
        chat_guid: The chat GUID.
    """
    await _bb(ctx).mark_chat_read(chat_guid)
    return "Chat marked as read."


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def mark_chat_unread(ctx: Context, chat_guid: str) -> str:
    """Mark an iMessage or SMS conversation as unread.

    Args:
        chat_guid: The chat GUID.
    """
    await _bb(ctx).mark_chat_unread(chat_guid)
    return "Chat marked as unread."


@mcp.tool(annotations=SEND)
async def start_typing(ctx: Context, chat_guid: str) -> str:
    """Show a typing indicator in an iMessage chat (visible to the other person).

    Sends an Apple iMessage typing indicator to the other participants in the chat.

    Args:
        chat_guid: The chat GUID.
    """
    await _bb(ctx).start_typing(chat_guid)
    return "Typing indicator started."


@mcp.tool(annotations=SEND)
async def stop_typing(ctx: Context, chat_guid: str) -> str:
    """Stop the typing indicator in an iMessage chat.

    Args:
        chat_guid: The chat GUID.
    """
    await _bb(ctx).stop_typing(chat_guid)
    return "Typing indicator stopped."


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_chat(ctx: Context, chat_guid: str) -> str:
    """Delete an entire iMessage or SMS conversation. This is irreversible.

    Permanently deletes the Apple iMessage or SMS text message thread from the
    bridged iPhone/Mac.

    Args:
        chat_guid: The chat GUID to delete.
    """
    await _bb(ctx).delete_chat(chat_guid)
    return "Chat deleted."


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@mcp.tool(annotations=SEND)
async def send_message(
    ctx: Context,
    chat_guid: str,
    message: str,
    reply_to_guid: str | None = None,
) -> dict[str, Any]:
    """Send an iMessage or SMS text message to an existing conversation.

    Sends an Apple iMessage or SMS text to the specified chat thread on the
    bridged iPhone/Mac.

    Args:
        chat_guid: The chat GUID to send to.
        message: The message text.
        reply_to_guid: Message GUID to reply to, creating a thread.
    """
    if reply_to_guid and not _send_private_api(ctx):
        raise ValueError(
            "Threaded replies require the BlueBubbles Private API send path, which is not "
            "active on this connection (the server's Private API is off, its helper is "
            "disconnected, or BLUEBUBBLES_SEND_METHOD pins sends to AppleScript)."
        )
    return await _bb(ctx).send_message(chat_guid, message, reply_to_guid=reply_to_guid)


@mcp.tool(annotations=SEND)
async def send_message_to_address(
    ctx: Context,
    address: str,
    message: str,
    service: Literal["iMessage", "SMS"] = "iMessage",
) -> dict[str, Any]:
    """Send an iMessage or SMS text message to a phone number or email address.

    Sends an Apple iMessage or SMS text to a mobile phone number or email, creating
    a new chat thread if one does not already exist on the bridged iPhone/Mac.

    The returned object differs by service, so read it accordingly:

    - service='iMessage' (the default) returns the **sent message**: `guid` is a
      message GUID and `text` is what was sent. `error` is 0 on success.
    - service='SMS' returns the **chat** the message went to: `guid` is a chat GUID
      (e.g. 'any;-;+15551234567'), `text` is absent or null, and the message itself
      is not included. A returned chat means the send was accepted.

    Args:
        address: Phone number (e.g. '+15551234567') or email address.
        message: The message text.
        service: 'iMessage' or 'SMS' (default iMessage). No other value exists —
                 there is no 'SMS/MMS' or 'text'; SMS covers both.
    """
    service = _canonical_service(service)
    if service == "SMS" and not _send_private_api(ctx):
        raise ValueError(
            "SMS service requires the BlueBubbles Private API send path, which is not "
            "active on this connection (the server's Private API is off, its helper is "
            "disconnected, or BLUEBUBBLES_SEND_METHOD pins sends to AppleScript)."
        )
    return await _bb(ctx).send_message_to_address(address, message, service=service)


@mcp.tool(annotations=SEND)
async def send_reaction(
    ctx: Context,
    chat_guid: str,
    message_guid: str,
    reaction: str,
) -> Any:
    """Send an Apple iMessage tapback reaction (like, love, laugh, etc.) to a text message.

    Sends an Apple iMessage tapback emoji reaction to a specific message in a chat.
    Only works on iMessage threads (not SMS).

    Args:
        chat_guid: The chat GUID containing the message.
        message_guid: The GUID of the message to react to.
        reaction: One of: love, like, dislike, laugh, emphasize, question.
                  Prefix with '-' to remove (e.g. '-love').
    """
    return await _bb(ctx).send_reaction(chat_guid, message_guid, reaction)


@mcp.tool(annotations=SEND)
async def edit_message(
    ctx: Context,
    message_guid: str,
    new_text: str,
) -> Any:
    """Edit a previously sent iMessage text message.

    Edits an Apple iMessage that was already sent. Only works on iMessage (not SMS)
    and only on messages sent from this device.

    Args:
        message_guid: GUID of the iMessage to edit.
        new_text: The new message text.
    """
    return await _bb(ctx).edit_message(message_guid, new_text)


@mcp.tool(annotations=DESTRUCTIVE)
async def unsend_message(ctx: Context, message_guid: str) -> Any:
    """Unsend (retract) a previously sent iMessage text message.

    Retracts an Apple iMessage that was already sent, removing it for all participants.
    Only works on iMessage (not SMS) and only on recently sent messages.

    Args:
        message_guid: GUID of the iMessage to unsend.
    """
    return await _bb(ctx).unsend_message(message_guid)


@mcp.tool(annotations=READ_ONLY)
async def search_messages(
    ctx: Context,
    query: str | None = None,
    chat_guid: str | None = None,
    limit: int = 25,
    offset: int = 0,
    after: int | None = None,
    before: int | None = None,
    from_address: str | None = None,
    extended: bool = False,
) -> list[dict[str, Any]]:
    """Search iMessage and SMS text messages by content, chat, or time range.

    Full-text search across Apple iMessage and SMS text message history on the
    bridged iPhone/Mac. Filter by keyword, conversation, or date range.

    For finding known content in history. To detect messages that are NEW since your
    last check, use get_recent_messages instead.

    Text search runs against the stored `text` column, so messages whose body lives
    only in `attributedBody` are unreachable by keyword even though their text appears
    in the response. An empty result is not proof that nothing matched.

    Args:
        query: Text to search for in message bodies.
        chat_guid: Limit search to a specific iMessage or SMS chat.
        limit: Max results (default 25).
        offset: Pagination offset.
        after: Only messages after this epoch-ms timestamp.
        before: Only messages before this epoch-ms timestamp.
        from_address: Only return messages from this sender. Pass an E.164 phone
                      number or email (e.g. '+15551234567'), or 'me' to filter
                      to messages sent by the user. Server-side filter.
        extended: KEEP FALSE. Only set True if you have verified the compact
                  subset is missing a field you need AND get_message(guid,
                  extended=True) cannot serve the specific message instead.
    """
    handle_address, from_me = _sender_filter(ctx, from_address)
    bb = _bb(ctx)
    data = await bb.search_messages(
        query=query,
        chat_guid=chat_guid,
        limit=limit,
        offset=offset,
        after=after,
        before=before,
        handle_address=handle_address,
        from_me=from_me,
    )
    await _attach_chats(bb, data, [])
    return _project(data, extended)


@mcp.tool(annotations=READ_ONLY)
async def get_message(
    ctx: Context,
    message_guid: str,
    extended: bool = False,
) -> dict[str, Any]:
    """Get a single iMessage or SMS text message by its GUID.

    Returns message details for one specific message. Prefer this over setting
    extended=True on bulk tools — fetch just the one message you need extra
    fields for rather than expanding an entire result set.

    Args:
        message_guid: The message GUID.
        extended: Set True to get all raw server fields (delivery flags, itemType,
                  groupActionType, etc.). Use when compact fields are insufficient
                  for a specific message. Default False returns the compact subset.
    """
    data = await _bb(ctx).get_message(message_guid)
    return _slim_message(data) if not extended else data


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def get_contacts(
    ctx: Context,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """Get all contacts from the Apple iPhone/Mac address book via BlueBubbles.

    Returns the contact list from the device running the BlueBubbles iMessage bridge,
    including names, phone numbers, and email addresses.

    Args:
        query: Optional filter string. Returns only contacts whose display name
               or any phone number/email contains this string (case-insensitive).
    """
    data = await _bb(ctx).get_contacts()
    if query:
        q = query.lower()
        data = [
            c
            for c in data
            if q in (c.get("displayName") or "").lower()
            or any(
                q in (addr.get("address") or "").lower()
                for addr in (c.get("phoneNumbers") or []) + (c.get("emails") or [])
            )
        ]
    return data


@mcp.tool(annotations=READ_ONLY)
async def lookup_contact(ctx: Context, addresses: list[str]) -> list[dict[str, Any]]:
    """Look up Apple iPhone contacts by phone number or email address.

    Resolves phone numbers or email addresses to contact names and details
    from the address book on the BlueBubbles-bridged iPhone/Mac.

    Args:
        addresses: List of phone numbers or email addresses to look up.
    """
    return await _bb(ctx).query_contacts(addresses)


@mcp.tool(annotations=READ_ONLY)
async def check_imessage(ctx: Context, address: str) -> Any:
    """Check if a phone number or email is registered for Apple iMessage.

    Determines whether a contact can receive iMessage (blue bubble) vs SMS only
    (green bubble) on Apple iPhone or Mac.

    Args:
        address: Phone number or email to check.
    """
    return await _bb(ctx).check_imessage_availability(address)


@mcp.tool(annotations=READ_ONLY)
async def check_facetime(ctx: Context, address: str) -> Any:
    """Check if a phone number or email is registered for Apple FaceTime.

    Args:
        address: Phone number or email to check.
    """
    return await _bb(ctx).check_facetime_availability(address)


# ---------------------------------------------------------------------------
# Group chat management
# ---------------------------------------------------------------------------


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def rename_group(ctx: Context, chat_guid: str, name: str) -> Any:
    """Rename an iMessage group chat.

    Sets a new display name for an Apple iMessage group text message thread.

    Args:
        chat_guid: The iMessage group chat GUID.
        name: New display name for the group.
    """
    return await _bb(ctx).rename_group(chat_guid, name)


@mcp.tool(annotations=SEND)
async def add_participant(ctx: Context, chat_guid: str, address: str) -> Any:
    """Add a participant to an iMessage group chat.

    Adds a contact by phone number or email to an Apple iMessage group text thread.

    Args:
        chat_guid: The iMessage group chat GUID.
        address: Phone number or email of the person to add.
    """
    return await _bb(ctx).add_participant(chat_guid, address)


@mcp.tool(annotations=DESTRUCTIVE)
async def remove_participant(ctx: Context, chat_guid: str, address: str) -> Any:
    """Remove a participant from an iMessage group chat.

    Removes a contact from an Apple iMessage group text thread by phone number or email.

    Args:
        chat_guid: The iMessage group chat GUID.
        address: Phone number or email of the person to remove.
    """
    return await _bb(ctx).remove_participant(chat_guid, address)


@mcp.tool(annotations=DESTRUCTIVE)
async def leave_chat(ctx: Context, chat_guid: str) -> str:
    """Leave an iMessage group chat.

    Exits an Apple iMessage group text message thread.

    Args:
        chat_guid: The iMessage group chat GUID to leave.
    """
    await _bb(ctx).leave_chat(chat_guid)
    return "Left the group chat."


# ---------------------------------------------------------------------------
# Scheduled messages
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def list_scheduled_messages(ctx: Context) -> list[dict[str, Any]]:
    """List all scheduled future iMessage and SMS text messages.

    Returns every scheduled record the BlueBubbles iMessage/SMS bridge holds — which
    is not the same as messages still waiting to go out. Read `status` on each one:
    'pending' is queued, 'in-progress' is firing, 'complete' was sent, and 'error'
    means the send already FAILED, with the reason in `error` and the attempt time in
    `sentAt`. The bridge announces such a failure only as a live event nobody here is
    listening for, so this tool is the only way to discover one. Check it before
    telling the user a scheduled message went out.
    """
    return await _bb(ctx).list_scheduled_messages()


@mcp.tool(annotations=SEND)
async def schedule_message(
    ctx: Context,
    chat_guid: str,
    message: str,
    scheduled_for: int,
) -> dict[str, Any]:
    """Schedule an iMessage or SMS text message to be sent at a future time.

    Queues an Apple iMessage or SMS text to be delivered automatically at the
    specified time via the BlueBubbles bridge.

    Scheduling successfully is NOT delivery. The send method is frozen into the
    record now and replayed verbatim whenever it fires — the bridge cannot re-decide
    it later — so a message scheduled while the Private API is active fails hard,
    with no fallback and no retry, if the injected helper has dropped by then
    (Messages.app restarting is enough). Nothing announces that at the time. Confirm
    with list_scheduled_messages, where the record turns up as status 'error'.

    Args:
        chat_guid: The iMessage or SMS chat GUID to send to.
        message: The message text.
        scheduled_for: When to send, as epoch milliseconds.
    """
    return await _bb(ctx).create_scheduled_message(chat_guid, message, scheduled_for)


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_scheduled_message(ctx: Context, schedule_id: int) -> str:
    """Cancel and delete a scheduled iMessage or SMS text message.

    Args:
        schedule_id: The ID of the scheduled message to cancel.
    """
    await _bb(ctx).delete_scheduled_message(schedule_id)
    return "Scheduled message deleted."


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def get_attachment_info(ctx: Context, attachment_guid: str) -> dict[str, Any]:
    """Get metadata for an iMessage or SMS attachment (photo, video, file, etc.).

    Returns filename, MIME type, and size for a media file or document attached
    to an Apple iMessage or SMS text message.

    Args:
        attachment_guid: The attachment GUID.
    """
    return await _bb(ctx).get_attachment(attachment_guid)


@mcp.tool(annotations=READ_ONLY)
async def download_attachment(ctx: Context, attachment_guid: str) -> Any:
    """Download a photo, video, or file attachment from an iMessage or SMS text message.

    Retrieves the binary content of a media file or document attached to an Apple
    iMessage or SMS text message. Images are returned as inline image data;
    other files are returned as base64-encoded data with metadata.

    Args:
        attachment_guid: The attachment GUID.
    """
    data = await _bb(ctx).download_attachment(attachment_guid)
    meta = await _bb(ctx).get_attachment(attachment_guid)
    mime_type: str = meta.get("mimeType") or "application/octet-stream"
    if mime_type.startswith("image/"):
        fmt = mime_type.split("/", 1)[1]
        return Image(data=data, format=fmt)
    return {
        "filename": meta.get("transferName"),
        "mime_type": mime_type,
        "size_bytes": len(data),
        "data_base64": base64.b64encode(data).decode(),
    }


@mcp.tool(annotations=SEND)
async def send_attachment(
    ctx: Context,
    chat_guid: str,
    data_base64: str,
    filename: str,
    mime_type: str = "application/octet-stream",
) -> dict[str, Any]:
    """Send a photo, video, or file attachment via iMessage or SMS.

    Sends a media file or document to an Apple iMessage or SMS chat thread
    on the bridged iPhone/Mac.

    Args:
        chat_guid: The iMessage or SMS chat GUID to send to.
        data_base64: The file contents as a base64-encoded string.
        filename: The filename (e.g. 'photo.jpg').
        mime_type: MIME type (e.g. 'image/jpeg'). Defaults to 'application/octet-stream'.
    """
    file_data = base64.b64decode(data_base64)
    return await _bb(ctx).send_attachment(chat_guid, file_data, filename, mime_type)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


app = typer.Typer()


@app.command()
def main(
    mask_errors: bool = typer.Option(
        True,
        "--mask-errors/--no-mask-errors",
        envvar="BLUEBUBBLES_MASK_ERRORS",
        help="Hide internal error details from MCP clients. Default: enabled.",
    ),
    my_address: str | None = typer.Option(
        None,
        "--my-address",
        envvar="BLUEBUBBLES_MY_ADDRESS",
        help="Override the detected iMessage address for the local user (e.g. '+15551234567'). "
        "Defaults to the address reported by the BlueBubbles server.",
    ),
) -> None:
    mcp.mask_error_details = mask_errors  # type: ignore[attr-defined]
    mcp._my_address_override = my_address  # type: ignore[attr-defined]
    # No banner: it is an ASCII art box plus a PyPI update nag written to
    # stderr, which is noise in an MCP client's logs. Suppressing it also
    # skips the version-check HTTP call on every startup.
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    app()
