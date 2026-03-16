# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly:

1. **Do not** open a public issue
2. Email the maintainers with a description of the vulnerability
3. Include steps to reproduce if possible
4. Allow reasonable time for a fix before public disclosure

## Security Model

### Architecture

- **Tower** runs with Docker socket access (`docker.sock`) to manage worker containers
- **Workers** run in isolated Docker containers, destroyed after each job
- **No shared volumes** between Tower and workers — config is injected via `put_archive`, results extracted via `get_archive`

### Hardening (`WORKER_HARDENED=true`)

When enabled, worker containers run with:

- `read_only=True` root filesystem (writable tmpfs only)
- `cap_drop=["ALL"]` — no Linux capabilities
- `no-new-privileges` security option
- `pids_limit=256` — fork bomb protection
- Size-limited tmpfs mounts (`/home/agent` 1G, `/tmp` 512M, `/output` 256M)

### API Authentication

- Bearer token auth via `TOWER_API_KEY` environment variable
- Timing-safe comparison (`hmac.compare_digest`)
- Public endpoints: `/health`, `/engines`, `/profiles`, `/docs`, `/ui`

### Protections

- **SSRF**: Webhook URLs validated against internal hosts at request time and before firing (DNS rebinding defense)
- **Path traversal**: Profile loading and hook injection validated with `Path.is_relative_to()`
- **Input validation**: Agent ID, profile, and plugin names restricted to `^[a-zA-Z0-9_-]{1,64}$`
- **Error sanitization**: Internal paths and Docker details stripped from error responses
- **Result size limits**: `MAX_RESULT_SIZE` prevents oversized container output from causing OOM

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |
