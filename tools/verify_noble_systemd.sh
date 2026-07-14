#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=tools/systemd_fixture_health.sh
source "$(dirname "${BASH_SOURCE[0]}")/systemd_fixture_health.sh"

if [[ $(id -u) -ne 0 ]]; then
    echo "Noble systemd integration must run as root" >&2
    exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release
if [[ ${VERSION_CODENAME:-} != noble || ${VERSION_ID:-} != 24.04 || ${ID:-} != ubuntu ]]; then
    echo "expected Ubuntu Noble 24.04, found ${PRETTY_NAME:-unknown}" >&2
    exit 1
fi
if [[ $(uname -m) != aarch64 ]]; then
    echo "Noble integration requires a real ARM64 userspace" >&2
    exit 1
fi
if [[ $(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")') != 3.12 ]]; then
    echo "Noble integration requires Python 3.12" >&2
    exit 1
fi
if [[ $(ps -p 1 -o comm= | tr -d '[:space:]') != systemd ]]; then
    echo "systemd is not PID 1 in the Noble integration fixture" >&2
    exit 1
fi
test "$(dpkg-query -W -f='${X-ReticulumPi-Platform-Profile}' reticulumpi-admin)" = \
    linux-arm64-ubuntu-noble-py312
test "$(stat -c '%U:%G %a' /usr/sbin/reticulumpi-admin)" = "root:root 755"
/usr/sbin/reticulumpi-admin --help >/dev/null

wait_for_systemd_running "Noble" 60

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
scenario=${RETICULUMPI_CI_SCENARIO:-fresh}
case "$scenario" in
    fresh|legacy-bridge) ;;
    *) echo "unsupported Noble fixture scenario: $scenario" >&2; exit 1 ;;
esac
if [[ $scenario = legacy-bridge && $install_root != /srv/reticulumpi ]]; then
    echo "the legacy bridge fixture must install the immutable release under /srv" >&2
    exit 1
fi

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

if [[ $scenario = legacy-bridge ]]; then
    production_features=(
        adsb
        captive-portal
        chrony-control
        dashboard
        gps
        lora
        meshcore
        meshtastic
        nomadnet
        offline-tools
        sensors
        shared-rnsd
        space
        watchdog
    )
    feature_args=()
    for feature in "${production_features[@]}"; do
        feature_args+=(--feature "$feature")
    done

    # Reproduce the production-shaped predecessor: mutable code, venv, and
    # MeshChat storage under /opt; service-home state under /home; active rnsd
    # and watchdog units; legacy sudoers; and unrelated dependent daemons that
    # must not be restarted by the bridge transaction.
    getent group reticulumpi >/dev/null || groupadd --system reticulumpi
    id -u reticulumpi >/dev/null 2>&1 || useradd --system --create-home \
        --home-dir /home/reticulumpi --shell /usr/sbin/nologin --gid reticulumpi reticulumpi

    # Model the five externally provisioned production artifact categories
    # without cloning mutable upstream code or opening SDR hardware. MeshChat
    # is a minimal signal-aware process fixture; each native radio command is
    # a fail-fast executable that must never be invoked by this qualification.
    external_root=/srv/reticulumpi-external
    meshchat_install=$external_root/meshchat
    artifact_bin=$external_root/bin
    install -d -o root -g root -m 0755 \
        "$external_root" "$meshchat_install" "$meshchat_install/.venv" \
        "$meshchat_install/.venv/bin" "$artifact_bin"
    install -o root -g root -m 0555 /dev/stdin "$meshchat_install/meshchat.py" <<'PY'
import signal
import sys
import time
from pathlib import Path


def main():
    storage = Path(sys.argv[sys.argv.index("--storage-dir") + 1])
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "stub-ready").write_text("packaged launcher reached MeshChat stub\n")
    signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_args: sys.exit(0))
    while True:
        time.sleep(60)
PY
    install -o root -g root -m 0555 /dev/stdin \
        "$meshchat_install/.venv/bin/python" <<'EOF'
#!/bin/sh
exec /opt/reticulumpi-ci-venv/bin/python "$@"
EOF
    for command in rtl_test dump1090 rtl_fm rtl_power; do
        install -o root -g root -m 0555 /dev/stdin "$artifact_bin/$command" <<'EOF'
#!/bin/sh
echo "Noble external-artifact fixture refuses hardware access" >&2
exit 64
EOF
    done

    meshchat_digest=$(/usr/sbin/reticulumpi-admin external-artifact digest \
        --kind tree "$meshchat_install")
    rtl_test_digest=$(/usr/sbin/reticulumpi-admin external-artifact digest \
        --kind file "$artifact_bin/rtl_test")
    dump1090_digest=$(/usr/sbin/reticulumpi-admin external-artifact digest \
        --kind file "$artifact_bin/dump1090")
    rtl_fm_digest=$(/usr/sbin/reticulumpi-admin external-artifact digest \
        --kind file "$artifact_bin/rtl_fm")
    rtl_power_digest=$(/usr/sbin/reticulumpi-admin external-artifact digest \
        --kind file "$artifact_bin/rtl_power")
    install -d -o root -g reticulumpi -m 0750 /etc/reticulumpi
    install -o root -g reticulumpi -m 0640 /dev/stdin \
        /etc/reticulumpi/external-artifacts.yaml <<EOF
schema: 1
artifacts:
  meshchat:
    kind: tree
    version: noble-ci-meshchat-stub-1
    path: $meshchat_install
    sha256: $meshchat_digest
  rtl_test:
    kind: file
    version: noble-ci-rtl-sdr-stub-1
    path: $artifact_bin/rtl_test
    sha256: $rtl_test_digest
  dump1090:
    kind: file
    version: noble-ci-dump1090-stub-1
    path: $artifact_bin/dump1090
    sha256: $dump1090_digest
  rtl_fm:
    kind: file
    version: noble-ci-rtl-sdr-stub-1
    path: $artifact_bin/rtl_fm
    sha256: $rtl_fm_digest
  rtl_power:
    kind: file
    version: noble-ci-rtl-sdr-stub-1
    path: $artifact_bin/rtl_power
    sha256: $rtl_power_digest
EOF

    test "$(stat -c '%U:%G %a' "$external_root")" = "root:root 755"
    test "$(stat -c '%U:%G %a' "$meshchat_install")" = "root:root 755"
    test "$(stat -c '%U:%G %a' "$meshchat_install/meshchat.py")" = "root:root 555"
    test "$(stat -c '%U:%G %a' "$artifact_bin/rtl_test")" = "root:root 555"
    test "$(stat -c '%U:%G %a' /etc/reticulumpi/external-artifacts.yaml)" = \
        "root:reticulumpi 640"

    install -d -o reticulumpi -g reticulumpi -m 0750 \
        /home/reticulumpi/.reticulum \
        /home/reticulumpi/.config/reticulumpi \
        /home/reticulumpi/.local/share/reticulumpi \
        /home/reticulumpi/.nomadnet \
        /home/reticulumpi/.nomadnet-tui
    install -d -o reticulumpi -g reticulumpi -m 0775 \
        /opt/reticulumpi /opt/reticulumpi/.venv /opt/reticulumpi/.venv/bin \
        /opt/reticulumpi/meshchat /opt/reticulumpi/meshchat/.venv \
        /opt/reticulumpi/meshchat/.venv/bin /opt/reticulumpi/meshchat/storage

    # This mutable predecessor is deliberately distinct from the independently
    # staged /srv tree.  The bridge may move its storage, but must leave this
    # code and virtualenv byte-for-byte and metadata-for-metadata intact so the
    # exact legacy service can be restored.
    install -o reticulumpi -g reticulumpi -m 0644 /dev/stdin \
        /opt/reticulumpi/meshchat/meshchat.py <<'PY'
raise SystemExit("legacy mutable MeshChat fixture must remain inactive")
PY
    install -o reticulumpi -g reticulumpi -m 0755 /dev/stdin \
        /opt/reticulumpi/meshchat/.venv/bin/python <<'EOF'
#!/bin/sh
# Legacy mutable MeshChat interpreter; retained only for exact rollback.
exec /opt/reticulumpi-ci-venv/bin/python "$@"
EOF

    install -o reticulumpi -g reticulumpi -m 0600 /dev/stdin \
        /home/reticulumpi/.reticulum/config <<'EOF'
[reticulum]
  enable_transport = Yes

[interfaces]
EOF
    runuser -u reticulumpi -- python - <<'PY'
from pathlib import Path

import RNS

identity = RNS.Identity()
identity.to_file(Path("/home/reticulumpi/.config/reticulumpi/identity"))
PY
    install -o reticulumpi -g reticulumpi -m 0600 /dev/stdin \
        /home/reticulumpi/.local/share/reticulumpi/legacy-state.txt <<'EOF'
legacy durable application state
EOF
    install -o reticulumpi -g reticulumpi -m 0600 /dev/stdin \
        /home/reticulumpi/.nomadnet/config <<'EOF'
[node]
  enable_node = Yes
EOF
    install -o reticulumpi -g reticulumpi -m 0600 /dev/stdin \
        /home/reticulumpi/.nomadnet-tui/config <<'EOF'
[textui]
  compact_mode = No
EOF
    install -o reticulumpi -g reticulumpi -m 0600 /dev/stdin \
        /opt/reticulumpi/meshchat/storage/continuity.txt <<'EOF'
legacy MeshChat storage continuity marker
EOF

    install -o reticulumpi -g reticulumpi -m 0755 /dev/stdin \
        /opt/reticulumpi/.venv/bin/reticulumpi <<'EOF'
#!/bin/sh
trap 'exit 0' TERM INT
while :; do
    sleep 3600 &
    wait "$!"
done
EOF
    install -o reticulumpi -g reticulumpi -m 0755 /dev/stdin \
        /opt/reticulumpi/.venv/bin/rnsd <<'EOF'
#!/bin/sh
trap 'exit 0' TERM INT
while :; do
    sleep 3600 &
    wait "$!"
done
EOF

    install -d -o root -g reticulumpi -m 0750 /etc/reticulumpi
    install -o root -g reticulumpi -m 0640 /dev/stdin /etc/reticulumpi/config.yaml <<'EOF'
reticulumpi:
  node_name: NobleLegacyBridge
  reticulum_config_dir: /home/reticulumpi/.reticulum
  use_shared_instance: true
  identity_path: /home/reticulumpi/.config/reticulumpi/identity
  log_level: 4
  plugin_paths: []
  plugins:
    heartbeat_announce:
      enabled: true
      interval_seconds: 3600
    web_dashboard:
      enabled: true
      host: 127.0.0.1
      port: 18080
      secret_dir: /home/reticulumpi/.config/reticulumpi
    meshchat_server:
      enabled: false
      install_dir: /opt/reticulumpi/meshchat
      storage_dir: /opt/reticulumpi/meshchat/storage
EOF

    install -o root -g root -m 0644 /dev/stdin \
        /etc/systemd/system/reticulumpi.service <<'EOF'
[Unit]
Description=Mutable legacy ReticulumPi fixture
After=network.target

[Service]
Type=simple
User=reticulumpi
Group=reticulumpi
Environment=HOME=/home/reticulumpi
WorkingDirectory=/opt/reticulumpi
ExecStart=/opt/reticulumpi/.venv/bin/reticulumpi --config /etc/reticulumpi/config.yaml
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
    install -o root -g root -m 0644 /dev/stdin \
        /etc/systemd/system/rnsd.service <<'EOF'
[Unit]
Description=Mutable legacy rnsd fixture
After=network.target

[Service]
Type=simple
User=reticulumpi
Group=reticulumpi
Environment=HOME=/home/reticulumpi
WorkingDirectory=/opt/reticulumpi
ExecStart=/opt/reticulumpi/.venv/bin/rnsd
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
    install -o root -g root -m 0644 /dev/stdin \
        /etc/systemd/system/rnsd-watchdog.service <<'EOF'
[Unit]
Description=Mutable legacy rnsd watchdog fixture

[Service]
Type=oneshot
ExecStart=/bin/true
EOF
    install -o root -g root -m 0644 /dev/stdin \
        /etc/systemd/system/rnsd-watchdog.timer <<'EOF'
[Unit]
Description=Mutable legacy rnsd watchdog timer fixture

[Timer]
OnBootSec=1h
OnUnitActiveSec=1h

[Install]
WantedBy=timers.target
EOF

    dependent_services=(gpsd.service chrony.service i2pd.service yggdrasil.service)
    for service in "${dependent_services[@]}"; do
        install -o root -g root -m 0644 /dev/stdin "/etc/systemd/system/$service" <<EOF
[Unit]
Description=Unrelated production dependency fixture for $service

[Service]
Type=simple
ExecStart=/bin/sleep infinity

[Install]
WantedBy=multi-user.target
EOF
    done
    install -d -o root -g root -m 0750 /etc/sudoers.d
    for policy in reticulumpi-services reticulumpi-captive-portal reticulumpi-chrony; do
        install -o root -g root -m 0440 /dev/stdin "/etc/sudoers.d/$policy" <<'EOF'
reticulumpi ALL=(root) NOPASSWD: /bin/true
EOF
    done
    install -o root -g root -m 0440 /dev/stdin \
        /etc/sudoers.d/reticulumpi-offline <<'EOF'
# Allow reticulumpi user to run offline simulation script
reticulumpi ALL=(ALL) NOPASSWD: /opt/reticulumpi/scripts/simulate_offline.sh
EOF

    # Production has this stale policy but not its historical target.  The
    # bridge must replace it with reviewed, root-owned candidate assets without
    # ever executing either helper during qualification.
    test ! -e /opt/reticulumpi/scripts/simulate_offline.sh
    test ! -e /usr/libexec/reticulumpi/simulate_offline.sh
    test ! -e /usr/share/reticulumpi/config/offline_profile.yaml

    systemctl daemon-reload
    systemctl enable --now "${dependent_services[@]}"
    systemctl enable --now rnsd.service reticulumpi.service rnsd-watchdog.timer
    for service in "${dependent_services[@]}"; do
        systemctl is-active --quiet "$service"
        systemctl is-enabled --quiet "$service"
    done

    identity=/home/reticulumpi/.config/reticulumpi/identity
    meshchat_state=/opt/reticulumpi/meshchat/storage/continuity.txt
    legacy_meshchat_code=/opt/reticulumpi/meshchat/meshchat.py
    legacy_meshchat_python=/opt/reticulumpi/meshchat/.venv/bin/python
    identity_hash=$(sha256sum "$identity" | cut -d ' ' -f 1)
    meshchat_hash=$(sha256sum "$meshchat_state" | cut -d ' ' -f 1)
    legacy_meshchat_code_hash=$(sha256sum "$legacy_meshchat_code" | cut -d ' ' -f 1)
    legacy_meshchat_python_hash=$(sha256sum "$legacy_meshchat_python" | cut -d ' ' -f 1)
    legacy_meshchat_code_stat=$(stat -c '%U:%G %a' "$legacy_meshchat_code")
    legacy_meshchat_python_stat=$(stat -c '%U:%G %a' "$legacy_meshchat_python")
    legacy_meshchat_tree_stat=$(stat -c '%U:%G %a' /opt/reticulumpi/meshchat)
    legacy_meshchat_venv_stat=$(stat -c '%U:%G %a' /opt/reticulumpi/meshchat/.venv)
    config_hash=$(sha256sum /etc/reticulumpi/config.yaml | cut -d ' ' -f 1)
    legacy_app_unit_hash=$(sha256sum /etc/systemd/system/reticulumpi.service | cut -d ' ' -f 1)
    legacy_rnsd_unit_hash=$(sha256sum /etc/systemd/system/rnsd.service | cut -d ' ' -f 1)
    legacy_watchdog_hash=$(sha256sum /etc/systemd/system/rnsd-watchdog.timer | cut -d ' ' -f 1)
    legacy_sudoers_hash=$(sha256sum /etc/sudoers.d/reticulumpi-chrony | cut -d ' ' -f 1)
    legacy_offline_sudoers_hash=$(sha256sum /etc/sudoers.d/reticulumpi-offline | cut -d ' ' -f 1)
    legacy_offline_sudoers_stat=$(stat -c '%U:%G %a' /etc/sudoers.d/reticulumpi-offline)
    mutable_app_hash=$(sha256sum /opt/reticulumpi/.venv/bin/reticulumpi | cut -d ' ' -f 1)
    mutable_rnsd_hash=$(sha256sum /opt/reticulumpi/.venv/bin/rnsd | cut -d ' ' -f 1)
    declare -A dependent_pids=()
    for service in "${dependent_services[@]}"; do
        dependent_pids[$service]=$(systemctl show "$service" -p MainPID --value)
        test "${dependent_pids[$service]}" -gt 1
    done

    # Exercise the real production bridge as a visible, non-mutating plan.  A
    # single transaction must report both scalar rewrites before it constructs
    # a candidate virtualenv or changes any legacy state.
    bridge_plan_output=$(/usr/sbin/reticulumpi-admin upgrade \
        --bundle "$fixture/good-0.3.0" --install-root "$install_root" \
        "${feature_args[@]}" --dry-run)
    printf '%s\n' "$bridge_plan_output"
    BRIDGE_PLAN_OUTPUT="$bridge_plan_output" python - <<'PY'
import json
import os

text = os.environ["BRIDGE_PLAN_OUTPUT"]
suffix = "\nDry run only; rerun with --apply to change the system."
if not text.endswith(suffix):
    raise SystemExit("legacy bridge dry-run marker is missing")
summary = json.loads(text[: -len(suffix)])
assert summary["legacy_bridge"] is True, summary
migrations = {
    (item["setting"], item.get("value"))
    for item in summary["configuration_migrations"]
}
assert {
    (
        "plugins.meshchat_server.install_dir",
        "/srv/reticulumpi-external/meshchat",
    ),
    (
        "plugins.meshchat_server.storage_dir",
        "/var/lib/reticulumpi/meshchat/storage",
    ),
} <= migrations, migrations
PY
    test ! -e "$install_root/current"
    test ! -e "$install_root/releases/0.3.0"
    test ! -e /var/lib/reticulumpi/meshchat/storage
    test "$(sha256sum /etc/reticulumpi/config.yaml | cut -d ' ' -f 1)" = "$config_hash"
    test "$(sha256sum "$meshchat_state" | cut -d ' ' -f 1)" = "$meshchat_hash"
    test "$(sha256sum "$legacy_meshchat_code" | cut -d ' ' -f 1)" = \
        "$legacy_meshchat_code_hash"
    test "$(sha256sum "$legacy_meshchat_python" | cut -d ' ' -f 1)" = \
        "$legacy_meshchat_python_hash"
    test "$(sha256sum /etc/sudoers.d/reticulumpi-offline | cut -d ' ' -f 1)" = \
        "$legacy_offline_sudoers_hash"
    test "$(stat -c '%U:%G %a' /etc/sudoers.d/reticulumpi-offline)" = \
        "$legacy_offline_sudoers_stat"
    test ! -e /opt/reticulumpi/scripts/simulate_offline.sh
    test ! -e /usr/libexec/reticulumpi/simulate_offline.sh
    test ! -e /usr/share/reticulumpi/config/offline_profile.yaml
    test "$(/usr/sbin/reticulumpi-admin external-artifact digest \
        --kind tree "$meshchat_install")" = "$meshchat_digest"

    /usr/sbin/reticulumpi-admin upgrade --bundle "$fixture/good-0.3.0" \
        --install-root "$install_root" "${feature_args[@]}" --apply --start
    systemctl is-active --quiet reticulumpi.service
    systemctl is-active --quiet rnsd.service
    systemctl is-active --quiet rnsd-watchdog.timer
    test "$(readlink -f "$install_root/current")" = "$install_root/releases/0.3.0"
    test ! -e /home/reticulumpi/.config/reticulumpi/identity
    test ! -e /opt/reticulumpi/meshchat/storage
    test "$(sha256sum /opt/reticulumpi/.venv/bin/reticulumpi | cut -d ' ' -f 1)" = "$mutable_app_hash"
    test "$(sha256sum /opt/reticulumpi/.venv/bin/rnsd | cut -d ' ' -f 1)" = "$mutable_rnsd_hash"
    test "$(sha256sum "$legacy_meshchat_code" | cut -d ' ' -f 1)" = \
        "$legacy_meshchat_code_hash"
    test "$(sha256sum "$legacy_meshchat_python" | cut -d ' ' -f 1)" = \
        "$legacy_meshchat_python_hash"
    test "$(stat -c '%U:%G %a' "$legacy_meshchat_code")" = "$legacy_meshchat_code_stat"
    test "$(stat -c '%U:%G %a' "$legacy_meshchat_python")" = \
        "$legacy_meshchat_python_stat"
    test "$(stat -c '%U:%G %a' /opt/reticulumpi/meshchat)" = \
        "$legacy_meshchat_tree_stat"
    test "$(stat -c '%U:%G %a' /opt/reticulumpi/meshchat/.venv)" = \
        "$legacy_meshchat_venv_stat"
    test ! -e /etc/sudoers.d/reticulumpi-offline
    test ! -e /opt/reticulumpi/scripts/simulate_offline.sh
    test "$(stat -c '%U:%G %a' /usr/libexec/reticulumpi/simulate_offline.sh)" = \
        "root:root 755"
    test "$(stat -c '%U:%G %a' /usr/share/reticulumpi/config/offline_profile.yaml)" = \
        "root:root 644"
    bash -n /usr/libexec/reticulumpi/simulate_offline.sh
    "$install_root/current/.venv/bin/python" - <<'PY'
from pathlib import Path

import yaml

profile = yaml.safe_load(
    Path("/usr/share/reticulumpi/config/offline_profile.yaml").read_text(
        encoding="utf-8"
    )
)
assert isinstance(profile, dict), profile
PY
    test "$(sha256sum /var/lib/reticulumpi/.config/reticulumpi/identity | cut -d ' ' -f 1)" = "$identity_hash"
    test "$(sha256sum /var/lib/reticulumpi/meshchat/storage/continuity.txt | cut -d ' ' -f 1)" = "$meshchat_hash"
    python - "${production_features[@]}" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path("/etc/reticulumpi/install.json").read_text())
expected_features = sorted(sys.argv[1:])
assert manifest["features"] == expected_features, manifest["features"]
profile = manifest["platform_profile"]
assert profile["profile_key"] == "linux-arm64-ubuntu-noble-py312", profile
assert profile["architecture"] == "arm64", profile
assert profile["distribution"] == "ubuntu", profile
assert profile["codename"] == "noble", profile
assert profile["version_id"] == "24.04", profile
assert profile["python_series"] == "3.12", profile
assert profile["dependency_lock_scope"] == "shared-universal", profile
assert profile["dependency_profiles"]["all-features"] == (
    "production-universal-all-features.txt"
), profile
config = Path("/etc/reticulumpi/config.yaml").read_text()
assert "storage_dir: /var/lib/reticulumpi/meshchat/storage" in config
assert "install_dir: /srv/reticulumpi-external/meshchat" in config
PY
    "$install_root/current/.venv/bin/python" - <<'PY'
from importlib.metadata import version

for distribution in (
    "RPi.GPIO",
    "meshtastic",
    "meshcore",
    "pyModeS",
    "pynmea2",
    "pyserial",
    "sgp4",
    "smbus2",
):
    version(distribution)
PY
    for service in "${dependent_services[@]}"; do
        systemctl is-active --quiet "$service"
        systemctl is-enabled --quiet "$service"
        test "$(systemctl show "$service" -p MainPID --value)" = "${dependent_pids[$service]}"
    done

    # Enable only the MeshChat stub for a behavioral packaged-launcher check.
    # The production policy must accept the immutable tree while the process
    # writes exclusively to the migrated /var/lib storage directory.
    python - <<'PY'
from pathlib import Path

path = Path("/etc/reticulumpi/config.yaml")
config = path.read_text()
old = "    meshchat_server:\n      enabled: false\n"
new = "    meshchat_server:\n      enabled: true\n"
if config.count(old) != 1:
    raise SystemExit("could not enable the exact MeshChat fixture block")
path.write_text(config.replace(old, new, 1))
PY
    systemctl restart reticulumpi.service
    started=$SECONDS
    until test -f /var/lib/reticulumpi/meshchat/storage/stub-ready; do
        systemctl is-active --quiet reticulumpi.service
        if (( SECONDS - started >= 30 )); then
            echo "packaged MeshChat launcher did not reach the safe stub" >&2
            journalctl -u reticulumpi.service --no-pager -n 100 >&2 || true
            exit 1
        fi
        sleep 1
    done
    pgrep -u reticulumpi -f 'meshchat_launcher.pydata.*--storage-dir.*/var/lib/reticulumpi/meshchat/storage' \
        >/dev/null
    test "$(/usr/sbin/reticulumpi-admin external-artifact digest \
        --kind tree "$meshchat_install")" = "$meshchat_digest"
    test -z "$(find "$meshchat_install" -name __pycache__ -print -quit)"

    # Expand the same canonical config to the full five-category contract and
    # run the packaged --check path. The radio executables are hashed but never
    # executed; PATH is scoped to this one preflight process.
    python - <<'PY'
from pathlib import Path

path = Path("/etc/reticulumpi/config.yaml")
with path.open("a", encoding="utf-8") as stream:
    stream.write(
        "    adsb_radar:\n"
        "      enabled: true\n"
        "      dump1090_bin: /srv/reticulumpi-external/bin/dump1090\n"
        "      enable_bias_tee: false\n"
        "    spectrum_scanner:\n"
        "      enabled: true\n"
        "      power_command: /srv/reticulumpi-external/bin/rtl_power\n"
        "    fm_receiver:\n"
        "      enabled: true\n"
    )
PY
    PATH="$artifact_bin:$PATH" "$install_root/current/.venv/bin/python" - <<'PY'
from pathlib import Path

from reticulumpi.config import AppConfig
from reticulumpi.external_artifacts import load_manifest

manifest = load_manifest("/etc/reticulumpi/external-artifacts.yaml")
assert {record.name for record in manifest} == {
    "meshchat",
    "rtl_test",
    "dump1090",
    "rtl_fm",
    "rtl_power",
}
assert {record.kind for record in manifest if record.name == "meshchat"} == {"tree"}
assert {record.kind for record in manifest if record.name != "meshchat"} == {"file"}
config = AppConfig("/etc/reticulumpi/config.yaml")
assert config.external_artifact_policy.required is True
assert Path(config.plugins["meshchat_server"]["install_dir"]) == Path(
    "/srv/reticulumpi-external/meshchat"
)
assert Path(config.plugins["meshchat_server"]["storage_dir"]) == Path(
    "/var/lib/reticulumpi/meshchat/storage"
)
PY
    runuser -u reticulumpi -- env PATH="$artifact_bin:$PATH" \
        "$install_root/current/.venv/bin/reticulumpi" \
        --config /etc/reticulumpi/config.yaml --check >/run/reticulumpi-config-check.txt
    grep -q 'Config validation: OK' /run/reticulumpi-config-check.txt
    grep -q 'meshchat_server: OK' /run/reticulumpi-config-check.txt
    grep -q 'adsb_radar: OK' /run/reticulumpi-config-check.txt
    grep -q 'spectrum_scanner: OK' /run/reticulumpi-config-check.txt
    grep -q 'fm_receiver: OK' /run/reticulumpi-config-check.txt

    bridge_backup=$(python -c 'import json; print(json.load(open("/etc/reticulumpi/install.json"))["legacy_bridge_backup"])')
    test -d "$bridge_backup"
    /usr/sbin/reticulumpi-admin rollback --to legacy --apply

    test ! -e "$install_root/current"
    test ! -e /etc/reticulumpi/install.json
    test -d "$install_root/releases/0.3.0"
    test -d "$bridge_backup"
    systemctl is-active --quiet reticulumpi.service
    systemctl is-enabled --quiet reticulumpi.service
    systemctl is-active --quiet rnsd.service
    systemctl is-enabled --quiet rnsd.service
    systemctl is-active --quiet rnsd-watchdog.timer
    systemctl is-enabled --quiet rnsd-watchdog.timer
    test "$(sha256sum /home/reticulumpi/.config/reticulumpi/identity | cut -d ' ' -f 1)" = "$identity_hash"
    test "$(sha256sum /opt/reticulumpi/meshchat/storage/continuity.txt | cut -d ' ' -f 1)" = "$meshchat_hash"
    test ! -e /var/lib/reticulumpi/.config/reticulumpi/identity
    test ! -e /var/lib/reticulumpi/meshchat/storage/continuity.txt
    test "$(sha256sum /etc/reticulumpi/config.yaml | cut -d ' ' -f 1)" = "$config_hash"
    test "$(sha256sum /etc/systemd/system/reticulumpi.service | cut -d ' ' -f 1)" = "$legacy_app_unit_hash"
    test "$(sha256sum /etc/systemd/system/rnsd.service | cut -d ' ' -f 1)" = "$legacy_rnsd_unit_hash"
    test "$(sha256sum /etc/systemd/system/rnsd-watchdog.timer | cut -d ' ' -f 1)" = "$legacy_watchdog_hash"
    test "$(sha256sum /etc/sudoers.d/reticulumpi-chrony | cut -d ' ' -f 1)" = "$legacy_sudoers_hash"
    test "$(sha256sum /etc/sudoers.d/reticulumpi-offline | cut -d ' ' -f 1)" = \
        "$legacy_offline_sudoers_hash"
    test "$(stat -c '%U:%G %a' /etc/sudoers.d/reticulumpi-offline)" = \
        "$legacy_offline_sudoers_stat"
    test ! -e /opt/reticulumpi/scripts/simulate_offline.sh
    test ! -e /usr/libexec/reticulumpi/simulate_offline.sh
    test ! -e /usr/share/reticulumpi/config/offline_profile.yaml
    test "$(sha256sum /opt/reticulumpi/.venv/bin/reticulumpi | cut -d ' ' -f 1)" = "$mutable_app_hash"
    test "$(sha256sum /opt/reticulumpi/.venv/bin/rnsd | cut -d ' ' -f 1)" = "$mutable_rnsd_hash"
    test "$(sha256sum "$legacy_meshchat_code" | cut -d ' ' -f 1)" = \
        "$legacy_meshchat_code_hash"
    test "$(sha256sum "$legacy_meshchat_python" | cut -d ' ' -f 1)" = \
        "$legacy_meshchat_python_hash"
    test "$(stat -c '%U:%G %a' "$legacy_meshchat_code")" = "$legacy_meshchat_code_stat"
    test "$(stat -c '%U:%G %a' "$legacy_meshchat_python")" = \
        "$legacy_meshchat_python_stat"
    test "$(stat -c '%U:%G %a' /opt/reticulumpi/meshchat)" = \
        "$legacy_meshchat_tree_stat"
    test "$(stat -c '%U:%G %a' /opt/reticulumpi/meshchat/.venv)" = \
        "$legacy_meshchat_venv_stat"
    test "$(/usr/sbin/reticulumpi-admin external-artifact digest \
        --kind tree "$meshchat_install")" = "$meshchat_digest"
    if pgrep -u reticulumpi -f 'meshchat_launcher.pydata' >/dev/null; then
        echo "MeshChat fixture process survived exact legacy rollback" >&2
        exit 1
    fi
    for service in "${dependent_services[@]}"; do
        systemctl is-active --quiet "$service"
        systemctl is-enabled --quiet "$service"
        test "$(systemctl show "$service" -p MainPID --value)" = "${dependent_pids[$service]}"
    done

    systemd-analyze verify \
        /etc/systemd/system/reticulumpi.service \
        /etc/systemd/system/rnsd.service \
        /etc/systemd/system/rnsd-watchdog.service \
        /etc/systemd/system/rnsd-watchdog.timer
    pytest -n0 --strict-config --strict-markers \
        tests/test_admin_cli.py \
        tests/test_admin_cli_regressions.py \
        tests/test_platform_policy.py \
        tests/test_runtime_layout.py
    echo "Noble ARM64 production-shaped legacy bridge and exact rollback passed"
    exit 0
fi

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

pytest -n0 --strict-config --strict-markers \
    tests/test_admin_cli.py \
    tests/test_admin_cli_regressions.py \
    tests/test_platform_policy.py \
    tests/test_runtime_layout.py

echo "Noble ARM64 systemd installation, recovery, and rollback regressions passed at $install_root"
