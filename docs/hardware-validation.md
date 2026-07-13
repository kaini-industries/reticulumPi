# Hardware Validation

Stable promotion requires Raspberry Pi 5 evidence for both supported production tuples: current
64-bit Bookworm/Python 3.11 and Ubuntu Noble 24.04/Python 3.12. The production-specific acceptance
run must use the same tuple as the deployed node.
Record model/serial identifiers without publishing private Reticulum identities or secrets.

## Fixture

- Raspberry Pi 5, official power supply, active cooling, durable storage
- RNode and a second Reticulum endpoint
- Meshtastic radio
- Supported RTL-SDR dongle
- USB or serial GPS
- Ethernet/Wi-Fi LAN with controllable internet loss

## Qualification sequence

After the first, `release-signing` environment gate has assembled the candidate, leave the
separate `release` environment unapproved. Download
`signed-release-candidate-vMAJOR.MINOR.PATCH` from that exact waiting release workflow run. Record
the signed tag commit plus the source CI, global-signing-request, and release workflow run IDs and
attempts. Verify the global Minisign manifest with the independently provisioned release key and
run `tools/offline_release.py verify-candidate` with the recorded repository/tag/commit/source-run
and candidate-run bindings. Record every digest and use only those files for the sequence below.
Do not rebuild the wheel, install archive, recovery packages, or container images on the fixture.

1. Fresh default (`/srv/reticulumpi`) and explicit `/opt/reticulumpi` compatibility installs;
   idempotent re-run and exact ownership.
2. N-1 upgrade, injected failure, automatic rollback, manual rollback, reboot, and unchanged
   identity hash.
3. Service readiness within 120 seconds and shutdown within 60 seconds.
4. RNode announce/receive and authenticated file transfer to the second endpoint.
5. Meshtastic connect, timeout, reconnect, and unplug/replug.
6. RTL-SDR contention/preemption, decoder failure/restart, and unplug/replug.
   For ACARS, AIS, ISM, radiosonde, SAME weather, and FM reception, verify that each failed
   process group is fully stopped and its lease is absent throughout backoff. Introduce a
   competing higher-priority slot before the retry expires and confirm the decoder does not
   relaunch until the scheduler grants a new ownership generation.
   For NOAA APT, inject failure in both `rtl_fm` and recorder stages during a scheduled pass.
   The entire capture process group must stop, the SDR lease must be released exactly once,
   and the partial pass must be marked failed without restarting mid-pass or decoding a
   truncated recording. A later scheduled pass may acquire a fresh generation normally.
7. GPS acquisition, loss, and reacquisition; NTP state follows each transition.
8. Online, forced-offline, and captive-portal transitions; hash
   `/etc/reticulumpi/config.yaml` before and after and confirm only the narrow runtime overlay
   under `/var/lib/reticulumpi` changes.
9. Dashboard LAN/TLS/auth/offline operation and NomadNet scoped-token access.
10. Container recreation with unchanged identity and durable state.

## Soak acceptance

Run the exact candidate for 72 hours. The result fails for any failed unit, orphan process,
reachable stopped destination, database integrity error, OOM kill, steadily increasing thread
or descriptor count, or more than 10% RSS growth after warm-up. Record temperatures,
throttling, reboots, plugin restarts, database checks, and resource high-water marks in the
release-verification file.

Hardware-only checks cannot be certified by CI. Until a human completes and signs this record, the
artifact remains a release candidate. Release owners approve the already-waiting protected GitHub
`release` environment only after attaching the signed record; the publication job then downloads
and re-verifies the artifact from its own unchanged workflow run. Never start a replacement
publication run after qualification because that would create a different workflow-artifact
identity.
The CI precursor for these installation checks runs in an ARM64 Bookworm systemd container. It
uses ephemeral Minisign keys solely to prove the production verification path, applies signed
fixture releases, injects both an interrupted pre-backup transaction and a service-start failure,
and checks recovery/rollback plus identity continuity. It is not Pi hardware evidence and does
not replace the reboot or peripheral qualification above.
