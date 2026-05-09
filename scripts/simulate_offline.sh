#!/usr/bin/env bash
set -euo pipefail

# Simulate no-internet operation by blocking outbound traffic to non-local
# destinations.  LAN, loopback, and multicast are preserved so the web
# dashboard, RNS AutoInterface, and serial devices keep working.
#
# Usage:
#   sudo scripts/simulate_offline.sh on  [--with-profile]
#   sudo scripts/simulate_offline.sh off [--with-profile]
#   scripts/simulate_offline.sh status

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CHAIN="RETICULUMPI_OFFLINE"
STATE_FILE="/var/lib/reticulumpi/offline_simulation.active"
CONFIG_FILE="/etc/reticulumpi/config.yaml"
CONFIG_BACKUP="/etc/reticulumpi/config.yaml.pre-offline"
OFFLINE_PROFILE="$PROJECT_DIR/config/reticulumpi/offline_profile.yaml"
SERVICE_USER="reticulumpi"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

die() { echo "Error: $*" >&2; exit 1; }

need_root() {
    [ "$(id -u)" -eq 0 ] || die "This command must be run as root (use sudo)."
}

chain_exists() {
    iptables -n -L "$CHAIN" &>/dev/null
}

chain6_exists() {
    ip6tables -n -L "$CHAIN" &>/dev/null
}

detect_local_subnets_v4() {
    ip -4 route show scope link 2>/dev/null \
        | awk '{print $1}' \
        | grep -E '^[0-9]+\.' \
        || true
}

# ---------------------------------------------------------------------------
# on — create firewall rules
# ---------------------------------------------------------------------------

cmd_on() {
    need_root

    if chain_exists; then
        echo "Offline simulation is already active."
        echo "Run '$(basename "$0") status' for details."
        return 0
    fi

    echo "=== Enabling offline simulation ==="

    # --- IPv4 ---
    iptables -N "$CHAIN"
    iptables -A "$CHAIN" -d 127.0.0.0/8 -j ACCEPT
    for subnet in $(detect_local_subnets_v4); do
        iptables -A "$CHAIN" -d "$subnet" -j ACCEPT
        echo "  Allow IPv4 subnet: $subnet"
    done
    iptables -A "$CHAIN" -d 224.0.0.0/4 -j ACCEPT      # multicast
    iptables -A "$CHAIN" -j DROP
    iptables -I OUTPUT 1 -j "$CHAIN"

    # --- IPv6 ---
    ip6tables -N "$CHAIN"
    ip6tables -A "$CHAIN" -d ::1/128 -j ACCEPT           # loopback
    ip6tables -A "$CHAIN" -d fe80::/10 -j ACCEPT          # link-local
    ip6tables -A "$CHAIN" -d ff00::/8 -j ACCEPT            # all multicast
    ip6tables -A "$CHAIN" -j DROP
    ip6tables -I OUTPUT 1 -j "$CHAIN"

    # state file
    mkdir -p "$(dirname "$STATE_FILE")"
    date -Iseconds > "$STATE_FILE"

    echo "  Outbound internet traffic is now blocked."
    echo "  Local LAN and multicast traffic is allowed."

    if [ "${1:-}" = "--with-profile" ]; then
        apply_offline_profile
    fi

    echo "=== Offline simulation active ==="
}

# ---------------------------------------------------------------------------
# off — remove firewall rules
# ---------------------------------------------------------------------------

cmd_off() {
    need_root

    echo "=== Disabling offline simulation ==="

    if [ "${1:-}" = "--with-profile" ]; then
        restore_online_profile
    fi

    # --- IPv4 ---
    if chain_exists; then
        iptables -D OUTPUT -j "$CHAIN" 2>/dev/null || true
        iptables -F "$CHAIN"
        iptables -X "$CHAIN"
        echo "  IPv4 rules removed."
    else
        echo "  IPv4 chain not found (already clean)."
    fi

    # --- IPv6 ---
    if chain6_exists; then
        ip6tables -D OUTPUT -j "$CHAIN" 2>/dev/null || true
        ip6tables -F "$CHAIN"
        ip6tables -X "$CHAIN"
        echo "  IPv6 rules removed."
    else
        echo "  IPv6 chain not found (already clean)."
    fi

    rm -f "$STATE_FILE"

    echo "=== Offline simulation disabled ==="
}

# ---------------------------------------------------------------------------
# status — show current state
# ---------------------------------------------------------------------------

cmd_status() {
    echo "=== Offline Simulation Status ==="

    if chain_exists; then
        echo "  IPv4 chain: ACTIVE"
        echo ""
        echo "  IPv4 rules:"
        iptables -n -L "$CHAIN" --line-numbers 2>/dev/null | sed 's/^/    /'
    else
        echo "  IPv4 chain: inactive"
    fi

    echo ""

    if chain6_exists; then
        echo "  IPv6 chain: ACTIVE"
        echo ""
        echo "  IPv6 rules:"
        ip6tables -n -L "$CHAIN" --line-numbers 2>/dev/null | sed 's/^/    /'
    else
        echo "  IPv6 chain: inactive"
    fi

    echo ""

    if [ -f "$STATE_FILE" ]; then
        echo "  State file: $STATE_FILE"
        echo "  Active since: $(cat "$STATE_FILE")"
    else
        echo "  State file: not present"
    fi

    if [ -f "$CONFIG_BACKUP" ]; then
        echo "  Config backup: $CONFIG_BACKUP (offline profile installed)"
    fi
}

# ---------------------------------------------------------------------------
# profile management
# ---------------------------------------------------------------------------

apply_offline_profile() {
    [ -f "$OFFLINE_PROFILE" ] || die "Offline profile not found: $OFFLINE_PROFILE"

    echo ""
    echo "  Installing offline config profile..."
    if [ -f "$CONFIG_BACKUP" ]; then
        echo "  Backup already exists — skipping backup."
    else
        cp "$CONFIG_FILE" "$CONFIG_BACKUP"
        echo "  Backed up: $CONFIG_FILE -> $CONFIG_BACKUP"
    fi

    cp "$OFFLINE_PROFILE" "$CONFIG_FILE"
    chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_FILE"
    echo "  Installed offline profile."

    restart_services
}

restore_online_profile() {
    if [ ! -f "$CONFIG_BACKUP" ]; then
        echo "  No config backup found — skipping profile restore."
        return 0
    fi

    echo ""
    echo "  Restoring original config..."
    mv "$CONFIG_BACKUP" "$CONFIG_FILE"
    chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_FILE"
    echo "  Restored: $CONFIG_FILE"

    restart_services
}

restart_services() {
    echo "  Restarting services..."
    if systemctl is-active --quiet rnsd; then
        systemctl restart rnsd
        echo "  Waiting for rnsd shared instance socket..."
        for i in $(seq 1 60); do
            ss -xa 2>/dev/null | grep -q "@rns/default" && break
            sleep 1
        done
        if ss -xa 2>/dev/null | grep -q "@rns/default"; then
            echo "  rnsd ready (${i}s)"
        else
            echo "  Warning: rnsd socket not detected after 60s, continuing anyway"
        fi
    fi
    systemctl restart reticulumpi
    echo "  Services restarted."
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

usage() {
    echo "Usage: $(basename "$0") {on|off|status} [--with-profile]"
    echo ""
    echo "Commands:"
    echo "  on   [--with-profile]  Block outbound internet traffic"
    echo "  off  [--with-profile]  Remove firewall rules and restore internet"
    echo "  status                 Show current simulation state"
    echo ""
    echo "Flags:"
    echo "  --with-profile   Also swap config to disable internet-dependent plugins"
    echo "                   (on: install offline profile, off: restore original)"
}

case "${1:-}" in
    on)     cmd_on "${2:-}" ;;
    off)    cmd_off "${2:-}" ;;
    status) cmd_status ;;
    -h|--help) usage ;;
    *)      usage; exit 1 ;;
esac
