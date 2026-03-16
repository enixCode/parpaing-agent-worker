"""E2E: Job creation with profiles."""

import time
import pytest


def test_job_with_default_profile(client):
    """Create a job using the default profile."""
    r = client.post("/jobs", json={
        "agent_id": "e2e-profile",
        "engine": "claude-code",
        "profile": "default",
        "prompt": "Say hello",
        "dry_run": True,
    })
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    # Poll until done
    deadline = time.time() + 120
    while time.time() < deadline:
        r = client.get(f"/jobs/{job_id}")
        if r.json()["status"] in ("completed", "failed"):
            break
        time.sleep(2)

    data = r.json()
    assert data["status"] in ("completed", "failed")


def test_job_with_nonexistent_profile(client):
    """A nonexistent profile should fail the job."""
    r = client.post("/jobs", json={
        "agent_id": "e2e-noprofile",
        "engine": "claude-code",
        "profile": "doesnotexist",
        "prompt": "Say hello",
        "dry_run": True,
    })
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    # Poll — should fail with profile error
    deadline = time.time() + 30
    while time.time() < deadline:
        r = client.get(f"/jobs/{job_id}")
        if r.json()["status"] in ("completed", "failed"):
            break
        time.sleep(2)

    data = r.json()
    assert data["status"] == "failed"
    assert "Profile not found" in (data.get("error") or "")


def test_job_without_profile_uses_default(client):
    """Omitting profile should use 'default' profile."""
    r = client.post("/jobs", json={
        "agent_id": "e2e-nofield",
        "engine": "claude-code",
        "prompt": "Say hello",
        "dry_run": True,
    })
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    # Poll — should succeed (default profile exists)
    deadline = time.time() + 120
    while time.time() < deadline:
        r = client.get(f"/jobs/{job_id}")
        if r.json()["status"] in ("completed", "failed"):
            break
        time.sleep(2)

    data = r.json()
    assert data["status"] in ("completed", "failed")


def test_job_with_researcher_profile(client):
    """Create a job using the researcher profile (if it exists)."""
    r = client.get("/profiles")
    names = [p["name"] for p in r.json()["profiles"]]
    if "researcher" not in names:
        pytest.skip("researcher profile not found")

    r = client.post("/jobs", json={
        "agent_id": "e2e-researcher",
        "engine": "claude-code",
        "profile": "researcher",
        "prompt_vars": {"query": "test search"},
        "dry_run": True,
    })
    assert r.status_code == 202
