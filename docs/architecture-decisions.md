# Architecture Decisions

These decisions are accepted for the 0.3 remediation line.

## Lifecycle and managed resources

Plugin readiness is distinct from construction and health. Hard dependents run only when
providers are ready. Lifecycle transitions are serialized without holding registry locks
across plugin code. Resources registered with a plugin—subscriptions, threads, executors,
tasks, processes, links, destinations, and request handlers—are released once in reverse
order on every stop/failure path. If a timed-out start resumes and registers a resource after
cleanup has closed, registration fails and that late resource receives an immediate bounded
daemon cleanup attempt. Legacy plugins remain compatible through 0.4.x.

Lifecycle calls run on daemon workers so a hung third-party plugin cannot hold interpreter
shutdown open. LXMF normally installs process signal handlers while constructing a router,
which Python permits only on the main thread. Built-in plugins therefore construct routers
through `reticulumpi.lxmf_compat.create_lxm_router()`: application-owned signal handlers stay
authoritative on lifecycle workers, while main-thread construction retains LXMF's normal
behavior. External plugins that create an LXMF router during `start()` should use the same
helper.

## Callback isolation

No third-party callback may block a global dispatcher. Event and announce subscribers use
bounded mailboxes and daemon workers. Timeouts disable only the offending registration and
publish bounded diagnostics.

## Transactional data migration

Migrations are immutable, checksummed, additive declarations. The engine locks the database,
checks integrity and disk space, creates a protected SQLite backup, dry-runs on a clone,
applies under one explicit transaction, validates, and restores on failure. Generic SQL from
the command line is intentionally unsupported.

## Process supervision

External pipelines are one managed process group. Launch is all-or-nothing; unexpected EOF
terminates the entire group, releases device leases, marks degraded state, and enters bounded
backoff. A scheduler-backed decoder never restarts its process group directly: it suspends its
slot, returns the physical lease, and a delayed worker only restores that registration's
eligibility. Normal priority arbitration and a fresh acquisition callback must grant ownership
before the replacement process can launch. Delayed workers carry a registration token, so they
cannot revive a slot that was removed and recreated while they slept. Retry delays use monotonic
time (1, 2, 4, 8, then 30 seconds) and remain capped at five attempts per ten minutes. Device
identity is canonicalized so index and serial aliases cannot double-claim an SDR.

## Clock semantics

Process-local deadlines, uptime, stability windows, stale-fix ages, in-memory peer TTLs,
cooldowns, sampling intervals, and active recording limits use a monotonic clock. NTP or
operator wall-clock corrections therefore cannot prematurely expire work, extend a timeout,
produce negative uptime, or bypass a resource limit. Wall time remains authoritative only for
persisted or externally reported timestamps and cross-restart expiry, including GPS fix times,
message/favorite metadata, MQTT packet/publication times, JWT expiry, recording filenames, and
remote ping timestamps. Monotonic readings are never persisted or sent as timestamps.

## Install and privilege model

Production releases are immutable, root-owned, and selected by an atomic symlink. Service
configuration is root-owned; runtime overrides are separate. Passwordless execution of
service-owned scripts is forbidden. The compatibility helper boundary is `/usr/libexec`; the
target design is a credential-checking socket-activated control broker with enumerated calls.

Application and RNS services share `/var/lib/reticulumpi` as `HOME`. XDG config, data, and
state remain conventional subtrees of that durable root, while XDG cache is isolated under
`/var/cache/reticulumpi`. This preserves third-party library expectations without granting
write access to a separate service home. Prior home-relative trees are migration inputs,
never live 0.3 targets.

## Dashboard and offline shell

The dashboard preserves its field-instrument character while semantic HTML, keyboard access,
contrast, touch targets, and reduced motion take priority. Feature assets load on demand. The
service worker caches only versioned static shell files, never API/auth data, and preserves a
complete prior shell until its replacement is valid.
