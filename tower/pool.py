"""Container pool - maintains warm worker containers ready for jobs."""

import asyncio
import logging
import uuid

import asyncpg
import docker.errors

from .config import (
    docker_client, WORKER_IMAGE, WORKER_NET,
    WORKER_MEM_LIMIT, WORKER_CPU_LIMIT, WORKER_RUNTIME,
    GATEWAY_URL, GATEWAY_CONTAINER,
    POOL_SIZE, POOL_CHECK_INTERVAL, POOL_MAX_IDLE,
)

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
            # Verify container is still alive (handles external kills)
            try:
                c = await asyncio.to_thread(docker_client().containers.get, row["container_id"])
                if c.status in ("running", "created"):
                    return row["container_id"], row["network_id"]
            except Exception:
                await self._pool.execute(
                    "DELETE FROM containers WHERE container_id = $1", row["container_id"],
                )
                logger.warning("Pool: skipped dead container %s", row["container_id"][:12])

        # Pool exhausted or all dead - create on-demand
        logger.warning("Pool exhausted, creating container on-demand")
        container_id = await self._create_warm(status="busy")
        return container_id, self._network_id

    async def release(self, container_id: str):
        """Destroy container, remove from DB. Network is shared - left alive."""
        await self._pool.execute(
            "DELETE FROM containers WHERE container_id = $1",
            container_id,
        )
        await self._destroy_container(container_id)
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
            await self._destroy_container(row["container_id"])
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
            try:
                docker_client().containers.get(row["container_id"])
            except Exception:
                await self._pool.execute("DELETE FROM containers WHERE id = $1", row["id"])
                tracked_ids.discard(row["container_id"])
                removed += 1
        if removed:
            logger.info("Pool startup: cleaned %d stale DB entries", removed)

        # 2. Docker -> DB: remove orphaned agent-* containers not in DB
        orphans = await asyncio.to_thread(
            docker_client().containers.list,
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
        """Create or find the shared worker network. Returns network ID.

        Validates existing networks have the correct config (internal, ICC disabled).
        Recreates the network if settings are wrong (e.g. created before hardening).
        """
        try:
            net = docker_client().networks.get(WORKER_NET)
            attrs = net.attrs or {}
            is_internal = attrs.get("Internal", False)
            icc = (attrs.get("Options") or {}).get("com.docker.network.bridge.enable_icc", "true")
            if is_internal and icc == "false":
                logger.info("Using existing worker network: %s", WORKER_NET)
                return net.id
            # Network exists but has wrong config - recreate
            logger.warning("Worker network %s has wrong config (internal=%s, icc=%s), recreating", WORKER_NET, is_internal, icc)
            net.remove()
        except docker.errors.NotFound:
            pass
        except docker.errors.APIError as e:
            if "has active endpoints" in str(e):
                logger.warning("Cannot recreate worker network (active containers) - using as-is")
                return docker_client().networks.get(WORKER_NET).id
            raise
        net = docker_client().networks.create(
            WORKER_NET,
            driver="bridge",
            internal=True,
            options={
                "com.docker.network.bridge.enable_icc": "false",
            },
        )
        logger.info("Created worker network: %s (internal=true, icc=false)", WORKER_NET)
        return net.id

    async def _create_warm(self, status: str = "ready") -> str:
        """Create a warm container on the shared network, insert into DB. Returns container_id."""
        # Workers get placeholder keys + base URL override (gateway injects real keys)
        gateway = GATEWAY_URL.rstrip("/")
        env = {
            "ANTHROPIC_BASE_URL": f"{gateway}/anthropic",
            "OPENAI_BASE_URL": f"{gateway}/openai",
            "ANTHROPIC_API_KEY": "gateway",
            "OPENAI_API_KEY": "gateway",
        }

        # Container config - security always on
        run_kwargs = dict(
            image=WORKER_IMAGE,
            detach=True,
            environment=env,
            name=f"agent-warm-{uuid.uuid4().hex[:8]}",
            network=WORKER_NET,
            remove=False,
            mem_limit=WORKER_MEM_LIMIT,
            nano_cpus=int(WORKER_CPU_LIMIT * 1e9),
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            pids_limit=100,
            ipc_mode="private",
        )

        # gVisor kernel-level isolation
        if WORKER_RUNTIME:
            run_kwargs["runtime"] = WORKER_RUNTIME

        container = await asyncio.to_thread(
            docker_client().containers.run, **run_kwargs,
        )

        # Connect gateway container to worker network
        await self._connect_container(GATEWAY_CONTAINER)

        await self._pool.execute(
            "INSERT INTO containers (container_id, network_id, status) VALUES ($1, $2, $3)",
            container.id, self._network_id, status,
        )
        return container.id

    async def _connect_container(self, container_name: str):
        """Connect a container to the worker network (idempotent)."""
        try:
            c = await asyncio.to_thread(
                docker_client().containers.get, container_name,
            )
            net = await asyncio.to_thread(
                docker_client().networks.get, WORKER_NET,
            )
            await asyncio.to_thread(net.connect, c)
        except docker.errors.APIError as e:
            if "already exists" not in str(e):
                logger.warning("Connect %s to worker network failed: %s", container_name, e)
        except Exception as e:
            logger.warning("Connect %s to worker network failed: %s", container_name, e)

    async def _destroy_container(self, container_id: str):
        """Kill + remove a container. Tolerant to failures."""
        try:
            c = docker_client().containers.get(container_id)
            await asyncio.to_thread(c.kill)
        except Exception:
            pass
        try:
            c = docker_client().containers.get(container_id)
            await asyncio.to_thread(c.remove)
        except Exception:
            pass
