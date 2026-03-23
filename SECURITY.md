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
- **No shared volumes** between Tower and workers - config is injected via `put_archive`, results extracted via `get_archive`

### Container Hardening (always enabled)

All worker containers run with security hardening:

- `cap_drop=["ALL"]` - no Linux capabilities
- `no-new-privileges:true` - prevent privilege escalation
- `pids_limit=100` - fork bomb protection
- `ipc_mode="private"` - isolated IPC namespace
- Internal network only (no direct internet access)
- Internal network (no internet access, gateway-only)
- Optional gVisor kernel-level isolation via `WORKER_RUNTIME=runsc`

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
- **XSS prevention**: Dashboard uses `escapeHtml()` for user-controlled content, API key stored in sessionStorage (not localStorage)
- **Null-byte stripping**: Worker `parse-job.js` strips null bytes and truncates oversized shell arguments
- **Config clamping**: All numeric env vars are auto-clamped to valid ranges at startup, preventing misconfiguration

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.3.x   | Yes       |
