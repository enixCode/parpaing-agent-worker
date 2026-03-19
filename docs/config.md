# Configuration Guide


All configuration is via environment variables in `.env`.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `TOWER_PORT` | `8420` | Tower exposed port |
| `TOWER_REPLICAS` | `1` | Number of Tower instances (horizontal scaling) |
| `TOWER_API_KEY` | - | Bearer token for API auth (empty = no auth) |
| `WORKER_IMAGE` | `agent-worker-worker` | Docker image name for worker containers |
| `WORKER_HARDENED` | `false` | Enable worker hardening (read_only, cap_drop, no-new-privileges, pids_limit) |
| `ANTHROPIC_API_KEY` | - | API key (pay-per-token) |
| `CLAUDE_CODE_OAUTH_TOKEN` | - | OAuth token (Pro/Max subscription) |
| `OPENAI_API_KEY` | - | OpenAI API key (OpenCode engine) |
| `DATABASE_URL` | `postgresql://tower:tower@db:5432/tower` | PostgreSQL connection string |
| `POSTGRES_USER` | `tower` | PostgreSQL user |
| `POSTGRES_PASSWORD` | **required** | PostgreSQL password (no default) |
| `POSTGRES_DB` | `tower` | PostgreSQL database |
| `PROFILES_DIR` | `/app/profiles` | Path to TOML profiles directory |
| `TEMPLATES_DIR` | `/app/templates` | Path to Jinja2 templates directory |
| `HOOKS_DIR` | `/app/hooks` | Path to hook scripts directory |
| `ENGINES_DIR` | `/app/engines` | Path to engine TOML configurations directory |
| `MAX_CONCURRENT_JOBS` | `10` | Max parallel worker containers (per Tower instance) |
| `JOB_TTL_HOURS` | `24` | Hours before finished jobs are cleaned up |
| `MAX_RETAINED_JOBS` | `1000` | Max finished jobs kept in DB |
| `WORKER_TIMEOUT_SECONDS` | `3600` | Default container timeout (1 hour) |
| `WORKER_MEM_LIMIT` | `512m` | Default container memory limit |
| `WORKER_CPU_LIMIT` | `1.0` | Default container CPU limit |
| `PROXY_URL` | - | Transparent proxy for workers (must be accessible from worker networks) |
| `MAX_RESULT_SIZE` | `10485760` | Max result.json size in bytes (10 MB default, prevents OOM) |
| `POOL_SIZE` | `3` | Number of warm containers maintained in the pool |
| `POOL_CHECK_INTERVAL` | `10` | Seconds between pool maintenance checks |
| `POOL_MAX_IDLE` | `3600` | Max seconds a container stays idle before being recycled |
| `CLEANUP_INTERVAL` | `600` | Seconds between job cleanup cycles |
| `WEBHOOK_TIMEOUT` | `10` | HTTP timeout in seconds for webhook calls |
| `DB_POOL_MIN_SIZE` | `2` | Minimum DB connections in asyncpg pool |
| `DB_POOL_MAX_SIZE` | `10` | Maximum DB connections in asyncpg pool |
| `WORKER_NET` | `agent-workers` | Docker network name for worker containers |
| `UI_PATH` | `/app/ui/index.html` | Path to the dashboard HTML file |

## Tower API Authentication

Set `TOWER_API_KEY` to require a bearer token on all endpoints except public ones:

- `/health`, `/metrics`, `/docs`, `/openapi.json`, `/engines`, `/profiles`
- Any path starting with `/ui`

```env
TOWER_API_KEY=my-secret-key
```

Then include the header in requests:

```bash
curl -H "Authorization: Bearer my-secret-key" http://localhost:8420/jobs
```

If `TOWER_API_KEY` is empty, the API is open (no auth).

## Engine Authentication

Parpaing handles orchestration, profiles, and infrastructure. You bring your own engine credentials. Each engine defines which env vars it needs in its TOML config (`[env] auth`). At least one must be set for the engine to be available.

### Claude Code

Two options - pick one:

#### Option A: API Key (pay-per-token)

Get a key from [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys).

```env
ANTHROPIC_API_KEY=sk-ant-...
```

#### Option B: OAuth Token (Pro/Max subscription) - Recommended

Use a long-lived OAuth token from your Claude Pro or Max subscription. No per-token billing - you pay only your subscription.

```bash
# Set your long-lived OAuth token
claude set-token
# Paste your token when prompted, then verify
claude -p "hello"
```

```env
CLAUDE_CODE_OAUTH_TOKEN=your-token-here
```

### OpenCode

Requires one of:

```env
ANTHROPIC_API_KEY=sk-ant-...   # uses Anthropic models
# or
OPENAI_API_KEY=sk-...          # uses OpenAI models
```

### Self-hosted (no cost)

With OpenCode and your own model backend, you can run Parpaing entirely self-hosted with no API costs. Configure OpenCode to point to your local inference server.

### Checking availability

```bash
curl http://localhost:8420/engines
# Returns each engine with "available": true/false based on which keys are set
```

## Container Pool

Tower maintains a pool of warm worker containers, ready to execute jobs instantly. All containers share a single `agent-workers` network with inter-container communication disabled (ICC=false), so workers cannot see each other.

### How It Works

1. On startup, Tower fills the pool to `POOL_SIZE` warm containers
2. When a job arrives, Tower acquires a container from the pool (atomic SQL, multi-tower safe)
3. Config is injected via `put_archive` (no shared volumes)
4. The container runs the agent, then Tower extracts the result via `get_archive`
5. Container is destroyed (completely clean; the shared network is kept alive)
6. Pool maintenance loop replenishes the pool automatically

### Configuration

| Variable | Default | Description |
|---|---|---|
| `POOL_SIZE` | `3` | Target number of warm containers. Higher = lower latency, more resources |
| `POOL_CHECK_INTERVAL` | `10` | Seconds between pool fill/prune checks |
| `POOL_MAX_IDLE` | `3600` | Idle containers older than this are recycled (prevents staleness) |

### Resource Usage

Each warm container consumes `WORKER_MEM_LIMIT` memory and `WORKER_CPU_LIMIT` CPU. Total pool overhead = `POOL_SIZE × resources`.

## Worker Hardening

Set `WORKER_HARDENED=true` in production to enable container security:

- `read_only=True` root filesystem (tmpfs for writable paths)
- `cap_drop=["ALL"]` - no Linux capabilities
- `no-new-privileges:true` - prevent privilege escalation
- PID limit 256 - prevents fork bombs

When `WORKER_HARDENED=false` (default), containers run without these restrictions (useful for development).

## Worker Hooks (Optional)

Run custom scripts inside the worker container, before and/or after claude execution. Hooks are defined per-profile and run in the worker's workspace directory.

### Setup

```bash
# 1. Create your hook scripts from examples
cp hooks/pre-job.example.sh hooks/setup.sh
cp hooks/post-job.example.sh hooks/collect.sh
chmod +x hooks/setup.sh hooks/collect.sh

# 2. Reference in your profile
# [hooks]
# pre = "setup.sh"
# post = "collect.sh"
```

### Pre-job hook

Runs before claude starts. Use it to clone repos, download files, or set up the workspace.

### Post-job hook

Runs after claude finishes. Receives env vars:

| Variable | Example |
|---|---|
| `JOB_STATUS` | `completed` or `failed` |
| `JOB_EXIT_CODE` | `0` |

Use it to collect output, cleanup workspace, or create summaries.

## LLM Gateway (Optional)

Route all LLM API calls through a reverse proxy so that real API keys never reach worker containers. Workers get placeholder keys and `*_BASE_URL` overrides pointing to the gateway.

### How it works

```
Worker Container                          Gateway (nginx:alpine)
  ANTHROPIC_BASE_URL=http://gateway:4000    /anthropic/* -> api.anthropic.com/*
  ANTHROPIC_API_KEY=gateway (placeholder)   inject real x-api-key header
  OPENAI_BASE_URL=http://gateway:4000       /openai/* -> api.openai.com/*
  OPENAI_API_KEY=gateway (placeholder)      inject real Authorization header
```

### Setup

1. Enable hardening (required for gateway mode):

```env
WORKER_HARDENED=true
GATEWAY_URL=http://agent-gateway:4000
```

2. Start with the gateway profile:

```bash
docker compose --profile gateway up -d
```

### Configuration

| Variable | Default | Description |
|---|---|---|
| `GATEWAY_URL` | - | Gateway URL (empty = disabled, keys injected directly) |
| `GATEWAY_CONTAINER` | `agent-gateway` | Gateway Docker container name |

### Security

- `WORKER_HARDENED=true` is mandatory when using the gateway (pool refuses to start otherwise)
- `cap_drop=ALL` prevents workers from sniffing traffic on the shared network
- The worker network is set to `internal=true` (no direct internet) with ICC enabled (workers can reach gateway)
- Workers only see placeholder API keys - real keys stay in the gateway container

### Health

The `/health` endpoint includes a `gateway` check when `GATEWAY_URL` is set:

```bash
curl http://localhost:8420/health
# {"status": "ok", "checks": {"db": "ok", "docker": "ok", "pool": "3 ready", "gateway": "ok"}}
```

## Transparent Proxy (Optional)

Route all worker outbound traffic through a proxy. Set `PROXY_URL` to an externally accessible proxy address:

```env
PROXY_URL=http://your-proxy-host:3128
```

Note: the proxy must be reachable from the shared `agent-workers` network (not via Docker internal DNS).

## Horizontal Scaling (Multiple Towers)

Run multiple Tower instances for higher throughput:

```bash
# In .env
TOWER_REPLICAS=3

# Or at runtime
docker compose up --scale tower=3 -d
```

Each Tower instance has its own concurrency semaphore (`MAX_CONCURRENT_JOBS`), so total capacity = `TOWER_REPLICAS × MAX_CONCURRENT_JOBS`.

All instances share the same PostgreSQL database, Docker socket, and container pool. Acquire is atomic (SQL `FOR UPDATE SKIP LOCKED`), so no two Towers grab the same container.

On startup, each Tower checks for orphaned containers (from crashed instances) and either re-adopts them or marks them as failed.

## Zero-Downtime Updates

### Update Tower (no job interruption)

```bash
docker compose build tower
docker compose up -d --no-deps tower
```

On shutdown, each Tower sets a graceful flag - running worker containers are **not** killed. When the new Tower instance starts, `recover_jobs()` re-adopts orphaned containers and collects their output.

### Update Worker (no job interruption)

```bash
docker compose build worker
```

Workers are ephemeral containers. Rebuilding the image only affects **new** pool containers - running workers continue with the old image until they finish naturally.

## Quick Start

```bash
cp .env.example .env
# Edit .env - fill in auth

docker compose up --build -d        # start tower + db
docker compose build worker          # build worker image

curl http://localhost:8420/health    # verify
```
