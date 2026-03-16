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
| `task` | no | Analyse le code dans /workspace... |

### prompts/code-review.md.j2

```jinja
Review du code dans /workspace.
{% if repo_url %}Le repo a été cloné depuis : {{ repo_url }}{% endif %}
{% if focus %}Focus : {{ focus }}{% endif %}
...
Vérifier les tests existants — lancer `{{ test_command | default("npm test || pytest || go test ./...") }}`
{% if branch %}Branche à reviewer : {{ branch }}{% endif %}
```

| Variable | Required | Default |
|----------|----------|---------|
| `repo_url` | no | — |
| `focus` | no | — |
| `test_command` | no | `npm test \|\| pytest \|\| go test ./...` |
| `branch` | no | — |

### prompts/researcher.md.j2

```jinja
Recherche approfondie : {{ query | default("Analyse le sujet fourni") }}
{% if criteria %}Critères de sélection : {{ criteria }}{% endif %}
...
{% if context %}Contexte additionnel : {{ context }}{% endif %}
```

| Variable | Required | Default |
|----------|----------|---------|
| `query` | no | Analyse le sujet fourni |
| `criteria` | no | — |
| `context` | no | — |

## CLAUDE.md Template

Injected into the worker's workspace as `.claude/CLAUDE.md`. Referenced via `[claude_md]` section in profiles.

### claude-md/agent-base.md.j2

```jinja
# Instructions Agent

Tu es un agent autonome dans un container Docker isolé.
- Workspace : `/workspace` (ton répertoire de travail)
- Output : `/output/result.json` (tes résultats finaux)
- Timeout : {{ timeout | default("3600") }}s | RAM : {{ mem_limit | default("2g") }} | CPU : {{ cpu_limit | default("2.0") }}

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

| Variable | Required | Default |
|----------|----------|---------|
| `role` | no | — |
| `guidelines` | no | — |
| `constraints` | no | — |
| `output_instructions` | no | — |
| `timeout` | no | `3600` |
| `mem_limit` | no | `512m` |
| `cpu_limit` | no | `2.0` |

## Creating a New Template

1. Add your `.md.j2` file in the appropriate subfolder
2. Reference it in a profile:

```toml
[prompt]
template = "prompts/my-template.md.j2"

[prompt.variables]
my_var = "value"

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
