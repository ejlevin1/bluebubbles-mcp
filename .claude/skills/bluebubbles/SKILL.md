---
name: bluebubbles
description: Send, read, search, and manage iMessage and SMS text messages on an Apple iPhone or Mac via the BlueBubbles MCP server. Use when the user wants to text someone, check messages, read unread texts, search iMessage history, manage group chats, send attachments, schedule messages, or do anything involving Apple iMessage or SMS on their iPhone or mobile device.
---

# BlueBubbles iMessage / SMS

Full access to the user's Apple iMessage and SMS via the `BlueBubbles` MCP server.

For the complete tool catalog see [references/tools.md](references/tools.md).
For patterns and best practices see [references/best-practices.md](references/best-practices.md).

## Core Workflows

### Check for new messages
1. Call `get_unread_chats` — recent messages are already included, no need to fetch again.
2. Batch-resolve participant addresses with `lookup_contact` before presenting to the user.
3. Offer to reply or mark chats as read.

### Send a text
1. If sending to a new address, call `check_imessage` to determine blue bubble vs SMS.
2. Use `send_message` (existing chat GUID) or `send_message_to_address` (phone/email).
3. For ambiguous intent ("draft a reply"), show the text and confirm before sending.

### Find a message
- Keyword search: `search_messages(query=..., chat_guid=..., after=..., before=...)`
- Browse a thread: `get_chat_messages` with `after`/`before` epoch-ms bounds.

### Send an attachment
1. Base64-encode the file.
2. Call `send_attachment` with `chat_guid`, `data_base64`, `filename`, and `mime_type`.

## Safety Rules

- **All sends are real** and immediately visible to the other person.
- **Always confirm** before: `unsend_message`, `delete_chat`, `remove_participant`, `leave_chat`.
- **iMessage only**: reactions, edit, unsend, and typing indicators do not work on SMS threads.
- **Always resolve contacts** — never show raw GUIDs or phone numbers to the user; use `lookup_contact` first.
