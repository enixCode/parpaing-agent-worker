"""Container pool - maintains warm worker containers ready for jobs."""

import asyncio
import logging
import uuid

import asyncpg

from ..config import (
    GATEWAY_URL, GATEWAY_CONTAINER,
    POOL_SIZE, POOL_CHECK_INTERVAL, POOL_MAX_IDLE,
)

logger = logging.getLogger("tower.pool")


class ContainerPool:
    """Maintains a pool of warm worker containers.

    The pool is DB-backed (containers table), so multiple Tower instances
    share the same pool. Acquire is atomic (FOR UPDATE SKIP LOCKED).

    Docker operations are delegated to the Runtime abstraction, allowing
    different backends (Compose, Swarm, K8s).
    """

    def __init__(self, runtime):
        self._runtime = runtime
        self._pool: asyncpg.Pool = None  # type: ignore[assignment]
        self._task: asyncio.Task | None = None
        self._network_id: str = ""

    @property
    def runtime(self):
        """Runtime instance for use by executor."""
        return self._runtime

    async def start(self, db_pool: asyncpg.Pool):
        """Attach to shared DB pool, ensure network, clean stale, fill pool, start maintenance."""
        self._pool = db_pool
        self._network_id = await self._runtime.ensure_network()
        await self._cleanup_stale()
        await self._fill()
        self._task = asyncio.create_task(self._maintain_loop())
        logger.info("Container pool started (target size: %d)", POOL_SIZE)

    async def shutdown(self):
        """Stop maintenance. Leave containers and network alive for re-adoption."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Container pool stopped")

    # --- Public API ---

    async def acquire(self) -> tuple[str, str]:
        """Atomically acquire a ready container. Returns (container_id, network_id).

        Verifies container is alive before returning. Falls back to on-demand creation.
        """
        for _ in range(POOL_SIZE or 3):
            row = await self._pool.fetchrow(
                """UPDATE containers SET status = 'busy'
                   WHERE id = (
                       SELECT id FROM containers
                       WHERE status = 'ready'
                       ORDER BY created_at
                       LIMIT 1
                       FOR UPDATE SKIP LOCKED
                   )
                   RETURNING container_id, network_id""",
            )
            if not row:
                break
            if await self._runtime.worker_alive(row["container_id"]):
                return row["container_id"], row["network_id"]
            await self._pool.execute(
                "DELETE FROM containers WHERE container_id = $1", row["container_id"],
            )
            logger.warning("Pool: skipped dead container %s", row["container_id"][:12])

        # Pool exhausted or all dead - create on-demand
        logger.warning("Pool exhausted, creating container on-demand")
        container_id = await self._create_warm(status="busy")
        return container_id, self._network_id

    async def release(self, container_id: str, job_id: str = ""):
        """Destroy container, remove from DB. Network is shared - left alive."""
        await self._pool.execute(
            "DELETE FROM containers WHERE container_id = $1",
            container_id,
        )
        await self._runtime.destroy_worker(container_id, job_id=job_id)
        logger.info("Pool: released container %s", container_id[:12])

    # --- Maintenance ---

    async def _maintain_loop(self):
        """Background loop: fill pool + prune stale containers."""
        while True:
            await asyncio.sleep(POOL_CHECK_INTERVAL)
            try:
                await self._prune_idle()
                await self._fill()
            except Exception:
                logger.exception("Pool maintenance error, will retry")

    async def _fill(self):
        """Create containers until pool reaches target size."""
        count = await self._pool.fetchval(
            "SELECT count(*) FROM containers WHERE status = 'ready'"
        )
        needed = POOL_SIZE - count
        if needed <= 0:
            return
        tasks = [self._create_warm() for _ in range(needed)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        created = sum(1 for r in results if not isinstance(r, Exception))
        if created:
            ready = count + created
            logger.info("Pool: created %d containers (ready: %d/%d)", created, ready, POOL_SIZE)
        for r in results:
            if isinstance(r, Exception):
                logger.warning("Pool: failed to create container: %s", r)

    async def _prune_idle(self):
        """Destroy containers that have been idle too long."""
        rows = await self._pool.fetch(
            """DELETE FROM containers
               WHERE status = 'ready'
               AND created_at < now() - make_interval(secs => $1)
               RETURNING container_id""",
            POOL_MAX_IDLE,
        )
        for row in rows:
            await self._runtime.destroy_worker(row["container_id"])
        if rows:
            logger.info("Pool: pruned %d stale containers", len(rows))

    async def _cleanup_stale(self):
        """On startup: remove DB entries whose Docker container no longer exists,
        and remove orphaned Docker containers not tracked in the DB."""
        # 1. DB -> Docker: remove DB entries for missing containers
        rows = await self._pool.fetch("SELECT id, container_id FROM containers")
        tracked_ids = {row["container_id"] for row in rows}
        removed = 0
        for row in rows:
            if not await self._runtime.worker_alive(row["container_id"]):
                await self._pool.execute("DELETE FROM containers WHERE id = $1", row["id"])
                tracked_ids.discard(row["container_id"])
                removed += 1
        if removed:
            logger.info("Pool startup: cleaned %d stale DB entries", removed)

        # 2. Docker -> DB: remove orphaned agent-* containers not in DB
        orphan_ids = await self._runtime.list_orphan_workers(tracked_ids)
        for oid in orphan_ids:
            await self._runtime.destroy_worker(oid)
        if orphan_ids:
            logger.info("Pool startup: removed %d orphaned containers", len(orphan_ids))

        # 3. Clean up orphan job directories (crash recovery)
        active_names = set()
        for row in await self._pool.fetch("SELECT container_id FROM containers"):
            active_names.add(self._runtime._resolve_name(row["container_id"]))
        await self._runtime.cleanup_orphan_dirs(active_names)

    # --- Container creation ---

    async def _create_warm(self, status: str = "ready") -> str:
        """Create a warm container on the shared network, insert into DB. Returns container_id."""
        gateway = GATEWAY_URL.rstrip("/")
        env = {
            "ANTHROPIC_BASE_URL": f"{gateway}/anthropic",
            "OPENAI_BASE_URL": f"{gateway}/openai",
            "ANTHROPIC_API_KEY": "gateway",
            "OPENAI_API_KEY": "gateway",
        }

        name = f"agent-warm-{uuid.uuid4().hex[:8]}"
        container_id = await self._runtime.create_worker(env, name)

        # Connect gateway container to worker network
        await self._runtime.connect_to_network(GATEWAY_CONTAINER)

        try:
            await self._pool.execute(
                "INSERT INTO containers (container_id, network_id, status) VALUES ($1, $2, $3)",
                container_id, self._network_id, status,
            )
        except Exception:
            logger.exception("Failed to register container %s, destroying orphan", container_id[:12])
            try:
                await self._runtime.destroy_worker(container_id)
            except Exception:
                logger.warning("Failed to cleanup orphaned container %s", container_id[:12])
            raise
        return container_id
