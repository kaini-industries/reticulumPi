# Connectivity Guide

Reticulum can communicate over virtually any medium. The Raspberry Pi supports all of these connection methods, and you can enable multiple interfaces simultaneously -- Reticulum automatically meshes traffic across all of them.

## At a Glance

| Connection Method | Hardware Needed | Cost | Range | Best For |
|---|---|---|---|---|
| WiFi/Ethernet (Auto) | Built-in | Free | LAN | Local mesh, getting started |
| TCP Client/Server | Internet connection | Free | Global | Internet gateway, remote nodes |
| RNode LoRa | USB LoRa board (from ~$15) | $15--150 | 1--100+ km | Long-range off-grid mesh |
| RNode Multi | RNode (firmware v1.74+) | $15--150 | Multi-channel | Simultaneous frequencies |
| Serial | USB-serial adapter + radio | $5--50 | Varies | Data radios, laser links, direct wiring |
| KISS TNC | Radio + TNC or sound card | $35--500 | 10--50 km | Amateur radio (VHF/UHF) |
| AX.25 KISS | Same as KISS TNC | $35--500 | 10--50 km | Ham radio with FCC-compliant ID |
| UDP | Network interface | Free | LAN | Bridging VLANs, special topologies |
| I2P | i2pd software | Free | Global | Anonymous, censorship-resistant |
| Pipe | Custom program | Free | Varies | Experimental transports |
| Backbone | Linux TCP | Free | Global | High-throughput transport nodes |

## WiFi and Ethernet (AutoInterface)

Works immediately with no configuration. The Pi's built-in WiFi (`wlan0`) and Ethernet (`eth0`) are automatically discovered. Peers on the same LAN find each other via IPv6 link-local multicast.

This is enabled by default in the example config. For most local setups, this is all you need.

## TCP Client and Server

Connect to remote Reticulum nodes anywhere on the Internet. The **TCP Server** listens for incoming connections (open port 4242 on your router). The **TCP Client** connects outbound to an existing Reticulum transport hub or another Pi node.

Combine with other interfaces to create an Internet gateway -- for example, a Pi with both an RNode radio and a TCP Server bridges local LoRa traffic to the wider Internet-connected Reticulum network.

> **Tip:** Instead of manually configuring TCP Client hubs, enable the [Transport Monitor's auto-discovery](plugins.md#transport-monitor) feature. It automatically maintains a pool of healthy community hub connections, replacing downed hubs and spreading connections across regions -- no manual hub management needed.

## LoRa Radio with RNode

[RNode](https://unsigned.io/rnode/) is an open-source LoRa transceiver designed specifically for Reticulum. It uses raw LoRa modulation (not LoRaWAN) and delivers long-range, low-power wireless mesh connectivity. The Raspberry Pi has no built-in LoRa hardware -- you need an external radio transceiver connected via USB.

### What Hardware to Buy

The cheapest path to LoRa on a Pi is a **LilyGO T-Beam** (~$25) or **Heltec LoRa32 v3** (~$15). These are ESP32-based development boards with LoRa radios built in. You flash them with RNode firmware and plug them into the Pi's USB port -- that's it.

**Recommended boards (sorted by value):**

| Board | Price | Frequency | Notes |
|-------|-------|-----------|-------|
| **LilyGO T3-S3** | ~$15 | 868/915 MHz | Cheapest option, compact, no GPS |
| **Heltec LoRa32 v3** | ~$15 | 868/915 MHz | Built-in OLED display, compact |
| **LilyGO T-Beam v1.1** | ~$25 | 868/915 MHz | GPS, 18650 battery holder, most popular choice |
| **LilyGO T-Beam Supreme** | ~$30 | 868/915 MHz | Upgraded T-Beam with better GPS and SX1262 radio |
| **LilyGO T-Deck** | ~$45 | 868/915 MHz | Built-in keyboard and screen |
| **RAK4631 (WisBlock)** | ~$30--50 | 868/915 MHz | Modular industrial system, very reliable |
| **Heltec LoRa32 v2** | ~$20 | 868/915 MHz | Older but widely available |
| **Unsigned RNode v2.x** | ~$100--150 | 868/915 MHz | Purpose-built for Reticulum, premium build quality |

> **Best starter pick:** LilyGO T-Beam v1.1 (~$25). It has GPS (useful for location-aware plugins), a battery holder for portable use, and excellent community support. Pair it with a 915 MHz antenna (or 868 MHz in EU) for dramatically better range than the stock stubby antenna.

**You also need:**
- A **USB-A to USB-C cable** (most boards use USB-C; some older ones use Micro-USB)
- An **antenna** matched to your frequency band. The stock stub antenna works but a 1/4 wave whip (~$5) or a directional Yagi (~$20) vastly improves range

### Frequency Bands by Region

| Region | Frequency | ISM Band |
|--------|-----------|----------|
| Americas (US, Canada, South America) | 915 MHz | ISM 902--928 MHz |
| Europe, Africa, Middle East | 868 MHz | ISM 863--870 MHz |
| Asia (varies by country) | 433 MHz or 868 MHz | Check local regulations |
| Worldwide (short range) | 2.4 GHz | ISM 2.4 GHz |

No amateur radio license is required for LoRa on ISM bands at legal power levels.

### Flashing RNode Firmware

The boards listed above ship with stock firmware -- you need to flash them with RNode firmware before they work with Reticulum. Do this from the Pi itself:

```bash
# Install the RNode configuration tool
.venv/bin/pip install rnodeconf

# Auto-detect the connected board and flash firmware
.venv/bin/rnodeconf --autoinstall
```

The `--autoinstall` command will:
1. Detect the board type on the USB port
2. Download the correct firmware
3. Flash it to the board
4. Configure default radio parameters

After flashing, the device shows up as `/dev/ttyUSB0` or `/dev/ttyACM0`.

### Connecting to the Pi

1. Plug the flashed RNode into any USB port on the Pi
2. Verify it appears:
   ```bash
   ls /dev/ttyUSB* /dev/ttyACM*
   ```
3. If using the bootstrap install, the `reticulumpi` user already has `dialout` group access for serial devices. For manual installs:
   ```bash
   sudo usermod -aG dialout $USER
   # Log out and back in for the group change to take effect
   ```

### Reticulum Configuration

Uncomment and edit the `[RNode LoRa Interface]` section in the production Reticulum config
(`/var/lib/reticulumpi/.reticulum/config`; a non-service development account may use
`~/.reticulum/config`):

```ini
[[RNode LoRa Interface]]
  type = RNodeInterface
  enabled = yes
  port = /dev/rnode
  frequency = 915000000
  bandwidth = 125000
  txpower = 7
  spreadingfactor = 8
  codingrate = 5
```

**Key parameters:**

| Parameter | Description | Typical Values |
|-----------|-------------|----------------|
| `port` | Stable by-id path or dedicated udev symlink for the RNode | `/dev/rnode`, `/dev/serial/by-id/...` |
| `frequency` | Center frequency in Hz | `915000000`, `868000000`, `433000000` |
| `bandwidth` | LoRa bandwidth in Hz | `125000` (standard), `250000` (faster), `62500` (longer range) |
| `txpower` | Transmit power in dBm | `2`--`17` (check local regulations) |
| `spreadingfactor` | LoRa spreading factor | `7` (fastest) to `12` (longest range) |
| `codingrate` | Forward error correction rate | `5` (4/5), `6` (4/6), `7` (4/7), `8` (4/8) |

**Tuning for range vs. speed:**
- **Maximum range:** `spreadingfactor = 12`, `bandwidth = 62500`, `codingrate = 8` -- very slow (~0.3 kbps) but reaches the farthest
- **Balanced (default):** `spreadingfactor = 8`, `bandwidth = 125000`, `codingrate = 5` -- good range with reasonable throughput (~2.5 kbps)
- **Maximum speed:** `spreadingfactor = 7`, `bandwidth = 500000`, `codingrate = 5` -- shortest range but highest throughput (~11 kbps)

### Range Expectations

| Environment | Antenna | Typical Range |
|-------------|---------|---------------|
| Urban, stock stub antenna | Included | 0.5--2 km |
| Urban, 1/4 wave whip | ~$5 | 2--5 km |
| Suburban/rural, whip antenna | ~$5 | 5--20 km |
| Hilltop/tower, directional Yagi | ~$20 | 20--100+ km |
| Line-of-sight, both ends elevated | Yagi | 100+ km documented |

A real-world test achieved a **15.75 km usable SSH link at 2.6 kbps** using standard RNode hardware.

### Multiple RNodes and Multi-Channel

You can connect **multiple RNode devices** to a single Pi (one per USB port), each on a different frequency. Reticulum will mesh traffic across all of them.

Use a stable `/dev/serial/by-id/...` path or dedicated udev symlink for every physical radio.
ReticulumPi reserves enabled RNS serial interfaces and gives each application gateway exclusive
ownership of its USB parent. A duplicate RNode, Meshtastic, MeshCore, link-tester, or direct-GPS
assignment fails closed instead of letting two libraries contend for one device. Do not use tty
auto-discovery on a multi-radio node. Direct serial configuration for GPS telemetry, the LoRa Link
Tester, MeshCore Gateway, and a standalone MeshCore Observer also rejects kernel-assigned
`/dev/ttyUSBn` and `/dev/ttyACMn` indexes. This validation is not applied to GPSD, MeshCore TCP, or
shared-observer modes because those modes do not open the configured serial device.

Alternatively, boards with RNode firmware **v1.74 or later** support **multi-channel mode** -- a single device operates on multiple frequencies simultaneously:

```ini
[[RNode Multi Interface]]
  type = RNodeMultiInterface
  enabled = yes
  port = /dev/rnode-multi

  [[RNode Multi Interface/Channel 1]]
    frequency = 915000000
    bandwidth = 125000
    txpower = 7
    spreadingfactor = 8
    codingrate = 5

  [[RNode Multi Interface/Channel 2]]
    frequency = 868000000
    bandwidth = 125000
    txpower = 7
    spreadingfactor = 8
    codingrate = 5
```

Firmware is not frozen indefinitely. Qualify each board, bootloader, firmware, region, and modem
configuration as described in [Hardware Validation](hardware-validation.md), then upgrade one radio
at a time with a saved configuration and a known-good recovery image. Runtime watchdog recovery
never flashes firmware automatically.

### Troubleshooting LoRa

| Problem | Solution |
|---------|----------|
| `/dev/ttyUSB0` not appearing | Try a different USB cable (data cables only, not charge-only). Check `dmesg \| tail` for errors |
| Permission denied on serial port | Add your user to the `dialout` group: `sudo usermod -aG dialout $USER` and re-login |
| Very short range | Replace the stock stub antenna with a proper 1/4 wave or Yagi antenna. Ensure the antenna matches your frequency band |
| No peers discovered | Verify both nodes use the same frequency, bandwidth, spreading factor, and coding rate. All parameters must match exactly |
| `rnodeconf --autoinstall` fails | Try specifying the port manually: `rnodeconf --autoinstall /dev/ttyUSB0`. Ensure no other program is using the serial port |

## Serial Interface

Sends raw Reticulum packets over any serial connection. The Pi has a built-in UART on GPIO pins 14 (TX) and 15 (RX), accessible as `/dev/ttyAMA0`. Any USB-serial adapter shows up as `/dev/ttyUSB0`.

**Hardware you can connect:**

| Device | Price | Range | Connection |
|--------|-------|-------|------------|
| **HC-12 radio pair** | ~$5 each | ~1 km | Wire to Pi GPIO UART (3.3V logic) |
| **3DR/SiK radio pair** | ~$30--50 | ~1 km | USB, plug-and-play |
| **Direct wire pair** (Pi-to-Pi) | ~$2 | Same room | GPIO UART cross-wired (TX-RX, RX-TX) |
| **Laser data link** (DIY) | ~$20--50 | Line-of-sight | Serial output to transmitter |
| **Any serial data radio** | Varies | Varies | USB-serial adapter or GPIO UART |

**Pi UART setup:**
1. Enable serial: `sudo raspi-config` > Interface Options > Serial Port > Enable
2. The port appears as `/dev/ttyAMA0`
3. For HC-12 or similar 3.3V serial radios, wire directly to GPIO pins 8 (TX) and 10 (RX) plus ground

**The HC-12 pair (~$10 total) is the cheapest possible radio link** -- wire one to each Pi's UART and you have a ~1 km serial bridge with zero software complexity.

## KISS TNC (Packet Radio)

Connects to [KISS](https://en.wikipedia.org/wiki/KISS_(TNC))-compatible packet radio modems for amateur radio operation on VHF/UHF bands. Requires an **amateur radio license** to transmit.

**What you need:**

| Setup | Hardware | Total Cost | Notes |
|-------|----------|------------|-------|
| **Software TNC** | Pi + USB sound card (~$10) + VHF/UHF radio (~$25 for Baofeng UV-5R) + audio cable | ~$35 | Runs [Dire Wolf](https://github.com/wb2osz/direwolf) on the Pi itself as a software modem |
| **Hardware TNC** | [Mobilinkd TNC4](http://www.mobilinkd.com/) (~$130) + any VHF/UHF radio | ~$155+ | Plug-and-play USB/Bluetooth TNC |
| **Open-source TNC** | [OpenModem](https://unsigned.io/openmodem/) (~$100) + radio | ~$125+ | Open-source packet radio modem, USB |

**Dire Wolf software TNC setup (cheapest):**
1. Install Dire Wolf: `sudo apt install direwolf`
2. Connect a USB sound card to the Pi
3. Wire the sound card audio in/out to your radio's mic/speaker jack
4. Configure Dire Wolf to expose a KISS TCP port
5. Point Reticulum's `KISSInterface` at the Dire Wolf KISS port

**Range:** 10--50 km line-of-sight typical for VHF/UHF. Supports configurable preamble, TX tail, persistence, and slot time for CSMA channel access.

## AX.25 KISS Interface

Same hardware as KISS TNC above, but adds AX.25 protocol framing with mandatory station identification beaconing. **Use this instead of plain KISS when operating on amateur radio frequencies** -- FCC Part 97 (US) and equivalent regulations elsewhere require periodic callsign identification.

Adds some per-packet overhead compared to plain KISS. Only needed for regulatory compliance.

## UDP Interface

Broadcasts Reticulum packets over UDP. Useful for bridging VLANs or network segments where IPv6 multicast (AutoInterface) doesn't work. Not needed on most standard networks where AutoInterface is sufficient.

## I2P Interface (Anonymous Networking)

Connects to the [Invisible Internet Project (I2P)](https://geti2p.net/) for anonymous, censorship-resistant global connectivity. Traffic is routed through multiple encrypted hops so neither endpoint's IP address is exposed.

**Setup on Pi:**
1. Install the I2P router: `sudo apt install i2pd`
2. Start it: `sudo systemctl enable --now i2pd`
3. Uncomment the `[I2P Interface]` section in your Reticulum config
4. On first start, Reticulum generates a persistent I2P address (this can take several minutes)

No port forwarding or public IP address is required.

## Pipe Interface

Bridges Reticulum packets through any external program's stdin/stdout. This is the most flexible interface -- it can wrap netcat tunnels, SSH connections, custom hardware drivers, or any command-line tool that reads and writes binary data.

```
# Example: tunnel Reticulum over an SSH connection
command = ssh user@remote "cat"
```

## Backbone Interface

A high-performance TCP server interface that uses Linux `epoll` for efficient handling of many simultaneous connections. Best suited for dedicated transport nodes that relay traffic for the broader network. Functionally similar to TCP Server but optimized for throughput on Linux systems (the Pi qualifies).

## Combining Multiple Interfaces

A key strength of Reticulum is that you can enable many interfaces at once. For example, a single Pi could run:

- **AutoInterface** for local WiFi/Ethernet peers
- **RNode** for long-range LoRa mesh
- **TCP Server** for Internet-connected nodes
- **I2P** for anonymous global reach

Reticulum automatically routes and meshes traffic across all active interfaces. Enable `enable_transport = True` in your Reticulum config to let the Pi relay traffic between interfaces, turning it into a full transport node.

See `config/reticulum/config.example` for ready-to-use configuration blocks for every interface type. For a safe starting point with only local mesh discovery (no TCP server), use `config/reticulum/config.minimal` instead.
