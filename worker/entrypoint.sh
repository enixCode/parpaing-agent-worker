#!/bin/bash
set -eu

# Wait for config injection via put_archive, then run job.
# Tower injects /tmp/config/.ready as the last file to signal readiness.

while [ ! -f /tmp/config/.ready ]; do sleep 0.1; done
exec /opt/run-job.sh
