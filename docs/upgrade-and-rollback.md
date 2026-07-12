# Upgrade and Rollback

ReticulumPi upgrades are staged releases. They never run `git pull`, update the live virtual
environment, or overwrite the active release in place.

## Before upgrading

1. Install the independently signed recovery-administrator package from the release channel. It
   provides a fixed, root-owned `/usr/sbin/reticulumpi-admin` (or `/usr/bin` equivalent). The
   compatibility launchers deliberately fail when this package is absent and never execute
   Python from the candidate bundle.
2. Obtain the signed ARM64 install bundle through the release channel. An unpacked signed
   source bundle or signed wheel plus its signed dependency profiles is accepted for recovery
   and bridge workflows.
3. Read its changelog and security notices.
4. Confirm free space covers the candidate release plus two copies of the captured system,
   durable, and service-home state described below.
5. Run diagnostics and a dry run:

```bash
reticulumpi-admin doctor
reticulumpi-admin upgrade --bundle /path/to/bundle --install-root /srv/reticulumpi --dry-run
```

The dry run reports the version, selected hash-locked dependency profile, features, root,
previous release, detected legacy layout, and prospective release.
It does not stop services or write system state.

If the installed legacy unit names a noncanonical configuration file, that unit path is
authoritative. The path may remain under a service-owned legacy home, but it may not contain
symlinks or overlap systemd, sudoers, backup, runtime, or other protected administration roots.
The administrator imports the verified content and retains its exact bytes, ownership, and mode
for rollback. A stale canonical file never silently overrides the unit's active configuration.

## Apply

```bash
sudo reticulumpi-admin upgrade \
  --bundle /path/to/bundle \
  --install-root /srv/reticulumpi \
  --feature dashboard \
  --apply
```

Before mutation, the administrator requires Linux/ARM64 and one supported OS/Python tuple:
Raspberry Pi OS/Debian Bookworm with Python 3.11, or Ubuntu Noble 24.04 with Python 3.12. It then
verifies the bundle and dependency-profile signatures. It
copies the external bundle, signature, manifest, constraints, and wheel through no-follow file
descriptors into a root-owned mode-`0700` snapshot, verifies that immutable snapshot, and never
reopens the external paths. It then acquires `/run/lock/reticulumpi-maintenance.lock` and durably
records a root-only `preparing` transaction at
`/var/backups/reticulumpi/admin/transaction.json` before creating the candidate release, its
virtual environment, or installing packages. It installs the selected
profile with `pip --require-hashes`, installs the wheel with `--no-deps`, and runs `pip check`.
Only after candidate validation does it stop the prior services, create and verify a backup
below `/var/backups/reticulumpi`, persist managed-file snapshots, and advance the journal to
`backed_up`. System/configuration paths and migrations are mutated only after that checkpoint.
It then atomically replaces the `current` symlink. Activation succeeds only after systemd
reports the service active and the application creates a fresh `/run/reticulumpi/ready`
marker within the 120-second startup deadline. When the Dashboard feature is installed, the
Dashboard must also create a fresh, service-owned, private
`/run/reticulumpi/dashboard-ready` marker after its listener binds. An activation timeout,
Dashboard bind failure, or identity mismatch
restores the previous release and every captured state root before restarting it.

Before legacy source roots are removed, the administrator uses SQLite's online backup API to clone
every database in the active canonical configuration/data roots into root-only transaction
evidence. Each clone must pass `integrity_check`; the journal records its path, `user_version`, and
table/index/trigger/view definitions. Every pre-existing database recorded by the pre-activation
backup must have a canonical live destination. Additive schema changes are allowed here; the exact
schema contract and prior-release compatibility are separate release/HIL gates.

If `rnsd` was active, its watchdog, ReticulumPi, and `rnsd` are stopped in that order before
the snapshot or release switch. Candidate `rnsd` is restarted and required to remain active;
rollback restores the exact former enabled/active state of every managed unit.

The newest three automatic backups are retained. A backup contains `/etc/reticulumpi` and
the complete canonical `/var/lib/reticulumpi` state tree, including Reticulum, XDG data,
and enabled NomadNet state. The root-only metadata
records the feature set, per-file SHA-256, mode, owner and group, verified SQLite backups,
and identity hashes. Symlinks and special files are rejected. All roots are staged and
verified before any live root is replaced; an injected switch failure reverses the roots
already replaced.

The transaction journal records identity hashes both before and after candidate activation.
Every pre-existing file named `identity` under the managed compatibility roots, plus the
configured primary identity, must remain present and byte-identical. New identities created
by newly enabled plugins are allowed and appear only in the after set.

For an N-1 upgrade, the old service home is treated only as a **legacy migration input**.
The administrator reads installed unit `Environment`, `WorkingDirectory`, and `ExecStart`
directives before falling back to the passwd home, so custom layouts are not mistaken for
the historical default. It detects the old `.reticulum`, `.config/reticulumpi`,
`.local/share/reticulumpi`, `.nomadnet`, and `.nomadnet-tui` trees, stages verified copies
under `/var/lib/reticulumpi`, and validates identity continuity before activation. The 0.3
services never write back to the legacy home.
The first bridge backup, exact roots, managed integration files, and service-state evidence are
carried across later immutable upgrades. They are not pruned automatically because only an
operator can decide that the hardware qualification and soak period have completed.

## Roll back code

```bash
reticulumpi-admin rollback --dry-run
sudo reticulumpi-admin rollback --apply

# Select a retained release explicitly
sudo reticulumpi-admin rollback --to 0.2.5 --apply
```

Manual rollback first creates the same complete safety snapshot. It normally changes the
active release without rewinding application data. If target activation or identity
verification fails, the administrator restores that snapshot and the former release.
Database migrations remain additive through 0.4.x so supported prior code can read the newer
schema.

## Database safety

```bash
reticulumpi-admin db plan
reticulumpi-admin db migrate --dry-run
sudo systemctl stop reticulumpi
sudo reticulumpi-admin db migrate --apply
reticulumpi-admin db backup --dry-run
sudo reticulumpi-admin db backup --apply
reticulumpi-admin db backups
sudo reticulumpi-admin db restore /var/backups/reticulumpi/db-.../messages.db \
  --database /var/lib/reticulumpi/messages.db --dry-run
sudo reticulumpi-admin db restore /var/backups/reticulumpi/db-.../messages.db \
  --database /var/lib/reticulumpi/messages.db --apply
```

`db plan` loads only enabled built-in declarations from the validated root-owned system
configuration and reports path, current/target version, pending versions, and checksums. It
never imports external plugin paths as root. `db migrate` defaults to a full clone-based dry
run that does not create the live target directory. Applying requires root and a stopped
service; each target is locked, integrity checked, backed up under
`/var/backups/reticulumpi/databases`, migrated atomically, and retained with its newest three
backups. Generic command-line SQL is unsupported. Restore also requires the stopped service,
rejects service-owned path aliases and final-component symlinks for both backup and target,
copies through no-follow descriptors, and creates a safety copy before atomic replacement.
Root-owned immutable platform aliases such as macOS `/var` are canonicalized without granting
the same trust to state-directory symlinks. Restoring older state discards messages and settings
created after that backup.

## Interrupted operation

Inspect the journal and manifest before taking manual action:

```bash
sudo cat /var/backups/reticulumpi/admin/transaction.json
sudo cat /etc/reticulumpi/install.json
readlink -f /srv/reticulumpi/current
reticulumpi-admin status --json
```

`status` reports the journal state, backup, recovery timestamp, and recovery evidence;
`doctor` fails while a transaction is unfinished. Before any new install, upgrade, or
rollback apply, the administrator automatically recovers an unfinished journal while holding
the maintenance lock. A `switching` transaction requires a verified state backup, durable
managed-unit snapshots, the previous release, and the exact prior enabled/active service
states. It stops candidate processes, restores units, release pointer, manifest,
configuration and state, verifies identities/readiness, removes only a disposable candidate,
and records `state: recovered`. Missing or contradictory evidence fails closed and requires
manual investigation; it never guesses from `current` alone. Never delete backups or the
previous release until the node has passed its post-upgrade hardware and identity-continuity
checks.

A `preparing` transaction predates all configuration and durable-state changes. Recovery verifies
that `current` still names the recorded previous release, restores the exact recorded service
states, recursively removes only the recorded incomplete candidate release when it is a real
directory directly beneath `releases/`, and records durable recovery evidence. A missing,
symlinked, or escaped candidate path fails closed.
