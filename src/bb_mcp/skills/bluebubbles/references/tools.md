# BlueBubbles Tool Reference

## Private API Availability

Some tools require the BlueBubbles Private API (a helper bundle installed on the Mac).
**If Private API is not enabled, these tools will not appear in the tool list at all** —
do not attempt to call them or suggest them to the user.

| Tool | Requires Private API |
|------|---------------------|
| `send_reaction` | ✅ |
| `edit_message` | ✅ |
| `unsend_message` | ✅ |
| `start_typing` | ✅ |
| `stop_typing` | ✅ |
| `send_attachment` | ✅ |
| `check_imessage` | ✅ |
| `check_facetime` | ✅ |

If a user asks for one of these features and the tool is unavailable, tell them it requires
the BlueBubbles Private API which is not enabled on their server.

## Reading

| Tool | Purpose |
|------|---------|
| `get_unread_chats` | Unread conversations with recent messages included — start here when checking messages |
| `get_recent_messages` | All messages across all chats within last N minutes |
| `list_chats` | All conversations sorted by recent activity |
| `get_chat` | Details and participants for a specific chat |
| `get_chat_messages` | Message history for a chat; supports `after`/`before` epoch-ms filtering and `ASC`/`DESC` sort |
| `search_messages` | Full-text search across all iMessage/SMS history; filter by `chat_guid`, `after`, `before` |
| `get_message` | Single message by GUID including attachments |

## Sending

| Tool | Purpose |
|------|---------|
| `send_message` | Send to an existing chat by GUID |
| `send_message_to_address` | Send to a phone number or email; `service` defaults to `"iMessage"` (`"SMS"` requires Private API) |
| `send_attachment` | ⚠️ Private API — Send a photo, video, or file; `data_base64` is base64-encoded file content |
| `send_reaction` | ⚠️ Private API — Tapback: `love`, `like`, `dislike`, `laugh`, `emphasize`, `question`; prefix with `-` to remove |
| `edit_message` | ⚠️ Private API — Edit a sent iMessage (iMessage only, not SMS) |
| `unsend_message` | ⚠️ Private API — Retract a sent iMessage — irreversible, confirm first |
| `schedule_message` | Queue for future delivery; `scheduled_for` is epoch milliseconds |

## Contacts

| Tool | Purpose |
|------|---------|
| `get_contacts` | Full address book with names, phone numbers, emails |
| `lookup_contact` | Resolve one or more phone numbers / emails to contact names — returns only address book matches; unrecognized numbers are silently omitted, fall back to formatted phone |
| `check_imessage` | ⚠️ Private API — Check whether an address supports iMessage (blue bubble vs green) |
| `check_facetime` | ⚠️ Private API — Check whether an address supports FaceTime |

## Group Chats

| Tool | Purpose |
|------|---------|
| `rename_group` | Set a new display name |
| `add_participant` | Add a contact by phone/email |
| `remove_participant` | Remove a contact — confirm first |
| `leave_chat` | Exit the group thread — confirm first |

## Chat State

| Tool | Purpose |
|------|---------|
| `mark_chat_read` | Send read receipt (visible to other person) |
| `mark_chat_unread` | Mark as unread locally |
| `start_typing` | ⚠️ Private API — Show typing indicator to other person |
| `stop_typing` | ⚠️ Private API — Stop typing indicator |

## Scheduled Messages

| Tool | Purpose |
|------|---------|
| `list_scheduled_messages` | All pending scheduled messages |
| `delete_scheduled_message` | Cancel a scheduled message by ID |

## Attachments

| Tool | Purpose |
|------|---------|
| `get_attachment_info` | Metadata: filename, MIME type, size |
| `download_attachment` | Retrieve file content as base64 |

## Server

| Tool | Purpose |
|------|---------|
| `ping` | Check connectivity |
| `get_server_info` | Version, OS, configuration |
