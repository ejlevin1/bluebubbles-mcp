---
name: bluebubbles
description: Send, read, search, and manage iMessage and SMS text messages on an Apple iPhone or Mac via the BlueBubbles MCP server. Use when the user wants to text someone, check messages, read unread texts, search iMessage history, manage group chats, send attachments, schedule messages, or do anything involving Apple iMessage or SMS on their iPhone or mobile device.
---

# BlueBubbles iMessage / SMS

Provides full access to the user's Apple iMessage and SMS via the `BlueBubbles` MCP server.

## Available MCP Tools

**Reading**
- `get_unread_chats` — unread conversations with recent messages (start here when checking messages)
- `get_recent_messages` — all messages across chats in the last N minutes
- `list_chats` — all conversations sorted by recent activity
- `get_chat` — details and participants for a specific chat
- `get_chat_messages` — message history for a chat, with time range filtering
- `search_messages` — full-text search across all iMessage/SMS history
- `get_message` — single message by GUID

**Sending**
- `send_message` — send to an existing chat by GUID
- `send_message_to_address` — send to a phone number or email (creates chat if needed); set `service` to `"iMessage"` or `"SMS"`
- `send_attachment` — send a photo, video, or file (base64-encoded)
- `send_reaction` — tapback reaction: `love`, `like`, `dislike`, `laugh`, `emphasize`, `question` (prefix with `-` to remove)
- `edit_message` — edit a sent iMessage
- `unsend_message` — retract a sent iMessage (irreversible)
- `schedule_message` — queue a message for future delivery (epoch ms)

**Contacts**
- `get_contacts` — full address book
- `lookup_contact` — resolve phone numbers or emails to names
- `check_imessage` — check if an address supports iMessage (blue bubble)
- `check_facetime` — check if an address supports FaceTime

**Group chats**
- `rename_group` — set a new display name
- `add_participant` / `remove_participant` — manage members
- `leave_chat` — exit a group thread

**Chat state**
- `mark_chat_read` — send read receipt
- `mark_chat_unread` — mark as unread
- `start_typing` / `stop_typing` — typing indicator

**Scheduled messages**
- `list_scheduled_messages` — pending scheduled messages
- `delete_scheduled_message` — cancel by ID

**Attachments**
- `get_attachment_info` — metadata (filename, MIME type, size)
- `download_attachment` — retrieve as base64

**Server**
- `ping` / `get_server_info` — connectivity and health check

## Workflows

### Check for new messages
1. Call `get_unread_chats` to see what needs attention.
2. For each chat, the recent messages are already included in the response.
3. Call `mark_chat_read` after reading if appropriate.

### Send a text
- To an existing chat: use `send_message` with the chat GUID.
- To a new number: use `send_message_to_address` with the phone number and `service: "iMessage"` or `"SMS"`.
- Check `check_imessage` first if unsure whether iMessage is available.

### Find a past message
- Use `search_messages` with a keyword and optional `chat_guid` or time bounds.
- Use `get_chat_messages` with `after`/`before` epoch-ms timestamps to browse a specific thread.

## Safety Rules

- **All actions are real.** Sends, reactions, and read receipts are immediately visible to the other person.
- **Destructive actions are irreversible.** Always confirm with the user before calling `unsend_message`, `delete_chat`, `remove_participant`, or `leave_chat`.
- **iMessage vs SMS.** Editing, unsending, and tapback reactions only work on iMessage threads, not SMS.
