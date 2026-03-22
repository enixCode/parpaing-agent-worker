# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Agent-worker is a Docker-based orchestration system. **Tower** (FastAPI) receives jobs, dispatches them to isolated **Worker** containers running AI coding agents, and returns results via an async job queue. Supports multiple engines (Claude Code, OpenCode) via TOML-based engine configs. No shared volumes - config is injected via `put_archive`, results are extracted via `get_archive`.

## Architecture

```
POST /jobs - LB :8420 - Tower :8420 - 202 {job_id} (immediate)
       │
       ▼
  LB (nginx :80, exposed as TOWER_PORT):
    Round-robin proxy to N Tower replicas
       │
       ▼
  Tower (FastAPI :8420) x TOWER_REPLICAS:
    1. Create job in JobStore (PostgreSQL - shared)
    2. asyncio.create_task(execute_job)
    3. Semaphore controls concurrency (MAX_CONCURRENT_JOBS per instance)
       │
       ▼
  execute_job (background):
    1. Load profile (TOML, LRU-cached) + render prompt & CLAUDE.md (Jinja2)
    2. Acquire warm container from pool (atomic SQL)
    3. put_archive config (job.json + CLAUDE.md + hooks) - triggers entrypoint
    4. container.wait() in thread
    5. get_archive result.json from stopped container
    6. Release container (destroy + replenish pool)
    7. Fire webhook if configured
       │
       ▼
  Worker Container (Node.js + engine binary):
    entrypoint.sh (wait for config) - run-job.sh - pre-job.sh - parse-job.js - {engine} {args} - post-job.sh - result.json
       │
       ▼
  Gateway (nginx :4000):
    Proxies /anthropic/ - api.anthropic.com, /openai/ - api.openai.com
    Workers get placeholder keys, real keys stay in gateway
       │
       ▼
  GET /jobs/{id} - poll status/result (any Tower instance)
  GET /jobs/{id}/wait - block until job finishes (?timeout=)
  DELETE /jobs/{id} - cancel + kill container (cross-instance via Docker)
  GET /engines - list available engines (public)
  GET /profiles - list available profiles (public)
  GET /health - deep check: DB + Docker + pool + gateway (public)
  GET /metrics - Prometheus metrics (public)
  GET /docs - Scalar API docs (public)
  GET /openapi.json - OpenAPI schema (public)
  GET /ui - web dashboard (single-page, public)
```

### Container Pool

Tower maintains a pool of warm worker containers (DB-backed `containers` table):

```
Pool Maintainer (background task)        Tower (job dispatch)
  │                                        │
  │ count(ready) < POOL_SIZE?              │ Job arrives
  │ - create container (shared network)    │ - UPDATE SET busy (atomic)
  │ - INSERT INTO containers               │ - put_archive config
  │                                        │ - container.wait()
  │ stale > POOL_MAX_IDLE?                 │ - get_archive result
  │ - destroy container + DELETE           │ - destroy container + DELETE
  │                                        │ - pool refills automatically
```

Acquire uses `FOR UPDATE SKIP LOCKED` - multi-tower safe, no race conditions. Orphan protection: if DB registration fails after container creation, the container is force-removed.

### Job State Machine

```
pending ──start_job()──- running ──finish_job()──- completed | failed | cancelled
```

All transitions are atomic SQL: `UPDATE WHERE status IN (...) RETURNING job_id`. Only one Tower wins each transition. `finish_job()` returns `False` if another Tower already finished the job - callers treat this as a no-op.

### Cancellation Pattern (Kubernetes-style)

No in-memory task tracking. Cancel = DB state change + Docker kill:
1. `finish_job(CANCELLED)` - atomic DB update
2. `pool.release()` - Docker kill + remove container
3. Background coroutine's `container.wait()` returns - its `finish_job()` is a no-op (status already cancelled)

### Multi-Tower Recovery

On startup, each Tower checks running jobs in PostgreSQL:
- Container still alive - re-adopt via semaphore (wait for completion, extract output)
- Container gone (`docker.errors.NotFound`) - mark as failed
- Docker transient error - skip (retry next restart)
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

Each worker container runs in isolation. Security hardening is **always enabled**:
- `cap_drop=["ALL"]` - no Linux capabilities
- `no-new-privileges:true`
- PID limit 100 (prevents fork bomb)
- `ipc_mode="private"`
- Memory & CPU limits (global defaults from config)
- Non-root `agent` user (UID 1000)
- Shared `agent-workers` network with ICC disabled (workers can't see each other)
- `internal=True` - workers have no direct internet access
- LLM Gateway (always enabled): workers get placeholder keys, real keys stay in gateway container
- Optional gVisor kernel-level isolation via `WORKER_RUNTIME=runsc`
- Container destroyed after each job (completely clean)

## File Structure

Key directories (use `ls` for full listing):

- `tower/` - Orchestrator (FastAPI): config, routes, models, engines, profiles, pool, worker, job_store, job_runner
- `worker/` - Agent container: Dockerfile, entrypoint.sh, run-job.sh, parse-job.js
- `engines/` - Engine configs (TOML) - one per AI tool
- `profiles/` - Agent profiles (TOML) - define defaults per use case
- `templates/` - Jinja2 templates: `prompts/` (.md.j2) and `claude-md/` (.md.j2)
- `hooks/` - Worker hook scripts (optional, per-profile)
- `db/` - PostgreSQL schema (init.sql: jobs + containers tables)
- `gateway/` - LLM Gateway (nginx reverse proxy, hides API keys)
- `lb/` - Load balancer (nginx round-robin to Tower replicas)
- `ui/` - Web dashboard (single HTML file)
- `docs/` - Documentation (mkdocs)
- `tests/unit/` - Unit tests (no Docker required)
- `tests/test_e2e_*.py` - E2E tests (requires docker compose up)

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

Unit tests cover: model validation, engine command building, profile variable validation, pool logic, job runner, worker helpers.

E2E tests use `dry_run=True` to avoid spawning real Claude agents. Set `TOWER_URL` and `TOWER_API_KEY` env vars to test against a custom endpoint (defaults: `http://localhost:8420`, no auth). E2E tests cover: auth, concurrency, endpoints, health, job lifecycle, load, profiles, validation, wait.

## Config Resolution (profiles.py)

**Precedence**: request fields override profile defaults. Profile-only fields (resources, hooks, claude_md template) cannot be set per-request.

```
resolve_config(request):
  1. load_engine(name) - EngineConfig (LRU-cached, max 32)
  2. _load_profile(name) - TOML dict (LRU-cached, max 64)
  3. Prompt: request.prompt wins, else render profile [prompt] template with merged vars
  4. CLAUDE.md: always from profile [claude_md] template with merged claude_md_vars
  5. Model/tools/turns/budget/output_format: request wins if set, else profile, else hardcoded default
  6. system_prompt: from request only
  7. mcp_config: from request only
  8. plugins: request wins if set, else profile [plugins.enabled]
  9. Resources (timeout): profile-only
  10. Hooks (pre/post): profile-only
  - returns frozen JobConfig dataclass
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
  5. parse-job.js: read job.json - shell-escaped variables - /tmp/_vars.sh
  6. Build engine CLI args, run (or dry-run: node simulation - {dry_run:true, args:[...]})
  7. Run post-job.sh hook (receives JOB_STATUS, JOB_EXIT_CODE env vars)
```

## Constants (config.py - single source of truth)

- `VERSION = "0.3.0"`
- `DEFAULT_MODEL = "claude-sonnet-4-6"`

## Code Conventions

- **Job ID format**: `{agent_id}-{12-char-hex}` (e.g., `test-01-a3f2b1c0e5d6`)
- **Safe IDs**: agent_id, profile, and plugin names validated with `^[a-zA-Z0-9_-]{1,64}$`
- **Status enum**: `pending - running - completed | failed | cancelled`
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

Essential vars (see `.env.example` for the full list with defaults):

| Variable | Purpose |
|---|---|
| `TOWER_PORT` | Tower exposed port (default: 8420) |
| `TOWER_REPLICAS` | Number of Tower instances (default: 1) |
| `TOWER_API_KEY` | Bearer token for API auth (empty = no auth) |
| `ANTHROPIC_API_KEY` | API key (pay-per-token) |
| `CLAUDE_CODE_OAUTH_TOKEN` | OAuth token (Pro/Max subscription) |
| `MAX_CONCURRENT_JOBS` | Max parallel containers per Tower instance (default: 10) |
| `POOL_SIZE` | Warm containers in pool (default: 3) |
| `WORKER_RUNTIME` | gVisor runtime - set to "runsc" for kernel-level isolation |
| `DATABASE_URL` | PostgreSQL connection string |

All numeric config values are auto-clamped to valid ranges at startup (see `config.py` `_clamp_int`/`_clamp_float`). `DB_POOL_MAX_SIZE` auto-sizes to `MAX_CONCURRENT_JOBS * 2 + 5` unless explicitly set.

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

[command.map]                      # request field - CLI flag mapping
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

`load_engine()` loads and caches engine configs from TOML. `is_engine_available()` always returns `True` (gateway handles auth). `list_engines()` returns all engines with availability status. Engine configs are LRU-cached (maxsize=32).

**TOML to job.json mapping**: `engines.py` flattens the TOML structure when building `EngineConfig`:
- `[command].map` - `EngineConfig.flag_map` - `job.json engine.flag_map`
- `[command].list_join` - `EngineConfig.list_join` - `job.json engine.list_join`
- `[output].mode` - `EngineConfig.output_mode` - `job.json engine.output_mode`
- `[output].format` - `EngineConfig.output_format` - `job.json engine.output_format`
- `[output].path` - `EngineConfig.output_path` - `job.json engine.output_path`

`worker.py._build_config_tar()` serializes the flattened fields into `job.json`, and `parse-job.js` reads them directly (e.g. `engine.flag_map`, `engine.list_join`).

The `engine` field is **required** in API requests. `job.json` includes the full engine config so the worker's `parse-job.js` can build the correct CLI invocation.

## Config: TOML Profiles + Jinja2 Templates

**Profiles** (`profiles/*.toml`): define agent defaults (model, tools, max_turns, prompt template, claude_md template, hooks, resources).

**Templates** (`templates/**/*.j2`): Jinja2 templates for prompts and CLAUDE.md. Undefined variables render as empty (non-strict `jinja2.Undefined`).

**Hooks** (`hooks/*.sh` or inline): pre/post scripts injected into the worker container via put_archive. Can be a filename reference from `hooks/` dir or an inline multiline script in the profile TOML.

Flow: `profile.toml - load defaults + render templates - tar archive (put_archive) - worker`

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

- **engines/** changed - update `docs/api.md` (GET /engines response), `CLAUDE.md` if new engine added
- **engines.py** changed - check if `worker/parse-job.js` contract still matches
- **models.py** changed - update `db/init.sql`, `docs/request.md`, `CLAUDE.md` if structure changed
- **Profiles** changed - update `docs/profiles.md`
- **Templates** added/changed - update `docs/templates.md`
- **Env vars** added/changed - update `.env.example`, `config.py`, `docs/config.md`, `CLAUDE.md` (NOT docker-compose.yml - `env_file: .env` passes all vars automatically)
- **API keys for new engines** - just add to `.env.example` + engine TOML `[env] auth` - pool reads engines dynamically
- **Endpoints** added/changed - update `docs/api.md`, `CLAUDE.md`
- **worker.py / job_store.py / pool.py** changed - check if `docs/` or `CLAUDE.md` need update
- **worker/ (entrypoint.sh, run-job.sh, parse-job.js)** changed - verify tower/worker.py contract still matches
- **Default values** changed - update `docs/request.md`, `docs/profiles.md`, `db/init.sql`
- **Constants** (version, default model, timeouts) - update only in `config.py` (single source of truth)

Always propagate changes across: code ↔ schema ↔ docs ↔ CLAUDE.md

**Critical rule**: every code change MUST include updates to associated documentation. Run a coherence check (grep for stale references) before considering any task complete.

## Tech Stack

- **Tower**: Python 3.12, FastAPI, Jinja2, Docker SDK, asyncpg, prometheus_client, httpx
- **Worker**: Node.js 22, engine binaries (Claude Code, OpenCode)
- **Config**: TOML (tomllib stdlib) + Jinja2
- **DB**: PostgreSQL 17 (job persistence + container pool)
- **Hooks**: Pre/post scripts injected into worker container (per-profile)
- **LB**: nginx (round-robin proxy to Tower replicas, SSE support)
- **Gateway**: nginx (LLM API proxy - hides API keys from workers)
- **Infra**: Docker Compose, `pg-data` volume, `agent-net` (LB+Tower+DB+Gateway), `agent-workers` (shared worker network, internal, ICC disabled)
