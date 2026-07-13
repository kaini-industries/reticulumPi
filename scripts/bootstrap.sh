#!/usr/bin/env bash
set -euo pipefail

# Compatibility launcher for the transactional administrator. It deliberately
# performs no package installation, source copying, chown, sudoers mutation, or
# live-environment update itself.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BUNDLE="$PROJECT_DIR"
INSTALL_ROOT="/srv/reticulumpi"
MODE=--dry-run
START=false
FEATURES=()
NOTES=()

usage() {
    cat <<'EOF'
Usage: scripts/bootstrap.sh [OPTIONS]

Transactional options:
  --dry-run                    Show the installation plan (default)
  --apply                      Apply the plan; must run as root
  --bundle PATH                Trusted source directory or release bundle
  --install-root PATH          Immutable release root (default /srv/reticulumpi)
  --install-dir PATH           Compatibility alias for --install-root
  --feature NAME               Pass an administrator feature through directly
  --start                      Start ReticulumPi after validation

Supported compatibility features:
  --with-dashboard             dashboard
  --with-nomadnet              nomadnet + shared-rnsd
  --with-lora                  lora
  --with-captive-portal        captive-portal privileged helper
  --with-offline-tools         offline-tools operator helper
  --with-chrony-control        chrony-control privileged helper
  --with-watchdog              rnsd watchdog units

External integrations:
  --with-i2p, --with-yggdrasil, and --with-signals are accepted as migration
  reminders, but their OS packages must be installed and reviewed separately.
  MeshChat and --node-name are no longer mutated by bootstrap; configure them
  explicitly after the transactional install.

Security prerequisite:
  Install the independently signed ReticulumPi recovery administrator package.
  This launcher never executes administrator Python from the candidate bundle.
EOF
}

add_feature() {
    FEATURES+=(--feature "$1")
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run) MODE=--dry-run; shift ;;
        --apply) MODE=--apply; shift ;;
        --start) START=true; shift ;;
        --bundle) BUNDLE="${2:?--bundle requires a path}"; shift 2 ;;
        --bundle=*) BUNDLE="${1#*=}"; shift ;;
        --install-root|--install-dir)
            INSTALL_ROOT="${2:?$1 requires a path}"
            shift 2
            ;;
        --install-root=*|--install-dir=*) INSTALL_ROOT="${1#*=}"; shift ;;
        --feature) add_feature "${2:?--feature requires a name}"; shift 2 ;;
        --feature=*) add_feature "${1#*=}"; shift ;;
        --with-dashboard) add_feature dashboard; shift ;;
        --with-nomadnet)
            add_feature nomadnet
            add_feature shared-rnsd
            shift
            ;;
        --with-lora) add_feature lora; shift ;;
        --with-captive-portal) add_feature captive-portal; shift ;;
        --with-offline-tools) add_feature offline-tools; shift ;;
        --with-chrony-control) add_feature chrony-control; shift ;;
        --with-watchdog) add_feature watchdog; shift ;;
        --with-i2p)
            NOTES+=("I2P: install and configure i2pd before enabling the RNS interface")
            shift
            ;;
        --with-yggdrasil)
            NOTES+=("Yggdrasil: install and configure it before enabling the transport plugin")
            shift
            ;;
        --with-signals)
            NOTES+=("Signal tools: install pinned decoder packages using the hardware guide")
            shift
            ;;
        --with-meshchat)
            echo "Error: --with-meshchat is not transactionally supported." >&2
            echo "Install reviewed MeshChat code under /srv/reticulumpi-external and keep its storage under /var/lib/reticulumpi." >&2
            exit 2
            ;;
        --node-name|--node-name=*)
            echo "Error: --node-name no longer rewrites configuration during installation." >&2
            echo "Set reticulumpi.node_name in /etc/reticulumpi/config.yaml after install." >&2
            exit 2
            ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ "$MODE" = --apply ] && [ "$(id -u)" -ne 0 ]; then
    echo "Error: --apply must run as root. Use sudo or run --dry-run." >&2
    exit 1
fi

if [ ! -e "$BUNDLE" ]; then
    echo "Error: bundle not found: $BUNDLE" >&2
    exit 1
fi

if [ "${#NOTES[@]}" -gt 0 ]; then
    echo "External integration notes:" >&2
    for note in "${NOTES[@]}"; do
        echo "  - $note" >&2
    done
fi

COMMAND=install
if [ -f /etc/reticulumpi/install.json ] \
    || [ -f /etc/systemd/system/reticulumpi.service ] \
    || [ -f /etc/reticulumpi/config.yaml ]; then
    # A manifest-less mutable predecessor is still an upgrade.  The
    # administrator uses this distinction to retain exact legacy rollback
    # evidence and to restore the predecessor without modern readiness markers.
    COMMAND=upgrade
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

ARGS=("$COMMAND" --bundle "$BUNDLE" --install-root "$INSTALL_ROOT" "$MODE")
ARGS+=("${FEATURES[@]}")
if [ "$START" = true ]; then
    ARGS+=(--start)
fi

exec "$ADMIN" "${ARGS[@]}"
