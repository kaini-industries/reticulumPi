#!/usr/bin/env bash
set -euo pipefail

# Compatibility entry point for the transactional administrator. This script
# never pulls a mutable branch or modifies the active virtual environment.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BUNDLE="$PROJECT_DIR"
INSTALL_ROOT="/opt/reticulumpi"
MODE=--dry-run

usage() {
    echo "Usage: $0 [--bundle PATH] [--install-root PATH] [--dry-run|--apply]"
    echo ""
    echo "The bundle must be a trusted ReticulumPi source directory or wheel."
    echo "A separately signed, root-owned recovery administrator must already be installed."
    echo "Updates are staged, validated, and switched atomically by reticulumpi-admin."
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --bundle) BUNDLE="${2:?--bundle requires a path}"; shift 2 ;;
        --bundle=*) BUNDLE="${1#*=}"; shift ;;
        --install-root|--install-dir)
            INSTALL_ROOT="${2:?$1 requires a path}"
            shift 2
            ;;
        --install-root=*|--install-dir=*) INSTALL_ROOT="${1#*=}"; shift ;;
        --dry-run) MODE=--dry-run; shift ;;
        --apply) MODE=--apply; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ ! -e "$BUNDLE" ]; then
    echo "Bundle not found: $BUNDLE" >&2
    exit 1
fi

is_trusted_admin() {
    local candidate="$1"
    local component uid mode permissions

    [ -f "$candidate" ] && [ ! -L "$candidate" ] && [ -x "$candidate" ] || return 1
    component="$candidate"
    while true; do
        uid="$(/usr/bin/stat -c '%u' -- "$component")" || return 1
        mode="$(/usr/bin/stat -c '%a' -- "$component")" || return 1
        [ "$uid" -eq 0 ] || return 1
        permissions=$((8#$mode))
        (( (permissions & 022) == 0 )) || return 1
        [ "$component" = / ] && break
        component="$(/usr/bin/dirname -- "$component")" || return 1
    done
}

ADMIN=""
for candidate in /usr/sbin/reticulumpi-admin /usr/bin/reticulumpi-admin; do
    if is_trusted_admin "$candidate"; then
        ADMIN="$candidate"
        break
    fi
done
if [ -z "$ADMIN" ]; then
    echo "Error: no trusted system reticulumpi-admin is installed." >&2
    echo "Install the signed ReticulumPi recovery administrator package first;" >&2
    echo "this launcher never executes administrator code from the release bundle." >&2
    exit 1
fi

if [ "$MODE" = --apply ] && [ "$(id -u)" -ne 0 ]; then
    echo "Error: --apply must run as root. Use sudo or run --dry-run." >&2
    exit 1
fi
exec "$ADMIN" upgrade --bundle "$BUNDLE" --install-root "$INSTALL_ROOT" "$MODE"
