#!/usr/bin/env bash
set -euo pipefail

# Privileged helper for the captive_portal plugin.  Called via sudo from
# the unprivileged reticulumpi service to manage iptables NAT rules and
# dnsmasq DNS overrides.
#
# Usage:
#   sudo captive_portal_helper.sh activate  <iface> <port> <gateway_ip>
#   sudo captive_portal_helper.sh deactivate
#   sudo captive_portal_helper.sh cleanup
#   sudo captive_portal_helper.sh status

CHAIN="RETICULUMPI_CAPTIVE"
DNSMASQ_CONF="/etc/dnsmasq.d/reticulumpi-captive-portal.conf"
STATE_FILE="/var/lib/reticulumpi/captive_portal.active"

PORTAL_DOMAINS=(
    captive.apple.com
    connectivitycheck.gstatic.com
    connectivitycheck.android.com
    www.msftconnecttest.com
    nmcheck.gnome.org
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

die() { echo "Error: $*" >&2; exit 1; }

need_root() {
    [ "$(id -u)" -eq 0 ] || die "Must be run as root (use sudo)."
}

chain_exists() {
    iptables -t nat -n -L "$CHAIN" &>/dev/null
}

# ---------------------------------------------------------------------------
# activate — install DNS overrides + iptables redirect
# ---------------------------------------------------------------------------

cmd_activate() {
    need_root
    local iface="${1:?Usage: activate <iface> <port> <gateway_ip>}"
    local port="${2:?Usage: activate <iface> <port> <gateway_ip>}"
    local gw_ip="${3:?Usage: activate <iface> <port> <gateway_ip>}"

    # --- input validation ---
    [[ "$iface" =~ ^[a-zA-Z0-9._-]+$ ]] || die "Invalid interface name: $iface"
    [[ "$port" =~ ^[0-9]+$ ]] && [ "$port" -ge 1024 ] && [ "$port" -le 65535 ] \
        || die "Port must be numeric 1024-65535, got: $port"
    [[ "$gw_ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Invalid IPv4 address: $gw_ip"

    trap 'cmd_cleanup 2>/dev/null; exit 1' ERR

    # --- dnsmasq config ---
    {
        echo "# Managed by ReticulumPi captive_portal plugin -- do not edit"
        for domain in "${PORTAL_DOMAINS[@]}"; do
            echo "address=/${domain}/${gw_ip}"
        done
    } > "$DNSMASQ_CONF"

    if systemctl is-active --quiet dnsmasq; then
        systemctl reload dnsmasq
    fi

    # --- iptables ---
    if ! chain_exists; then
        iptables -t nat -N "$CHAIN"
        iptables -t nat -A "$CHAIN" \
            -i "$iface" -p tcp --dport 80 \
            -j REDIRECT --to-port "$port"
        iptables -t nat -I PREROUTING 1 -j "$CHAIN"
    fi

    # state file
    mkdir -p "$(dirname "$STATE_FILE")"
    date -Iseconds > "$STATE_FILE"

    echo "activated"
}

# ---------------------------------------------------------------------------
# deactivate — remove iptables + DNS overrides
# ---------------------------------------------------------------------------

cmd_deactivate() {
    need_root

    # --- iptables ---
    if chain_exists; then
        iptables -t nat -D PREROUTING -j "$CHAIN" 2>/dev/null || true
        iptables -t nat -F "$CHAIN"
        iptables -t nat -X "$CHAIN"
    fi

    # --- dnsmasq config ---
    if [ -f "$DNSMASQ_CONF" ]; then
        rm -f "$DNSMASQ_CONF"
        if systemctl is-active --quiet dnsmasq; then
            systemctl reload dnsmasq
        fi
    fi

    rm -f "$STATE_FILE"

    echo "deactivated"
}

# ---------------------------------------------------------------------------
# cleanup — same as deactivate but silent (for startup crash recovery)
# ---------------------------------------------------------------------------

cmd_cleanup() {
    need_root

    if chain_exists; then
        iptables -t nat -D PREROUTING -j "$CHAIN" 2>/dev/null || true
        iptables -t nat -F "$CHAIN" 2>/dev/null || true
        iptables -t nat -X "$CHAIN" 2>/dev/null || true
    fi

    if [ -f "$DNSMASQ_CONF" ]; then
        rm -f "$DNSMASQ_CONF"
        if systemctl is-active --quiet dnsmasq; then
            systemctl reload dnsmasq 2>/dev/null || true
        fi
    fi

    rm -f "$STATE_FILE"

    echo "cleaned"
}

# ---------------------------------------------------------------------------
# status — report current state
# ---------------------------------------------------------------------------

cmd_status() {
    local chain_active="false"
    local dns_active="false"
    local state_file_present="false"

    chain_exists && chain_active="true"
    [ -f "$DNSMASQ_CONF" ] && dns_active="true"
    [ -f "$STATE_FILE" ] && state_file_present="true"

    echo "chain_active=${chain_active}"
    echo "dns_active=${dns_active}"
    echo "state_file=${state_file_present}"
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

case "${1:-}" in
    activate)   cmd_activate "${2:-}" "${3:-}" "${4:-}" ;;
    deactivate) cmd_deactivate ;;
    cleanup)    cmd_cleanup ;;
    status)     cmd_status ;;
    *)
        echo "Usage: $(basename "$0") {activate|deactivate|cleanup|status}" >&2
        exit 1
        ;;
esac
