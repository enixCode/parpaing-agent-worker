# Request Fields & Defaults


## AgentRunRequest

Used by `POST /jobs`.

| Field | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `agent_id` | `string` | **required** | `^[a-zA-Z0-9_-]{1,64}$` | Identifier for the agent (used in job_id prefix) |
| `engine` | `string` | **required** | `^[a-zA-Z0-9_-]{1,64}$` | Engine to use (e.g. `claude-code`, `opencode`) |
| `prompt` | `string\|null` | `null` | Max 100,000 chars | Direct prompt. If null, uses profile template |
| `profile` | `string` | `"default"` | `^[a-zA-Z0-9_-]{1,64}$` | Profile name (e.g. `"researcher"` loads `profiles/researcher.toml`). Must exist |
| `prompt_vars` | `dict` | `{}` | | Variables injected into Jinja2 prompt template |
| `claude_md_vars` | `dict` | `{}` | | Variables injected into profile's CLAUDE.md template |
| `plugins` | `list[string]` | `[]` | Each item: `^[a-zA-Z0-9_-]{1,64}$` | Claude Code plugins to activate |
| `allowed_tools` | `list[string]` | `[]` | | Tools the agent can use (e.g. Read, Grep, Bash). Empty = all |
| `max_turns` | `int\|null` | `null` | 1-100 | Max conversation turns. `null` = unlimited |
| `max_budget_usd` | `float\|null` | `null` | >0, <=50 | Max spend in USD. `null` = unlimited |
| `mcp_config` | `dict\|null` | `null` | | MCP server configuration (passed to Claude Code CLI) |
| `model` | `string\|null` | `null` | `^[a-zA-Z0-9._/-]{1,128}$` | Model override (e.g. `claude-opus-4-6`, `anthropic/claude-sonnet-4`). `null` = profile default |
| `system_prompt` | `string\|null` | `null` | Max 50,000 chars | Override the system prompt entirely |
| `output_format` | `string\|null` | `null` | `json`, `text`, `stream-json` | Output format. `null` = profile default or `json` |
| `dry_run` | `bool` | `false` | | Test mode - logs the command without running Claude |

## JobCreateRequest (extends AgentRunRequest)

| Field | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `webhook_url` | `string\|null` | `null` | Max 2048 chars, `http`/`https` only, no internal hosts (SSRF protection) | URL called on job completion (POST with job result) |

## Profile-Only Settings

These settings are configured in profiles, not in the API request:

| Setting | Profile Section | Description |
|---|---|---|
| Worker timeout | `[resources] timeout` | Container timeout in seconds |
| CLAUDE.md template | `[claude_md] template` | Jinja2 template for CLAUDE.md |
| Pre-job hook | `[hooks] pre` | Script to run before claude |
| Post-job hook | `[hooks] post` | Script to run after claude |

## JobCreateResponse

Returned by `POST /jobs` (HTTP 202).

| Field | Type | Description |
|---|---|---|
| `job_id` | `string` | Unique job identifier (`{agent_id}-{12-char-hex}`) |
| `status` | `string` | Always `pending` on creation |

## Examples

Minimal request (Claude Code):

```json
{
  "agent_id": "my-agent",
  "engine": "claude-code",
  "prompt": "Do whatever you need"
}
```

With a specific profile:

```json
{
  "agent_id": "my-agent",
  "engine": "claude-code",
  "profile": "researcher"
}
```

With a different engine:

```json
{
  "agent_id": "my-agent",
  "engine": "opencode",
  "prompt": "Refactor the auth module"
}
```

With explicit limits:

```json
{
  "agent_id": "my-agent",
  "engine": "claude-code",
  "prompt": "Analyze this code",
  "max_turns": 50,
  "max_budget_usd": 5.0
}
```

## JobResponse

Returned by `GET /jobs/{id}` and `GET /jobs`.

| Field | Type | Description |
|---|---|---|
| `job_id` | `string` | Unique job identifier |
| `status` | `string` | `pending`, `running`, `completed`, `failed`, or `cancelled` |
| `engine` | `string\|null` | Engine used for this job |
| `profile` | `string\|null` | Profile used for this job |
| `created_at` | `datetime` | When the job was created |
| `started_at` | `datetime\|null` | When the job started running |
| `finished_at` | `datetime\|null` | When the job finished |
| `exit_code` | `int\|null` | Container exit code |
| `result` | `dict\|null` | Job output (engine-dependent) |
| `error` | `string\|null` | Error message if failed |
