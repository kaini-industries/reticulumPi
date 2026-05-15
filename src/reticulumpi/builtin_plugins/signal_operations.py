"""Signal operations plugin — detection, classification, correlation, persistence.

Subscribes to events from all signal plugins, maintains a unified contact
model, detects and classifies unknown signals from spectrum sweeps, and
persists observations to SQLite.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase

_SIGOPS_DB_DEFAULT = "~/.local/share/reticulumpi/signal_operations.db"

_SCHEMA_SQL = """\
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS signal_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    freq_hz INTEGER NOT NULL,
    bandwidth_hz INTEGER NOT NULL,
    power_db REAL NOT NULL,
    signal_type TEXT,
    classification_name TEXT,
    confidence REAL DEFAULT 0.0,
    duration_s REAL DEFAULT 0.0,
    source_plugin TEXT,
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_ts ON signal_observations(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_obs_freq ON signal_observations(freq_hz);

CREATE TABLE IF NOT EXISTS signal_baselines (
    freq_bin_hz INTEGER NOT NULL,
    hour_of_day INTEGER NOT NULL,
    day_of_week INTEGER NOT NULL,
    avg_power_db REAL NOT NULL,
    stddev_db REAL DEFAULT 0.0,
    sample_count INTEGER DEFAULT 1,
    updated_at REAL NOT NULL,
    PRIMARY KEY (freq_bin_hz, hour_of_day, day_of_week)
);

CREATE TABLE IF NOT EXISTS contacts (
    id TEXT PRIMARY KEY,
    contact_type TEXT NOT NULL,
    identifier TEXT NOT NULL,
    display_name TEXT,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    observation_count INTEGER DEFAULT 1,
    sources_json TEXT,
    lat REAL,
    lon REAL,
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_ct_type ON contacts(contact_type);
CREATE INDEX IF NOT EXISTS idx_ct_last ON contacts(last_seen DESC);

CREATE TABLE IF NOT EXISTS correlation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    event_type TEXT NOT NULL,
    contact_ids_json TEXT,
    description TEXT,
    sources_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_corr_ts ON correlation_events(timestamp DESC);
"""


@dataclass
class DetectedSignal:
    center_freq_hz: int
    bandwidth_hz: int
    peak_power_db: float
    timestamp: float


@dataclass
class SignalTrack:
    center_freq_hz: int
    bandwidth_hz: int
    peak_power_db: float
    first_seen: float
    last_seen: float
    duration_s: float = 0.0
    observation_count: int = 1
    intermittent: bool = False
    classification: str | None = None
    confidence: float = 0.0


@dataclass
class Contact:
    id: str
    contact_type: str
    identifier: str
    display_name: str = ""
    first_seen: float = 0.0
    last_seen: float = 0.0
    observation_count: int = 0
    sources: set[str] = field(default_factory=set)
    lat: float | None = None
    lon: float | None = None
    distance_nm: float | None = None
    bearing_deg: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _contact_id(contact_type: str, identifier: str) -> str:
    raw = f"{contact_type}:{identifier}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class SignalOperationsPlugin(PluginBase):
    """Signal detection, classification, correlation, and persistence."""

    plugin_name = "signal_operations"
    plugin_version = "0.1.0"
    plugin_description = "Signal operations — detection, classification, correlation"
    broadcast_tier = 2
    broadcast_keys = "sigops"

    def validate_config(self) -> None:
        self._detection_threshold_db = float(
            self.config.get("detection_threshold_db", 10.0),
        )
        self._min_bandwidth_hz = int(self.config.get("min_bandwidth_hz", 5000))
        self._min_duration_s = float(self.config.get("min_duration_s", 1.0))
        self._baseline_alpha = float(self.config.get("baseline_alpha", 0.02))
        self._max_history_days = int(self.config.get("max_history_days", 30))
        self._max_contacts = int(self.config.get("max_contacts", 1000))
        self._stale_contact_timeout = float(
            self.config.get("stale_contact_timeout", 3600),
        )
        self._correlation_interval = float(
            self.config.get("correlation_interval_s", 30),
        )
        self._db_path = os.path.expanduser(
            self.config.get("db_path", _SIGOPS_DB_DEFAULT),
        )
        self._receiver_lat = self.config.get("receiver_lat")
        self._receiver_lon = self.config.get("receiver_lon")
        if self._receiver_lat is not None:
            self._receiver_lat = float(self._receiver_lat)
        if self._receiver_lon is not None:
            self._receiver_lon = float(self._receiver_lon)

    def start(self) -> None:
        self._active = True

        self._baseline_db: dict[int, float] = {}
        self._active_signals: dict[int, SignalTrack] = {}
        self._signals_lock = threading.Lock()
        self._contacts: dict[str, Contact] = {}
        self._contacts_lock = threading.Lock()
        self._correlation_events: deque[dict[str, Any]] = deque(maxlen=200)
        self._detector_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=100)

        self._stats = {
            "signals_detected_total": 0,
            "signals_classified": 0,
            "signals_unknown": 0,
            "contacts_total": 0,
            "contacts_active": 0,
            "correlations_total": 0,
            "observations_persisted": 0,
        }
        self._snapshot_cache: dict[str, Any] = {}
        self._snapshot_dirty = True

        self._signal_db: list[dict[str, Any]] = []
        self._classify_load_db()

        self._db_init()
        self._db_load_contacts()

        self._subscribe_events()

        self._start_thread(self._detection_loop, name="sigops-detector")
        self._start_thread(self._maintenance_loop, name="sigops-maintenance")

    def stop(self) -> None:
        self._active = False
        self._unsubscribe_events()
        self._db_flush()
        self._join_threads(timeout=5.0)

    # ── event subscriptions ─────────────────────────────────────────

    def _subscribe_events(self) -> None:
        sub = self.event_bus.subscribe
        sub_off = self.event_bus.subscribe_offloaded
        sub(events.SPECTRUM_SWEEP, self._on_spectrum_sweep)
        sub_off(events.SIGOPS_SIGNAL_DETECTED, self._on_sigops_detection)
        sub_off(events.ADSB_AIRCRAFT_DETECTED, self._on_adsb)
        sub_off(events.ADSB_AIRCRAFT_LOST, self._on_adsb_lost)
        sub_off(events.ADSB_EMERGENCY_SQUAWK, self._on_adsb_emergency)
        sub_off(events.ACARS_MESSAGE_DECODED, self._on_acars)
        sub_off(events.AIS_VESSEL_DETECTED, self._on_ais)
        sub_off(events.AIS_VESSEL_LOST, self._on_ais_lost)
        sub_off(events.RADIOSONDE_DETECTED, self._on_radiosonde)
        sub_off(events.RADIOSONDE_BURST, self._on_radiosonde)
        sub_off(events.RADIOSONDE_LOST, self._on_radiosonde_lost)
        sub_off(events.WEATHER_ALERT_RECEIVED, self._on_weather_alert)
        sub_off(events.LORA_PEER_ANNOUNCE_RECEIVED, self._on_lora_peer)
        sub_off(events.ISM_DEVICE_DETECTED, self._on_ism_device)
        sub_off(events.ISM_DEVICE_LOST, self._on_ism_lost)
        sub(events.GPS_FIX_UPDATED, self._on_gps_fix)
        sub(events.GPS_FIX_RECEIVED, self._on_gps_fix)

    def _unsubscribe_events(self) -> None:
        unsub = self.event_bus.unsubscribe
        unsub(events.SPECTRUM_SWEEP, self._on_spectrum_sweep)
        unsub(events.SIGOPS_SIGNAL_DETECTED, self._on_sigops_detection)
        unsub(events.ADSB_AIRCRAFT_DETECTED, self._on_adsb)
        unsub(events.ADSB_AIRCRAFT_LOST, self._on_adsb_lost)
        unsub(events.ADSB_EMERGENCY_SQUAWK, self._on_adsb_emergency)
        unsub(events.ACARS_MESSAGE_DECODED, self._on_acars)
        unsub(events.AIS_VESSEL_DETECTED, self._on_ais)
        unsub(events.AIS_VESSEL_LOST, self._on_ais_lost)
        unsub(events.RADIOSONDE_DETECTED, self._on_radiosonde)
        unsub(events.RADIOSONDE_BURST, self._on_radiosonde)
        unsub(events.RADIOSONDE_LOST, self._on_radiosonde_lost)
        unsub(events.WEATHER_ALERT_RECEIVED, self._on_weather_alert)
        unsub(events.LORA_PEER_ANNOUNCE_RECEIVED, self._on_lora_peer)
        unsub(events.ISM_DEVICE_DETECTED, self._on_ism_device)
        unsub(events.ISM_DEVICE_LOST, self._on_ism_lost)
        unsub(events.GPS_FIX_UPDATED, self._on_gps_fix)
        unsub(events.GPS_FIX_RECEIVED, self._on_gps_fix)

    # ── GPS ──────────────────────────────────────────────────────────

    def _on_gps_fix(self, _event_type: str, data: dict) -> None:
        lat, lon = data.get("lat"), data.get("lon")
        if lat is not None and lon is not None:
            self._receiver_lat = float(lat)
            self._receiver_lon = float(lon)

    def _on_sigops_detection(self, _event_type: str, data: dict) -> None:
        source = data.get("source", "")
        if source == "spectrum_scanner":
            return
        sig = DetectedSignal(
            center_freq_hz=int(data.get("freq_hz", 0)),
            bandwidth_hz=int(data.get("bandwidth_hz", 0)),
            peak_power_db=float(data.get("power_db", 0.0)),
            timestamp=data.get("timestamp", time.time()),
        )
        name = data.get("signal_type", data.get("type", ""))
        conf = float(data.get("confidence", 0.0))
        if sig.center_freq_hz > 0:
            self._db_save_observation(sig, name, conf, source=source)
        self._stats["signals_detected_total"] += 1
        self._snapshot_dirty = True

    # ── detection engine ─────────────────────────────────────────────

    def _on_spectrum_sweep(self, _event_type: str, data: dict) -> None:
        if not self._active:
            return
        try:
            self._detector_queue.put_nowait({
                "bins_hz": data.get("bins_hz"),
                "powers_db": data.get("powers_db"),
                "timestamp": data.get("timestamp", time.time()),
            })
        except queue.Full:
            pass

    def _detection_loop(self) -> None:
        while self._active:
            try:
                item = self._detector_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            bins_hz = item.get("bins_hz")
            powers_db = item.get("powers_db")
            ts = item.get("timestamp", time.time())
            if not bins_hz or not powers_db:
                continue
            self._update_baseline(bins_hz, powers_db)
            detected = self._find_signals(bins_hz, powers_db, ts)
            for sig in detected:
                name, conf = self._classify_signal(sig)
                self._track_signal(sig, name, conf)
                self._db_save_observation(sig, name, conf)

    def _update_baseline(self, bins_hz: list, powers_db: list) -> None:
        alpha = self._baseline_alpha
        for i, freq in enumerate(bins_hz):
            if i >= len(powers_db):
                break
            p = powers_db[i]
            if p is None:
                continue
            prev = self._baseline_db.get(freq)
            if prev is None:
                self._baseline_db[freq] = p
            else:
                self._baseline_db[freq] = prev + alpha * (p - prev)

    def _find_signals(
        self, bins_hz: list, powers_db: list, ts: float,
    ) -> list[DetectedSignal]:
        if not self._baseline_db:
            return []
        threshold = self._detection_threshold_db
        min_bw = self._min_bandwidth_hz

        in_signal = False
        sig_bins: list[tuple[int, float]] = []
        detected: list[DetectedSignal] = []

        for i, freq in enumerate(bins_hz):
            if i >= len(powers_db):
                break
            p = powers_db[i]
            if p is None:
                if in_signal:
                    detected.extend(
                        self._finish_signal(sig_bins, ts, min_bw),
                    )
                    sig_bins = []
                    in_signal = False
                continue
            baseline = self._baseline_db.get(freq)
            if baseline is None:
                continue
            if p > baseline + threshold:
                in_signal = True
                sig_bins.append((freq, p))
            elif in_signal:
                detected.extend(
                    self._finish_signal(sig_bins, ts, min_bw),
                )
                sig_bins = []
                in_signal = False

        if in_signal and sig_bins:
            detected.extend(self._finish_signal(sig_bins, ts, min_bw))

        return detected[:20]

    def _finish_signal(
        self,
        bins: list[tuple[int, float]],
        ts: float,
        min_bw: int,
    ) -> list[DetectedSignal]:
        if not bins:
            return []
        freqs = [b[0] for b in bins]
        powers = [b[1] for b in bins]
        bin_width = freqs[1] - freqs[0] if len(freqs) > 1 else 0
        bw = freqs[-1] - freqs[0] + bin_width
        if bw < min_bw:
            return []
        peak_idx = powers.index(max(powers))
        center = freqs[peak_idx]
        return [DetectedSignal(
            center_freq_hz=center,
            bandwidth_hz=bw,
            peak_power_db=max(powers),
            timestamp=ts,
        )]

    def _track_signal(
        self, sig: DetectedSignal, name: str | None, conf: float,
    ) -> None:
        qfreq = sig.center_freq_hz // 10000 * 10000
        publish_evt = None
        with self._signals_lock:
            existing = self._active_signals.get(qfreq)
            if existing is not None:
                gap = sig.timestamp - existing.last_seen
                if gap > 30:
                    existing.intermittent = True
                existing.last_seen = sig.timestamp
                existing.duration_s = sig.timestamp - existing.first_seen
                existing.observation_count += 1
                existing.peak_power_db = max(existing.peak_power_db, sig.peak_power_db)
                if name and (existing.classification is None or conf > existing.confidence):
                    existing.classification = name
                    existing.confidence = conf
            else:
                track = SignalTrack(
                    center_freq_hz=sig.center_freq_hz,
                    bandwidth_hz=sig.bandwidth_hz,
                    peak_power_db=sig.peak_power_db,
                    first_seen=sig.timestamp,
                    last_seen=sig.timestamp,
                    classification=name,
                    confidence=conf,
                )
                self._active_signals[qfreq] = track
                self._stats["signals_detected_total"] += 1
                if name:
                    self._stats["signals_classified"] += 1
                else:
                    self._stats["signals_unknown"] += 1
                self._snapshot_dirty = True
                publish_evt = events.SIGOPS_SIGNAL_CLASSIFIED if name else events.SIGOPS_SIGNAL_UNKNOWN

        if publish_evt is not None:
            try:
                self.event_bus.publish(publish_evt, {
                    "freq_hz": sig.center_freq_hz,
                    "freq_mhz": round(sig.center_freq_hz / 1e6, 4),
                    "bandwidth_hz": sig.bandwidth_hz,
                    "power_db": round(sig.peak_power_db, 1),
                    "classification": name,
                    "confidence": round(conf, 2),
                })
            except Exception:
                pass

    # ── classification engine ────────────────────────────────────────

    def _classify_load_db(self) -> None:
        seed_path = os.path.join(
            os.path.dirname(__file__), "signal_db.json",
        )
        try:
            if os.path.exists(seed_path):
                with open(seed_path) as f:
                    self._signal_db = json.load(f)
        except Exception:
            self.log.debug("Could not load signal_db.json", exc_info=True)

        user_path = os.path.expanduser(
            "~/.local/share/reticulumpi/signal_db_user.json",
        )
        try:
            if os.path.exists(user_path):
                with open(user_path) as f:
                    user_db = json.load(f)
                self._signal_db.extend(user_db)
        except Exception:
            pass

    def _classify_signal(
        self, sig: DetectedSignal,
    ) -> tuple[str | None, float]:
        if not self._signal_db:
            return None, 0.0

        freq_mhz = sig.center_freq_hz / 1e6
        bw_khz = sig.bandwidth_hz / 1000

        best_name = None
        best_score = 0.0

        for entry in self._signal_db:
            fmin = entry.get("freq_min_mhz", 0)
            fmax = entry.get("freq_max_mhz", 0)
            if freq_mhz < fmin or freq_mhz > fmax:
                continue

            frange = fmax - fmin
            if frange > 0:
                center = (fmin + fmax) / 2
                offset = abs(freq_mhz - center) / (frange / 2)
                freq_score = max(0, 1.0 - offset) * 0.6
            else:
                freq_score = 0.6

            expected_bw = entry.get("bandwidth_khz", 0)
            if expected_bw > 0 and bw_khz > 0:
                ratio = min(bw_khz, expected_bw) / max(bw_khz, expected_bw)
                bw_score = ratio * 0.4
            else:
                bw_score = 0.2

            score = freq_score + bw_score
            if score > best_score:
                best_score = score
                best_name = entry.get("name")

        if best_score < 0.3:
            return None, 0.0
        return best_name, round(best_score, 2)

    def manual_classify(
        self, freq_hz: int, name: str, extra: dict | None = None,
    ) -> dict[str, Any]:
        entry = {
            "name": name,
            "freq_min_mhz": round(freq_hz / 1e6 - 0.05, 4),
            "freq_max_mhz": round(freq_hz / 1e6 + 0.05, 4),
            "bandwidth_khz": 0,
            "modulation": "unknown",
            "description": f"User-classified signal at {freq_hz / 1e6:.4f} MHz",
        }
        if extra:
            entry.update({
                k: v for k, v in extra.items()
                if k in ("bandwidth_khz", "modulation", "description")
            })
        freq_mhz = freq_hz / 1e6
        self._signal_db = [
            e for e in self._signal_db
            if not (e.get("freq_min_mhz", 0) <= freq_mhz <= e.get("freq_max_mhz", 0)
                    and e.get("name") == name)
        ]
        self._signal_db.append(entry)

        user_path = os.path.expanduser(
            "~/.local/share/reticulumpi/signal_db_user.json",
        )
        try:
            existing = []
            if os.path.exists(user_path):
                with open(user_path) as f:
                    existing = json.load(f)
            existing = [
                e for e in existing
                if not (e.get("freq_min_mhz", 0) <= freq_mhz <= e.get("freq_max_mhz", 0)
                        and e.get("name") == name)
            ]
            existing.append(entry)
            os.makedirs(os.path.dirname(user_path), exist_ok=True)
            with open(user_path, "w") as f:
                json.dump(existing, f, indent=2)
        except Exception:
            self.log.debug("Could not save user signal DB", exc_info=True)

        return {"status": "classified", "entry": entry}

    # ── correlation engine ───────────────────────────────────────────

    def _upsert_contact(
        self,
        contact_type: str,
        identifier: str,
        source: str,
        display_name: str = "",
        lat: float | None = None,
        lon: float | None = None,
        metadata: dict | None = None,
    ) -> Contact:
        cid = _contact_id(contact_type, identifier)
        now = time.time()
        with self._contacts_lock:
            existing = self._contacts.get(cid)
            if existing is not None:
                existing.last_seen = now
                existing.observation_count += 1
                existing.sources.add(source)
                if lat is not None:
                    existing.lat = lat
                if lon is not None:
                    existing.lon = lon
                if display_name:
                    existing.display_name = display_name
                if metadata:
                    existing.metadata.update(metadata)
                self._update_contact_distance(existing)
                return existing

            if len(self._contacts) >= self._max_contacts:
                self.log.warning(
                    "Max contacts (%d) reached, dropping %s",
                    self._max_contacts, identifier,
                )
                return Contact(
                    id=cid, contact_type=contact_type,
                    identifier=identifier, first_seen=now, last_seen=now,
                )

            contact = Contact(
                id=cid,
                contact_type=contact_type,
                identifier=identifier,
                display_name=display_name or identifier,
                first_seen=now,
                last_seen=now,
                observation_count=1,
                sources={source},
                lat=lat,
                lon=lon,
                metadata=metadata or {},
            )
            self._update_contact_distance(contact)
            self._contacts[cid] = contact
            self._stats["contacts_total"] += 1
            self._snapshot_dirty = True

        try:
            self.event_bus.publish(events.SIGOPS_CONTACT_NEW, {
                "id": cid,
                "type": contact_type,
                "identifier": identifier,
                "source": source,
            })
        except Exception:
            pass

        return contact

    def _update_contact_distance(self, contact: Contact) -> None:
        if (
            contact.lat is not None
            and contact.lon is not None
            and self._receiver_lat is not None
            and self._receiver_lon is not None
        ):
            from reticulumpi.geo import haversine_nm, bearing_deg
            contact.distance_nm = round(
                haversine_nm(
                    self._receiver_lat, self._receiver_lon,
                    contact.lat, contact.lon,
                ), 1,
            )
            contact.bearing_deg = round(
                bearing_deg(
                    self._receiver_lat, self._receiver_lon,
                    contact.lat, contact.lon,
                ), 0,
            )

    def _on_adsb(self, _event_type: str, data: dict) -> None:
        icao = data.get("icao", "")
        if not icao:
            return
        self._upsert_contact(
            "aircraft", icao, "adsb_radar",
            display_name=data.get("callsign", "") or icao,
            lat=data.get("latitude"),
            lon=data.get("longitude"),
            metadata={
                "callsign": data.get("callsign"),
                "altitude": data.get("altitude"),
                "speed": data.get("ground_speed"),
            },
        )

    def _on_adsb_lost(self, _event_type: str, data: dict) -> None:
        icao = data.get("icao", "")
        if not icao:
            return
        cid = _contact_id("aircraft", icao)
        with self._contacts_lock:
            contact = self._contacts.get(cid)
            if contact:
                contact.metadata["status"] = "lost"

    def _on_adsb_emergency(self, _event_type: str, data: dict) -> None:
        icao = data.get("icao", "")
        if not icao:
            return
        contact = self._upsert_contact(
            "aircraft", icao, "adsb_radar",
            metadata={"emergency": True, "squawk": data.get("squawk")},
        )
        self._add_correlation(
            "adsb_emergency",
            [contact.id],
            f"Emergency squawk {data.get('squawk')} from {icao}",
            ["adsb_radar"],
        )

    def _on_acars(self, _event_type: str, data: dict) -> None:
        tail = data.get("tail", "")
        if not tail:
            return
        flight = data.get("flight", "")
        self._upsert_contact(
            "aircraft", tail, "acars_decoder",
            display_name=flight or tail,
            metadata={
                "flight": flight,
                "label": data.get("label"),
                "acars_tail": tail,
            },
        )

        adsb = self.app.get_plugin("adsb_radar")
        if adsb is not None:
            snap = adsb.get_snapshot()
            aircraft_list = snap.get("aircraft", [])
            for ac in aircraft_list:
                cs = (ac.get("callsign") or "").strip()
                if cs and flight and flight.strip() in cs:
                    self._add_correlation(
                        "acars_adsb_merge",
                        [_contact_id("aircraft", tail), _contact_id("aircraft", ac.get("icao", ""))],
                        f"ACARS {tail}/{flight} matches ADS-B {ac.get('icao')}",
                        ["acars_decoder", "adsb_radar"],
                    )
                    break

    def _on_ais(self, _event_type: str, data: dict) -> None:
        mmsi = str(data.get("mmsi", ""))
        if not mmsi:
            return
        self._upsert_contact(
            "vessel", mmsi, "ais_receiver",
            display_name=data.get("name", "") or mmsi,
            lat=data.get("lat"),
            lon=data.get("lon"),
            metadata={
                "ship_type": data.get("ship_type"),
                "callsign": data.get("callsign"),
                "speed_kts": data.get("speed_kts"),
            },
        )

    def _on_ais_lost(self, _event_type: str, data: dict) -> None:
        mmsi = str(data.get("mmsi", ""))
        if not mmsi:
            return
        cid = _contact_id("vessel", mmsi)
        with self._contacts_lock:
            contact = self._contacts.get(cid)
            if contact:
                contact.metadata["status"] = "lost"

    def _on_radiosonde(self, _event_type: str, data: dict) -> None:
        sid = data.get("id", "")
        if not sid:
            return
        self._upsert_contact(
            "balloon", sid, "radiosonde_tracker",
            display_name=f"Sonde {sid}",
            metadata={
                "type": data.get("type"),
                "alt_m": data.get("alt_m"),
            },
        )

    def _on_radiosonde_lost(self, _event_type: str, data: dict) -> None:
        sid = data.get("id", "")
        if not sid:
            return
        cid = _contact_id("balloon", sid)
        with self._contacts_lock:
            contact = self._contacts.get(cid)
            if contact:
                contact.metadata["status"] = "lost"

    def _on_weather_alert(self, _event_type: str, data: dict) -> None:
        event_code = data.get("event_code", "")
        severity = data.get("severity", "info")
        fips = data.get("fips_codes", [])
        if severity in ("extreme", "severe") and fips:
            self._add_correlation(
                "weather_alert",
                [],
                f"{data.get('event_desc', event_code)} for {', '.join(fips[:3])}",
                ["weather_alert"],
            )

    def _on_lora_peer(self, _event_type: str, data: dict) -> None:
        peer_hash = data.get("destination_hash", "")
        if not peer_hash:
            return
        self._upsert_contact(
            "mesh_peer", peer_hash, "lora_diagnostics",
            display_name=data.get("app_name", "") or peer_hash[:12],
            metadata={
                "app_name": data.get("app_name"),
                "hops": data.get("hops"),
            },
        )

    def _on_ism_device(self, _event_type: str, data: dict) -> None:
        key = data.get("key", "")
        if not key:
            return
        self._upsert_contact(
            "ism_device", key, "ism_decoder",
            display_name=data.get("model", key),
            metadata={
                "model": data.get("model"),
                "id": data.get("id"),
            },
        )

    def _on_ism_lost(self, _event_type: str, data: dict) -> None:
        key = data.get("key", "")
        if not key:
            return
        cid = _contact_id("ism_device", key)
        with self._contacts_lock:
            contact = self._contacts.get(cid)
            if contact:
                contact.metadata["status"] = "lost"

    def _add_correlation(
        self,
        event_type: str,
        contact_ids: list[str],
        description: str,
        sources: list[str],
    ) -> None:
        now = time.time()
        entry = {
            "timestamp": now,
            "event_type": event_type,
            "contact_ids": contact_ids,
            "description": description,
            "sources": sources,
        }
        self._correlation_events.appendleft(entry)
        self._stats["correlations_total"] += 1
        self._snapshot_dirty = True

        try:
            self.event_bus.publish(events.SIGOPS_CORRELATION, entry)
        except Exception:
            pass

        self._db_save_correlation(entry)

    # ── maintenance ──────────────────────────────────────────────────

    def _maintenance_loop(self) -> None:
        while self._active:
            self._sleep_while_active(self._correlation_interval)
            if not self._active:
                break
            self._evict_stale_contacts()
            self._evict_stale_signals()
            self._db_purge_old()
            self._update_stats()
            self._snapshot_dirty = True

    def _evict_stale_contacts(self) -> None:
        now = time.time()
        cutoff = now - self._stale_contact_timeout
        with self._contacts_lock:
            stale_ids = [
                cid for cid, c in self._contacts.items()
                if c.last_seen < cutoff
            ]
            for cid in stale_ids:
                contact = self._contacts.pop(cid)
                try:
                    self.event_bus.publish(events.SIGOPS_CONTACT_LOST, {
                        "id": cid,
                        "type": contact.contact_type,
                        "identifier": contact.identifier,
                    })
                except Exception:
                    pass

    def _evict_stale_signals(self) -> None:
        now = time.time()
        with self._signals_lock:
            stale = [
                qfreq for qfreq, t in self._active_signals.items()
                if now - t.last_seen > 300
            ]
            for qfreq in stale:
                del self._active_signals[qfreq]

    def _update_stats(self) -> None:
        with self._contacts_lock:
            self._stats["contacts_active"] = len(self._contacts)

    # ── persistence ──────────────────────────────────────────────────

    def _db_init(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.executescript(_SCHEMA_SQL)
        except Exception:
            self.log.exception("Failed to initialize sigops database")

    def _db_load_contacts(self) -> None:
        try:
            with sqlite3.connect(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT id, contact_type, identifier, display_name, "
                    "first_seen, last_seen, observation_count, sources_json, "
                    "lat, lon, metadata_json FROM contacts "
                    "ORDER BY last_seen DESC LIMIT ?",
                    (self._max_contacts,),
                ).fetchall()
            now = time.time()
            cutoff = now - self._stale_contact_timeout
            with self._contacts_lock:
                for row in rows:
                    if row[5] < cutoff:
                        continue
                    sources = set()
                    if row[7]:
                        try:
                            sources = set(json.loads(row[7]))
                        except Exception:
                            pass
                    meta = {}
                    if row[10]:
                        try:
                            meta = json.loads(row[10])
                        except Exception:
                            pass
                    contact = Contact(
                        id=row[0],
                        contact_type=row[1],
                        identifier=row[2],
                        display_name=row[3] or row[2],
                        first_seen=row[4],
                        last_seen=row[5],
                        observation_count=row[6],
                        sources=sources,
                        lat=row[8],
                        lon=row[9],
                        metadata=meta,
                    )
                    self._contacts[row[0]] = contact
                self._stats["contacts_total"] = len(self._contacts)
                self._stats["contacts_active"] = len(self._contacts)
        except Exception:
            self.log.debug("Could not load contacts from DB", exc_info=True)

    def _db_save_observation(
        self, sig: DetectedSignal, name: str | None, conf: float,
        source: str = "spectrum_scanner",
    ) -> None:
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO signal_observations "
                    "(timestamp, freq_hz, bandwidth_hz, power_db, signal_type, "
                    "classification_name, confidence, source_plugin) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        sig.timestamp, sig.center_freq_hz, sig.bandwidth_hz,
                        round(sig.peak_power_db, 1),
                        "classified" if name else "detected",
                        name, round(conf, 2), source,
                    ),
                )
            self._stats["observations_persisted"] += 1
        except Exception:
            self.log.debug("Failed to save observation", exc_info=True)

    def _db_save_correlation(self, entry: dict) -> None:
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO correlation_events "
                    "(timestamp, event_type, contact_ids_json, description, sources_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        entry["timestamp"],
                        entry["event_type"],
                        json.dumps(entry.get("contact_ids", [])),
                        entry.get("description", ""),
                        json.dumps(entry.get("sources", [])),
                    ),
                )
        except Exception:
            self.log.debug("Failed to save correlation", exc_info=True)

    def _db_flush(self) -> None:
        try:
            with sqlite3.connect(self._db_path) as conn:
                with self._contacts_lock:
                    for contact in self._contacts.values():
                        conn.execute(
                            "INSERT OR REPLACE INTO contacts "
                            "(id, contact_type, identifier, display_name, "
                            "first_seen, last_seen, observation_count, "
                            "sources_json, lat, lon, metadata_json) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                contact.id, contact.contact_type,
                                contact.identifier, contact.display_name,
                                contact.first_seen, contact.last_seen,
                                contact.observation_count,
                                json.dumps(sorted(contact.sources)),
                                contact.lat, contact.lon,
                                json.dumps(contact.metadata),
                            ),
                        )
        except Exception:
            self.log.debug("Error flushing contacts to DB", exc_info=True)

    def _db_purge_old(self) -> None:
        cutoff = time.time() - (self._max_history_days * 86400)
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "DELETE FROM signal_observations WHERE timestamp < ?",
                    (cutoff,),
                )
                conn.execute(
                    "DELETE FROM correlation_events WHERE timestamp < ?",
                    (cutoff,),
                )
        except Exception:
            self.log.debug("Failed to purge old records", exc_info=True)

    # ── public query methods ─────────────────────────────────────────

    def get_overview(self) -> dict[str, Any]:
        """Lightweight summary with counts.  WS snapshot (broadcast_snapshot)
        returns full lists instead — different shapes by design."""
        with self._signals_lock:
            active_sigs = len(self._active_signals)
        with self._contacts_lock:
            active_contacts = len(self._contacts)
        return {
            "stats": dict(self._stats),
            "active_signals": active_sigs,
            "active_contacts": active_contacts,
            "correlation_events": len(self._correlation_events),
        }

    def get_contacts(
        self, contact_type: str = "", limit: int = 100,
    ) -> dict[str, Any]:
        with self._contacts_lock:
            contacts = list(self._contacts.values())
        if contact_type:
            contacts = [c for c in contacts if c.contact_type == contact_type]
        contacts.sort(key=lambda c: c.last_seen, reverse=True)
        return {
            "contacts": [self._contact_to_dict(c) for c in contacts[:limit]],
            "total": len(contacts),
        }

    def get_contact_detail(self, contact_id: str) -> dict[str, Any] | None:
        with self._contacts_lock:
            contact = self._contacts.get(contact_id)
        if contact is None:
            return None
        return self._contact_to_dict(contact)

    def get_detections(
        self,
        since: float = 0,
        freq_min: int = 0,
        freq_max: int = 0,
        signal_type: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        try:
            sql = "SELECT timestamp, freq_hz, bandwidth_hz, power_db, signal_type, classification_name, confidence FROM signal_observations WHERE 1=1"
            params: list[Any] = []
            if since > 0:
                sql += " AND timestamp > ?"
                params.append(since)
            if freq_min > 0:
                sql += " AND freq_hz >= ?"
                params.append(freq_min)
            if freq_max > 0:
                sql += " AND freq_hz <= ?"
                params.append(freq_max)
            if signal_type:
                sql += " AND signal_type = ?"
                params.append(signal_type)
            sql += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)

            with sqlite3.connect(self._db_path) as conn:
                rows = conn.execute(sql, params).fetchall()
            return {
                "detections": [
                    {
                        "timestamp": r[0],
                        "freq_hz": r[1],
                        "freq_mhz": round(r[1] / 1e6, 4),
                        "bandwidth_hz": r[2],
                        "power_db": r[3],
                        "signal_type": r[4],
                        "classification": r[5],
                        "confidence": r[6],
                    }
                    for r in rows
                ],
            }
        except Exception:
            return {"detections": []}

    def get_baseline(self, limit: int = 2000) -> dict[str, Any]:
        items = sorted(self._baseline_db.items())
        total = len(items)
        if limit:
            items = items[:limit]
        entries = [
            {"freq_hz": f, "freq_mhz": round(f / 1e6, 4), "power_db": round(p, 1)}
            for f, p in items
        ]
        return {"bins": entries, "bin_count": total}

    def get_correlations(self, limit: int = 50) -> dict[str, Any]:
        return {
            "events": list(self._correlation_events)[:limit],
            "total": len(self._correlation_events),
        }

    def get_aggregate_stats(self) -> dict[str, Any]:
        try:
            with sqlite3.connect(self._db_path) as conn:
                total = conn.execute(
                    "SELECT COUNT(*) FROM signal_observations",
                ).fetchone()[0]
                by_type = conn.execute(
                    "SELECT signal_type, COUNT(*) FROM signal_observations GROUP BY signal_type",
                ).fetchall()
                by_class = conn.execute(
                    "SELECT classification_name, COUNT(*) FROM signal_observations "
                    "WHERE classification_name IS NOT NULL GROUP BY classification_name "
                    "ORDER BY COUNT(*) DESC LIMIT 20",
                ).fetchall()
            with self._signals_lock:
                active_sigs = len(self._active_signals)
            with self._contacts_lock:
                active_contacts = len(self._contacts)
            return {
                "total_observations": total,
                "by_type": {r[0]: r[1] for r in by_type if r[0]},
                "by_classification": {r[0]: r[1] for r in by_class},
                "active_signals": active_sigs,
                "active_contacts": active_contacts,
            }
        except Exception:
            return {"total_observations": 0}

    def reset_baseline(self) -> None:
        self._baseline_db.clear()
        with self._signals_lock:
            self._active_signals.clear()

    def _contact_to_dict(self, c: Contact) -> dict[str, Any]:
        return {
            "id": c.id,
            "contact_type": c.contact_type,
            "identifier": c.identifier,
            "display_name": c.display_name,
            "first_seen": c.first_seen,
            "last_seen": c.last_seen,
            "observation_count": c.observation_count,
            "sources": sorted(c.sources),
            "lat": c.lat,
            "lon": c.lon,
            "distance_nm": c.distance_nm,
            "bearing_deg": c.bearing_deg,
            "metadata": c.metadata,
        }

    # ── snapshot & broadcast ─────────────────────────────────────────

    def get_snapshot(self) -> dict[str, Any]:
        return dict(self._snapshot_cache)

    def broadcast_snapshot(self, cycle_count: int = 0) -> dict[str, Any] | None:
        if self._snapshot_dirty:
            self._update_snapshot_cache()
        snap = self.get_snapshot()
        return snap if snap else None

    def _update_snapshot_cache(self) -> None:
        with self._contacts_lock:
            contacts = sorted(
                self._contacts.values(),
                key=lambda c: c.last_seen,
                reverse=True,
            )[:50]
            contact_list = [self._contact_to_dict(c) for c in contacts]

        with self._signals_lock:
            signals = sorted(
                self._active_signals.values(),
                key=lambda s: s.last_seen,
                reverse=True,
            )[:20]
        signal_list = [
            {
                "freq_hz": s.center_freq_hz,
                "freq_mhz": round(s.center_freq_hz / 1e6, 4),
                "bandwidth_hz": s.bandwidth_hz,
                "power_db": round(s.peak_power_db, 1),
                "classification": s.classification,
                "confidence": round(s.confidence, 2),
                "duration_s": round(s.duration_s, 1),
                "intermittent": s.intermittent,
                "observation_count": s.observation_count,
            }
            for s in signals
        ]

        self._snapshot_cache = {
            "stats": dict(self._stats),
            "active_signals": signal_list,
            "contacts": contact_list,
            "recent_correlations": list(self._correlation_events)[:10],
        }
        self._snapshot_dirty = False

    def get_status(self) -> dict[str, Any]:
        with self._contacts_lock:
            contacts_active = len(self._contacts)
        return {
            "active": self._active,
            "signals_detected": self._stats["signals_detected_total"],
            "contacts_active": contacts_active,
            "correlations": self._stats["correlations_total"],
            "observations": self._stats["observations_persisted"],
        }
