# Installation Layout

This document describes the supported production filesystem contract. A Git checkout is a
build input, never a runtime installation and never a root-executed update source.

## Supported baseline

- Raspberry Pi 5 with a 64-bit ARM64 userspace
- Raspberry Pi OS/Debian Bookworm with Python 3.11, or Ubuntu 24.04 LTS Noble with Python 3.12
- Dedicated `reticulumpi` system account with `/usr/sbin/nologin`
- Default install root `/srv/reticulumpi`; custom roots such as `/opt/reticulumpi` are
  supported when passed consistently to bootstrap and `reticulumpi-admin`

Production in-place and service-user-owned code installations are rejected.
Every existing custom-root ancestor, including the final root when it already exists, must be
root-owned and not writable by group or other. A nonexistent root is accepted only below the
nearest existing ancestor that satisfies the same rule; `/home/<user>` and other user-controlled
trees are therefore never production roots.

## Filesystem contract

```text
/srv/reticulumpi/
  current -> releases/0.2.5
  releases/<version>/
    .venv/                    root-owned immutable Python environment
    RELEASE                   installed release identifier

/etc/reticulumpi/
  config.yaml                 root:reticulumpi, 0640
  install.json                release/features/rollback manifest

/var/lib/reticulumpi/         service HOME and reticulumpi-owned durable state
  .reticulum/                 Reticulum configuration and transport state
  .config/reticulumpi/        identity, dashboard hash/token/session files
  .local/share/reticulumpi/   plugin identities, databases, and durable content
  .local/state/               XDG state for supporting libraries
  .nomadnet/                  optional NomadNet daemon content
  .nomadnet-tui/              optional browse-only NomadNet TUI state
  runtime-overrides.yaml      allowlisted controls (`internet.force_offline` only)

/var/cache/reticulumpi/       XDG cache root (tiles, TLEs, disposable downloads)
/var/backups/reticulumpi/     root-only verified backups, newest three retained
  admin/                      root-only administration evidence, mode 0700
    transaction.json          recoverable journal, mode 0600
    captive_portal.active     atomic privileged-helper state marker
/run/reticulumpi/             runtime files
  ready                       whole-application readiness marker
  dashboard-ready             listener-bound Dashboard readiness marker

/usr/libexec/reticulumpi/     root-owned reviewed privileged helpers
/etc/systemd/system/          rendered units and feature-specific drop-ins
```

Both `reticulumpi.service` and `rnsd.service` receive this environment:

```text
HOME=/var/lib/reticulumpi
XDG_CONFIG_HOME=/var/lib/reticulumpi/.config
XDG_DATA_HOME=/var/lib/reticulumpi/.local/share
XDG_STATE_HOME=/var/lib/reticulumpi/.local/state
XDG_CACHE_HOME=/var/cache/reticulumpi
```

systemd creates the state and cache roots as `reticulumpi:reticulumpi` mode `0750`; the
services run with `UMask=0077`, so newly created identities, databases, and supporting
directories are private unless code deliberately applies a narrower shared mode.
The transaction journal is deliberately outside this service-owned tree and outside every state
root replaced during rollback, so the service cannot unlink it and restore cannot discard the
only interruption evidence.

An old service home is a **legacy migration input only**, never a 0.3 runtime target.
During an N-1 upgrade the administrator inspects the installed unit's `Environment=HOME`,
XDG/state variables, `WorkingDirectory`, `ExecStart` executable root, and `--config` path
before using the passwd home fallback. It detects the former `.reticulum`,
`.config/reticulumpi`, `.local/share/reticulumpi`, `.nomadnet`, and `.nomadnet-tui`
trees there, migrates verified copies into the canonical state root, and leaves rollback
evidence. Services are not granted write access to the legacy home.

The service cannot modify `/etc/reticulumpi/config.yaml`, its code, virtual environment, or
privileged helpers. Runtime controls write only the schema-validated override file under
`/var/lib/reticulumpi`.

Forced-offline transitions never swap the system configuration. The broker installs or
removes a same-directory, fsynced runtime overlay atomically; the only accepted payload is:

```yaml
internet:
  force_offline: true
```

Firewall simulation and overlay state are reported separately. The historical
`--with-profile` option is accepted as a compatibility no-op; the narrow overlay is now
always coupled to `on`/`off`.

## Bootstrap compatibility launcher

`scripts/bootstrap.sh` performs no installation logic itself. It translates supported legacy
flags to `reticulumpi-admin install` or `upgrade`; the administrator owns release creation,
backups, unit/helper rendering, atomic switch, readiness, and rollback. Bootstrap defaults to
dry-run and never installs OS packages, copies mutable source into production, writes sudoers
rules, imports bundle Python, searches `PATH`, or executes an administrator from `current`.
An installed unit or canonical configuration without `install.json` identifies the mutable legacy
layout, so bootstrap deliberately routes that first transition through `upgrade` and retains a
complete `rollback --to legacy` checkpoint.

Both compatibility launchers accept only a fixed `/usr/sbin/reticulumpi-admin` or
`/usr/bin/reticulumpi-admin` whose file and path components are root-owned and not writable by
group or other. The first install therefore requires the independently signed recovery-
administrator package from the release channel. The launcher fails closed when it is absent;
the candidate bundle cannot bootstrap the privileged code that verifies that same bundle.

```bash
bash scripts/bootstrap.sh --with-dashboard --dry-run
sudo bash scripts/bootstrap.sh --with-dashboard --apply --start
sudo bash scripts/bootstrap.sh --apply --install-dir /srv/reticulumpi --with-nomadnet
```

The following privilege-bearing options are opt-in:

- `--with-captive-portal`
- `--with-offline-tools`
- `--with-chrony-control`

NomadNet selects the packaged extra and `shared-rnsd` unit. MeshChat, node naming, hardware
groups, and external I2P/Yggdrasil/signal packages are explicit operator steps rather than
installer mutations.

## Transactional administrator

All mutating commands default to dry-run behavior unless `--apply` is supplied. Applying
requires root and one exclusive maintenance lock.

After read-only platform, signature, manifest, dependency-profile, and disk-space validation,
the administrator first copies untrusted release inputs through no-follow descriptors into a
private mode-`0700` transaction snapshot and verifies Minisign and hashes only there. It never
reopens the external artifact. It then writes the root-only `preparing` transaction journal before
creating the release directory, virtual environment, or package installation. Configuration and durable
path preparation occurs only after the complete state backup and managed-file snapshots are
verified and the journal advances to `backed_up`.

```bash
reticulumpi-admin install --bundle reticulumpi-install-arm64-0.3.0.tar.gz \
  --feature dashboard --dry-run
sudo reticulumpi-admin install --bundle reticulumpi-install-arm64-0.3.0.tar.gz \
  --feature dashboard --apply --start
reticulumpi-admin status --json
reticulumpi-admin doctor
```

An apply operation first enforces one complete supported tuple: Linux/ARM64 plus either
Raspberry Pi OS/Debian Bookworm and Python 3.11, or Ubuntu Noble 24.04 and Python 3.12.
It selects the signed `core`, `dashboard-nomadnet`, or `all-features` hash lock, installs that
profile with `--require-hashes`, installs the wheel with `--no-deps`, creates a new release
environment, and runs `pip check`,
backs up configuration and durable state, records a transaction journal, atomically switches
the `current` symlink, then checks application readiness. The verified transaction snapshot
includes `/etc/reticulumpi` and the complete canonical `/var/lib/reticulumpi` tree. Every
copied tree is symlink-free and its file
hashes, modes, owners, and groups are verified; SQLite files use the SQLite backup API. A
switch failure reverses every state-root replacement before the old release is restarted.

The administrator removes `/run/reticulumpi/ready` and, when selected,
`/run/reticulumpi/dashboard-ready` before activation. The release must then
remain systemd-active and create a fresh, service-owned readiness marker within the shared
120-second startup deadline. The Dashboard marker is written only after the aiohttp listener
binds and is removed on bind failure, TLS fail-close, stop-before-ready, and normal stop.
SHA-256 hashes for all pre-existing conventional plugin
identity files and the configured primary identity are recorded before and after activation;
any missing or changed identity rolls the transaction back. A newly enabled plugin may
create a new identity, which is retained and recorded after activation.

Wheel-only upgrades require an existing system configuration and signed sibling dependency
profiles; source and ARM64 install bundles also render units and install root-owned helpers.
Interrupted apply and rollback journals are recovered automatically before the next mutation.
Recovery uses the verified backup, durable managed-file snapshots, prior pointer/manifest,
identity hashes, and exact prior service states; missing evidence fails closed.

## systemd contract

The application receives 120 seconds to start and 60 seconds to stop; its internal cleanup
budget is shorter than the systemd deadline. Optional services are expressed as drop-ins,
not unconditional base-unit dependencies. `ProtectSystem=strict`, `ProtectHome=true`,
`PrivateTmp`, resource controls, and `ReadWritePaths` limited to `/var/lib/reticulumpi` and
`/var/cache/reticulumpi` constrain runtime mutation.

Inspect the rendered result after installation:

```bash
systemd-analyze verify /etc/systemd/system/reticulumpi.service
systemctl cat reticulumpi.service
sudo -u reticulumpi test ! -w /etc/reticulumpi/config.yaml
sudo -u reticulumpi test ! -w /srv/reticulumpi/current
```

See [Upgrade and Rollback](upgrade-and-rollback.md) for transaction recovery and
[Security Model](security-model.md) for ownership rationale.
