# bluebubbles-mcp

MCP server for [BlueBubbles](https://bluebubbles.app) — access iMessage from any MCP client.

Built from scratch with no third-party MCP dependencies beyond the official [`mcp`](https://pypi.org/project/mcp/) SDK and [`httpx`](https://pypi.org/project/httpx/).

> **This project is a fork of [metaember/bluebubbles-mcp](https://github.com/metaember/bluebubbles-mcp).**
> The original implementation, architecture, and toolset were created by [@metaember](https://github.com/metaember) — huge credit to them for the excellent foundation this builds on.

## Prerequisites

- A running [BlueBubbles server](https://bluebubbles.app) with API access enabled
- Python 3.11+ **or** Docker

## Setup

### Docker (recommended)

Pull and run the pre-built image from GitHub Container Registry:

```bash
docker run --rm -i \
  -e BLUEBUBBLES_URL=https://your-bluebubbles-server \
  -e BLUEBUBBLES_PASSWORD=your-server-password \
  ghcr.io/ejlevin1/bluebubbles-mcp:latest
```

Or use docker-compose for local development (copy `.env.example` to `.env` and fill in your values):

```bash
BLUEBUBBLES_URL=https://your-bluebubbles-server \
BLUEBUBBLES_PASSWORD=your-server-password \
docker compose up
```

### uvx (no install required)

Run directly from the GitHub repo without cloning:

```bash
uvx --from git+https://github.com/ejlevin1/bluebubbles-mcp bb-mcp
```

### From source

```bash
git clone https://github.com/ejlevin1/bluebubbles-mcp.git
cd bluebubbles-mcp
just setup   # installs deps and git hooks
```

## Configuration

### uvx (Claude Code / MCP client)

```json
{
  "mcpServers": {
    "bluebubbles": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/ejlevin1/bluebubbles-mcp", "bb-mcp"],
      "env": {
        "BLUEBUBBLES_URL": "https://your-bluebubbles-server",
        "BLUEBUBBLES_PASSWORD": "your-server-password"
      }
    }
  }
}
```

### Docker (Claude Code / MCP client)

```json
{
  "mcpServers": {
    "bluebubbles": {
      "command": "docker",
      "args": ["run", "--rm", "-i",
        "-e", "BLUEBUBBLES_URL",
        "-e", "BLUEBUBBLES_PASSWORD",
        "ghcr.io/ejlevin1/bluebubbles-mcp:latest"
      ],
      "env": {
        "BLUEBUBBLES_URL": "https://your-bluebubbles-server",
        "BLUEBUBBLES_PASSWORD": "your-server-password"
      }
    }
  }
}
```

### From source (Claude Code / MCP client)

```json
{
  "mcpServers": {
    "bluebubbles": {
      "command": "uv",
      "args": ["--directory", "/path/to/bluebubbles-mcp", "run", "python", "-m", "bb_mcp.server"],
      "env": {
        "BLUEBUBBLES_URL": "https://your-bluebubbles-server",
        "BLUEBUBBLES_PASSWORD": "your-server-password"
      }
    }
  }
}
```

## Tools

Tools marked **Private API** are removed from the tool list at startup when the
BlueBubbles server reports `private_api: false`, so a client only ever sees tools that
can actually run.

| Tool | Description | Annotations |
|------|-------------|-------------|
| `ping` | Check server connectivity | read-only |
| `get_server_info` | Server info and health | read-only |
| `list_chats` | List conversations by recent activity | read-only |
| `get_chat` | Chat details with participants | read-only |
| `get_chat_messages` | Messages from a chat | read-only |
| `search_messages` | Search by text, chat, time range | read-only |
| `get_message` | Single message by GUID | read-only |
| `get_contacts` | All contacts | read-only |
| `lookup_contact` | Look up by phone/email | read-only |
| `get_my_address` | Your own iMessage identity, for filtering your messages out of a thread | read-only |
| `check_imessage` | Check iMessage registration | read-only, **Private API** |
| `check_facetime` | Check FaceTime registration | read-only, **Private API** |
| `list_scheduled_messages` | List future messages | read-only |
| `get_recent_messages` | New + changed messages since a cursor (incremental polling); optional `chat_guid` to scope to one chat | read-only |
| `get_unread_chats` | Chats with unread messages + their latest messages | read-only |
| `get_attachment_info` | Attachment metadata | read-only |
| `download_attachment` | Download attachment as base64 | read-only |
| `mark_chat_read` | Send read receipt | idempotent, open-world, **Private API** |
| `mark_chat_unread` | Mark chat unread | idempotent, **Private API** |
| `rename_group` | Rename a group chat | idempotent |
| `start_typing` | Show typing indicator | open-world, **Private API** |
| `stop_typing` | Stop typing indicator | open-world, **Private API** |
| `send_message` | Send to existing chat | open-world |
| `send_message_to_address` | Send to phone/email | open-world |
| `send_attachment` | Send a file attachment | open-world, **Private API** |
| `send_reaction` | Tapback reaction | open-world, **Private API** |
| `edit_message` | Edit a sent message | open-world, **Private API** |
| `schedule_message` | Schedule a future message | open-world |
| `add_participant` | Add to group chat | open-world |
| `unsend_message` | Retract a message | destructive, open-world, **Private API** |
| `remove_participant` | Remove from group chat | destructive, open-world |
| `leave_chat` | Leave a group chat | destructive, open-world |
| `delete_chat` | Delete a conversation | destructive, open-world |
| `delete_scheduled_message` | Cancel scheduled message | destructive, open-world |

## Skill

The server bundles an agent skill — the messaging workflows, contact-resolution rules,
E.164 normalization, and destructive-action confirmations an agent needs to use these
tools well — and publishes it over MCP as `skill://` resources. It ships inside the
package (`src/bb_mcp/skills/bluebubbles/`), so uvx, Docker, and source installs all serve
it; there is nothing to copy into `~/.claude/skills/`.

| Resource | Contents |
|----------|----------|
| `skill://bluebubbles/SKILL.md` | The skill itself: rules, contact resolution, workflows |
| `skill://bluebubbles/_manifest` | JSON listing every skill file with size and SHA-256 |
| `skill://bluebubbles/{path*}` | Template for supporting files, e.g. `skill://bluebubbles/references/tools.md` |

Supporting files are reached through the template rather than listed individually — read
the manifest to discover them. Clients that don't support MCP resources simply ignore
these; the tools work the same either way.

## Testing

```bash
just test               # unit tests, no server needed
just test-integration   # read-only against a live server (skips without .env)
just e2e                # end-to-end through the MCP tool layer, read-only
```

Integration tests skip themselves when `BLUEBUBBLES_URL` / `BLUEBUBBLES_PASSWORD` are
absent, so CI runs them as no-ops.

A small number of integration tests **send real messages** to verify the polling cursor
against a message that did not exist when the cursor was issued. They are gated on
`TEST_WRITE_GUID` (see `.env.example`) and skip entirely when it is unset. Without the
Private API a sent message cannot be unsent, so point that variable at a chat you own —
texting your own number gives you a self-thread that works well. To exclude them
explicitly regardless of environment:

```bash
uv run pytest tests/integration -m "not write"
```

### Releases and version pinning

Merges to `main` are versioned automatically. `python-semantic-release` reads the
[Conventional Commits](https://www.conventionalcommits.org/) since the last tag,
bumps `version` in `pyproject.toml`, tags `vX.Y.Z`, and cuts a GitHub Release.

| Commit type | Bump |
|---|---|
| `fix:` / `perf:` | patch — `0.5.0` → `0.5.1` |
| `feat:` | minor — `0.5.0` → `0.6.0` |
| `feat!:` or `BREAKING CHANGE:` | major — `0.5.0` → `1.0.0` |
| `docs:` / `chore:` / `test:` / `refactor:` / `ci:` | no release |

**Pin to a tag.** A branch ref is a moving target, and uv caches the built wheel —
so `git+https://github.com/ejlevin1/bluebubbles-mcp` (or `@main`) can serve a
stale build long after main has moved, and needs `--refresh` to update. A version
tag is immutable, so caching is correct rather than a hazard:

```json
{
  "mcpServers": {
    "bluebubbles": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/ejlevin1/bluebubbles-mcp@v0.5.0", "bb-mcp"],
      "env": {
        "BLUEBUBBLES_URL": "https://your-bluebubbles-server",
        "BLUEBUBBLES_PASSWORD": "your-server-password"
      }
    }
  }
}
```

Release tags also build versioned Docker images: `ghcr.io/ejlevin1/bluebubbles-mcp:0.5.0`
and `:0.5`, alongside the existing `:latest` and `:<sha>`.

### Validating a branch over uvx

`just smoke-uvx` installs the server straight from git the way an MCP client
would, then validates tools and the bundled skill over stdio — including
fetching every skill file and checking its size and SHA-256 against the manifest.

```bash
just smoke-uvx                                   # default repo, default branch
just smoke-uvx --ref my-branch                   # a branch, tag, or commit SHA
just smoke-uvx --source "git+file://$PWD" --ref my-branch   # local repo, no push needed
just smoke-uvx --from 'git+https://github.com/you/fork@sha'
```

Requires `BLUEBUBBLES_URL` and `BLUEBUBBLES_PASSWORD` (`just` loads `.env`).

## Polling for new messages

`get_recent_messages` returns a cursor. Pass it back as `since` on the next call and you
get only what is new or changed since then — no duplicates, and including edits and
unsends of messages that are already old.

```python
result = get_recent_messages(minutes=60)          # first call seeds the window

while True:
    for message in result["messages"]:
        handle(message)
    for message in result["changed"]:             # edited or unsent since last poll
        reconcile(message)

    if not result["has_more"]:
        sleep(30)                                  # caught up; wait before polling again
    result = get_recent_messages(since=result["cursor"])
```

Pass `chat_guid` to follow a single conversation. A scoped poll returns only that
chat's deltas, and is the right way to watch one thread — `get_chat_messages` cannot
show you an edit or an unsend.

Three rules matter:

- **`has_more: true` means poll again immediately.** There is a backlog and you are
  holding a partial page; waiting for the next interval just delays it.
- **Pass the cursor back verbatim.** It encodes two independent watermarks, and a
  malformed value is rejected rather than silently reinterpreted.
- **A cursor belongs to the scope that minted it.** Replaying a scoped cursor on a
  global poll, or the reverse, is rejected rather than silently skipping messages. Seed
  a new scope with `minutes` instead of reusing the other scope's cursor.

A scoped poll cannot surface messages that belong to no chat, such as some SMS
shortcodes and 2FA senders. Poll globally for those.

Do not poll by calling with `minutes` repeatedly — that re-reads the same messages every
time and can never show you an edit or an unsend, because `after` filters only on when a
message was *created*.

### Why two watermarks

A message edited a moment ago may be years old, so it sorts near the front by creation
date. A single cursor derived from such a batch lands *behind* where the poll started,
and the next poll returns the identical batch forever. The cursor therefore tracks
creation and modification separately, and `messages` and `changed` are reported apart.

### Other fields

| Field | Meaning |
|-------|---------|
| `reactions` | Tapbacks among the new messages, with the message GUID each targets |
| `cursor_advanced` | `false` means nothing was consumed — check `notes` and back off |
| `counts.no_chat` | Messages belonging to no conversation (SMS shortcodes, 2FA senders) |
| `stalled_ms` | Non-null when more messages than one page can hold share a millisecond |
| `notes` | Warnings worth surfacing; empty in the happy path |

## License

MIT
