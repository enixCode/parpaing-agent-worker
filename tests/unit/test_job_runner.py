"""Unit tests for job_runner - sanitization, output collection, webhooks, execution."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import docker.errors
import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from tower.runner.executor import (
    _sanitize_error,
    _collect_output,
    _finish_and_webhook,
    _fire_webhook,
    _wait_and_finish,
    execute_job,
    recover_jobs,
    _readopt_container,
    cleanup_loop,
)
from tower.store import JobStatus


# ---------------------------------------------------------------------------
# Helpers: fake Docker exception classes with __module__ = "docker.errors"
# ---------------------------------------------------------------------------

def _make_docker_error(class_name: str, message: str = "something went wrong"):
    """Create a fake exception whose type lives in 'docker.errors'."""
    cls = type(class_name, (Exception,), {"__module__": "docker.errors"})
    return cls(message)


def _make_mock_runtime():
    """Create a mock Runtime with all async methods."""
    rt = AsyncMock()
    rt.extract_result = AsyncMock(return_value=None)
    rt.extract_stderr = AsyncMock(return_value=None)
    rt.wait_for_completion = AsyncMock(return_value={"StatusCode": 0})
    rt.get_logs = AsyncMock(return_value="")
    rt.inject_config = AsyncMock()
    rt.worker_alive = AsyncMock(return_value=True)
    rt.destroy_worker = AsyncMock()
    return rt


def _make_mock_pool(runtime=None):
    """Create a mock pool with a runtime."""
    pool = MagicMock()
    pool.runtime = runtime or _make_mock_runtime()
    pool.acquire = AsyncMock(return_value=("cid-1", "net-1"))
    pool.release = AsyncMock()
    return pool


# ===================================================================
# 1. _sanitize_error  (pure function)
# ===================================================================

class TestSanitizeErrorDockerTypes:
    def test_not_found_error(self):
        err = _make_docker_error("NotFound")
        assert _sanitize_error(err) == "Container not found"

    def test_api_error(self):
        err = _make_docker_error("APIError")
        assert _sanitize_error(err) == "Container runtime error"

    def test_other_docker_error(self):
        err = _make_docker_error("DockerException")
        assert _sanitize_error(err) == "Docker error"

    def test_image_not_found(self):
        err = _make_docker_error("ImageNotFound")
        assert _sanitize_error(err) == "Container not found"


class TestSanitizeErrorRedaction:
    def test_docker_internal_path_redacted(self):
        err = ValueError("file /var/lib/docker/overlay2/abc123/merged not found")
        result = _sanitize_error(err)
        assert "/var/lib/docker/" not in result
        assert "[redacted]" in result

    def test_container_hash_redacted(self):
        long_hash = "a" * 64
        err = ValueError(f"container {long_hash} exited")
        result = _sanitize_error(err)
        assert long_hash not in result
        assert "[redacted]" in result

    def test_app_path_redacted(self):
        err = ValueError("Error in /app/tower/worker.py line 42")
        result = _sanitize_error(err)
        assert "/app/" not in result
        assert "[redacted]" in result

    def test_docker_socket_redacted(self):
        err = ValueError("Cannot connect to unix:///var/run/docker.sock")
        result = _sanitize_error(err)
        assert "docker.sock" not in result
        assert "[redacted]" in result


class TestSanitizeErrorPassthrough:
    def test_normal_message_passed_through(self):
        err = ValueError("some normal error")
        assert _sanitize_error(err) == "some normal error"

    def test_truncated_to_500(self):
        err = ValueError("x" * 1000)
        result = _sanitize_error(err)
        assert len(result) == 500


# ===================================================================
# 2. _collect_output  (mock runtime)
# ===================================================================

class TestCollectOutput:
    @pytest.mark.asyncio
    async def test_success(self):
        runtime = _make_mock_runtime()
        runtime.extract_result.return_value = {"cost": 0.01, "message": "done"}

        output = await _collect_output("job-1", "wid-1", 0, "some logs", runtime)

        assert output["exit_code"] == 0
        assert output["result"] == {"cost": 0.01, "message": "done"}
        assert "error" not in output

    @pytest.mark.asyncio
    async def test_failed_no_result(self):
        runtime = _make_mock_runtime()

        output = await _collect_output("job-2", "wid-2", 1, "crash", runtime)

        assert output["exit_code"] == 1
        assert output["error"] == "Agent exited with code 1"

    @pytest.mark.asyncio
    async def test_result_with_error_key(self):
        runtime = _make_mock_runtime()
        runtime.extract_result.return_value = {"error": "prompt too long"}

        output = await _collect_output("job-3", "wid-3", 1, "", runtime)

        assert output["error"] == "prompt too long"
        assert "result" not in output

    @pytest.mark.asyncio
    async def test_result_raw_fallback(self):
        runtime = _make_mock_runtime()
        runtime.extract_result.return_value = {"result_raw": "not valid json output"}

        output = await _collect_output("job-4", "wid-4", 0, "", runtime)

        assert output["result_raw"] == "not valid json output"
        assert "result" not in output
        assert "error" not in output

    @pytest.mark.asyncio
    async def test_logs_truncated(self):
        runtime = _make_mock_runtime()
        long_logs = "x" * 5000

        output = await _collect_output("job-5", "wid-5", 0, long_logs, runtime)

        assert len(output["logs"]) == 2000

    @pytest.mark.asyncio
    async def test_stderr_included(self):
        runtime = _make_mock_runtime()
        runtime.extract_stderr.return_value = "warning: something"

        output = await _collect_output("job-6", "wid-6", 0, "", runtime)

        assert output["stderr"] == "warning: something"


# ===================================================================
# 3. _finish_and_webhook  (mock store + webhook)
# ===================================================================

class TestFinishAndWebhook:
    @pytest.mark.asyncio
    async def test_exit_0_completed(self):
        store = MagicMock()
        store.finish_job = AsyncMock(return_value=True)
        output = {"exit_code": 0, "result": {"ok": True}}

        result = await _finish_and_webhook("job-1", output, store)

        store.finish_job.assert_awaited_once_with(
            "job-1", JobStatus.COMPLETED, result=output, exit_code=0,
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_exit_1_failed(self):
        store = MagicMock()
        store.finish_job = AsyncMock(return_value=True)
        output = {"exit_code": 1, "error": "crash"}

        await _finish_and_webhook("job-2", output, store)

        store.finish_job.assert_awaited_once_with(
            "job-2", JobStatus.FAILED, result=output, exit_code=1,
        )

    @pytest.mark.asyncio
    @patch("tower.runner.executor._fire_webhook", new_callable=AsyncMock)
    async def test_webhook_fired_on_success(self, mock_webhook):
        store = MagicMock()
        store.finish_job = AsyncMock(return_value=True)
        output = {"exit_code": 0}

        await _finish_and_webhook("job-3", output, store, webhook_url="https://example.com/hook")

        mock_webhook.assert_awaited_once_with("https://example.com/hook", output)

    @pytest.mark.asyncio
    @patch("tower.runner.executor._fire_webhook", new_callable=AsyncMock)
    async def test_webhook_not_fired_when_already_finished(self, mock_webhook):
        store = MagicMock()
        store.finish_job = AsyncMock(return_value=False)
        output = {"exit_code": 0}

        await _finish_and_webhook("job-4", output, store, webhook_url="https://example.com/hook")

        mock_webhook.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("tower.runner.executor._fire_webhook", new_callable=AsyncMock)
    async def test_webhook_not_fired_when_url_none(self, mock_webhook):
        store = MagicMock()
        store.finish_job = AsyncMock(return_value=True)
        output = {"exit_code": 0}

        await _finish_and_webhook("job-5", output, store, webhook_url=None)

        mock_webhook.assert_not_awaited()


# ===================================================================
# 4. _fire_webhook  (mock httpx + is_internal_host)
# ===================================================================

class TestFireWebhook:
    @pytest.mark.asyncio
    @patch("tower.runner.executor.is_internal_host", return_value=False)
    @patch("tower.runner.executor.httpx.AsyncClient")
    async def test_external_url_fires(self, mock_client_cls, mock_internal):
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await _fire_webhook("https://example.com/hook", {"exit_code": 0})

        mock_client.post.assert_awaited_once_with(
            "https://example.com/hook", json={"exit_code": 0},
        )

    @pytest.mark.asyncio
    @patch("tower.runner.executor.is_internal_host", return_value=True)
    @patch("tower.runner.executor.httpx.AsyncClient")
    async def test_internal_host_blocked(self, mock_client_cls, mock_internal):
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await _fire_webhook("http://localhost:8080/hook", {"exit_code": 0})

        mock_client.post.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("tower.runner.executor.is_internal_host", return_value=False)
    @patch("tower.runner.executor.httpx.AsyncClient")
    async def test_httpx_error_caught(self, mock_client_cls, mock_internal):
        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.ConnectError("connection refused")
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        # Should not raise
        await _fire_webhook("https://example.com/hook", {"exit_code": 0})


# ===================================================================
# 5. _wait_and_finish  (mock runtime via pool)
# ===================================================================

class TestWaitAndFinish:
    @pytest.mark.asyncio
    async def test_success_releases_container(self):
        runtime = _make_mock_runtime()
        runtime.wait_for_completion.return_value = {"StatusCode": 0}
        runtime.get_logs.return_value = "some logs"
        runtime.extract_result.return_value = {"ok": True}
        pool = _make_mock_pool(runtime)
        store = MagicMock()
        store.finish_job = AsyncMock(return_value=True)

        await _wait_and_finish("job-1", "cid-1", 60, store, pool)

        store.finish_job.assert_awaited_once()
        pool.release.assert_awaited_once_with("cid-1", job_id="job-1")

    @pytest.mark.asyncio
    async def test_failed_exit_code(self):
        runtime = _make_mock_runtime()
        runtime.wait_for_completion.return_value = {"StatusCode": 1}
        runtime.get_logs.return_value = "crash"
        pool = _make_mock_pool(runtime)
        store = MagicMock()
        store.finish_job = AsyncMock(return_value=True)

        await _wait_and_finish("job-2", "cid-2", 60, store, pool)

        args = store.finish_job.call_args
        assert args[0][1] == JobStatus.FAILED

    @pytest.mark.asyncio
    async def test_timeout_finishes_then_releases(self):
        runtime = _make_mock_runtime()
        runtime.wait_for_completion.side_effect = asyncio.TimeoutError()
        pool = _make_mock_pool(runtime)
        store = MagicMock()
        store.finish_job = AsyncMock(return_value=True)

        await _wait_and_finish("job-3", "cid-3", 1, store, pool)

        store.finish_job.assert_awaited_once()
        assert store.finish_job.call_args[1]["error"] == "Timed out after 1s"
        pool.release.assert_awaited_once_with("cid-3", job_id="job-3")

    @pytest.mark.asyncio
    async def test_already_cancelled_skips_release(self):
        """If finish_job returns False (job already cancelled), skip release."""
        runtime = _make_mock_runtime()
        runtime.wait_for_completion.return_value = {"StatusCode": 0}
        runtime.get_logs.return_value = ""
        pool = _make_mock_pool(runtime)
        store = MagicMock()
        store.finish_job = AsyncMock(return_value=False)

        await _wait_and_finish("job-4", "cid-4", 60, store, pool)

        pool.release.assert_not_awaited()


# ===================================================================
# 6. execute_job  (mock everything)
# ===================================================================

def _make_fake_job(job_id, status=JobStatus.PENDING):
    job = MagicMock()
    job.job_id = job_id
    job.status = status
    job.request = MagicMock()
    job.webhook_url = None
    return job


class TestExecuteJob:
    @pytest.mark.asyncio
    async def test_cancelled_job_returns_early(self):
        store = MagicMock()
        store.get = AsyncMock(return_value=_make_fake_job("j1", JobStatus.CANCELLED))
        pool = _make_mock_pool()
        sem = asyncio.Semaphore(1)

        await execute_job("j1", store, sem, pool)

        store.start_job.assert_not_called() if hasattr(store, 'start_job') else None

    @pytest.mark.asyncio
    async def test_missing_job_returns_early(self):
        store = MagicMock()
        store.get = AsyncMock(return_value=None)
        pool = _make_mock_pool()
        sem = asyncio.Semaphore(1)

        await execute_job("j2", store, sem, pool)

    @pytest.mark.asyncio
    async def test_already_taken_returns_early(self):
        store = MagicMock()
        store.get = AsyncMock(return_value=_make_fake_job("j3"))
        store.start_job = AsyncMock(return_value=False)
        pool = _make_mock_pool()
        sem = asyncio.Semaphore(1)

        await execute_job("j3", store, sem, pool)

        pool.acquire.assert_not_called() if hasattr(pool, 'acquire') else None

    @pytest.mark.asyncio
    @patch("tower.runner.executor._wait_and_finish", new_callable=AsyncMock)
    @patch("tower.runner.executor.resolve_config")
    async def test_cancelled_during_acquire_releases(self, mock_resolve, mock_wait):
        config = MagicMock()
        config.timeout = 60
        mock_resolve.return_value = config

        store = MagicMock()
        store.get = AsyncMock(return_value=_make_fake_job("j4"))
        store.start_job = AsyncMock(return_value=True)
        store.set_container = AsyncMock(return_value=False)  # cancelled during acquire
        store.finish_job = AsyncMock(return_value=True)

        pool = _make_mock_pool()
        sem = asyncio.Semaphore(1)

        mock_duration = MagicMock()
        with patch("tower.metrics.JOB_DURATION", mock_duration):
            await execute_job("j4", store, sem, pool)

        pool.release.assert_awaited_once_with("cid-1", job_id="j4")
        pool.runtime.inject_config.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("tower.runner.executor._wait_and_finish", new_callable=AsyncMock)
    @patch("tower.runner.executor.resolve_config")
    async def test_exception_releases_and_marks_failed(self, mock_resolve, mock_wait):
        mock_resolve.side_effect = ValueError("bad config")

        store = MagicMock()
        store.get = AsyncMock(return_value=_make_fake_job("j5"))
        store.start_job = AsyncMock(return_value=True)
        store.finish_job = AsyncMock(return_value=True)

        pool = _make_mock_pool()
        pool.acquire = AsyncMock()
        sem = asyncio.Semaphore(1)

        mock_duration = MagicMock()
        with patch("tower.metrics.JOB_DURATION", mock_duration):
            await execute_job("j5", store, sem, pool)

        store.finish_job.assert_awaited_once()
        args = store.finish_job.call_args
        assert args[0][1] == JobStatus.FAILED


# ===================================================================
# 7. recover_jobs  (mock store + runtime)
# ===================================================================

class TestRecoverJobs:
    @pytest.mark.asyncio
    async def test_no_pending_no_running(self):
        store = MagicMock()
        store.get_pending_jobs = AsyncMock(return_value=[])
        store.get_running_jobs = AsyncMock(return_value=[])
        sem = asyncio.Semaphore(1)
        pool = _make_mock_pool()

        await recover_jobs(store, sem, pool)

    @pytest.mark.asyncio
    async def test_container_not_found_marks_failed(self):
        store = MagicMock()
        store.get_pending_jobs = AsyncMock(return_value=[])
        store.get_running_jobs = AsyncMock(return_value=[("j1", "cid-dead")])
        store.finish_job = AsyncMock(return_value=True)
        sem = asyncio.Semaphore(1)
        runtime = _make_mock_runtime()
        runtime.worker_alive.return_value = False
        pool = _make_mock_pool(runtime)

        await recover_jobs(store, sem, pool)

        store.finish_job.assert_awaited_once()
        assert "container lost" in store.finish_job.call_args[1]["error"]

    @pytest.mark.asyncio
    async def test_no_container_id_marks_failed(self):
        store = MagicMock()
        store.get_pending_jobs = AsyncMock(return_value=[])
        store.get_running_jobs = AsyncMock(return_value=[("j2", None)])
        store.finish_job = AsyncMock(return_value=True)
        sem = asyncio.Semaphore(1)
        pool = _make_mock_pool()

        await recover_jobs(store, sem, pool)

        store.finish_job.assert_awaited_once()


# ===================================================================
# 8. _readopt_container
# ===================================================================

class TestReadoptContainer:
    @pytest.mark.asyncio
    async def test_job_not_found_returns(self):
        store = MagicMock()
        store.get = AsyncMock(return_value=None)
        pool = _make_mock_pool()

        await _readopt_container("j1", "cid-1", store, pool)

        pool.release.assert_not_called() if hasattr(pool, 'release') else None

    @pytest.mark.asyncio
    @patch("tower.runner.executor._wait_and_finish", new_callable=AsyncMock)
    @patch("tower.runner.executor.resolve_config")
    async def test_config_error_uses_default_timeout(self, mock_resolve, mock_wait):
        mock_resolve.side_effect = ValueError("bad profile")
        job = _make_fake_job("j2")
        store = MagicMock()
        store.get = AsyncMock(return_value=job)
        pool = _make_mock_pool()

        await _readopt_container("j2", "cid-2", store, pool)

        # Should use WORKER_TIMEOUT_SECONDS as fallback
        mock_wait.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("tower.runner.executor._wait_and_finish", new_callable=AsyncMock)
    @patch("tower.runner.executor.resolve_config")
    async def test_exception_releases_and_marks_failed(self, mock_resolve, mock_wait):
        mock_resolve.return_value = MagicMock(timeout=60)
        mock_wait.side_effect = Exception("boom")
        job = _make_fake_job("j3")
        store = MagicMock()
        store.get = AsyncMock(return_value=job)
        store.finish_job = AsyncMock(return_value=True)
        pool = _make_mock_pool()

        await _readopt_container("j3", "cid-3", store, pool)

        pool.release.assert_awaited_once_with("cid-3", job_id="j3")
        store.finish_job.assert_awaited_once()
        assert store.finish_job.call_args[0][1] == JobStatus.FAILED


# ===================================================================
# 9. cleanup_loop
# ===================================================================

class TestCleanupLoop:
    @pytest.mark.asyncio
    async def test_runs_cleanup_and_stops(self):
        store = MagicMock()
        store.cleanup_old = AsyncMock(return_value=5)
        call_count = 0

        original_sleep = asyncio.sleep

        async def fake_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError
            await original_sleep(0)

        with patch("tower.runner.executor.asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await cleanup_loop(store)

        store.cleanup_old.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handles_cleanup_exception(self):
        store = MagicMock()
        store.cleanup_old = AsyncMock(side_effect=Exception("db down"))
        call_count = 0

        async def fake_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError

        with patch("tower.runner.executor.asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await cleanup_loop(store)

        # Should not crash - exception is caught and loop continues
        store.cleanup_old.assert_awaited_once()
