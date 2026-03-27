"""Engine loading and availability checking."""

import io
import logging
import tomllib
from dataclasses import dataclass

logger = logging.getLogger("tower.engines")


@dataclass(frozen=True)
class EngineConfig:
    """Resolved engine - everything needed to build a CLI command."""
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


# Parsed engine cache (cleared on config mutations)
_parsed_engines: dict[str, EngineConfig | None] = {}


def _parse_engine(engine_id: str, content: str) -> EngineConfig:
    """Parse TOML content into an EngineConfig."""
    data = tomllib.load(io.BytesIO(content.encode("utf-8")))
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


def load_engine(engine_id: str) -> EngineConfig | None:
    """Load engine config from ConfigStore (cached after first parse)."""
    if engine_id in _parsed_engines:
        return _parsed_engines[engine_id]

    from .store.configs import ConfigStore
    store = ConfigStore.instance()
    content = store.get(engine_id, "engine") if store else None
    if content is None:
        _parsed_engines[engine_id] = None
        return None

    cfg = _parse_engine(engine_id, content)
    _parsed_engines[engine_id] = cfg
    return cfg


def invalidate_caches():
    """Clear parsed engine cache (call after config mutations)."""
    _parsed_engines.clear()


def is_engine_available(engine: EngineConfig) -> bool:
    """Always available - gateway handles auth."""
    return True


def list_engines() -> list[dict]:
    """List all available engines with availability status."""
    from .store.configs import ConfigStore
    store = ConfigStore.instance()
    if not store:
        return []

    engines = []
    for item in store.list_by_type("engine"):
        cfg = load_engine(item["name"])
        if cfg:
            engines.append({
                "id": cfg.id,
                "name": cfg.name,
                "description": cfg.description,
                "available": is_engine_available(cfg),
            })
    return engines
