"""E2E: Full job lifecycle — create, poll, list, cancel."""

import time
import pytest

from conftest import DEFAULT_ENGINE


def _wait_for_status(client, job_id, target_statuses, timeout=120):
    """Poll until job reaches one of the target statuses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/jobs/{job_id}")
        assert r.status_code == 200
        status = r.json()["status"]
        if status in target_statuses:
            return r.json()
        time.sleep(2)
    pytest.fail(f"Job {job_id} did not reach {target_statuses} within {timeout}s (last: {status})")


class TestDryRun:
    """Dry-run jobs skip Claude but exercise the full pipeline."""

    def test_create_dry_run_job(self, client):
        r = client.post("/jobs", json={
            "agent_id": "e2e-dry",
            "engine": DEFAULT_ENGINE,
            "prompt": "Say hello",
            "dry_run": True,
        })
        assert r.status_code == 202
        data = r.json()
        assert "job_id" in data
        assert data["status"] == "pending"
        self.__class__.job_id = data["job_id"]

    def test_poll_until_done(self, client):
        job_id = self.__class__.job_id
        result = _wait_for_status(client, job_id, {"completed", "failed"})
        assert result["exit_code"] is not None
        assert result["started_at"] is not None
        assert result["finished_at"] is not None

    def test_get_job_details(self, client):
        job_id = self.__class__.job_id
        r = client.get(f"/jobs/{job_id}")
        assert r.status_code == 200
        data = r.json()
        assert data["job_id"] == job_id
        assert data["status"] in ("completed", "failed")


class TestCancel:
    """Create a job and cancel it before it finishes."""

    def test_create_and_cancel(self, client):
        # Use a long-running prompt so we have time to cancel
        r = client.post("/jobs", json={
            "agent_id": "e2e-cancel",
            "engine": DEFAULT_ENGINE,
            "prompt": "Count slowly from 1 to 1000000, one number per line",
            "dry_run": True,
        })
        assert r.status_code == 202
        job_id = r.json()["job_id"]

        # Try to cancel — dry-run jobs may finish very fast
        time.sleep(1)

        r = client.delete(f"/jobs/{job_id}")
        # 200 if still running/pending, 409 if already finished
        assert r.status_code in (200, 409)

        if r.status_code == 200:
            assert r.json()["status"] == "cancelled"
            r = client.get(f"/jobs/{job_id}")
            assert r.json()["status"] == "cancelled"
        else:
            # Job finished before we could cancel — verify it's in a terminal state
            r = client.get(f"/jobs/{job_id}")
            assert r.json()["status"] in ("completed", "failed")

    def test_cancel_already_cancelled(self, client):
        """Double cancel should return 409."""
        r = client.post("/jobs", json={
            "agent_id": "e2e-dblcancel",
            "engine": DEFAULT_ENGINE,
            "prompt": "hello",
            "dry_run": True,
        })
        job_id = r.json()["job_id"]
        time.sleep(1)

        # First cancel
        client.delete(f"/jobs/{job_id}")

        # Second cancel → 409
        r = client.delete(f"/jobs/{job_id}")
        assert r.status_code == 409


class TestListJobs:
    """List and filter jobs."""

    def test_list_all_jobs(self, client):
        r = client.get("/jobs")
        assert r.status_code == 200
        data = r.json()
        jobs = data["jobs"] if isinstance(data, dict) else data
        assert isinstance(jobs, list)

    def test_list_jobs_filter_cancelled(self, client):
        r = client.get("/jobs?status=cancelled")
        assert r.status_code == 200
        data = r.json()
        jobs = data["jobs"] if isinstance(data, dict) else data
        for job in jobs:
            assert job["status"] == "cancelled"

    def test_list_jobs_filter_nonexistent_status(self, client):
        """Invalid status filter → 422 with clear error message."""
        r = client.get("/jobs?status=banana")
        assert r.status_code == 422


class TestRemovedEndpoints:
    """Endpoints that were removed should return 404/405."""

    def test_result_url_removed(self, client):
        r = client.get("/jobs/fake-id/result-url")
        # Endpoint removed — FastAPI returns 404 (no route match)
        assert r.status_code in (404, 405)
