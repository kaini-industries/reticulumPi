> **Historical / non-normative.** This incident-discussion draft records one former node
> configuration and software version. Current installation, troubleshooting, and security
> documentation takes precedence.

## Summary

I'm unable to establish links to any remote destination through TCP hub connections, despite announces and path resolution working correctly. Link requests leave successfully, but link proofs never arrive back. This affects all remote nodes across multiple hubs, suggesting a routing/forwarding issue rather than a destination-specific problem.

I'm hoping someone can help me understand whether this is a configuration issue on my end, a known limitation, or something worth investigating further.

## Environment

- **RNS version:** 1.1.4
- **Platform:** Raspberry Pi 5 (ARM64), Raspberry Pi OS Bookworm, Python 3.12
- **Daemon:** `rnsd` with `enable_transport = True`, `share_instance = True`
- **Application layer:** ReticulumPi + NomadNet connecting via `LocalClientInterface`

## Network Configuration

```ini
[reticulum]
  enable_transport = True
  share_instance = True

[interfaces]

  [[TCP Server Interface]]
    type = TCPServerInterface
    enabled = True
    listen_ip = 0.0.0.0
    listen_port = 4242
    # Port forwarded — externally reachable at 107.208.177.42:4242

  [[TCP Client stoppedcold]]
    type = TCPClientInterface
    enabled = True
    target_host = rns.stoppedcold.com
    target_port = 4242

  [[TCP Client beleth]]
    type = TCPClientInterface
    enabled = True
    target_host = rns.beleth.net
    target_port = 4242

  [[TCP Client dismail]]
    type = TCPClientInterface
    enabled = True
    target_host = rns.dismail.de
    target_port = 7822

  [[I2P Interface]]
    type = I2PInterface
    enabled = True
    connectable = yes
    # i2pd status: OK, not firewalled
```

## What Works

- **Announces flow correctly.** 6000+ known destinations, 130+ active paths through the hubs.
- **Path resolution works.** `rnpath` resolves destinations at 2–5 hops through hub connections.
- **Loopback links work.** Link to our own NomadNet (0 hops) succeeds in 0.3s.
- **TCP connections are bidirectional.** All hub connections show 500KB+ traffic in both directions.

## What Fails

Every link attempt to any remote destination (1+ hops) times out. Consistent across:

- All tested remote destinations (dozens attempted)
- Multiple independent TCP hubs (stoppedcold, beleth, dismail, liamcottle, quixote)
- Freshly learned paths (resolved seconds before the link attempt)
- Paths at various hop counts (2 through 5)
- Tried with and without `mode = gateway` on client interfaces

### `rnprobe` output

```
$ rnprobe -v nomadnetwork.node <destination_hash>
Sent probe 1 (16 bytes) to <dest> via <hub_transport> on TCPInterface[TCP Client beleth/rns.beleth.net:4242]
Probe timed out
Sent 1, received 0, packet loss 100.0%
```

The probe is confirmed sent through the hub — but no response ever arrives.

### `rnsd` debug log

```
[Debug] Path request for <hash> on LocalInterface[rns/default]
[Debug] Answering path request for <hash> on LocalInterface[rns/default], path is known
[Debug] Clamping link MTU to 8.19 KB
[Debug] Trying to rediscover path for <hash> since an attempted local client link was never established
```

The link request was sent, no proof arrived, and rnsd fell back to path rediscovery.

## Analysis

1. **Outbound works:** Link requests and probes leave our node through the TCPClientInterface and reach the hub (confirmed by rnprobe).

2. **Return path is broken:** The remote destination should generate a link proof that travels back through the transport chain to the hub. At the hub, it needs to be forwarded back down our TCPClientInterface connection. This step appears to fail.

3. **Not destination-specific.** The failure is universal across all remote nodes and all hubs. Loopback (0 hops) works perfectly.

4. **Tried extensively:**
   - 5 different TCP hubs
   - `mode = gateway` on client and server interfaces
   - Port 4242 forwarded and externally reachable (verified with `nc`)
   - I2P interface with connectable=yes, i2pd status OK
   - Fresh paths only (dropped stale paths, waited for re-announcement)

## Questions

1. **Is there a required hub-side configuration** (e.g., `mode = gateway` on the hub's TCPServerInterface) that enables bidirectional link proof forwarding? Does the hub need BackboneInterface instead of TCPServerInterface?

2. **Does `enable_transport = True` on the client side affect** how the hub handles return routing for our connection?

3. **Has anyone confirmed working bidirectional link establishment** as a TCPClientInterface client through a public hub? If so, which hub and what configuration?

4. **Is there a way to diagnose the hub's routing table** to verify it has a valid return path for our connection?

## Also: New Public Transport Node

If anyone wants to test direct peering, our node is publicly reachable:

```ini
[[ReticulumPi]]
  type = TCPClientInterface
  enabled = yes
  target_host = 107.208.177.42
  target_port = 4242
```

Running: Transport, NomadNet, MeshChat, LXMF propagation.

Any guidance appreciated — happy to run additional diagnostics or packet captures.
