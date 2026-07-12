#!/usr/bin/env bash
set -euo pipefail

# Root-owned helper invoked by the privileged control broker. It simulates an
# internet outage while preserving loopback, directly attached LANs, and
# multicast. Application state is changed only through AppConfig's allowlisted
# runtime overlay; /etc/reticulumpi/config.yaml is never read or replaced.

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

CHAIN="RETICULUMPI_OFFLINE"
STATE_DIR="/var/lib/reticulumpi"
STATE_FILE="$STATE_DIR/offline_simulation.active"
RUNTIME_OVERLAY="$STATE_DIR/runtime-overrides.yaml"
OFFLINE_OVERLAY="/usr/share/reticulumpi/config/offline_profile.yaml"
SERVICE_USER="reticulumpi"

die() {
    echo "Error: $*" >&2
    exit 1
}

need_root() {
    [ "$(id -u)" -eq 0 ] || die "This helper must run as root."
}

validate_compatibility_flag() {
    case "${1:-}" in
        ""|--with-profile) ;;
        *) die "unknown option: $1" ;;
    esac
}

chain_exists() {
    iptables -n -L "$CHAIN" >/dev/null 2>&1
}

chain6_exists() {
    ip6tables -n -L "$CHAIN" >/dev/null 2>&1
}

detect_local_subnets_v4() {
    ip -4 route show scope link 2>/dev/null \
        | awk '{print $1}' \
        | grep -E '^[0-9]+([.][0-9]+){3}/[0-9]+$' \
        || true
}

prepare_state_directory() {
    [ ! -L "$STATE_DIR" ] || die "state directory may not be a symlink: $STATE_DIR"
    [ ! -L "$RUNTIME_OVERLAY" ] || die "runtime overlay may not be a symlink"
    install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_DIR"
}

validate_offline_overlay() {
    [ -f "$OFFLINE_OVERLAY" ] && [ ! -L "$OFFLINE_OVERLAY" ] \
        || die "offline overlay is missing or unsafe: $OFFLINE_OVERLAY"

    # Keep this parser intentionally strict: comments and blank lines are
    # ignored, and the only accepted payload is internet.force_offline=true.
    local actual expected
    actual="$(grep -Ev '^[[:space:]]*(#|$)' "$OFFLINE_OVERLAY")"
    expected="$(printf 'internet:\n  force_offline: true')"
    [ "$actual" = "$expected" ] \
        || die "offline overlay contains keys outside internet.force_offline"
}

fsync_path() {
    # GNU coreutils sync supports -f on Raspberry Pi OS Bookworm.
    sync -f "$1"
}

apply_offline_overlay() {
    prepare_state_directory
    validate_offline_overlay

    local temporary
    temporary="$(mktemp "$STATE_DIR/.runtime-overrides.XXXXXX")"
    trap 'rm -f "${temporary:-}"' RETURN
    install -m 0600 -o "$SERVICE_USER" -g "$SERVICE_USER" \
        "$OFFLINE_OVERLAY" "$temporary"
    fsync_path "$temporary"
    mv -Tf -- "$temporary" "$RUNTIME_OVERLAY"
    fsync_path "$STATE_DIR"
    trap - RETURN
    echo "  Applied runtime overlay: internet.force_offline=true"
}

remove_offline_overlay() {
    prepare_state_directory
    if [ ! -e "$RUNTIME_OVERLAY" ]; then
        echo "  Runtime overlay is already absent."
        return 0
    fi
    [ -f "$RUNTIME_OVERLAY" ] && [ ! -L "$RUNTIME_OVERLAY" ] \
        || die "runtime overlay is not a regular file"
    rm -f -- "$RUNTIME_OVERLAY"
    fsync_path "$STATE_DIR"
    echo "  Removed forced-offline runtime overlay."
}

schedule_reticulumpi_restart() {
    if systemctl is-active --quiet reticulumpi.service; then
        systemd-run \
            --quiet \
            --collect \
            --unit="reticulumpi-offline-restart-$$" \
            --on-active=2s \
            /usr/bin/systemctl restart reticulumpi.service
        echo "  ReticulumPi restart scheduled."
    fi
}

cleanup_firewall() {
    if chain_exists; then
        while iptables -C OUTPUT -j "$CHAIN" >/dev/null 2>&1; do
            iptables -D OUTPUT -j "$CHAIN"
        done
        iptables -F "$CHAIN"
        iptables -X "$CHAIN"
    fi
    if chain6_exists; then
        while ip6tables -C OUTPUT -j "$CHAIN" >/dev/null 2>&1; do
            ip6tables -D OUTPUT -j "$CHAIN"
        done
        ip6tables -F "$CHAIN"
        ip6tables -X "$CHAIN"
    fi
}

write_state_file() {
    prepare_state_directory
    local temporary
    temporary="$(mktemp "$STATE_DIR/.offline-simulation.XXXXXX")"
    trap 'rm -f "${temporary:-}"' RETURN
    date -u +'%Y-%m-%dT%H:%M:%SZ' > "$temporary"
    chmod 0600 "$temporary"
    chown root:root "$temporary"
    fsync_path "$temporary"
    mv -Tf -- "$temporary" "$STATE_FILE"
    fsync_path "$STATE_DIR"
    trap - RETURN
}

cmd_on() {
    need_root
    validate_compatibility_flag "${1:-}"

    if chain_exists || chain6_exists; then
        if ! chain_exists || ! chain6_exists; then
            die "offline firewall is partially configured; run '$(basename "$0") off'"
        fi
        echo "Offline firewall is already active; refreshing its runtime overlay."
        apply_offline_overlay
        write_state_file
        schedule_reticulumpi_restart
        return 0
    fi

    echo "=== Enabling offline simulation ==="
    trap 'cleanup_firewall >/dev/null 2>&1 || true' ERR

    iptables -N "$CHAIN"
    iptables -A "$CHAIN" -d 127.0.0.0/8 -j ACCEPT
    while IFS= read -r subnet; do
        [ -n "$subnet" ] || continue
        iptables -A "$CHAIN" -d "$subnet" -j ACCEPT
        echo "  Allow IPv4 subnet: $subnet"
    done < <(detect_local_subnets_v4)
    iptables -A "$CHAIN" -d 224.0.0.0/4 -j ACCEPT
    iptables -A "$CHAIN" -j DROP
    iptables -I OUTPUT 1 -j "$CHAIN"

    ip6tables -N "$CHAIN"
    ip6tables -A "$CHAIN" -d ::1/128 -j ACCEPT
    ip6tables -A "$CHAIN" -d fe80::/10 -j ACCEPT
    ip6tables -A "$CHAIN" -d ff00::/8 -j ACCEPT
    ip6tables -A "$CHAIN" -j DROP
    ip6tables -I OUTPUT 1 -j "$CHAIN"

    apply_offline_overlay
    write_state_file
    trap - ERR
    schedule_reticulumpi_restart

    echo "  Outbound internet traffic is blocked; LAN and multicast remain available."
    echo "=== Offline simulation active ==="
}

cmd_off() {
    need_root
    validate_compatibility_flag "${1:-}"

    echo "=== Disabling offline simulation ==="
    cleanup_firewall
    remove_offline_overlay
    rm -f -- "$STATE_FILE"
    fsync_path "$STATE_DIR"
    schedule_reticulumpi_restart
    echo "=== Offline simulation disabled ==="
}

cmd_status() {
    echo "=== Offline Simulation Status ==="
    chain_exists && echo "  IPv4 firewall: active" || echo "  IPv4 firewall: inactive"
    chain6_exists && echo "  IPv6 firewall: active" || echo "  IPv6 firewall: inactive"
    # STATE_DIR is intentionally service-owned. Never open marker contents as
    # root: an unprivileged service process could replace the pathname with a
    # symlink between status requests. The marker is not an authority; firewall
    # and overlay state above/below are the authoritative status signals.
    if [ -L "$STATE_FILE" ]; then
        echo "  State marker: unsafe (ignored)"
    elif [ -f "$STATE_FILE" ]; then
        echo "  State marker: present"
    else
        echo "  State marker: absent"
    fi
    [ -f "$RUNTIME_OVERLAY" ] && [ ! -L "$RUNTIME_OVERLAY" ] \
        && echo "  Forced-offline overlay: active" \
        || echo "  Forced-offline overlay: inactive"
}

usage() {
    echo "Usage: $(basename "$0") {on|off|status}"
    echo
    echo "  on      Block internet traffic and atomically apply the forced-offline overlay"
    echo "  off     Remove firewall rules and the forced-offline overlay"
    echo "  status  Report firewall and overlay state"
    echo
    echo "The historical --with-profile flag is accepted as a compatibility no-op."
}

case "${1:-}" in
    on) cmd_on "${2:-}" ;;
    off) cmd_off "${2:-}" ;;
    status)
        [ "$#" -eq 1 ] || die "status accepts no arguments"
        cmd_status
        ;;
    -h|--help) usage ;;
    *) usage; exit 1 ;;
esac
