# BlueBubbles Tool Reference

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
| `send_message_to_address` | Send to a phone number or email; set `service` to `"iMessage"` or `"SMS"`; creates chat if needed |
| `send_attachment` | Send a photo, video, or file; `data_base64` is base64-encoded file content |
| `send_reaction` | Tapback: `love`, `like`, `dislike`, `laugh`, `emphasize`, `question`; prefix with `-` to remove |
| `edit_message` | Edit a sent iMessage (iMessage only, not SMS) |
| `unsend_message` | Retract a sent iMessage — irreversible, confirm first |
| `schedule_message` | Queue for future delivery; `scheduled_for` is epoch milliseconds |

## Contacts

| Tool | Purpose |
|------|---------|
| `get_contacts` | Full address book with names, phone numbers, emails |
| `lookup_contact` | Resolve one or more phone numbers / emails to contact names |
| `check_imessage` | Check whether an address supports iMessage (blue bubble vs green) |
| `check_facetime` | Check whether an address supports FaceTime |

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
| `start_typing` | Show typing indicator to other person |
| `stop_typing` | Stop typing indicator |

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
