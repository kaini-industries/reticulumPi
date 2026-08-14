# Hardware Validation

Stable promotion requires Raspberry Pi 5 evidence for both supported production tuples: current
64-bit Bookworm/Python 3.11 and Ubuntu Noble 24.04/Python 3.12. The production-specific acceptance
run must use the same tuple as the deployed node.
Record model/serial identifiers without publishing private Reticulum identities or secrets.

## Fixture

- Raspberry Pi 5, official power supply, active cooling, durable storage
- RNode and a second Reticulum endpoint
- Meshtastic gateway radio
- Dedicated Meshtastic Link Tester radio and a remote ACK-capable Meshtastic endpoint
- MeshCore companion radio; add a second companion when qualifying standalone Observer mode
- Supported RTL-SDR dongle
- USB or serial GPS
- Ethernet/Wi-Fi LAN with controllable internet loss

## Stable serial identities

Do not qualify a multi-radio fixture with `/dev/ttyUSBN` or `/dev/ttyACMN` configuration. Discover
the kernel-provided stable links and record each physical parent before starting ReticulumPi:

```bash
ls -l /dev/serial/by-id/
udevadm info --query=property --name=/dev/ttyACM0 \
  | grep -E '^(ID_VENDOR_ID|ID_MODEL_ID|ID_SERIAL_SHORT|ID_PATH)='
```

Prefer the exact `/dev/serial/by-id/...` name. A dedicated alias is acceptable when an operator
installs and reviews a rule such as this, replacing every example value with the recorded identity:

```udev
SUBSYSTEM=="tty", ATTRS{idVendor}=="239a", ATTRS{idProduct}=="8029", \
  ATTRS{serial}=="0123456789abcdef", SYMLINK+="meshtastic", GROUP="dialout", MODE="0660"
```

Use a distinct rule and alias for `rnode`, `meshtastic`, `meshcore`, `meshcore-observer`,
`lora-link-tester`, and `gps`; never point two aliases at one physical parent. Reload rules and
replug only during an announced maintenance window, then compare `readlink -f` and `udevadm info`
with the signed fixture inventory. For containers, map the stable host path to the same dedicated
alias inside the container. Test re-enumeration by changing kernel index order and prove every
plugin either reacquires its recorded identity or fails closed.

## LoRa firmware compatibility policy

ReticulumPi does not permanently pin Meshtastic, MeshCore, RNode, or other radio firmware.
Device firmware and the host-side protocol library have different compatibility boundaries:

- A release locks its host Python dependencies so its install is reproducible.
- Radio firmware may move forward after the exact board/firmware combination passes this
  hardware sequence. Do not infer compatibility from a successful USB open or cached node data.
- Record the board, USB VID/PID/serial, bootloader, radio firmware, host protocol-library version,
  region, modem preset, and configuration-backup digest for every qualified tuple.
- Upgrade one physical radio at a time. Export its configuration and channel keys first, retain a
  known-good firmware image, and keep another management path to the node.
- Never flash firmware as an automatic recovery action. Runtime recovery is limited to a bounded
  protocol reboot and, when explicitly enabled, a USB bus reset. A human schedules firmware
  changes separately.

Use a table like this in the signed validation record:

| Protocol | Board | Firmware | Host library | Region/preset | Active probe | Reset/reopen | 72h radio soak |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Meshtastic | record | record | record | record | pass/fail | pass/fail | pass/fail |
| Meshtastic Link Tester | record | record | record | record | ACK/NAK | pass/fail | pass/fail |
| MeshCore | record | record | record | record | pass/fail | pass/fail | pass/fail |
| RNode | record | record | record | record | pass/fail | pass/fail | pass/fail |

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
   idempotent re-run and exact ownership. Prove the running module resolves inside the immutable
   release directory and that its fresh virtual environment contains the signed constraint-set
   versions; never accept an editable checkout or a reused legacy virtual environment.
2. N-1 upgrade, injected failure, automatic rollback, manual rollback, reboot, and unchanged
   identity hash. Include the service account's prior passwd-home as a legacy migration input:
   migrate its RNS configuration, identity, and plugin state to `/var/lib/reticulumpi` before
   starting units protected by `ProtectHome=true`, then prove content digests and ownership.
3. Service readiness within 120 seconds and shutdown within 60 seconds.
4. RNode announce/receive and authenticated file transfer to the second endpoint. The packaged
   `rnsd-watchdog` checks the shared-instance socket, not physical-radio responsiveness;
   `rnstatus --json` plus a correlated exchange with the second endpoint is the RNode liveness
   gate. Record counters before and after the exchange rather than accepting `status=true` alone.
5. Exercise every installed LoRa protocol and record its firmware tuple:
   - Meshtastic: verify a correlated local metadata response, then hold the serial response path
     silent while continuing MQTT traffic. The watchdog must detect the physical-radio failure;
     MQTT traffic and cached node information must not refresh physical health.
   - Through an authenticated dashboard session, capture `/api/meshtastic/device` and verify the
     reported board/firmware tuple matches the independently recorded physical radio.
   - In MQTT mode, reject bad credentials and a withheld CONNACK without publishing readiness.
     For a legacy node-number-only storage directory, record the old number, prove the first start
     rotates it exactly once before publication, and verify `meshtastic_node_num`,
     `meshtastic_packet_ids.json`, and its stable lock are owner-only. Capture packet IDs on both
     sides of two process restarts and prove they are nonzero and disjoint. A corrupt or mismatched
     state member must fail closed; subsequent clean restarts must retain the enrolled identity.
   - Make a soft reboot request and prove that no recovery event is emitted until a new serial
     generation opens and answers a correlated metadata request. Repeat with the optional USB bus
     reset. Confirm the configured reopen delay is honored.
   - Restart ReticulumPi between reset attempts and confirm the durable hourly circuit breaker
     retains its count. Corrupt its state file and move the wall clock backwards; both cases must
     fail closed without issuing another reset.
   - Replace the radio with a different USB identity at the same path. The node must refuse to
     reset or claim it until an operator deliberately updates the expected identity.
   - MeshCore: suppress device-query responses, verify consecutive-failure hysteresis, bounded
     reconnect, unplug/replug recovery, rejection of error-shaped `DEVICE_INFO` responses, and
     continued retry while the internet is unavailable. Exercise Gateway, shared Observer, and a
     standalone Observer radio; prove every standalone reopen revalidates the physical identity.
   - Link Tester: use its dedicated radio to record correlated ACK, NAK, and timeout outcomes.
     Inject an immediate serial exception and a hung send, then prove the old generation closes and
     a new identity-validated generation resumes probes. Unplug/replug it while Meshtastic Gateway
     traffic continues and prove neither plugin receives the other's packets. Confirm a negative
     count is rejected and exactly zero is accepted only as an explicit unlimited test.
   - RNode and all other configured serial consumers: attempt a duplicate serial-device claim and
     confirm startup refuses the conflict instead of allowing two protocol stacks to race the same
     file descriptor. Include RNS `RNodeInterface`, `SerialInterface`, `KISSInterface`, and
     `WeaveInterface` configurations using quoted values, inline comments, and valid unindented
     ConfigObj keys.
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
11. Wait at least one collection interval for every enabled database-backed plugin and assert the
    journal contains no `sqlite3.ProgrammingError`, especially after background tracker work.

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
