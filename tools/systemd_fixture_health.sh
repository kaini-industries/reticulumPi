#!/usr/bin/env bash

wait_for_systemd_running() {
    local label=$1
    local timeout_seconds=${2:-60}
    local started=$SECONDS
    local state

    while true; do
        state=$(systemctl is-system-running 2>/dev/null || true)
        case "$state" in
            running)
                return 0
                ;;
            initializing|starting)
                if (( SECONDS - started >= timeout_seconds )); then
                    echo "$label systemd did not finish booting within ${timeout_seconds} seconds (last state: $state)" >&2
                    systemctl --failed --no-pager >&2 || true
                    return 1
                fi
                sleep 1
                ;;
            *)
                echo "$label systemd fixture is not healthy: ${state:-unknown}" >&2
                systemctl --failed --no-pager >&2 || true
                return 1
                ;;
        esac
    done
}
