<p align="center">
  <img src="docs/banners/hero-banner.svg" alt="Parpaing" width="100%"/>
</p>

<h1 align="center">Parpaing</h1>

<p align="center">
  <strong>The building block for AI agents.</strong><br/>
  Run any AI coding agent in isolated Docker containers via a simple REST API.
</p>

<p align="center">
  <a href="docker-compose.yml"><img src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white" alt="Docker"/></a>
  <a href="tower/"><img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python"/></a>
  <a href="tower/main.py"><img src="https://img.shields.io/badge/FastAPI-0.135-009688?logo=fastapi&logoColor=white" alt="FastAPI"/></a>
  <a href="db/init.sql"><img src="https://img.shields.io/badge/PostgreSQL-17-4169E1?logo=postgresql&logoColor=white" alt="PostgreSQL"/></a>
  <a href="engines/"><img src="https://img.shields.io/badge/Multi--Engine-Claude_Code_%7C_OpenCode-cc785c?logo=anthropic&logoColor=white" alt="Engines"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0-blue.svg" alt="License"/></a>
</p>

Send a prompt, get structured results. Parpaing handles container orchestration, job queuing, and result collection. Use it standalone or as the execution backend behind your own SaaS.

> **Status: MVP** - Core features work (job queue, container pool, dashboard, multi-engine). Not production-hardened yet. See [Roadmap](#roadmap) for what's planned.
>
> **What works:** create/poll/cancel/wait jobs, profiles, web dashboard, multi-tower scaling.
>
> **What's missing for production:** multi-tenant auth, rate limiting, file upload, cost tracking, CI/CD.

## Architecture

<p align="center">
  <img src="docs/architecture.svg" alt="Architecture" width="100%"/>
</p>

## Quick Start

```bash
cp .env.example .env
# → set POSTGRES_PASSWORD and at least one engine auth key (see .env.example)

docker compose up --build -d

# Open dashboard
open http://localhost:8420/ui
```

<p align="center">
  <img src="docs/assets/dashboard.png" alt="Dashboard" width="100%"/>
</p>

## Usage

### Python

```python
import requests

API = "http://localhost:8420"

# Create a job and wait for the result (blocking)
r = requests.post(f"{API}/jobs", json={
    "agent_id": "test",
    "engine": "claude-code",
    "prompt": "List files in /workspace",
})
job_id = r.json()["job_id"]

# Wait for completion (blocks until done, timeout 1h)
result = requests.get(f"{API}/jobs/{job_id}/wait").json()
print(result["status"])   # completed / failed
print(result["result"])   # agent output

```

```python
# Use a profile with variables
r = requests.post(f"{API}/jobs", json={
    "agent_id": "search",
    "engine": "claude-code",
    "profile": "researcher",
    "prompt_vars": {"query": "best CI tools"},
})
result = requests.get(f"{API}/jobs/{r.json()['job_id']}/wait").json()
```

### curl

```bash
# Create + wait
JOB_ID=$(curl -s -X POST http://localhost:8420/jobs \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"test","engine":"claude-code","prompt":"Hello"}' | jq -r .job_id)

curl http://localhost:8420/jobs/$JOB_ID/wait    # blocks until done

# Cancel
curl -X DELETE http://localhost:8420/jobs/$JOB_ID
```

## API

See [docs/api.md](docs/api.md) for full reference and [docs/request.md](docs/request.md) for request fields.

Interactive docs available at [/docs](http://localhost:8420/docs) when running.

## Profiles

Profiles are reusable agent configurations in TOML. They define the model, tools, prompt template, hooks, and resource limits.

<p align="center">
  <img src="docs/assets/profiles.svg" alt="Profile System" width="100%"/>
</p>

| Profile | Purpose |
|---------|---------|
| `default` | General-purpose agent |
| `researcher` | Deep research with web search |
| `code-review` | Code quality analysis |

No profile needed for simple jobs - `default` is used automatically.

Discover profiles and their variables via `GET /profiles`.

See [`docs/profiles.md`](docs/profiles.md) and [`docs/templates.md`](docs/templates.md).

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `WORKER_IMAGE` | `agent-worker-worker` | Worker Docker image |
| `TOWER_API_KEY` | - | Bearer token for API auth |
| `ANTHROPIC_API_KEY` | - | Anthropic API key (Claude Code / OpenCode) |
| `CLAUDE_CODE_OAUTH_TOKEN` | - | Claude OAuth token (Pro/Max plan) |
| `OPENAI_API_KEY` | - | OpenAI API key (OpenCode) |
| `TOWER_REPLICAS` | `1` | Tower instances (Docker replicas) |
| `MAX_CONCURRENT_JOBS` | `10` | Max parallel jobs per Tower |
| `POOL_SIZE` | `3` | Warm containers in pool |
| `WORKER_MEM_LIMIT` | `512m` | Memory per worker container |
| `WORKER_CPU_LIMIT` | `1.0` | CPUs per worker container |
| `WORKER_HARDENED` | `false` | Container hardening (read_only, cap_drop, pids_limit) |
| `WORKER_TIMEOUT_SECONDS` | `3600` | Max job duration |
| `PROXY_URL` | - | HTTP proxy for worker internet access |

### Engine Authentication

Parpaing handles orchestration, profiles, and infrastructure - you bring your own engine credentials.

| Engine | Auth | Cost model |
|--------|------|------------|
| **Claude Code** | `ANTHROPIC_API_KEY` (pay-per-token) or `CLAUDE_CODE_OAUTH_TOKEN` (Pro/Max subscription) | API usage or flat subscription |
| **OpenCode** | `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` | API usage |

**Recommended for Claude Code** - use a long-lived OAuth token from your Pro/Max subscription:

```bash
claude set-token
# Paste your OAuth token when prompted - it's stored for CLI use
# Verify it works
claude -p "hello"
```

Then set `CLAUDE_CODE_OAUTH_TOKEN` in `.env`. No per-token billing - you pay only your subscription.

Check which engines are available: `GET /engines`.

## Production

Set in `.env`:
```bash
TOWER_API_KEY=your-strong-random-token
POSTGRES_PASSWORD=strong-password
WORKER_HARDENED=true
```

Hardening enables: `read_only` root filesystem, `cap_drop=ALL`, `no-new-privileges`, `pids_limit=256`, tmpfs mounts.

Scale: `TOWER_REPLICAS=3 docker compose up -d`. Add TLS with a reverse proxy (Traefik, Caddy) in front of Tower.

## Project Structure

```
agent-worker/
├── tower/                 # Orchestrator (FastAPI)
│   ├── main.py            #   Routes, auth, health check
│   ├── config.py          #   Environment variables
│   ├── models.py          #   Request/response validation
│   ├── profiles.py        #   Profile loading + template rendering
│   ├── engines.py         #   Engine loading + availability
│   ├── pool.py            #   Warm container pool (DB-backed)
│   ├── worker.py          #   Config injection + result extraction
│   ├── job_store.py       #   PostgreSQL persistence
│   └── job_runner.py      #   Job execution + webhook
├── worker/                # Agent container
│   ├── Dockerfile         #   Node.js 22 + engine binaries (Claude Code, OpenCode)
│   ├── entrypoint.sh      #   Waits for config, runs job
│   ├── run-job.sh         #   Hooks + engine execution
│   └── parse-job.js       #   JSON → shell variables
├── profiles/              # Agent profiles (TOML)
├── templates/             # Jinja2 templates (prompts, agent instructions)
├── hooks/                 # Pre/post hook scripts
├── db/                    # PostgreSQL schema
├── tests/                 # E2E tests
├── docs/                  # Documentation
├── ui/                    # Web dashboard
└── docker-compose.yml
```

## Scaling

```bash
# Multiple Tower instances
TOWER_REPLICAS=3 docker compose up -d

# All replicas share PostgreSQL - any Tower can serve any job
# Container pool is DB-backed - atomic acquire, no race conditions
# Cancel works cross-instance via Docker socket
```

## Roadmap

- [ ] **Profiles, prompts & hooks in DB** - manage via API instead of TOML files

- [ ] **Multi-tenant auth** - users, orgs, quotas, scoped API keys

- [ ] **File upload** - inject files into `/workspace` via API

- [ ] **More engines** - Codex, Gemini CLI, Aider, etc.

- [ ] **WebSocket push** - real-time status changes without polling

- [ ] **Cost tracking** - tokens & USD per job, aggregated metrics

- [ ] **Scheduling** - cron / recurring jobs

- [ ] **CI/CD** - automated build, test, deploy pipeline

## License

Parpaing is licensed under the [GNU Affero General Public License v3.0](LICENSE). If you deploy a modified version as a network service, you must make the source code available to its users.
