---
name: bluebubbles
description: Send, read, search, and manage iMessage/SMS via BlueBubbles. Use when the user wants to text, check messages, search history, manage group chats, or anything involving their iPhone messages.
---

# BlueBubbles iMessage / SMS

Full access to the user's Apple iMessage and SMS via the BlueBubbles MCP server.

## Rules

1. **All sends are real.** Messages are immediately delivered and visible. If intent is ambiguous ("draft a reply"), show the text and confirm before sending.
2. **Always resolve contacts.** Never show raw GUIDs or phone numbers to the user. Use memory first, then `lookup_contact` for unknowns. See [Contact Resolution](#contact-resolution).
3. **Confirm destructive actions.** Always ask before: `unsend_message`, `delete_chat`, `remove_participant`, `leave_chat`.
4. **iMessage vs SMS awareness.** Reactions, edit, unsend, and typing indicators only work on iMessage (blue bubble) threads — they silently fail on SMS.
5. **Check memory first.** Before any messaging task, check `memory://tools/bluebubbles/` for tool patterns and `memory://user/relationships/` for known contacts.

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
Read(memory://user/relationships/<name>.md)      → get address + iMessage status from memory
  OR lookup_contact + check_imessage             → if not in memory
send_message(chat_guid, text)                    → existing thread
send_message_to_address(address, text, service)  → new thread
```

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

## Decision Tree

```
User wants to check messages?
  → get_unread_chats (messages already included)
  → Resolve contacts: memory first, then batch lookup_contact for unknowns
  → Store any newly resolved frequent contacts to memory
  → Never call get_chat_messages unless user wants MORE history

Sending to a new address?
  → Check memory://user/relationships/ for known address
  → If not in memory: check_imessage to determine service
  → Inform user if falling back to SMS (green bubble)
  → After sending: store contact if relationship is meaningful

User mentions a contact by name?
  → Check memory://user/relationships/<name-slug>.md first
  → If not in memory: get_contacts or lookup_contact to resolve
  → Store the name→address mapping for next time
  → Never guess phone numbers

User wants to react/edit/unsend?
  → Verify thread is iMessage, not SMS
  → Confirm before unsend (irreversible)

User references a group chat by name?
  → Check memory://tools/bluebubbles/group-chats.md
  → If not in memory: list_chats to find, then store the mapping
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
