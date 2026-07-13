"""Deterministic managed and operator dashboard TLS maintenance tests."""

from __future__ import annotations

import asyncio
import datetime
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from reticulumpi.builtin_plugins.web_dashboard.plugin import WebDashboardPlugin
from reticulumpi.builtin_plugins.web_dashboard.ssl_utils import (
    _atomic_write,
    _collect_san_strings,
    _normalise_now,
    generate_self_signed_cert,
    validate_cert_pair,
)
from reticulumpi.plugin_base import PluginHealth


UTC = datetime.timezone.utc
ISSUED_AT = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _make_plugin(mock_app, *, managed: bool, cert_file: str, key_file: str, cert_dir: str):
    mock_app.config.node_name = "TestNode"
    plugin = WebDashboardPlugin(mock_app, {"enabled": True, "password": "test"})
    plugin._active = True
    plugin._ssl_ctx = MagicMock()
    plugin._site = AsyncMock()
    plugin._tls_managed = managed
    plugin._tls_state = "valid"
    plugin._tls_last_check = None
    plugin._tls_last_renewal = None
    plugin._tls_last_error = None
    plugin._tls_degraded = False
    plugin._tls_failed_closed = False
    plugin._tls_cert_file = cert_file
    plugin._tls_key_file = key_file
    plugin._tls_cert_dir = cert_dir
    plugin._tls_common_name = "TestNode"
    plugin._tls_extra_hostnames = ["pi.test"]
    plugin._tls_required_sans = _collect_san_strings(plugin._tls_extra_hostnames) if managed else []
    return plugin


def _managed_material(tmp_path):
    cert_dir = str(tmp_path / "managed")
    cert_file, key_file = generate_self_signed_cert(
        cert_dir,
        "TestNode",
        extra_sans=["pi.test"],
        now=ISSUED_AT,
    )
    return cert_dir, cert_file, key_file


def test_managed_bundle_is_single_atomic_mode_0600_file(tmp_path):
    _, cert_file, key_file = _managed_material(tmp_path)

    assert cert_file == key_file
    assert os.stat(cert_file).st_mode & 0o777 == 0o600
    material = Path(cert_file).read_bytes()
    assert b"BEGIN CERTIFICATE" in material
    assert b"BEGIN RSA PRIVATE KEY" in material


def test_atomic_tls_write_handles_short_os_writes(tmp_path, monkeypatch):
    real_write = os.write
    calls = 0

    def short_write(fd, data):
        nonlocal calls
        calls += 1
        return real_write(fd, data[:7])

    monkeypatch.setattr(os, "write", short_write)
    destination = tmp_path / "bundle.pem"
    payload = b"certificate-and-private-key" * 16

    _atomic_write(str(destination), payload, 0o600)

    assert destination.read_bytes() == payload
    assert calls > 1
    assert destination.stat().st_mode & 0o777 == 0o600


def test_managed_check_does_not_renew_with_more_than_30_days(tmp_path, mock_app):
    cert_dir, cert_file, key_file = _managed_material(tmp_path)
    plugin = _make_plugin(
        mock_app,
        managed=True,
        cert_file=cert_file,
        key_file=key_file,
        cert_dir=cert_dir,
    )
    before = Path(cert_file).read_bytes()

    renewed = asyncio.run(
        plugin._check_tls_certificate(now=ISSUED_AT + datetime.timedelta(days=334))
    )

    assert renewed is False
    assert Path(cert_file).read_bytes() == before
    plugin._ssl_ctx.load_cert_chain.assert_not_called()
    assert plugin._tls_state == "valid"


def test_managed_check_renews_at_30_day_boundary_and_reloads(tmp_path, mock_app):
    cert_dir, cert_file, key_file = _managed_material(tmp_path)
    plugin = _make_plugin(
        mock_app,
        managed=True,
        cert_file=cert_file,
        key_file=key_file,
        cert_dir=cert_dir,
    )
    before = Path(cert_file).read_bytes()
    renewal_time = ISSUED_AT + datetime.timedelta(days=335)

    renewed = asyncio.run(plugin._check_tls_certificate(now=renewal_time))

    assert renewed is True
    assert Path(cert_file).read_bytes() != before
    plugin._ssl_ctx.load_cert_chain.assert_called_once_with(cert_file, key_file)
    assert plugin._tls_last_renewal == renewal_time.isoformat()
    assert plugin._tls_state == "renewed"
    assert WebDashboardPlugin.TLS_CHECK_INTERVAL_SECONDS == 86400


def test_reload_failure_restores_valid_bundle_and_marks_degraded(tmp_path, mock_app):
    cert_dir, cert_file, key_file = _managed_material(tmp_path)
    plugin = _make_plugin(
        mock_app,
        managed=True,
        cert_file=cert_file,
        key_file=key_file,
        cert_dir=cert_dir,
    )
    plugin._ssl_ctx.load_cert_chain.side_effect = [OSError("reload failed"), None]
    before = Path(cert_file).read_bytes()

    renewed = asyncio.run(
        plugin._check_tls_certificate(now=ISSUED_AT + datetime.timedelta(days=335))
    )

    assert renewed is False
    assert Path(cert_file).read_bytes() == before
    assert plugin._ssl_ctx.load_cert_chain.call_count == 2
    plugin._site.stop.assert_not_awaited()
    assert plugin._tls_state == "degraded"
    assert plugin.plugin_health is PluginHealth.DEGRADED


def test_invalid_operator_pair_is_unchanged_and_listener_fails_closed(tmp_path, mock_app):
    cert_a, _ = generate_self_signed_cert(
        str(tmp_path / "operator-a"),
        "OperatorA",
        now=ISSUED_AT,
    )
    _, key_b = generate_self_signed_cert(
        str(tmp_path / "operator-b"),
        "OperatorB",
        now=ISSUED_AT,
    )
    plugin = _make_plugin(
        mock_app,
        managed=False,
        cert_file=cert_a,
        key_file=key_b,
        cert_dir=str(tmp_path),
    )
    cert_before = Path(cert_a).read_bytes()
    key_before = Path(key_b).read_bytes()
    site = plugin._site

    renewed = asyncio.run(plugin._check_tls_certificate(now=ISSUED_AT))

    assert renewed is False
    assert Path(cert_a).read_bytes() == cert_before
    assert Path(key_b).read_bytes() == key_before
    site.stop.assert_awaited_once()
    assert plugin._site is None
    assert plugin._tls_state == "failed_closed"
    assert plugin.plugin_health is PluginHealth.DEGRADED


def test_configured_operator_path_is_never_completed_by_generator(mock_app, tmp_path):
    config = {
        "enabled": True,
        "password": "test",
        "ssl": {
            "enabled": True,
            "auto_generate": True,
            "cert_file": str(tmp_path / "operator.crt"),
        },
    }

    with pytest.raises(ValueError, match="configured together"):
        WebDashboardPlugin(mock_app, config)
    assert not (tmp_path / "dashboard.pem").exists()


def test_managed_generator_reuses_valid_bundle_and_logs_decision(tmp_path):
    cert_dir = str(tmp_path / "managed")
    logger = MagicMock()
    first = generate_self_signed_cert(
        cert_dir,
        "TestNode",
        logger,
        extra_sans=["pi.test"],
        now=ISSUED_AT,
    )
    before = Path(first[0]).read_bytes()

    second = generate_self_signed_cert(
        cert_dir,
        "TestNode",
        logger,
        extra_sans=["pi.test"],
        now=ISSUED_AT + datetime.timedelta(days=1),
    )

    assert second == first
    assert Path(second[0]).read_bytes() == before
    assert any("Reusing existing" in call.args[0] for call in logger.info.call_args_list)


def test_valid_legacy_pair_is_migrated_to_one_atomic_bundle(tmp_path):
    source_dir = str(tmp_path / "source")
    source_bundle, _ = generate_self_signed_cert(
        source_dir,
        "TestNode",
        extra_sans=["pi.test"],
        now=ISSUED_AT,
    )
    material = Path(source_bundle).read_bytes()
    key_marker = b"-----BEGIN RSA PRIVATE KEY-----"
    key_offset = material.index(key_marker)

    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    (legacy_dir / "dashboard.crt").write_bytes(material[:key_offset])
    (legacy_dir / "dashboard.key").write_bytes(material[key_offset:])
    (legacy_dir / "dashboard.key").chmod(0o600)
    logger = MagicMock()

    bundle, key = generate_self_signed_cert(
        str(legacy_dir),
        "TestNode",
        logger,
        extra_sans=["pi.test"],
        now=ISSUED_AT + datetime.timedelta(days=1),
    )

    assert bundle == key == str(legacy_dir / "dashboard.pem")
    assert Path(bundle).read_bytes() == material
    assert any("Migrated managed TLS" in call.args[0] for call in logger.info.call_args_list)


def test_tls_validation_reports_unreadable_material_and_missing_sans(tmp_path):
    with pytest.raises(ValueError, match="cannot be loaded"):
        validate_cert_pair(str(tmp_path / "missing.crt"), str(tmp_path / "missing.key"))

    bundle, _ = generate_self_signed_cert(
        str(tmp_path / "managed"),
        "TestNode",
        now=ISSUED_AT,
    )
    with pytest.raises(ValueError, match="missing SANs"):
        validate_cert_pair(
            bundle,
            bundle,
            required_sans=["not-issued.example"],
            now=ISSUED_AT,
        )


def test_tls_validation_rejects_future_cert_insecure_key_and_symlink(tmp_path):
    future = ISSUED_AT + datetime.timedelta(days=2)
    bundle, _ = generate_self_signed_cert(
        str(tmp_path / "future"),
        "FutureNode",
        extra_sans=["future.test"],
        now=future,
    )

    with pytest.raises(ValueError, match="not yet valid"):
        validate_cert_pair(bundle, bundle, required_sans=["future.test"], now=ISSUED_AT)

    Path(bundle).chmod(0o644)
    with pytest.raises(ValueError, match="private key permissions"):
        validate_cert_pair(bundle, bundle, required_sans=["future.test"], now=future)

    Path(bundle).chmod(0o600)
    linked = tmp_path / "linked.pem"
    linked.symlink_to(bundle)
    with pytest.raises(ValueError, match="certificate cannot be loaded"):
        validate_cert_pair(str(linked), str(linked), now=future)


def test_operator_tls_requires_explicit_sans_and_checks_before_validity_guard(mock_app):
    with pytest.raises(ValueError, match="at least one required SAN"):
        WebDashboardPlugin(
            mock_app,
            {
                "enabled": True,
                "password": "test",
                "ssl": {
                    "enabled": True,
                    "cert_file": "/etc/ssl/certs/operator.pem",
                    "key_file": "/etc/ssl/private/operator.key",
                },
            },
        )

    plugin = WebDashboardPlugin(mock_app, {"enabled": True, "password": "test"})
    plugin._tls_managed = False
    plugin._tls_expires_at = ISSUED_AT + datetime.timedelta(hours=25)
    assert plugin._tls_check_delay(now=ISSUED_AT) == 60 * 60

    plugin._tls_managed = True
    plugin._tls_expires_at = ISSUED_AT + datetime.timedelta(days=30, minutes=10)
    assert plugin._tls_check_delay(now=ISSUED_AT) == 10 * 60


def test_operator_tls_fails_closed_at_one_day_validity_guard(tmp_path, mock_app):
    cert_dir, cert_file, key_file = _managed_material(tmp_path)
    plugin = _make_plugin(
        mock_app,
        managed=False,
        cert_file=cert_file,
        key_file=key_file,
        cert_dir=cert_dir,
    )
    plugin._tls_required_sans = ["pi.test"]

    renewed = asyncio.run(
        plugin._check_tls_certificate(now=ISSUED_AT + datetime.timedelta(days=364))
    )

    assert renewed is False
    assert plugin._tls_state == "failed_closed"
    assert plugin._site is None


def test_naive_tls_clock_is_normalized_to_utc():
    naive = datetime.datetime(2026, 1, 2, 3, 4, 5)
    normalized = _normalise_now(naive)
    assert normalized == naive.replace(tzinfo=UTC)


def test_atomic_tls_write_removes_partial_file_when_write_stalls(tmp_path, monkeypatch):
    destination = tmp_path / "dashboard.pem"
    monkeypatch.setattr(os, "write", lambda _fd, _data: 0)

    with pytest.raises(OSError, match="short write"):
        _atomic_write(str(destination), b"material", 0o600)

    assert not destination.exists()
    assert list(tmp_path.glob(".tls-*")) == []
