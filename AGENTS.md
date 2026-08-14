# ReticulumPi Agent Working Agreement

## Scope and precedence

This file applies to the entire repository. A more specific `AGENTS.md` may add or narrow rules
for its own subtree.

The current user request and all system, developer, safety, sandbox, and tool-approval rules take
precedence over this file. This file records standing repository-owner authorization; it does not
grant credentials, bypass a protected environment, or override an approval prompt imposed by the
platform.

For release and production work, treat `docs/release-process.md` and
`docs/hardware-validation.md` as normative. Do not revive older Git-pull deployment guidance from
historical notes, compatibility documentation, or `.claude/commands/deploy.md`.

## Project priorities

- Preserve ReticulumPi as a portable, fully functional on-grid or off-grid multi-radio node.
- Protect stable device identity, exclusive serial ownership, graceful recovery, and bounded
  shutdown across Meshtastic, MeshCore, GPS, Reticulum, and related radio paths.
- Keep security and recovery behavior fail-closed. Do not weaken checks merely to make a test,
  workflow, device, or release pass.
- Preserve user changes, production data, release evidence, and rollback material unless the user
  explicitly authorizes their destruction.

## Standing authorization

For work that is within the user's requested scope, the repository owner grants standing
authorization for the actions below. Do not pause for another human confirmation solely because
one of these routine actions is needed. This authority never converts an explicitly read-only,
review-only, diagnostic, or planning request into authorization to edit or publish changes.

### Local investigation and implementation

- Read repository files, Git history, logs, test output, and ignored-file metadata. Never print or
  disclose secret values.
- Make scoped source, test, documentation, configuration-example, workflow, and tooling changes.
- Install or use development dependencies in an isolated project environment and create disposable
  task-specific files under a safe temporary directory.
- Run focused tests, the full test suite, linters, format checks, package builds, documentation
  checks, browser tests, and non-privileged container checks in proportion to the change.
- Remove only disposable scratch output created by the agent during the current task. This does not
  authorize cleanup of user data, caches, evidence, packages, images, volumes, or prior work.

### Branches, commits, pushes, and pull requests

- Fetch remote metadata and create or switch to a non-protected `codex/<description>` branch based
  on the current `origin/main`.
- Stage only the intended paths, create clear commits, and push the current `codex/*` branch without
  force.
- Open and update draft or ready pull requests against `main`; edit their descriptions, add factual
  comments, request review, and address review feedback.
- Monitor the pull request through CI and report whether the exact head commit is ready to merge.

Before staging, inspect `git status` and the complete intended diff. Treat pre-existing or
concurrent changes as belonging to the user or another task. Never silently include them. Do not
rewrite or force-push published history, and do not approve the agent's own pull request.
Even if GitHub does not enforce branch protection, apply these boundaries as repository policy.

### GitHub and CI observation

- Read repository, pull request, issue, workflow, job, check, annotation, environment, deployment,
  release, package, and attestation metadata needed for the task.
- Monitor existing CI runs to a terminal state, inspect logs, and correlate every conclusion with
  the exact repository, event, ref, commit SHA, run ID, and run attempt.
- Download existing Actions artifacts or packages for read-only inspection. Verify their names,
  IDs, digests, provenance, attestations, signatures, manifests, contents, timestamps, and source
  run bindings in a unique temporary directory outside the repository and `~/.codex`.
- Run local, non-publishing verification and vulnerability inspection against downloaded artifacts.
  Do not execute untrusted artifact contents with elevated privileges.

CI monitoring and artifact inspection are read-only authority. Manually dispatching, approving,
rerunning, or cancelling a workflow, uploading or deleting an artifact, or publishing or deleting
a package is not authorized by this section.

An already-configured credential may be used through its intended client for these in-scope,
read-only calls without another prompt. Do not print, copy, export, decrypt, inspect, or otherwise
expose the underlying secret value.

### Read-only production inspection

When the current user request explicitly places the production ReticulumPi device in scope, the
agent may perform non-mutating checks such as reading service status, health endpoints, logs,
versions, device inventory, and redacted configuration structure. Do not expose credentials or
secret configuration values. Any command that can change service, device, package, network,
filesystem, database, or boot state requires the production approval described below.

## Human approval required

Obtain fresh, explicit human approval immediately before any of the following actions. An approval
for one exact action does not silently extend to a later version, run, environment, or target. The
approval request must identify the action and exact affected objects, such as the PR and head SHA,
tag and commit, workflow run and attempt, artifact digest, environment, or production host.

### Protected Git and GitHub actions

- Merge or enable auto-merge for a pull request, or push directly to `main` or another protected
  branch.
- Create, sign, move, push, or delete a Git tag.
- Create, publish, promote, edit, unpublish, or delete a GitHub Release, container/package version,
  release asset, attestation, or release-channel artifact.
- Approve or bypass a protected GitHub environment or release gate, including `release-signing`,
  `release`, and Gate 85.
- Dispatch, rerun, cancel, or delete a workflow or workflow run, including release and signing
  workflows.
- Change repository, organization, Actions, branch-protection, ruleset, environment, reviewer,
  package-visibility, or deployment-policy settings.

An environment setting such as `prevent_self_review: false` is not agent authority. Only the
configured human reviewer may approve a protected release gate.

### Secrets and credentials

- Read, reveal, copy, upload, rotate, revoke, or change a secret, token, password, private key,
  signing key, recovery key, or credential scope.
- Authenticate a new account or device, authorize OAuth/device flow, or change GitHub CLI scopes.
- Place a secret in GitHub, CI, an artifact, a log, the repository, or a production host.

Secret names and the fact that a credential is present may be inspected when necessary, but values
must remain unread and undisclosed without approval. The Minisign private key remains offline and
must never be uploaded to GitHub, committed, copied into a release artifact, or placed on the
production device.

### Production mutation

- Deploy, install, upgrade, downgrade, roll back, migrate, repair, or remove production software or
  data.
- Write production configuration; restart, stop, start, enable, or disable a service; reboot a host;
  reset or power-cycle a radio; change packages, firewall, networking, users, permissions, storage,
  databases, or systemd state.
- Execute an installer, recovery bundle, signed candidate, or other artifact on production.

Production deployment is artifact-only and must not use `git pull`, a source checkout, or mutable
branch state. Approval to inspect production is not approval to mutate it. Approval for a
production transaction and approval for later release promotion are separate; neither implies the
other. If the exact external runbook or required evidence cannot be authenticated, stop before the
first production write.

### Destructive cleanup

- Delete or overwrite tracked work, user files, untracked work of uncertain ownership, Git history,
  branches, tags, releases, packages, artifacts, workflow evidence, logs, signatures, manifests,
  production data, Docker images, containers, volumes, caches, or frozen validation records.
- Run destructive Git operations, force pushes, broad filesystem deletion, Docker pruning, data
  resets, or cleanup whose ownership or reversibility is uncertain.
- Start a privileged container, mount host cgroups or sensitive host paths into a container, or run
  another local operation capable of changing host-level services or persistent state.

When destructive cleanup is approved, first identify the exact objects, preserve required forensic
or rollback evidence, and verify the result without broadening the deletion scope.

## Working method

1. Inspect the current branch, `origin/main`, worktree state, applicable instructions, and relevant
   release or hardware records before editing.
2. Make the smallest coherent change that satisfies the request. Preserve unrelated work and avoid
   opportunistic rewrites.
3. Add deterministic regressions for behavior changes, especially lifecycle, concurrency,
   ownership, recovery, security, release-control, and failure paths.
4. Validate proportionally and report exact commands and outcomes. Fix the underlying issue rather
   than weakening assertions, timeouts, safety checks, or coverage policy.
5. Review the complete diff and secret scan before committing. Stage explicit paths, use a concise
   present-tense commit subject, and push only the task branch.
6. Open or update a PR with a concise Summary, rationale or root cause, Validation, Boundaries, and
   any deferred or hardware-only checks. Monitor CI for the exact head SHA. Stop at merge readiness
   for the human to merge.

## Validation guide

Use repository-native commands where possible:

- Focused Python test: `.venv/bin/pytest tests/test_<area>.py -q`
- Full suite: `make test`
- Serial coverage/debug lane: `make test-serial`
- Lint and formatting: `make lint` and `make format-check`
- Packaging: `make package-check`
- Documentation: `make docs-check`
- Dashboard assets after JavaScript or CSS changes: `npm ci`, `npm run build:dashboard`, then
  `npm run check:dashboard`
- Whitespace integrity: `git diff --check`

Tests must not require live radios, production credentials, or external services unless the user
explicitly authorizes that integration scope. Mock external hardware and networks in the normal
test suite. Do not regenerate committed assets or snapshots merely to silence a failing check.

For higher-risk runtime, installer, recovery, security, dependency, workflow, or release-control
changes, run the relevant focused checks plus the full applicable CI-equivalent lanes. Accurately
report intentionally skipped platform, privileged, hardware, tag-only, or release-only gates; a
green subset is not evidence that an unrun gate passed.

## Release and evidence invariants

These controls are categorical for an active candidate. A chat approval does not authorize an
exception; changing them requires a separate reviewed policy change before candidate work begins.

- `setuptools-scm` and a new signed annotated tag are the sole release-version source. Never edit a
  package version literal or reuse, move, or rehabilitate a failed release version.
- Build once, verify exact digests and attestations, sign offline, qualify the identical artifacts,
  and promote without rebuilding.
- Keep the publication environment waiting until artifact-only hardware acceptance, reboot checks,
  a continuous 72-hour soak, and signed Gate 85 evidence are complete.
- Preserve frozen evidence byte-for-byte. Record corrections, interruptions, or retries in a new
  successor revision that binds its immediate predecessor. This protection includes ignored
  `.codex-*` evidence as well as tracked verification records.
- A cancelled or partially published release is not reusable even if its remote artifacts are later
  removed.
- Release, signing, candidate, and publication workflows are attempt-1-only. Do not rerun a failed,
  cancelled, partially published, or already-qualified release workflow; withdraw the version and
  begin a new successor instead.
- Never claim a gate passed from configuration, intent, or a previous version. Bind evidence to the
  exact version, commit, tag object, workflow run and attempt, artifact ID and digest, signature,
  target device, and observation interval.
