# BlueBubbles Best Practices

## Always Enrich Contacts Before Presenting Conversations

When listing or summarizing chats or messages for the user, raw GUIDs and phone numbers are not helpful. Resolve them to real names first.

**Pattern:**
1. Fetch chats or messages.
2. Collect all participant addresses (phone numbers or emails) from the results.
3. Call `lookup_contact` with the full list in one batch call.
4. Substitute names into the output before showing the user.

```
# Instead of: "iMessage;-;+15551234567 sent you a message"
# Show:        "John Smith sent you a message"
```

If `lookup_contact` returns no match, fall back to the formatted phone number or email — never show a raw GUID.

---

## Check iMessage Availability Before Sending

Before sending to a new address, call `check_imessage` to determine whether the contact supports iMessage. This avoids accidentally sending SMS when the user expects a blue bubble, or vice versa.

**Pattern:**
1. Call `check_imessage(address)`.
2. If `true` → use `service: "iMessage"`.
3. If `false` → use `service: "SMS"`, and inform the user it will be a green bubble SMS.
4. If the user explicitly specifies SMS or iMessage, respect that choice without checking.

---

## Batch Contact Lookups

`lookup_contact` accepts a list of addresses. Always batch — never call it once per contact in a loop.

```
# Good: one call for all participants
lookup_contact(addresses=["+15551234567", "+15559876543", "bob@example.com"])

# Bad: three separate calls
lookup_contact(addresses=["+15551234567"])
lookup_contact(addresses=["+15559876543"])
lookup_contact(addresses=["bob@example.com"])
```

---

## Unread Chats Workflow

`get_unread_chats` already includes recent messages — do not call `get_chat_messages` again for each chat unless you need more history.

**Pattern:**
1. Call `get_unread_chats(message_limit=5)`.
2. Batch-resolve all participant addresses with `lookup_contact`.
3. Summarise each unread thread using the already-included messages.
4. Ask the user if they want to reply or mark any chats as read.

---

## Searching Message History

For time-based lookups, use epoch-millisecond timestamps with `search_messages` or `get_chat_messages`.

```
# Last 24 hours
after = int((time.time() - 86400) * 1000)

# Specific date range
after = int(datetime(2024, 1, 1).timestamp() * 1000)
before = int(datetime(2024, 1, 31).timestamp() * 1000)
```

For keyword searches, use `search_messages(query="keyword")`. Combine with `chat_guid` to scope to one thread.

---

## Presenting Attachments

When a message contains attachments, call `get_attachment_info` first to get the filename and MIME type. Only call `download_attachment` if the user actually needs the file content — it returns base64 data and is bandwidth-heavy.

---

## Group Chat Management

Before adding or removing participants, call `get_chat` with `with_fields=["participants"]` to confirm the current membership. Present the current list to the user before making changes.

---

## Safety Checklist for Destructive Actions

Before calling any of these, confirm with the user:

| Tool | Risk |
|------|------|
| `unsend_message` | Permanent; other person sees it retracted |
| `delete_chat` | Deletes entire thread locally |
| `remove_participant` | Removes person from group; visible to everyone |
| `leave_chat` | Exits thread; cannot rejoin without being re-added |

For `send_message` and `send_message_to_address`, if the user's intent is ambiguous (e.g. "draft a reply"), show the message text and ask for confirmation before sending.

---

## iMessage-Only Features

These tools only work on iMessage threads (blue bubble). They will fail or have no effect on SMS:

- `send_reaction` — tapbacks
- `edit_message` — message editing
- `unsend_message` — message retraction
- `start_typing` / `stop_typing` — typing indicators

Use `check_imessage` to verify before attempting these on an unfamiliar contact.
