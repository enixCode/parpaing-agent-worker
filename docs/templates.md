# Templates Guide


Templates are Jinja2 files in `templates/` used to generate prompts and CLAUDE.md for workers.

Jinja2 uses **lenient undefined mode** (`jinja2.Undefined`): any variable not provided renders as an empty string instead of raising an error.

## Directory Layout

```
templates/
├── prompts/              # Prompt templates
│   ├── default.md.j2
│   ├── code-review.md.j2
│   └── researcher.md.j2
└── claude-md/            # CLAUDE.md templates
    └── agent-base.md.j2
```

## Prompt Templates

Referenced by profiles via `[prompt] template = "prompts/name.md.j2"`.

Variables come from `[prompt.variables]` in the profile, merged with `prompt_vars` from the request (request wins).

### prompts/default.md.j2

```jinja
{{ task | default("Analyse le code dans /workspace et produis un rapport structuré.") }}
```

| Variable | Required | Default |
|----------|----------|---------|
| `task` | no | `Analyse le projet` (profile default) |

### prompts/code-review.md.j2

```jinja
Review du code dans /workspace.
{% if repo_url %}Le repo a été cloné depuis : {{ repo_url }}{% endif %}
{% if focus %}Focus : {{ focus }}{% endif %}
...
Vérifier les tests existants - lancer `{{ test_command | default("npm test || pytest || go test ./...") }}`
{% if branch %}Branche à reviewer : {{ branch }}{% endif %}
```

| Variable | Required | Default |
|----------|----------|---------|
| `repo_url` | yes | `https://github.com/org/repo` |
| `focus` | no | `security, performance` |
| `test_command` | no | `npm test \|\| pytest \|\| go test ./...` |
| `branch` | no | `main` |

### prompts/researcher.md.j2

```jinja
Recherche approfondie : {{ query | default("Analyse le sujet fourni") }}
{% if criteria %}Critères de sélection : {{ criteria }}{% endif %}
...
{% if context %}Contexte additionnel : {{ context }}{% endif %}
```

| Variable | Required | Default |
|----------|----------|---------|
| `query` | yes | `Trouver le meilleur framework pour...` |
| `criteria` | no | `Open source, actif, bonne doc` |
| `context` | no | `Projet web moderne` |

## CLAUDE.md Template

Injected into the worker's workspace as `.claude/CLAUDE.md`. Referenced via `[claude_md]` section in profiles.

### claude-md/agent-base.md.j2

```jinja
# Instructions Agent

Tu es un agent autonome dans un container Docker isolé.
- Workspace : `/workspace` (ton répertoire de travail)
- Output : `/output/result.json` (tes résultats finaux)
- Timeout : {{ timeout | default("3600") }}s | RAM : {{ mem_limit | default("512m") }} | CPU : {{ cpu_limit | default("2.0") }}

{% if role %}
## Role
{{ role }}
{% endif %}

{% if guidelines %}
## Guidelines
{{ guidelines }}
{% endif %}

{% if constraints %}
## Contraintes
{{ constraints }}
{% endif %}

{% if output_instructions %}
## Format de sortie
{{ output_instructions }}
{% endif %}
```

| Variable | Required | Default | Source |
|----------|----------|---------|-------|
| `role` | no | - | `[claude_md.variables]` in profile |
| `guidelines` | no | - | `[claude_md.variables]` in profile |
| `constraints` | no | - | `[claude_md.variables]` in profile |
| `output_instructions` | no | - | `[claude_md.variables]` in profile |
| `timeout` | no | `3600` | Auto-injected from `[resources].timeout` or `WORKER_TIMEOUT_SECONDS` |
| `mem_limit` | no | `2g` (injected) / `512m` (template fallback) | Auto-injected from `WORKER_MEM_LIMIT` |
| `cpu_limit` | no | `1.0` | Auto-injected from `WORKER_CPU_LIMIT` |

`timeout`, `mem_limit`, and `cpu_limit` are auto-injected by `profiles.py` using `setdefault` before rendering. They reflect the actual config values (from `config.py` or the profile's `[resources].timeout`), so the template always shows accurate limits. The Jinja2 `| default()` filters are only fallbacks if injection is bypassed. Request-level `claude_md_vars` can override them.

## Variable Definitions

Profile variables support two formats:

**Typed format** (recommended) - supports validation, defaults, required flag, allowed values, and description:

```toml
[prompt.variables.repo_url]
type = "string"          # string | integer | float | boolean
default = "https://github.com/org/repo"
required = true
description = "Git repository URL to clone and review"
enum = ["val1", "val2"]  # optional - restricts allowed values
```

**Legacy format** - plain key/value, value is used as the default:

```toml
[prompt.variables]
my_var = "default value"
```

Both formats work. Typed definitions are validated by `_validate_vars()` in `profiles.py`. Extra variables provided in the request but not defined in the profile are passed through to the template (flexible).

## Creating a New Template

1. Add your `.md.j2` file in the appropriate subfolder
2. Reference it in a profile:

```toml
[prompt]
template = "prompts/my-template.md.j2"

[prompt.variables.my_var]
type = "string"
default = "value"
description = "What this variable does"

[claude_md]
template = "claude-md/agent-base.md.j2"

[claude_md.variables]
guidelines = "Be concise"
```

Or override variables from the request:

```json
{
  "agent_id": "my-agent",
  "engine": "claude-code",
  "profile": "researcher",
  "prompt_vars": {"query": "best CI tools"},
  "claude_md_vars": {"guidelines": "Focus on open source tools"}
}
```
