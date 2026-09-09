"""Deterministic contract tests for the lab-only hardware smoke runner."""

from __future__ import annotations

import datetime as dt
import json
import os
from collections import deque
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tools import lab_hil


VALID_RNS_CONFIG = """\
[reticulum]
  share_instance = false
  enable_transport = false
  discover_interfaces = false
  autoconnect_discovered_interfaces = 0

[interfaces]
  [[Lab Client RNode]]
    type = RNodeInterface
    enabled = yes
    port = /dev/rnode-lab
    frequency = 915000000
    bandwidth = 125000
    txpower = 7
    spreadingfactor = 8
    codingrate = 5
"""


def _config_document(tmp_path: Path) -> dict[str, Any]:
    rns_config = tmp_path / "rns"
    rns_config.mkdir(mode=0o700)
    rns_config.chmod(0o700)
    rns_file = rns_config / "config"
    rns_file.write_text(VALID_RNS_CONFIG, encoding="utf-8")
    rns_file.chmod(0o600)
    identity = tmp_path / "client.identity"
    identity.write_bytes(bytes(range(64)))
    identity.chmod(0o600)
    return {
        "schema": 1,
        "classification": lab_hil.CLASSIFICATION,
        "production": False,
        "fixture_id": "bookworm-lab-a",
        "destination": "ab" * 16,
        "rns_config_dir": str(rns_config),
        "client_identity": str(identity),
        "expected_node": "remote-lab-node",
        "expected_version": "0.3.8.dev1+g1234567",
        "expected_local_interface_name": "Lab Client RNode",
        "expected_local_port": "/dev/rnode-lab",
        "expected_interface_name": "RNodeInterface[RNode LoRa]",
        "expected_interface_type": "RNodeInterface",
        "timeout_seconds": 30,
    }


def _write_config(tmp_path: Path, document: dict[str, Any]) -> Path:
    path = tmp_path / "lab-hil.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)
    return path


def _interface_response(
    *, rxb: int, txb: int, online: bool = True, extra_interfaces: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "ok": True,
        "data": [
            {
                "name": "RNodeInterface[RNode LoRa]",
                "type": "RNodeInterface",
                "online": online,
                "rxb": rxb,
                "txb": txb,
            }
        ]
        + (extra_interfaces or []),
    }


class _FakeClient:
    def __init__(
        self,
        *,
        version: str = "0.3.8.dev1+g1234567",
        after: tuple[int, int] = (140, 260),
        extra_interfaces: list[dict[str, Any]] | None = None,
    ) -> None:
        self.closed = False
        self.paths: list[str] = []
        self.responses = deque(
            [
                _interface_response(rxb=100, txb=200, extra_interfaces=extra_interfaces),
                {"ok": True, "node": "remote-lab-node", "time": 123.0},
                {
                    "ok": True,
                    "data": {
                        "node_name": "remote-lab-node",
                        "version": version,
                        "failed_plugins": [],
                    },
                },
                _interface_response(rxb=after[0], txb=after[1], extra_interfaces=extra_interfaces),
            ]
        )

    def connect(self) -> bool:
        return True

    def request(self, path: str, data: Any = None, timeout: float | None = None) -> dict[str, Any]:
        assert data is None
        assert timeout == 30.0
        self.paths.append(path)
        return self.responses.popleft()

    def close(self) -> None:
        self.closed = True


class _FakePipeEnd:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeReceiver(_FakePipeEnd):
    def __init__(self, result: Any = None) -> None:
        super().__init__()
        self.result = result

    def poll(self, timeout: float) -> bool:
        return self.result is not None

    def recv(self) -> Any:
        if self.result is None:
            raise AssertionError("no child result was expected")
        return self.result


class _FakeProcess:
    def __init__(self, *, alive: bool, exitcode: int | None) -> None:
        self._alive = alive
        self.exitcode = exitcode
        self.started = False
        self.terminated = False
        self.killed = False
        self.closed = False
        self.join_timeouts: list[float] = []

    def start(self) -> None:
        self.started = True

    def join(self, timeout: float) -> None:
        self.join_timeouts.append(timeout)

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False
        self.exitcode = -15

    def kill(self) -> None:
        self.killed = True
        self._alive = False
        self.exitcode = -9

    def close(self) -> None:
        self.closed = True


class _FakeProcessContext:
    def __init__(self, process: _FakeProcess, result: Any = None) -> None:
        self.process = process
        self.receiver = _FakeReceiver(result)
        self.sender = _FakePipeEnd()
        self.process_args: tuple[Any, ...] | None = None

    def Pipe(self, *, duplex: bool) -> tuple[_FakeReceiver, _FakePipeEnd]:
        assert duplex is False
        return self.receiver, self.sender

    def Process(self, *, target: Any, args: tuple[Any, ...], daemon: bool) -> _FakeProcess:
        assert target is lab_hil._isolated_probe_child
        assert daemon is True
        self.process_args = args
        return self.process


def _clock() -> Any:
    values = iter(
        (
            dt.datetime(2026, 9, 8, 12, 0, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 9, 8, 12, 0, 2, tzinfo=dt.timezone.utc),
        )
    )
    return lambda: next(values)


def test_load_config_requires_explicit_nonproduction_classification(tmp_path: Path) -> None:
    document = _config_document(tmp_path)
    config = lab_hil.load_config(_write_config(tmp_path, document))

    assert config.fixture_id == "bookworm-lab-a"
    assert config.timeout_seconds == 30.0
    assert config.identity_bytes == bytes(range(64))
    assert config.expected_local_interface_name == "Lab Client RNode"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"classification": "release-candidate"}, "config_classification_invalid"),
        ({"production": True}, "config_production_forbidden"),
        ({"timeout_seconds": True}, "config_timeout_invalid"),
        ({"timeout_seconds": 61}, "config_timeout_invalid"),
        ({"destination": "AB" * 16}, "config_field_invalid"),
        ({"gate85_eligible": True}, "config_keys_invalid"),
    ],
)
def test_load_config_rejects_release_or_malformed_inputs(
    tmp_path: Path, change: dict[str, Any], code: str
) -> None:
    document = _config_document(tmp_path)
    document.update(change)

    with pytest.raises(lab_hil.LabHilError) as stopped:
        lab_hil.load_config(_write_config(tmp_path, document))

    assert stopped.value.code == code


def test_load_config_rejects_nonprivate_identity(tmp_path: Path) -> None:
    document = _config_document(tmp_path)
    Path(document["client_identity"]).chmod(0o644)

    with pytest.raises(lab_hil.LabHilError) as stopped:
        lab_hil.load_config(_write_config(tmp_path, document))

    assert stopped.value.code == "identity_unsafe"


def test_load_config_rejects_invalid_identity_material(tmp_path: Path) -> None:
    document = _config_document(tmp_path)
    Path(document["client_identity"]).write_bytes(b"not an RNS identity")

    with pytest.raises(lab_hil.LabHilError) as stopped:
        lab_hil.load_config(_write_config(tmp_path, document))

    assert stopped.value.code == "identity_invalid"


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("missing", "rns_config_file_unavailable"),
        ("symlink", "rns_config_file_unsafe"),
        ("public", "rns_config_file_unsafe"),
    ],
)
def test_load_config_rejects_missing_symlinked_or_public_rns_config(
    tmp_path: Path, mutation: str, code: str
) -> None:
    document = _config_document(tmp_path)
    config_path = Path(document["rns_config_dir"]) / "config"
    if mutation == "missing":
        config_path.unlink()
    elif mutation == "symlink":
        target = tmp_path / "rns-target"
        target.write_text(VALID_RNS_CONFIG, encoding="utf-8")
        target.chmod(0o600)
        config_path.unlink()
        config_path.symlink_to(target)
    else:
        config_path.chmod(0o640)

    with pytest.raises(lab_hil.LabHilError) as stopped:
        lab_hil.load_config(_write_config(tmp_path, document))

    assert stopped.value.code == code


@pytest.mark.parametrize(
    "unsafe_reticulum",
    [
        "share_instance = true",
        "enable_transport = true",
        "discover_interfaces = true",
        "autoconnect_discovered_interfaces = 1",
        "network_identity = 00",
    ],
)
def test_load_config_rejects_shared_transport_or_dynamic_rns_paths(
    tmp_path: Path, unsafe_reticulum: str
) -> None:
    document = _config_document(tmp_path)
    rns_path = Path(document["rns_config_dir"]) / "config"
    if unsafe_reticulum.startswith("network_identity"):
        content = VALID_RNS_CONFIG.replace(
            "  share_instance = false", f"  share_instance = false\n  {unsafe_reticulum}"
        )
    else:
        key = unsafe_reticulum.split(" = ", 1)[0]
        content = VALID_RNS_CONFIG.replace(
            next(line.strip() for line in VALID_RNS_CONFIG.splitlines() if key in line),
            unsafe_reticulum,
        )
    rns_path.write_text(content, encoding="utf-8")

    with (
        patch("RNS.Reticulum") as reticulum,
        pytest.raises(lab_hil.LabHilError) as stopped,
    ):
        lab_hil.load_config(_write_config(tmp_path, document))

    assert stopped.value.code == "rns_topology_unsafe"
    reticulum.assert_not_called()


@pytest.mark.parametrize(
    ("line", "replacement"),
    [
        ("  share_instance = false\n", ""),
        ("  enable_transport = false\n", "  enable_transport = maybe\n"),
        ("  discover_interfaces = false\n", "  Discover_Interfaces = false\n"),
        (
            "  autoconnect_discovered_interfaces = 0\n",
            "  autoconnect_discovered_interfaces = maybe\n",
        ),
    ],
)
def test_load_config_requires_explicit_valid_rns_isolation_flags(
    tmp_path: Path, line: str, replacement: str
) -> None:
    document = _config_document(tmp_path)
    rns_path = Path(document["rns_config_dir"]) / "config"
    rns_path.write_text(VALID_RNS_CONFIG.replace(line, replacement), encoding="utf-8")

    with pytest.raises(lab_hil.LabHilError) as stopped:
        lab_hil.load_config(_write_config(tmp_path, document))

    assert stopped.value.code == "rns_topology_unsafe"


def test_load_config_rejects_duplicate_configobj_sections(tmp_path: Path) -> None:
    document = _config_document(tmp_path)
    rns_path = Path(document["rns_config_dir"]) / "config"
    rns_path.write_text(VALID_RNS_CONFIG + "\n[reticulum]\n", encoding="utf-8")

    with pytest.raises(lab_hil.LabHilError) as stopped:
        lab_hil.load_config(_write_config(tmp_path, document))

    assert stopped.value.code == "rns_config_parse_failed"


def test_load_config_uses_configobj_interpolation_and_rns_short_circuit(tmp_path: Path) -> None:
    document = _config_document(tmp_path)
    rns_path = Path(document["rns_config_dir"]) / "config"
    rns_path.write_text(
        VALID_RNS_CONFIG.replace(
            "    enabled = yes",
            "    interface_enabled = yes\n    enabled = not-a-boolean",
        ).replace("    spreadingfactor = 8", "    spreadingfactor = %(codingrate)s"),
        encoding="utf-8",
    )

    config = lab_hil.load_config(_write_config(tmp_path, document))

    assert config.expected_local_port == "/dev/rnode-lab"


def test_load_config_rejects_invalid_first_rns_enabled_flag(tmp_path: Path) -> None:
    document = _config_document(tmp_path)
    rns_path = Path(document["rns_config_dir"]) / "config"
    rns_path.write_text(
        VALID_RNS_CONFIG.replace(
            "    enabled = yes", "    interface_enabled = maybe\n    enabled = yes"
        ),
        encoding="utf-8",
    )

    with pytest.raises(lab_hil.LabHilError) as stopped:
        lab_hil.load_config(_write_config(tmp_path, document))

    assert stopped.value.code == "rns_topology_unsafe"


@pytest.mark.parametrize(
    "extra_interface",
    [
        "",
        "\n  [[TCP escape]]\n    type = TCPClientInterface\n    enabled = yes\n",
    ],
)
def test_load_config_requires_exactly_one_enabled_rnode(
    tmp_path: Path, extra_interface: str
) -> None:
    document = _config_document(tmp_path)
    rns_path = Path(document["rns_config_dir"]) / "config"
    content = VALID_RNS_CONFIG + extra_interface
    if not extra_interface:
        content = content.replace("    enabled = yes", "    enabled = no")
    rns_path.write_text(content, encoding="utf-8")

    with pytest.raises(lab_hil.LabHilError) as stopped:
        lab_hil.load_config(_write_config(tmp_path, document))

    assert stopped.value.code == "rns_topology_unsafe"


def test_load_config_allows_disabled_nonradio_sections(tmp_path: Path) -> None:
    document = _config_document(tmp_path)
    rns_path = Path(document["rns_config_dir"]) / "config"
    rns_path.write_text(
        VALID_RNS_CONFIG
        + "\n  [[Disabled TCP]]\n    type = TCPClientInterface\n    enabled = no\n",
        encoding="utf-8",
    )

    config = lab_hil.load_config(_write_config(tmp_path, document))

    assert config.expected_local_interface_name == "Lab Client RNode"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected_local_interface_name", "Another RNode"),
        ("expected_local_port", "/dev/ttyUSB0"),
        ("expected_local_port", "/dev/another-rnode"),
        ("expected_interface_type", "AutoInterface"),
    ],
)
def test_load_config_binds_exact_local_rnode_and_stable_port(
    tmp_path: Path, field: str, value: str
) -> None:
    document = _config_document(tmp_path)
    document[field] = value

    with pytest.raises(lab_hil.LabHilError) as stopped:
        lab_hil.load_config(_write_config(tmp_path, document))

    assert stopped.value.code == "rns_topology_unsafe"


@pytest.mark.parametrize(
    ("configured_type", "configured_port"),
    [
        ("AutoInterface", "/dev/rnode-lab"),
        ("RNodeInterface", "/dev/ttyUSB0"),
    ],
)
def test_load_config_rejects_wrong_type_or_indexed_configured_port(
    tmp_path: Path, configured_type: str, configured_port: str
) -> None:
    document = _config_document(tmp_path)
    document["expected_local_port"] = configured_port
    rns_path = Path(document["rns_config_dir"]) / "config"
    rns_path.write_text(
        VALID_RNS_CONFIG.replace("type = RNodeInterface", f"type = {configured_type}").replace(
            "port = /dev/rnode-lab", f"port = {configured_port}"
        ),
        encoding="utf-8",
    )

    with pytest.raises(lab_hil.LabHilError) as stopped:
        lab_hil.load_config(_write_config(tmp_path, document))

    assert stopped.value.code == "rns_topology_unsafe"


@pytest.mark.parametrize(
    ("key", "replacement"),
    [
        ("frequency", None),
        ("frequency", "not-an-integer"),
        ("frequency", "136999999"),
        ("frequency", "3000000001"),
        ("bandwidth", "7799"),
        ("bandwidth", "1625001"),
        ("txpower", "-1"),
        ("txpower", "38"),
        ("spreadingfactor", "4"),
        ("spreadingfactor", "13"),
        ("codingrate", "4"),
        ("codingrate", "9"),
    ],
)
def test_load_config_rejects_missing_invalid_or_out_of_range_rnode_radio_settings(
    tmp_path: Path, key: str, replacement: str | None
) -> None:
    document = _config_document(tmp_path)
    rns_path = Path(document["rns_config_dir"]) / "config"
    line = next(
        line for line in VALID_RNS_CONFIG.splitlines() if line.strip().startswith(f"{key} =")
    )
    updated = VALID_RNS_CONFIG.replace(
        f"{line}\n", "" if replacement is None else f"    {key} = {replacement}\n"
    )
    rns_path.write_text(updated, encoding="utf-8")

    with (
        patch("RNS.Reticulum") as reticulum,
        pytest.raises(lab_hil.LabHilError) as stopped,
    ):
        lab_hil.load_config(_write_config(tmp_path, document))

    assert stopped.value.code == "rns_topology_unsafe"
    reticulum.assert_not_called()


def test_load_config_rejects_lab_state_inside_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = _config_document(tmp_path)
    config_path = _write_config(tmp_path, document)
    monkeypatch.setattr(lab_hil, "REPOSITORY_ROOT", tmp_path)

    with pytest.raises(lab_hil.LabHilError) as stopped:
        lab_hil.load_config(config_path)

    assert stopped.value.code == "config_unsafe"


def test_default_client_uses_only_the_configured_identity_and_rns_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = _config_document(tmp_path)
    config = lab_hil.load_config(_write_config(tmp_path, document))
    captured: dict[str, Any] = {}
    sentinel = object()

    def constructor(**kwargs: Any) -> object:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr("reticulumpi.remote_client.RemoteClient", constructor)

    assert lab_hil._default_client_factory(config) is sentinel
    snapshot = Path(captured.pop("reticulum_config_dir"))
    assert captured == {
        "destination_hex": config.destination,
        "timeout": config.timeout_seconds,
        "identity_bytes": config.identity_bytes,
        "output": None,
    }
    assert snapshot != Path(document["rns_config_dir"])
    assert snapshot.joinpath("config").read_bytes() == config.rns_config_bytes
    assert snapshot.stat().st_mode & 0o777 == 0o500
    assert snapshot.joinpath("config").stat().st_mode & 0o777 == 0o400
    assert snapshot.joinpath("storage").stat().st_mode & 0o777 == 0o700
    assert snapshot.joinpath("storage/cache").stat().st_mode & 0o777 == 0o700


def test_default_client_never_reopens_replaced_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = _config_document(tmp_path)
    config = lab_hil.load_config(_write_config(tmp_path, document))
    identity_path = Path(document["client_identity"])
    identity_path.unlink()
    identity_path.write_bytes(b"replacement must not be read")
    captured: dict[str, Any] = {}

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("reticulumpi.remote_client.RemoteClient", Client)

    lab_hil._default_client_factory(config)

    assert captured["identity_bytes"] == bytes(range(64))
    assert "identity_path" not in captured


def test_default_client_never_reopens_replaced_rns_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = _config_document(tmp_path)
    config = lab_hil.load_config(_write_config(tmp_path, document))
    original_payload = config.rns_config_bytes
    original_path = Path(document["rns_config_dir"]) / "config"
    original_path.write_text(
        VALID_RNS_CONFIG.replace("share_instance = false", "share_instance = true"),
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("reticulumpi.remote_client.RemoteClient", Client)

    lab_hil._default_client_factory(config)

    snapshot = Path(captured["reticulum_config_dir"])
    assert snapshot != Path(document["rns_config_dir"])
    assert snapshot.joinpath("config").read_bytes() == original_payload
    assert b"share_instance = true" not in snapshot.joinpath("config").read_bytes()


def test_isolated_probe_accepts_only_bounded_success_result_and_cleans_snapshot(
    tmp_path: Path,
) -> None:
    config = lab_hil.load_config(_write_config(tmp_path, _config_document(tmp_path)))
    expected = lab_hil._execute_probe(config, lambda _config: _FakeClient())
    process = _FakeProcess(alive=False, exitcode=0)
    context = _FakeProcessContext(process, expected)

    result = lab_hil._run_isolated_probe(config, context=context)

    assert result == expected
    assert context.process_args is not None
    snapshot = Path(context.process_args[1])
    assert not snapshot.exists()


def test_isolated_probe_maps_os_exit_like_child_termination_without_leaking_snapshot(
    tmp_path: Path,
) -> None:
    config = lab_hil.load_config(_write_config(tmp_path, _config_document(tmp_path)))
    process = _FakeProcess(alive=False, exitcode=255)
    context = _FakeProcessContext(process)

    result = lab_hil._run_isolated_probe(config, context=context)

    assert result == lab_hil._failed_probe_result("probe_child_exit")
    assert process.started is True
    assert process.terminated is False
    assert context.process_args is not None
    snapshot = Path(context.process_args[1])
    assert not snapshot.exists()


def test_isolated_probe_timeout_terminates_child_and_cleans_private_snapshot(
    tmp_path: Path,
) -> None:
    config = lab_hil.load_config(_write_config(tmp_path, _config_document(tmp_path)))
    process = _FakeProcess(alive=True, exitcode=None)
    context = _FakeProcessContext(process)

    result = lab_hil._run_isolated_probe(config, context=context, timeout_seconds=0.01)

    assert result == lab_hil._failed_probe_result("probe_child_timeout")
    assert process.terminated is True
    assert process.killed is False
    assert process.join_timeouts == [0.01, lab_hil.ISOLATED_PROBE_STOP_GRACE_SECONDS]
    assert context.process_args is not None
    snapshot = Path(context.process_args[1])
    assert not snapshot.exists()


@pytest.mark.parametrize("failure_code", ["probe_child_exit", "probe_child_timeout"])
def test_isolated_child_failure_still_publishes_stable_redacted_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_code: str
) -> None:
    document = _config_document(tmp_path)
    config = lab_hil.load_config(_write_config(tmp_path, document))
    report_path = tmp_path / f"{failure_code}.json"
    monkeypatch.setattr(
        lab_hil,
        "_run_isolated_probe",
        lambda _config: lab_hil._failed_probe_result(failure_code),
    )

    with pytest.raises(lab_hil.LabHilError) as stopped:
        lab_hil.run(config, report_path, clock=_clock())

    assert stopped.value.code == failure_code
    report = json.loads(report_path.read_text(encoding="ascii"))
    assert report["status"] == "failed"
    assert report["failure"] == {"code": failure_code}
    assert report["checks"] == lab_hil._new_checks()
    payload = report_path.read_text(encoding="ascii")
    assert document["destination"] not in payload
    assert document["client_identity"] not in payload
    assert document["rns_config_dir"] not in payload
    assert config.identity_bytes.hex() not in payload
    assert report["fixture_id"] in payload
    assert report["target_digest"] in payload


def test_run_records_fixed_authenticated_round_trip_without_release_authority(
    tmp_path: Path,
) -> None:
    document = _config_document(tmp_path)
    config_path = _write_config(tmp_path, document)
    config = lab_hil.load_config(config_path)
    client = _FakeClient()
    report_path = tmp_path / "report.json"

    report = lab_hil.run(
        config,
        report_path,
        client_factory=lambda _config: client,
        clock=_clock(),
    )

    assert client.paths == ["/interfaces", "/ping", "/status", "/interfaces"]
    assert client.closed is True
    assert report["status"] == "passed"
    assert report["release_evidence"] is False
    assert report["gate85_eligible"] is False
    assert report["operator_declared_nonproduction"] is True
    assert "production" not in report
    assert report["checks"]["client_close_invoked"] is True
    assert "client_closed" not in report["checks"]
    assert report["observed"]["interface"]["rx_bytes_delta"] == 40
    assert report["observed"]["interface"]["tx_bytes_delta"] == 60
    assert report_path.stat().st_mode & 0o777 == 0o600

    payload = report_path.read_text(encoding="ascii")
    assert (
        payload
        == json.dumps(
            json.loads(payload),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    assert document["destination"] not in payload
    assert document["client_identity"] not in payload
    assert document["rns_config_dir"] not in payload
    assert config.identity_bytes.hex() not in payload
    assert report["fixture_id"] in payload
    assert report["observed"]["node"] in payload
    assert report["observed"]["version"] in payload
    assert report["observed"]["interface"]["name"] in payload
    assert report["target_digest"] in payload


def test_run_rejects_another_online_nonlocal_remote_interface(tmp_path: Path) -> None:
    config = lab_hil.load_config(_write_config(tmp_path, _config_document(tmp_path)))
    client = _FakeClient(
        extra_interfaces=[
            {
                "name": "TCPClientInterface[escape]",
                "type": "TCPClientInterface",
                "online": True,
                "rxb": 10,
                "txb": 20,
            }
        ]
    )

    with pytest.raises(lab_hil.LabHilError) as stopped:
        lab_hil.run(
            config,
            tmp_path / "ambiguous.json",
            client_factory=lambda _config: client,
            clock=_clock(),
        )

    assert stopped.value.code == "interface_path_ambiguous"


@pytest.mark.parametrize(
    "extra_interface",
    [
        {
            "name": "TCPClientInterface[offline]",
            "type": "TCPClientInterface",
            "online": False,
            "rxb": 0,
            "txb": 0,
        },
        {
            "name": "LocalClientInterface[Local]",
            "type": "LocalClientInterface",
            "online": True,
            "rxb": 1,
            "txb": 1,
        },
        {
            "name": "LocalServerInterface[Local]",
            "type": "LocalServerInterface",
            "online": True,
            "rxb": 1,
            "txb": 1,
        },
    ],
)
def test_run_ignores_offline_or_local_remote_interfaces(
    tmp_path: Path, extra_interface: dict[str, Any]
) -> None:
    config = lab_hil.load_config(_write_config(tmp_path, _config_document(tmp_path)))

    report = lab_hil.run(
        config,
        tmp_path / "unambiguous.json",
        client_factory=lambda _config: _FakeClient(extra_interfaces=[extra_interface]),
        clock=_clock(),
    )

    assert report["status"] == "passed"


@pytest.mark.parametrize(
    ("client", "code"),
    [
        (_FakeClient(version="0.3.9"), "version_mismatch"),
        (_FakeClient(after=(100, 260)), "interface_counters_static"),
        (_FakeClient(after=(140, 200)), "interface_counters_static"),
    ],
)
def test_run_fails_closed_and_still_writes_nonrelease_report(
    tmp_path: Path, client: _FakeClient, code: str
) -> None:
    config = lab_hil.load_config(_write_config(tmp_path, _config_document(tmp_path)))
    report_path = tmp_path / "failed-report.json"

    with pytest.raises(lab_hil.LabHilError) as stopped:
        lab_hil.run(
            config,
            report_path,
            client_factory=lambda _config: client,
            clock=_clock(),
        )

    assert stopped.value.code == code
    assert client.closed is True
    report = json.loads(report_path.read_text(encoding="ascii"))
    assert report["status"] == "failed"
    assert report["failure"] == {"code": code}
    assert report["release_evidence"] is False
    assert report["gate85_eligible"] is False


def test_authenticated_check_requires_a_successful_protected_request(tmp_path: Path) -> None:
    config = lab_hil.load_config(_write_config(tmp_path, _config_document(tmp_path)))
    client = _FakeClient()
    client.responses[0] = {"ok": False, "error": "unauthorized"}
    report_path = tmp_path / "unauthorized.json"

    with pytest.raises(lab_hil.LabHilError) as stopped:
        lab_hil.run(
            config,
            report_path,
            client_factory=lambda _config: client,
            clock=_clock(),
        )

    assert stopped.value.code == "response_invalid"
    report = json.loads(report_path.read_text(encoding="ascii"))
    assert report["checks"]["authenticated_link"] is False


def test_run_refuses_to_replace_a_report_before_constructing_client(tmp_path: Path) -> None:
    config = lab_hil.load_config(_write_config(tmp_path, _config_document(tmp_path)))
    report_path = tmp_path / "report.json"
    report_path.write_text("preserve\n", encoding="utf-8")
    constructed = False

    def factory(_config: lab_hil.LabHilConfig) -> _FakeClient:
        nonlocal constructed
        constructed = True
        return _FakeClient()

    with pytest.raises(lab_hil.LabHilError) as stopped:
        lab_hil.run(config, report_path, client_factory=factory, clock=_clock())

    assert stopped.value.code == "report_exists"
    assert constructed is False
    assert report_path.read_text(encoding="utf-8") == "preserve\n"


@pytest.mark.parametrize("directory_name", [".codex-r32-lab", "release-verification"])
def test_report_path_rejects_evidence_namespaces(tmp_path: Path, directory_name: str) -> None:
    report_directory = tmp_path / directory_name
    report_directory.mkdir(mode=0o700)

    with pytest.raises(lab_hil.LabHilError) as stopped:
        lab_hil._validate_report_path(report_directory / "report.json")

    assert stopped.value.code == "report_path_forbidden"


def test_report_path_rejects_repository() -> None:
    with pytest.raises(lab_hil.LabHilError) as stopped:
        lab_hil._validate_report_path(lab_hil.REPOSITORY_ROOT / "lab-report.json")

    assert stopped.value.code == "report_path_forbidden"


def test_report_parent_swap_is_rejected_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_directory = tmp_path / "reports"
    report_directory.mkdir(mode=0o700)
    report_path = report_directory / "report.json"
    displaced_directory = tmp_path / "reports-before-swap"
    original_open = os.open
    swapped = False

    def swap_before_open(
        path: str | os.PathLike[str], flags: int, *args: Any, **kwargs: Any
    ) -> int:
        nonlocal swapped
        if not swapped and Path(path) == report_directory and flags & getattr(os, "O_DIRECTORY", 0):
            swapped = True
            report_directory.rename(displaced_directory)
            report_directory.mkdir(mode=0o700)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(lab_hil.os, "open", swap_before_open)

    with pytest.raises(lab_hil.LabHilError) as stopped:
        lab_hil._write_report(report_path, {"status": "passed"})

    assert stopped.value.code == "report_parent_unsafe"
    assert not report_path.exists()
    assert not displaced_directory.joinpath("report.json").exists()


def test_report_partial_write_never_publishes_or_leaves_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_path = tmp_path / "report.json"
    original_write = os.write
    writes = 0

    def fail_after_partial_write(descriptor: int, payload: bytes | memoryview) -> int:
        nonlocal writes
        writes += 1
        if writes == 1:
            return original_write(descriptor, payload[:4])
        raise OSError("injected partial write failure")

    monkeypatch.setattr(lab_hil.os, "write", fail_after_partial_write)

    with pytest.raises(lab_hil.LabHilError) as stopped:
        lab_hil._write_report(report_path, {"status": "passed"})

    assert stopped.value.code == "report_write_failed"
    assert not report_path.exists()
    assert list(tmp_path.glob(".lab-hil-*.tmp")) == []


def test_report_publish_race_preserves_existing_bytes_and_removes_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_path = tmp_path / "report.json"
    original_link = os.link

    def race_link(source: str, destination: str, **kwargs: Any) -> None:
        report_path.write_bytes(b"racer-owned\n")
        original_link(source, destination, **kwargs)

    monkeypatch.setattr(lab_hil.os, "link", race_link)

    with pytest.raises(lab_hil.LabHilError) as stopped:
        lab_hil._write_report(report_path, {"status": "passed"})

    assert stopped.value.code == "report_exists"
    assert report_path.read_bytes() == b"racer-owned\n"
    assert list(tmp_path.glob(".lab-hil-*.tmp")) == []
