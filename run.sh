#!/bin/sh
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec uv run --no-cache --env-file "$SCRIPT_DIR/.env" bb-mcp
