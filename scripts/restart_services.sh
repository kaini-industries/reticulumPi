#!/usr/bin/env bash
set -euo pipefail

# Root-owned helper invoked only by the validated control broker. Restart rnsd
# synchronously, verify its shared-instance socket, then schedule ReticulumPi's
# own restart after the broker has had time to flush its response.

[ "$(id -u)" -eq 0 ] || {
    echo "restart_services.sh must run as root" >&2
    exit 1
}

if systemctl is-active --quiet rnsd.service; then
    systemctl restart rnsd.service
    ready=false
    for _attempt in $(seq 1 60); do
        if ss -xa 2>/dev/null | grep -q "@rns/default"; then
            ready=true
            break
        fi
        sleep 1
    done
    if [ "$ready" != true ]; then
        echo "rnsd did not expose @rns/default within 60 seconds" >&2
        exit 1
    fi
fi

systemd-run \
    --quiet \
    --collect \
    --unit=reticulumpi-deferred-restart \
    --on-active=2s \
    /usr/bin/systemctl restart reticulumpi.service

echo "ReticulumPi restart scheduled"
