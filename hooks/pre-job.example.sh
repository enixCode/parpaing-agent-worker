#!/bin/bash
set -eu

# Pre-job hook — runs inside the worker container BEFORE the agent starts.
# Working directory: /workspace
#
# Reference in a profile:
#   [hooks]
#   pre = "pre-job.example.sh"
#
# Examples:
#   git clone "$REPO_URL" .
#   curl -sL "$FILE_URL" -o input.txt
#   npm install 2>/dev/null || true

echo "[hook] pre-job — nothing to do"
