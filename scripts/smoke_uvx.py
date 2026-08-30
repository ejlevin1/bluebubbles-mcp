#!/usr/bin/env python3
"""Smoke test: verify the uvx configuration works end-to-end.

Spawns the MCP server via uvx straight from git — the same way an MCP client
would — and exercises tools plus the bundled skill resources over stdio.

Usage:
    uv run scripts/smoke_uvx.py                         # default repo, default branch
    uv run scripts/smoke_uvx.py --ref my-branch         # a branch, tag, or commit SHA
    uv run scripts/smoke_uvx.py --source git+file://$PWD --ref my-branch
    uv run scripts/smoke_uvx.py --from 'git+https://github.com/you/fork@sha'

    just smoke-uvx --ref my-branch

Requires BLUEBUBBLES_URL and BLUEBUBBLES_PASSWORD in the environment (or .env,
which `just` loads for you).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

DEFAULT_SOURCE = "git+https://github.com/ejlevin1/bluebubbles-mcp"

#: Files the skill must ship. Kept explicit so a packaging change that silently
#: drops one fails here instead of quietly shipping a skill with no references.
EXPECTED_SKILL_FILES = {
    "SKILL.md",
    "references/best-practices.md",
    "references/tools.md",
}

#: Every skill doc is comfortably over 3KB; a stub would fall well under.
MIN_SKILL_FILE_BYTES = 1_000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the MCP server via uvx from git and validate it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--source",
        default=os.environ.get("UVX_SOURCE", DEFAULT_SOURCE),
        help=f"Package location, without a ref. Any spec uvx accepts — a git URL, "
        f"a local 'git+file:///path/to/repo', or a directory. "
        f"Default: {DEFAULT_SOURCE}",
    )
    parser.add_argument(
        "--ref",
        default=os.environ.get("UVX_REF"),
        help="Branch, tag, or commit SHA to check out, appended to --source as '@REF'. "
        "Default: the repository's default branch.",
    )
    parser.add_argument(
        "--from",
        dest="from_spec",
        help="A complete uvx --from spec, ref included. Overrides --source/--ref.",
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Reuse uv's cached build. By default --refresh is passed so a moving "
        "branch is always re-fetched.",
    )
    return parser.parse_args()


def build_spec(args: argparse.Namespace) -> str:
    if args.from_spec:
        return args.from_spec
    return f"{args.source}@{args.ref}" if args.ref else args.source


async def check_tools(session: ClientSession) -> None:
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


async def read_text(session: ClientSession, uri: str) -> str:
    result = await session.read_resource(uri)  # type: ignore[arg-type]
    assert result.contents, f"{uri} returned no content"
    text = getattr(result.contents[0], "text", None)
    assert text is not None, f"{uri} returned no text content"
    return text


async def check_skill(session: ClientSession) -> None:
    """Verify the bundled skill is served, and served intact."""
    resources = await session.list_resources()
    uris = {str(r.uri) for r in resources.resources}
    print(f"  Resources: {sorted(uris)}")
    assert "skill://bluebubbles/SKILL.md" in uris, "skill main file not published"
    assert "skill://bluebubbles/_manifest" in uris, "skill manifest not published"

    templates = await session.list_resource_templates()
    patterns = {t.uriTemplate for t in templates.resourceTemplates}
    assert "skill://bluebubbles/{path*}" in patterns, (
        "skill file template not published"
    )

    manifest = json.loads(await read_text(session, "skill://bluebubbles/_manifest"))
    advertised = {f["path"] for f in manifest["files"]}
    missing = EXPECTED_SKILL_FILES - advertised
    assert not missing, f"skill is missing files: {sorted(missing)}"

    # Fetch every advertised file and confirm the bytes survived the trip.
    # The manifest is generated from the shipped files, so a matching hash only
    # proves the transfer was clean — a build that shipped a truncated file would
    # produce a manifest that agrees with it. The size floor is what catches that.
    for entry in manifest["files"]:
        path = entry["path"]
        served = (await read_text(session, f"skill://bluebubbles/{path}")).encode()
        digest = "sha256:" + hashlib.sha256(served).hexdigest()
        assert digest == entry["hash"], f"{path}: bytes do not match manifest hash"
        assert len(served) == entry["size"], f"{path}: size does not match manifest"
        assert len(served) > MIN_SKILL_FILE_BYTES, (
            f"{path}: only {len(served)} bytes — looks truncated"
        )
        print(f"  {path}: {len(served)} bytes, sha256 OK")

    skill = await read_text(session, "skill://bluebubbles/SKILL.md")
    assert "name: bluebubbles" in skill, "SKILL.md frontmatter looks wrong"
    tools_doc = await read_text(session, "skill://bluebubbles/references/tools.md")
    assert "Private API" in tools_doc, "references/tools.md content looks wrong"
    print("  skill content: OK")


async def main() -> None:
    args = parse_args()

    url = os.environ.get("BLUEBUBBLES_URL")
    password = os.environ.get("BLUEBUBBLES_PASSWORD")
    if not url or not password:
        print(
            "ERROR: BLUEBUBBLES_URL and BLUEBUBBLES_PASSWORD must be set",
            file=sys.stderr,
        )
        sys.exit(1)

    spec = build_spec(args)
    uvx_args = ["--from", spec, "bb-mcp"]
    if not args.no_refresh:
        uvx_args.insert(0, "--refresh")

    print(f"Launching server via uvx from {spec} ...")
    params = StdioServerParameters(
        command="uvx",
        args=uvx_args,
        env={**os.environ, "BLUEBUBBLES_URL": url, "BLUEBUBBLES_PASSWORD": password},
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"  Connected: {init.serverInfo.name} v{init.serverInfo.version}")
            await check_tools(session)
            await check_skill(session)

    print(f"\nuvx smoke test PASSED ({spec})")


if __name__ == "__main__":
    asyncio.run(main())
