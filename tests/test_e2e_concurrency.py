"""E2E: Concurrent job creation - multiple jobs in parallel."""

import time

from conftest import DEFAULT_ENGINE


def _create_job(client, agent_id):
    """Create a dry-run job and return (job_id, status_code)."""
    r = client.post("/jobs", json={
        "agent_id": agent_id,
        "engine": DEFAULT_ENGINE,
        "prompt": "Say hello",
        "dry_run": True,
    })
    return r.json().get("job_id"), r.status_code


def test_concurrent_job_creation(client):
    """Create 5 jobs rapidly - all should be accepted."""
    job_ids = []
    for i in range(5):
        job_id, status = _create_job(client, f"e2e-conc-{i}")
        assert status == 202, f"Job {i} rejected with status {status}"
        job_ids.append(job_id)

    # All should appear in job list
    r = client.get("/jobs")
    data = r.json()
    jobs_list = data["jobs"] if isinstance(data, dict) else data
    listed_ids = {j["job_id"] for j in jobs_list}
    for jid in job_ids:
        assert jid in listed_ids, f"Job {jid} not found in list"

    # Wait for all to finish
    deadline = time.time() + 180
    while time.time() < deadline:
        r = client.get("/jobs")
        data = r.json()
        all_jobs = data["jobs"] if isinstance(data, dict) else data
        statuses = {j["job_id"]: j["status"] for j in all_jobs if j["job_id"] in job_ids}
        if all(s in ("completed", "failed", "cancelled") for s in statuses.values()):
            break
        time.sleep(3)

    # Verify all finished
    for jid in job_ids:
        r = client.get(f"/jobs/{jid}")
        assert r.json()["status"] in ("completed", "failed", "cancelled")
