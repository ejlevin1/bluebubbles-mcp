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
- iMessage: yes

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

## iMessage-Only Features

These fail silently on SMS threads:
- `send_reaction` — tapbacks
- `edit_message` — editing
- `unsend_message` — retraction
- `start_typing` / `stop_typing` — indicators

If unsure whether a thread is iMessage: `check_imessage(address)`.

## Destructive Actions — Always Confirm

| Tool | Impact |
|------|--------|
| `unsend_message` | Permanent retraction; other person sees it |
| `delete_chat` | Deletes entire thread locally |
| `remove_participant` | Removes from group; visible to everyone |
| `leave_chat` | Exits thread; cannot rejoin without invite |
