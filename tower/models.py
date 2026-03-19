"""Pydantic models for Tower API."""

import re
import socket
from datetime import datetime
from ipaddress import ip_address
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

# Safe identifier: alphanumeric, dash, underscore only
_SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


_MAX_PROMPT_LEN = 100_000
_MAX_SYSTEM_PROMPT_LEN = 50_000

# PostgreSQL JSONB rejects null bytes and bare surrogates
_PG_UNSAFE = re.compile(r"[\x00\ud800-\udfff]")

def _strip_pg_unsafe(s: str) -> str:
    """Remove null bytes and bare surrogates that PostgreSQL JSONB rejects."""
    return _PG_UNSAFE.sub("", s)


class AgentRunRequest(BaseModel):
    """Run a Claude Code agent in an isolated container."""

    agent_id: str = Field(description="Job identifier prefix (e.g. 'audit-01'). Used in job_id: {agent_id}-{hex}.")
    prompt: str | None = Field(None, description="Direct prompt. If set, overrides the profile's prompt template.")
    engine: str = Field(description="**Required.** Engine to use. Use GET /engines to list available engines.")
    profile: str = Field("default", description="Profile name. Use GET /profiles to list available profiles and their variables.")
    prompt_vars: dict = Field(default_factory=dict, description="Variables injected into the profile's prompt template (Jinja2).")
    claude_md_vars: dict = Field(default_factory=dict, description="Variables injected into the profile's CLAUDE.md template.")
    plugins: list[str] = Field(default_factory=list, description="Claude Code plugins to activate.")
    allowed_tools: list[str] = Field(default_factory=list, description="Tools the agent can use (e.g. Read, Grep, Bash). Empty = all tools allowed.")
    max_turns: int | None = Field(None, description="Max conversation turns (1-100).")
    max_budget_usd: float | None = Field(None, description="Max spend in USD for this job (0-50).")
    mcp_config: dict | None = Field(None, description="MCP server configuration (passed to Claude Code CLI).")
    model: str | None = Field(None, description="Claude model override (e.g. claude-sonnet-4-6, claude-opus-4-6).")
    system_prompt: str | None = Field(None, description="Override the system prompt entirely.")
    output_format: str | None = Field(None, description="Output format: json, text, or stream-json.")
    dry_run: bool = Field(False, description="Test mode - logs the command without running Claude.")

    @field_validator("agent_id")
    @classmethod
    def validate_agent_id(cls, v: str) -> str:
        if not _SAFE_ID.match(v):
            raise ValueError("agent_id must be 1-64 alphanumeric/dash/underscore chars")
        return v

    @field_validator("engine")
    @classmethod
    def validate_engine(cls, v: str) -> str:
        if not _SAFE_ID.match(v):
            raise ValueError("engine must be 1-64 alphanumeric/dash/underscore chars")
        return v

    @field_validator("profile")
    @classmethod
    def validate_profile(cls, v: str) -> str:
        if not _SAFE_ID.match(v):
            raise ValueError("profile must be 1-64 alphanumeric/dash/underscore chars")
        return v

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, v: str | None) -> str | None:
        if v is not None and len(v) > _MAX_PROMPT_LEN:
            raise ValueError(f"prompt exceeds {_MAX_PROMPT_LEN} characters")
        if v is not None:
            v = _strip_pg_unsafe(v)
        return v

    @field_validator("system_prompt")
    @classmethod
    def validate_system_prompt(cls, v: str | None) -> str | None:
        if v is not None and len(v) > _MAX_SYSTEM_PROMPT_LEN:
            raise ValueError(f"system_prompt exceeds {_MAX_SYSTEM_PROMPT_LEN} characters")
        if v is not None:
            v = _strip_pg_unsafe(v)
        return v

    @field_validator("plugins")
    @classmethod
    def validate_plugins(cls, v: list[str]) -> list[str]:
        for p in v:
            if not _SAFE_ID.match(p):
                raise ValueError(f"Invalid plugin name: {p}")
        return v

    @field_validator("model")
    @classmethod
    def validate_model(cls, v: str | None) -> str | None:
        if v is not None and not re.match(r"^[a-zA-Z0-9._/-]{1,128}$", v):
            raise ValueError("Invalid model name")
        return v

    @field_validator("output_format")
    @classmethod
    def validate_output_format(cls, v: str | None) -> str | None:
        if v is not None and v not in ("json", "text", "stream-json"):
            raise ValueError("output_format must be json, text, or stream-json")
        return v

    @field_validator("max_turns")
    @classmethod
    def validate_max_turns(cls, v: int | None) -> int | None:
        if v is not None and (v < 1 or v > 100):
            raise ValueError("max_turns must be between 1 and 100")
        return v

    @field_validator("max_budget_usd")
    @classmethod
    def validate_max_budget(cls, v: float | None) -> float | None:
        if v is not None and (v <= 0 or v > 50.0):
            raise ValueError("max_budget_usd must be greater than 0 and at most 50")
        return v



def is_internal_host(hostname: str) -> bool:
    """Check if hostname resolves to a private/internal address (DNS-aware)."""
    if hostname in ("localhost", "db", "tower", "postgres"):
        return True
    # Check literal IP
    try:
        addr = ip_address(hostname)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        pass
    # Resolve DNS and check all addresses
    try:
        for info in socket.getaddrinfo(hostname, None):
            addr = ip_address(info[4][0])
            if addr.is_private or addr.is_loopback or addr.is_link_local:
                return True
    except socket.gaierror:
        pass
    return False


class JobCreateRequest(AgentRunRequest):
    """Create an async job. Returns immediately with job_id (202)."""

    webhook_url: str | None = Field(None, description="URL to POST the result when job completes. Must be https in production.")

    @field_validator("webhook_url")
    @classmethod
    def validate_webhook_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if len(v) > 2048:
            raise ValueError("webhook_url exceeds 2048 characters")
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("webhook_url must use http or https")
        if not parsed.hostname:
            raise ValueError("webhook_url must have a hostname")
        if is_internal_host(parsed.hostname):
            raise ValueError("webhook_url cannot target internal hosts")
        return v


class JobCreateResponse(BaseModel):
    job_id: str
    status: str


class JobResponse(BaseModel):
    job_id: str
    status: str
    engine: str | None = None
    profile: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    result: dict | None = None
    error: str | None = None
