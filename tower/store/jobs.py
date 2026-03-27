"""PostgreSQL job store - persists jobs across restarts."""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from ..models import JobResponse, AgentRunRequest

import asyncpg

from ..config import DB_POOL_MIN_SIZE, DB_POOL_MAX_SIZE

logger = logging.getLogger("tower.job_store")

_REQUEST_FIELDS = set(AgentRunRequest.model_fields.keys())


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    job_id: str
    status: JobStatus
    request: AgentRunRequest
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    container_id: str | None = None
    exit_code: int | None = None
    result: dict | None = None
    error: str | None = None
    webhook_url: str | None = None

    def to_response(self) -> JobResponse:
        "Turn the Job into a response model for the API client."
        return JobResponse(
        job_id=self.job_id, status=self.status,
        engine=self.request.engine,
        profile=self.request.profile,
        created_at=self.created_at,
        started_at=self.started_at, finished_at=self.finished_at,
        exit_code=self.exit_code, result=self.result, error=self.error,
    )

def _parse_json(val, default):
    """Parse a JSONB value that asyncpg may return as str."""
    if val is None:
        return default
    if isinstance(val, str):
        return json.loads(val)
    return val


def _row_to_job(row: asyncpg.Record) -> Job:
    """Convert a DB row to a Job dataclass."""
    request_data = _parse_json(row["request"], {})
    valid = {k: v for k, v in request_data.items() if k in _REQUEST_FIELDS}
    return Job(
        job_id=row["job_id"],
        status=JobStatus(row["status"]),
        request=AgentRunRequest(**valid),
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        container_id=row["container_id"],
        exit_code=row["exit_code"],
        result=_parse_json(row["result"], None),
        error=row["error"],
        webhook_url=row["webhook_url"],
    )


class JobStore:
    def __init__(self, dsn: str, ttl_hours: int = 24, max_retained: int = 1000):
        self._dsn = dsn
        self._ttl_hours = ttl_hours
        self._max_retained = max_retained
        self._pool: asyncpg.Pool | None = None

    async def connect(self):
        """Initialize connection pool."""
        self._pool = await asyncpg.create_pool(self._dsn, min_size=DB_POOL_MIN_SIZE, max_size=DB_POOL_MAX_SIZE)
        logger.info("Connected to PostgreSQL")

    async def ensure_schema(self, schema_path: Path):
        """Execute schema file (idempotent - safe to re-run)."""
        sql = schema_path.read_text()
        await self._pool.execute(sql)
        logger.info("Schema verified")

    @property
    def db_pool(self) -> asyncpg.Pool:
        """Expose the DB connection pool for sharing."""
        return self._pool

    async def close(self):
        if self._pool:
            await self._pool.close()

    async def create(self, job_id: str, request: AgentRunRequest,
                     webhook_url: str | None = None) -> Job:
        request_data = request.model_dump(include=_REQUEST_FIELDS)
        await self._pool.execute(
            """INSERT INTO jobs (job_id, agent_id, status, request, webhook_url)
               VALUES ($1, $2, $3, $4, $5)""",
            job_id, request.agent_id, JobStatus.PENDING.value,
            json.dumps(request_data), webhook_url,
        )
        return Job(
            job_id=job_id, status=JobStatus.PENDING,
            request=request, webhook_url=webhook_url,
        )

    async def get(self, job_id: str) -> Job | None:
        row = await self._pool.fetchrow("SELECT * FROM jobs WHERE job_id = $1", job_id)
        if not row:
            return None
        return _row_to_job(row)

    async def start_job(self, job_id: str, started_at: datetime) -> bool:
        """Atomically mark job as running (only if pending)."""
        row = await self._pool.fetchval(
            """UPDATE jobs SET status = 'running', started_at = $1
               WHERE job_id = $2 AND status = 'pending'
               RETURNING job_id""",
            started_at, job_id,
        )
        return row is not None

    async def set_container(self, job_id: str, container_id: str) -> bool:
        """Attach container ID (only if still running)."""
        row = await self._pool.fetchval(
            """UPDATE jobs SET container_id = $1
               WHERE job_id = $2 AND status = 'running'
               RETURNING job_id""",
            container_id, job_id,
        )
        return row is not None

    async def finish_job(self, job_id: str, status: JobStatus, *,
                         finished_at: datetime | None = None,
                         result: dict | None = None,
                         exit_code: int | None = None,
                         error: str | None = None) -> bool:
        """Atomically finish job (only if still pending or running)."""
        if finished_at is None:
            finished_at = datetime.now(timezone.utc)
        row = await self._pool.fetchval(
            """UPDATE jobs SET status = $1, finished_at = $2,
               result = $3, exit_code = $4, error = $5
               WHERE job_id = $6 AND status IN ('pending', 'running')
               RETURNING job_id""",
            status.value, finished_at,
            json.dumps(result) if result is not None else None, exit_code, error,
            job_id,
        )
        return row is not None

    async def list_all(self, status_filter: str | None = None, limit: int = 50, offset: int = 0) -> tuple[list[Job], int]:
        """List jobs with pagination. Returns (jobs, total_count)."""
        if status_filter and status_filter not in {s.value for s in JobStatus}:
            raise ValueError(f"Invalid status filter. Must be one of: {', '.join(s.value for s in JobStatus)}")
        if status_filter:
            total = await self._pool.fetchval(
                "SELECT count(*) FROM jobs WHERE status = $1", status_filter
            )
            rows = await self._pool.fetch(
                "SELECT * FROM jobs WHERE status = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
                status_filter, limit, offset
            )
        else:
            total = await self._pool.fetchval("SELECT count(*) FROM jobs")
            rows = await self._pool.fetch(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT $1 OFFSET $2", limit, offset
            )
        return [_row_to_job(r) for r in rows], total

    async def cleanup_old(self) -> int:
        """Delete finished jobs older than TTL, cap total retained. Atomic."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # TTL cleanup
                result = await conn.execute(
                    """DELETE FROM jobs
                       WHERE status IN ('completed', 'failed', 'cancelled')
                       AND finished_at < now() - make_interval(hours => $1)""",
                    self._ttl_hours,
                )
                # asyncpg execute() returns PostgreSQL command tag e.g. "DELETE 5"
                ttl_removed = int(result.split()[-1]) if result else 0

                # Cap: single atomic DELETE with OFFSET
                result = await conn.execute(
                    """DELETE FROM jobs WHERE job_id IN (
                           SELECT job_id FROM jobs
                           WHERE status IN ('completed', 'failed', 'cancelled')
                           ORDER BY finished_at DESC NULLS LAST
                           OFFSET $1
                       )""",
                    self._max_retained,
                )
                cap_removed = int(result.split()[-1]) if result else 0

        return ttl_removed + cap_removed

    async def get_running_jobs(self) -> list[tuple[str, str | None]]:
        """Return (job_id, container_id) pairs for all running jobs."""
        rows = await self._pool.fetch(
            "SELECT job_id, container_id FROM jobs WHERE status = 'running'"
        )
        return [(r["job_id"], r["container_id"]) for r in rows]

    async def get_pending_jobs(self) -> list[str]:
        """Return job_ids for all pending jobs (orphaned after crash)."""
        rows = await self._pool.fetch(
            "SELECT job_id FROM jobs WHERE status = 'pending' ORDER BY created_at"
        )
        return [r["job_id"] for r in rows]

