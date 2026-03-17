#!/bin/bash
set -eu

# Post-job hook - runs inside the worker container AFTER the agent finishes.
# Working directory: /workspace
#
# Available env vars:
#   JOB_STATUS    - "completed" or "failed"
#   JOB_EXIT_CODE - e.g. "0"
#
# Reference in a profile:
#   [hooks]
#   post = "post-job.example.sh"
#
# Examples:
#   cp important-file.txt /output/
#   echo "{\"status\":\"$JOB_STATUS\"}" > /output/summary.json

echo "[hook] post-job - status: ${JOB_STATUS:-unknown}, exit: ${JOB_EXIT_CODE:-?}"
