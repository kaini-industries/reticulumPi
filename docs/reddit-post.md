**Title:** New public transport node — ReticulumPi (107.208.177.42:4242) + seeking help with link establishment

Hey everyone! I've set up a Raspberry Pi 5 as a public Reticulum transport node. If you'd like to peer directly:

```ini
[[ReticulumPi]]
  type = TCPClientInterface
  enabled = yes
  target_host = 107.208.177.42
  target_port = 4242
```

**Services running:**
- Transport relay (enable_transport = True)
- NomadNet page server
- MeshChat
- LXMF message echo + info bot
- I2P interface: `oeoszkbihene4sxjbgi43qljxlsqvjj5suxhl3bjh6uanadquxdq.b32.i2p`

**Also seeking help:** I'm experiencing an issue where link establishment fails through TCP hubs. Announces and path resolution work fine (6000+ known destinations), but every link attempt times out — probes sent via the hub never get responses back. Loopback links work perfectly. I've tested across 5 different hubs (stoppedcold, beleth, dismail, liamcottle, quixote) with the same result.

I've filed a detailed discussion on GitHub: [link to your discussion]

Has anyone successfully established links to remote NomadNet nodes through TCP hubs? Would love to compare configurations. Running RNS 1.1.4 on Pi 5.
