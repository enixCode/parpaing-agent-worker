"""Tower configuration - environment variables and constants."""

import logging
import os
from pathlib import Path
from urllib.parse import urlparse

import docker

_log = logging.getLogger("tower.config")


def _clamp_int(name: str, val: int, lo: int, hi: int) -> int:
    """Clamp integer config value to [lo, hi], warn if out of bounds."""
    if val < lo or val > hi:
        clamped = max(lo, min(val, hi))
        _log.warning("%s=%d out of bounds [%d, %d], clamped to %d", name, val, lo, hi, clamped)
        return clamped
    return val


def _clamp_float(name: str, val: float, lo: float, hi: float) -> float:
    """Clamp float config value to [lo, hi], warn if out of bounds."""
    if not (lo <= val <= hi):
        clamped = max(lo, min(val, hi))
        _log.warning("%s=%s out of bounds [%s, %s], clamped to %s", name, val, lo, hi, clamped)
        return clamped
    return val

VERSION = "0.3.0"
DEFAULT_MODEL = "claude-sonnet-4-6"
WORKER_IMAGE = os.environ.get("WORKER_IMAGE", "agent-worker-worker")
PROFILES_DIR = Path(os.environ.get("PROFILES_DIR", "/app/profiles"))
TEMPLATES_DIR = Path(os.environ.get("TEMPLATES_DIR", "/app/templates"))
HOOKS_DIR = Path(os.environ.get("HOOKS_DIR", "/app/hooks"))
ENGINES_DIR = Path(os.environ.get("ENGINES_DIR", "/app/engines"))
UI_PATH = Path(os.environ.get("UI_PATH", "/app/ui/index.html"))
WORKER_NET = os.environ.get("WORKER_NET", "agent-workers")

MAX_CONCURRENT_JOBS = _clamp_int("MAX_CONCURRENT_JOBS", int(os.environ.get("MAX_CONCURRENT_JOBS", "10")), 1, 100)
JOB_TTL_HOURS = _clamp_int("JOB_TTL_HOURS", int(os.environ.get("JOB_TTL_HOURS", "24")), 1, 720)
MAX_RETAINED_JOBS = _clamp_int("MAX_RETAINED_JOBS", int(os.environ.get("MAX_RETAINED_JOBS", "1000")), 10, 100000)
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://tower:tower@db:5432/tower")

WORKER_TIMEOUT_SECONDS = _clamp_int("WORKER_TIMEOUT_SECONDS", int(os.environ.get("WORKER_TIMEOUT_SECONDS", "3600")), 10, 86400)
WORKER_MEM_LIMIT = os.environ.get("WORKER_MEM_LIMIT", "2g")
WORKER_CPU_LIMIT = _clamp_float("WORKER_CPU_LIMIT", float(os.environ.get("WORKER_CPU_LIMIT", "1.0")), 0.1, 16.0)

# gVisor runtime for kernel-level isolation (requires gVisor installed on host)
WORKER_RUNTIME = os.environ.get("WORKER_RUNTIME", "")

# Container pool
POOL_SIZE = _clamp_int("POOL_SIZE", int(os.environ.get("POOL_SIZE", "3")), 0, 50)
POOL_CHECK_INTERVAL = _clamp_int("POOL_CHECK_INTERVAL", int(os.environ.get("POOL_CHECK_INTERVAL", "10")), 5, 3600)
POOL_MAX_IDLE = _clamp_int("POOL_MAX_IDLE", int(os.environ.get("POOL_MAX_IDLE", "3600")), 60, 86400)

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

MAX_RESULT_SIZE = _clamp_int("MAX_RESULT_SIZE", int(os.environ.get("MAX_RESULT_SIZE", str(10 * 1024 * 1024))), 1024, 100 * 1024 * 1024)

# Job cleanup
CLEANUP_INTERVAL = _clamp_int("CLEANUP_INTERVAL", int(os.environ.get("CLEANUP_INTERVAL", "600")), 60, 86400)
WEBHOOK_TIMEOUT = _clamp_int("WEBHOOK_TIMEOUT", int(os.environ.get("WEBHOOK_TIMEOUT", "10")), 1, 60)

# DB connection pool - auto-size max to handle concurrent jobs + maintenance
_db_pool_max_default = max(10, MAX_CONCURRENT_JOBS * 2 + 5)
DB_POOL_MIN_SIZE = _clamp_int("DB_POOL_MIN_SIZE", int(os.environ.get("DB_POOL_MIN_SIZE", "2")), 1, 50)
DB_POOL_MAX_SIZE = _clamp_int("DB_POOL_MAX_SIZE", int(os.environ.get("DB_POOL_MAX_SIZE", str(_db_pool_max_default))), DB_POOL_MIN_SIZE, 100)

_docker_client = None

def docker_client():
    """Return (and cache) the Docker client. Lazy to avoid import-time crashes."""
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.from_env()
    return _docker_client
