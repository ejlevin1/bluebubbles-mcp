#!/usr/bin/env python3
"""Integration test: validate that the tool list matches the server's capabilities.

Spawns the MCP server locally and checks:
  - Core read/write tools are always present
  - Private API tools are present only when private_api=true on the server
  - No unexpected tools appear

Usage:
    uv run scripts/validate_tools.py
    uv run scripts/validate_tools.py --send-message "+15551234567"  # or set BLUEBUBBLES_MY_ADDRESS
    BLUEBUBBLES_URL=http://... BLUEBUBBLES_PASSWORD=... uv run scripts/validate_tools.py
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from typing import Optional

import typer

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ALWAYS_PRESENT = [
    "ping",
    "get_server_info",
    "get_my_address",
    "list_chats",
    "get_chat",
    "get_chat_messages",
    "get_recent_messages",
    "get_unread_chats",
    "search_messages",
    "get_message",
    "send_message",
    "send_message_to_address",
    "schedule_message",
    "mark_chat_read",
    "mark_chat_unread",
    "get_contacts",
    "lookup_contact",
    "rename_group",
    "add_participant",
    "remove_participant",
    "leave_chat",
    "list_scheduled_messages",
    "delete_scheduled_message",
    "get_attachment_info",
    "download_attachment",
    "delete_chat",
]

PRIVATE_API_ONLY = [
    "send_reaction",
    "edit_message",
    "unsend_message",
    "start_typing",
    "stop_typing",
    "send_attachment",
    "check_imessage",
    "check_facetime",
]


def _load_dotenv() -> None:
    """Load .env from the project root into os.environ if vars are missing."""
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def get_server_private_api_status(url: str, password: str) -> bool:
    """Query /server/info directly to get the ground-truth private_api flag."""
    req_url = f"{url.rstrip('/')}/api/v1/server/info?password={password}"
    with urllib.request.urlopen(req_url, timeout=10) as resp:
        body = json.loads(resp.read())
    return bool(body.get("data", {}).get("private_api", False))


def check(condition: bool, label: str) -> bool:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    return condition


app = typer.Typer()


@app.command()
def main(
    send_message: Optional[str] = typer.Option(
        None,
        "--send-message",
        envvar="BLUEBUBBLES_MY_ADDRESS",
        metavar="ADDRESS",
        help="After validation, send a test message to this address via send_message_to_address. "
        "Defaults to BLUEBUBBLES_MY_ADDRESS if set.",
    ),
    url: Optional[str] = typer.Option(
        None,
        "--url",
        envvar="BLUEBUBBLES_URL",
        help="BlueBubbles server URL.",
    ),
    password: Optional[str] = typer.Option(
        None,
        "--password",
        envvar="BLUEBUBBLES_PASSWORD",
        help="BlueBubbles server password.",
    ),
) -> None:
    """Validate that the BlueBubbles MCP tool list matches server capabilities."""
    # Fall back to .env for any values not already set
    if not url or not password:
        _load_dotenv()
        url = url or os.environ.get("BLUEBUBBLES_URL")
        password = password or os.environ.get("BLUEBUBBLES_PASSWORD")

    if not url or not password:
        typer.echo(
            "ERROR: BLUEBUBBLES_URL and BLUEBUBBLES_PASSWORD must be set (or present in .env)",
            err=True,
        )
        raise typer.Exit(1)

    asyncio.run(_run(url=url, password=password, send_message=send_message))


async def _run(url: str, password: str, send_message: str | None) -> None:
    print(f"Server: {url}")

    print("\n[1] Checking server private_api status via REST...")
    try:
        private_api_enabled = get_server_private_api_status(url, password)
    except Exception as e:
        typer.echo(f"  ERROR: Could not reach server — {e}", err=True)
        raise typer.Exit(1)
    print(f"  private_api = {private_api_enabled}")

    print("\n[2] Spawning MCP server and listing tools...")
    params = StdioServerParameters(
        command="uv",
        args=["run", "bb-mcp"],
        env={**os.environ, "BLUEBUBBLES_URL": url, "BLUEBUBBLES_PASSWORD": password},
    )

    failures = 0
    send_results: list[tuple[str, object]] = []

    async with stdio_client(params, errlog=open(os.devnull, "w")) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_resp = await session.list_tools()
            tool_names = {t.name for t in tools_resp.tools}

            if send_message:
                ta_args = {
                    "address": send_message,
                    "message": "Test message from validate_tools.py (send_message_to_address)",
                }
                print(f"\n[*] send_message_to_address({json.dumps(ta_args)})")
                result = await session.call_tool("send_message_to_address", ta_args)
                send_results.append(("send_message_to_address", result))

                chat_guid = f"any;-;{send_message}"
                sm_args = {
                    "chat_guid": chat_guid,
                    "message": "Test message from validate_tools.py (send_message)",
                }
                print(f"[*] send_message({json.dumps(sm_args)})")
                result = await session.call_tool("send_message", sm_args)
                send_results.append(("send_message", result))

    print(f"  Tools returned: {sorted(tool_names)}\n")

    print("[3] Validating always-present tools...")
    for name in ALWAYS_PRESENT:
        if not check(name in tool_names, f"{name} present"):
            failures += 1

    print(
        f"\n[4] Validating Private API tools (expected {'present' if private_api_enabled else 'absent'})..."
    )
    for name in PRIVATE_API_ONLY:
        if private_api_enabled:
            if not check(name in tool_names, f"{name} present (private_api=true)"):
                failures += 1
        else:
            if not check(name not in tool_names, f"{name} absent (private_api=false)"):
                failures += 1

    print("\n[5] Checking for unexpected tools...")
    known = set(ALWAYS_PRESENT) | set(PRIVATE_API_ONLY)
    unexpected = tool_names - known
    if unexpected:
        print(
            f"  [WARN] Unknown tools (not in ALWAYS_PRESENT or PRIVATE_API_ONLY): {sorted(unexpected)}"
        )
        print(
            "         Add them to the appropriate list in this script if they are intentional."
        )
    else:
        print("  [PASS] No unexpected tools")

    for tool_name, result in send_results:
        print(f"\n[{tool_name} result]")
        if result.isError:
            print(f"  [FAIL] {result.content}")
            failures += 1
        else:
            print("  [PASS] message sent")
            for block in result.content:
                print(f"  {block.text[:200]}")

    print()
    if failures:
        print(f"FAILED — {failures} check(s) did not pass.")
        raise typer.Exit(1)
    else:
        print("PASSED — tool list matches server capabilities.")


if __name__ == "__main__":
    app()
