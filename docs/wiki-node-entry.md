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
