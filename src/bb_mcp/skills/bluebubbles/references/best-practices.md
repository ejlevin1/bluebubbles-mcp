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

These are fixed windows. To watch for *new* messages instead, use the cursor — see below.

### `from_address` matches the OTHER party

`from_address` filters on who *sent* the message — the other person in the conversation,
not the user. Get the user's own addresses from `get_my_address` and never pass one of
them as `from_address`: it returns nothing (or, in a self-chat, only that thread). To
find messages the user sent, pass the literal string `'me'` instead.

## Polling for New Messages

`get_recent_messages` returns a `cursor`. Pass it back as `since` and the next call
returns only what changed since then. Add `chat_guid` to scope the poll to one
conversation instead of every chat — see [SKILL.md](../SKILL.md#getting-updates-in-a-chat).

- First call: `get_recent_messages(minutes=60)` (optionally with `chat_guid` to scope it)
- Every call after: `get_recent_messages(since=<cursor>)` (add `chat_guid` if scoped)
- `has_more: true` → poll again immediately, there is a backlog
- `messages` = newly created; `changed` = edited or unsent since last poll
- Pass the cursor back verbatim; a malformed one is rejected, not reinterpreted
- **A cursor is bound to the scope it was minted in.** A cursor from a scoped
  (`chat_guid`-bound) poll cannot be replayed as a global poll, and a global cursor
  cannot be replayed as a scoped poll — either direction raises, including against a
  *different* chat's scope. A seed (`minutes=`, an epoch, or an ISO date) is
  scope-neutral and never raises; only an already-encoded cursor token carries scope.
  Keep a separate cursor per scope you poll.
- **A scoped poll cannot surface chat-less messages** (SMS shortcodes, 2FA senders) —
  they have no `chat.guid` to match against.

Re-calling with `minutes`, `after`, or `before` is not polling — those filters match only
when a message was *created*, so they re-read the same messages every time and can never
show you an edit or an unsend.

Text search runs against the stored `text` column, so messages whose body lives only in
`attributedBody` will not match a keyword even though their text appears in the response.
An empty result is not proof of absence.

## Attachments

- Use `get_attachment_info` first (filename, MIME, size)
- Only call `download_attachment` if user actually needs the content — it returns full base64 and is heavy

## Group Chat Changes

Before adding/removing participants:
1. `get_chat(chat_guid)` to see current membership
2. Present current list to user
3. Confirm the change

## Sending Messages — Private API vs AppleScript

BlueBubbles has two send methods: **Private API** (requires a helper bundle) and
**AppleScript** (always available). The server picks one automatically, per connection,
at startup — Private API when the server reports `private_api: true` AND its helper is
connected (`helper_connected: true`); AppleScript otherwise. This is **connection-level,
not per call** — nothing in a tool call chooses the method. Check `get_server_info` →
`private_api`, or simply check whether Private API tools appear in the tool list, to know
which one is active. The server operator can override the choice with
`BLUEBUBBLES_SEND_METHOD` (`auto` default, `apple-script`, or `private-api`).

### Scheduled messages freeze the send method

`schedule_message` records the send method at the moment you schedule, not when the
message fires — BlueBubbles only auto-resolves a method it was not given, and its API
requires one. So a message scheduled while Private API was available still tries Private
API days later, and fails hard if the helper has since dropped (a Messages.app restart or
an OS update is enough). There is no fallback and no retry.

Scheduling successfully therefore does not mean the message was sent. The failure is
recorded, not lost: `list_scheduled_messages` returns `status` and `error` per record.
Check those before reporting to the user that a scheduled message went out.

### AppleScript is a fallback, not a guarantee

Do not assume `send_message` / `send_message_to_address` always succeed under
AppleScript. A real recipient returned `AppleScript error -1700` on every attempt under
AppleScript and delivered cleanly once the send used the Private API — the address and
recipient were fine the whole time; the send *method* was the problem. If Private API
tools are present in the tool list, sends already prefer that path automatically, so this
mainly matters when explaining a past failure or when `BLUEBUBBLES_SEND_METHOD` has been
forced to `apple-script`.

### What requires Private API

- Threaded replies (`reply_to_guid` on `send_message`) — now genuinely threads the reply
  when the Private API is active. Previously this field was silently dropped on every
  send, so replies never threaded regardless of the argument; that no longer applies.
- `send_reaction` — tapbacks
- `edit_message` — editing
- `unsend_message` — retraction
- `start_typing` / `stop_typing` — typing indicators
- `send_attachment` — file/photo/video sends
- `check_imessage` / `check_facetime` — availability checks

These tools are **automatically removed from the tool list** when Private API is not enabled. If they are absent, the server does not support them.

### If a send fails

**Feature calls** — `send_reaction`, `edit_message`, `unsend_message`, `start_typing` /
`stop_typing`, `send_attachment`, `check_imessage`, `check_facetime` — are stripped from
the tool list when the Private API is off, so if one is callable at all it should work; a
500 here genuinely means "this feature requires the Private API." **Do not retry.** Tell
the user the feature isn't available on this server, and offer a plain-text alternative
if a threaded reply or reaction was intended.

**Plain sends** (`send_message`, `send_message_to_address`) are a different class of
failure. The server already picked its own send method for the whole connection (see
above) — a failure here is a **send-path** problem, not a "this needs the Private API"
problem, and treating it as one can send you chasing the wrong fix. **Do not retry** — it
will fail again the same way. **Do not cycle `chat_guid`/address suffix variants**
(bare `+15551234567`, `(smsft)`, `(smsfp)`) hoping a different one works: bare, `(smsft)`,
and `(smsfp)` have been observed to fail *identically* on the same failing send, because
the suffix was never the cause. Report the failure to the user as-is rather than
reinterpreting it.

### chat_guid format for sends

The `iMessage;-;<address>` prefix causes the **AppleScript** path to hang (~25s); this is
specific to AppleScript and does not reproduce under the Private API. The MCP client
normalizes `iMessage;-;` to `any;-;` automatically for both reads and sends, so this only
matters if you construct GUIDs manually — always use `any;-;` for 1:1 chats.

### Address suffixes: `(smsft)` / `(smsfp)`

These are real address identifiers, not noise to strip. BlueBubbles appends them to
distinguish separate threads that share a phone number (for example, an iMessage thread
versus an SMS-forwarded thread to the same number). They are valid in both `chat_guid`
and `address` positions — do not strip them when filtering, searching, or resolving
contacts, and do not assume `+15551234567` and `+15551234567(smsft)` are the same
conversation. One phone number can correspond to several distinct chats.

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
