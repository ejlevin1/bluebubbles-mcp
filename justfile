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

# Run tests
test *args:
    uv run --extra dev pytest tests/ -v {{ args }}

# Run tests with coverage report
coverage:
    uv run --extra dev pytest tests/ -v --cov=src/bb_mcp --cov-report=term-missing

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
