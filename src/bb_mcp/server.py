"""MCP server for BlueBubbles iMessage bridge."""

from __future__ import annotations

import asyncio
import base64
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import typer
from fastmcp import Context, FastMCP
from fastmcp.utilities.logging import get_logger
from fastmcp.utilities.types import Image
from mcp.types import ToolAnnotations

from bb_mcp.client import BlueBubblesClient

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
]


@asynccontextmanager
async def lifespan(server: FastMCP):
    url = os.environ.get("BLUEBUBBLES_URL")
    password = os.environ.get("BLUEBUBBLES_PASSWORD")
    if not url or not password:
        raise RuntimeError(
            "BLUEBUBBLES_URL and BLUEBUBBLES_PASSWORD environment variables are required"
        )
    client = BlueBubblesClient(url, password)
    info = await client.server_info()
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
    me = override or info.get("detected_imessage") or info.get("detected_icloud")
    try:
        yield {
            "bb": client,
            "private_api": bool(info.get("private_api")),
            "me": me,
        }
    finally:
        await client.close()


mcp = FastMCP(
    "BlueBubbles",
    instructions=(
        "Use this server for anything involving iMessage or SMS text messages on the user's "
        "Apple iPhone or Mac. It bridges the user's real personal messaging via BlueBubbles.\n\n"
        "Covers: reading and sending iMessage/SMS, searching message history, managing group "
        "chats, sending attachments/photos/files, scheduling future messages, tapback reactions, "
        "editing/unsending messages, marking chats read/unread, looking up contacts, and checking "
        "iMessage or FaceTime availability for a phone number or email.\n\n"
        "Not for: email, phone/FaceTime calls, or other platforms (Slack, WhatsApp, Telegram).\n\n"
        "All sends, reactions, and read receipts are real and visible to the other person. "
        "Always confirm with the user before destructive actions (delete chat, unsend message, "
        "remove participant).\n\n"
        "If a 'bluebubbles' skill is available, load it now for workflows, the full tool "
        "catalog, and best practices before proceeding."
    ),
    lifespan=lifespan,
)


def _bb(ctx: Context) -> BlueBubblesClient:
    return ctx.lifespan_context["bb"]


def _private_api(ctx: Context) -> bool:
    return ctx.lifespan_context["private_api"]


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


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def get_my_address(ctx: Context) -> str:
    """Get the iMessage address (email or phone number) for the user who owns this device.

    Returns the address that identifies the local user — useful for filtering
    messages sent by the user (pass this value as from_address in search/fetch tools).
    Returns the BLUEBUBBLES_MY_ADDRESS override if set, otherwise the address
    detected from the BlueBubbles server.
    """
    me = ctx.lifespan_context.get("me")
    if not me:
        raise ValueError(
            "Could not determine user address: server did not return detected_imessage."
        )
    return me


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
    data = await _bb(ctx).get_chat_messages(
        chat_guid,
        limit=limit,
        offset=offset,
        sort=sort,
        after=after,
        before=before,
        handle_address=_resolve_address(ctx, from_address) if from_address else None,
    )
    return _project(data, extended)


@mcp.tool(annotations=READ_ONLY)
async def get_recent_messages(
    ctx: Context,
    minutes: int = 60,
    limit: int = 50,
    from_address: str | None = None,
    extended: bool = False,
) -> list[dict[str, Any]]:
    """Get recent iMessage and SMS text messages across all conversations within a time window.

    Fetches the latest Apple iMessage and SMS messages received on the bridged iPhone/Mac
    across all chats, useful for checking what text messages arrived recently.

    Args:
        minutes: How far back to look (default 60 minutes).
        limit: Max messages to return (default 50).
        from_address: Only return messages from this sender. Pass an E.164 phone
                      number or email (e.g. '+15551234567'), or 'me' to filter
                      to messages sent by the user. Server-side filter.
        extended: KEEP FALSE. Only set True if you have verified the compact
                  subset is missing a field you need AND get_message(guid,
                  extended=True) cannot serve the specific message instead.
    """
    after = int((time.time() - minutes * 60) * 1000)
    data = await _bb(ctx).search_messages(
        after=after,
        limit=limit,
        handle_address=_resolve_address(ctx, from_address) if from_address else None,
    )
    return _project(data, extended)


@mcp.tool(annotations=READ_ONLY)
async def get_unread_chats(
    ctx: Context,
    message_limit: int = 5,
    extended: bool = False,
) -> list[dict[str, Any]]:
    """Get all iMessage and SMS conversations with unread text messages.

    Returns Apple iMessage and SMS chats that have unread messages on the bridged
    iPhone/Mac, along with their most recent messages.

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
    if reply_to_guid and not _private_api(ctx):
        raise ValueError(
            "Threaded replies require the BlueBubbles Private API, which is not enabled on this server."
        )
    return await _bb(ctx).send_message(chat_guid, message, reply_to_guid=reply_to_guid)


@mcp.tool(annotations=SEND)
async def send_message_to_address(
    ctx: Context,
    address: str,
    message: str,
    service: str = "iMessage",
) -> dict[str, Any]:
    """Send an iMessage or SMS text message to a phone number or email address.

    Sends an Apple iMessage or SMS text to a mobile phone number or email, creating
    a new chat thread if one does not already exist on the bridged iPhone/Mac.

    Args:
        address: Phone number (e.g. '+15551234567') or email address.
        message: The message text.
        service: 'iMessage' or 'SMS' (default iMessage).
    """
    if service.upper() == "SMS" and not _private_api(ctx):
        raise ValueError(
            "SMS service requires the BlueBubbles Private API, which is not enabled on this server."
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
    data = await _bb(ctx).search_messages(
        query=query,
        chat_guid=chat_guid,
        limit=limit,
        offset=offset,
        after=after,
        before=before,
        handle_address=_resolve_address(ctx, from_address) if from_address else None,
    )
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

    Returns queued messages waiting to be sent via the BlueBubbles iMessage/SMS bridge.
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
    mcp.mask_error_details = mask_errors
    mcp._my_address_override = my_address  # type: ignore[attr-defined]
    mcp.run(transport="stdio")


if __name__ == "__main__":
    app()
