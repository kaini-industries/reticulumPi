"""Tests for the Reticulum config parser/writer."""

from __future__ import annotations

import os
import textwrap

import pytest

from reticulumpi import rns_config
from reticulumpi.rns_config import (
    InterfaceEntry,
    RNSConfigError,
    add_interface_section,
    parse_enabled_rns_serial_interfaces,
    parse_rns_config,
    parse_rns_config_from_lines,
    remove_interface_property,
    set_interface_enabled,
    set_interface_property,
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

    def test_configobj_scalar_forms_and_unindented_keys_are_parsed_without_mutation(self):
        source = """\
 [interfaces] # top-level comment
 [["Quoted RNode"]] # interface comment
type = "RNodeInterface" # type comment
interface_enabled = 'yes' # enable comment
port = "/dev/serial/by-id/radio#one" # port comment
[[[Channel One]]]
enabled = yes
port = /dev/must-not-replace-parent
"""
        lines = source.splitlines(keepends=True)

        returned_lines, interfaces = parse_rns_config_from_lines(lines)

        assert returned_lines is lines
        assert "".join(returned_lines) == source
        assert len(interfaces) == 1
        interface = interfaces[0]
        assert interface.name == "Quoted RNode"
        assert interface.iface_type == "RNodeInterface"
        assert interface.enabled is True
        assert interface.properties["port"] == "/dev/serial/by-id/radio#one"
        assert interface.enabled_lines == [3]

    def test_missing_enable_flags_match_rns_disabled_default(self, tmp_path):
        path = tmp_path / "config"
        path.write_text(
            "[interfaces]\n[[RNode]]\ntype = RNodeInterface\nport = /dev/rnode\n",
            encoding="utf-8",
        )

        _, interfaces = parse_rns_config(str(path))

        assert interfaces[0].enabled is False
        assert interfaces[0].enabled_line == -1
        assert interfaces[0].enabled_lines == []

    @pytest.mark.parametrize(
        ("flags", "expected"),
        [
            ("interface_enabled = yes", True),
            ("interface_enabled = no\nenabled = yes", True),
            ("interface_enabled = yes\nenabled = no", True),
            ("interface_enabled = no\nenabled = no", False),
        ],
    )
    def test_enabled_aliases_use_rns_or_semantics(self, tmp_path, flags, expected):
        path = tmp_path / "config"
        path.write_text(
            f"[interfaces]\n[[RNode]]\ntype = RNodeInterface\n{flags}\n",
            encoding="utf-8",
        )

        _, interfaces = parse_rns_config(str(path))

        assert interfaces[0].enabled is expected


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

    def test_disable_updates_both_rns_enable_aliases_and_preserves_inline_comments(self):
        source = """\
[interfaces]
[[RNode]]
type = RNodeInterface
interface_enabled = "yes"# legacy comment
enabled = 'yes' # current comment
port = /dev/rnode
"""
        lines = source.splitlines(keepends=True)
        _, interfaces = parse_rns_config_from_lines(lines)

        updated = set_interface_enabled(lines, interfaces[0], False)
        _, reparsed = parse_rns_config_from_lines(updated)

        assert "interface_enabled = no# legacy comment\n" in updated
        assert "enabled = no # current comment\n" in updated
        assert reparsed[0].enabled is False

    def test_property_update_preserves_inline_comment_and_other_bytes(self):
        source = """\
[interfaces]
[[RNode]]
type = RNodeInterface
enabled = yes
port = "/dev/old#identity"   # keep this comment
"""
        lines = source.splitlines(keepends=True)
        _, interfaces = parse_rns_config_from_lines(lines)

        updated = set_interface_property(lines, interfaces[0], "port", "/dev/new")

        assert updated[:-1] == lines[:-1]
        assert updated[-1] == "port = /dev/new   # keep this comment\n"


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
        new_lines = add_interface_section(lines, "New One", "AutoInterface", {})
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


class TestSerialReservationParserDefensivePaths:
    @pytest.mark.parametrize(
        "interface_types",
        [set(), {""}, {"RNodeInterface", 1}],
    )
    def test_rejects_invalid_serial_interface_type_sets(self, tmp_path, interface_types):
        with pytest.raises(ValueError, match="non-empty strings"):
            parse_enabled_rns_serial_interfaces(
                str(tmp_path / "does-not-need-to-exist"),
                interface_types,
            )

    def test_absent_interfaces_section_is_empty(self, tmp_path):
        path = tmp_path / "config"
        path.write_text("[reticulum]\nenable_transport = yes\n", encoding="utf-8")

        assert (
            parse_enabled_rns_serial_interfaces(
                str(path),
                {"RNodeInterface"},
            )
            == []
        )

    def test_scalar_interfaces_value_is_rejected(self, tmp_path):
        path = tmp_path / "config"
        path.write_text("interfaces = not-a-section\n", encoding="utf-8")

        with pytest.raises(RNSConfigError, match="must be a section"):
            parse_enabled_rns_serial_interfaces(str(path), {"RNodeInterface"})

    def test_unresolved_interface_type_is_rejected(self, tmp_path):
        path = tmp_path / "config"
        path.write_text(
            "[interfaces]\n[[RNode]]\ntype = %(missing_type)s\nenabled = yes\nport = /dev/rnode\n",
            encoding="utf-8",
        )

        with pytest.raises(RNSConfigError, match="Could not resolve.*type"):
            parse_enabled_rns_serial_interfaces(str(path), {"RNodeInterface"})


class TestEditorDefensivePaths:
    def test_toggle_honors_legacy_enabled_line_field(self):
        lines = ["[[RNode]]\n", "enabled = yes\n"]
        entry = InterfaceEntry(name="RNode", enabled_line=1)

        assert set_interface_enabled(lines, entry, False) == [
            "[[RNode]]\n",
            "enabled = no\n",
        ]

    def test_property_insert_and_remove_stop_at_nested_section(self):
        lines = [
            "[interfaces]\n",
            "[[RNode]]\n",
            "type = RNodeInterface\n",
            "[[[Channel]]]\n",
            "port = /dev/nested\n",
        ]
        entry = InterfaceEntry(name="RNode", start_line=1)

        inserted = set_interface_property(lines, entry, "port", "/dev/parent")
        assert inserted == [
            "[interfaces]\n",
            "[[RNode]]\n",
            "type = RNodeInterface\n",
            "port = /dev/parent\n",
            "[[[Channel]]]\n",
            "port = /dev/nested\n",
        ]
        assert remove_interface_property(inserted, entry, "missing") == inserted

    def test_scalar_parse_failure_preserves_source_spelling(self, monkeypatch):
        def invalid_config(*_args, **_kwargs):
            raise rns_config.ConfigObjError("invalid scalar")

        monkeypatch.setattr(rns_config, "ConfigObj", invalid_config)

        assert rns_config._parse_configobj_scalar("  raw value  ") == "raw value"

    def test_property_replacement_handles_missing_equals_and_escaped_quotes(self):
        assert rns_config._replace_property_value("not a property\n", "new") == ("not a property\n")
        assert (
            rns_config._replace_property_value(
                'port = "/dev/radio\\"one"  # retain\n',
                "/dev/new",
            )
            == "port = /dev/new  # retain\n"
        )
