# Release Process

## Supported lanes

- Production: Raspberry Pi 5 ARM64 on Raspberry Pi OS/Debian Bookworm with Python 3.11, or
  Ubuntu Noble 24.04 with Python 3.12.
- CI compatibility: Python 3.11, 3.12, 3.13, and 3.14.
- Containers: ARM64 and AMD64 from one wheel and source revision.

## Candidate gate

The Bookworm fixture executes this tagged documentation smoke test verbatim, with a temporary
home and without CI credentials in its environment:

```bash bookworm-doctest
test "$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')" = "3.11"
reticulumpi --help >/dev/null
reticulumpi-admin --help >/dev/null
python tools/check_docs.py
```

1. Resolve every release-scoped row in `audit-remediation-2026-07.md` or document an approved
   deferral that is not P0/P1, then freeze the candidate commit.
2. Run Ruff check/format, ShellCheck, Python/JavaScript/YAML/Compose/sudoers/systemd checks,
   deterministic documentation links/stale-reference/help snapshots, dependency and secret
   scanning, the parallel suite, serial branch coverage, the dashboard performance lane, and
   the install/recovery/rollback suite with systemd running as PID 1 on both supported production
   tuples.
3. Create and cryptographically verify an annotated `vMAJOR.MINOR.PATCH` tag for the frozen
   commit. Pushing that immutable tag starts the candidate artifact build.
4. Build wheel and sdist once from the tag. Run `twine check`, verify artifact versions against
   the tag, install with the source unavailable, discover plugins, and fetch dashboard assets.
   Assemble the ARM64 install archive from that same source/wheel input; do not rebuild it after
   qualification.
5. Build both container architectures from that exact wheel. Test TERM handling and persistent
   identity across recreation.
6. Generate SHA-256 manifest, CycloneDX SBOM, provenance, Minisign signature, and release
   notes.
7. Qualify the exact artifacts on the hardware fixture and complete the 72-hour soak.
8. Promote those artifacts without rebuilding. A failed candidate version is never retagged or
   reused; fix the issue and choose a new version.

## Recovery administrator prerequisite

The compatibility launchers are not bootstrap verifiers. They never import or execute Python
from a candidate bundle, source checkout, `PATH` lookup, or mutable installed release. A first
install requires an independently signed recovery-administrator OS package that installs a
regular root-owned, non-group/other-writable `reticulumpi-admin` at `/usr/sbin` (or `/usr/bin`)
and is authenticated independently of the candidate bundle. Until that package is available
through the release channel, production first installation is intentionally blocked rather than
falling back to unverified code.

Release qualification must install this package first, verify its ownership and mode, prove both
launchers reject its absence or unsafe permissions, and then use it to verify the candidate's
Minisign signature and exact hash manifest before bundle Python is built or installed.

### Building the recovery-administrator Debian package

`tools/build_admin_deb.py` packages the already validated ReticulumPi wheel independently of an
install candidate. The administrator import surface consists only of the Python standard library
and reviewed first-party modules extracted from that wheel. Its separate third-party runtime must
therefore be an exact empty site-packages directory with an exact zero-byte SHA-256 manifest. Any
file, symlink, special entry, or even empty subdirectory is rejected; wheelhouse mode is
unsupported. Adding first-party recovery code does not authorize adding PyYAML, RNS, LXMF, or any
other distribution to that runtime. This removes third-party dependency resolution, ABI selection,
and import shadowing from the root recovery package. The builder does not invoke pip, access the
network, install the result, or hold release signing keys.

Both supported profiles are built from the same validated wheel with non-colliding filenames:

```bash
mkdir -p build/admin-runtime
: > build/admin-runtime.SHA256SUMS
python tools/build_admin_deb.py \
  --wheel dist/reticulumpi-0.3.0-py3-none-any.whl \
  --wheel-sha256 "$(sha256sum dist/reticulumpi-0.3.0-py3-none-any.whl | cut -d' ' -f1)" \
  --runtime-kind site-packages \
  --runtime-source build/admin-runtime \
  --runtime-manifest build/admin-runtime.SHA256SUMS \
  --platform-profile linux-arm64-debian-bookworm-py311 \
  --version 0.3.0 \
  --source-date-epoch "$SOURCE_DATE_EPOCH" \
  --output dist/reticulumpi-admin_0.3.0_linux-arm64-debian-bookworm-py311_arm64.deb
```

Repeat with profile `linux-arm64-ubuntu-noble-py312` and the matching profile in the output
filename. The Bookworm package declares Python 3.11 (but not 3.12); the Noble package declares
Python 3.12 (but not 3.13). Both also depend on `python3-venv` and `minisign`. The administrator
still preflights `/usr/bin/systemctl` and `/usr/sbin/useradd` before mutation, so the target OS must
provide systemd and the normal account-management utilities.

Branch CI uses the normalized PEP 440 version embedded in the validated wheel, including
setuptools-scm development versions, so both systemd lanes exercise the package before a tag.
Protected publication still accepts only the exact stable version derived from the signed release
tag.

The package installs its root-owned payload below `/usr/lib/reticulumpi-admin`, including the
administrator, stable-help formatter, platform policy, external-artifact digest implementation,
dependency-free migration catalog and configuration projection, migration engine, and runtime
counters from the exact wheel. The fixed `/usr/sbin/reticulumpi-admin` wrapper runs
`/usr/bin/python3 -I -S` with a fixed launcher; it does not consult `PYTHONPATH`, the working
directory, `PATH`, an install candidate, or `current`. `external-artifact digest`, `db plan`, and
`db migrate --dry-run` consequently remain available before the normal release environment and its
third-party dependencies exist. Migration configuration is projected fail-closed from the trusted
system file; the recovery administrator does not import the normal PyYAML loader, a full plugin
implementation, or an external plugin path as root.

Each package emits a matching `.deb.sha256` sidecar. The `recovery-admin` CI job uploads both
profile packages and both sidecars. The corresponding ARM64 systemd lane installs its package
with `dpkg`, checks the embedded platform profile, and executes
`/usr/sbin/reticulumpi-admin --help` through the isolated launcher before installation, recovery,
and rollback tests use that path. Release staging revalidates each sidecar, Debian archive
structure, profile-correct dependency metadata, embedded wheel digest, required first-party
recovery modules, and empty-runtime manifest digest. Each installed-package lane invokes the fixed
wrapper from outside the source checkout, computes external-artifact digests, and exercises a real
enabled-plugin database plan and clone-only dry run before candidate installation. It then includes
all four files in the global `SHA256SUMS`; the normal Minisign step signs that manifest and GitHub
publication uploads the packages with the other exact assets. Release signing remains separate
from the builder; a per-package `.minisig` produced by the builder would be a release failure.

## Quality thresholds

| Release | Line | Branch | Changed code | Critical modules |
|---|---:|---:|---:|---:|
| 0.2.5 | 50% | Reported | 90% | 90% |
| 0.3.0 | 65% | 55% | 90% | 90% |
| 0.3.1+ | 70% | 60% | 90% | 90% |

Critical modules include identity, authentication, lifecycle, migration, and installation.
Warnings from first-party code fail the release lane.

### Automated coverage enforcement

The serial Python 3.11 lane writes branch-aware `coverage.xml` and immediately runs
`tools/check_coverage_gate.py`. The gate recomputes aggregate counts from the file and line
records instead of trusting rounded XML rate attributes. It always requires 90% of changed
executable lines and 90% line and branch coverage for each critical module:

- `identity_manager.py`
- `builtin_plugins/web_dashboard/auth.py`
- `app.py`
- `plugin_base.py`
- `migrations.py`
- `admin_cli.py`

The optional `--release-version` selects the aggregate row in the table above. An omitted
version uses the current 0.3.2 stabilization policy. CI uses that policy for pull requests and
ordinary main pushes, then passes the exact `vMAJOR.MINOR.PATCH` name for a tag build so the
candidate cannot avoid its release-specific line or branch threshold.

Changed-line comparison is also fail-closed. Coverage checkout uses full history
(`fetch-depth: 0`), and the gate derives its base from the GitHub event payload:

- pull requests use `pull_request.base.sha`;
- ordinary branch pushes use `before`;
- a release tag compares with the newest lower strict SemVer tag reachable from the candidate;
- the initial `v0.3.2` release is bootstrapped from the exact historical 0.2.4 version-boundary
  commit `89249b8b58cb86ac14ff7179abbbca3cb762d2a4`. The gate requires that commit to
  declare version 0.2.4 and be a strict first-parent ancestor of the candidate. This is a
  coverage baseline, not a retroactive tag, release, or claim of signed 0.2.4 provenance;
- any other first release without an explicit version-controlled bootstrap compares with Git's
  empty tree so every production source line is changed;
- a newly created branch whose `before` is all zeroes uses the checked-out commit's first parent;
- a root initial push with no parent, an unavailable event commit, a deleted ref, malformed
  payload, or a push whose `after` does not match `HEAD` fails instead of silently skipping
  changed-code coverage.

For a local comparison, supply a base revision directly:

```bash
python tools/check_coverage_gate.py origin/main \
  --coverage-xml coverage.xml \
  --release-version 0.3.2
```

For `v0.3.2`, the pinned bootstrap still enforces 90% changed-line coverage across every
executable line changed since the 0.2.4 boundary, in addition to the 70% aggregate line, 60%
aggregate branch, and 90% line-and-branch critical-module gates. It is deliberately embedded in
the reviewed gate rather than supplied through a mutable environment variable or CLI override.

Input/report failures exit 2, a coverage-policy failure exits 1, and a complete pass exits 0.

## Versioning and evidence

`setuptools-scm` is the only project-version source. Do not edit a version string in
`pyproject.toml`, package code, or generated `_version.py`. A clean release checkout gets its
exact version from a strict `vMAJOR.MINOR.PATCH` tag; ordinary commits receive an SCM-derived
development version. Wheels and sdists contain the generated version module, while Git export
archives use `.git_archival.txt` substitution. A source tree with neither SCM metadata,
generated metadata, nor a versioned parent directory reports the explicit non-release fallback
`0+unknown`.

Create and verify the tag on a trusted release workstation:

```bash
git tag -s v0.3.0 -m "ReticulumPi 0.3.0"
git verify-tag v0.3.0
```

The tag-triggered CI lane rejects lightweight tags, unsigned tag objects, noncanonical names,
and wheel/sdist metadata that differs from the tag. Structural CI inspection does not replace
cryptographic trust validation. Before promotion, run the verifier with the configured trusted
release keyring and attach its transcript to the release-verification record:

```bash
python tools/verify_release_tag.py --require-signature --verify-signature \
  v0.3.0 dist/*.whl dist/*.tar.gz
```

No signed release tag or published signed artifact is evidenced by this working tree. The
tag-only publication lane described below is an enforcement mechanism, not evidence that it
has run successfully. Record trusted tag verification and the resulting attestations in the
versioned release-verification record.

## Hash-locked dependency profiles

Release installs use generated universal hash-checking profiles shared by both qualified ARM64
production lanes. Their canonical `production-universal` names describe that shared scope; the
platform preflight still rejects every untested OS/Python tuple:

- `constraints/production-universal-core.txt` contains core production dependencies.
- `constraints/production-universal-dashboard-nomadnet.txt` contains the container's core plus
  Dashboard and NomadNet profile.
- `constraints/production-universal-all-features.txt` is the complete standalone dependency set used
  whenever any package feature beyond Dashboard/NomadNet is selected.
- `constraints/production-universal-build.txt` contains pinned build and artifact-check tooling.

The adjacent `.in` files are the reviewable policy inputs. Every resolved package is pinned and
every permitted distribution has a SHA-256 hash. Core and Dashboard/NomadNet runtime profiles
require binary distributions. The reviewed all-features profile may contain explicitly hashed
source distributions for radio/hardware tooling; it never permits an unlisted source, URL,
mutable VCS reference, or editable install.

Administrators released before the canonical rename expect the retired `bookworm-py311-*`
filenames and cannot consume new bundles. Install the matching independently packaged recovery
administrator before publishing or applying a canonical-only bundle. New administrators retain
fail-closed read compatibility with previously signed legacy-name bundles, but new bundle builds
reject the retired aliases.

Native executables and MeshChat are a separate trust domain. Production uses
`external_artifacts.mode: required` with the root-owned
`/etc/reticulumpi/external-artifacts.yaml`; its schema-1 records bind absolute paths to immutable
version labels and SHA-256 digests. See `config/reticulumpi/external-artifacts.example.yaml`.
The production manifest contains five independently reviewed categories: the complete MeshChat
tree, `rtl_test`, `dump1090`, `rtl_fm`, and `rtl_power`.
Generate reviewed values without executing the artifact:

```bash
reticulumpi-admin external-artifact digest --kind file /usr/bin/rtl_fm
reticulumpi-admin external-artifact digest --kind tree /srv/reticulumpi-external/meshchat
```

This recovery command uses the same no-follow file and deterministic tree algorithm as runtime
verification without importing PyYAML or executing the artifact. Install the manifest as
`root:reticulumpi` mode `0640`. MeshChat's digest includes its virtual environment, and first-party
radio plugins preflight their complete tool list plus `rtl_test`. Mutable labels such as `latest`,
`main`, and `nightly` are rejected.

Regenerate all four locks with the reviewed uv version and one explicit cutoff date. Change the
cutoff only in a dependency-update change that reviews the resulting diff:

```bash
uv --version  # expected for this lock generation: 0.9.14
uv pip compile constraints/production-universal-core.in \
  --python-version 3.11 --universal --generate-hashes --only-binary :all: \
  --exclude-newer 2026-07-11 -o constraints/production-universal-core.txt
uv pip compile constraints/production-universal-dashboard-nomadnet.in \
  --python-version 3.11 --universal --generate-hashes --only-binary :all: \
  --exclude-newer 2026-07-11 -o constraints/production-universal-dashboard-nomadnet.txt
uv pip compile constraints/production-universal-all-features.in \
  --python-version 3.11 --universal --generate-hashes \
  --exclude-newer 2026-07-11 -o constraints/production-universal-all-features.txt
uv pip compile constraints/production-universal-build.in \
  --python-version 3.11 --universal --generate-hashes --only-binary :all: \
  --exclude-newer 2026-07-11 -o constraints/production-universal-build.txt
```

Build and smoke-test the artifact on Bookworm/Python 3.11 with isolation disabled only after the
locked build environment has been installed, then run the same locks and artifact through the
Noble/Python 3.12 ARM64 systemd gate:

```bash
python3.11 -m venv /tmp/reticulumpi-release-venv
/tmp/reticulumpi-release-venv/bin/pip install --require-hashes --only-binary :all: \
  -r constraints/production-universal-build.txt
/tmp/reticulumpi-release-venv/bin/python -m build --no-isolation
/tmp/reticulumpi-release-venv/bin/twine check dist/*
/tmp/reticulumpi-release-venv/bin/python scripts/verify_wheel.py dist/*.whl \
  --requirements constraints/production-universal-dashboard-nomadnet.txt
```

The canonical production install archive contains that qualified prebuilt wheel, so the target
does not need a Python build backend. The signed unpacked-source compatibility path instead runs
`python -m pip wheel --no-deps`; before using it, pip's isolated build resolver must have reviewed
offline access to the exact build prerequisites declared by `pyproject.toml` and locked in
`constraints/production-universal-build.txt`. Do not resolve them from the network during a
maintenance window. Prefer the install archive whenever it is available.

CI uploads this wheel once. Each architecture-specific Docker build downloads and consumes that
artifact; `docker/Dockerfile` never builds a second wheel. The local `make package-wheel` target
provides the same prerequisite for Docker/Compose development builds.

The ARM64 Bookworm gate also builds `docker/arm64-all-features-ci.Dockerfile` from that wheel. It
installs the complete all-features lock with pip hash checking, runs `pip check`, and verifies
the hardware integration distributions are installed. This disposable validation image may
contain a compiler for reviewed source distributions; no compiler is copied into production.
The digest-pinned `docker/noble-systemd-ci.Dockerfile` independently exercises fresh installation,
recovery, readiness failure, rollback, and both supported install roots on Ubuntu 24.04/Python
3.12. Its additional production-shaped bridge starts from mutable `/opt` code, service-home state,
legacy rnsd/watchdog units and sudoers, and a legacy MeshChat configuration whose install and
storage paths both name that mutable layout. A reviewed MeshChat stub is independently pre-staged as
a root-owned immutable `/srv/reticulumpi-external/meshchat` tree; the bridge never promotes the
mutable checkout into that trust domain. It installs the exact production feature selection from
the all-features hash lock into an immutable `/srv` release, atomically rewrites MeshChat's
`install_dir` and `storage_dir`, verifies the persisted Noble platform profile plus identity and
storage continuity, and qualifies the schema-1 manifest for that immutable tree and safe fixtures
for `rtl_test`, `dump1090`, `rtl_fm`, and `rtl_power`. The gate computes those values through the
installed recovery administrator, runs a dependency-free database plan/dry run, starts only a
signal-aware MeshChat stub through the packaged launcher, runs packaged config preflight without
opening SDR hardware, and proves unrelated dependent services were not restarted. Finally,
`rollback --to legacy` restores the exact configuration and storage while verifying the retained
legacy code and virtual environment were never changed. Release promotion requires both systemd
lanes. Real MeshChat, radio reception, USB behavior, and RF output remain hardware-in-the-loop gates.

## ARM64 install-bundle contract

The production administrator accepts
`reticulumpi-install-arm64-<VERSION>.tar.gz`. The archive sits beside an outer
global release `SHA256SUMS` and `SHA256SUMS.minisig`; the manifest contains an entry naming
the archive by basename (alongside the other release assets) and is verified with the
installed root-owned release key before extraction.

The gzip tar contains exactly one `reticulumpi-<VERSION>/` root and only regular files and
directories. Links, devices, FIFOs, traversal, multiple roots, more than 20,000 files, or more
than 2 GiB of declared file data are rejected. The root contains:

```text
reticulumpi-<VERSION>/
  bundle.json
  reticulumpi-<VERSION>-py3-none-any.whl
  pyproject.toml
  src/
  systemd/
  config/
  scripts/
  constraints/production-universal-core.txt
  constraints/production-universal-dashboard-nomadnet.txt
  constraints/production-universal-all-features.txt
  SHA256SUMS
  SHA256SUMS.minisig
```

`bundle.json` is a JSON object with `schema: 1`, `kind: "reticulumpi-install"`, the exact
filename/project `version`, `architecture: "arm64"`, and `wheel` set to the safe basename of
the one prebuilt wheel in the root. The administrator copies that wheel and installs it with
`--no-deps`; it never rebuilds the source in an install archive. The inner signed hash manifest must
exactly cover every source-root file other than the manifest and its signature. Thus the outer
signature authenticates the archive bytes and the inner signature remains valid after safe
extraction. Publication CI must build this layout from the frozen tag, verify both signatures,
run the administrator's dry run against the exact archive, and complete the apply/rollback test
with that archive during hardware qualification. The Bookworm fixture separately runs every
transactional apply/failure/recovery regression before candidate signing.

An operator must also perform a filesystem-capacity gate before apply. The administrator's apply
path rejects less than `max(256 MiB, 2 * bundle bytes + 2 * current /var/lib bytes)` free on the
filesystem containing the install root, but that is a minimum corruption-prevention check, not a
complete capacity planner. The dry run does not perform it. Manually include the expanded candidate
virtual environment, private input snapshot, every discovered legacy and canonical state backup,
temporary SQLite clones, and source-build workspace when the compatibility source path is used.
Check the install, backup, state, and temporary filesystems separately when they are distinct mounts.

## Protected tag publication

The `release-candidate` and `release` jobs in `.github/workflows/ci.yml` run only for a strict
release tag and depend on every static-analysis, browser, performance, Bookworm/systemd, test,
coverage, package, and container job. They consume the wheel, sdist, CycloneDX SBOM, and both
architecture-specific Docker save archives produced by those jobs. Neither invokes a package
or container build.

Configure repository variables `RELEASE_TAG_PUBLIC_KEY`, `RELEASE_TAG_FINGERPRINT`, and
`MINISIGN_PUBLIC_KEY` with the trusted OpenPGP tag-signing key/fingerprint and independently
distributed Minisign release public key. The read-only `release-tag-trust` job uses only pinned
checkout plus system Git/GPG commands; it does not execute repository code before verifying the
tag. An unsigned tag or a tag from any other signer cannot reach the privileged Bookworm fixture
or candidate-signing job.

Configure the GitHub `release-signing` environment with the passwordless Minisign secret
`MINISIGN_SECRET_KEY`. Restrict it to release tags and signing owners. This environment has only
read-only repository permission; it can produce a signed workflow artifact but cannot publish a
release or container.

Configure the separate GitHub `release` environment before enabling publication:

- require approval from the release owners after the exact workflow artifacts have passed the
  Pi 5 and representative-device qualification;
- prevent unreviewed branches or non-release tags from deploying to the environment.

Generate the CI signing key once on a trusted, offline workstation with the pinned Minisign
version and `minisign -G -W`. Protect the secret as a release credential, retain an offline
recovery copy, and publish the public key independently. The workflow writes the secret only
to a mode-`0600` runner file, passes `-W` to every signing operation, never prints the key, and
deletes the runner file on exit. A password-protected interactive key is intentionally
incompatible with this non-interactive lane. The `release` environment receives no signing
secret.

The gated workflow then:

1. imports only the configured tag public key into an isolated temporary keyring, runs
   cryptographic `git verify-tag`, and requires the configured full signer fingerprint;
2. in the read-only signing job, assembles the deterministic ARM64 install bundle from the exact
   validated sdist and wheel;
3. verifies the install bundle's inner signature and exact-tree manifest;
4. stages the exact wheel, sdist, SBOM, install bundle, and AMD64/ARM64 image archives;
5. writes and verifies one global `SHA256SUMS` plus `SHA256SUMS.minisig`, runs the administrator
   dry run, and uploads one immutable signed-candidate workflow artifact;
6. pauses at the protected `release` environment while operators test that exact signed
   candidate and attach the hardware/soak record;
7. after approval, downloads the same workflow artifact and re-verifies its Minisign signature
   and every SHA-256 digest;
8. loads the two validated image archives, refuses existing version tags, pushes per-architecture
   tags, and creates the versioned multi-architecture GHCR manifest without rebuilding;
9. creates GitHub artifact provenance, wheel SBOM, and registry image attestations; and
10. creates the immutable GitHub release with generated notes, verification instructions, the
   exact assets, and the promoted image digest.

The publication script fails closed if the registry cannot prove all three version tags are
absent. The GitHub release step likewise refuses an existing release. Never delete and reuse a
failed version; fix the candidate and create a new signed tag. Enable GitHub immutable releases
for the repository so published tags and assets cannot later be replaced.

Download all release assets into one directory and verify them with the independently obtained
public key before extraction or installation:

```bash
minisign -Vm SHA256SUMS -x SHA256SUMS.minisig -p /usr/share/reticulumpi/release.pub
sha256sum --check --strict SHA256SUMS
gh attestation verify reticulumpi-<VERSION>-py3-none-any.whl \
  --repo OWNER/reticulumPi
gh attestation verify oci://ghcr.io/OWNER/reticulumpi:<VERSION> \
  --repo OWNER/reticulumPi
```

The public key must not be copied from the release it is being used to authenticate.

Each candidate receives
`docs/release-verification/<version>.md` containing artifact hashes, CI links, hardware
inventory, identity continuity, migration/rollback result, soak metrics, and approvals.

Never replace a published artifact or registry tag under an existing version.
