"""Background job execution - acquires containers, runs agents, updates job store."""

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import docker.errors
import httpx

from ..config import WORKER_TIMEOUT_SECONDS, CLEANUP_INTERVAL, WEBHOOK_TIMEOUT
from ..store import JobStore, JobStatus
from ..models import is_internal_host
from ..store import ContainerPool
from ..profiles import resolve_config
from .worker import (
    inject_config, extract_result, extract_stderr,
    get_container,
)

logger = logging.getLogger("tower.job_runner")

# --- Error sanitization ---

_SENSITIVE_PATTERNS = re.compile(
    r"/var/lib/docker/[^\s\"']*"
    r"|[0-9a-f]{64}"
    r"|unix:///var/run/docker\.sock"
    r"|/app/[^\s\"']*",
    re.IGNORECASE,
)

_MAX_ERROR_LEN = 500
_MAX_LOG_LEN = 2000


def _sanitize_error(e: Exception) -> str:
    """Return a safe error message, stripping internal paths and Docker details."""
    error_type = type(e).__name__
    if hasattr(type(e), "__module__") and "docker" in type(e).__module__:
        if "NotFound" in error_type:
            return "Container not found"
        if "APIError" in error_type:
            return "Container runtime error"
        return "Docker error"
    sanitized = _SENSITIVE_PATTERNS.sub("[redacted]", str(e))
    return sanitized[:_MAX_ERROR_LEN]


# --- Output collection ---

async def _collect_output(job_id: str, container, exit_code: int, logs: str) -> dict:
    """Collect result + stderr from stopped container via get_archive."""
    output = {
        "job_id": job_id,
        "exit_code": exit_code,
        "logs": logs[-_MAX_LOG_LEN:],
    }

    result = await extract_result(container)
    if result:
        if "error" in result:
            output["error"] = result["error"]
        elif "result_raw" in result:
            output["result_raw"] = result["result_raw"]
        else:
            output["result"] = result

    stderr = await extract_stderr(container)
    if stderr:
        output["stderr"] = stderr

    if exit_code != 0 and "error" not in output:
        output["error"] = f"Agent exited with code {exit_code}"

    return output


async def _finish_and_webhook(job_id: str, output: dict, store: JobStore,
                              webhook_url: str | None = None) -> bool:
    """Finish job in DB and fire webhook if configured."""
    exit_code = output.get("exit_code", -1)
    status = JobStatus.COMPLETED if exit_code == 0 else JobStatus.FAILED
    updated = await store.finish_job(
        job_id, status, result=output, exit_code=exit_code,
    )
    if updated and webhook_url:
        await _fire_webhook(webhook_url, output)
    return updated


# --- Wait + collect + finish (shared by execute_job and recovery) ---

async def _wait_and_finish(job_id: str, container_id: str, container,
                           timeout: int, store: JobStore, pool: ContainerPool,
                           webhook_url: str | None = None):
    """Wait for container exit, collect output, release, finish job."""
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(container.wait),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("Job %s timed out after %ds", job_id, timeout)
        await pool.release(container_id)
        await store.finish_job(
            job_id, JobStatus.FAILED,
            error=f"Timed out after {timeout}s",
        )
        return

    exit_code = result.get("StatusCode", -1)
    logs = (await asyncio.to_thread(container.logs)).decode(errors="replace")
    output = await _collect_output(job_id, container, exit_code, logs)

    updated = await _finish_and_webhook(job_id, output, store, webhook_url)
    if updated:
        # We won the finish - release container (cancel handler didn't)
        await pool.release(container_id)
        status = "completed" if exit_code == 0 else "failed"
        logger.info("Job %s %s (exit_code=%d)", job_id, status, exit_code)


# --- Job execution ---

async def execute_job(job_id: str, store: JobStore, semaphore: asyncio.Semaphore,
                      pool: ContainerPool, dry_run: bool = False):
    """Background task: acquire container from pool, inject config, run agent."""
    async with semaphore:
        job = await store.get(job_id)
        if not job or job.status == JobStatus.CANCELLED:
            return

        now = datetime.now(timezone.utc)
        if not await store.start_job(job_id, started_at=now):
            logger.info("Job %s already taken or cancelled", job_id)
            return
        logger.info("Job %s running", job_id)

        # Lazy import to avoid circular dependency
        from ..main import JOB_DURATION

        t0 = time.monotonic()
        container_id = None
        try:
            config = resolve_config(job.request)
            timeout = config.timeout or WORKER_TIMEOUT_SECONDS

            container_id, _network_id = await pool.acquire()
            logger.info("Job %s acquired container %s", job_id, container_id[:12])
            container = await asyncio.to_thread(get_container, container_id)

            if not await store.set_container(job_id, container_id):
                logger.info("Job %s cancelled during acquire, releasing container", job_id)
                await pool.release(container_id)
                return

            await inject_config(container, config, dry_run=dry_run, job_id=job_id)
            await _wait_and_finish(job_id, container_id, container,
                                   timeout, store, pool, job.webhook_url)

        except Exception as e:
            logger.exception("Job %s failed", job_id)
            if container_id:
                await pool.release(container_id)
            await store.finish_job(
                job_id, JobStatus.FAILED, error=_sanitize_error(e),
            )
        finally:
            JOB_DURATION.observe(time.monotonic() - t0)


# --- Recovery ---

async def recover_jobs(store: JobStore, semaphore: asyncio.Semaphore, pool: ContainerPool):
    """Full recovery: re-dispatch pending jobs + re-adopt running containers."""
    # 1. Pending jobs: Tower crashed between create and execute_job
    pending = await store.get_pending_jobs()
    for job_id in pending:
        asyncio.create_task(execute_job(job_id, store, semaphore, pool))
    if pending:
        logger.info("Recovery: re-dispatched %d pending jobs", len(pending))

    # 2. Running jobs: check if their containers are still alive
    running = await store.get_running_jobs()
    if not running:
        return

    readopted, failed = 0, 0
    for job_id, container_id in running:
        if container_id:
            try:
                get_container(container_id)
            except docker.errors.NotFound:
                pass  # Container gone - mark failed below
            except Exception as e:
                logger.warning("Recovery: Docker error for job %s, skipping: %s", job_id, e)
                continue
            else:
                async def _readopt(jid, cid):
                    async with semaphore:
                        await _readopt_container(jid, cid, store, pool)
                asyncio.create_task(_readopt(job_id, container_id))
                readopted += 1
                continue

        await store.finish_job(
            job_id, JobStatus.FAILED,
            error="Tower restarted (container lost)",
        )
        failed += 1

    if readopted or failed:
        logger.info("Recovery: %d re-adopted, %d marked failed", readopted, failed)


async def _readopt_container(job_id: str, container_id: str, store: JobStore, pool: ContainerPool):
    """Re-adopt a running container after Tower restart."""
    try:
        job = await store.get(job_id)
        if not job:
            return

        try:
            config = resolve_config(job.request)
            timeout = config.timeout or WORKER_TIMEOUT_SECONDS
        except Exception:
            timeout = WORKER_TIMEOUT_SECONDS

        container = await asyncio.to_thread(get_container, container_id)
        await _wait_and_finish(job_id, container_id, container,
                               timeout, store, pool, job.webhook_url)

    except Exception as e:
        logger.warning("Failed to re-adopt job %s: %s", job_id, e)
        if container_id:
            await pool.release(container_id)
        await store.finish_job(
            job_id, JobStatus.FAILED,
            error=f"Re-adoption failed: {_sanitize_error(e)}",
        )


# --- Lifecycle ---

async def cleanup_loop(store: JobStore):
    """Periodic cleanup of old finished jobs."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        try:
            removed = await store.cleanup_old()
            if removed:
                logger.info("Cleanup: %d jobs evicted", removed)
        except Exception:
            logger.exception("Cleanup cycle failed, will retry next cycle")


async def _fire_webhook(url: str, payload: dict):
    """Fire-and-forget webhook. Single attempt, no retry."""
    try:
        hostname = urlparse(url).hostname
        if hostname and await asyncio.to_thread(is_internal_host, hostname):
            logger.warning("Webhook to %s blocked: resolves to internal address", url)
            return
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT) as client:
            await client.post(url, json=payload)
    except Exception as e:
        logger.warning("Webhook to %s failed: %s", url, e)
