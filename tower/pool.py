"""Container pool - maintains warm worker containers ready for jobs."""

import asyncio
import logging
import os
import uuid

import asyncpg

from .config import (
    docker_client, WORKER_IMAGE, WORKER_NET,
    WORKER_MEM_LIMIT, WORKER_CPU_LIMIT, WORKER_HARDENED,
    PROXY_URL, POOL_SIZE, POOL_CHECK_INTERVAL, POOL_MAX_IDLE,
)
from .engines import list_engines

logger = logging.getLogger("tower.pool")


class ContainerPool:
    """Maintains a pool of warm worker containers.

    The pool is DB-backed (containers table), so multiple Tower instances
    share the same pool. Acquire is atomic (FOR UPDATE SKIP LOCKED).

    All workers share a single Docker bridge network with inter-container
    communication disabled (ICC=false). This prevents subnet exhaustion
    and isolates workers from each other while allowing internet access.
    """

    def __init__(self):
        self._pool: asyncpg.Pool = None  # type: ignore[assignment]
        self._task: asyncio.Task | None = None
        self._network_id: str = ""

    async def start(self, db_pool: asyncpg.Pool):
        """Attach to shared DB pool, ensure network, clean stale, fill pool, start maintenance."""
        self._pool = db_pool
        self._network_id = await asyncio.to_thread(self._ensure_network)
        await self._cleanup_stale()
        await self._fill()
        self._task = asyncio.create_task(self._maintain_loop())
        logger.info("Container pool started (target size: %d, network: %s)", POOL_SIZE, WORKER_NET)

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

        Raises RuntimeError if no container available (pool exhausted).
        """
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
            # Pool exhausted - create one on-demand
            logger.warning("Pool exhausted, creating container on-demand")
            container_id = await self._create_warm()
            await self._pool.execute(
                "UPDATE containers SET status = 'busy' WHERE container_id = $1",
                container_id,
            )
            return container_id, self._network_id
        return row["container_id"], row["network_id"]

    async def release(self, container_id: str):
        """Destroy container, remove from DB. Network is shared - left alive."""
        await self._pool.execute(
            "DELETE FROM containers WHERE container_id = $1",
            container_id,
        )
        await self._destroy_container(container_id)

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
               AND created_at < now() - ($1 || ' seconds')::interval
               RETURNING container_id""",
            str(POOL_MAX_IDLE),
        )
        for row in rows:
            await self._destroy_container(row["container_id"])
        if rows:
            logger.info("Pool: pruned %d stale containers", len(rows))

    async def _cleanup_stale(self):
        """On startup: remove DB entries whose Docker container no longer exists,
        and remove orphaned Docker containers not tracked in the DB."""
        # 1. DB → Docker: remove DB entries for missing containers
        rows = await self._pool.fetch("SELECT id, container_id FROM containers")
        tracked_ids = {row["container_id"] for row in rows}
        removed = 0
        for row in rows:
            try:
                docker_client.containers.get(row["container_id"])
            except Exception:
                await self._pool.execute("DELETE FROM containers WHERE id = $1", row["id"])
                tracked_ids.discard(row["container_id"])
                removed += 1
        if removed:
            logger.info("Pool startup: cleaned %d stale DB entries", removed)

        # 2. Docker → DB: remove orphaned agent-* containers not in DB
        orphans = await asyncio.to_thread(
            docker_client.containers.list,
            all=True,
            filters={"name": "agent-", "status": ["created", "exited"]},
        )
        orphan_count = 0
        for c in orphans:
            if c.id not in tracked_ids:
                try:
                    await asyncio.to_thread(c.remove, force=True)
                    orphan_count += 1
                except Exception:
                    pass
        if orphan_count:
            logger.info("Pool startup: removed %d orphaned containers", orphan_count)

    # --- Docker operations ---

    @staticmethod
    def _ensure_network() -> str:
        """Create or find the shared worker network. Returns network ID."""
        try:
            net = docker_client.networks.get(WORKER_NET)
            logger.info("Using existing worker network: %s", WORKER_NET)
            return net.id
        except Exception:
            pass
        net = docker_client.networks.create(
            WORKER_NET,
            driver="bridge",
            internal=bool(PROXY_URL),
            options={"com.docker.network.bridge.enable_icc": "false"},
        )
        logger.info("Created worker network: %s (icc=false, internal=%s)", WORKER_NET, bool(PROXY_URL))
        return net.id

    async def _create_warm(self) -> str:
        """Create a warm container on the shared network, insert into DB. Returns container_id."""
        # Collect all auth env vars declared by all engines
        env = {}
        for engine in list_engines():
            for key in engine.get("env_auth", []):
                val = os.environ.get(key, "")
                if val:
                    env[key] = val
        if PROXY_URL:
            env.update({
                "HTTP_PROXY": PROXY_URL, "HTTPS_PROXY": PROXY_URL,
                "http_proxy": PROXY_URL, "https_proxy": PROXY_URL,
            })

        # Base container config
        run_kwargs = dict(
            image=WORKER_IMAGE,
            detach=True,
            environment=env,
            name=f"agent-warm-{uuid.uuid4().hex[:8]}",
            network=WORKER_NET,
            remove=False,
            mem_limit=WORKER_MEM_LIMIT,
            nano_cpus=int(WORKER_CPU_LIMIT * 1e9),
        )

        # Hardening (production)
        if WORKER_HARDENED:
            run_kwargs.update(
                read_only=True,
                cap_drop=["ALL"],
                tmpfs={"/home/agent": "size=1g,uid=1000,gid=1000",
                       "/tmp": "size=512m",
                       "/output": "size=256m,uid=1000,gid=1000"},
                security_opt=["no-new-privileges:true"],
                pids_limit=256,
            )

        container = await asyncio.to_thread(
            docker_client.containers.run, **run_kwargs,
        )

        await self._pool.execute(
            "INSERT INTO containers (container_id, network_id) VALUES ($1, $2)",
            container.id, self._network_id,
        )
        return container.id

    async def _destroy_container(self, container_id: str):
        """Kill + remove a container. Tolerant to failures."""
        try:
            c = docker_client.containers.get(container_id)
            await asyncio.to_thread(c.kill)
        except Exception:
            pass
        try:
            c = docker_client.containers.get(container_id)
            await asyncio.to_thread(c.remove)
        except Exception:
            pass
