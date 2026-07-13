# Security Model

ReticulumPi treats network peers, browser clients, local processes, plugin input, release
bundles, and service-owned files as untrusted until explicitly authorized.

## Trust boundaries

- Reticulum and LXMF provide transport encryption and identity primitives. Plugins still
  authorize the remote identity for every operation.
- The dashboard authenticates sessions before mutation. Loopback location alone grants no
  authority.
- `/etc/reticulumpi` and release code are administrator-controlled. The service controls
  only `/var/lib/reticulumpi`, `/var/cache/reticulumpi`, and `/run/reticulumpi`.
- Code run through sudo must be root-owned, immutable to the service, and accept a closed
  set of validated arguments.
- A source checkout is a build input, not a production runtime or trusted update channel.
- Separately provisioned MeshChat and native radio tools are trusted only through the
  root-owned schema-1 external-artifact manifest. Each record binds an absolute path to an
  immutable version label and SHA-256; affected plugin activation fails closed before
  construction. Development mode retains ordinary `PATH` lookup and is non-production only.

## Identity and secrets

Identity creation and restore use one sibling lock, same-directory temporary files, mode
`0600`, file and directory fsync, and atomic replacement. Persistence failure is fatal;
starting with an ephemeral identity would silently change the node's cryptographic name.

The generated dashboard password is written only to an atomic, fsynced, mode-`0600`
bootstrap file and is never logged. Bootstrap sessions are restricted until a durable
password change removes that file; rotation invalidates sessions and their WebSockets. The
local-service token is a separate mode-`0600`, read-only loopback secret under
`/run/reticulumpi` and rotates on every service start.

Configuration is root-owned `0640`. Runtime controls persist only allowlisted overrides in
`/var/lib/reticulumpi/runtime-overrides.yaml`; malformed configuration is never replaced.
The forced-offline helper applies or removes only `internet.force_offline` in that overlay
and never backs up, rewrites, or substitutes `/etc/reticulumpi/config.yaml`.

File transfer begins with resource acceptance disabled and rechecks link-scoped identity
authorization for uploads and metadata requests. A completed upload is written to a private
same-directory temporary inode, fsynced, published with a no-replace hard link, and followed by
a directory fsync; existing files and symlinks are never followed or overwritten. Persistence
failure is reported as a failed transfer rather than a successful receive.

The service account's `HOME` is `/var/lib/reticulumpi`; its XDG config, data, and state
roots are conventional subdirectories there, while `XDG_CACHE_HOME` is
`/var/cache/reticulumpi`. Both application and RNS units use `ProtectHome=true` and receive
no writable path under `/home`.

## Privileged operations

The installer places reviewed helpers under `/usr/libexec/reticulumpi`, owned by root and not
writable by the service. Offline, captive portal, and chrony helpers are opt-in. Obsolete
passwordless sudo rules are removed during installation.

The 0.3 interface is a root-owned, socket-activated control broker with peer credential
checks, a 4 KiB message limit, a five-second request-read deadline, and enumerated operations.
The socket is mode `0660`, accepts
only the `reticulumpi` peer UID, and exposes restart, captive-portal, and chrony operations
whose arguments are independently validated.
Its unit runs isolated Python only from the root-owned immutable `current` release. Captive-portal
state is kept in the root-only administration directory, and captive/DNS configuration writes use
same-directory temporary files plus atomic replacement so service-planted symlinks are never
followed by root.
Operators should still omit every privileged feature they do not need and review installed
rules and units:

```bash
sudo find /usr/libexec/reticulumpi -type f -not -user root -o -perm /022
systemctl cat reticulumpi.service
systemctl cat reticulumpi-control.socket reticulumpi-control@.service
```

## Deployment and updates

Release code and virtual environments are immutable and root-owned. Upgrades build a new
release, verify dependencies, back up state, switch atomically, and roll back on activation
failure. CI verifies installed wheels rather than relying only on editable checkouts. Production
updates consume signed release artifacts only; neither a source checkout nor `git pull` is an
authorized deployment mechanism.

The Minisign release private key remains on a trusted offline workstation. GitHub stores only the
independently distributed public key, and neither the `release-signing` nor `release` environment
contains the private key. Tag CI first attests an exact input manifest. The workstation verifies
that attestation and its repository, tag, commit, run ID, and run-attempt bindings before signing
the install manifest. A tag-bound workflow then emits and attests the exact global release
manifest; the workstation verifies the complete nested payload and both run bindings before
signing that manifest. Only the two public detached signatures are sent back as workflow inputs.
The protected jobs re-verify every binding and exact asset allowlist before candidate assembly or
publication.

Containers run as fixed UID/GID with `HOME=/data`, a read-only configuration mount, a
single durable data volume, a disposable cache, and `tini`. Containers contain no compiler,
sudo, systemd, source checkout, or host privileged broker.

## Operator checklist

- Bind the dashboard to loopback unless remote access is required.
- Use TLS and a network allowlist for LAN exposure.
- Never forward the local-service token through a reverse proxy.
- Treat `X-Forwarded-Proto` as authoritative only when `reverse_proxy.enabled` is true and the
  immediate peer is inside the explicitly configured `trusted_networks`; client/LAN ranges do
  not belong in that list.
- Default file transfer to `deny`; use exact identity allowlists where needed.
- Back up and verify identity hashes before and after every maintenance event.
- Review the release manifest, SBOM, checksums, and verification record.
- Report vulnerabilities privately as described in [SECURITY.md](../SECURITY.md).
