# Contributing to Agent Worker

Thanks for your interest in contributing! This guide covers everything you need to get started.

## Prerequisites

- Docker & Docker Compose
- An Anthropic API key or Claude OAuth token

## Setup

```bash
git clone <repo-url> && cd agent-worker
cp .env.example .env
# Fill in ANTHROPIC_API_KEY (or CLAUDE_CODE_OAUTH_TOKEN) and POSTGRES_PASSWORD
docker compose up --build -d
```

Verify with `curl http://localhost:8420/health`.

## Project Structure

| Directory | Role | Stack |
|---|---|---|
| `tower/` | Orchestrator (API + job queue) | Python 3.12, FastAPI, asyncpg |
| `worker/` | Ephemeral agent containers | Node.js 22, Claude Code CLI |
| `profiles/` | Agent config (TOML) | tomllib (stdlib) |
| `templates/` | Prompt/settings templates | Jinja2 |
| `db/` | PostgreSQL schema | SQL |
| `docs/` | Documentation | Markdown |

## Making Changes

### 1. Branch

Create a branch from `main` with a descriptive name:

```bash
git checkout -b feat/my-feature
```

### 2. Code

Follow the sustainability rules — when you change one file, update all related files:

- **models.py** → `db/init.sql`, `docs/request.md`, `CLAUDE.md`
- **Profiles** → `docs/profiles.md`
- **Templates** → `docs/templates.md`
- **Env vars** → `.env.example`, `config.py`, `docs/config.md`, `CLAUDE.md` (NOT docker-compose.yml — `env_file: .env` passes all vars)
- **Endpoints** → `docs/api.md`, `CLAUDE.md`
- **Default values** → `docs/request.md`, `docs/profiles.md`, `db/init.sql`
- **docs/** → set `Generated:` date to today

### 3. Test

```bash
docker compose up --build -d
# Submit a test job
curl -X POST http://localhost:8420/jobs \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "test", "engine": "claude-code", "prompt": "List files in /workspace"}'
```

### 4. Commit

Write clear, concise commit messages. One logical change per commit.

## Principles

- **KISS** — Keep it simple. Minimal code that works.
- **SOLID** — Single responsibility per module/function.
- **YAGNI** — Don't build what isn't needed yet.

## Reporting Issues

Open an issue with:
- Steps to reproduce
- Expected vs actual behavior
- Docker and OS version

## Pull Requests

- Keep PRs focused on a single change
- Ensure docs are updated (see sustainability rules above)
- Describe what and why in the PR description
