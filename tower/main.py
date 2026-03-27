"""Tower - spawns Claude Code agent containers, returns results."""

import asyncio
import hmac
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from prometheus_client import generate_latest

import httpx

import io
import tomllib

import jinja2

from .config import (
    VERSION, MAX_CONCURRENT_JOBS, JOB_TTL_HOURS, MAX_RETAINED_JOBS,
    DATABASE_URL, TOWER_API_KEY, WORKER_TIMEOUT_SECONDS, UI_PATH,
    GATEWAY_URL, PROFILES_DIR, ENGINES_DIR, TEMPLATES_DIR, docker_client,
)
from .store import JobStore, JobStatus, ContainerPool, ConfigStore
from .runner import execute_job, recover_jobs, cleanup_loop
from .models import (
    JobCreateRequest, JobCreateResponse, JobResponse,
    ConfigCreateRequest, ConfigUpdateRequest, ConfigResponse, ConfigListItem,
    _VALID_CONFIG_TYPES,
)
from .engines import list_engines as _list_engines, load_engine, is_engine_available
from .engines import invalidate_caches as _invalidate_engine_caches
from .profiles import list_profiles as _list_profiles, _load_profile
from .profiles import invalidate_caches as _invalidate_profile_caches
from .metrics import JOBS_TOTAL, JOBS_ACTIVE, JOBS_BY_STATUS, POOL_READY, JOB_DURATION


_log = logging.getLogger("tower")
_log.setLevel(logging.INFO)
if not _log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    _log.addHandler(_h)
logger = _log

store = JobStore(dsn=DATABASE_URL, max_retained=MAX_RETAINED_JOBS, ttl_hours=JOB_TTL_HOURS)
pool = ContainerPool()
semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

_MAX_LIST_LIMIT = 200
_WAIT_MAX_TIMEOUT = 7200
_WAIT_POLL_INTERVAL = 2


# --- Lifecycle ---

@asynccontextmanager
async def lifespan(app):
    """Startup and shutdown lifecycle."""
    await store.connect()
    await ConfigStore.init(store.db_pool)
    await ConfigStore.instance().seed_from_files(PROFILES_DIR, ENGINES_DIR, TEMPLATES_DIR)
    await pool.start(store.db_pool)
    await recover_jobs(store, semaphore, pool)
    cleanup_task = asyncio.create_task(cleanup_loop(store))
    yield
    logger.info("Graceful shutdown - leaving running containers for re-adoption")
    cleanup_task.cancel()
    await pool.shutdown()
    await store.close()


_AUTH_PUBLIC = {"/", "/health", "/metrics", "/docs", "/openapi.json", "/engines", "/profiles", "/configs"}

app = FastAPI(title="Parpaing", version=VERSION, docs_url=None, redoc_url=None, lifespan=lifespan)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/ui")


@app.get("/docs", include_in_schema=False)
async def scalar_docs():
    return HTMLResponse("""<!doctype html>
<html>
<head><title>Parpaing API</title><meta charset="utf-8"/></head>
<body>
<script id="api-reference" data-url="/openapi.json"
  data-configuration='{"theme":"kepler","layout":"modern"}'></script>
<script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>
</body>
</html>""")


@app.get("/ui", include_in_schema=False)
async def ui_dashboard():
    path = UI_PATH
    if not path.exists():
        raise HTTPException(404, "Dashboard not available")
    return HTMLResponse(path.read_text(encoding="utf-8"))


def _custom_openapi():
    """Inject dynamic engine/profile enums into OpenAPI schema."""
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)

    engine_names = [e["id"] for e in _list_engines()]
    profile_names = [p["name"] for p in _list_profiles()]

    schemas = schema.get("components", {}).get("schemas", {})
    props = schemas.get("JobCreateRequest", {}).get("properties", {})
    if "engine" in props and engine_names:
        props["engine"]["enum"] = engine_names
    if "profile" in props and profile_names:
        props["profile"]["enum"] = profile_names

    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi


# --- Validation errors ---

@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """Return human-readable validation errors instead of raw Pydantic JSON."""
    messages = []
    for err in exc.errors():
        field = ".".join(str(p) for p in err["loc"] if p != "body")
        msg = err["msg"].removeprefix("Value error, ")
        value = err.get("input")
        text = f"'{field}': {msg}"
        if value is not None:
            text += f" (got: {value!r})"
        messages.append(text)
    detail = "; ".join(messages) if len(messages) > 1 else messages[0]
    return JSONResponse(status_code=422, content={"error": detail})


# --- Middleware ---

@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Add security headers to all responses."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Bearer token auth on all endpoints except public ones.

    /configs is public for GET only - mutations require auth.
    """
    path = request.url.path
    is_public = path in _AUTH_PUBLIC or path.startswith("/ui")
    # /configs/* GET is public, but POST/PUT/DELETE require auth
    if path.startswith("/configs") and request.method == "GET":
        is_public = True
    if TOWER_API_KEY and not is_public:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(token, TOWER_API_KEY):
            logger.warning("Auth failed: %s %s", request.method, path)
            return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key"})
    return await call_next(request)


# --- Health & profiles ---

@app.get("/health")
async def health():
    """Deep health check: DB connectivity + Docker socket."""
    checks = {"db": "ok", "docker": "ok", "pool": "ok"}
    healthy = True

    try:
        await store.db_pool.fetchval("SELECT 1")
    except Exception:
        checks["db"] = "unavailable"
        healthy = False
        logger.warning("Health check: DB unavailable")

    try:
        docker_client().ping()
    except Exception:
        checks["docker"] = "unavailable"
        healthy = False
        logger.warning("Health check: Docker unavailable")

    try:
        pool_ready = await store.db_pool.fetchval(
            "SELECT count(*) FROM containers WHERE status = 'ready'"
        )
        checks["pool"] = f"{pool_ready} ready"
    except Exception:
        checks["pool"] = "unknown"

    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(f"{GATEWAY_URL}/health")
            checks["gateway"] = "ok" if r.status_code == 200 else "unavailable"
    except Exception:
        checks["gateway"] = "unavailable"
        healthy = False

    status_code = 200 if healthy else 503
    return JSONResponse(
        content={"status": "ok" if healthy else "degraded", "checks": checks},
        status_code=status_code,
    )


@app.get("/engines")
def list_engines_endpoint():
    return {"engines": _list_engines()}


@app.get("/profiles")
def list_profiles_endpoint():
    return {"profiles": _list_profiles()}


# --- Config CRUD ---

def _invalidate_all_caches():
    """Clear parsed caches in profiles and engines after config mutations."""
    _invalidate_profile_caches()
    _invalidate_engine_caches()
    # Reset OpenAPI schema so engine/profile enums get refreshed
    app.openapi_schema = None


def _validate_config_content(config_type: str, content: str):
    """Validate content is parseable for the given type."""
    if config_type == "profile" or config_type == "engine":
        try:
            tomllib.load(io.BytesIO(content.encode("utf-8")))
        except Exception as e:
            raise HTTPException(422, f"Invalid TOML: {e}")
    elif config_type == "template":
        try:
            jinja2.Template(content)
        except jinja2.TemplateSyntaxError as e:
            raise HTTPException(422, f"Invalid Jinja2 template: {e}")


@app.get("/configs")
def list_configs(type: str | None = None):
    """List configs, optionally filtered by type."""
    cs = ConfigStore.instance()
    if type:
        if type not in _VALID_CONFIG_TYPES:
            raise HTTPException(422, f"Invalid type. Must be one of: {', '.join(_VALID_CONFIG_TYPES)}")
        return {"configs": cs.list_by_type(type)}
    return {"configs": cs.list_all()}


@app.get("/configs/{config_type}/{name:path}")
def get_config(config_type: str, name: str):
    """Get full config content by type and name."""
    if config_type not in _VALID_CONFIG_TYPES:
        raise HTTPException(422, f"Invalid type. Must be one of: {', '.join(_VALID_CONFIG_TYPES)}")
    entry = ConfigStore.instance().get_full(name, config_type)
    if not entry:
        raise HTTPException(404, f"Config not found: {config_type}/{name}")
    return entry


@app.post("/configs/{config_type}", status_code=201)
async def create_config(config_type: str, req: ConfigCreateRequest):
    """Create a new config."""
    if config_type not in _VALID_CONFIG_TYPES:
        raise HTTPException(422, f"Invalid type. Must be one of: {', '.join(_VALID_CONFIG_TYPES)}")
    _validate_config_content(config_type, req.content)
    try:
        entry = await ConfigStore.instance().create(req.name, config_type, req.content, req.description)
    except ValueError as e:
        raise HTTPException(409, str(e))
    _invalidate_all_caches()
    logger.info("Config created: %s/%s", config_type, req.name)
    return entry


@app.put("/configs/{config_type}/{name:path}")
async def update_config(config_type: str, name: str, req: ConfigUpdateRequest):
    """Update an existing config."""
    if config_type not in _VALID_CONFIG_TYPES:
        raise HTTPException(422, f"Invalid type. Must be one of: {', '.join(_VALID_CONFIG_TYPES)}")
    _validate_config_content(config_type, req.content)
    try:
        entry = await ConfigStore.instance().update(name, config_type, req.content, req.description)
    except KeyError as e:
        raise HTTPException(404, str(e))
    _invalidate_all_caches()
    logger.info("Config updated: %s/%s", config_type, name)
    return entry


@app.delete("/configs/{config_type}/{name:path}")
async def delete_config(config_type: str, name: str):
    """Delete a config."""
    if config_type not in _VALID_CONFIG_TYPES:
        raise HTTPException(422, f"Invalid type. Must be one of: {', '.join(_VALID_CONFIG_TYPES)}")
    try:
        await ConfigStore.instance().delete(name, config_type)
    except KeyError as e:
        raise HTTPException(404, str(e))
    _invalidate_all_caches()
    logger.info("Config deleted: %s/%s", config_type, name)
    return {"deleted": f"{config_type}/{name}"}


@app.get("/metrics", include_in_schema=False)
async def metrics():
    """Prometheus metrics endpoint."""
    try:
        pool_ready = await store.db_pool.fetchval(
            "SELECT count(*) FROM containers WHERE status = 'ready'"
        )
        POOL_READY.set(pool_ready)
        rows = await store.db_pool.fetch(
            "SELECT status, count(*) AS cnt FROM jobs GROUP BY status"
        )
        active = 0
        for row in rows:
            JOBS_BY_STATUS.labels(status=row["status"]).set(row["cnt"])
            if row["status"] == "running":
                active = row["cnt"]
        JOBS_ACTIVE.set(active)
    except Exception:
        pass
    return PlainTextResponse(generate_latest(), media_type="text/plain; version=0.0.4")


# --- Async job queue ---

@app.post("/jobs", status_code=202)
async def create_job(req: JobCreateRequest) -> JobCreateResponse:
    engine = load_engine(req.engine)
    if engine is None:
        raise HTTPException(422, f"Engine not found: {req.engine}")
    if not is_engine_available(engine):
        raise HTTPException(422, f"Engine '{req.engine}' not available - set one of: {', '.join(engine.env_auth)}")
    if req.profile != "default" and _load_profile(req.profile) is None:
        raise HTTPException(422, f"Profile not found: {req.profile}")

    job_id = f"{req.agent_id}-{uuid.uuid4().hex[:12]}"
    await store.create(job_id, req, req.webhook_url)
    JOBS_TOTAL.labels(profile=req.profile).inc()
    logger.info("Job %s created (engine=%s, profile=%s, agent=%s)", job_id, req.engine, req.profile, req.agent_id)
    asyncio.create_task(execute_job(job_id, store, semaphore, pool, dry_run=req.dry_run))
    return JobCreateResponse(job_id=job_id, status="pending")


@app.get("/jobs")
async def list_jobs(status: str | None = None, limit: int = 50, offset: int = 0):
    if limit < 1 or limit > _MAX_LIST_LIMIT:
        raise HTTPException(422, f"limit must be between 1 and {_MAX_LIST_LIMIT}")
    if offset < 0:
        raise HTTPException(422, "offset must be >= 0")
    try:
        jobs, total = await store.list_all(status_filter=status, limit=limit, offset=offset)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {
        "jobs": [j.to_response() for j in jobs],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/jobs/{job_id}")
async def get_job(job_id: str) -> JobResponse:
    job = await store.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job.to_response()


@app.delete("/jobs/{job_id}")
async def cancel_job(job_id: str):
    """Cancel a job: mark cancelled in DB, then kill + release container."""
    job = await store.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    cancelled = await store.finish_job(job_id, JobStatus.CANCELLED)
    if not cancelled:
        # Re-fetch to get current status (avoid stale data)
        job = await store.get(job_id)
        current = job.status.value if job and hasattr(job.status, "value") else (job.status if job else "unknown")
        raise HTTPException(409, f"Job already {current}")

    # Re-fetch after atomic update to get current container_id
    job = await store.get(job_id)
    if job and job.container_id:
        await pool.release(job.container_id)

    logger.info("Job %s cancelled", job_id)
    return {"job_id": job_id, "status": "cancelled"}


@app.get("/jobs/{job_id}/wait")
async def wait_job(job_id: str, timeout: int = WORKER_TIMEOUT_SECONDS):
    """Block until job finishes. Returns the final job result."""
    job = await store.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    timeout = min(max(timeout, 1), _WAIT_MAX_TIMEOUT)
    deadline = asyncio.get_running_loop().time() + timeout

    while job.status in (JobStatus.PENDING, JobStatus.RUNNING):
        if asyncio.get_running_loop().time() > deadline:
            raise HTTPException(408, "Wait timed out")
        await asyncio.sleep(_WAIT_POLL_INTERVAL)
        job = await store.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")

    return job.to_response()
