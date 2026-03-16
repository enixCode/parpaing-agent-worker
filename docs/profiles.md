# Profiles Guide


Profiles are TOML files in `profiles/` that define reusable agent configurations. The **filename** (without `.toml`) is the profile name used in requests. The `[agent].id` field is informational only.

## TOML Structure

```toml
[agent]
id = "my-profile"                      # informational only (filename = profile name)
description = "What this agent does"   # shown in GET /profiles
model = "claude-sonnet-4-6"            # default model
# max_turns = 20                       # omit for unlimited
# max_budget_usd = 5.0                 # omit for unlimited
output_format = "json"                 # json | text | stream-json

[tools]
allowed = ["Read", "Grep", "Glob", "Bash", "Write", "Edit"]

[prompt]
template = "prompts/my-template.md.j2"   # Jinja2 template path (relative to templates/)

[prompt.variables.task]
type = "string"                          # string | integer | float | boolean
default = "Analyse le projet"            # default value
required = false                         # optional (default: false)
description = "Task to perform"          # human-readable description (optional)

[claude_md]
template = "claude-md/agent-base.md.j2"  # Jinja2 template for CLAUDE.md

[claude_md.variables]
role = "Agent role description"
guidelines = "Behavior guidelines"
constraints = "Hard limits"
output_instructions = "Expected output format"

[resources]
timeout = 3600         # container timeout in seconds

[plugins]
enabled = ["plugin-name"]   # Claude Code plugins to activate

[hooks]
pre = "setup.sh"       # filename from hooks/ dir, or multiline string = inline script
post = "collect.sh"    # filename from hooks/ dir, or multiline string = inline script
```

## Sections

### [agent]

| Key | Default | Description |
|---|---|---|
| `id` | — | Informational identifier (not used for lookup — filename is the profile name) |
| `description` | `""` | Short description, returned by `GET /profiles` |
| `model` | `claude-sonnet-4-6` | Model used by the worker |
| `max_turns` | unlimited | Max conversation turns |
| `max_budget_usd` | unlimited | Max spend per job |
| `output_format` | `json` | Output format: `json`, `text`, or `stream-json` |

### [tools]

| Key | Default | Description |
|---|---|---|
| `allowed` | `[]` (all) | List of Claude Code tools. Empty = all tools allowed |

Tool examples: `Read`, `Grep`, `Glob`, `Bash`, `Edit`, `Write`, `WebSearch`, `WebFetch`, `Bash(git log *)`, `Bash(npm test)`

### [prompt]

| Key | Description |
|---|---|
| `template` | Path to Jinja2 template relative to `templates/` |
| `variables` | Variable definitions injected into the template (defaults) |

Variables use **typed definitions** (`[prompt.variables.<name>]` sub-tables). All included profiles use this format:

| Field | Required | Description |
|---|---|---|
| `type` | yes | Value type: `string`, `integer`, `float`, or `boolean` |
| `default` | no | Default value if not provided in the request |
| `required` | no | If `true`, the request must provide this variable (default: `false`) |
| `enum` | no | List of allowed values (validation fails if value not in list) |
| `description` | no | Human-readable description of the variable |

```toml
[prompt.variables.repo_url]
type = "string"
default = "https://github.com/org/repo"
required = true
description = "Git repository URL to clone and review"
```

Legacy flat format (`[prompt.variables]` with `key = "value"` pairs) is still supported but not recommended.

Request `prompt_vars` are merged on top of profile variables (request wins).

### [claude_md]

| Key | Description |
|---|---|
| `template` | Path to Jinja2 template relative to `templates/` |
| `variables` | Key-value pairs injected into the template (defaults) |

All profiles use `claude-md/agent-base.md.j2`. Common variables: `role`, `guidelines`, `constraints`, `output_instructions`.

Request `claude_md_vars` are merged on top of profile variables (request wins).

### [resources]

| Key | Default | Description |
|---|---|---|
| `timeout` | `WORKER_TIMEOUT_SECONDS` env | Container timeout in seconds (used by job_runner) |

Resources are **profile-only** — they cannot be overridden per-request.

### [plugins]

| Key | Description |
|---|---|
| `enabled` | List of plugin names to pre-install in the worker |

### [hooks]

| Key | Description |
|---|---|
| `pre` | Hook to run in the worker before the engine starts |
| `post` | Hook to run in the worker after the engine finishes |

Hooks can be defined in two ways:

- **Filename** (single line): references a script from the `hooks/` directory (e.g. `pre = "setup.sh"`)
- **Inline script** (multiline string): the script body is written directly in the TOML file

```toml
# Filename reference
[hooks]
pre = "setup.sh"

# Inline script
[hooks]
pre = """#!/bin/bash
echo "Setting up workspace..."
cd /workspace && git clone "$REPO_URL" .
"""
```

Hooks run inside the worker container with access to the workspace and output directories. See `hooks/*.example.sh` for templates.

## Request-Only Fields

These fields exist only in the API request and cannot be set in profiles:

| Field | Description |
|---|---|
| `system_prompt` | Override the system prompt entirely (passed directly to Claude Code CLI) |
| `mcp_config` | MCP server configuration dict (written as `mcp.json` in the job config directory) |

## Included Profiles

### default.toml

General-purpose agent that adapts to your prompt.

| Property | Value |
|---|---|
| Model | `claude-sonnet-4-6` |
| Tools | `Read`, `Grep`, `Glob`, `Bash`, `Write`, `Edit` |
| Budget | unlimited |
| Timeout | default (env) |

**Prompt variables**: `task` (default: `"Analyse le projet"`)

### code-review.toml

Code review — clones a repo, analyzes code quality and tests.

| Property | Value |
|---|---|
| Model | `claude-opus-4-6` |
| Tools | `Read`, `Grep`, `Glob`, `Bash` |
| Budget | $10.00 |
| Timeout | 1800s (30 min) |

**Prompt variables**: `repo_url` (required, default: `"https://github.com/org/repo"`), `focus` (default: `"security, performance"`), `branch` (default: `"main"`)

### researcher.toml

Deep research — explores options, compares, recommends with sources.

| Property | Value |
|---|---|
| Model | `claude-opus-4-6` |
| Tools | `Read`, `Grep`, `Glob`, `Bash`, `WebSearch`, `WebFetch` |
| Budget | $15.00 |
| Timeout | 3600s (60 min) |

**Prompt variables**: `query` (default: `"Trouver le meilleur framework pour..."`), `criteria` (default: `"Open source, actif, bonne doc"`), `context` (default: `"Projet web moderne"`)

## Priority Order

```
Request fields  >  Profile defaults  >  System defaults
```

Example: if the request sends `model = "claude-sonnet-4-6"` and the profile has `model = "claude-opus-4-6"`, the request wins. Resources and hooks are profile-only and cannot be overridden per-request.
