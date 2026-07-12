# ReticulumPi 0.2.5 Security Bridge Advisory

Published: 2026-07-10
Audience: every operator upgrading from 0.2.4 or an older mutable-layout installation

## Required action

Earlier bootstrap/update layouts could leave passwordless sudo rules pointing at helpers in
a service-writable installation tree. Generated dashboard passwords may also have appeared
in historical service journals. Treat both conditions as credential exposure even when the
node is not internet-facing.

Before reconnecting an upgraded node to an untrusted LAN:

1. Record the current identity hash and take a protected backup.
2. Apply the 0.2.5 recovery bundle using the transactional administrator and verify that the
   service starts with the same identity.
3. Remove every obsolete passwordless rule:

   ```bash
   sudo rm -f \
     /etc/sudoers.d/reticulumpi-services \
     /etc/sudoers.d/reticulumpi-offline \
     /etc/sudoers.d/reticulumpi-captive-portal \
     /etc/sudoers.d/reticulumpi-chrony
   sudo visudo -c
   ```

4. After the transactional release is active, remove obsolete helper copies from the old
   service-owned layout. For the former default root:

   ```bash
   sudo rm -f \
     /opt/reticulumpi/scripts/restart_services.sh \
     /opt/reticulumpi/scripts/simulate_offline.sh \
     /opt/reticulumpi/scripts/captive_portal_helper.sh \
     /opt/reticulumpi/scripts/chrony_helper.sh
   ```

   Use the actual legacy root when it was customized. Do not remove the active immutable
   release. Reviewed helpers, when selected, belong under `/usr/libexec/reticulumpi` and
   must be owned by `root:root` without group/other write permission.

5. Rotate the dashboard password and invalidate all old sessions. For an auto-managed
   password, isolate remote dashboard access first, then run:

   ```bash
   sudo systemctl stop reticulumpi.service
   sudo -u reticulumpi rm -f \
     /var/lib/reticulumpi/.config/reticulumpi/sessions.db \
     /var/lib/reticulumpi/.config/reticulumpi/sessions.db-wal \
     /var/lib/reticulumpi/.config/reticulumpi/sessions.db-shm \
     /var/lib/reticulumpi/.config/reticulumpi/dashboard_secret
   sudo systemctl start reticulumpi.service
   sudo cat /var/lib/reticulumpi/.config/reticulumpi/dashboard_password.txt
   ```

   Sign in with the new bootstrap value and complete the required password-change dialog.
   The mode-`0600` bootstrap file and restricted session remain until the replacement hash
   is durably written. A successful password change deletes the file, invalidates the
   bootstrap session, closes its WebSocket, and requires a fresh login. Do not delete the
   bootstrap file merely because login succeeded.

   If the password is supplied by environment or root-owned configuration, rotate it at
   that source while the service is stopped and remove the session database as above.

6. Restarting the service rotates the loopback local-API token. Do not forward or archive
   that runtime token. Retain historical journals according to policy; rotation, not log
   deletion, removes the value's authority.

For nodes coming from the mutable layout, `/home/reticulumpi` is a **legacy migration input only**.
Let the transactional administrator migrate and verify that tree before using
the canonical commands above; do not grant the 0.3 services write access to the old home.

## Verification

```bash
sudo find /usr/libexec/reticulumpi -type f \( ! -user root -o -perm /022 \) -print
sudo find /etc/sudoers.d -maxdepth 1 -name 'reticulumpi-*' -print
sudo stat -c '%U:%G %a %n' \
  /etc/reticulumpi/config.yaml \
  /var/lib/reticulumpi/.config/reticulumpi/dashboard_secret
reticulumpi-admin doctor
```

The first two `find` commands should print no unsafe helper and no legacy ReticulumPi sudo
rule. The system configuration should be `root:reticulumpi 640`; dashboard secret material
should be owned by the service account and mode `600`.

This bridge advisory remains published for the full 0.3 support window.
