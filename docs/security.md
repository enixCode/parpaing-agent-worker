# Security

Parpaing runs untrusted AI agents in isolated Docker containers. Security is defense-in-depth - multiple independent layers so that no single failure compromises the system.

## Architecture Overview

```
Internet
  |
  v
Tower (FastAPI) --- auth middleware --- PostgreSQL
  |                                      (parameterized queries)
  v
Container Pool --- cap_drop=ALL, no-new-privileges, pids_limit
  |
  v
Worker (non-root, SUID stripped, dumb-init)
  |
  v
Gateway (nginx) --- real API keys hidden from workers
  |
  v
LLM APIs (Anthropic, OpenAI)
```

Workers have no internet access - they can only reach the gateway on an internal Docker network.

## Authentication

Tower uses bearer token auth via `TOWER_API_KEY`. Comparison uses `hmac.compare_digest()` to prevent timing attacks.

**Public endpoints** (no auth required):

- `/health`, `/metrics`, `/docs`, `/openapi.json`
- `/engines`, `/profiles`
- `/configs` (GET only - mutations require auth)
- `/ui/*`

All other endpoints require `Authorization: Bearer <token>`.

If `TOWER_API_KEY` is empty, the API runs without auth (development only).

**Limitations**: Single shared API key, no per-user RBAC. For multi-tenant deployments, add an auth proxy (e.g., Supabase Auth, OAuth2 proxy) in front of Tower.

## Input Validation

All user input is validated at multiple layers:

### Pydantic (API layer)

| Field | Constraint |
|---|---|
| `agent_id` | `^[a-zA-Z0-9_-]{1,64}$` |
| `engine` | `^[a-zA-Z0-9_-]{1,64}$` |
| `profile` | `^[a-zA-Z0-9_-]{1,64}$` |
| `model` | `^[a-zA-Z0-9._/-]{1,128}$` |
| `output_format` | Enum: `json`, `text`, `stream-json` |
| `max_turns` | 1-100 |
| `max_budget_usd` | 0-50 |
| `prompt` | Max 100 MB, null bytes stripped |
| `system_prompt` | Max 500 KB, null bytes stripped |
| `plugins` | Each validated as safe ID |
| `webhook_url` | Max 2048 chars, http/https only, no internal hosts |
| Config `content` | Max 100 KB |
| Config `name` | Safe ID or template path |

### PostgreSQL (DB layer)

```sql
agent_id text NOT NULL CHECK (agent_id ~ '^[a-zA-Z0-9_-]{1,64}$')
webhook_url text CHECK (webhook_url IS NULL OR length(webhook_url) <= 2048)
```

PostgreSQL-unsafe characters (null bytes, bare surrogates) are stripped before storage.

### SQL Injection

All queries use asyncpg parameterized statements (`$1`, `$2`, ...). No string interpolation in SQL.

## Container Isolation

Every worker container is hardened with these settings (always enabled, not configurable):

| Setting | Value | Purpose |
|---|---|---|
| `cap_drop` | `["ALL"]` | Strip all Linux capabilities |
| `security_opt` | `no-new-privileges:true` | Block privilege escalation (setuid/setgid) |
| `pids_limit` | `100` | Prevent fork bombs |
| `ipc_mode` | `private` | Isolated IPC namespace |
| `mem_limit` | Configurable (default `2g`) | Prevent memory exhaustion |
| `nano_cpus` | Configurable (default `1.0`) | Prevent CPU exhaustion |
| `network` | Internal bridge | No internet access |

### Worker Image Hardening

- **Non-root user**: runs as `agent` (UID 1000)
- **SUID/SGID bits removed**: `find / -perm -4000 -exec chmod u-s {} \;` at build time
- **dumb-init**: proper signal handling and zombie reaping
- **Ephemeral**: container destroyed after each job (completely clean)

### Optional: gVisor

Set `WORKER_RUNTIME=runsc` for kernel-level isolation via gVisor. This intercepts all syscalls and runs them in a user-space kernel, preventing container escape via kernel exploits. Requires gVisor installed on the Docker host.

## Network Isolation

```
agent-net (Tower + DB + Gateway)
  |
  +--- agent-workers (internal=true)
         |
         +--- Worker containers (no internet)
         +--- Gateway container
```

- **`agent-workers`**: `internal=true` - blocks all external traffic. Workers can only communicate with the gateway on this network.
- **`agent-net`**: Tower, DB, and Gateway. Not exposed to workers.
- **ICC**: Enabled on `agent-workers` so workers can reach the gateway.
- **`cap_drop=ALL`**: Prevents workers from sniffing traffic on the shared network.

## Secret Management

Real API keys never reach worker containers:

```
Tower (.env)                  Gateway (nginx)              Worker
  ANTHROPIC_API_KEY=sk-...      proxy_set_header             ANTHROPIC_API_KEY=gateway
  OPENAI_API_KEY=sk-...         x-api-key: $REAL_KEY         ANTHROPIC_BASE_URL=http://gateway:4000
```

1. Tower reads real keys from `.env`
2. Gateway receives real keys via Docker env vars
3. Workers receive placeholder keys (`"gateway"`) and `*_BASE_URL` pointing to the gateway
4. Gateway injects real keys into upstream requests via nginx `proxy_set_header`

**Headers forwarded by gateway**: `anthropic-beta`, `anthropic-version` (required by Anthropic API).

**Supports both**: API key (`x-api-key`) and OAuth token (`Authorization: Bearer`).

## SSRF Prevention

Webhook URLs are validated at two points:

### 1. Request validation (models.py)

- Scheme must be `http` or `https`
- Hostname resolved via DNS - blocks private, loopback, and link-local IPs
- Blocks known internal hostnames: `localhost`, `db`, `tower`, `postgres`
- Max 2048 characters

### 2. Webhook delivery (executor.py)

Before sending the webhook, the hostname is re-validated against internal addresses. This prevents DNS rebinding attacks (where a hostname resolves to a public IP at validation time but to an internal IP at delivery time).

### Gateway URL validation (config.py)

`GATEWAY_URL` is validated at startup:

- Must use `http` or `https`
- Blocks `localhost`, `127.0.0.1`, `::1`, `0.0.0.0`
- Blocks `169.254.*` and `metadata.*` (cloud metadata services)

## Path Traversal Prevention

Hook scripts can be loaded from the `HOOKS_DIR` directory. Path traversal is blocked:

```python
src = (HOOKS_DIR / hook_val).resolve()
if not src.is_relative_to(HOOKS_DIR.resolve()):
    logger.warning("Hook path traversal blocked: %s", hook_val)
    continue
```

Attempts like `../../etc/passwd` are resolved and checked against the hooks directory boundary.

## Config Injection Safety

Config is delivered to workers via Docker `put_archive` (tar):

```
/tmp/config/
  job.json         - prompt, model, engine config
  CLAUDE.md        - rendered template
  settings.json    - plugins config
  mcp.json         - MCP server config
  pre-job.sh       - hook script
  post-job.sh      - hook script
  .ready           - marker (triggers entrypoint, MUST be last)
```

**Safety properties**:

- Single atomic `put_archive` call (all-or-nothing)
- All files owned by UID 1000 (agent user)
- `.ready` marker added last - entrypoint blocks until it appears
- No shell expansion in tar creation
- Entrypoint times out if config never arrives (`CONFIG_TIMEOUT`, default 300s)

## Error Sanitization

Errors returned to clients are sanitized to prevent information leaks:

- Docker internal paths (`/var/lib/docker/...`) - redacted
- Hex strings (64+ chars, potential keys) - redacted
- Docker socket paths - redacted
- Internal app paths (`/app/...`) - redacted
- Docker SDK errors mapped to generic messages (`"Container not found"`, `"Docker error"`)
- Error messages truncated to 500 characters
- Log output truncated to 2000 characters
- Raw results truncated to 5000 characters

## Security Headers

All responses include:

| Header | Value | Purpose |
|---|---|---|
| `X-Content-Type-Options` | `nosniff` | Prevent MIME sniffing |
| `X-Frame-Options` | `DENY` | Prevent clickjacking |
| `Cache-Control` | `no-store` | Prevent caching sensitive data |

For production behind a TLS proxy, consider adding `Strict-Transport-Security` (HSTS) and `Content-Security-Policy` at the reverse proxy level.

## Concurrency Controls

- **Semaphore**: `MAX_CONCURRENT_JOBS` (default 10) per Tower instance limits parallel containers
- **Atomic acquire**: `FOR UPDATE SKIP LOCKED` prevents two Towers from grabbing the same container
- **Result size limit**: `MAX_RESULT_SIZE` (default 10 MB) prevents OOM from oversized results
- **Container timeout**: `WORKER_TIMEOUT_SECONDS` (default 3600s) kills runaway containers
- **PID limit**: 100 processes per container

No request-level rate limiting is built in. For public deployments, add rate limiting at the reverse proxy level (e.g., nginx `limit_req`, Cloudflare).

## Template Rendering

Jinja2 templates are used for prompts and CLAUDE.md files:

- Templates are validated as syntactically correct on create/update
- Undefined variables render as empty (non-strict mode) - prevents crashes from missing vars
- Template variables come from validated profile fields (`prompt_vars`, `claude_md_vars`)
- Templates are admin-controlled (stored in DB, mutations require auth)
- Autoescape is disabled (templates produce markdown/plain text, not HTML)

## Monitoring

### Prometheus metrics (`/metrics`)

- `parpaing_jobs_total` - total jobs by status
- `parpaing_jobs_active` - currently running jobs
- `parpaing_pool_ready` - warm containers available
- `parpaing_job_duration_seconds` - job execution time histogram

### Health endpoint (`/health`)

Deep check covering DB, Docker daemon, container pool, and gateway connectivity.

### Logging

- Auth failures logged with method and path
- Container lifecycle events (create, acquire, release, destroy)
- Job state transitions
- Webhook delivery success/failure
- Path traversal attempts
- SSRF blocks

## Production Deployment Checklist

- [ ] Set a strong `TOWER_API_KEY`
- [ ] Set a strong `POSTGRES_PASSWORD`
- [ ] Set `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` (not both needed)
- [ ] Run behind a TLS-terminating reverse proxy (nginx, Caddy, Cloudflare)
- [ ] Add rate limiting at the reverse proxy level
- [ ] Mount Docker socket as read-only (already default in docker-compose.yml)
- [ ] Review hook scripts in `HOOKS_DIR` (admin-controlled only)
- [ ] Consider `WORKER_RUNTIME=runsc` for gVisor isolation
- [ ] Monitor `/metrics` and `/health` endpoints
- [ ] Set up log aggregation for Tower container logs
- [ ] Regularly update base images (`postgres`, `nginx:alpine`, `node:22-slim`)
- [ ] Pin npm dependency versions in worker Dockerfile for reproducibility

## Security Summary

| Layer | Controls |
|---|---|
| **API** | Bearer auth, HMAC timing-safe, input validation, security headers |
| **Database** | Parameterized queries, schema constraints, no SQL interpolation |
| **Containers** | cap_drop=ALL, no-new-privileges, PID limit, non-root, SUID stripped |
| **Network** | Internal bridge, no internet, gateway-only LLM access |
| **Secrets** | Placeholder keys in workers, real keys in gateway only, error redaction |
| **Webhooks** | SSRF prevention (DNS-aware, dual validation, internal host blocking) |
| **Files** | Path traversal prevention, atomic tar injection, size limits |
| **Monitoring** | Prometheus metrics, health checks, structured logging |
