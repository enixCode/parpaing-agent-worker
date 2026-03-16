"""Shared fixtures for e2e tests."""

import os
import httpx
import pytest

BASE_URL = os.environ.get("TOWER_URL", "http://localhost:8420")
API_KEY = os.environ.get("TOWER_API_KEY", "")
DEFAULT_ENGINE = os.environ.get("TEST_ENGINE", "claude-code")


@pytest.fixture(scope="session")
def headers():
    h = {"Content-Type": "application/json"}
    if API_KEY:
        h["Authorization"] = f"Bearer {API_KEY}"
    return h


@pytest.fixture(scope="class")
def client(headers):
    """Per-class client to avoid stale connection issues across test modules."""
    transport = httpx.HTTPTransport(retries=2)
    with httpx.Client(base_url=BASE_URL, headers=headers, timeout=30, transport=transport) as c:
        yield c
