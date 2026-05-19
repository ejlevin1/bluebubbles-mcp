set dotenv-load := true

# List available recipes
default:
    @just --list

# Install dependencies and git hooks
setup:
    uv sync --extra dev
    lefthook install

# Install all dependencies including dev extras
install:
    uv sync --extra dev

# Run unit tests
test *args:
    uv run --extra dev pytest tests/ --ignore=tests/integration -v {{ args }}

# Run integration tests (requires BLUEBUBBLES_URL and BLUEBUBBLES_PASSWORD)
test-integration *args:
    uv run --extra dev pytest tests/integration -v {{ args }}

# Run unit tests with coverage report
coverage:
    uv run --extra dev pytest tests/ --ignore=tests/integration -v --cov=src/bb_mcp --cov-report=term-missing

# Run smoke test against uvx config (requires BLUEBUBBLES_URL and BLUEBUBBLES_PASSWORD)
smoke-uvx:
    uv run scripts/smoke_uvx.py

# Run smoke test against Docker config (requires BLUEBUBBLES_URL and BLUEBUBBLES_PASSWORD)
smoke-docker:
    uv run scripts/smoke_docker.py

# Validate MCP tool list matches server capabilities; optionally send a test message
# Usage: just validate-tools [+15551234567]
validate-tools phone="":
    uv run scripts/validate_tools.py {{ if phone != "" { "--send-message " + phone } else { "" } }}

# Run end-to-end tests against the live server (read-only by default)
# Pass --send to also send real messages: just e2e --send
e2e *args="--no-send":
    uv run scripts/e2e_test.py {{ args }}

# Lint with ruff
lint:
    uv run --extra dev ruff check src/ tests/

# Format with ruff
fmt:
    uv run --extra dev ruff format src/ tests/

# Lint + format check (non-destructive, for CI)
check:
    uv run --extra dev ruff check src/ tests/
    uv run --extra dev ruff format --check src/ tests/

# Type-check with mypy
typecheck:
    uv run --extra dev mypy src/

# Run pre-commit hook checks
pre-commit:
    lefthook run pre-commit

# Run pre-push hook checks
pre-push:
    lefthook run pre-push

# Run all checks (lint, format, typecheck, tests)
ci: check typecheck test

# Build Docker image locally
docker-build:
    docker build -t bluebubbles-mcp:local .

# Run the MCP server via docker-compose (requires BLUEBUBBLES_URL and BLUEBUBBLES_PASSWORD)
docker-up:
    docker compose up

# Remove docker-compose containers
docker-down:
    docker compose down

# Start the MCP server directly (requires BLUEBUBBLES_URL and BLUEBUBBLES_PASSWORD in env or .env)
serve:
    uv run python -m bb_mcp.server
