#!/bin/sh
set -e

# Start rnsd in background for shared instance mode.
# This allows reticulumPi and NomadNet to share one Reticulum transport.
# Harmless if nomadnet_server plugin is not enabled.
if command -v rnsd >/dev/null 2>&1; then
    echo "Starting rnsd..."
    rnsd &
    # Give rnsd time to create the shared instance socket
    sleep 2
fi

# Start reticulumPi as PID 1 (receives signals for graceful shutdown)
exec reticulumpi "$@"
