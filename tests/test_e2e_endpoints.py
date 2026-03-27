"""E2E: Miscellaneous endpoints - engines, metrics, security headers."""

import pytest

from conftest import DEFAULT_ENGINE


class TestEnginesEndpoint:

    def test_engines_endpoint(self, client):
        """GET /engines should return 200 with a list of engines."""
        r = client.get("/engines")
        assert r.status_code == 200
        data = r.json()
        assert "engines" in data
        assert isinstance(data["engines"], list)


class TestMetricsEndpoint:

    def test_metrics_endpoint(self, client):
        """GET /metrics should return 200 with text/plain Prometheus content."""
        r = client.get("/metrics")
        assert r.status_code == 200
        content_type = r.headers.get("content-type", "")
        assert "text/plain" in content_type
        # Basic Prometheus format check
        assert "tower_" in r.text


class TestSecurityHeaders:

    def test_security_headers(self, client):
        """Responses should include required security headers."""
        r = client.get("/health")
        assert r.status_code == 200
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
        assert r.headers.get("X-Frame-Options") == "DENY"
        assert r.headers.get("Cache-Control") == "no-store"
