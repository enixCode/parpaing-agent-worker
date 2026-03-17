"""Unit tests for job_runner - sanitization, output collection, webhooks."""

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from tower.job_runner import (
    _sanitize_error,
    _collect_output,
    _finish_and_webhook,
    _fire_webhook,
)
from tower.job_store import JobStatus


# ---------------------------------------------------------------------------
# Helpers: fake Docker exception classes with __module__ = "docker.errors"
# ---------------------------------------------------------------------------

def _make_docker_error(class_name: str, message: str = "something went wrong"):
    """Create a fake exception whose type lives in 'docker.errors'."""
    cls = type(class_name, (Exception,), {"__module__": "docker.errors"})
    return cls(message)


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
# 2. _collect_output  (mock container + worker functions)
# ===================================================================

class TestCollectOutput:
    @pytest.mark.asyncio
    @patch("tower.job_runner.extract_stderr", new_callable=AsyncMock, return_value=None)
    @patch("tower.job_runner.extract_result", new_callable=AsyncMock)
    async def test_success(self, mock_result, mock_stderr):
        mock_result.return_value = {"cost": 0.01, "message": "done"}
        container = MagicMock()

        output = await _collect_output("job-1", container, 0, "some logs")

        assert output["exit_code"] == 0
        assert output["result"] == {"cost": 0.01, "message": "done"}
        assert "error" not in output

    @pytest.mark.asyncio
    @patch("tower.job_runner.extract_stderr", new_callable=AsyncMock, return_value=None)
    @patch("tower.job_runner.extract_result", new_callable=AsyncMock, return_value=None)
    async def test_failed_no_result(self, mock_result, mock_stderr):
        container = MagicMock()

        output = await _collect_output("job-2", container, 1, "crash")

        assert output["exit_code"] == 1
        assert output["error"] == "Agent exited with code 1"

    @pytest.mark.asyncio
    @patch("tower.job_runner.extract_stderr", new_callable=AsyncMock, return_value=None)
    @patch("tower.job_runner.extract_result", new_callable=AsyncMock)
    async def test_result_with_error_key(self, mock_result, mock_stderr):
        mock_result.return_value = {"error": "prompt too long"}
        container = MagicMock()

        output = await _collect_output("job-3", container, 1, "")

        assert output["error"] == "prompt too long"
        assert "result" not in output

    @pytest.mark.asyncio
    @patch("tower.job_runner.extract_stderr", new_callable=AsyncMock, return_value=None)
    @patch("tower.job_runner.extract_result", new_callable=AsyncMock)
    async def test_result_raw_fallback(self, mock_result, mock_stderr):
        mock_result.return_value = {"result_raw": "not valid json output"}
        container = MagicMock()

        output = await _collect_output("job-4", container, 0, "")

        assert output["result_raw"] == "not valid json output"
        assert "result" not in output
        assert "error" not in output

    @pytest.mark.asyncio
    @patch("tower.job_runner.extract_stderr", new_callable=AsyncMock, return_value=None)
    @patch("tower.job_runner.extract_result", new_callable=AsyncMock, return_value=None)
    async def test_logs_truncated(self, mock_result, mock_stderr):
        long_logs = "x" * 5000
        container = MagicMock()

        output = await _collect_output("job-5", container, 0, long_logs)

        assert len(output["logs"]) == 2000

    @pytest.mark.asyncio
    @patch("tower.job_runner.extract_stderr", new_callable=AsyncMock)
    @patch("tower.job_runner.extract_result", new_callable=AsyncMock, return_value=None)
    async def test_stderr_included(self, mock_result, mock_stderr):
        mock_stderr.return_value = "warning: something"
        container = MagicMock()

        output = await _collect_output("job-6", container, 0, "")

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
    @patch("tower.job_runner._fire_webhook", new_callable=AsyncMock)
    async def test_webhook_fired_on_success(self, mock_webhook):
        store = MagicMock()
        store.finish_job = AsyncMock(return_value=True)
        output = {"exit_code": 0}

        await _finish_and_webhook("job-3", output, store, webhook_url="https://example.com/hook")

        mock_webhook.assert_awaited_once_with("https://example.com/hook", output)

    @pytest.mark.asyncio
    @patch("tower.job_runner._fire_webhook", new_callable=AsyncMock)
    async def test_webhook_not_fired_when_already_finished(self, mock_webhook):
        store = MagicMock()
        store.finish_job = AsyncMock(return_value=False)
        output = {"exit_code": 0}

        await _finish_and_webhook("job-4", output, store, webhook_url="https://example.com/hook")

        mock_webhook.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("tower.job_runner._fire_webhook", new_callable=AsyncMock)
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
    @patch("tower.job_runner.is_internal_host", return_value=False)
    @patch("tower.job_runner.httpx.AsyncClient")
    async def test_external_url_fires(self, mock_client_cls, mock_internal):
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await _fire_webhook("https://example.com/hook", {"exit_code": 0})

        mock_client.post.assert_awaited_once_with(
            "https://example.com/hook", json={"exit_code": 0},
        )

    @pytest.mark.asyncio
    @patch("tower.job_runner.is_internal_host", return_value=True)
    @patch("tower.job_runner.httpx.AsyncClient")
    async def test_internal_host_blocked(self, mock_client_cls, mock_internal):
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await _fire_webhook("http://localhost:8080/hook", {"exit_code": 0})

        mock_client.post.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("tower.job_runner.is_internal_host", return_value=False)
    @patch("tower.job_runner.httpx.AsyncClient")
    async def test_httpx_error_caught(self, mock_client_cls, mock_internal):
        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.ConnectError("connection refused")
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        # Should not raise
        await _fire_webhook("https://example.com/hook", {"exit_code": 0})
