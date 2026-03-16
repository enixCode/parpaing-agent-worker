"""Unit tests for worker helpers — config injection and result extraction."""

import io
import json
import tarfile
from unittest.mock import MagicMock, patch

import pytest

from tower.engines import EngineConfig
from tower.profiles import JobConfig
from tower.worker import _build_config_tar, _extract_file_from_archive, extract_result, extract_stderr


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def engine():
    return EngineConfig(
        id="claude-code", name="Claude Code", description="test",
        binary="claude",
        prompt_flag="-p", static_args=["--verbose"],
        flag_map={"model": "--model", "max_turns": "--max-turns"},
        list_join={}, output_mode="file", output_format="json",
        output_path="/output/result.json", env_auth=["ANTHROPIC_API_KEY"],
    )


@pytest.fixture
def minimal_config(engine):
    """Minimal JobConfig — no optional fields."""
    return JobConfig(
        engine=engine,
        prompt="Say hello",
        model="claude-opus-4-6",
        allowed_tools=[],
        max_turns=None,
        max_budget_usd=None,
        output_format="json",
        system_prompt=None,
        mcp_config=None,
        claude_md=None,
        plugins=[],
        hook_pre=None,
        hook_post=None,
        timeout=None,
    )


@pytest.fixture
def full_config(engine):
    """JobConfig with all optional fields set."""
    return JobConfig(
        engine=engine,
        prompt="Do stuff",
        model="claude-opus-4-6",
        allowed_tools=["Read", "Write"],
        max_turns=10,
        max_budget_usd=5.0,
        output_format="json",
        system_prompt="You are helpful.",
        mcp_config={"mcpServers": {"test": {"command": "node", "args": ["server.js"]}}},
        claude_md="# Instructions\nBe helpful.",
        plugins=["code-review", "linter"],
        hook_pre="#!/bin/bash\necho pre",
        hook_post="#!/bin/bash\necho post",
        timeout=600,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tar(name: str, content: bytes) -> list[bytes]:
    """Build a tar archive as a list of chunks (simulates Docker get_archive stream)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    buf.seek(0)
    return [buf.read()]


def _make_empty_tar() -> list[bytes]:
    """Build an empty tar archive."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w"):
        pass
    buf.seek(0)
    return [buf.read()]


def _make_dir_tar(name: str) -> list[bytes]:
    """Build a tar archive containing a directory entry (not a file)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.type = tarfile.DIRTYPE
        tar.addfile(info)
    buf.seek(0)
    return [buf.read()]


def _read_tar(data: bytes) -> dict[str, bytes]:
    """Read a tar archive and return {name: content} mapping."""
    result = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
        for member in tar.getmembers():
            f = tar.extractfile(member)
            result[member.name] = f.read() if f else b""
    return result


def _tar_members_ordered(data: bytes) -> list[str]:
    """Return member names in order from a tar archive."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
        return [m.name for m in tar.getmembers()]


def _tar_member_mode(data: bytes, name: str) -> int:
    """Return the file mode for a given member in a tar archive."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
        for m in tar.getmembers():
            if m.name == name:
                return m.mode
    raise KeyError(name)


async def _fake_to_thread(func, *args, **kwargs):
    """Replace asyncio.to_thread: call the function synchronously."""
    return func(*args, **kwargs)


# ===========================================================================
# 1. _build_config_tar
# ===========================================================================

class TestBuildConfigTarBasic:
    """Basic tar structure and mandatory files."""

    def test_contains_job_json(self, minimal_config):
        data = _build_config_tar(minimal_config)
        files = _read_tar(data)
        assert "config/job.json" in files

    def test_job_json_content(self, minimal_config):
        data = _build_config_tar(minimal_config)
        files = _read_tar(data)
        job = json.loads(files["config/job.json"])
        assert job["prompt"] == "Say hello"
        assert job["model"] == "claude-opus-4-6"
        assert job["dry_run"] is False

    def test_dry_run_flag(self, minimal_config):
        data = _build_config_tar(minimal_config, dry_run=True)
        files = _read_tar(data)
        job = json.loads(files["config/job.json"])
        assert job["dry_run"] is True

    def test_job_json_engine_section(self, minimal_config):
        data = _build_config_tar(minimal_config)
        files = _read_tar(data)
        job = json.loads(files["config/job.json"])
        assert job["engine"]["id"] == "claude-code"
        assert job["engine"]["binary"] == "claude"
        assert job["engine"]["prompt_flag"] == "-p"

    def test_contains_ready_marker(self, minimal_config):
        data = _build_config_tar(minimal_config)
        files = _read_tar(data)
        assert "config/.ready" in files

    def test_ready_marker_is_last(self, minimal_config):
        data = _build_config_tar(minimal_config)
        names = _tar_members_ordered(data)
        assert names[-1] == "config/.ready"

    def test_ready_marker_is_last_with_all_options(self, full_config):
        data = _build_config_tar(full_config)
        names = _tar_members_ordered(data)
        assert names[-1] == "config/.ready"

    def test_minimal_has_two_files(self, minimal_config):
        data = _build_config_tar(minimal_config)
        names = _tar_members_ordered(data)
        assert names == ["config/job.json", "config/.ready"]

    def test_uid_gid_set(self, minimal_config):
        data = _build_config_tar(minimal_config)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
            for member in tar.getmembers():
                assert member.uid == 1000
                assert member.gid == 1000


class TestBuildConfigTarOptionalFiles:
    """Optional files: claude_md, mcp_config, plugins."""

    def test_claude_md_included(self, engine):
        config = JobConfig(
            engine=engine, prompt="test", model="m", allowed_tools=[], max_turns=None,
            max_budget_usd=None, output_format="json", system_prompt=None, mcp_config=None,
            claude_md="# My instructions", plugins=[], hook_pre=None, hook_post=None, timeout=None,
        )
        data = _build_config_tar(config)
        files = _read_tar(data)
        assert files["config/CLAUDE.md"] == b"# My instructions"

    def test_claude_md_excluded_when_none(self, minimal_config):
        data = _build_config_tar(minimal_config)
        files = _read_tar(data)
        assert "config/CLAUDE.md" not in files

    def test_mcp_config_included(self, engine):
        mcp = {"mcpServers": {"fs": {"command": "node"}}}
        config = JobConfig(
            engine=engine, prompt="test", model="m", allowed_tools=[], max_turns=None,
            max_budget_usd=None, output_format="json", system_prompt=None, mcp_config=mcp,
            claude_md=None, plugins=[], hook_pre=None, hook_post=None, timeout=None,
        )
        data = _build_config_tar(config)
        files = _read_tar(data)
        assert json.loads(files["config/mcp.json"]) == mcp

    def test_mcp_config_excluded_when_none(self, minimal_config):
        data = _build_config_tar(minimal_config)
        files = _read_tar(data)
        assert "config/mcp.json" not in files

    def test_plugins_settings_json(self, engine):
        config = JobConfig(
            engine=engine, prompt="test", model="m", allowed_tools=[], max_turns=None,
            max_budget_usd=None, output_format="json", system_prompt=None, mcp_config=None,
            claude_md=None, plugins=["lint", "format"], hook_pre=None, hook_post=None, timeout=None,
        )
        data = _build_config_tar(config)
        files = _read_tar(data)
        settings = json.loads(files["config/settings.json"])
        assert settings == {"enabledPlugins": {"lint": True, "format": True}}

    def test_plugins_excluded_when_empty(self, minimal_config):
        data = _build_config_tar(minimal_config)
        files = _read_tar(data)
        assert "config/settings.json" not in files


class TestBuildConfigTarHooksInline:
    """Inline hooks (multiline strings in profile TOML)."""

    def test_inline_pre_hook(self, engine):
        config = JobConfig(
            engine=engine, prompt="test", model="m", allowed_tools=[], max_turns=None,
            max_budget_usd=None, output_format="json", system_prompt=None, mcp_config=None,
            claude_md=None, plugins=[], hook_pre="#!/bin/bash\necho hello", hook_post=None, timeout=None,
        )
        data = _build_config_tar(config)
        files = _read_tar(data)
        assert files["config/pre-job.sh"] == b"#!/bin/bash\necho hello"

    def test_inline_post_hook(self, engine):
        config = JobConfig(
            engine=engine, prompt="test", model="m", allowed_tools=[], max_turns=None,
            max_budget_usd=None, output_format="json", system_prompt=None, mcp_config=None,
            claude_md=None, plugins=[], hook_pre=None, hook_post="#!/bin/bash\necho done", timeout=None,
        )
        data = _build_config_tar(config)
        files = _read_tar(data)
        assert files["config/post-job.sh"] == b"#!/bin/bash\necho done"

    def test_inline_hooks_executable(self, engine):
        config = JobConfig(
            engine=engine, prompt="test", model="m", allowed_tools=[], max_turns=None,
            max_budget_usd=None, output_format="json", system_prompt=None, mcp_config=None,
            claude_md=None, plugins=[], hook_pre="#!/bin/bash\necho pre", hook_post="#!/bin/bash\necho post",
            timeout=None,
        )
        data = _build_config_tar(config)
        assert _tar_member_mode(data, "config/pre-job.sh") == 0o755
        assert _tar_member_mode(data, "config/post-job.sh") == 0o755

    def test_no_hooks_when_none(self, minimal_config):
        data = _build_config_tar(minimal_config)
        files = _read_tar(data)
        assert "config/pre-job.sh" not in files
        assert "config/post-job.sh" not in files


class TestBuildConfigTarHooksFile:
    """File-based hooks loaded from HOOKS_DIR."""

    def test_file_hook_loaded(self, engine, tmp_path):
        hook_file = tmp_path / "my-hook.sh"
        hook_file.write_bytes(b"#!/bin/bash\necho from file")
        config = JobConfig(
            engine=engine, prompt="test", model="m", allowed_tools=[], max_turns=None,
            max_budget_usd=None, output_format="json", system_prompt=None, mcp_config=None,
            claude_md=None, plugins=[], hook_pre="my-hook.sh", hook_post=None, timeout=None,
        )
        with patch("tower.worker.HOOKS_DIR", tmp_path):
            data = _build_config_tar(config)
        files = _read_tar(data)
        assert files["config/pre-job.sh"] == b"#!/bin/bash\necho from file"

    def test_file_hook_executable(self, engine, tmp_path):
        hook_file = tmp_path / "hook.sh"
        hook_file.write_bytes(b"#!/bin/bash\necho x")
        config = JobConfig(
            engine=engine, prompt="test", model="m", allowed_tools=[], max_turns=None,
            max_budget_usd=None, output_format="json", system_prompt=None, mcp_config=None,
            claude_md=None, plugins=[], hook_pre="hook.sh", hook_post=None, timeout=None,
        )
        with patch("tower.worker.HOOKS_DIR", tmp_path):
            data = _build_config_tar(config)
        assert _tar_member_mode(data, "config/pre-job.sh") == 0o755

    def test_path_traversal_blocked(self, engine, tmp_path):
        config = JobConfig(
            engine=engine, prompt="test", model="m", allowed_tools=[], max_turns=None,
            max_budget_usd=None, output_format="json", system_prompt=None, mcp_config=None,
            claude_md=None, plugins=[], hook_pre="../../etc/passwd", hook_post=None, timeout=None,
        )
        with patch("tower.worker.HOOKS_DIR", tmp_path):
            data = _build_config_tar(config)
        files = _read_tar(data)
        assert "config/pre-job.sh" not in files

    def test_hook_file_not_found_skipped(self, engine, tmp_path):
        config = JobConfig(
            engine=engine, prompt="test", model="m", allowed_tools=[], max_turns=None,
            max_budget_usd=None, output_format="json", system_prompt=None, mcp_config=None,
            claude_md=None, plugins=[], hook_pre="nonexistent.sh", hook_post=None, timeout=None,
        )
        with patch("tower.worker.HOOKS_DIR", tmp_path):
            data = _build_config_tar(config)
        files = _read_tar(data)
        assert "config/pre-job.sh" not in files

    def test_post_hook_file_loaded(self, engine, tmp_path):
        hook_file = tmp_path / "post.sh"
        hook_file.write_bytes(b"#!/bin/bash\necho post")
        config = JobConfig(
            engine=engine, prompt="test", model="m", allowed_tools=[], max_turns=None,
            max_budget_usd=None, output_format="json", system_prompt=None, mcp_config=None,
            claude_md=None, plugins=[], hook_pre=None, hook_post="post.sh", timeout=None,
        )
        with patch("tower.worker.HOOKS_DIR", tmp_path):
            data = _build_config_tar(config)
        files = _read_tar(data)
        assert files["config/post-job.sh"] == b"#!/bin/bash\necho post"


# ===========================================================================
# 2. _extract_file_from_archive
# ===========================================================================

class TestExtractFileFromArchive:
    """Pure function — no mocking needed, use synthetic tar bytes."""

    def test_normal_extraction(self):
        stream = _make_tar("result.json", b'{"ok": true}')
        content, size = _extract_file_from_archive(stream)
        assert content == b'{"ok": true}'
        assert size == len(b'{"ok": true}')

    def test_empty_tar(self):
        stream = _make_empty_tar()
        content, size = _extract_file_from_archive(stream)
        assert content == b""
        assert size == 0

    def test_directory_member_returns_empty(self):
        stream = _make_dir_tar("output/")
        content, size = _extract_file_from_archive(stream)
        assert content == b""
        assert size == 0

    @patch("tower.worker.MAX_RESULT_SIZE", 7000)
    def test_oversized_member_returns_empty_with_size(self):
        """member.size > MAX_RESULT_SIZE but tar fits under streaming cap.

        Tar minimum size is 10240 bytes (20 blocks). With MAX_RESULT_SIZE=7000,
        streaming cap is 11096 (> 10240). Content of 8000 > 7000 triggers
        the member.size check.
        """
        big_content = b"x" * 8000
        stream = _make_tar("result.json", big_content)
        content, size = _extract_file_from_archive(stream)
        assert content == b""
        assert size == 8000

    @patch("tower.worker.MAX_RESULT_SIZE", 50)
    def test_streaming_size_limit(self):
        """Total stream bytes exceeding MAX_RESULT_SIZE + 4096 triggers early abort."""
        # Create content larger than 50 + 4096 = 4146 bytes total tar size
        big_content = b"x" * 5000
        stream = _make_tar("result.json", big_content)
        content, size = _extract_file_from_archive(stream)
        assert content == b""
        assert size > 50

    def test_multiple_chunks(self):
        """Stream can arrive as multiple chunks."""
        full = _make_tar("result.json", b"hello")[0]
        mid = len(full) // 2
        stream = [full[:mid], full[mid:]]
        content, size = _extract_file_from_archive(stream)
        assert content == b"hello"

    def test_binary_content(self):
        raw = bytes(range(256))
        stream = _make_tar("data.bin", raw)
        content, size = _extract_file_from_archive(stream)
        assert content == raw
        assert size == 256


# ===========================================================================
# 3. extract_result (async — mock Docker container)
# ===========================================================================

class TestExtractResult:
    """Async tests with mocked Docker container."""

    @pytest.mark.asyncio
    async def test_valid_json(self):
        result_data = {"status": "completed", "output": "Hello"}
        tar_stream = _make_tar("result.json", json.dumps(result_data).encode())
        container = MagicMock()
        container.get_archive.return_value = (tar_stream, None)
        with patch("tower.worker.asyncio.to_thread", side_effect=_fake_to_thread):
            result = await extract_result(container)
        assert result == result_data

    @pytest.mark.asyncio
    async def test_invalid_json_fallback(self):
        tar_stream = _make_tar("result.json", b"not json {{{")
        container = MagicMock()
        container.get_archive.return_value = (tar_stream, None)
        with patch("tower.worker.asyncio.to_thread", side_effect=_fake_to_thread):
            result = await extract_result(container)
        assert "result_raw" in result
        assert result["result_raw"] == "not json {{{"

    @pytest.mark.asyncio
    @patch("tower.worker.MAX_RESULT_SIZE", 50)
    async def test_oversized_returns_error(self):
        big = b"x" * 200
        tar_stream = _make_tar("result.json", big)
        container = MagicMock()
        container.get_archive.return_value = (tar_stream, None)
        with patch("tower.worker.asyncio.to_thread", side_effect=_fake_to_thread):
            result = await extract_result(container)
        assert "error" in result
        assert "too large" in result["error"]

    @pytest.mark.asyncio
    async def test_get_archive_fails_returns_none(self):
        container = MagicMock()
        container.get_archive.side_effect = Exception("container gone")
        with patch("tower.worker.asyncio.to_thread", side_effect=_fake_to_thread):
            result = await extract_result(container)
        assert result is None


# ===========================================================================
# 4. extract_stderr (async — mock Docker container)
# ===========================================================================

class TestExtractStderr:
    """Async tests with mocked Docker container."""

    @pytest.mark.asyncio
    async def test_valid_stderr(self):
        tar_stream = _make_tar("stderr.log", b"some warning\n")
        container = MagicMock()
        container.get_archive.return_value = (tar_stream, None)
        with patch("tower.worker.asyncio.to_thread", side_effect=_fake_to_thread):
            result = await extract_stderr(container)
        assert result == "some warning\n"

    @pytest.mark.asyncio
    @patch("tower.worker.MAX_RESULT_SIZE", 50)
    async def test_oversized_returns_none(self):
        big = b"x" * 200
        tar_stream = _make_tar("stderr.log", big)
        container = MagicMock()
        container.get_archive.return_value = (tar_stream, None)
        with patch("tower.worker.asyncio.to_thread", side_effect=_fake_to_thread):
            result = await extract_stderr(container)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_archive_fails_returns_none(self):
        container = MagicMock()
        container.get_archive.side_effect = Exception("not found")
        with patch("tower.worker.asyncio.to_thread", side_effect=_fake_to_thread):
            result = await extract_stderr(container)
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_stderr_returns_none(self):
        tar_stream = _make_tar("stderr.log", b"")
        container = MagicMock()
        container.get_archive.return_value = (tar_stream, None)
        with patch("tower.worker.asyncio.to_thread", side_effect=_fake_to_thread):
            result = await extract_stderr(container)
        assert result is None

    @pytest.mark.asyncio
    async def test_long_stderr_truncated_to_2000(self):
        long_text = "A" * 5000
        tar_stream = _make_tar("stderr.log", long_text.encode())
        container = MagicMock()
        container.get_archive.return_value = (tar_stream, None)
        with patch("tower.worker.asyncio.to_thread", side_effect=_fake_to_thread):
            result = await extract_stderr(container)
        assert len(result) == 2000
        assert result == "A" * 2000
