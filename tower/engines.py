"""Engine loading and availability checking."""

import functools
import logging
import os
import tomllib
from dataclasses import dataclass

from .config import ENGINES_DIR

logger = logging.getLogger("tower.engines")


@dataclass(frozen=True)
class EngineConfig:
    """Resolved engine — everything needed to build a CLI command."""
    id: str
    name: str
    description: str
    binary: str
    prompt_flag: str
    static_args: list[str]
    flag_map: dict[str, str]
    list_join: dict[str, str]
    output_mode: str
    output_format: str
    output_path: str | None
    env_auth: list[str]          # at least ONE must be set (API keys)


@functools.lru_cache(maxsize=32)
def load_engine(engine_id: str) -> EngineConfig | None:
    """Load engine config from TOML file."""
    path = (ENGINES_DIR / f"{engine_id}.toml").resolve()
    if not path.is_relative_to(ENGINES_DIR.resolve()):
        logger.warning("Engine path traversal blocked: %s", engine_id)
        return None
    if not path.exists():
        return None
    with open(path, "rb") as f:
        data = tomllib.load(f)

    engine = data.get("engine", {})
    command = data.get("command", {})
    output = data.get("output", {})
    env = data.get("env", {})

    return EngineConfig(
        id=engine.get("id", engine_id),
        name=engine.get("name", engine_id),
        description=engine.get("description", ""),
        binary=command.get("binary", engine_id),
        prompt_flag=command.get("prompt_flag", "-p"),
        static_args=command.get("static_args", []),
        flag_map=command.get("map", {}),
        list_join=command.get("list_join", {}),
        output_mode=output.get("mode", "stdout"),
        output_format=output.get("format", "json"),
        output_path=output.get("path"),
        env_auth=env.get("auth", env.get("required", [])),
    )


def is_engine_available(engine: EngineConfig) -> bool:
    """Check if at least one auth env var is set."""
    if not engine.env_auth:
        return True
    return any(os.environ.get(k) for k in engine.env_auth)


def list_engines() -> list[dict]:
    """List all available engines with availability status."""
    engines = []
    if ENGINES_DIR.exists():
        for f in ENGINES_DIR.glob("*.toml"):
            cfg = load_engine(f.stem)
            if cfg:
                engines.append({
                    "id": cfg.id,
                    "name": cfg.name,
                    "description": cfg.description,
                    "available": is_engine_available(cfg),
                    "env_auth": cfg.env_auth,
                })
    return engines
