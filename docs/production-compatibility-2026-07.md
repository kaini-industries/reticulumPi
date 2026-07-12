# Production Compatibility Runbook — July 2026

This document is the redacted compatibility contract for migrating the existing production node
to the refactored ReticulumPi release. It intentionally excludes credentials, network addresses,
identity hashes, radio/device serials, MQTT content, and geographic data.

## Release-blocking baseline

The observed production baseline is **Ubuntu 24.04, ARM64, and Python 3.12**. Local platform policy
and a digest-pinned Noble systemd lane now model this tuple alongside Raspberry Pi OS Bookworm and
Python 3.11. The release must not be installed until the Noble image has executed the exact
hash-locked candidate successfully; static CI wiring alone is not qualification. Bookworm evidence
cannot substitute for qualifying the actual production baseline.

Production currently uses a legacy mixed layout: application code and its environment are under
`/opt`, while service-owned configuration, identities, databases, and other durable state are
under the service account's `/home` tree. Treat both roots as migration inputs. Do not assume that
the passwd home alone describes every active path; derive paths from effective systemd units,
drop-ins, application configuration, and process arguments.

The target layout is:

- root-owned, immutable staged releases under `/srv/reticulumpi`, with an atomic `current` link;
- system configuration and manifests under `/etc/reticulumpi`;
- durable service state under `/var/lib/reticulumpi` and disposable cache under
  `/var/cache/reticulumpi`;
- verified, root-only backups and the transaction journal under `/var/backups/reticulumpi`; and
- no runtime writes to the legacy `/opt` or `/home` application trees after cutover.

MeshChat must be split during migration. Its reviewed code and virtual environment belong in a
root-owned immutable external tree, such as `/srv/reticulumpi-external/meshchat`; its storage,
database, configuration, and identity remain writable durable state under `/var/lib/reticulumpi`.
Never place MeshChat storage inside the hashed code tree.

## Production feature contract

Preserve and qualify these installed feature categories exactly; absence from a new default is not
permission to disable one:

- package/application features: `adsb`, `dashboard`, `gps`, `lora`, `meshcore`, `meshtastic`,
  `nomadnet`, `sensors`, and `space`;
- integration and privilege features: `shared-rnsd`, `captive-portal`, and `chrony-control`; and
- operational behavior: watchdog preservation across install, activation, and rollback.

Preserve the service account's required supplementary groups: `dialout`, `plugdev`, `gpio`, `spi`,
`i2c`, and `yggdrasil`. Verify membership before service activation and again after reboot.

Inventory and preserve the effective relationships with `gpsd`, `chrony`, `hostapd`, `dnsmasq`,
`yggdrasil`, `i2pd`, Avahi, and lighttpd. Also preserve reviewed udev rules and the effective RTL
kernel-module blacklist. The migration must retain former enabled/disabled and active/inactive
states rather than enabling every discovered service.

Production activation requires a root-owned external-artifact manifest with immutable version and
SHA-256 records for all five required artifact categories:

1. the complete immutable MeshChat code/virtual-environment tree;
2. `rtl_test`;
3. `dump1090`;
4. `rtl_fm`; and
5. `rtl_power`.

Resolve each executable to an absolute path without executing it while generating the manifest.
Activation must fail closed if an enabled feature's record is missing, mutable, or does not match
the installed bytes.

## Migration gates

### 1. Read-only capture

Before changing production, capture a locally protected, mode-`0600` inventory of the OS, Python
environment, packages, effective systemd units/drop-ins, service states, configuration paths,
ownership, group membership, udev rules, RTL blacklist, listeners, enabled features, external
artifact versions, state roots, and database schemas. Redact it before committing any derived
fixture or report.

Record file metadata and hashes for durable state, but never copy credentials or identity material
into the repository. Use the SQLite backup API for live databases and run integrity checks against
the backups.

### 2. Production-derived qualification

Create sanitized fixtures for Ubuntu 24.04/ARM64/Python 3.12, the legacy `/opt` plus `/home`
layout, the effective feature set, unit relationships, database schemas, and custom state paths.
The exact signed candidate must pass:

- hash-locked installation and `pip check` on the production Python baseline;
- dry-run migration with every source and destination path listed;
- systemd verification, startup readiness, shutdown deadlines, watchdog behavior, and reboot;
- database clone migration, integrity checks, and compatibility with the rollback release;
- all five external-artifact checks;
- group, udev, RTL blacklist, and dependent-service checks; and
- representative RNode, Meshtastic, MeshCore, GPS, SDR, Dashboard, NomadNet, networking, and
  captive-portal behavior.

The Noble ARM64 systemd lane covers the manifest/config portion of this requirement with a
root-owned immutable MeshChat stub tree and fail-fast dummy `rtl_test`, `dump1090`, `rtl_fm`, and
`rtl_power` files. It starts only the MeshChat stub through the packaged launcher and never executes
the radio fixtures. Real MeshChat behavior and every SDR/RF assertion remain HIL requirements.

On 2026-07-12, the production-shaped lane passed locally against one consistent working-tree
candidate in all three modes: fresh `/opt`, fresh `/srv`, and legacy `/opt` plus service-home state
to immutable `/srv` with exact legacy rollback. This is useful implementation evidence, but it is
not a substitute for a signed release transcript, production dry-run, real MeshChat validation, or
hardware-in-the-loop qualification.

### 3. State, database, and identity acceptance

Before activation, create and verify a full snapshot of `/etc/reticulumpi`, every discovered
legacy state root, the canonical `/var/lib/reticulumpi` target, relevant managed units/drop-ins,
and prior service states. The transaction journal must exist outside all state roots that rollback
can replace.

Cutover passes only when:

- every configured database is present, reports an expected schema, and passes SQLite integrity;
- every pre-existing Reticulum, application, plugin, NomadNet, and MeshChat identity is present and
  byte-identical to its preflight value;
- newly enabled components may create new identities, but existing identities may not change;
- both application and Dashboard readiness markers are fresh and service-owned where applicable;
- required listeners, hardware paths, plugins, watchdogs, and dependent services are healthy; and
- no service writes to the retired `/opt` or `/home` runtime paths.

Any failed identity, database, readiness, or dependency gate triggers rollback; it is never a
warning-only condition.

## Staged production rollout

1. Install the independently signed, root-owned recovery administrator before using a candidate.
2. Verify the exact signed ARM64/Ubuntu 24.04/Python 3.12 candidate and its dependency and external-
   artifact manifests in a private staging directory.
3. Run administrator and database dry-runs. Review path mappings, feature selection, service-state
   preservation, disk-space requirements, and rollback evidence.
4. Stop services only inside the approved maintenance window, create verified backups, then stage
   the candidate side-by-side under `/srv/reticulumpi`.
5. Switch atomically, apply state migrations, restore the recorded service relationships, and run
   every acceptance gate above.
6. On failure, stop candidate processes, restore the full legacy code/configuration/state layout
   and exact prior service states, verify identity and database continuity, then restart the former
   environment. A first migration must support this complete legacy restore even when no earlier
   immutable release exists.
7. Retain the legacy installation and verified backups until the exact candidate completes a
   **72-hour soak** with no failed units, identity drift, database integrity errors, orphaned
   processes, OOM kills, unexpected restarts, resource leaks, or peripheral recovery failures.

## SDR recovery and remaining physical preflight

An incorrectly quoted inspection command unintentionally launched a second SDR utility, which
reset one dongle. The unintended processes were terminated immediately and every managed service
retained its original PID with zero restarts. A final read-only USB inventory on 2026-07-12 showed
both expected RTL2832-family dongles enumerated again; no radio executable was launched during that
confirmation. The transient reset is still a hardware-in-the-loop warning: before the maintenance
window, record stable device links and power/cabling/kernel behavior, then make the exact candidate
pass repeated SDR claim/release, contention, failure/restart, and unplug/replug tests on
representative hardware. Do not proceed with production cutover while any expected dongle is
absent or unstable.
