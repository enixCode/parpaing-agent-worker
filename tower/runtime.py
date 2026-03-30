"""Runtime abstraction - Docker Compose and Swarm backends.

Both runtimes use a shared volume for config injection and result extraction.
The only differences are: container lifecycle (containers vs services),
networking (bridge vs overlay), and wait mechanism (blocking vs polling).
"""

import asyncio
import json
import logging
import shutil
import time
from abc import ABC, abstractmethod
from pathlib import Path

import docker.errors
import docker.types
import docker.utils

from .config import (
    docker_client, WORKER_IMAGE, WORKER_NET,
    WORKER_MEM_LIMIT, WORKER_CPU_LIMIT, WORKER_RUNTIME,
    MAX_RESULT_SIZE, JOBS_DIR, JOBS_VOLUME,
)
from .profiles import JobConfig
from .runner.worker import _config_files

logger = logging.getLogger("tower.runtime")

_MAX_RAW_RESULT_LEN = 5000
_MAX_STDERR_LEN = 2000


# --- Base Runtime (shared filesystem logic) ---

class Runtime(ABC):
    """Abstraction over container orchestration backends.

    Inject/extract/cleanup are shared (filesystem-based).
    Subclasses only implement Docker-specific operations.
    """

    def __init__(self):
        self._network_id: str = ""
        self._jobs_dir: Path = JOBS_DIR
        self._worker_names: dict[str, str] = {}  # worker_id -> name

    def _resolve_name(self, worker_id: str) -> str:
        """Resolve worker name from ID. Override in subclass if needed."""
        return self._worker_names.get(worker_id, worker_id[:12])

    # --- Shared: config injection (filesystem) ---

    async def inject_config(self, worker_id: str, config: JobConfig,
                            dry_run: bool = False, job_id: str = "") -> None:
        """Write config files to the worker's shared volume directory."""
        worker_name = self._resolve_name(worker_id)
        config_dir = self._jobs_dir / worker_name / "config"

        files = _config_files(config, dry_run)
        for name, content, mode in files:
            path = config_dir / name
            await asyncio.to_thread(path.write_bytes, content)
            await asyncio.to_thread(path.chmod, mode)

        logger.info("Injected config for job %s (worker %s)", job_id, worker_name)

    # --- Shared: result extraction (filesystem) ---

    async def extract_result(self, worker_id: str, job_id: str = "") -> dict | None:
        worker_name = self._resolve_name(worker_id)
        result_path = self._jobs_dir / worker_name / "output" / "result.json"
        try:
            content = await asyncio.to_thread(result_path.read_bytes)
            if len(content) > MAX_RESULT_SIZE:
                return {"error": f"result.json too large ({len(content)} bytes, max {MAX_RESULT_SIZE})"}
            text = content.decode(errors="replace")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"result_raw": text[:_MAX_RAW_RESULT_LEN]}
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.warning("Failed to extract result.json: %s", e)
            return None

    async def extract_stderr(self, worker_id: str, job_id: str = "") -> str | None:
        worker_name = self._resolve_name(worker_id)
        stderr_path = self._jobs_dir / worker_name / "output" / "stderr.log"
        try:
            content = await asyncio.to_thread(stderr_path.read_bytes)
            if len(content) > MAX_RESULT_SIZE:
                return None
            text = content.decode(errors="replace")
            return text[-_MAX_STDERR_LEN:] if text else None
        except FileNotFoundError:
            return None
        except Exception:
            return None

    # --- Shared: cleanup orphan job directories ---

    async def cleanup_orphan_dirs(self, active_worker_names: set[str]) -> int:
        """Remove job directories not associated with any active worker."""
        if not self._jobs_dir.exists():
            return 0
        removed = 0
        try:
            for entry in self._jobs_dir.iterdir():
                if entry.is_dir() and entry.name not in active_worker_names:
                    await asyncio.to_thread(shutil.rmtree, str(entry), True)
                    removed += 1
        except Exception:
            logger.exception("Error cleaning orphan job directories")
        if removed:
            logger.info("Cleaned %d orphan job directories", removed)
        return removed

    # --- Abstract: Docker-specific operations ---

    @abstractmethod
    async def ensure_network(self) -> str:
        """Create or verify the worker network. Returns network_id."""

    @abstractmethod
    async def create_worker(self, env: dict, name: str) -> str:
        """Create a new worker with shared volume mount. Returns worker_id."""

    @abstractmethod
    async def destroy_worker(self, worker_id: str, job_id: str = "") -> None:
        """Kill/remove worker and clean up its job directory."""

    @abstractmethod
    async def worker_alive(self, worker_id: str) -> bool:
        """Check if a worker is still running."""

    @abstractmethod
    async def wait_for_completion(self, worker_id: str, timeout: int) -> dict:
        """Wait for worker to finish. Returns {"StatusCode": int}."""

    @abstractmethod
    async def get_logs(self, worker_id: str) -> str:
        """Get worker stdout/stderr logs."""

    @abstractmethod
    async def list_orphan_workers(self, tracked_ids: set[str]) -> list[str]:
        """List worker IDs not in tracked_ids (orphans to clean up)."""

    @abstractmethod
    async def connect_to_network(self, container_name: str) -> None:
        """Connect a named container to the worker network (idempotent)."""


# --- Docker Compose Runtime ---

class ComposeRuntime(Runtime):
    """Docker Compose - local containers via Docker SDK."""

    async def ensure_network(self) -> str:
        self._network_id = await asyncio.to_thread(self._ensure_network_sync)
        return self._network_id

    async def create_worker(self, env: dict, name: str) -> str:
        # Pre-create directories (writable by worker UID 1000)
        config_dir = self._jobs_dir / name / "config"
        output_dir = self._jobs_dir / name / "output"
        config_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_dir.chmod(0o777)

        jobs_dir_str = str(self._jobs_dir)
        env["WORKER_CONFIG_DIR"] = f"{jobs_dir_str}/{name}/config"
        env["WORKER_OUTPUT_DIR"] = f"{jobs_dir_str}/{name}/output"

        run_kwargs = dict(
            image=WORKER_IMAGE,
            detach=True,
            environment=env,
            name=name,
            network=WORKER_NET,
            remove=False,
            mem_limit=WORKER_MEM_LIMIT,
            nano_cpus=int(WORKER_CPU_LIMIT * 1e9),
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            pids_limit=100,
            ipc_mode="private",
            volumes={JOBS_VOLUME: {"bind": jobs_dir_str, "mode": "rw"}},
        )
        if WORKER_RUNTIME:
            run_kwargs["runtime"] = WORKER_RUNTIME

        container = await asyncio.to_thread(
            docker_client().containers.run, **run_kwargs,
        )
        self._worker_names[container.id] = name
        return container.id

    async def destroy_worker(self, worker_id: str, job_id: str = "") -> None:
        worker_name = self._resolve_name(worker_id)
        try:
            c = docker_client().containers.get(worker_id)
            await asyncio.to_thread(c.kill)
        except Exception:
            pass
        try:
            c = docker_client().containers.get(worker_id)
            await asyncio.to_thread(c.remove)
        except Exception:
            pass
        # Clean up job directory
        worker_dir = self._jobs_dir / worker_name
        if worker_dir.exists():
            await asyncio.to_thread(shutil.rmtree, str(worker_dir), True)
        self._worker_names.pop(worker_id, None)

    async def worker_alive(self, worker_id: str) -> bool:
        try:
            c = await asyncio.to_thread(docker_client().containers.get, worker_id)
            return c.status in ("running", "created")
        except Exception:
            return False

    async def wait_for_completion(self, worker_id: str, timeout: int) -> dict:
        container = await asyncio.to_thread(docker_client().containers.get, worker_id)
        return await asyncio.wait_for(
            asyncio.to_thread(container.wait),
            timeout=timeout,
        )

    async def get_logs(self, worker_id: str) -> str:
        container = await asyncio.to_thread(docker_client().containers.get, worker_id)
        raw = await asyncio.wait_for(
            asyncio.to_thread(container.logs),
            timeout=60,
        )
        return raw.decode(errors="replace")

    async def list_orphan_workers(self, tracked_ids: set[str]) -> list[str]:
        containers = await asyncio.to_thread(
            docker_client().containers.list,
            all=True,
            filters={"name": "agent-", "status": ["created", "exited"]},
        )
        return [c.id for c in containers if c.id not in tracked_ids]

    async def connect_to_network(self, container_name: str) -> None:
        try:
            c = await asyncio.to_thread(docker_client().containers.get, container_name)
            net = await asyncio.to_thread(docker_client().networks.get, WORKER_NET)
            await asyncio.to_thread(net.connect, c)
        except docker.errors.APIError as e:
            if "already exists" not in str(e):
                logger.warning("Connect %s to worker network failed: %s", container_name, e)
        except Exception as e:
            logger.warning("Connect %s to worker network failed: %s", container_name, e)

    @staticmethod
    def _ensure_network_sync() -> str:
        try:
            net = docker_client().networks.get(WORKER_NET)
            if (net.attrs or {}).get("Internal", False):
                logger.info("Using existing worker network: %s", WORKER_NET)
                return net.id
            logger.warning("Worker network %s is not internal, recreating", WORKER_NET)
            net.remove()
        except docker.errors.NotFound:
            pass
        except docker.errors.APIError as e:
            if "has active endpoints" in str(e):
                logger.warning("Cannot recreate worker network (active containers) - using as-is")
                return docker_client().networks.get(WORKER_NET).id
            raise
        net = docker_client().networks.create(WORKER_NET, driver="bridge", internal=True)
        logger.info("Created worker network: %s (internal=true)", WORKER_NET)
        return net.id


# --- Docker Swarm Runtime ---

_SWARM_POLL_INTERVAL = 2
_SWARM_TERMINAL_STATES = {"complete", "failed", "shutdown", "rejected", "orphaned"}


class SwarmRuntime(Runtime):
    """Docker Swarm - multi-node via services + shared volume."""

    def _resolve_name(self, worker_id: str) -> str:
        if worker_id in self._worker_names:
            return self._worker_names[worker_id]
        try:
            svc = docker_client().services.get(worker_id)
            name = svc.attrs.get("Spec", {}).get("Name", worker_id[:12])
            self._worker_names[worker_id] = name
            return name
        except Exception:
            return worker_id[:12]

    async def ensure_network(self) -> str:
        try:
            net = await asyncio.to_thread(docker_client().networks.get, WORKER_NET)
            self._network_id = net.id
            logger.info("Using existing worker network: %s (%s)", WORKER_NET, net.attrs.get("Driver", "?"))
            return self._network_id
        except docker.errors.NotFound:
            self._network_id = await asyncio.to_thread(self._ensure_network_sync)
            return self._network_id

    async def create_worker(self, env: dict, name: str) -> str:
        # Pre-create directories (writable by worker UID 1000)
        config_dir = self._jobs_dir / name / "config"
        output_dir = self._jobs_dir / name / "output"
        config_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_dir.chmod(0o777)

        jobs_dir_str = str(self._jobs_dir)
        env["WORKER_CONFIG_DIR"] = f"{jobs_dir_str}/{name}/config"
        env["WORKER_OUTPUT_DIR"] = f"{jobs_dir_str}/{name}/output"
        env_list = [f"{k}={v}" for k, v in env.items()]

        mounts = [
            docker.types.Mount(target=jobs_dir_str, source=JOBS_VOLUME, type="volume"),
        ]

        mem_bytes = docker.utils.parse_bytes(WORKER_MEM_LIMIT)
        cpu_nano = int(WORKER_CPU_LIMIT * 1e9)

        svc = await asyncio.to_thread(
            docker_client().services.create,
            image=WORKER_IMAGE,
            name=name,
            env=env_list,
            mounts=mounts,
            cap_drop=["ALL"],
            init=True,
            user="1000",
            networks=[WORKER_NET],
            resources=docker.types.Resources(mem_limit=mem_bytes, cpu_limit=cpu_nano),
            restart_policy=docker.types.RestartPolicy(condition="none"),
        )
        self._worker_names[svc.id] = name
        logger.info("Swarm: created service %s (%s)", name, svc.id[:12])
        return svc.id

    async def destroy_worker(self, worker_id: str, job_id: str = "") -> None:
        worker_name = self._resolve_name(worker_id)
        try:
            svc = await asyncio.to_thread(docker_client().services.get, worker_id)
            await asyncio.to_thread(svc.remove)
        except Exception:
            pass
        worker_dir = self._jobs_dir / worker_name
        if worker_dir.exists():
            await asyncio.to_thread(shutil.rmtree, str(worker_dir), True)
        self._worker_names.pop(worker_id, None)

    async def worker_alive(self, worker_id: str) -> bool:
        try:
            svc = await asyncio.to_thread(docker_client().services.get, worker_id)
            tasks = svc.tasks()
            if not tasks:
                return True
            return tasks[0]["Status"]["State"] not in _SWARM_TERMINAL_STATES
        except Exception:
            return False

    async def wait_for_completion(self, worker_id: str, timeout: int) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                svc = await asyncio.to_thread(docker_client().services.get, worker_id)
                tasks = svc.tasks()
                if tasks:
                    state = tasks[0]["Status"]["State"]
                    if state in _SWARM_TERMINAL_STATES:
                        exit_code = tasks[0]["Status"].get("ContainerStatus", {}).get("ExitCode", -1)
                        return {"StatusCode": exit_code}
            except Exception:
                pass
            await asyncio.sleep(_SWARM_POLL_INTERVAL)
        raise asyncio.TimeoutError()

    async def get_logs(self, worker_id: str) -> str:
        try:
            svc = await asyncio.to_thread(docker_client().services.get, worker_id)
            raw = await asyncio.to_thread(svc.logs, stdout=True, stderr=True)
            if isinstance(raw, bytes):
                return raw.decode(errors="replace")
            return b"".join(raw).decode(errors="replace")
        except Exception as e:
            logger.warning("Swarm: failed to get logs for %s: %s", worker_id[:12], e)
            return ""

    async def list_orphan_workers(self, tracked_ids: set[str]) -> list[str]:
        try:
            services = await asyncio.to_thread(
                docker_client().services.list, filters={"name": "agent-"},
            )
            return [s.id for s in services if s.id not in tracked_ids]
        except Exception:
            return []

    async def connect_to_network(self, container_name: str) -> None:
        pass  # Swarm services are connected at creation via networks param

    @staticmethod
    def _ensure_network_sync() -> str:
        net = docker_client().networks.create(
            WORKER_NET, driver="overlay", internal=True, attachable=True,
        )
        logger.info("Created worker overlay network: %s (internal=true)", WORKER_NET)
        return net.id


# --- Factory ---

def create_runtime(mode: str) -> Runtime:
    """Create a Runtime instance based on RUNTIME_MODE."""
    if mode == "compose":
        return ComposeRuntime()
    if mode == "swarm":
        return SwarmRuntime()
    raise ValueError(f"Unknown RUNTIME_MODE: {mode!r} (expected: compose, swarm)")
