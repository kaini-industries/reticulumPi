#!/usr/bin/env bash
set -euo pipefail

if [[ $(id -u) -ne 0 ]]; then
    echo "Bookworm systemd integration must run as root" >&2
    exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release
if [[ ${VERSION_CODENAME:-} != bookworm || ${ID:-} != debian ]]; then
    echo "expected Debian Bookworm, found ${PRETTY_NAME:-unknown}" >&2
    exit 1
fi
if [[ $(uname -m) != aarch64 ]]; then
    echo "Bookworm integration requires a real ARM64 userspace" >&2
    exit 1
fi
if [[ $(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")') != 3.11 ]]; then
    echo "Bookworm integration requires Python 3.11" >&2
    exit 1
fi
if [[ $(ps -p 1 -o comm= | tr -d '[:space:]') != systemd ]]; then
    echo "systemd is not PID 1 in the Bookworm integration fixture" >&2
    exit 1
fi
test "$(dpkg-query -W -f='${X-ReticulumPi-Platform-Profile}' reticulumpi-admin)" = \
    linux-arm64-debian-bookworm-py311
test "$(stat -c '%U:%G %a' /usr/sbin/reticulumpi-admin)" = "root:root 755"
/usr/sbin/reticulumpi-admin --help >/dev/null

started=$SECONDS
while [[ $(systemctl is-system-running 2>/dev/null || true) == starting ]]; do
    if (( SECONDS - started >= 60 )); then
        echo "systemd did not finish booting within 60 seconds" >&2
        systemctl --failed --no-pager >&2 || true
        exit 1
    fi
    sleep 1
done
state=$(systemctl is-system-running 2>/dev/null || true)
if [[ $state != running ]]; then
    echo "Bookworm systemd fixture is not healthy: $state" >&2
    systemctl --failed --no-pager >&2 || true
    exit 1
fi

probe=/etc/systemd/system/reticulumpi-ci-probe.service
cleanup() {
    systemctl stop reticulumpi-ci-probe.service >/dev/null 2>&1 || true
    rm -f "$probe" /run/reticulumpi-ci-systemd-ok
    systemctl daemon-reload >/dev/null 2>&1 || true
}
trap cleanup EXIT
install -m 0644 /dev/stdin "$probe" <<'EOF'
[Unit]
Description=ReticulumPi systemd CI execution probe

[Service]
Type=oneshot
ExecStart=/usr/bin/touch /run/reticulumpi-ci-systemd-ok
RemainAfterExit=yes
EOF
systemctl daemon-reload
systemctl start reticulumpi-ci-probe.service
systemctl is-active --quiet reticulumpi-ci-probe.service
test -f /run/reticulumpi-ci-systemd-ok

install_root=${RETICULUMPI_CI_INSTALL_ROOT:-/opt/reticulumpi}
case "$install_root" in
    /opt/reticulumpi|/srv/reticulumpi) ;;
    *) echo "unsupported fixture install root: $install_root" >&2; exit 1 ;;
esac

fixture=/run/reticulumpi-systemd-fixture
install -d -m 0700 "$fixture"
umask 077
minisign -G -W -p "$fixture/release.pub" -s "$fixture/release.key" >/dev/null
install -d -o root -g root -m 0755 /usr/share/reticulumpi
install -o root -g root -m 0644 "$fixture/release.pub" /usr/share/reticulumpi/release.pub

python tools/build_systemd_ci_bundle.py --source /workspace \
    --output "$fixture/good-0.3.0" --version 0.3.0 --signing-key "$fixture/release.key"

admin=(/usr/sbin/reticulumpi-admin install --bundle "$fixture/good-0.3.0" \
    --install-root "$install_root" --apply --start)
"${admin[@]}"
systemctl is-active --quiet reticulumpi.service
systemctl is-active --quiet reticulumpi-control.socket
test "$(readlink -f "$install_root/current")" = "$install_root/releases/0.3.0"
test "$(stat -c '%U:%G %a' /etc/reticulumpi/config.yaml)" = "root:reticulumpi 640"
identity=/var/lib/reticulumpi/.config/reticulumpi/identity
test "$(stat -c '%U %a' "$identity")" = "reticulumpi 600"
identity_hash=$(sha256sum "$identity" | cut -d ' ' -f 1)

# Reapplying the exact signed artifact is a verified no-op: it must not create
# another release, restart the service, or change the identity.
active_since=$(systemctl show reticulumpi.service -p ActiveEnterTimestampMonotonic --value)
"${admin[@]}"
test "$(systemctl show reticulumpi.service -p ActiveEnterTimestampMonotonic --value)" = "$active_since"
test "$(sha256sum "$identity" | cut -d ' ' -f 1)" = "$identity_hash"

# Inject a durable pre-backup power interruption. The next apply must restore
# the recorded systemd state, remove only the partial candidate, and then take
# the same-artifact no-op path.
partial="$install_root/releases/0.3.9-interrupted"
install -d -o root -g root -m 0755 "$partial"
python - "$install_root" "$partial" <<'PY'
import sys
from pathlib import Path
from reticulumpi import admin_cli as admin

root = Path(sys.argv[1])
candidate = Path(sys.argv[2])
admin._atomic_json(
    admin.JOURNAL_FILE,
    {
        "schema": 1,
        "operation": "upgrade",
        "install_root": str(root),
        "version": "0.3.9-interrupted",
        "features": [],
        "previous_release": str((root / "current").resolve()),
        "new_release": str(candidate),
        "remove_candidate": True,
        "backup": None,
        "services_before": admin._service_state_snapshot(),
        "state": "preparing",
    },
    0o600,
)
PY
"${admin[@]}"
test ! -e "$partial"
test "$(python -c 'import json; print(json.load(open("/var/backups/reticulumpi/admin/transaction.json"))["state"])')" = recovered
systemctl is-active --quiet reticulumpi.service
test "$(sha256sum "$identity" | cut -d ' ' -f 1)" = "$identity_hash"

if [[ $install_root = /opt/reticulumpi ]]; then
    python tools/build_systemd_ci_bundle.py --source /workspace \
        --output "$fixture/bad-0.3.2" --version 0.3.2 \
        --signing-key "$fixture/release.key" --failing-service
    if /usr/sbin/reticulumpi-admin upgrade --bundle "$fixture/bad-0.3.2" \
        --install-root "$install_root" --apply --start; then
        echo "injected candidate unexpectedly passed readiness" >&2
        exit 1
    fi
    test "$(readlink -f "$install_root/current")" = "$install_root/releases/0.3.0"
    test "$(python -c 'import json; print(json.load(open("/var/backups/reticulumpi/admin/transaction.json"))["state"])')" = rolled_back
    systemctl is-active --quiet reticulumpi.service
    test "$(sha256sum "$identity" | cut -d ' ' -f 1)" = "$identity_hash"

    python tools/build_systemd_ci_bundle.py --source /workspace \
        --output "$fixture/good-0.3.1" --version 0.3.1 --signing-key "$fixture/release.key"
    /usr/sbin/reticulumpi-admin upgrade --bundle "$fixture/good-0.3.1" \
        --install-root "$install_root" --apply --start
    test "$(readlink -f "$install_root/current")" = "$install_root/releases/0.3.1"
    /usr/sbin/reticulumpi-admin rollback --to 0.3.0 --apply
    test "$(readlink -f "$install_root/current")" = "$install_root/releases/0.3.0"
    systemctl is-active --quiet reticulumpi.service
    test "$(sha256sum "$identity" | cut -d ' ' -f 1)" = "$identity_hash"
fi

systemd-analyze verify \
    /etc/systemd/system/reticulumpi.service \
    /etc/systemd/system/reticulumpi-control.socket \
    /etc/systemd/system/reticulumpi-control@.service
"$install_root/current/.venv/bin/reticulumpi-admin" doctor

python tools/run_doc_shell_examples.py --require-bookworm

pytest -n0 --strict-config --strict-markers \
    tests/test_admin_cli.py \
    tests/test_admin_cli_regressions.py \
    tests/test_runtime_layout.py

echo "Bookworm ARM64 systemd installation, recovery, and rollback regressions passed at $install_root"
