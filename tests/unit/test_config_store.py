"""Unit tests for ConfigStore in-memory cache operations."""

import pytest
from datetime import datetime, timezone
from tower.store.configs import ConfigStore, VALID_TYPES


class TestConfigStoreCache:
    """Test sync read operations on the in-memory cache."""

    def _make_store(self, entries=None):
        """Create a ConfigStore with a pre-populated cache (no DB)."""
        store = ConfigStore.__new__(ConfigStore)
        store._pool = None
        store._cache = {}
        if entries:
            for name, ctype, content, desc in entries:
                now = datetime.now(timezone.utc)
                store._cache[(name, ctype)] = {
                    "name": name, "type": ctype, "content": content,
                    "description": desc, "created_at": now, "updated_at": now,
                }
        return store

    def test_get_existing(self):
        store = self._make_store([("default", "profile", "[agent]\nid = 'default'", "Default")])
        assert store.get("default", "profile") == "[agent]\nid = 'default'"

    def test_get_missing(self):
        store = self._make_store()
        assert store.get("missing", "profile") is None

    def test_get_wrong_type(self):
        store = self._make_store([("default", "profile", "content", "")])
        assert store.get("default", "engine") is None

    def test_get_full(self):
        store = self._make_store([("default", "profile", "content", "desc")])
        entry = store.get_full("default", "profile")
        assert entry is not None
        assert entry["name"] == "default"
        assert entry["type"] == "profile"
        assert entry["content"] == "content"
        assert entry["description"] == "desc"

    def test_get_full_missing(self):
        store = self._make_store()
        assert store.get_full("x", "profile") is None

    def test_list_by_type(self):
        store = self._make_store([
            ("default", "profile", "a", "Default profile"),
            ("researcher", "profile", "b", "Research profile"),
            ("claude-code", "engine", "c", "Engine"),
        ])
        profiles = store.list_by_type("profile")
        assert len(profiles) == 2
        names = [p["name"] for p in profiles]
        assert "default" in names
        assert "researcher" in names
        # Should not include content
        assert "content" not in profiles[0]

    def test_list_by_type_empty(self):
        store = self._make_store()
        assert store.list_by_type("engine") == []

    def test_list_all(self):
        store = self._make_store([
            ("default", "profile", "a", ""),
            ("claude-code", "engine", "b", ""),
            ("prompts/default.md.j2", "template", "c", ""),
        ])
        all_items = store.list_all()
        assert len(all_items) == 3

    def test_list_all_sorted(self):
        store = self._make_store([
            ("z-profile", "profile", "a", ""),
            ("a-profile", "profile", "b", ""),
            ("claude-code", "engine", "c", ""),
        ])
        all_items = store.list_all()
        # Should be sorted by (type, name)
        types = [i["type"] for i in all_items]
        assert types[0] == "engine"  # engine comes before profile alphabetically


class TestConfigStoreValidTypes:
    """Test that valid types constant is correct."""

    def test_valid_types(self):
        assert "profile" in VALID_TYPES
        assert "engine" in VALID_TYPES
        assert "template" in VALID_TYPES
        assert len(VALID_TYPES) == 3


class TestExtractTomlDescription:
    """Test TOML description extraction helper."""

    def test_extracts_description(self):
        content = '[agent]\nid = "test"\ndescription = "My test profile"'
        desc = ConfigStore._extract_toml_description(content)
        assert desc == "My test profile"

    def test_no_description(self):
        content = '[agent]\nid = "test"'
        desc = ConfigStore._extract_toml_description(content)
        assert desc == ""

    def test_single_quoted(self):
        content = "description = 'Single quoted'"
        desc = ConfigStore._extract_toml_description(content)
        assert desc == "Single quoted"


class TestConfigModels:
    """Test config-related Pydantic models."""

    def test_create_request_valid(self):
        from tower.models import ConfigCreateRequest
        req = ConfigCreateRequest(name="my-profile", content="[agent]\nid='test'", description="Test")
        assert req.name == "my-profile"
        assert req.description == "Test"

    def test_create_request_template_name(self):
        from tower.models import ConfigCreateRequest
        req = ConfigCreateRequest(name="prompts/my-template.md.j2", content="{{ var }}")
        assert req.name == "prompts/my-template.md.j2"

    def test_create_request_invalid_name(self):
        from tower.models import ConfigCreateRequest
        with pytest.raises(ValueError):
            ConfigCreateRequest(name="../../etc/passwd", content="test")

    def test_create_request_empty_content(self):
        from tower.models import ConfigCreateRequest
        with pytest.raises(ValueError):
            ConfigCreateRequest(name="test", content="   ")

    def test_update_request_valid(self):
        from tower.models import ConfigUpdateRequest
        req = ConfigUpdateRequest(content="new content")
        assert req.content == "new content"
        assert req.description is None

    def test_update_request_empty_content(self):
        from tower.models import ConfigUpdateRequest
        with pytest.raises(ValueError):
            ConfigUpdateRequest(content="")
