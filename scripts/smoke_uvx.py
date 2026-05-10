#!/usr/bin/env python3
"""Smoke test: verify the uvx configuration works end-to-end.

Spawns the MCP server via uvx and exercises a ping + list_chats call
through the MCP protocol over stdio, then exits.

Usage:
    BLUEBUBBLES_URL=https://... BLUEBUBBLES_PASSWORD=... uv run scripts/smoke_uvx.py
"""

from __future__ import annotations

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    url = os.environ.get("BLUEBUBBLES_URL")
    password = os.environ.get("BLUEBUBBLES_PASSWORD")
    if not url or not password:
        print(
            "ERROR: BLUEBUBBLES_URL and BLUEBUBBLES_PASSWORD must be set",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Launching server via uvx...")
    params = StdioServerParameters(
        command="uvx",
        args=["--from", "git+https://github.com/ejlevin1/bluebubbles-mcp", "bb-mcp"],
        env={**os.environ, "BLUEBUBBLES_URL": url, "BLUEBUBBLES_PASSWORD": password},
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            tool_names = [t.name for t in tools.tools]
            print(f"  Tools available: {len(tool_names)}")
            assert "ping" in tool_names, "ping tool missing"
            assert "list_chats" in tool_names, "list_chats tool missing"

            print("  Calling ping...")
            result = await session.call_tool("ping", {})
            assert result.content, "ping returned no content"
            print("  ping: OK")

            print("  Calling list_chats...")
            result = await session.call_tool("list_chats", {"limit": 3})
            assert result.content, "list_chats returned no content"
            print("  list_chats: OK")

    print("\nuvx smoke test PASSED")


if __name__ == "__main__":
    asyncio.run(main())
