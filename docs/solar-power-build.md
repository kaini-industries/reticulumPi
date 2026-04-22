# Solar Power Build for ReticulumPi

Reference document for running the ReticulumPi node off-grid using solar + battery storage.

> **Status:** Planning / reference. Not yet deployed. Verify current product specs before purchasing.

---

## Power Consumption Estimate

Based on the hardware inventory as of 2026-04-19: Raspberry Pi 5, 3× RAK 4631 boards (RNode / Meshtastic / MeshCore), GlobalSat BU-353N GPS, RTL-SDR dongle, powered USB hub.

| Component | Typical Draw |
|---|---|
| Raspberry Pi 5 (this workload) | ~5 W |
| 3× RAK 4631 (idle RX, brief TX spikes) | ~0.8 W |
| GlobalSat BU-353N GPS | ~0.4 W |
| RTL-SDR (spectrum_scanner running) | ~1.3 W |
| Powered USB hub + SD storage | ~0.8 W |
| Conversion losses (5V buck, ~90%) | ~0.9 W |
| **Total continuous** | **~9–10 W** |

- **Peak draw** (concurrent LoRa TX + SDR scans): ~13–14 W
- **Idle draw** (SDR disabled): ~6–7 W
- **Daily energy:** ~240 Wh/day (10 W × 24 h)

> **Real-world measurement recommended:** plug Pi USB-C input through an inline meter (UM34C, PZEM-017, or similar) and log 24 h. Estimates are within ±2 W but battery sizing compounds errors.

---

## Sizing Targets

Assuming a temperate climate (NL/UK/DE latitudes, matching the project's Amsterdam/Dublin transport hub references):

**Peak Sun Hours (PSH):** ~3.5 summer, ~0.8–1 winter, ~2.5 annual average.

**Panel sizing:**
- Summer-only / fair-weather: ~100 W
- Year-round temperate: **300–400 W** (winter-sized ×1.5–2 for losses)
- Sunny climates (ES, AZ, CA): 150–200 W sufficient

**Battery sizing (LiFePO₄, 80% DoD, 3-day autonomy):**
- Minimum: 12 V × 60 Ah = 720 Wh usable
- Comfortable: 12 V × 100 Ah = ~1 kWh usable, ~4 days autonomy

---

## Build Options Compared

### Option 1 — Plug-and-play (~$900–1,400)

All-in-one power station + portable panel. Simple, no wiring.

| Item | Options | ~USD |
|---|---|---|
| Power station | EcoFlow Delta 2 (1024 Wh) / Bluetti AC180 (1152 Wh) / Anker Solix C1000 (1056 Wh) | $700–900 |
| Solar panel | EcoFlow 220W bifacial / Jackery SolarSaga 200W | $400–500 |

**Trade-off:** Station's inverter idles at 5–10 W even on DC loads — that's up to 50% of our 10 W total budget. Use DC-only / Eco mode to minimize.

### Option 2 — DIY balanced (~$550–750, recommended)

No inverter losses — Pi runs natively on 12→5V DC. Best watts-per-dollar for 24/7 10W load.

| Item | Product | Spec | ~USD |
|---|---|---|---|
| Battery | LiTime 12V 100Ah LiFePO4 | 1280 Wh, BMS built-in | $280–330 |
| Charge controller | Victron SmartSolar MPPT 75/15 | Bluetooth logging | $100 |
| Solar panel | Renogy 200W mono rigid (or 2× 100W) | | $150–200 |
| DC-DC converter | Pololu / DROK 12V→5V 5A USB-C | | $15–25 |
| Wiring + 10A fuse + MC4 + mounts | — | | $40–60 |

Optional: **Victron SmartShunt** (~$100) for battery SoC monitoring via BLE — valuable for a headless off-grid node.

### Option 3 — Premium DIY (~$900–1,200)

For remote/unattended deployment.

| Item | Product | ~USD |
|---|---|---|
| Battery | Battle Born 100Ah / Renogy 12V 100Ah Smart | $500–900 |
| Controller | Victron SmartSolar MPPT 100/30 | $200 |
| Panel | 2× Newpowa 200W mono (400W total) | $250–350 |
| DC-DC | Meanwell SD-15B-5 sealed | $30 |
| Enclosure | IP55 weatherproof box | $50–100 |

---

## Panel Comparison: EcoFlow vs Jackery vs Rigid

| Factor | EcoFlow | Jackery | Rigid (Renogy/Newpowa) |
|---|---|---|---|
| Connector | MC4 standard | Proprietary DC8020 (adapter included) | MC4 standard |
| Bifacial option | Yes (220W) | No | Yes (select models) |
| IP rating | IP68 on 400W | IP65 junction box | IP67 typical, framed glass |
| Portability | Foldable | Foldable | Fixed mount |
| $/W | Better | Premium | **Best** |
| Warranty | 12 mo (extendable) | 3–5 yr | 10–25 yr |

**For a fixed install:** rigid panels win on $/W and lifetime. Foldable EcoFlow/Jackery only make sense if the node will be moved.

---

## Selected Build — Hybrid: Anker Solix C1000 + Renogy 250W Bifacial N-Type

### ⚠️ Verify Before Ordering

Two compatibility points to confirm on current product datasheets:

- **Renogy 250W Bifacial N-Type Voc:** typically ~37V; cold-weather Voc rises to ~1.15× rated (~42V in winter)
- **Anker Solix C1000 solar input voltage range:** commonly cited as 11–32V / 10A / 300W via XT-60

If the C1000's 32V ceiling is accurate, a single 250W panel will exceed the limit in cold weather. Fallback options:

1. Drop to a **100W or 175W Renogy Bifacial** (lower Voc, within window)
2. Use **EcoFlow Delta 2** instead (accepts up to 60V / 500W solar input)
3. Run a **separate DIY 12V system** for solar charging and keep the C1000 as a backup/switchable power source (effectively running Options 1 and 2 in parallel rather than hybrid)

### Core Components

| Item | Spec | ~USD |
|---|---|---|
| Anker Solix C1000 | 1056 Wh LiFePO4, 1500W AC, 100W USB-C PD, built-in MPPT | $700–800 |
| Renogy 250W Bifacial N-Type | Mono, ~23% front, +up to 25% bifacial gain, MC4 | $180–230 |

### Cables & Connectors

| Item | Purpose | ~USD |
|---|---|---|
| MC4 to XT-60 adapter cable | Panel → C1000 solar input (Anker often includes; verify in box) | $15 (if needed) |
| MC4 extension cables, 10–20ft pair, 10 AWG | Panel location → C1000 | $25 |
| USB-C to USB-C PD cable, 100W rated, 6ft | C1000 → Pi 5 | $15 |
| Short 18 AWG extension | Powered USB hub AC adapter | $10 |

### Protection & Safety

| Item | Purpose | ~USD |
|---|---|---|
| MC4 inline fuse holder + 15A fuse | Between panel and C1000 | $15 |
| Surge protector | Wall AC → C1000 (if grid-charging) | $20 |
| CO / smoke detector | Monitoring the 1500W energy store if installed indoors | $25 |

### Panel Mounting (Bifacial-specific)

Bifacial panels need clearance behind for rear-side light capture. Do not flush-mount to dark roof.

| Item | Notes | ~USD |
|---|---|---|
| Renogy Adjustable Tilt Mount Bracket | 14/21/28/35/45° presets | $75 |
| *or* Pole mount kit (Renogy / EcoWorthy) | Better bifacial yield with sky visible behind | $60–100 |
| Ground anchors + pavers | ballast or augers | $30–50 |
| Light gravel / white EPDM under panel | +10–15% bifacial rear yield | $20–40 |

### Pi 5 Power Path

Two options:

**Path A — Simplest:**
`C1000 USB-C PD (100W) → Pi 5`
Pi may show "low-power" warning if negotiated at 5V/3A. With powered hub handling peripherals, Pi itself stays under 3A. Works fine for this build.

**Path B — Most efficient:**
`C1000 12V car port → DROK 12V→5V 5A USB-C buck converter → Pi 5`
Skips PD negotiation, delivers clean 5V/5A. ~$20 extra.

### Optional / Recommended

| Item | Why | ~USD |
|---|---|---|
| Anker app | Free — live solar yield, SoC, historical charts via BLE/WiFi | free |
| Waveshare UPS HAT C | Brownout safety during C1000 output reconnects | $25 |
| IP65 junction box | Weatherproof panel fuse / wiring | $20 |
| MC4 crimp tool + spare connectors | For custom cable lengths | $35 |

### Total Estimate

- **Essentials only:** ~$1,050–1,200
- **With recommended extras:** ~$1,200–1,400

---

## Expected Performance (Hybrid Build)

| Scenario | Daily Yield | C1000 Autonomy Contribution |
|---|---|---|
| Summer, temperate EU, bifacial bonus | 1.2–1.7 kWh | Full recharge + 900 Wh headroom |
| Winter, temperate EU, worst case | 200–400 Wh | 1–2 grid top-ups/week likely needed |
| Sunny climates year-round | 1.8–2.4 kWh | Complete grid independence |

---

## Deployment Checklist

- [ ] Measure actual 24h power draw with inline USB meter — confirms or corrects the 240 Wh/day estimate
- [ ] Verify Anker Solix C1000 solar input voltage/wattage range on current product page
- [ ] Verify Renogy 250W Bifacial N-Type Voc/Vmp on current datasheet
- [ ] Confirm MC4→XT-60 cable ships with Anker unit (or order separately)
- [ ] Test Pi 5 USB-C PD behavior with the C1000 unit — does it negotiate 5V/5A or fall back to 3A?
- [ ] Select mounting location — ideally south-facing (N hemisphere), unshaded from 9am–3pm, with reflective surface below for bifacial gain
- [ ] Plan fuse/combiner placement — accessible but weather-protected
- [ ] Decide on indoor vs outdoor C1000 location (ventilation, temp range, theft risk)

---

## Future Integration Ideas

- `solar_monitor` plugin polling the Anker app's BLE API for SoC / solar yield
- `system_monitor` extension logging Pi USB-C input voltage to detect brownouts
- Automatic plugin throttling (disable `spectrum_scanner`) when battery SoC < 40% — reclaims ~15% of load

---

*Last updated: 2026-04-20*
