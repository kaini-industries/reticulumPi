#!/bin/sh
set -e

umask 077
runtime_dir=${RETICULUMPI_RUNTIME_DIR:-/run/reticulumpi}
ready_file=${RETICULUMPI_READY_FILE:-$runtime_dir/ready}
rnsd_pid_file=${RETICULUMPI_RNSD_PID_FILE:-$runtime_dir/rnsd.pid}
mkdir -p \
    /data/.config/reticulumpi \
    /data/.local/share/reticulumpi \
    /data/.local/state \
    /data/.reticulum \
    /cache \
    "$runtime_dir"
rm -f "$ready_file" "$rnsd_pid_file"

# Start rnsd in background for shared instance mode.
# This allows reticulumPi and NomadNet to share one Reticulum transport.
# Harmless if nomadnet_server plugin is not enabled.
if command -v rnsd >/dev/null 2>&1; then
    echo "Starting rnsd..."
    rnsd &
    rnsd_pid=$!
    rnsd_pid_tmp="$rnsd_pid_file.$$"
    printf '%s\n' "$rnsd_pid" >"$rnsd_pid_tmp"
    chmod 0600 "$rnsd_pid_tmp"
    mv -f "$rnsd_pid_tmp" "$rnsd_pid_file"
    rns_config="${RETICULUMPI_RNS_CONFIG_DIR:-$HOME/.reticulum}/config"
    rnsd_socket_ready() {
        python -c '
import socket
import sys

from RNS.vendor.configobj import ConfigObj

reticulum = ConfigObj(sys.argv[1]).get("reticulum", {})
socket_type = str(reticulum.get("shared_instance_type", "unix")).lower()
instance_name = str(reticulum.get("instance_name", "default"))
port = int(reticulum.get("shared_instance_port", 37428))
family = socket.AF_INET if socket_type == "tcp" else socket.AF_UNIX
address = ("127.0.0.1", port) if family == socket.AF_INET else "\0rns/" + instance_name
probe = socket.socket(family, socket.SOCK_STREAM)
probe.settimeout(0.25)
try:
    result = probe.connect_ex(address)
finally:
    probe.close()
raise SystemExit(0 if result == 0 else 1)
' "$rns_config"
    }
    ready=false
    attempts=0
    while [ "$attempts" -lt 60 ]; do
        if ! kill -0 "$rnsd_pid" 2>/dev/null; then
            echo "rnsd exited before becoming ready" >&2
            exit 1
        fi
        # Do not launch an RNS client until rnsd has finished creating its
        # storage tree and opened the configured shared-instance socket.
        if rnsd_socket_ready && rnstatus >/dev/null 2>&1; then
            ready=true
            break
        fi
        attempts=$((attempts + 1))
        sleep 0.5
    done
    if [ "$ready" != true ]; then
        echo "rnsd did not become ready within 30 seconds" >&2
        exit 1
    fi

    # ``exec`` below makes ReticulumPi the direct tini child while preserving
    # rnsd as its child. Detect both a missing process and an unreaped zombie,
    # invalidate readiness immediately, and terminate ReticulumPi so the
    # container restart policy can recover the dependency as one unit.
    service_pid=$$
    (
        while kill -0 "$rnsd_pid" 2>/dev/null; do
            rnsd_state=""
            if [ -r "/proc/$rnsd_pid/stat" ]; then
                IFS=' ' read -r _rnsd_stat_pid _rnsd_stat_name rnsd_state _rnsd_stat_rest \
                    <"/proc/$rnsd_pid/stat" || rnsd_state=""
            fi
            [ "$rnsd_state" = "Z" ] && break
            sleep 1
        done
        rm -f "$ready_file" "$rnsd_pid_file"
        echo "rnsd exited after startup; stopping ReticulumPi" >&2
        kill -TERM "$service_pid" 2>/dev/null || true
    ) &
fi

# Start reticulumPi as PID 1 (receives signals for graceful shutdown)
exec reticulumpi "$@"
