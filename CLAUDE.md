# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Agent-worker is a Docker-based orchestration system. **Tower** (FastAPI) receives jobs, dispatches them to isolated **Worker** containers running AI coding agents, and returns results via an async job queue. Supports multiple engines (Claude Code, OpenCode) via TOML-based engine configs. No shared volumes - config is injected via `put_archive`, results are extracted via `get_archive`.

## Architecture

```
POST /jobs → Tower :8420 → 202 {job_id} (immediate)
       │
       ▼
  Tower (FastAPI :8420) × TOWER_REPLICAS:
    1. Create job in JobStore (PostgreSQL - shared)
    2. asyncio.create_task(execute_job)
    3. Semaphore controls concurrency (MAX_CONCURRENT_JOBS per instance)
       │
       ▼
  execute_job (background):
    1. Load profile (TOML, LRU-cached) + render prompt & CLAUDE.md (Jinja2)
    2. Acquire warm container from pool (atomic SQL)
    3. put_archive config (job.json + CLAUDE.md + hooks) → triggers entrypoint
    4. container.wait() in thread
    5. get_archive result.json from stopped container
    6. Release container (destroy + replenish pool)
    7. Fire webhook if configured
       │
       ▼
  Worker Container (Node.js + engine binary):
    entrypoint.sh (wait for config) → run-job.sh → pre-job.sh → parse-job.js → {engine} {args} → post-job.sh → result.json
       │
       ▼
  GET /jobs/{id} → poll status/result (any Tower instance)
  GET /jobs/{id}/wait → block until job finishes (?timeout=)
  DELETE /jobs/{id} → cancel + kill container (cross-instance via Docker)
  GET /engines → list available engines + auth status (public)
  GET /profiles → list available profiles (public)
  GET /health → deep check: DB + Docker + pool (public)
  GET /metrics → Prometheus metrics (public)
  GET /docs → Scalar API docs (public)
  GET /openapi.json → OpenAPI schema (public)
  GET /ui → web dashboard (single-page, public)
```

### Container Pool

Tower maintains a pool of warm worker containers (DB-backed `containers` table):

```
Pool Maintainer (background task)        Tower (job dispatch)
  │                                        │
  │ count(ready) < POOL_SIZE?              │ Job arrives
  │ → create container (shared network)    │ → UPDATE SET busy (atomic)
  │ → INSERT INTO containers               │ → put_archive config
  │                                        │ → container.wait()
  │ stale > POOL_MAX_IDLE?                 │ → get_archive result
  │ → destroy container + DELETE           │ → destroy container + DELETE
  │                                        │ → pool refills automatically
```

Acquire uses `FOR UPDATE SKIP LOCKED` - multi-tower safe, no race conditions.

### Job State Machine

```
pending ──start_job()──▶ running ──finish_job()──▶ completed | failed | cancelled
```

All transitions are atomic SQL: `UPDATE WHERE status IN (...) RETURNING job_id`. Only one Tower wins each transition. `finish_job()` returns `False` if another Tower already finished the job - callers treat this as a no-op.

### Cancellation Pattern (Kubernetes-style)

No in-memory task tracking. Cancel = DB state change + Docker kill:
1. `finish_job(CANCELLED)` - atomic DB update
2. `pool.release()` - Docker kill + remove container
3. Background coroutine's `container.wait()` returns → its `finish_job()` is a no-op (status already cancelled)

### Multi-Tower Recovery

On startup, each Tower checks running jobs in PostgreSQL:
- Container still alive → re-adopt via semaphore (wait for completion, extract output)
- Container gone (`docker.errors.NotFound`) → mark as failed
- Docker transient error → skip (retry next restart)
- Cancel works cross-instance: kills container via Docker socket, any Tower can cancel any job

### Config Injection (put_archive)

Tower builds a tar archive and injects it into the running container at `/tmp/config/`:
```
config/
  ├── job.json         (prompt, model, tools, limits, dry_run, engine config)
  ├── CLAUDE.md        (rendered from profile template)
  ├── settings.json    (plugins config, if any)
  ├── mcp.json         (MCP server config, if any)
  ├── pre-job.sh       (hook script, if any)
  ├── post-job.sh      (hook script, if any)
  └── .ready           (marker file - triggers entrypoint, MUST be last in tar)
```

### Result Extraction (get_archive)

After container stops, Tower extracts files from the container's writable layer:
- `/output/result.json` - claude output or dry-run simulation
- `/output/stderr.log` - stderr capture (non-dry-run only)

### Worker Security Model

Each worker container runs in isolation. Hardening is **conditional** on `WORKER_HARDENED=true`:
- `read_only=True` root filesystem (tmpfs: `/home/agent` 1g, `/tmp` 512m, `/output` 256m)
- `cap_drop=["ALL"]` - no Linux capabilities
- `no-new-privileges:true`
- PID limit 256 (prevents fork bomb)
- Memory & CPU limits (global defaults from config)
- Non-root `agent` user (UID 1000)
- Shared `agent-workers` network with ICC disabled (workers can't see each other)
- `internal=True` - workers have no direct internet access
- LLM Gateway (always enabled): workers get placeholder keys, real keys stay in gateway container
- Container destroyed after each job (completely clean)

## File Structure

```
agent-worker/
├── db/                    ← PostgreSQL schema
│   └── init.sql           ← jobs + containers tables
├── gateway/               ← LLM Gateway (nginx reverse proxy)
│   └── gateway.conf.template ← nginx template (envsubst)
├── docs/                  ← API & config documentation
│   ├── api.md
│   ├── request.md
│   ├── profiles.md
│   ├── templates.md
│   ├── config.md
│   ├── architecture.svg   ← Architecture diagram
│   ├── assets/            ← Static assets for docs
│   │   ├── dashboard.png
│   │   └── profiles.svg
│   └── banners/
│       └── hero-banner.svg
├── engines/               ← Engine configs (TOML) - one per AI tool
│   ├── claude-code.toml   ← Anthropic Claude Code CLI
│   └── opencode.toml      ← OpenCode (SST/Anomaly)
├── profiles/              ← Agent profiles (TOML)
│   ├── default.toml
│   ├── code-review.toml
│   └── researcher.toml
├── templates/             ← Jinja2 templates
│   ├── prompts/           ← Prompt templates (.md.j2)
│   └── claude-md/         ← CLAUDE.md templates
├── hooks/                 ← Worker hook scripts (optional)
│   ├── pre-job.example.sh ← Example pre-job hook
│   └── post-job.example.sh ← Example post-job hook
├── tower/                 ← Orchestrator (FastAPI)
│   ├── config.py          ← Env vars, Docker client, pool config
│   ├── main.py            ← App + routes (health, /jobs CRUD, /engines)
│   ├── models.py          ← Pydantic models (request/response)
│   ├── engines.py         ← Engine loading + availability checking
│   ├── profiles.py        ← Profile loading, template rendering, config resolution
│   ├── pool.py            ← Container pool (warm containers, DB-backed)
│   ├── worker.py          ← Config injection (put_archive), result extraction (get_archive)
│   ├── job_store.py       ← PostgreSQL job store with TTL cleanup
│   └── job_runner.py      ← Background job execution + webhook
├── ui/                    ← Web dashboard (single HTML file)
│   └── index.html         ← Dashboard: job list, create, cancel
├── worker/                ← Agent container
│   ├── Dockerfile         ← Node.js 22 + engine binaries
│   ├── entrypoint.sh      ← Waits for config injection, then exec run-job.sh
│   ├── run-job.sh         ← Engine-agnostic job execution (hooks + CLI + result)
│   └── parse-job.js       ← Parse job.json + engine config → shell variables
├── tests/                 ← Test suite
│   ├── unit/              ← Unit tests (no Docker required)
│   │   ├── test_models.py
│   │   ├── test_engines.py
│   │   ├── test_profiles.py
│   │   ├── test_job_runner.py
│   │   ├── test_job_store.py
│   │   └── test_worker.py
│   └── ...                ← E2E tests (conftest, health, validation, lifecycle, etc.)
├── docker-compose.yml
└── .env.example
```

## Commands

```bash
cp .env.example .env       # fill in auth (API key or OAuth token)
docker compose up --build -d             # single Tower (default)
TOWER_REPLICAS=3 docker compose up -d    # scale to 3 Towers
docker compose up --scale tower=3 -d     # alternative scaling
docker compose build worker

# Health (public - no auth required)
curl http://localhost:8420/health

# Create async job (engine is required)
curl -X POST http://localhost:8420/jobs \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOWER_API_KEY" \
  -d '{"agent_id": "test", "engine": "claude-code", "prompt": "List files in /workspace"}'

# Wait for result (blocking)
curl -H "Authorization: Bearer $TOWER_API_KEY" http://localhost:8420/jobs/{job_id}/wait

# Poll status
curl -H "Authorization: Bearer $TOWER_API_KEY" http://localhost:8420/jobs/{job_id}

# Cancel
curl -X DELETE -H "Authorization: Bearer $TOWER_API_KEY" http://localhost:8420/jobs/{job_id}

# List jobs
curl -H "Authorization: Bearer $TOWER_API_KEY" http://localhost:8420/jobs?status=running

# List available engines (public)
curl http://localhost:8420/engines

# List available profiles (public)
curl http://localhost:8420/profiles
```

## Testing

```bash
# Unit tests (fast, no Docker required)
pytest tests/unit/ -v

# E2E tests (requires docker compose up)
pytest tests/                          # run all E2E tests
pytest tests/test_e2e_health.py        # run a single test file
pytest tests/test_e2e_health.py -k "test_health"  # run a single test
```

Unit tests cover: model validation, engine command building, profile variable validation.

E2E tests use `dry_run=True` to avoid spawning real Claude agents. Set `TOWER_URL` and `TOWER_API_KEY` env vars to test against a custom endpoint (defaults: `http://localhost:8420`, no auth).

## Config Resolution (profiles.py)

**Precedence**: request fields override profile defaults. Profile-only fields (resources, hooks, claude_md template) cannot be set per-request.

```
resolve_config(request):
  1. load_engine(name) → EngineConfig (LRU-cached, max 32)
  2. _load_profile(name) → TOML dict (LRU-cached, max 64)
  3. Prompt: request.prompt wins, else render profile [prompt] template with merged vars
  4. CLAUDE.md: always from profile [claude_md] template with merged claude_md_vars
  5. Model/tools/turns/budget/output_format: request wins if set, else profile, else hardcoded default
  6. system_prompt: from request only
  7. mcp_config: from request only
  8. plugins: request wins if set, else profile [plugins.enabled]
  9. Resources (timeout): profile-only
  10. Hooks (pre/post): profile-only
  → returns frozen JobConfig dataclass
```

### Worker Entrypoint Flow

```
entrypoint.sh:
  Wait for /tmp/config/.ready (injected by Tower via put_archive)
  exec run-job.sh

run-job.sh (7 steps):
  1. Init ~/.claude (empty JSON to skip onboarding)
  2. Copy settings.json (plugins) if present
  3. Copy CLAUDE.md to workspace/.claude/ if present
  4. Run pre-job.sh hook (fails entire job on non-zero)
  5. parse-job.js: read job.json → shell-escaped variables → /tmp/_vars.sh
  6. Build engine CLI args, run (or dry-run: node simulation → {dry_run:true, args:[...]})
  7. Run post-job.sh hook (receives JOB_STATUS, JOB_EXIT_CODE env vars)
```

## Code Conventions

- **Job ID format**: `{agent_id}-{12-char-hex}` (e.g., `test-01-a3f2b1c0e5d6`)
- **Safe IDs**: agent_id, profile, and plugin names validated with `^[a-zA-Z0-9_-]{1,64}$`
- **Status enum**: `pending → running → completed | failed | cancelled`
- **DB schema**: request stored as single JSONB column (not flattened into individual columns)
- **Container pool**: DB-backed `containers` table, atomic acquire via `FOR UPDATE SKIP LOCKED`
- **Logging**: per-module loggers (`tower`, `tower.job_runner`, `tower.worker`, `tower.job_store`, `tower.profiles`, `tower.engines`, `tower.pool`)
- **Atomic DB updates**: `UPDATE WHERE status IN ('pending','running') RETURNING` prevents multi-tower race conditions
- **Cancellation pattern**: DB state + `pool.release()` (kill + remove container)
- **Graceful shutdown**: leaves running containers for re-adoption by other Towers
- **Path traversal prevention**: `Path.is_relative_to()` checks on profile loading and hook injection
- **SSRF prevention**: webhook URLs validated at request time + re-validated before HTTP call (DNS rebinding defense)
- **Blocking calls**: Docker SDK calls (`put_archive`, `get_archive`, `container.wait`) wrapped in `asyncio.to_thread`

## Key Environment Variables

| Variable | Purpose |
|---|---|
| `TOWER_PORT` | Tower exposed port (default: 8420) |
| `TOWER_REPLICAS` | Number of Tower instances (default: 1) |
| `TOWER_API_KEY` | Bearer token for API auth (empty = no auth) |
| `ANTHROPIC_API_KEY` | API key (pay-per-token) |
| `CLAUDE_CODE_OAUTH_TOKEN` | OAuth token (Pro/Max subscription) |
| `WORKER_IMAGE` | Docker image name for worker |
| `ENGINES_DIR` | Path to engine TOML configs (default: `/app/engines`) |
| `GATEWAY_URL` | LLM Gateway URL (default: http://agent-gateway:4000) |
| `GATEWAY_CONTAINER` | Gateway Docker container name (default: agent-gateway) |
| `MAX_RESULT_SIZE` | Max result.json size in bytes (default: 10 MB) |
| `MAX_CONCURRENT_JOBS` | Max parallel containers per Tower instance (default: 10) |
| `JOB_TTL_HOURS` | Hours to keep finished jobs (default: 24) |
| `MAX_RETAINED_JOBS` | Max finished jobs in DB (default: 1000) |
| `WORKER_TIMEOUT_SECONDS` | Max worker container runtime (default: 3600) |
| `WORKER_MEM_LIMIT` | Worker memory limit (default: 512m) |
| `WORKER_CPU_LIMIT` | Worker CPU limit (default: 1.0) |
| `WORKER_HARDENED` | Enable container hardening (read_only, cap_drop, tmpfs) (default: false) |
| `POOL_SIZE` | Warm containers in pool (default: 3) |
| `POOL_CHECK_INTERVAL` | Pool maintenance interval in seconds (default: 10) |
| `POOL_MAX_IDLE` | Max idle time before container recycling (default: 3600) |
| `CLEANUP_INTERVAL` | Seconds between job cleanup cycles (default: 600) |
| `WEBHOOK_TIMEOUT` | HTTP timeout for webhook calls in seconds (default: 10) |
| `DB_POOL_MIN_SIZE` | Minimum asyncpg connections (default: 2) |
| `DB_POOL_MAX_SIZE` | Maximum asyncpg connections (default: 10) |
| `WORKER_NET` | Docker network name for worker containers (default: agent-workers) |
| `UI_PATH` | Path to dashboard HTML file (default: /app/ui/index.html) |
| `PROFILES_DIR` | Path to profile TOML configs (default: `/app/profiles`) |
| `TEMPLATES_DIR` | Path to Jinja2 templates (default: `/app/templates`) |
| `HOOKS_DIR` | Path to hook scripts (default: `/app/hooks`) |
| `DATABASE_URL` | PostgreSQL connection string (default: `postgresql://tower:tower@db:5432/tower`) |
| `POSTGRES_USER` | PostgreSQL user (default: tower) |
| `POSTGRES_PASSWORD` | PostgreSQL password (required) |
| `POSTGRES_DB` | PostgreSQL database (default: tower) |

## Engine System (engines.py)

Engines define how to invoke each AI tool. Each engine is a TOML file in `engines/` with sections:

```toml
[engine]
id = "claude-code"
name = "Claude Code"
description = "Anthropic Claude Code CLI agent"

[command]
binary = "claude"                  # CLI binary name
prompt_flag = "-p"                 # flag for prompt (empty = positional arg)
static_args = ["--verbose"]        # always-present args

[command.map]                      # request field → CLI flag mapping
model = "--model"
output_format = "--output-format"
max_turns = "--max-turns"
max_budget_usd = "--max-budget-usd"
system_prompt = "--system-prompt"
allowed_tools = "--allowedTools"

[command.list_join]                # how to join list values per field
allowed_tools = ","

[output]
mode = "stdout"                    # stdout (capture stdout) | file (read from path)
format = "json"                    # json | text
# path = "/output/result.json"    # where engine writes output (file mode only)

[env]
auth = ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"]  # at least ONE must be set
```

`load_engine()` loads and caches engine configs from TOML. `is_engine_available()` checks if required auth env vars are set. `list_engines()` returns all engines with availability status. Engine configs are LRU-cached (maxsize=32).

The `engine` field is **required** in API requests. `job.json` includes the full engine config so the worker's `parse-job.js` can build the correct CLI invocation.

## Config: TOML Profiles + Jinja2 Templates

**Profiles** (`profiles/*.toml`): define agent defaults (model, tools, max_turns, prompt template, claude_md template, hooks, resources).

**Templates** (`templates/**/*.j2`): Jinja2 templates for prompts and CLAUDE.md. Undefined variables render as empty (non-strict `jinja2.Undefined`).

**Hooks** (`hooks/*.sh` or inline): pre/post scripts injected into the worker container via put_archive. Can be a filename reference from `hooks/` dir or an inline multiline script in the profile TOML.

Flow: `profile.toml → load defaults + render templates → tar archive (put_archive) → worker`

**Profile is mandatory** (default: `"default"`). Request fields override profile values. Error if profile not found.

**Profile variables** support typed definitions with validation:

```toml
[prompt.variables.REPO_URL]
type = "string"          # string | integer | float | boolean
default = ""
required = true
enum = ["val1", "val2"]  # optional - restricts allowed values
```

Legacy format (`key = "value"`) still supported. Variables are passed to Jinja2 templates and can be overridden per-request via `prompt_vars`.

## Sustainability Rules

When modifying any code, ALWAYS update the related files to keep everything in sync:

- **engines/** changed → update `docs/api.md` (GET /engines response), `CLAUDE.md` if new engine added
- **engines.py** changed → check if `worker/parse-job.js` contract still matches
- **models.py** changed → update `db/init.sql`, `docs/request.md`, `CLAUDE.md` if structure changed
- **Profiles** changed → update `docs/profiles.md`
- **Templates** added/changed → update `docs/templates.md`
- **Env vars** added/changed → update `.env.example`, `config.py`, `docs/config.md`, `CLAUDE.md` (NOT docker-compose.yml - `env_file: .env` passes all vars automatically)
- **API keys for new engines** → just add to `.env.example` + engine TOML `[env] auth` - pool reads engines dynamically
- **Endpoints** added/changed → update `docs/api.md`, `CLAUDE.md`
- **worker.py / job_store.py / pool.py** changed → check if `docs/` or `CLAUDE.md` need update
- **worker/ (entrypoint.sh, run-job.sh, parse-job.js)** changed → verify tower/worker.py contract still matches
- **Default values** changed → update `docs/request.md`, `docs/profiles.md`, `db/init.sql`
- **Constants** (version, default model, timeouts) → update only in `config.py` (single source of truth)

Always propagate changes across: code ↔ schema ↔ docs ↔ CLAUDE.md

**Critical rule**: every code change MUST include updates to associated documentation. Run a coherence check (grep for stale references) before considering any task complete.

## Tech Stack

- **Tower**: Python 3.12, FastAPI, Jinja2, Docker SDK, asyncpg, prometheus_client, httpx
- **Worker**: Node.js 22, engine binaries (Claude Code, OpenCode)
- **Config**: TOML (tomllib stdlib) + Jinja2
- **DB**: PostgreSQL 17 (job persistence + container pool)
- **Hooks**: Pre/post scripts injected into worker container (per-profile)
- **Infra**: Docker Compose, `pg-data` volume, `agent-net` (Tower+DB), `agent-workers` (shared worker network, ICC disabled)
