#!/usr/bin/env bash
set -euo pipefail

# Verify that offline simulation is working correctly.
# Checks firewall rules, LAN connectivity, service health, and plugin status.
#
# Usage: scripts/verify_offline.sh

CHAIN="RETICULUMPI_OFFLINE"
STATE_FILE="/var/lib/reticulumpi/offline_simulation.active"
CONFIG_BACKUP="/etc/reticulumpi/config.yaml.pre-offline"
DASHBOARD="http://127.0.0.1:8080"

PASS=0
FAIL=0
SKIP=0

check() {
    local label="$1"
    shift
    if "$@" &>/dev/null; then
        echo "  PASS  $label"
        ((PASS++))
    else
        echo "  FAIL  $label"
        ((FAIL++))
    fi
}

check_fail() {
    local label="$1"
    shift
    if ! "$@" &>/dev/null; then
        echo "  PASS  $label"
        ((PASS++))
    else
        echo "  FAIL  $label (expected failure but succeeded)"
        ((FAIL++))
    fi
}

skip() {
    local label="$1"
    echo "  SKIP  $label"
    ((SKIP++))
}

# ---------------------------------------------------------------------------
# 1. Firewall checks
# ---------------------------------------------------------------------------

echo "=== Firewall ==="

check "IPv4 chain exists" iptables -n -L "$CHAIN"
check "IPv6 chain exists" ip6tables -n -L "$CHAIN"
check "State file exists" test -f "$STATE_FILE"

check_fail "DNS resolution blocked (google.com)" \
    timeout 3 getent hosts google.com

check_fail "TCP to 8.8.8.8:53 blocked" \
    timeout 3 bash -c 'echo | nc -w2 8.8.8.8 53'

# ---------------------------------------------------------------------------
# 2. LAN connectivity
# ---------------------------------------------------------------------------

echo ""
echo "=== LAN Connectivity ==="

GW=$(ip -4 route show default 2>/dev/null | awk '{print $3; exit}')
if [ -n "$GW" ]; then
    check "Default gateway ($GW) reachable" ping -c1 -W2 "$GW"
else
    skip "Default gateway (none configured)"
fi

check "Dashboard responds ($DASHBOARD)" \
    curl -sf -o /dev/null --max-time 5 "$DASHBOARD"

# ---------------------------------------------------------------------------
# 3. Service health
# ---------------------------------------------------------------------------

echo ""
echo "=== Services ==="

check "rnsd active" systemctl is-active --quiet rnsd
check "reticulumpi active" systemctl is-active --quiet reticulumpi

ERR_COUNT=$(journalctl -u reticulumpi --since "5 minutes ago" -p err --no-pager -q 2>/dev/null | wc -l)
if [ "$ERR_COUNT" -eq 0 ]; then
    echo "  PASS  No error-level log entries (last 5 min)"
    ((PASS++))
else
    echo "  FAIL  $ERR_COUNT error-level log entries (last 5 min)"
    ((FAIL++))
fi

# ---------------------------------------------------------------------------
# 4. Plugin health (if offline profile installed)
# ---------------------------------------------------------------------------

echo ""
echo "=== Plugins ==="

if [ -f "$CONFIG_BACKUP" ]; then
    STATUS=$(curl -sf --max-time 5 "$DASHBOARD/api/status" 2>/dev/null || echo "")
    if [ -n "$STATUS" ]; then
        FAILED=$(echo "$STATUS" | python3 -c "
import sys, json
d = json.load(sys.stdin)
failed = d.get('failed_plugins', [])
print(len(failed))
" 2>/dev/null || echo "?")

        if [ "$FAILED" = "0" ]; then
            echo "  PASS  No failed plugins"
            ((PASS++))
        elif [ "$FAILED" = "?" ]; then
            skip "Could not parse plugin status"
        else
            echo "  FAIL  $FAILED failed plugin(s)"
            ((FAIL++))
        fi
    else
        skip "Dashboard API not reachable (auth required?)"
    fi
else
    skip "Plugin check (offline profile not installed)"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
echo "=== Summary ==="
echo "  $PASS passed, $FAIL failed, $SKIP skipped"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
