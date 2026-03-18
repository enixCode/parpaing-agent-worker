"""Unit tests for engine config."""

from unittest.mock import patch

import pytest
from tower.engines import EngineConfig, is_engine_available


@pytest.fixture
def claude_engine():
    return EngineConfig(
        id="claude-code", name="Claude Code", description="test",
        binary="claude",
        prompt_flag="-p", static_args=["--verbose"],
        flag_map={"model": "--model", "max_turns": "--max-turns", "allowed_tools": "--allowedTools"},
        list_join={"allowed_tools": ","},
        output_mode="stdout", output_format="json", output_path=None,
        env_auth=["ANTHROPIC_API_KEY"],
    )


class TestEngineConfig:
    def test_frozen(self, claude_engine):
        with pytest.raises(AttributeError):
            claude_engine.id = "other"


class TestEngineAvailabilityGateway:
    """Engine availability when GATEWAY_URL is set."""

    def test_gateway_mode_always_available(self, claude_engine):
        with patch("tower.engines.GATEWAY_URL", "http://gateway:4000", create=True), \
             patch("tower.config.GATEWAY_URL", "http://gateway:4000"):
            assert is_engine_available(claude_engine) is True

    def test_gateway_mode_no_env_keys_needed(self):
        engine = EngineConfig(
            id="test", name="Test", description="",
            binary="test", prompt_flag="-p", static_args=[],
            flag_map={}, list_join={},
            output_mode="stdout", output_format="json", output_path=None,
            env_auth=["MISSING_KEY_1", "MISSING_KEY_2"],
        )
        with patch("tower.engines.GATEWAY_URL", "http://gateway:4000", create=True), \
             patch("tower.config.GATEWAY_URL", "http://gateway:4000"):
            assert is_engine_available(engine) is True

    def test_no_gateway_requires_keys(self, claude_engine):
        with patch("tower.engines.GATEWAY_URL", "", create=True), \
             patch("tower.config.GATEWAY_URL", ""), \
             patch.dict("os.environ", {}, clear=True):
            assert is_engine_available(claude_engine) is False
