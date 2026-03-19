#!/bin/bash
set -eu

# Wait for config injection via put_archive, then run job.
# Tower injects /tmp/config/.ready as the last file to signal readiness.

TIMEOUT=${CONFIG_TIMEOUT:-300}
ELAPSED=0

while [ ! -f /tmp/config/.ready ]; do
    sleep 0.1
    ELAPSED=$((ELAPSED + 1))
    if [ "$ELAPSED" -ge "$((TIMEOUT * 10))" ]; then
        echo "[worker] Config not received after ${TIMEOUT}s, exiting" >&2
        exit 1
    fi
done

exec /opt/run-job.sh
