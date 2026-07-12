# Container Deployment

The production image consumes an already-built ReticulumPi wheel and installs it into a
compiler-free Bookworm/Python 3.11 runtime with hash-locked Dashboard/NomadNet dependencies.
It does not build from the source tree. It runs as fixed UID/GID 10001 under `tini`. All stages
share the multi-architecture digest pinned by `PYTHON_BOOKWORM_IMAGE`; updating that digest is
an explicit release change and requires rebuilding and qualifying both ARM64 and AMD64 images.

From a development checkout, build exactly one wheel before invoking Compose:

```bash
make dev
make package-wheel
mkdir -p docker/config
cp docker/config.example.yaml docker/config/config.yaml
# Review the provided container-specific /data paths first.
nano docker/config/config.yaml
docker compose -f docker/docker-compose.yml up --build -d
docker compose -f docker/docker-compose.yml logs -f
```

The Docker build fails if `dist/` contains zero or multiple ReticulumPi wheels. CI avoids that
ambiguity by downloading the single wheel produced by its package job into each ARM64/AMD64
build context. Rebuild the wheel after source changes; do not let Docker create a divergent
artifact.

## Storage contract

| Path | Purpose | Persistence |
|---|---|---|
| `/config/config.yaml` | Administrator configuration | Read-only bind mount |
| `/data` | `HOME`, XDG config/data/state, identity, RNS config, databases, dashboard secrets, NomadNet | Required volume |
| `/cache` | Tiles and rebuildable caches | Bounded disposable tmpfs |
| `/run/reticulumpi` | Runtime state | Container lifecycle |

Deleting `reticulumpi-data` deletes the node identity and durable messages. Back it up before
`docker compose down -v`.

The health probe requires the current application readiness marker, the recorded live `rnsd`
process identity, and a successful bounded `rnstatus` query. A stale marker therefore cannot
conceal a dead or PID-reused daemon. The entrypoint also detects `rnsd` exit/zombie state,
deletes both runtime markers, and terminates ReticulumPi so the restart policy recovers the pair
together. Release verification deliberately kills `rnsd` and requires exit within 15 seconds.

The published runtime contains no MeshChat or native radio executables. Derived images that add
them must mount a read-only, root-owned external-artifact manifest and retain required mode.

## Networking and hardware

Host networking is required for AutoInterface multicast and common Reticulum TCP/UDP setups.
Linux serial devices can be passed explicitly:

```yaml
devices:
  - /dev/ttyUSB0:/dev/ttyUSB0
group_add:
  - "20"  # host dialout GID; confirm on the host
```

Do not use `privileged: true`. Docker Desktop on macOS cannot directly pass typical USB
serial/SDR devices; use a Linux host or VM.

## Persistence verification

Record the identity hash, create representative message/dashboard state, recreate the
container without removing volumes, then confirm the same identity, database rows, NomadNet
content, and dashboard secrets. Release CI and ARM64 hardware qualification must record this
evidence; a successful local image build alone does not satisfy the persistence gate.

The CI recreation probe runs both architectures with a read-only root filesystem, all Linux
capabilities dropped, `no-new-privileges`, a PID limit, no external network, and bounded runtime
tmpfs mounts. It also verifies the fixed UID/GID, absent build/privilege tooling, writable-path
contract, readiness file, dashboard shell, authenticated API boundary, graceful 60-second TERM,
SQLite integrity, mode-`0600` identity/secrets/databases, and exact durable-state continuity.
Symbolic links or special files anywhere in the probed durable paths fail the gate.

A separate digest-pinned Debian Bookworm/Python 3.11 fixture boots systemd as PID 1. A second
digest-pinned Ubuntu Noble/Python 3.12 fixture exercises the production host tuple. The release
job cannot run unless that fixture reaches a healthy system state, executes a real unit, verifies
the installed unit set, and passes the administrator's transactional install, failed-upgrade,
interrupted-recovery, ownership, custom-root, archive, and rollback regressions. A runner that
cannot provide the required isolated privileged systemd fixture fails the gate; it does not skip
publication checks.

## Image release requirements

Stable images are built for `linux/arm64` and `linux/amd64` from the same signed source and
wheel, use a digest-pinned base, include an SBOM/provenance record, and are promoted without
rebuilding after qualification. The image contains no sudo, systemd, source checkout, build
toolchain, or host control broker.

For a signed tag, the protected publication job downloads the two already-validated Docker save
archives. It checks the archive sidecar hash, single image tag, Linux OS, and architecture before
staging them. Promotion then loads those exact bytes, refuses an existing version or
architecture tag, pushes immutable per-architecture tags, creates the versioned GHCR manifest,
and records the registry digest in the release notes and GitHub attestation. The publication
path contains no `docker build` or `docker save` operation.

Pull by the release digest when immutability matters:

```bash
docker pull ghcr.io/OWNER/reticulumpi@sha256:<RELEASE-MANIFEST-DIGEST>
```
