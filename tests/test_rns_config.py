"""Tests for the Reticulum config parser/writer."""

from __future__ import annotations

import os
import textwrap

import pytest

from reticulumpi.rns_config import (
    InterfaceEntry,
    add_interface_section,
    parse_rns_config,
    set_interface_enabled,
    write_rns_config,
)

SAMPLE_CONFIG = textwrap.dedent("""\
    # Reticulum Configuration
    [reticulum]
      enable_transport = True
      share_instance = True

    [logging]
      loglevel = 4

    [interfaces]

    [[Auto Discovery Interface]]
      type = AutoInterface
      enabled = yes

    [[TCP Server Interface]]
      type = TCPServerInterface
      enabled = yes
      listen_ip = 0.0.0.0
      listen_port = 4242
      mode = gateway

    [[TCP Client beleth]]
      type = TCPClientInterface
      enabled = yes
      target_host = rns.beleth.net
      target_port = 4242

    [[Disabled Interface]]
      type = TCPClientInterface
      enabled = no
      target_host = example.com
      target_port = 1234
""")


@pytest.fixture()
def config_file(tmp_path):
    p = tmp_path / "config"
    p.write_text(SAMPLE_CONFIG, encoding="utf-8")
    return str(p)


class TestParse:
    def test_parse_interface_count(self, config_file):
        _, interfaces = parse_rns_config(config_file)
        assert len(interfaces) == 4

    def test_parse_names(self, config_file):
        _, interfaces = parse_rns_config(config_file)
        names = [i.name for i in interfaces]
        assert names == [
            "Auto Discovery Interface",
            "TCP Server Interface",
            "TCP Client beleth",
            "Disabled Interface",
        ]

    def test_parse_types(self, config_file):
        _, interfaces = parse_rns_config(config_file)
        types = [i.iface_type for i in interfaces]
        assert types == [
            "AutoInterface",
            "TCPServerInterface",
            "TCPClientInterface",
            "TCPClientInterface",
        ]

    def test_parse_enabled(self, config_file):
        _, interfaces = parse_rns_config(config_file)
        enabled = [i.enabled for i in interfaces]
        assert enabled == [True, True, True, False]

    def test_parse_properties(self, config_file):
        _, interfaces = parse_rns_config(config_file)
        tcp_server = interfaces[1]
        assert tcp_server.properties["listen_ip"] == "0.0.0.0"
        assert tcp_server.properties["listen_port"] == "4242"
        assert tcp_server.properties["mode"] == "gateway"

    def test_parse_enabled_line_tracked(self, config_file):
        _, interfaces = parse_rns_config(config_file)
        for iface in interfaces:
            assert iface.enabled_line >= 0

    def test_parse_enabled_variants(self, tmp_path):
        config = textwrap.dedent("""\
            [interfaces]
            [[A]]
              type = AutoInterface
              enabled = True
            [[B]]
              type = AutoInterface
              enabled = true
            [[C]]
              type = AutoInterface
              enabled = 1
            [[D]]
              type = AutoInterface
              enabled = False
            [[E]]
              type = AutoInterface
              enabled = false
            [[F]]
              type = AutoInterface
              enabled = 0
        """)
        p = tmp_path / "config"
        p.write_text(config, encoding="utf-8")
        _, ifaces = parse_rns_config(str(p))
        results = [i.enabled for i in ifaces]
        assert results == [True, True, True, False, False, False]


class TestToggle:
    def test_disable_interface(self, config_file):
        lines, interfaces = parse_rns_config(config_file)
        auto = interfaces[0]
        assert auto.enabled is True
        new_lines = set_interface_enabled(lines, auto, False)
        # Re-parse to verify
        with open(config_file, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        _, new_ifaces = parse_rns_config(config_file)
        assert new_ifaces[0].enabled is False

    def test_enable_interface(self, config_file):
        lines, interfaces = parse_rns_config(config_file)
        disabled = interfaces[3]
        assert disabled.enabled is False
        new_lines = set_interface_enabled(lines, disabled, True)
        with open(config_file, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        _, new_ifaces = parse_rns_config(config_file)
        assert new_ifaces[3].enabled is True

    def test_toggle_preserves_other_interfaces(self, config_file):
        lines, interfaces = parse_rns_config(config_file)
        new_lines = set_interface_enabled(lines, interfaces[0], False)
        with open(config_file, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        _, new_ifaces = parse_rns_config(config_file)
        # Only first interface changed
        assert new_ifaces[0].enabled is False
        assert new_ifaces[1].enabled is True
        assert new_ifaces[2].enabled is True
        assert new_ifaces[3].enabled is False

    def test_toggle_preserves_comments(self, config_file):
        lines, interfaces = parse_rns_config(config_file)
        new_lines = set_interface_enabled(lines, interfaces[0], False)
        text = "".join(new_lines)
        assert "# Reticulum Configuration" in text


class TestRoundTrip:
    def test_write_preserves_content(self, config_file):
        lines, _ = parse_rns_config(config_file)
        write_rns_config(config_file, lines)
        with open(config_file, "r", encoding="utf-8") as f:
            result = f.read()
        assert result == SAMPLE_CONFIG

    def test_atomic_write(self, config_file):
        lines, _ = parse_rns_config(config_file)
        write_rns_config(config_file, lines)
        # No leftover temp files
        d = os.path.dirname(config_file)
        temps = [f for f in os.listdir(d) if f.startswith(".rns_config_")]
        assert temps == []


class TestAddInterface:
    def test_add_rnode(self, config_file):
        lines, interfaces = parse_rns_config(config_file)
        assert len(interfaces) == 4
        new_lines = add_interface_section(
            lines,
            "RNode LoRa Interface",
            "RNodeInterface",
            {
                "port": "/dev/ttyACM0",
                "frequency": "915000000",
                "bandwidth": "125000",
                "txpower": "7",
                "spreadingfactor": "8",
                "codingrate": "5",
            },
        )
        with open(config_file, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        _, new_ifaces = parse_rns_config(config_file)
        assert len(new_ifaces) == 5
        rnode = new_ifaces[4]
        assert rnode.name == "RNode LoRa Interface"
        assert rnode.iface_type == "RNodeInterface"
        assert rnode.enabled is True
        assert rnode.properties["port"] == "/dev/ttyACM0"
        assert rnode.properties["frequency"] == "915000000"

    def test_add_preserves_existing(self, config_file):
        lines, _ = parse_rns_config(config_file)
        new_lines = add_interface_section(
            lines, "New One", "AutoInterface", {}
        )
        with open(config_file, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        _, new_ifaces = parse_rns_config(config_file)
        # Original 4 + new 1
        assert len(new_ifaces) == 5
        assert new_ifaces[0].name == "Auto Discovery Interface"
        assert new_ifaces[4].name == "New One"

    def test_add_to_empty_interfaces(self, tmp_path):
        config = textwrap.dedent("""\
            [reticulum]
              enable_transport = True

            [interfaces]
        """)
        p = tmp_path / "config"
        p.write_text(config, encoding="utf-8")
        lines, ifaces = parse_rns_config(str(p))
        assert len(ifaces) == 0
        new_lines = add_interface_section(lines, "Test", "AutoInterface", {})
        p.write_text("".join(new_lines), encoding="utf-8")
        _, new_ifaces = parse_rns_config(str(p))
        assert len(new_ifaces) == 1
        assert new_ifaces[0].name == "Test"


class TestNoEnabledLine:
    def test_insert_enabled_when_missing(self, tmp_path):
        config = textwrap.dedent("""\
            [interfaces]
            [[Bare Interface]]
              type = AutoInterface
        """)
        p = tmp_path / "config"
        p.write_text(config, encoding="utf-8")
        lines, ifaces = parse_rns_config(str(p))
        assert len(ifaces) == 1
        assert ifaces[0].enabled_line == -1
        new_lines = set_interface_enabled(lines, ifaces[0], False)
        p.write_text("".join(new_lines), encoding="utf-8")
        _, new_ifaces = parse_rns_config(str(p))
        assert new_ifaces[0].enabled is False
