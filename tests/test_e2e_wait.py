"""E2E: /jobs/{id}/wait endpoint - blocking wait for job completion."""

import time
import pytest

from conftest import DEFAULT_ENGINE


class TestWait:

    def test_wait_happy_path(self, client):
        """POST a dry_run job then GET /wait - should return completed result."""
        r = client.post("/jobs", json={
            "agent_id": "e2e-wait",
            "engine": DEFAULT_ENGINE,
            "prompt": "Say hello",
            "dry_run": True,
        })
        assert r.status_code == 202
        job_id = r.json()["job_id"]

        r = client.get(f"/jobs/{job_id}/wait", timeout=60)
        assert r.status_code == 200
        data = r.json()
        assert data["job_id"] == job_id
        assert data["status"] in ("completed", "failed")
        assert data["exit_code"] is not None
        assert data["finished_at"] is not None

    def test_wait_already_completed(self, client):
        """Calling /wait on an already-terminal job returns 200 immediately."""
        r = client.post("/jobs", json={
            "agent_id": "e2e-wait-done",
            "engine": DEFAULT_ENGINE,
            "prompt": "Say hello",
            "dry_run": True,
        })
        assert r.status_code == 202
        job_id = r.json()["job_id"]

        # Poll until terminal
        deadline = time.time() + 60
        while time.time() < deadline:
            r = client.get(f"/jobs/{job_id}")
            assert r.status_code == 200
            if r.json()["status"] in ("completed", "failed"):
                break
            time.sleep(1)
        else:
            pytest.fail(f"Job {job_id} did not reach terminal state within 60s")

        # Now call /wait - should return immediately with 200
        r = client.get(f"/jobs/{job_id}/wait", timeout=10)
        assert r.status_code == 200
        assert r.json()["job_id"] == job_id
        assert r.json()["status"] in ("completed", "failed")

    def test_wait_timeout(self, client):
        """GET /wait?timeout=1 returns 408 when job is still pending."""
        # dry_run=True job is queued but may not finish within 1s of the wait call
        r = client.post("/jobs", json={
            "agent_id": "e2e-wait-to",
            "engine": DEFAULT_ENGINE,
            "prompt": "Say hello",
            "dry_run": True,
        })
        assert r.status_code == 202
        job_id = r.json()["job_id"]

        # Hit /wait immediately with timeout=1 before the job can complete
        r = client.get(f"/jobs/{job_id}/wait?timeout=1", timeout=15)

        # If job happened to finish before the wait call, skip gracefully
        if r.status_code == 200:
            pytest.skip("dry_run job completed before wait call - environment too fast to test timeout")

        assert r.status_code == 408

    def test_wait_nonexistent(self, client):
        """GET /wait on a fake job id should return 404."""
        r = client.get("/jobs/fake-id-does-not-exist/wait")
        assert r.status_code == 404
