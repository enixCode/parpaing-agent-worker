"""E2E: Authentication - missing or wrong API key should be rejected."""

import os
import httpx
import pytest

from conftest import DEFAULT_ENGINE

BASE_URL = os.environ.get("TOWER_URL", "http://localhost:8420")
API_KEY = os.environ.get("TOWER_API_KEY", "")


class TestAuth:

    def test_auth_missing_key(self):
        """POST /jobs without Authorization header should return 401 when API key is configured."""
        if not API_KEY:
            pytest.skip("TOWER_API_KEY not set - auth middleware inactive")

        with httpx.Client(base_url=BASE_URL, timeout=10) as c:
            r = c.post("/jobs", json={
                "agent_id": "e2e-auth",
                "engine": DEFAULT_ENGINE,
                "prompt": "hello",
                "dry_run": True,
            })
        assert r.status_code == 401

    def test_auth_wrong_key(self):
        """POST /jobs with wrong Bearer token should return 401 when API key is configured."""
        if not API_KEY:
            pytest.skip("TOWER_API_KEY not set - auth middleware inactive")

        with httpx.Client(
            base_url=BASE_URL,
            headers={"Authorization": "Bearer totally-wrong-key", "Content-Type": "application/json"},
            timeout=10,
        ) as c:
            r = c.post("/jobs", json={
                "agent_id": "e2e-auth",
                "engine": DEFAULT_ENGINE,
                "prompt": "hello",
                "dry_run": True,
            })
        assert r.status_code == 401
