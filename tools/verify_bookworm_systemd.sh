#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=tools/systemd_fixture_health.sh
source "$(dirname "${BASH_SOURCE[0]}")/systemd_fixture_health.sh"

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

wait_for_systemd_running "Bookworm" 60

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

# Prove the independently installed recovery administrator can load its
# database planner before any candidate release or candidate virtualenv
# exists.  A same-named package in both cwd and PYTHONPATH must be ignored by
# the isolated /usr/sbin launcher, and both read-only commands must leave the
# trusted configuration and all persistent roots untouched.
recovery_gate=$fixture/hostile-recovery-imports
install -d -o root -g root -m 0700 "$recovery_gate/reticulumpi"
install -o root -g root -m 0600 /dev/stdin "$recovery_gate/reticulumpi/__init__.py" <<'PY'
raise SystemExit("hostile cwd/PYTHONPATH ReticulumPi package was imported")
PY
getent group reticulumpi >/dev/null || groupadd --system reticulumpi
id -u reticulumpi >/dev/null 2>&1 || useradd --system --create-home \
    --home-dir /home/reticulumpi --shell /usr/sbin/nologin --gid reticulumpi reticulumpi
install -d -o root -g root -m 0750 /etc/reticulumpi
install -o root -g root -m 0644 /dev/stdin /etc/reticulumpi/config.yaml <<'EOF'
reticulumpi:
  plugins:
    messaging_hub:
      enabled: true
      db_path: /var/lib/reticulumpi/recovery-admin-gate/messaging_hub.db
EOF
recovery_db_parent=/var/lib/reticulumpi/recovery-admin-gate
recovery_db=$recovery_db_parent/messaging_hub.db
recovery_sentinel=$recovery_db_parent/preserve.txt
install -d -o reticulumpi -g reticulumpi -m 0750 \
    /var/lib/reticulumpi "$recovery_db_parent"
runuser -u reticulumpi -- python - "$recovery_db" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as connection:
    connection.execute(
        """CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            transport TEXT NOT NULL,
            direction TEXT NOT NULL,
            msg_type TEXT NOT NULL,
            from_id TEXT,
            from_name TEXT,
            to_id TEXT,
            to_name TEXT,
            text TEXT NOT NULL,
            status TEXT DEFAULT 'sent',
            metadata TEXT
        )"""
    )
    connection.execute(
        """INSERT INTO messages(
            timestamp, transport, direction, msg_type, from_id, from_name,
            to_id, to_name, text, status, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (1234.5, "lxmf", "in", "message", "source", "Source", "dest", "Dest",
         "recovery gate row", "received", '{"fixture":true}'),
    )
    connection.execute("PRAGMA user_version = 0")
PY
chmod 0600 "$recovery_db"
install -o reticulumpi -g reticulumpi -m 0600 /dev/stdin "$recovery_sentinel" <<'EOF'
parent directory continuity marker
EOF

sqlite_gate_snapshot() {
    python - "$recovery_db" <<'PY'
import json
import sqlite3
import sys

with sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True) as connection:
    schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    rows = connection.execute(
        """SELECT id, timestamp, transport, direction, msg_type, from_id,
                  from_name, to_id, to_name, text, status, metadata
           FROM messages ORDER BY id"""
    ).fetchall()
    user_version = connection.execute("PRAGMA user_version").fetchone()[0]
print(json.dumps(
    {"rows": rows, "schema": schema, "user_version": user_version},
    separators=(",", ":"),
    sort_keys=True,
))
PY
}

recovery_config_hash=$(sha256sum /etc/reticulumpi/config.yaml | cut -d ' ' -f 1)
recovery_config_stat=$(stat -c '%U:%G %a' /etc/reticulumpi/config.yaml)
recovery_db_hash=$(sha256sum "$recovery_db" | cut -d ' ' -f 1)
recovery_sentinel_hash=$(sha256sum "$recovery_sentinel" | cut -d ' ' -f 1)
recovery_db_snapshot=$(sqlite_gate_snapshot)
recovery_db_stat=$(stat -c '%U:%G %a' "$recovery_db")
recovery_parent_stat=$(stat -c '%U:%G %a' "$recovery_db_parent")
recovery_parent_entries=$(find "$recovery_db_parent" -mindepth 1 -maxdepth 1 \
    -printf '%f %y %i %U:%G %m\n' | LC_ALL=C sort)
test "$recovery_db_stat" = "reticulumpi:reticulumpi 600"
test "$recovery_parent_stat" = "reticulumpi:reticulumpi 750"
test "$(python - "$recovery_db" <<'PY'
import sqlite3
import sys
with sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True) as connection:
    print(connection.execute("PRAGMA user_version").fetchone()[0])
PY
)" = 0
for sidecar in -journal -wal -shm; do
    test ! -e "$recovery_db$sidecar"
done
test ! -e "$install_root/current"
test ! -e "$install_root/releases"
test ! -e /var/backups/reticulumpi
recovery_plan_output=$(
    cd "$recovery_gate"
    PYTHONPATH="$recovery_gate" /usr/sbin/reticulumpi-admin db plan
)
recovery_dry_run_output=$(
    cd "$recovery_gate"
    PYTHONPATH="$recovery_gate" /usr/sbin/reticulumpi-admin db migrate --dry-run
)
messaging_hub_checksum=12b2e1c83e074d185961b16e54a658c4b3da4e060fe8c316527b42ced6c0a8f8
test "$recovery_plan_output" = \
    "messaging_hub: path=$recovery_db current=0 target=1 pending=1 checksums=$messaging_hub_checksum"
test "$recovery_dry_run_output" = \
    "messaging_hub: path=$recovery_db from=0 to=1 dry_run=true pending=1 checksums=$messaging_hub_checksum
Dry run only; stop the service and rerun with --apply to migrate."
printf '%s\n' "$recovery_plan_output" "$recovery_dry_run_output"
test "$(sha256sum /etc/reticulumpi/config.yaml | cut -d ' ' -f 1)" = \
    "$recovery_config_hash"
test "$(stat -c '%U:%G %a' /etc/reticulumpi/config.yaml)" = "$recovery_config_stat"
test "$(sha256sum "$recovery_db" | cut -d ' ' -f 1)" = "$recovery_db_hash"
test "$(sha256sum "$recovery_sentinel" | cut -d ' ' -f 1)" = \
    "$recovery_sentinel_hash"
test "$(sqlite_gate_snapshot)" = "$recovery_db_snapshot"
test "$(stat -c '%U:%G %a' "$recovery_db")" = "$recovery_db_stat"
test "$(stat -c '%U:%G %a' "$recovery_db_parent")" = "$recovery_parent_stat"
test "$(find "$recovery_db_parent" -mindepth 1 -maxdepth 1 \
    -printf '%f %y %i %U:%G %m\n' | LC_ALL=C sort)" = "$recovery_parent_entries"
for sidecar in -journal -wal -shm; do
    test ! -e "$recovery_db$sidecar"
done
test ! -e "$install_root/current"
test ! -e "$install_root/releases"
test ! -e /var/backups/reticulumpi
rm -f "$recovery_db" "$recovery_sentinel"
rmdir "$recovery_db_parent" /var/lib/reticulumpi
rm -f /etc/reticulumpi/config.yaml
rmdir /etc/reticulumpi

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
