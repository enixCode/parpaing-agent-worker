#!/bin/bash
set -euo pipefail

# --- Worker job runner (engine-agnostic) ---
# Reads config from /tmp/config/ (injected by Tower via put_archive)
# Builds CLI command from engine config in job.json
# Writes result to /output/result.json

CONFIG_DIR="${WORKER_CONFIG_DIR:-/tmp/config}"
OUTPUT_DIR="${WORKER_OUTPUT_DIR:-/output}"
WORKSPACE_DIR="/workspace"

echo "[worker] Starting job..."

# 1. Init ~/.claude (prevents onboarding wizard - claude-code specific, harmless for others)
mkdir -p ~/.claude
echo '{}' > ~/.claude/.claude.json 2>/dev/null || true

# 2. Apply settings.json if present (plugins config)
if [ -f "${CONFIG_DIR}/settings.json" ]; then
    cp "${CONFIG_DIR}/settings.json" ~/.claude/settings.json
    echo "[worker] Applied settings.json"
fi

# 3. Inject CLAUDE.md if provided
if [ -f "${CONFIG_DIR}/CLAUDE.md" ]; then
    mkdir -p "${WORKSPACE_DIR}/.claude"
    cp "${CONFIG_DIR}/CLAUDE.md" "${WORKSPACE_DIR}/.claude/CLAUDE.md"
    echo "[worker] Applied CLAUDE.md"
fi

# 4. Run pre-job hook (if present)
if [ -f "${CONFIG_DIR}/pre-job.sh" ]; then
    echo "[worker] Running pre-job hook..."
    (cd "${WORKSPACE_DIR}" && "${CONFIG_DIR}/pre-job.sh") || {
        echo "[worker] Pre-job hook failed with exit code $?"
        exit 1
    }
    echo "[worker] Pre-job hook completed"
fi

# 5. Parse job.json → engine config + CLI args
JOB_JSON="${CONFIG_DIR}/job.json"
if [ ! -f "${JOB_JSON}" ]; then
    echo "[worker] ERROR: job.json not found"
    exit 1
fi

_VARS="/tmp/_vars.sh"
node /opt/parse-job.js "${JOB_JSON}" "${_VARS}"
source "${_VARS}"
rm -f "${_VARS}"

if [ -z "${PROMPT:-}" ]; then
    echo "[worker] ERROR: empty prompt"
    exit 1
fi

BINARY="${ENGINE_BINARY:?ENGINE_BINARY not set by parse-job.js}"
echo "[worker] Engine: ${ENGINE_ID:-unknown} (${BINARY})"

# ENGINE_ARGS is sourced as a bash array from _vars.sh
ARGS=("${ENGINE_ARGS[@]}")

# Add engine-agnostic flags
if [ "${BINARY}" = "claude" ]; then
    ARGS+=(--dangerously-skip-permissions)
    [ -f "${CONFIG_DIR}/mcp.json" ] && ARGS+=(--mcp-config "${CONFIG_DIR}/mcp.json")
fi

# 6. Run agent (or dry-run)
cd "${WORKSPACE_DIR}"
mkdir -p "${OUTPUT_DIR}"

if [ "${DRY_RUN:-}" = "1" ]; then
    echo "[worker] DRY_RUN - command that would run:"
    echo "${BINARY} ${ARGS[*]}"
    ENGINE_ID="${ENGINE_ID:-claude-code}" BINARY="${BINARY}" \
      node -e "process.stdout.write(JSON.stringify({dry_run:true, engine:process.env.ENGINE_ID, binary:process.env.BINARY, args:process.argv.slice(1)}))" \
      -- "${ARGS[@]}" > "${OUTPUT_DIR}/result.json"
    EXIT_CODE=0
else
    echo "[worker] Starting ${BINARY}..."
    if [ "${OUTPUT_MODE:-stdout}" = "file" ]; then
        "${BINARY}" "${ARGS[@]}" 2>"${OUTPUT_DIR}/stderr.log"
        EXIT_CODE=$?
        # Copy output from engine-specific path
        if [ -n "${OUTPUT_PATH:-}" ] && [ -f "${OUTPUT_PATH}" ]; then
            cp "${OUTPUT_PATH}" "${OUTPUT_DIR}/result.json"
        fi
    else
        "${BINARY}" "${ARGS[@]}" 2>"${OUTPUT_DIR}/stderr.log" | tee "${OUTPUT_DIR}/result.json"
        EXIT_CODE=${PIPESTATUS[0]}
    fi
    echo "[worker] ${BINARY} exited with code: ${EXIT_CODE}"
fi

# 7. Run post-job hook (if present)
if [ -f "${CONFIG_DIR}/post-job.sh" ]; then
    echo "[worker] Running post-job hook..."
    export JOB_STATUS=$( [ "${EXIT_CODE}" = "0" ] && echo "completed" || echo "failed" )
    export JOB_EXIT_CODE="${EXIT_CODE}"
    if (cd "${WORKSPACE_DIR}" && "${CONFIG_DIR}/post-job.sh"); then
        echo "[worker] Post-job hook completed"
    else
        echo "[worker] Post-job hook failed with exit code $?"
    fi
fi

exit ${EXIT_CODE}