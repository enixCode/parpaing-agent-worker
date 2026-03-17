"""Unit tests for job_store - pure functions, Job dataclass, and JobStore methods."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from tower.job_store import Job, JobStatus, JobStore, _parse_json, _row_to_job
from tower.models import AgentRunRequest, JobResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(**overrides) -> AgentRunRequest:
    defaults = {"agent_id": "test", "engine": "claude-code", "prompt": "test prompt"}
    defaults.update(overrides)
    return AgentRunRequest(**defaults)


def _make_row(**overrides) -> dict:
    """Return a dict that behaves like asyncpg.Record for __getitem__."""
    defaults = {
        "job_id": "test-abc123",
        "status": "pending",
        "request": {"agent_id": "test", "engine": "claude-code", "prompt": "test prompt"},
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "started_at": None,
        "finished_at": None,
        "container_id": None,
        "exit_code": None,
        "result": None,
        "error": None,
        "webhook_url": None,
    }
    defaults.update(overrides)
    return defaults


def _mock_pool():
    """Create a mock asyncpg pool with async methods."""
    pool = MagicMock()
    pool.execute = AsyncMock()
    pool.fetchrow = AsyncMock()
    pool.fetchval = AsyncMock()
    pool.fetch = AsyncMock()
    pool.acquire = MagicMock()
    return pool


# ===========================================================================
# _parse_json
# ===========================================================================

class TestParseJson:
    def test_none_returns_default(self):
        assert _parse_json(None, {"fallback": True}) == {"fallback": True}

    def test_dict_returned_as_is(self):
        d = {"key": "value"}
        assert _parse_json(d, {}) is d

    def test_valid_json_string_parsed(self):
        assert _parse_json('{"a": 1}', {}) == {"a": 1}

    def test_invalid_json_string_raises(self):
        with pytest.raises(json.JSONDecodeError):
            _parse_json("{bad json", {})

    def test_none_returns_none_default(self):
        assert _parse_json(None, None) is None

    def test_list_returned_as_is(self):
        lst = [1, 2, 3]
        assert _parse_json(lst, []) is lst


# ===========================================================================
# _row_to_job
# ===========================================================================

class TestRowToJob:
    def test_valid_row_all_fields(self):
        row = _make_row(
            status="running",
            container_id="ctr-1",
            exit_code=0,
            result={"output": "ok"},
            error=None,
            webhook_url="https://example.com/hook",
        )
        job = _row_to_job(row)
        assert job.job_id == "test-abc123"
        assert job.status == JobStatus.RUNNING
        assert job.request.agent_id == "test"
        assert job.container_id == "ctr-1"
        assert job.exit_code == 0
        assert job.result == {"output": "ok"}
        assert job.webhook_url == "https://example.com/hook"

    def test_unknown_request_fields_filtered(self):
        row = _make_row(request={
            "agent_id": "test",
            "engine": "claude-code",
            "prompt": "hi",
            "unknown_field": "should be dropped",
            "another_bad": 42,
        })
        job = _row_to_job(row)
        assert job.request.agent_id == "test"
        assert not hasattr(job.request, "unknown_field")

    def test_none_result(self):
        row = _make_row(result=None)
        job = _row_to_job(row)
        assert job.result is None

    def test_json_string_result_parsed(self):
        row = _make_row(result='{"cost": 0.01}')
        job = _row_to_job(row)
        assert job.result == {"cost": 0.01}

    def test_json_string_request_parsed(self):
        row = _make_row(request='{"agent_id": "test", "engine": "claude-code", "prompt": "hi"}')
        job = _row_to_job(row)
        assert job.request.agent_id == "test"

    def test_timestamps_preserved(self):
        ts = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        row = _make_row(created_at=ts, started_at=ts)
        job = _row_to_job(row)
        assert job.created_at == ts
        assert job.started_at == ts


# ===========================================================================
# Job.to_response
# ===========================================================================

class TestJobToResponse:
    def test_complete_job(self):
        req = _make_request()
        job = Job(
            job_id="test-abc123",
            status=JobStatus.COMPLETED,
            request=req,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            started_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
            finished_at=datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc),
            exit_code=0,
            result={"output": "done"},
            error=None,
        )
        resp = job.to_response()
        assert isinstance(resp, JobResponse)
        assert resp.job_id == "test-abc123"
        assert resp.status == JobStatus.COMPLETED
        assert resp.engine == "claude-code"
        assert resp.profile == "default"
        assert resp.exit_code == 0
        assert resp.result == {"output": "done"}

    def test_pending_job_no_optional_fields(self):
        req = _make_request()
        job = Job(
            job_id="test-xyz",
            status=JobStatus.PENDING,
            request=req,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        resp = job.to_response()
        assert resp.started_at is None
        assert resp.finished_at is None
        assert resp.exit_code is None
        assert resp.result is None
        assert resp.error is None

    def test_engine_and_profile_from_request(self):
        req = _make_request(profile="researcher")
        job = Job(
            job_id="test-1",
            status=JobStatus.RUNNING,
            request=req,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        resp = job.to_response()
        assert resp.engine == "claude-code"
        assert resp.profile == "researcher"


# ===========================================================================
# JobStatus enum
# ===========================================================================

class TestJobStatus:
    def test_all_values_exist(self):
        assert JobStatus.PENDING.value == "pending"
        assert JobStatus.RUNNING.value == "running"
        assert JobStatus.COMPLETED.value == "completed"
        assert JobStatus.FAILED.value == "failed"
        assert JobStatus.CANCELLED.value == "cancelled"

    def test_is_string_enum(self):
        assert isinstance(JobStatus.PENDING, str)

    def test_count(self):
        assert len(JobStatus) == 5


# ===========================================================================
# JobStore methods (mocked asyncpg pool)
# ===========================================================================

class TestJobStoreCreate:
    @pytest.mark.asyncio
    async def test_create_returns_job(self):
        store = JobStore(dsn="postgres://fake", ttl_hours=24)
        store._pool = _mock_pool()
        store._pool.execute.return_value = None

        req = _make_request()
        job = await store.create("test-abc123", req)

        assert job.job_id == "test-abc123"
        assert job.status == JobStatus.PENDING
        assert job.request is req
        store._pool.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_passes_webhook_url(self):
        store = JobStore(dsn="postgres://fake")
        store._pool = _mock_pool()

        req = _make_request()
        job = await store.create("test-1", req, webhook_url="https://example.com/hook")

        assert job.webhook_url == "https://example.com/hook"
        call_args = store._pool.execute.call_args[0]
        # args: (sql, job_id, agent_id, status, json_request, webhook_url)
        assert call_args[5] == "https://example.com/hook"

    @pytest.mark.asyncio
    async def test_create_serializes_request_as_json(self):
        store = JobStore(dsn="postgres://fake")
        store._pool = _mock_pool()

        req = _make_request()
        await store.create("test-1", req)

        call_args = store._pool.execute.call_args[0]
        # args: (sql, job_id, agent_id, status, json_request, webhook_url)
        parsed = json.loads(call_args[4])
        assert parsed["agent_id"] == "test"
        assert parsed["engine"] == "claude-code"


class TestJobStoreGet:
    @pytest.mark.asyncio
    async def test_get_existing(self):
        store = JobStore(dsn="postgres://fake")
        store._pool = _mock_pool()
        store._pool.fetchrow.return_value = _make_row()

        job = await store.get("test-abc123")

        assert job is not None
        assert job.job_id == "test-abc123"

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self):
        store = JobStore(dsn="postgres://fake")
        store._pool = _mock_pool()
        store._pool.fetchrow.return_value = None

        job = await store.get("nonexistent")

        assert job is None


class TestJobStoreStartJob:
    @pytest.mark.asyncio
    async def test_start_success(self):
        store = JobStore(dsn="postgres://fake")
        store._pool = _mock_pool()
        store._pool.fetchval.return_value = "test-abc123"

        result = await store.start_job("test-abc123", datetime.now(timezone.utc))

        assert result is True

    @pytest.mark.asyncio
    async def test_start_not_found(self):
        store = JobStore(dsn="postgres://fake")
        store._pool = _mock_pool()
        store._pool.fetchval.return_value = None

        result = await store.start_job("nonexistent", datetime.now(timezone.utc))

        assert result is False


class TestJobStoreSetContainer:
    @pytest.mark.asyncio
    async def test_set_container_success(self):
        store = JobStore(dsn="postgres://fake")
        store._pool = _mock_pool()
        store._pool.fetchval.return_value = "test-abc123"

        result = await store.set_container("test-abc123", "ctr-abc")

        assert result is True

    @pytest.mark.asyncio
    async def test_set_container_not_running(self):
        store = JobStore(dsn="postgres://fake")
        store._pool = _mock_pool()
        store._pool.fetchval.return_value = None

        result = await store.set_container("test-abc123", "ctr-abc")

        assert result is False


class TestJobStoreFinishJob:
    @pytest.mark.asyncio
    async def test_finish_success(self):
        store = JobStore(dsn="postgres://fake")
        store._pool = _mock_pool()
        store._pool.fetchval.return_value = "test-abc123"

        result = await store.finish_job(
            "test-abc123", JobStatus.COMPLETED,
            result={"output": "ok"}, exit_code=0,
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_finish_already_finished(self):
        store = JobStore(dsn="postgres://fake")
        store._pool = _mock_pool()
        store._pool.fetchval.return_value = None

        result = await store.finish_job("test-abc123", JobStatus.FAILED, error="timeout")

        assert result is False

    @pytest.mark.asyncio
    async def test_finish_serializes_result_as_json(self):
        store = JobStore(dsn="postgres://fake")
        store._pool = _mock_pool()
        store._pool.fetchval.return_value = "test-1"

        await store.finish_job("test-1", JobStatus.COMPLETED, result={"key": "val"})

        call_args = store._pool.fetchval.call_args[0]
        # $3 is the result JSON string
        assert json.loads(call_args[3]) == {"key": "val"}

    @pytest.mark.asyncio
    async def test_finish_none_result_passes_none(self):
        store = JobStore(dsn="postgres://fake")
        store._pool = _mock_pool()
        store._pool.fetchval.return_value = "test-1"

        await store.finish_job("test-1", JobStatus.FAILED, error="boom")

        call_args = store._pool.fetchval.call_args[0]
        # $3 is None when result is None
        assert call_args[3] is None


class TestJobStoreListAll:
    @pytest.mark.asyncio
    async def test_list_no_filter(self):
        store = JobStore(dsn="postgres://fake")
        store._pool = _mock_pool()
        store._pool.fetchval.return_value = 2
        store._pool.fetch.return_value = [
            _make_row(job_id="j1"),
            _make_row(job_id="j2"),
        ]

        jobs, total = await store.list_all()

        assert total == 2
        assert len(jobs) == 2
        assert jobs[0].job_id == "j1"

    @pytest.mark.asyncio
    async def test_list_with_status_filter(self):
        store = JobStore(dsn="postgres://fake")
        store._pool = _mock_pool()
        store._pool.fetchval.return_value = 1
        store._pool.fetch.return_value = [_make_row(status="running")]

        jobs, total = await store.list_all(status_filter="running")

        assert total == 1
        assert jobs[0].status == JobStatus.RUNNING

    @pytest.mark.asyncio
    async def test_list_invalid_status_raises(self):
        store = JobStore(dsn="postgres://fake")
        store._pool = _mock_pool()

        with pytest.raises(ValueError, match="Invalid status filter"):
            await store.list_all(status_filter="bogus")


class TestJobStoreCleanupOld:
    @pytest.mark.asyncio
    async def test_cleanup_parses_delete_counts(self):
        store = JobStore(dsn="postgres://fake", ttl_hours=24, max_retained=100)
        store._pool = _mock_pool()

        # Mock the context manager chain: pool.acquire() -> conn -> conn.transaction()
        mock_conn = MagicMock()
        mock_conn.execute = AsyncMock(side_effect=["DELETE 5", "DELETE 3"])
        mock_conn.transaction = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(), __aexit__=AsyncMock()
        ))

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        store._pool.acquire.return_value = mock_ctx

        removed = await store.cleanup_old()

        assert removed == 8

    @pytest.mark.asyncio
    async def test_cleanup_empty_result(self):
        store = JobStore(dsn="postgres://fake", ttl_hours=24, max_retained=100)
        store._pool = _mock_pool()

        mock_conn = MagicMock()
        mock_conn.execute = AsyncMock(side_effect=["DELETE 0", "DELETE 0"])
        mock_conn.transaction = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(), __aexit__=AsyncMock()
        ))

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        store._pool.acquire.return_value = mock_ctx

        removed = await store.cleanup_old()

        assert removed == 0


class TestJobStoreGetRunningJobs:
    @pytest.mark.asyncio
    async def test_returns_tuples(self):
        store = JobStore(dsn="postgres://fake")
        store._pool = _mock_pool()
        store._pool.fetch.return_value = [
            {"job_id": "j1", "container_id": "ctr-1"},
            {"job_id": "j2", "container_id": None},
        ]

        result = await store.get_running_jobs()

        assert result == [("j1", "ctr-1"), ("j2", None)]

    @pytest.mark.asyncio
    async def test_empty(self):
        store = JobStore(dsn="postgres://fake")
        store._pool = _mock_pool()
        store._pool.fetch.return_value = []

        result = await store.get_running_jobs()

        assert result == []


class TestJobStoreGetPendingJobs:
    @pytest.mark.asyncio
    async def test_returns_job_ids(self):
        store = JobStore(dsn="postgres://fake")
        store._pool = _mock_pool()
        store._pool.fetch.return_value = [
            {"job_id": "j1"},
            {"job_id": "j2"},
        ]

        result = await store.get_pending_jobs()

        assert result == ["j1", "j2"]

    @pytest.mark.asyncio
    async def test_empty(self):
        store = JobStore(dsn="postgres://fake")
        store._pool = _mock_pool()
        store._pool.fetch.return_value = []

        result = await store.get_pending_jobs()

        assert result == []
