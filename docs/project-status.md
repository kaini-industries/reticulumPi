# Project Status and Recovery Roadmap

Last verified: **2026-09-08**

This document is the tracked resume point for ReticulumPi development. It is not release evidence,
does not replace the immutable records under `docs/release-verification/`, and grants no production,
signing, reboot, Gate 85, or publication authority.

## Executive status

ReticulumPi has a substantial, healthy software baseline. The v0.3.7 exact-tag CI job passed 5,042
tests with one Minisign-dependent skip. The
[generated code reference](generated-code-reference.md) inventories the built-in plugins, public
events, and Dashboard routes. The full local test, coverage, lint, documentation, Dashboard,
packaging, and Chromium checks pass on the recovery checkout.

Development stalled after the v0.3.7 source freeze. Release qualification became a second,
mostly ignored codebase of one-shot controllers. Setup and diagnostic failures repeatedly required
new successor namespaces. At the start of this recovery, no reusable hardware-in-the-loop (HIL)
or soak runner had been added to the tracked repository. Product development stopped even though
the application test suite was green. This recovery branch adds the first narrow, lab-only RNS and
RNode smoke lane; the broader HIL and soak gap remains.

The recovery strategy is therefore:

1. preserve the v0.3.7 candidate and all historical evidence without weakening its gates;
2. keep new product work on successor branches from `origin/main`;
3. replace bespoke qualification controllers with a small, tracked, repeatable lab harness before
   freezing another candidate; and
4. qualify one core multi-radio vertical slice before expanding the feature surface again.

## v0.3.7 release snapshot

This is a status snapshot, not a promotion decision.

| Item | Verified state |
| --- | --- |
| Signed annotated tag | `v0.3.7`, tag object `73d1807a7048553d0cc4e81357c4b78fce6b13a9`, commit `4376a11e2ae2f2b777cbe9b0b45d168f2ded3a23` |
| Exact-tag CI | Run `31830509519`, attempt 1, successful |
| Candidate finalization | Run `31857835143`, attempt 1, successful |
| Release workflow | Run `31858671507`, attempt 1, waiting |
| Candidate assembly | Job `94947949177`, successful |
| Candidate artifact | `signed-release-candidate-v0.3.7`, artifact `9239847203` |
| Artifact digest | `sha256:788ec62bfbe4b2bfd50fc5053a8f8356282d4028897f9ec074337dd9a44961b7` |
| Promotion | Job `94948200059`, waiting at the protected `release` environment |
| GitHub Release | Absent |
| Physical qualification | Not completed for either supported Pi 5 tuple |
| Reboot continuity | Not completed |
| 72-hour soak | Not started |
| Gate 85 | Not completed or approved |

GitHub reports that the candidate artifact expires at `2026-09-14T02:16:37Z`. GitHub also limits
an environment approval wait to 30 days, so the waiting promotion job is expected to fail at about
the same time if it remains unapproved. Under the repository's attempt-1-only and no-rebuild
policy, that failure would withdraw v0.3.7; it must not be worked around with a rerun or premature
approval.

The local R32 trail does not establish production readiness. Its v4 preflight made one SSH attempt
and failed acceptance; v5 and v6 failed before Minisign or network execution; v7 is an inert,
unsealed design whose pinned temporary inputs are no longer present. No R32 Gate 00, deployment,
HIL, reboot, soak, Gate 85, or publication is recorded.

### Immediate v0.3.7 decision

Only resume v0.3.7 qualification if both physical Pi 5 tuples, all required peripherals, the
offline public verification material, approved maintenance windows, and enough uninterrupted time
for both physical passes, their reboot checks, the full 72-hour soak, a signed and authenticated
Gate 85 record, human review, approval, and promotion are available before the GitHub deadline.
Every production write, service change, reboot, protected-environment approval, and release
publication still requires its own explicit human approval.

Otherwise, preserve the evidence and let a separately reviewed withdrawal record close v0.3.7.
Begin the successor only after the repeatable lab qualification path below is tracked and reviewed.

## Capability truth matrix

The statuses intentionally distinguish implementation from real-world qualification.

| Capability | Implemented | Deterministic/CI | Tracked integration | Physical HIL | Exact-candidate production |
| --- | --- | --- | --- | --- | --- |
| Plugin lifecycle, dependencies, cleanup | Yes | Yes | Partial | Not run | No |
| Reticulum IP and RNode interfaces | Yes | Yes | Not run | Not run | No |
| Heterogeneous radios and multiple RNodes | Yes | Yes | No | Not run | No |
| Meshtastic and MeshCore gateways, one instance per plugin type | Yes | Yes | No | Not run | No |
| Multiple instances of the same gateway plugin | No | No | No | No | No |
| Stable serial ownership and recovery | Yes | Yes | No | Not run | No |
| GPS and Internet loss/recovery | Yes | Yes | No | Not run | No |
| SDR scheduling across all SDR consumers | Partial | Partial | No | Not run | No |
| Dashboard | Yes | Yes | Partial | Not run | No |
| Cross-network messaging | Yes | Yes | No | Not run | No |
| Transactional install and rollback | Yes | Yes | Partial | Not run | No |
| Blank-Pi installation without Internet | No complete wheelhouse/appliance | No | No | No | No |
| Signed build-once release flow | Yes | Yes | Partial | Not run | No |
| Repeatable HIL and soak tooling | Partial | Yes | Not run | Not run | No |

`Yes` means the layer is implemented or verified, `Partial` means only a documented subset is
covered, `Not run` means an applicable lane exists but has no recorded execution, and `No` means
the capability or evidence is absent. The tracked integration coverage is currently limited to
container/systemd fixtures, browser fixtures, candidate assembly, and the new opt-in lab smoke
test. No live lab run of that smoke test has been recorded. It covers one authenticated,
non-mutating control path over one named RNode; it does not cover the complete peripheral fixture,
installation, reboot, fault recovery, soak, or an exact candidate.

## Why development stalled

### 1. Qualification mechanics overwhelmed product work

At audit time, all top-level ignored `.codex-*` trees together contained roughly 290 directories,
1,754 files within those trees, and 81 MiB of allocated data; R17-R32 controller and evidence trees
made up the majority. The latest v6 and v7 preflight designs alone total more than 11,000 lines.
None was the tracked, reusable HIL and soak tooling needed by the release process.

### 2. Repeatable diagnostics were treated as one-shot evidence

Candidate identity and promotion must remain one-shot and fail closed. Read-only setup diagnostics
do not need the same lifecycle. Conflating the two turned recoverable path, ordering, and SSH setup
errors into terminal revision forks.

### 3. The software and release state diverged

The tracked v0.3.7 verification record still describes pre-tag readiness, while the tag, CI runs,
signed candidate, and waiting protected job already exist. Ignored local records contain newer
facts that another clone or CI job cannot discover or authenticate.

### 4. Hardware qualification was specified but not productized

`docs/hardware-validation.md` defined a strong manual contract, but `origin/main` had no HIL or soak
command, Make target, lab profile, or machine-readable result validator. The recovery branch now
adds an opt-in, non-production `make test-hil` smoke test and a create-once JSON result. It still
lacks candidate preflight, install, restart, reboot, peripheral fault-recovery, and soak runners.
The normal suite uses local fixtures and subprocesses, but deliberately excludes live radios,
external-network production integration, and production credentials.

### 5. Several headline capabilities remain architectural goals

- The application registry is keyed by one `plugin_name`, so repeated Meshtastic or MeshCore
  gateway instances are not represented.
- ADS-B, spectrum, LoRa scanning, and FM paths do not all participate in the shared SDR scheduler;
  some require dedicated dongles despite broader product language.
- The signed install bundle does not yet contain a complete dependency wheelhouse or native-tool
  payload for installation on a blank disconnected Pi.
- Several advertised plugins lack complete example configuration and operator guidance.

## Recovery milestones

### M0 - Contain and make state visible

- Preserve all historical release evidence and the exact v0.3.7 candidate.
- Keep v0.3.7 production and publication gates closed until their documented evidence exists.
- Develop from current `origin/main` on small `codex/*` branches.
- Maintain this matrix as the single tracked project resume point.

Exit: the v0.3.7 rescue-or-withdraw decision is explicit, evidence is retained, and no ordinary
development depends on ignored controller state.

### M1 - Build a repeatable lab qualification lane (started)

- Retain the new candidate-agnostic, non-mutating remote-API RNS/RNode smoke command with stable
  redacted result codes as the first repeatable vertical slice.
- Expand it into a complete candidate preflight without granting release-evidence authority to
  ordinary lab rehearsals.
- Add tracked fixture inventory and result schemas for both supported Pi tuples.
- Add a repeatable core integration command and a checkpointed soak recorder.
- Keep exploratory diagnostics retryable; create authoritative one-shot evidence only after the
  repeatable lane passes.
- Bind final evidence to the exact tag, commit, run attempts, artifact ID/digest, fixture IDs, and
  observation interval.

Exit: a clean lab Pi can run preflight repeatedly, failures are actionable, and a passing run can
be converted into immutable candidate evidence without bespoke source generation.

### M2 - Qualify the core vertical slice

Run RNode, one Meshtastic radio, one MeshCore radio, GPS, and Auto/TCP connectivity concurrently.
Prove bidirectional messaging, Internet loss and return, radio loss and reacquisition, serial
exclusivity, bounded shutdown, identity continuity, reboot recovery, and bounded CPU, memory,
threads, descriptors, queues, and database growth.

Exit: the same tracked procedure passes on Bookworm/Python 3.11 and Noble/Python 3.12 Pi 5 hosts.

### M3 - Make same-type gateway instances and scale a first-class contract

- Add stable instance IDs and configuration for multiple gateways of the same plugin type while
  preserving the existing support for heterogeneous radios and multiple RNodes.
- Define dependency and Dashboard semantics for one-to-many plugin instances.
- Set explicit supported limits for radios, Reticulum interfaces, Internet hubs, nodes, message
  rate, threads, descriptors, memory, and CPU.
- Add deterministic contention, disconnect, restart, and fairness tests before physical scale HIL.

Exit: the documented scale target passes synthetic load and a representative physical fixture.

### M4 - Unify SDR ownership

Move every shareable RTL-SDR consumer behind `SdrScheduler`, or explicitly declare and validate a
dedicated-dongle requirement. Add multi-dongle arbitration, priority, preemption, and recovery HIL.

Exit: product claims, configuration, scheduler behavior, and physical tests agree.

### M5 - Make installation genuinely off-grid

Produce a signed dependency wheelhouse or appliance image for each supported Pi tuple. Define the
trust and update path for required native radio tools and optional MeshChat assets. Test fresh
install, recovery, update, rollback, and identity preservation with the Internet physically absent.

Exit: a blank supported Pi can reach a working core node from only signed offline media.

### M6 - Finish operator experience and release a successor

- Add complete example sections and validation for every supported plugin.
- Ship reviewed minimal-off-grid, multi-radio, hybrid-Internet, and full-lab profiles.
- Complete both physical tuple passes, reboot checks, a 72-hour soak on the designated tuple, and
  signed Gate 85 evidence against one unchanged successor candidate.
- Promote only that exact candidate through the protected release environment.

## Development discipline

- Keep product changes small enough to qualify independently; avoid cross-cutting release batches.
- Every behavior change receives deterministic failure-path coverage.
- Track `implemented`, `mock-tested`, `integration-tested`, `HIL-tested`, and
  `production-qualified` separately.
- Store reusable tools and schemas in Git. Store private raw evidence outside Git, with a redacted
  tracked summary that identifies its authenticated archive.
- Never weaken identity, serial ownership, recovery, privilege, signature, or publication checks
  to make a lane pass.
- Do not freeze the next candidate until M1's repeatable preflight passes on the intended fixtures.
