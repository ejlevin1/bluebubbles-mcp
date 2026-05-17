# Best Practices

## Contact Resolution — Memory Storage

### When to store a contact

Store when:
- User sends/receives messages with this contact (active relationship)
- User refers to someone by name and you resolve their address
- Contact appears in multiple conversations (frequent participant)

Do NOT store:
- One-off spam or unknown numbers
- Contacts the user never mentions or interacts with meaningfully

### Memory path and format

```
memory://user/relationships/<name-slug>.md
```

Example: `memory://user/relationships/john-smith.md`

```markdown
# John Smith

## Contact Info
- Phone: +15551234567
- Email: john@example.com
- iMessage: yes  ← omit or set "unknown" if check_imessage is not available

## Context
- <relationship: colleague, friend, family, etc.>
- <relevant context: "prefers text over email", "user's manager">
```

### Group chat mappings

Store group chats the user references by name:

```
memory://tools/bluebubbles/group-chats.md
```

Content: list of display names → chat GUIDs so you don't need to `list_chats` each time.

### Resolve ALL participants, not just the sender

When you fetch a chat, resolve every participant address — not only whoever last messaged:

```
get_chat(chat_guid)
  → participants[].address  (all members)
  → filter against memory://user/relationships/
  → lookup_contact([remaining unknown addresses])  ← one batch call
```

This is mandatory for group chats. Never display a participant list with raw phone numbers or GUIDs.

### Batch lookup reminder

`lookup_contact` accepts a list — always batch. Never call once per address:

```
lookup_contact(addresses=["+15551234567", "+15559876543", "bob@example.com"])
```

If no match found, fall back to formatted phone/email — never show a raw GUID.

### If `lookup_contact` is not in the active tool set

Load it via ToolCatalog before giving up:

```
ToolCatalog(load=true, task_description="bluebubbles lookup contact resolve phone number")
```

ToolCatalog enables matching tools and returns their names in `enabledTools`. Once loaded, proceed with the normal batch-lookup. Never skip contact resolution because the tool wasn't pre-loaded.

## Unread Chats

`get_unread_chats` already includes recent messages. Do **not** call `get_chat_messages` for each chat unless the user asks for more history.

## Time-Based Search

`after` and `before` parameters are **epoch milliseconds**:
- Last 24h: `current_epoch_ms - 86400000`
- Specific date: convert to epoch ms

Combine `search_messages(query=...)` with `chat_guid` to scope to one thread.

## Attachments

- Use `get_attachment_info` first (filename, MIME, size)
- Only call `download_attachment` if user actually needs the content — it returns full base64 and is heavy

## Group Chat Changes

Before adding/removing participants:
1. `get_chat(chat_guid)` to see current membership
2. Present current list to user
3. Confirm the change

## Sending Messages — Private API vs AppleScript

BlueBubbles has two send methods: **Private API** (requires a helper bundle) and **AppleScript** (always available). Check `get_server_info` → `private_api` to know which is active, or simply check whether Private API tools appear in the tool list.

### What always works (AppleScript)

- `send_message` — basic text send to an existing chat GUID
- `send_message_to_address` — basic text send to a phone number or email
- `schedule_message` — schedule future delivery

### What requires Private API

- Threaded replies (`reply_to_guid` on `send_message`)
- `send_reaction` — tapbacks
- `edit_message` — editing
- `unsend_message` — retraction
- `start_typing` / `stop_typing` — typing indicators
- `send_attachment` — file/photo/video sends
- `check_imessage` / `check_facetime` — availability checks

These tools are **automatically removed from the tool list** when Private API is not enabled. If they are absent, the server does not support them.

### If a send fails with a 500 error

1. **Do not retry** — it will fail again the same way
2. Tell the user the feature requires the Private API, which is not enabled on this server
3. Offer to send a plain text version if a threaded reply or reaction was intended

### chat_guid format for sends

AppleScript requires the `any;-;<address>` GUID format for 1:1 chats. The `iMessage;-;<address>` prefix will cause the server to hang. The MCP client normalizes this automatically, but if constructing GUIDs manually always use `any;-;`.

## iMessage-Only Features

These fail silently on SMS threads AND require Private API:
- `send_reaction` — tapbacks
- `edit_message` — editing
- `unsend_message` — retraction
- `start_typing` / `stop_typing` — indicators

If unsure whether a thread is iMessage and `check_imessage` is available: call it to verify.
If `check_imessage` is not in the tool list (Private API disabled): assume iMessage for blue-bubble contacts and SMS for others, or ask the user.

## Destructive Actions — Always Confirm

| Tool | Impact |
|------|--------|
| `unsend_message` | Permanent retraction; other person sees it |
| `delete_chat` | Deletes entire thread locally |
| `remove_participant` | Removes from group; visible to everyone |
| `leave_chat` | Exits thread; cannot rejoin without invite |
