"""Worker helpers - config injection, result extraction."""

import asyncio
import io
import json
import logging
import tarfile

from .config import docker_client, HOOKS_DIR, MAX_RESULT_SIZE
from .profiles import JobConfig

logger = logging.getLogger("tower.worker")

_TAR_HEADER_OVERHEAD = 4096
_MAX_RAW_RESULT_LEN = 5000
_MAX_STDERR_LEN = 2000


# --- Config injection ---

def _build_config_tar(config: JobConfig, dry_run: bool = False) -> bytes:
    """Build a tar archive with all config files for the worker.

    Files are prefixed with 'config/' so put_archive("/tmp", tar) creates /tmp/config/*.
    The .ready marker is added last to signal the entrypoint.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:

        def _add(name: str, content: str | bytes, mode: int = 0o644):
            data = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(name=f"config/{name}")
            info.size = len(data)
            info.mode = mode
            info.uid = 1000
            info.gid = 1000
            tar.addfile(info, io.BytesIO(data))

        # job.json (includes dry_run flag and engine config for worker)
        _add("job.json", json.dumps({
            "prompt": config.prompt,
            "allowed_tools": config.allowed_tools,
            "max_turns": config.max_turns,
            "max_budget_usd": config.max_budget_usd,
            "model": config.model,
            "output_format": config.output_format,
            "system_prompt": config.system_prompt,
            "dry_run": dry_run,
            "engine": {
                "id": config.engine.id,
                "binary": config.engine.binary,
                "prompt_flag": config.engine.prompt_flag,
                "static_args": config.engine.static_args,
                "flag_map": config.engine.flag_map,
                "list_join": config.engine.list_join,
                "output_mode": config.engine.output_mode,
                "output_format": config.engine.output_format,
                "output_path": config.engine.output_path,
            },
        }))

        if config.mcp_config:
            _add("mcp.json", json.dumps(config.mcp_config))

        if config.claude_md:
            _add("CLAUDE.md", config.claude_md)

        if config.plugins:
            _add("settings.json", json.dumps(
                {"enabledPlugins": {p: True for p in config.plugins}}
            ))

        # Hooks (inline script or filename from HOOKS_DIR)
        for hook_val, dest in [(config.hook_pre, "pre-job.sh"), (config.hook_post, "post-job.sh")]:
            if not hook_val:
                continue
            if "\n" in hook_val:
                # Inline script from profile TOML
                _add(dest, hook_val, mode=0o755)
            else:
                # Filename reference → read from HOOKS_DIR
                src = (HOOKS_DIR / hook_val).resolve()
                if not src.is_relative_to(HOOKS_DIR.resolve()):
                    logger.warning("Hook path traversal blocked: %s", hook_val)
                    continue
                if not src.exists():
                    logger.warning("Hook script not found: %s", hook_val)
                    continue
                _add(dest, src.read_bytes(), mode=0o755)

        # .ready marker - MUST be last (signals entrypoint to start)
        _add(".ready", "")

    buf.seek(0)
    return buf.read()


async def inject_config(container, config: JobConfig, dry_run: bool = False):
    """Inject config files into a running container via put_archive."""
    tar_data = _build_config_tar(config, dry_run)
    await asyncio.wait_for(
        asyncio.to_thread(container.put_archive, "/tmp", tar_data),
        timeout=60,
    )
    logger.debug("Injected config into container %s", container.short_id)


# --- Result extraction ---

def _extract_file_from_archive(stream) -> tuple[bytes, int]:
    """Extract a single file from a Docker get_archive stream.

    Streams data with a size cap to prevent OOM from oversized files.
    """
    chunks = []
    total = 0
    for chunk in stream:
        total += len(chunk)
        if total > MAX_RESULT_SIZE + _TAR_HEADER_OVERHEAD:  # tar header overhead
            return (b"", total)
        chunks.append(chunk)
    raw = b"".join(chunks)
    buf = io.BytesIO(raw)
    with tarfile.open(fileobj=buf, mode="r") as tar:
        members = tar.getmembers()
        if not members:
            return (b"", 0)
        member = members[0]
        if member.size > MAX_RESULT_SIZE:
            return (b"", member.size)
        if not member.isfile():
            return (b"", 0)
        f = tar.extractfile(member)
        return (f.read(), member.size) if f else (b"", 0)


async def extract_result(container) -> dict | None:
    """Extract result.json from a stopped container via get_archive."""
    try:
        stream, _ = await asyncio.wait_for(
            asyncio.to_thread(container.get_archive, "/output/result.json"),
            timeout=120,
        )
        content, size = _extract_file_from_archive(stream)
        if size > MAX_RESULT_SIZE:
            return {"error": f"result.json too large ({size} bytes, max {MAX_RESULT_SIZE})"}
        text = content.decode()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"result_raw": text[:_MAX_RAW_RESULT_LEN]}
    except Exception as e:
        logger.warning("Failed to extract result.json: %s", e)
    return None


async def extract_stderr(container) -> str | None:
    """Extract stderr.log from a stopped container if present."""
    try:
        stream, _ = await asyncio.wait_for(
            asyncio.to_thread(container.get_archive, "/output/stderr.log"),
            timeout=120,
        )
        content, size = _extract_file_from_archive(stream)
        if size > MAX_RESULT_SIZE:
            return None
        text = content.decode(errors="replace")
        return text[-_MAX_STDERR_LEN:] if text else None
    except Exception:
        return None


# --- Container access ---

def get_container(container_id: str):
    """Get a Docker container by ID. Raises docker.errors.NotFound if gone."""
    return docker_client().containers.get(container_id)
