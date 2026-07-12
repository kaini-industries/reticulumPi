> **Historical / non-normative.** This community-directory draft may contain stale public
> endpoints and capabilities. It is not a supported peering configuration; current security
> and installation documentation takes precedence.

Add this row to the Community Node List table:

| ReticulumPi | `107.208.177.42` | 4242 | Yes | `oeoszkbihene4sxjbgi43qljxlsqvjj5suxhl3bjh6uanadquxdq.b32.i2p` | NomadNet, MeshChat, LXMF, Transport |

Config snippet for peering:

```ini
[[ReticulumPi]]
  type = TCPClientInterface
  enabled = yes
  target_host = 107.208.177.42
  target_port = 4242
```
