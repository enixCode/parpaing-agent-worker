"""Tower configuration - environment variables and constants."""

import os
from pathlib import Path
from urllib.parse import urlparse

import docker

VERSION = "0.3.0"
DEFAULT_MODEL = "claude-sonnet-4-6"
WORKER_IMAGE = os.environ.get("WORKER_IMAGE", "agent-worker-worker")
PROFILES_DIR = Path(os.environ.get("PROFILES_DIR", "/app/profiles"))
TEMPLATES_DIR = Path(os.environ.get("TEMPLATES_DIR", "/app/templates"))
HOOKS_DIR = Path(os.environ.get("HOOKS_DIR", "/app/hooks"))
ENGINES_DIR = Path(os.environ.get("ENGINES_DIR", "/app/engines"))
UI_PATH = Path(os.environ.get("UI_PATH", "/app/ui/index.html"))
WORKER_NET = os.environ.get("WORKER_NET", "agent-workers")

MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "10"))
JOB_TTL_HOURS = int(os.environ.get("JOB_TTL_HOURS", "24"))
MAX_RETAINED_JOBS = int(os.environ.get("MAX_RETAINED_JOBS", "1000"))
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://tower:tower@db:5432/tower")

WORKER_TIMEOUT_SECONDS = int(os.environ.get("WORKER_TIMEOUT_SECONDS", "3600"))
WORKER_MEM_LIMIT = os.environ.get("WORKER_MEM_LIMIT", "2g")
WORKER_CPU_LIMIT = float(os.environ.get("WORKER_CPU_LIMIT", "1.0"))

# gVisor runtime for kernel-level isolation (requires gVisor installed on host)
WORKER_RUNTIME = os.environ.get("WORKER_RUNTIME", "")

# Container pool
POOL_SIZE = int(os.environ.get("POOL_SIZE", "3"))
POOL_CHECK_INTERVAL = int(os.environ.get("POOL_CHECK_INTERVAL", "10"))
POOL_MAX_IDLE = int(os.environ.get("POOL_MAX_IDLE", "3600"))

# Tower API authentication (empty = no auth)
TOWER_API_KEY = os.environ.get("TOWER_API_KEY", "")

# LLM Gateway - hides API keys from worker containers (always enabled)
_gateway_url_raw = os.environ.get("GATEWAY_URL", "http://agent-gateway:4000")

def _validate_gateway_url(url: str) -> str:
    """Validate GATEWAY_URL to prevent SSRF: only http/https, no localhost/internal hosts."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"GATEWAY_URL must use http or https scheme, got: {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    _blocked = ("localhost", "127.0.0.1", "::1", "0.0.0.0")
    if host in _blocked:
        raise ValueError(f"GATEWAY_URL host {host!r} is not allowed (SSRF protection)")
    if host.startswith("169.254.") or host.startswith("metadata."):
        raise ValueError(f"GATEWAY_URL host {host!r} is not allowed (SSRF protection)")
    return url

GATEWAY_URL = _validate_gateway_url(_gateway_url_raw)
GATEWAY_CONTAINER = os.environ.get("GATEWAY_CONTAINER", "agent-gateway")

MAX_RESULT_SIZE = int(os.environ.get("MAX_RESULT_SIZE", str(10 * 1024 * 1024)))  # 10 MB

# Job cleanup
CLEANUP_INTERVAL = int(os.environ.get("CLEANUP_INTERVAL", "600"))  # seconds
WEBHOOK_TIMEOUT = int(os.environ.get("WEBHOOK_TIMEOUT", "10"))  # seconds

# DB connection pool
DB_POOL_MIN_SIZE = int(os.environ.get("DB_POOL_MIN_SIZE", "2"))
DB_POOL_MAX_SIZE = int(os.environ.get("DB_POOL_MAX_SIZE", "10"))

_docker_client = None

def docker_client():
    """Return (and cache) the Docker client. Lazy to avoid import-time crashes."""
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.from_env()
    return _docker_client
