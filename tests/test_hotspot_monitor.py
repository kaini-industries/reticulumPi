"""Tests for the hotspot_monitor plugin."""

from unittest.mock import MagicMock, patch

from reticulumpi.builtin_plugins.hotspot_monitor import (
    HotspotMonitorPlugin,
    _get_interface_ip,
    _parse_dnsmasq_leases,
    _parse_hostapd_conf,
    _parse_iw_info,
    _parse_iw_station_dump,
)

SAMPLE_HOSTAPD_CONF = """\
interface=wlan0
driver=nl80211
ssid=Reticulum Pi
hw_mode=g
channel=1
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=offgrid4life
wpa_key_mgmt=WPA-PSK
wpa_pairwise=TKIP
rsn_pairwise=CCMP
"""

SAMPLE_IW_INFO = """\
Interface wlan0
\tifindex 3
\twdev 0x1
\taddr 2c:cf:67:e7:17:91
\tssid Reticulum Pi
\ttype AP
\twiphy 0
\tchannel 1 (2412 MHz), width: 20 MHz, center1: 2412 MHz
\ttxpower 31.00 dBm
"""

SAMPLE_IW_STATION_DUMP = """\
Station 5c:e9:1e:b2:58:78 (on wlan0)
\tinactive time:\t6000 ms
\trx bytes:\t91450883
\trx packets:\t404037
\ttx bytes:\t311998882
\ttx packets:\t381752
\ttx failed:\t617
\ttx bitrate:\t65.0 MBit/s
\trx bitrate:\t24.0 MBit/s
\tauthorized:\tyes
\tauthenticated:\tyes
\tassociated:\tyes
\tconnected time:\t677681 seconds
Station aa:bb:cc:dd:ee:ff (on wlan0)
\trx bytes:\t1024
\ttx bytes:\t2048
\tconnected time:\t120 seconds
"""

SAMPLE_DNSMASQ_LEASES = """\
1778408748 5c:e9:1e:b2:58:78 10.0.0.32 Erics-MBP-2 01:5c:e9:1e:b2:58:78
1778408800 aa:bb:cc:dd:ee:ff 10.0.0.33 * 01:aa:bb:cc:dd:ee:ff
"""

SAMPLE_IP_ADDR = """\
3: wlan0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500
    link/ether 2c:cf:67:e7:17:91 brd ff:ff:ff:ff:ff:ff
    inet 10.0.0.1/24 brd 10.0.0.255 scope global wlan0
       valid_lft forever preferred_lft forever
"""


class TestParseHostapdConf:
    def test_parses_basic_config(self, tmp_path):
        conf = tmp_path / "hostapd.conf"
        conf.write_text(SAMPLE_HOSTAPD_CONF)
        result = _parse_hostapd_conf(str(conf))
        assert result["interface"] == "wlan0"
        assert result["ssid"] == "Reticulum Pi"
        assert result["channel"] == 1
        assert result["security"] == "WPA2"

    def test_missing_file_returns_empty(self):
        result = _parse_hostapd_conf("/nonexistent/path")
        assert result == {}

    def test_wpa3_detection(self, tmp_path):
        conf = tmp_path / "hostapd.conf"
        conf.write_text("interface=wlan0\nssid=Test\nwpa=2\nwpa_key_mgmt=SAE\n")
        result = _parse_hostapd_conf(str(conf))
        assert result["security"] == "WPA3"

    def test_open_network(self, tmp_path):
        conf = tmp_path / "hostapd.conf"
        conf.write_text("interface=wlan0\nssid=Open\n")
        result = _parse_hostapd_conf(str(conf))
        assert result["security"] == "Open"

    def test_comments_and_blanks_ignored(self, tmp_path):
        conf = tmp_path / "hostapd.conf"
        conf.write_text("# comment\n\ninterface=wlan0\n")
        result = _parse_hostapd_conf(str(conf))
        assert result["interface"] == "wlan0"


class TestParseIwInfo:
    @patch("reticulumpi.builtin_plugins.hotspot_monitor.subprocess.run")
    def test_parses_ap_info(self, mock_run):
        mock_run.return_value = MagicMock(stdout=SAMPLE_IW_INFO)
        result = _parse_iw_info("wlan0")
        assert result["ssid"] == "Reticulum Pi"
        assert result["type"] == "AP"
        assert result["channel"] == 1
        assert result["frequency"] == 2412

    @patch("reticulumpi.builtin_plugins.hotspot_monitor.subprocess.run")
    def test_handles_missing_iw(self, mock_run):
        mock_run.side_effect = OSError("iw not found")
        result = _parse_iw_info("wlan0")
        assert result == {}


class TestParseIwStationDump:
    @patch("reticulumpi.builtin_plugins.hotspot_monitor.subprocess.run")
    def test_parses_two_stations(self, mock_run):
        mock_run.return_value = MagicMock(stdout=SAMPLE_IW_STATION_DUMP)
        stations = _parse_iw_station_dump("wlan0")
        assert len(stations) == 2

        sta0 = stations[0]
        assert sta0["mac"] == "5c:e9:1e:b2:58:78"
        assert sta0["rx_bytes"] == 91450883
        assert sta0["tx_bytes"] == 311998882
        assert sta0["connected_time"] == 677681
        assert sta0["inactive_time_ms"] == 6000

        sta1 = stations[1]
        assert sta1["mac"] == "aa:bb:cc:dd:ee:ff"
        assert sta1["rx_bytes"] == 1024
        assert sta1["tx_bytes"] == 2048
        assert sta1["inactive_time_ms"] is None

    @patch("reticulumpi.builtin_plugins.hotspot_monitor.subprocess.run")
    def test_empty_output(self, mock_run):
        mock_run.return_value = MagicMock(stdout="")
        assert _parse_iw_station_dump("wlan0") == []


class TestParseDnsmasqLeases:
    def test_parses_leases(self, tmp_path):
        leases_file = tmp_path / "dnsmasq.leases"
        leases_file.write_text(SAMPLE_DNSMASQ_LEASES)
        result = _parse_dnsmasq_leases(str(leases_file))
        assert result["5c:e9:1e:b2:58:78"]["ip"] == "10.0.0.32"
        assert result["5c:e9:1e:b2:58:78"]["hostname"] == "Erics-MBP-2"
        assert result["aa:bb:cc:dd:ee:ff"]["hostname"] is None

    def test_missing_file(self):
        result = _parse_dnsmasq_leases("/nonexistent")
        assert result == {}


class TestGetInterfaceIp:
    @patch("reticulumpi.builtin_plugins.hotspot_monitor.subprocess.run")
    def test_parses_ip(self, mock_run):
        mock_run.return_value = MagicMock(stdout=SAMPLE_IP_ADDR)
        assert _get_interface_ip("wlan0") == "10.0.0.1"

    @patch("reticulumpi.builtin_plugins.hotspot_monitor.subprocess.run")
    def test_no_ip(self, mock_run):
        mock_run.return_value = MagicMock(stdout="3: wlan0: <NO-CARRIER>")
        assert _get_interface_ip("wlan0") is None


class TestHotspotMonitorPlugin:
    def test_start_no_config(self, mock_app, tmp_path):
        plugin = HotspotMonitorPlugin(mock_app, {
            "enabled": True,
            "hostapd_conf": str(tmp_path / "missing.conf"),
        })
        plugin.start()
        assert plugin._snapshot is None
        assert plugin._threads == []
        plugin.stop()

    def test_start_with_config(self, mock_app, tmp_path):
        conf = tmp_path / "hostapd.conf"
        conf.write_text(SAMPLE_HOSTAPD_CONF)
        plugin = HotspotMonitorPlugin(mock_app, {
            "enabled": True,
            "hostapd_conf": str(conf),
        })
        plugin.start()
        assert plugin._iface == "wlan0"
        assert len(plugin._threads) == 1
        plugin.stop()

    def test_broadcast_snapshot_returns_none_before_collect(self, mock_app, tmp_path):
        plugin = HotspotMonitorPlugin(mock_app, {
            "enabled": True,
            "hostapd_conf": str(tmp_path / "missing.conf"),
        })
        plugin.start()
        assert plugin.broadcast_snapshot() is None
        plugin.stop()

    def test_get_status_before_collect(self, mock_app, tmp_path):
        plugin = HotspotMonitorPlugin(mock_app, {
            "enabled": True,
            "hostapd_conf": str(tmp_path / "missing.conf"),
        })
        plugin.start()
        status = plugin.get_status()
        assert "active" in status
        plugin.stop()

    @patch("reticulumpi.builtin_plugins.hotspot_monitor._get_interface_ip", return_value="10.0.0.1")
    @patch("reticulumpi.builtin_plugins.hotspot_monitor._parse_dnsmasq_leases")
    @patch("reticulumpi.builtin_plugins.hotspot_monitor._parse_iw_station_dump")
    @patch("reticulumpi.builtin_plugins.hotspot_monitor._parse_iw_info")
    def test_collect_active_ap(self, mock_info, mock_stations, mock_leases, mock_ip, mock_app, tmp_path):
        conf = tmp_path / "hostapd.conf"
        conf.write_text(SAMPLE_HOSTAPD_CONF)
        plugin = HotspotMonitorPlugin(mock_app, {
            "enabled": True,
            "hostapd_conf": str(conf),
        })
        plugin.start()

        mock_info.return_value = {"type": "AP", "ssid": "Reticulum Pi", "channel": 1, "frequency": 2412}
        mock_stations.return_value = [{"mac": "5c:e9:1e:b2:58:78", "hostname": None, "ip": None, "rx_bytes": 100, "tx_bytes": 200, "connected_time": 60}]
        mock_leases.return_value = {"5c:e9:1e:b2:58:78": {"ip": "10.0.0.32", "hostname": "Test-Mac"}}

        result = plugin._collect()
        assert result["active"] is True
        assert result["ssid"] == "Reticulum Pi"
        assert result["client_count"] == 1
        assert result["clients"][0]["hostname"] == "Test-Mac"
        assert result["clients"][0]["ip"] == "10.0.0.32"
        plugin.stop()

    @patch("reticulumpi.builtin_plugins.hotspot_monitor._get_interface_ip", return_value=None)
    @patch("reticulumpi.builtin_plugins.hotspot_monitor._parse_iw_info")
    def test_collect_inactive_ap(self, mock_info, mock_ip, mock_app, tmp_path):
        conf = tmp_path / "hostapd.conf"
        conf.write_text(SAMPLE_HOSTAPD_CONF)
        plugin = HotspotMonitorPlugin(mock_app, {
            "enabled": True,
            "hostapd_conf": str(conf),
        })
        plugin.start()

        mock_info.return_value = {"type": "managed"}
        result = plugin._collect()
        assert result["active"] is False
        assert result["client_count"] == 0
        assert result["clients"] == []
        plugin.stop()
