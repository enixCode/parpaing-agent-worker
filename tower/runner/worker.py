"""Worker helpers - config file generation and tar extraction utilities."""

import io
import json
import logging
import tarfile

from ..config import HOOKS_DIR, MAX_RESULT_SIZE

logger = logging.getLogger("tower.worker")

_TAR_HEADER_OVERHEAD = 4096
_MAX_RAW_RESULT_LEN = 5000
_MAX_STDERR_LEN = 2000


# --- Config file generation ---

def _config_files(config, dry_run: bool = False) -> list[tuple[str, bytes, int]]:
    """Build the list of config files for a job.

    Returns a list of (filename, content_bytes, unix_mode) tuples.
    The .ready marker is always last (signals the worker entrypoint to start).
    """
    files: list[tuple[str, bytes, int]] = []

    def _add(name: str, content: str | bytes, mode: int = 0o644):
        data = content.encode() if isinstance(content, str) else content
        files.append((name, data, mode))

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
            # Filename reference - read from HOOKS_DIR
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

    return files


def _build_config_tar(config, dry_run: bool = False) -> bytes:
    """Build a tar archive with all config files for the worker.

    Files are prefixed with 'config/' so put_archive("/tmp", tar) creates /tmp/config/*.
    The .ready marker is added last to signal the entrypoint.
    """
    files = _config_files(config, dry_run)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data, mode in files:
            info = tarfile.TarInfo(name=f"config/{name}")
            info.size = len(data)
            info.mode = mode
            info.uid = 1000
            info.gid = 1000
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf.read()


# --- Tar extraction utilities (used by ComposeRuntime) ---

def _extract_file_from_archive(stream) -> tuple[bytes, int]:
    """Extract a single file from a Docker get_archive stream.

    Streams data with a size cap to prevent OOM from oversized files.
    """
    chunks = []
    total = 0
    for chunk in stream:
        total += len(chunk)
        if total > MAX_RESULT_SIZE + _TAR_HEADER_OVERHEAD:
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
