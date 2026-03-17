"""Unit tests for engine config."""

import pytest
from tower.engines import EngineConfig


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
