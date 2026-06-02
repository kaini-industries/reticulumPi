"""Hotspot Monitor plugin - reports Wi-Fi AP status and connected clients."""

from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path
from typing import Any

from reticulumpi.plugin_base import PluginBase


class HotspotMonitorPlugin(PluginBase):
    plugin_name = "hotspot_monitor"
    plugin_version = "1.0.0"
    plugin_description = "Monitors Wi-Fi hotspot (hostapd) status and clients"
    broadcast_tier = 2
    broadcast_keys = "hotspot"

    def start(self) -> None:
        self._active = True
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] | None = None

        conf_path = self.config.get(
            "hostapd_conf",
            "/etc/hostapd/hostapd.conf",
        )
        self._static = _parse_hostapd_conf(conf_path)
        self._iface = self._static.get("interface", "wlan0")

        if not self._static:
            self.log.info("hostapd.conf not found at %s — hotspot monitoring disabled", conf_path)
            return

        self._thread = self._start_thread(self._collect_loop, "hotspot-mon")
        self.log.info("Hotspot monitor active (interface: %s)", self._iface)

    def stop(self) -> None:
        self._active = False
        self._join_threads()

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            snap = self._snapshot
        if snap:
            return {
                "active": True,
                "ap_active": snap.get("active", False),
                "client_count": snap.get("client_count", 0),
            }
        return {"active": self._active}

    def broadcast_snapshot(self, cycle_count: int = 0) -> dict[str, Any] | None:
        with self._lock:
            return self._snapshot

    def _collect_loop(self) -> None:
        interval = self.config.get("collect_interval_seconds", 15)
        while self._active:
            try:
                data = self._collect()
                with self._lock:
                    self._snapshot = data
            except Exception:
                self.log.exception("Error collecting hotspot data")
            self._sleep_while_active(interval)

    def _collect(self) -> dict[str, Any]:
        info = _parse_iw_info(self._iface)
        ap_active = info.get("type") == "AP"

        result: dict[str, Any] = {
            "active": ap_active,
            "ssid": info.get("ssid") or self._static.get("ssid"),
            "channel": info.get("channel") or self._static.get("channel"),
            "frequency": info.get("frequency"),
            "security": self._static.get("security", "Unknown"),
            "interface": self._iface,
            "ip": _get_interface_ip(self._iface),
            "clients": [],
            "client_count": 0,
        }

        if ap_active:
            leases = _parse_dnsmasq_leases()
            stations = _parse_iw_station_dump(self._iface)
            for sta in stations:
                lease = leases.get(sta["mac"])
                if lease:
                    sta["hostname"] = lease.get("hostname")
                    sta["ip"] = lease.get("ip")
                result["clients"].append(sta)
            result["client_count"] = len(stations)

        return result


def _parse_hostapd_conf(path: str) -> dict[str, Any]:
    try:
        text = Path(path).read_text()
    except OSError:
        return {}

    conf: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        conf[key.strip()] = val.strip()

    result: dict[str, Any] = {}
    if "interface" in conf:
        result["interface"] = conf["interface"]
    if "ssid" in conf:
        result["ssid"] = conf["ssid"]
    if "channel" in conf:
        try:
            result["channel"] = int(conf["channel"])
        except ValueError:
            result["channel"] = conf["channel"]

    wpa = conf.get("wpa", "")
    wpa_mgmt = conf.get("wpa_key_mgmt", "")
    if "SAE" in wpa_mgmt:
        result["security"] = "WPA3"
    elif wpa == "2":
        result["security"] = "WPA2"
    elif wpa == "1":
        result["security"] = "WPA"
    elif wpa == "3":
        result["security"] = "WPA2/WPA3"
    else:
        result["security"] = "Open"

    return result


_IW_INFO_RE = {
    "ssid": re.compile(r"^\s+ssid\s+(.+)$", re.MULTILINE),
    "type": re.compile(r"^\s+type\s+(\S+)", re.MULTILINE),
    "channel": re.compile(r"^\s+channel\s+(\d+)\s+\((\d+)\s+MHz\)", re.MULTILINE),
}


def _parse_iw_info(iface: str) -> dict[str, Any]:
    try:
        out = subprocess.run(
            ["iw", "dev", iface, "info"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        text = out.stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}

    result: dict[str, Any] = {}
    m = _IW_INFO_RE["ssid"].search(text)
    if m:
        result["ssid"] = m.group(1).strip()
    m = _IW_INFO_RE["type"].search(text)
    if m:
        result["type"] = m.group(1)
    m = _IW_INFO_RE["channel"].search(text)
    if m:
        result["channel"] = int(m.group(1))
        result["frequency"] = int(m.group(2))

    return result


_STATION_SPLIT = re.compile(r"^Station\s+([0-9a-f:]{17})\s+\(on\s+\S+\)", re.MULTILINE)


def _parse_iw_station_dump(iface: str) -> list[dict[str, Any]]:
    try:
        out = subprocess.run(
            ["iw", "dev", iface, "station", "dump"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        text = out.stdout
    except (OSError, subprocess.TimeoutExpired):
        return []

    stations: list[dict[str, Any]] = []
    parts = _STATION_SPLIT.split(text)
    # parts = ['', mac1, block1, mac2, block2, ...]
    for i in range(1, len(parts) - 1, 2):
        mac = parts[i].lower()
        block = parts[i + 1]
        sta: dict[str, Any] = {"mac": mac, "hostname": None, "ip": None, "inactive_time_ms": None}
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("inactive time:"):
                val = line.split(":", 1)[1].strip().split()[0]
                sta["inactive_time_ms"] = _int_or_none(val)
            elif line.startswith("rx bytes:"):
                sta["rx_bytes"] = _int_or_none(line.split(":", 1)[1])
            elif line.startswith("tx bytes:"):
                sta["tx_bytes"] = _int_or_none(line.split(":", 1)[1])
            elif line.startswith("connected time:"):
                val = line.split(":", 1)[1].strip().split()[0]
                sta["connected_time"] = _int_or_none(val)
            elif line.startswith("signal:"):
                val = line.split(":", 1)[1].strip().split()[0]
                sta["signal"] = _int_or_none(val)
            elif line.startswith("tx bitrate:"):
                sta["tx_bitrate"] = line.split(":", 1)[1].strip()
            elif line.startswith("rx bitrate:"):
                sta["rx_bitrate"] = line.split(":", 1)[1].strip()
        stations.append(sta)

    return stations


def _parse_dnsmasq_leases(path: str = "/var/lib/misc/dnsmasq.leases") -> dict[str, dict[str, str]]:
    try:
        text = Path(path).read_text()
    except OSError:
        return {}

    leases: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            mac = parts[1].lower()
            leases[mac] = {"ip": parts[2], "hostname": parts[3] if parts[3] != "*" else None}
    return leases


_IP_ADDR_RE = re.compile(r"inet\s+(\d+\.\d+\.\d+\.\d+)")


def _get_interface_ip(iface: str) -> str | None:
    try:
        out = subprocess.run(
            ["ip", "-4", "addr", "show", iface],
            capture_output=True,
            text=True,
            timeout=5,
        )
        m = _IP_ADDR_RE.search(out.stdout)
        return m.group(1) if m else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _int_or_none(s: str | None) -> int | None:
    if s is None:
        return None
    try:
        return int(s.strip())
    except (ValueError, AttributeError):
        return None
