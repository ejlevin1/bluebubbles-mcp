---
name: bluebubbles
description: Send, read, search, and manage iMessage/SMS via BlueBubbles. Use when the user wants to text, check messages, search history, manage group chats, or anything involving their iPhone messages.
---

# BlueBubbles iMessage / SMS

Full access to the user's Apple iMessage and SMS via the BlueBubbles MCP server.

## Rules

1. **All sends are real.** Messages are immediately delivered and visible. If intent is ambiguous ("draft a reply"), show the text and confirm before sending.
2. **Always resolve contacts.** Never show raw GUIDs or phone numbers to the user. Use memory first, then `lookup_contact` for unknowns. See [Contact Resolution](#contact-resolution).
3. **Always normalize phone numbers to E.164 format** before passing to any tool. Strip formatting, assume US (+1) if no country code given.
   - `555-555-1234` → `+15555551234`
   - `(555) 555-1234` → `+15555551234`
   - `+44 7911 123456` → `+447911123456`
   Never pass a raw unformatted number to a tool.
4. **Confirm destructive actions.** Always ask before: `unsend_message`, `delete_chat`, `remove_participant`, `leave_chat`.
5. **Private API tools may not be available.** `send_reaction`, `edit_message`, `unsend_message`, `start_typing`, `stop_typing`, `send_attachment`, `check_imessage`, and `check_facetime` all require the BlueBubbles Private API. If they are absent from the tool list, the server does not support them — tell the user rather than attempting to call them.
6. **iMessage vs SMS awareness.** Reactions, edit, unsend, and typing indicators only work on iMessage threads AND require Private API. SMS only supports basic send.
7. **Check memory first.** Before any messaging task, check `memory://tools/bluebubbles/` for tool patterns and `memory://user/relationships/` for known contacts.

## Contact Resolution

**Every phone number or email must be resolved to a name before showing to the user.** Non-negotiable.

### Resolution order (fastest first):

1. **Memory** — `List(memory://user/relationships/)`. If an address is already stored, use it without calling `lookup_contact`.
2. **Batch lookup** — `lookup_contact([unknown addresses])` in a single call. Never loop.
3. **Store** — save meaningful new contacts to memory for next time.

```
List(memory://user/relationships/)        → known contacts
lookup_contact([remaining unknowns])      → one batch call
Store meaningful new contacts to memory
Present with human names
```

### Resolving all conversation participants

When entering or displaying any chat, **always** resolve every participant — not just the message sender:

```
get_chat(chat_guid)                              → fetch full participant list
List(memory://user/relationships/)               → filter out already-known addresses
lookup_contact([all unknown participant addrs])  → one batch call for the rest
Present participant names, never raw addresses
```

Never skip this step for group chats — all members must be named before the conversation is shown.

### If `lookup_contact` is not directly available

If `lookup_contact` is not in your active tool set, load it via ToolCatalog before proceeding:

```
ToolCatalog(load=true, task_description="bluebubbles lookup contact resolve phone number")
```

This enables any matching tools returned in `enabledTools`. After loading, proceed with the normal batch-lookup flow. **Do not skip resolution or fall back to raw addresses** because the tool wasn't pre-loaded — always attempt to load it first.

For memory format, storage criteria, and group chat mappings — see [references/best-practices.md](references/best-practices.md).

## Core Workflows

### Check messages
```
List(memory://user/relationships/)        → recall known contacts
get_unread_chats                          → messages already included (don't re-fetch)
lookup_contact([unknown addresses only])  → batch-resolve unknowns in one call
Store meaningful new contacts to memory
Present summary → offer reply or mark_chat_read
```

### Send a text
```
Read(memory://user/relationships/<name>.md)      → get address from memory
  OR lookup_contact                              → if not in memory
send_message(chat_guid, text)                    → existing thread
send_message_to_address(address, text, service)  → new thread
```
Note: `check_imessage` requires Private API. If unavailable, default to iMessage service
and inform the user if the send fails that the address may not support iMessage.

### Search history
```
search_messages(query, chat_guid?, after?, before?)   → keyword search
get_chat_messages(chat_guid, after?, before?, sort?)  → browse a thread
```
Time filters use **epoch milliseconds**.

### Send attachment
```
send_attachment(chat_guid, data_base64, filename, mime_type)
```
Requires Private API. If `send_attachment` is not in the tool list, tell the user
attachment sending is not supported on this server.

### Schedule a message
```
schedule_message(chat_guid, message, scheduled_for)   → scheduled_for is epoch milliseconds
list_scheduled_messages                               → see pending scheduled messages
delete_scheduled_message(id)                          → cancel a scheduled message
```

## chat_guid Format

GUIDs identify conversations. The format encodes the chat type:

| Format | Meaning |
|--------|---------|
| `any;-;+15551234567` | 1:1 direct message with a phone number |
| `any;-;user@example.com` | 1:1 direct message with an email |
| `any;+;chat<hash>` | Group chat |

**Never construct GUIDs manually for group chats** — always look them up via `list_chats` or `get_chat`. For 1:1 chats, `any;-;<E.164 address>` is reliable. The `iMessage;-;` prefix is invalid for AppleScript sends — the MCP client normalizes this automatically.

## Decision Tree

```
User wants to check messages?
  → get_unread_chats (messages already included)
  → Resolve contacts: memory first, then batch lookup_contact for unknowns
  → Store any newly resolved frequent contacts to memory
  → Never call get_chat_messages unless user wants MORE history

Sending to a new address?
  → Check memory://user/relationships/ for known address
  → If not in memory: lookup_contact to resolve name
  → If check_imessage is available: use it to determine service
  → If check_imessage is NOT available: default to iMessage, inform user if send fails
  → After sending: store contact if relationship is meaningful

User mentions a contact by name?
  → Check memory://user/relationships/<name-slug>.md first
  → If not in memory: get_contacts or lookup_contact to resolve
  → Store the name→address mapping for next time
  → Never guess phone numbers

User wants to react/edit/unsend?
  → Check if send_reaction/edit_message/unsend_message is in the tool list
  → If NOT available: tell user these features require Private API, not enabled on this server
  → If available: verify thread is iMessage, not SMS
  → Confirm before unsend (irreversible)

User references a group chat by name?
  → Check memory://tools/bluebubbles/group-chats.md
  → If not in memory: list_chats to find, then store the mapping

User wants to schedule a message?
  → Normalize address to E.164, convert delivery time to epoch milliseconds
  → schedule_message(chat_guid, message, scheduled_for)
  → Confirm scheduled time back to user
```

## Memory

This skill uses two memory locations:

| Path | Contains |
|------|----------|
| `memory://user/relationships/<name>.md` | Contact info (phone, email, iMessage status) and relationship context |
| `memory://tools/bluebubbles/group-chats.md` | Group chat name → GUID mappings |
| `memory://tools/bluebubbles/<pattern>.md` | Tool quirks or workarounds discovered through use |

**Read before every messaging task. Store after meaningful discoveries.**

## References

- **[references/tools.md](references/tools.md)** — Complete tool catalog with parameters
- **[references/best-practices.md](references/best-practices.md)** — Patterns for contact resolution, search, groups, attachments
