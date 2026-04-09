"""Unit tests for Pydantic models - validation logic."""

import pytest
from tower.models import JobCreateRequest, AgentRunRequest, is_internal_host


class TestAgentIdValidation:
    def test_valid_ids(self):
        for aid in ["test", "my-agent-01", "audit_v2", "A" * 64]:
            r = AgentRunRequest(agent_id=aid, engine="claude-code")
            assert r.agent_id == aid

    def test_invalid_ids(self):
        for aid in ["", "a b", "test!", "../etc", "A" * 65]:
            with pytest.raises(ValueError):
                AgentRunRequest(agent_id=aid, engine="claude-code")


class TestEngineValidation:
    def test_valid(self):
        r = AgentRunRequest(agent_id="t", engine="claude-code")
        assert r.engine == "claude-code"

    def test_invalid(self):
        with pytest.raises(ValueError):
            AgentRunRequest(agent_id="t", engine="../bad")


class TestProfileValidation:
    def test_default(self):
        r = AgentRunRequest(agent_id="t", engine="claude-code")
        assert r.profile == "default"

    def test_custom(self):
        r = AgentRunRequest(agent_id="t", engine="claude-code", profile="researcher")
        assert r.profile == "researcher"

    def test_invalid(self):
        with pytest.raises(ValueError):
            AgentRunRequest(agent_id="t", engine="claude-code", profile="../../etc")


class TestPromptValidation:
    def test_none(self):
        r = AgentRunRequest(agent_id="t", engine="claude-code")
        assert r.prompt is None

    def test_valid(self):
        r = AgentRunRequest(agent_id="t", engine="claude-code", prompt="hello")
        assert r.prompt == "hello"

    def test_large_prompt_accepted(self):
        """Prompt limit is 100M - 100K chars should be accepted."""
        r = AgentRunRequest(agent_id="t", engine="claude-code", prompt="x" * 100_001)
        assert len(r.prompt) == 100_001


class TestModelValidation:
    def test_valid(self):
        r = AgentRunRequest(agent_id="t", engine="claude-code", model="claude-sonnet-4-6")
        assert r.model == "claude-sonnet-4-6"

    def test_invalid(self):
        with pytest.raises(ValueError):
            AgentRunRequest(agent_id="t", engine="claude-code", model="bad model!")


class TestMaxTurns:
    def test_valid(self):
        r = AgentRunRequest(agent_id="t", engine="claude-code", max_turns=10)
        assert r.max_turns == 10

    def test_too_low(self):
        with pytest.raises(ValueError):
            AgentRunRequest(agent_id="t", engine="claude-code", max_turns=0)

    def test_too_high(self):
        with pytest.raises(ValueError):
            AgentRunRequest(agent_id="t", engine="claude-code", max_turns=101)


class TestOutputFormat:
    def test_valid(self):
        for fmt in ["json", "text", "stream-json"]:
            r = AgentRunRequest(agent_id="t", engine="claude-code", output_format=fmt)
            assert r.output_format == fmt

    def test_invalid(self):
        with pytest.raises(ValueError):
            AgentRunRequest(agent_id="t", engine="claude-code", output_format="xml")


class TestWebhookUrl:
    def test_valid(self):
        r = JobCreateRequest(agent_id="t", engine="claude-code", webhook_url="https://example.com/hook")
        assert r.webhook_url == "https://example.com/hook"

    def test_internal_blocked(self):
        with pytest.raises(ValueError):
            JobCreateRequest(agent_id="t", engine="claude-code", webhook_url="http://localhost:8080/hook")

    def test_invalid_scheme(self):
        with pytest.raises(ValueError):
            JobCreateRequest(agent_id="t", engine="claude-code", webhook_url="ftp://example.com")


class TestDryRun:
    def test_default_false(self):
        r = AgentRunRequest(agent_id="t", engine="claude-code")
        assert r.dry_run is False

    def test_set_true(self):
        r = AgentRunRequest(agent_id="t", engine="claude-code", dry_run=True)
        assert r.dry_run is True


class TestIsInternalHost:
    def test_localhost(self):
        assert is_internal_host("localhost") is True

    def test_db(self):
        assert is_internal_host("db") is True

    def test_loopback(self):
        assert is_internal_host("127.0.0.1") is True

    def test_private(self):
        assert is_internal_host("192.168.1.1") is True

    def test_public(self):
        assert is_internal_host("8.8.8.8") is False
