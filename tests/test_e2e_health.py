"""E2E: Health & profiles endpoints."""


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["checks"]["db"] == "ok"
    assert data["checks"]["docker"] == "ok"
    assert "pool" in data["checks"]


def test_profiles(client):
    r = client.get("/profiles")
    assert r.status_code == 200
    data = r.json()
    assert "profiles" in data
    assert isinstance(data["profiles"], list)
    # At least default.toml should exist
    names = [p["name"] for p in data["profiles"]]
    assert "default" in names
