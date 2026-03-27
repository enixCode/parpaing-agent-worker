"""Profile loading, template rendering, and config resolution."""

import io
import logging
import tomllib
from dataclasses import dataclass

import jinja2

from .config import DEFAULT_MODEL, WORKER_TIMEOUT_SECONDS, WORKER_MEM_LIMIT, WORKER_CPU_LIMIT
from .engines import EngineConfig, load_engine, is_engine_available
from .models import AgentRunRequest

logger = logging.getLogger("tower.profiles")

# --- Variable validation ---

_TYPE_MAP = {"string": str, "integer": int, "float": (int, float), "boolean": bool}


def _validate_vars(definitions: dict, provided: dict, section: str) -> dict:
    """Validate provided vars against typed definitions, apply defaults."""
    merged = {}
    for key, spec in definitions.items():
        if not isinstance(spec, dict) or "type" not in spec:
            # Legacy format: plain value = default
            merged[key] = provided.get(key, spec)
            continue
        vtype = spec["type"]
        default = spec.get("default")
        required = spec.get("required", False)
        enum = spec.get("enum")

        if key in provided:
            val = provided[key]
        elif default is not None:
            val = default
        elif required:
            raise ValueError(f"{section}.{key}: required variable missing")
        else:
            merged[key] = None
            continue

        expected = _TYPE_MAP.get(vtype)
        if expected and not isinstance(val, expected):
            raise ValueError(f"{section}.{key}: expected {vtype}, got {type(val).__name__}")
        if enum and val not in enum:
            raise ValueError(f"{section}.{key}: must be one of {enum}, got '{val}'")
        merged[key] = val

    # Pass through extra vars not in definitions (flexible)
    for key, val in provided.items():
        if key not in merged:
            merged[key] = val
    return merged


# --- Template engine ---

def _render_template(template_path: str, variables: dict) -> str:
    """Render a template from ConfigStore by path-like name."""
    from .store.configs import ConfigStore
    store = ConfigStore.instance()
    content = store.get(template_path, "template") if store else None
    if content is None:
        raise ValueError(f"Template not found: {template_path}")
    tmpl = jinja2.Template(content, undefined=jinja2.Undefined)
    return tmpl.render(**variables)


# --- Profile loading ---

# Parsed TOML cache (cleared on config mutations)
_parsed_profiles: dict[str, dict | None] = {}


def _load_profile(name: str) -> dict | None:
    """Load profile from ConfigStore (cached after first parse)."""
    if name in _parsed_profiles:
        return _parsed_profiles[name]

    from .store.configs import ConfigStore
    store = ConfigStore.instance()
    content = store.get(name, "profile") if store else None
    if content is None:
        _parsed_profiles[name] = None
        return None

    data = tomllib.load(io.BytesIO(content.encode("utf-8")))
    _parsed_profiles[name] = data
    return data


def invalidate_caches():
    """Clear parsed profile cache (call after config mutations)."""
    _parsed_profiles.clear()


# --- JobConfig ---

@dataclass(frozen=True)
class JobConfig:
    """Immutable resolved config - everything needed to write job files."""
    engine: EngineConfig
    prompt: str
    model: str
    allowed_tools: list[str]
    max_turns: int | None
    max_budget_usd: float | None
    output_format: str
    system_prompt: str | None
    mcp_config: dict | None
    claude_md: str | None
    plugins: list[str]
    hook_pre: str | None
    hook_post: str | None
    timeout: int | None


def resolve_config(req: AgentRunRequest) -> JobConfig:
    """Load profile, merge with request, render templates. No mutation of req."""
    # Load engine
    engine_id = req.engine
    engine = load_engine(engine_id)
    if engine is None:
        raise ValueError(f"Engine not found: {engine_id}")
    if not is_engine_available(engine):
        raise ValueError(f"Engine '{engine_id}' not available - set one of: {', '.join(engine.env_auth)}")

    profile = _load_profile(req.profile)
    if profile is None:
        raise ValueError(f"Profile not found: {req.profile}")

    agent = profile.get("agent", {})
    tools = profile.get("tools", {})
    prompt_cfg = profile.get("prompt", {})
    plugins_cfg = profile.get("plugins", {})
    claude_md_cfg = profile.get("claude_md", {})
    hooks = profile.get("hooks", {})
    resources = profile.get("resources", {})

    # Prompt: request wins, else profile template
    prompt = req.prompt
    if not prompt and prompt_cfg.get("template"):
        definitions = prompt_cfg.get("variables", {})
        merged_vars = _validate_vars(definitions, req.prompt_vars, "prompt_vars")
        prompt = _render_template(prompt_cfg["template"], merged_vars)
    if not prompt:
        raise ValueError(f"No prompt: provide prompt in request or template in profile '{req.profile}'")

    # Claude_md: always from profile template
    claude_md = None
    if claude_md_cfg.get("template"):
        definitions = claude_md_cfg.get("variables", {})
        merged_vars = _validate_vars(definitions, req.claude_md_vars, "claude_md_vars")
        # Inject real resource values so the template shows accurate limits
        merged_vars.setdefault("timeout", str(resources.get("timeout") or WORKER_TIMEOUT_SECONDS))
        merged_vars.setdefault("mem_limit", WORKER_MEM_LIMIT)
        merged_vars.setdefault("cpu_limit", str(WORKER_CPU_LIMIT))
        claude_md = _render_template(claude_md_cfg["template"], merged_vars)

    return JobConfig(
        engine=engine,
        prompt=prompt,
        model=req.model or agent.get("model") or DEFAULT_MODEL,
        allowed_tools=req.allowed_tools or tools.get("allowed", []),
        max_turns=req.max_turns if req.max_turns is not None else agent.get("max_turns"),
        max_budget_usd=req.max_budget_usd if req.max_budget_usd is not None else agent.get("max_budget_usd"),
        output_format=req.output_format or agent.get("output_format") or "json",
        system_prompt=req.system_prompt,
        mcp_config=req.mcp_config,
        claude_md=claude_md,
        plugins=req.plugins or plugins_cfg.get("enabled", []),
        hook_pre=hooks.get("pre"),
        hook_post=hooks.get("post"),
        timeout=resources.get("timeout"),
    )


# --- Public: list profiles ---

def list_profiles() -> list[dict]:
    """List all available profiles (public info only, no template paths)."""
    from .store.configs import ConfigStore
    store = ConfigStore.instance()
    if not store:
        return []

    profiles = []
    for item in store.list_by_type("profile"):
        data = _load_profile(item["name"])
        if data:
            agent = data.get("agent", {})
            raw_vars = data.get("prompt", {}).get("variables", {})
            # Normalize: typed dicts stay as-is, plain values -> {type, default}
            prompt_vars = {}
            for k, v in raw_vars.items():
                if isinstance(v, dict) and "type" in v:
                    prompt_vars[k] = v
                else:
                    prompt_vars[k] = {"type": type(v).__name__, "default": v}
            profiles.append({
                "name": item["name"],
                "description": agent.get("description", ""),
                "model": agent.get("model"),
                "prompt_vars": prompt_vars,
            })
    return profiles
