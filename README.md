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
| `check_imessage` | Check iMessage registration | read-only |
| `check_facetime` | Check FaceTime registration | read-only |
| `list_scheduled_messages` | List future messages | read-only |
| `get_recent_messages` | Messages from last N minutes across all chats | read-only |
| `get_unread_chats` | Chats with unread messages + their latest messages | read-only |
| `get_attachment_info` | Attachment metadata | read-only |
| `download_attachment` | Download attachment as base64 | read-only |
| `mark_chat_read` | Send read receipt | idempotent, open-world |
| `mark_chat_unread` | Mark chat unread (local) | idempotent |
| `rename_group` | Rename a group chat | idempotent |
| `start_typing` | Show typing indicator | open-world |
| `stop_typing` | Stop typing indicator | open-world |
| `send_message` | Send to existing chat | open-world |
| `send_message_to_address` | Send to phone/email | open-world |
| `send_attachment` | Send a file attachment | open-world |
| `send_reaction` | Tapback reaction | open-world |
| `edit_message` | Edit a sent message | open-world |
| `schedule_message` | Schedule a future message | open-world |
| `add_participant` | Add to group chat | open-world |
| `unsend_message` | Retract a message | destructive, open-world |
| `remove_participant` | Remove from group chat | destructive, open-world |
| `leave_chat` | Leave a group chat | destructive, open-world |
| `delete_chat` | Delete a conversation | destructive, open-world |
| `delete_scheduled_message` | Cancel scheduled message | destructive, open-world |

## License

MIT
