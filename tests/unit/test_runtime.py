"""Unit tests for runtime abstraction - factory, shared filesystem ops, Swarm-specific."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tower.runtime import Runtime, ComposeRuntime, SwarmRuntime, create_runtime


# ===========================================================================
# 1. Factory
# ===========================================================================

class TestCreateRuntime:
    def test_compose_mode(self):
        rt = create_runtime("compose")
        assert isinstance(rt, ComposeRuntime)

    def test_swarm_mode(self):
        rt = create_runtime("swarm")
        assert isinstance(rt, SwarmRuntime)

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError, match="Unknown RUNTIME_MODE"):
            create_runtime("kubernetes")

    def test_empty_mode_raises(self):
        with pytest.raises(ValueError):
            create_runtime("")


# ===========================================================================
# Helpers
# ===========================================================================

def _make_runtime(tmp_path, cls=SwarmRuntime, worker_id="svc-1", worker_name="agent-warm-test"):
    """Create a Runtime with jobs_dir pointing to tmp_path and a known worker name."""
    rt = cls()
    rt._jobs_dir = tmp_path
    rt._worker_names[worker_id] = worker_name
    return rt


def _setup_dirs(tmp_path, worker_name):
    """Create config/output dirs that create_worker would have made."""
    config_dir = tmp_path / worker_name / "config"
    output_dir = tmp_path / worker_name / "output"
    config_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir.chmod(0o777)
    return config_dir, output_dir


def _make_config_mock():
    config = MagicMock()
    config.prompt = "hello"
    config.allowed_tools = []
    config.max_turns = None
    config.max_budget_usd = None
    config.model = "test-model"
    config.output_format = "json"
    config.system_prompt = None
    config.mcp_config = None
    config.claude_md = None
    config.plugins = []
    config.hook_pre = None
    config.hook_post = None
    config.engine = MagicMock(
        id="test", binary="test", prompt_flag="-p",
        static_args=[], flag_map={}, list_join={},
        output_mode="stdout", output_format="json", output_path=None,
    )
    return config


# ===========================================================================
# 2. Shared inject_config (filesystem - tested via SwarmRuntime)
# ===========================================================================

class TestInjectConfig:
    @pytest.mark.asyncio
    async def test_writes_config_files(self, tmp_path):
        rt = _make_runtime(tmp_path)
        _setup_dirs(tmp_path, "agent-warm-test")

        await rt.inject_config("svc-1", _make_config_mock(), job_id="job-abc")

        config_dir = tmp_path / "agent-warm-test" / "config"
        assert (config_dir / "job.json").exists()
        assert (config_dir / ".ready").exists()
        job_data = json.loads((config_dir / "job.json").read_text())
        assert job_data["prompt"] == "hello"

    @pytest.mark.asyncio
    async def test_claude_md_written(self, tmp_path):
        rt = _make_runtime(tmp_path)
        _setup_dirs(tmp_path, "agent-warm-test")
        config = _make_config_mock()
        config.claude_md = "# Instructions"

        await rt.inject_config("svc-1", config, job_id="job-xyz")

        assert (tmp_path / "agent-warm-test" / "config" / "CLAUDE.md").read_text() == "# Instructions"


# ===========================================================================
# 3. Shared extract_result (filesystem)
# ===========================================================================

class TestExtractResult:
    @pytest.mark.asyncio
    async def test_valid_json(self, tmp_path):
        rt = _make_runtime(tmp_path)
        _, output_dir = _setup_dirs(tmp_path, "agent-warm-test")
        (output_dir / "result.json").write_text('{"status": "ok"}')

        result = await rt.extract_result("svc-1")
        assert result == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_missing_returns_none(self, tmp_path):
        rt = _make_runtime(tmp_path)
        result = await rt.extract_result("svc-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_json_fallback(self, tmp_path):
        rt = _make_runtime(tmp_path)
        _, output_dir = _setup_dirs(tmp_path, "agent-warm-test")
        (output_dir / "result.json").write_text("not json {{{")

        result = await rt.extract_result("svc-1")
        assert "result_raw" in result

    @pytest.mark.asyncio
    @patch("tower.runtime.MAX_RESULT_SIZE", 10)
    async def test_oversized_returns_error(self, tmp_path):
        rt = _make_runtime(tmp_path)
        _, output_dir = _setup_dirs(tmp_path, "agent-warm-test")
        (output_dir / "result.json").write_text("x" * 100)

        result = await rt.extract_result("svc-1")
        assert "error" in result
        assert "too large" in result["error"]


# ===========================================================================
# 4. Shared extract_stderr (filesystem)
# ===========================================================================

class TestExtractStderr:
    @pytest.mark.asyncio
    async def test_valid_stderr(self, tmp_path):
        rt = _make_runtime(tmp_path)
        _, output_dir = _setup_dirs(tmp_path, "agent-warm-test")
        (output_dir / "stderr.log").write_bytes(b"some warning")

        result = await rt.extract_stderr("svc-1")
        assert result == "some warning"

    @pytest.mark.asyncio
    async def test_missing_returns_none(self, tmp_path):
        rt = _make_runtime(tmp_path)
        result = await rt.extract_stderr("svc-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_returns_none(self, tmp_path):
        rt = _make_runtime(tmp_path)
        _, output_dir = _setup_dirs(tmp_path, "agent-warm-test")
        (output_dir / "stderr.log").write_bytes(b"")

        result = await rt.extract_stderr("svc-1")
        assert result is None


# ===========================================================================
# 5. Shared cleanup_orphan_dirs
# ===========================================================================

class TestCleanupOrphanDirs:
    @pytest.mark.asyncio
    async def test_removes_orphan_dirs(self, tmp_path):
        rt = _make_runtime(tmp_path)
        (tmp_path / "agent-warm-orphan").mkdir()
        (tmp_path / "agent-warm-active").mkdir()

        removed = await rt.cleanup_orphan_dirs({"agent-warm-active"})
        assert removed == 1
        assert not (tmp_path / "agent-warm-orphan").exists()
        assert (tmp_path / "agent-warm-active").exists()

    @pytest.mark.asyncio
    async def test_no_dir_no_error(self, tmp_path):
        rt = _make_runtime(tmp_path)
        rt._jobs_dir = tmp_path / "nonexistent"
        removed = await rt.cleanup_orphan_dirs(set())
        assert removed == 0


# ===========================================================================
# 6. SwarmRuntime - destroy_worker (cleanup)
# ===========================================================================

class TestSwarmDestroyWorker:
    @pytest.mark.asyncio
    async def test_cleanup_directory(self, tmp_path):
        rt = _make_runtime(tmp_path)
        _setup_dirs(tmp_path, "agent-warm-test")
        (tmp_path / "agent-warm-test" / "config" / "job.json").write_text("{}")

        with patch("tower.runtime.docker_client") as mock_dc:
            mock_dc.return_value.services.get.side_effect = Exception("not found")
            await rt.destroy_worker("svc-1", job_id="job-1")

        assert not (tmp_path / "agent-warm-test").exists()


# ===========================================================================
# 7. SwarmRuntime - wait_for_completion (polling)
# ===========================================================================

class TestSwarmWaitForCompletion:
    @pytest.mark.asyncio
    async def test_completed_task(self):
        rt = SwarmRuntime()
        mock_svc = MagicMock()
        mock_svc.tasks.return_value = [{
            "Status": {"State": "complete", "ContainerStatus": {"ExitCode": 0}}
        }]
        with patch("tower.runtime.docker_client") as mock_dc:
            mock_dc.return_value.services.get.return_value = mock_svc
            result = await rt.wait_for_completion("svc-1", timeout=10)
        assert result == {"StatusCode": 0}

    @pytest.mark.asyncio
    async def test_failed_task(self):
        rt = SwarmRuntime()
        mock_svc = MagicMock()
        mock_svc.tasks.return_value = [{
            "Status": {"State": "failed", "ContainerStatus": {"ExitCode": 1}}
        }]
        with patch("tower.runtime.docker_client") as mock_dc:
            mock_dc.return_value.services.get.return_value = mock_svc
            result = await rt.wait_for_completion("svc-1", timeout=10)
        assert result == {"StatusCode": 1}

    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        rt = SwarmRuntime()
        mock_svc = MagicMock()
        mock_svc.tasks.return_value = [{"Status": {"State": "running"}}]
        with patch("tower.runtime.docker_client") as mock_dc, \
             patch("tower.runtime._SWARM_POLL_INTERVAL", 0.01):
            mock_dc.return_value.services.get.return_value = mock_svc
            with pytest.raises(asyncio.TimeoutError):
                await rt.wait_for_completion("svc-1", timeout=0.05)
