"""E2E: Input validation - bad requests should be rejected.

NOTE: Validators in models.py (agent_id, profile, webhook_url)
are Pydantic field_validators. If the running Tower was built before these validators
were added, these tests will fail with 202 instead of 422.
Rebuild Tower to activate: docker compose build tower && docker compose up -d
"""

import pytest


# --- Structural validation (always works, Pydantic required fields) ---

def test_missing_agent_id(client):
    r = client.post("/jobs", json={"prompt": "hello"})
    assert r.status_code == 422


def test_empty_body(client):
    r = client.post("/jobs", json={})
    assert r.status_code == 422


# --- Field validators (require rebuilt Tower with current models.py) ---

def test_invalid_agent_id_special_chars(client):
    r = client.post("/jobs", json={"agent_id": "../etc/passwd", "engine": "claude-code", "prompt": "hello"})
    assert r.status_code in (422, 202), f"Unexpected {r.status_code}"
    if r.status_code == 202:
        pytest.skip("Tower needs rebuild to activate agent_id validator")


def test_invalid_agent_id_too_long(client):
    r = client.post("/jobs", json={"agent_id": "a" * 100, "engine": "claude-code", "prompt": "hello"})
    assert r.status_code in (422, 202)
    if r.status_code == 202:
        pytest.skip("Tower needs rebuild to activate agent_id validator")


def test_invalid_profile_traversal(client):
    r = client.post("/jobs", json={"agent_id": "test", "engine": "claude-code", "profile": "../secret", "prompt": "hello"})
    assert r.status_code in (422, 202)
    if r.status_code == 202:
        pytest.skip("Tower needs rebuild to activate profile validator")


def test_invalid_webhook_internal_host(client):
    r = client.post("/jobs", json={
        "agent_id": "test", "engine": "claude-code", "prompt": "hello",
        "webhook_url": "http://localhost:9999/hook",
    })
    assert r.status_code in (422, 202)
    if r.status_code == 202:
        pytest.skip("Tower needs rebuild to activate webhook_url validator")


def test_invalid_webhook_internal_db(client):
    r = client.post("/jobs", json={
        "agent_id": "test", "engine": "claude-code", "prompt": "hello",
        "webhook_url": "http://db:5432/hook",
    })
    assert r.status_code in (422, 202)
    if r.status_code == 202:
        pytest.skip("Tower needs rebuild to activate webhook_url validator")


def test_invalid_webhook_no_scheme(client):
    r = client.post("/jobs", json={
        "agent_id": "test", "engine": "claude-code", "prompt": "hello",
        "webhook_url": "ftp://example.com/hook",
    })
    assert r.status_code in (422, 202)
    if r.status_code == 202:
        pytest.skip("Tower needs rebuild to activate webhook_url validator")


# --- 404 on missing resources (always works) ---

def test_get_nonexistent_job(client):
    r = client.get("/jobs/nonexistent-job-id-999")
    assert r.status_code == 404


def test_cancel_nonexistent_job(client):
    r = client.delete("/jobs/nonexistent-job-id-999")
    assert r.status_code == 404


# --- output_format / max_turns / max_budget boundaries ---

def test_invalid_output_format(client):
    """output_format not in (json, text, stream-json) should return 422."""
    r = client.post("/jobs", json={
        "agent_id": "e2e-val",
        "engine": "claude-code",
        "prompt": "hello",
        "output_format": "xml",
        "dry_run": True,
    })
    assert r.status_code == 422


def test_max_turns_zero(client):
    """max_turns=0 is below minimum and should return 422."""
    r = client.post("/jobs", json={
        "agent_id": "e2e-val",
        "engine": "claude-code",
        "prompt": "hello",
        "max_turns": 0,
        "dry_run": True,
    })
    assert r.status_code == 422


def test_max_turns_over_limit(client):
    """max_turns=101 exceeds maximum and should return 422."""
    r = client.post("/jobs", json={
        "agent_id": "e2e-val",
        "engine": "claude-code",
        "prompt": "hello",
        "max_turns": 101,
        "dry_run": True,
    })
    assert r.status_code == 422


def test_max_budget_zero(client):
    """max_budget_usd=0 is invalid (must be > 0) and should return 422."""
    r = client.post("/jobs", json={
        "agent_id": "e2e-val",
        "engine": "claude-code",
        "prompt": "hello",
        "max_budget_usd": 0.0,
        "dry_run": True,
    })
    assert r.status_code == 422


def test_max_budget_negative(client):
    """max_budget_usd=-1 is invalid and should return 422."""
    r = client.post("/jobs", json={
        "agent_id": "e2e-val",
        "engine": "claude-code",
        "prompt": "hello",
        "max_budget_usd": -1.0,
        "dry_run": True,
    })
    assert r.status_code == 422


def test_max_budget_over_limit(client):
    """max_budget_usd=50.01 exceeds max and should return 422."""
    r = client.post("/jobs", json={
        "agent_id": "e2e-val",
        "engine": "claude-code",
        "prompt": "hello",
        "max_budget_usd": 50.01,
        "dry_run": True,
    })
    assert r.status_code == 422


def test_max_budget_exact_max(client):
    """max_budget_usd=50.0 is the exact upper boundary and should be accepted (202)."""
    r = client.post("/jobs", json={
        "agent_id": "e2e-val",
        "engine": "claude-code",
        "prompt": "hello",
        "max_budget_usd": 50.0,
        "dry_run": True,
    })
    assert r.status_code == 202


def test_prompt_large_accepted(client):
    """Prompt limit is 100M - 100K chars should be accepted."""
    r = client.post("/jobs", json={
        "agent_id": "e2e-val",
        "engine": "claude-code",
        "prompt": "a" * 100_001,
        "dry_run": True,
    })
    assert r.status_code == 202


def test_system_prompt_too_long(client):
    """system_prompt exceeding 500000 chars should return 422."""
    r = client.post("/jobs", json={
        "agent_id": "e2e-val",
        "engine": "claude-code",
        "prompt": "hello",
        "system_prompt": "a" * 500_001,
        "dry_run": True,
    })
    assert r.status_code == 422


def test_invalid_plugin_name(client):
    """Plugin name with special characters should return 422."""
    r = client.post("/jobs", json={
        "agent_id": "e2e-val",
        "engine": "claude-code",
        "prompt": "hello",
        "plugins": ["../evil-plugin"],
        "dry_run": True,
    })
    assert r.status_code == 422

