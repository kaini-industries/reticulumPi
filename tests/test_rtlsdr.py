"""Tests for the shared RTL-SDR device resolver."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import reticulumpi.rtlsdr as rtlsdr
from reticulumpi.rtlsdr import (
    DeviceBusyError,
    DeviceLease,
    _run_rtl_test,
    claim_device,
    configured_device,
    enumerate_devices,
    get_lease_metrics,
    invalidate_cache,
    refresh_device_lease,
    release_device,
    reset_cache,
    resolve_device,
)

_RTL_TEST_OUTPUT = """\
Found 3 device(s):
  0:  RTLSDRBlog, Blog V4, SN: 00000001
  1:  Nooelec, SMArTee XTR v5ee, SN: 07143901
  2:  Nooelec, SMArTee XTR v5ee, SN: 14342860

Using device 0: Generic RTL2832U OEM
"""


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_cache()
    yield
    reset_cache()


def _mock_rtl_test(output: str = _RTL_TEST_OUTPUT, which: str = "/usr/bin/rtl_test"):
    """Patch shutil.which and _run_rtl_test to return canned device list."""
    from reticulumpi.rtlsdr import _DEVICE_RE

    devices = []
    for line in output.splitlines():
        m = _DEVICE_RE.match(line)
        if m:
            devices.append((int(m.group(1)), m.group(2)))
    return [
        patch("reticulumpi.rtlsdr.shutil.which", return_value=which),
        patch("reticulumpi.rtlsdr._run_rtl_test", return_value=devices),
    ]


class TestEnumerateDevices:
    def test_inventory_probe_cannot_open_a_real_device(self):
        proc = MagicMock()
        proc.stderr = iter(
            [
                "Found 1 device(s):\n",
                "  0:  RTLSDRBlog, Blog V4, SN: 2147483647\n",
            ]
        )
        with patch("reticulumpi.rtlsdr.subprocess.Popen", return_value=proc) as popen:
            devices = _run_rtl_test("/usr/bin/rtl_test")

        assert devices == [(0, "2147483647")]
        argv = popen.call_args.args[0]
        assert argv[:2] == ["/usr/bin/rtl_test", "-d"]
        selector = argv[2]
        parsed_index = int(selector.split("-", 1)[0])
        assert parsed_index == 2_147_483_647
        assert parsed_index > len(devices)
        assert len(selector) > 256
        assert selector != "2147483647"
        assert not "2147483647".startswith(selector)
        assert not "2147483647".endswith(selector)

    def test_parses_three_devices(self):
        with _mock_rtl_test()[0], _mock_rtl_test()[1]:
            devices = enumerate_devices()
        assert devices == [(0, "00000001"), (1, "07143901"), (2, "14342860")]

    def test_empty_when_rtl_test_missing(self):
        with patch("reticulumpi.rtlsdr.shutil.which", return_value=None):
            assert enumerate_devices() == []

    def test_cache_reuses_result(self):
        patches = _mock_rtl_test()
        with patches[0], patches[1] as mock_run:
            enumerate_devices()
            enumerate_devices()
            assert mock_run.call_count == 1

    def test_empty_when_rtl_test_fails(self):
        with (
            patch("reticulumpi.rtlsdr.shutil.which", return_value="/usr/bin/rtl_test"),
            patch("reticulumpi.rtlsdr._run_rtl_test", side_effect=OSError("fail")),
        ):
            assert enumerate_devices() == []


class TestResolveDevice:
    def test_invalid_selector_is_rejected(self):
        with pytest.raises(ValueError, match="Unknown RTL-SDR device selector"):
            resolve_device("1", selector="invalid")

    def test_serial_match_returns_index(self):
        with _mock_rtl_test()[0], _mock_rtl_test()[1]:
            assert resolve_device("00000001") == 0
            reset_cache()
        with _mock_rtl_test()[0], _mock_rtl_test()[1]:
            assert resolve_device("07143901") == 1
            reset_cache()
        with _mock_rtl_test()[0], _mock_rtl_test()[1]:
            assert resolve_device("14342860") == 2

    def test_numeric_fallback(self):
        with _mock_rtl_test()[0], _mock_rtl_test()[1]:
            assert resolve_device("1") == 1

    def test_three_digit_numeric_index(self):
        with _mock_rtl_test()[0], _mock_rtl_test()[1]:
            assert resolve_device("100") == 100

    def test_eight_digit_unknown_serial_raises(self):
        with _mock_rtl_test()[0], _mock_rtl_test()[1]:
            with pytest.raises(RuntimeError, match="not found"):
                resolve_device("12345678")

    def test_explicit_index_bypasses_matching_eight_digit_serial(self):
        with _mock_rtl_test()[0], _mock_rtl_test()[1]:
            assert resolve_device("00000001", selector="index") == 1

    def test_explicit_serial_never_falls_back_to_numeric_index(self):
        with _mock_rtl_test()[0], _mock_rtl_test()[1]:
            with pytest.raises(RuntimeError, match="serial '1' not found"):
                resolve_device("1", selector="serial")

    def test_explicit_serial_remains_strict_without_enumerated_devices(self):
        with patch("reticulumpi.rtlsdr.shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="serial '00000001' not found"):
                resolve_device("00000001", selector="serial")

    def test_eight_digit_fallback_no_devices(self):
        with patch("reticulumpi.rtlsdr.shutil.which", return_value=None):
            assert resolve_device("12345678") == 12345678

    def test_negative_index_raises(self):
        with _mock_rtl_test()[0], _mock_rtl_test()[1]:
            with pytest.raises(RuntimeError, match="not found"):
                resolve_device("-1")

    def test_numeric_fallback_no_devices(self):
        with patch("reticulumpi.rtlsdr.shutil.which", return_value=None):
            assert resolve_device("0") == 0

    def test_unknown_serial_raises(self):
        with _mock_rtl_test()[0], _mock_rtl_test()[1]:
            with pytest.raises(RuntimeError, match="not found"):
                resolve_device("NOSUCH")

    def test_non_numeric_non_serial_raises_no_devices(self):
        with patch("reticulumpi.rtlsdr.shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="not found"):
                resolve_device("abc")

    def test_duplicate_serial_raises(self):
        patches = _mock_rtl_test()
        with patches[0], patches[1]:
            resolve_device("00000001", caller="adsb_radar")
            with pytest.raises(RuntimeError, match="already claimed by 'adsb_radar'"):
                resolve_device("00000001", caller="spectrum_scanner")

    def test_numeric_index_and_serial_share_one_claim(self):
        patches = _mock_rtl_test()
        with patches[0], patches[1]:
            resolve_device("1", caller="ais_receiver")
            with pytest.raises(RuntimeError, match="already claimed"):
                resolve_device("07143901", caller="spectrum_scanner")

    def test_zero_padded_explicit_index_claims_the_indexed_device(self):
        patches = _mock_rtl_test()
        with patches[0], patches[1]:
            lease = claim_device(
                "00000001",
                caller="spectrum_scanner",
                selector="index",
            )
            assert lease.index == 1
            assert lease.canonical_id == "serial:07143901"
            with pytest.raises(RuntimeError, match="already claimed"):
                resolve_device("07143901", caller="ais_receiver", selector="serial")
            lease.release()

    def test_busy_claim_exposes_authoritative_immutable_resolution(self):
        patches = _mock_rtl_test()
        with patches[0], patches[1]:
            lease = claim_device("07143901", caller="ais_receiver", selector="serial")
            with pytest.raises(DeviceBusyError) as exc_info:
                claim_device("1", caller="weather_alert", selector="index")

        assert exc_info.value.index == 1
        assert exc_info.value.canonical_id == "serial:07143901"
        assert exc_info.value.resolved.index == 1
        assert exc_info.value.resolved.canonical_id == "serial:07143901"
        with pytest.raises((AttributeError, TypeError)):
            exc_info.value.resolved.canonical_id = "serial:changed"
        lease.release()

    def test_auto_zero_padded_index_release_uses_recorded_canonical_claim(self):
        patches = _mock_rtl_test()
        with patches[0], patches[1], patch("reticulumpi.rtlsdr.log.info") as info:
            assert resolve_device("01", caller="ais_receiver") == 1
            assert get_lease_metrics() == {"canonical_claims": 1}
        info.assert_not_called()

        with patch(
            "reticulumpi.rtlsdr.enumerate_devices",
            side_effect=AssertionError("release must use the recorded canonical claim"),
        ):
            release_device("01", caller="ais_receiver")
        assert get_lease_metrics() == {"canonical_claims": 0}

    def test_claim_lease_uses_one_enumeration_snapshot(self):
        first = [(1, "FIRST")]
        changed = [(1, "CHANGED")]
        with patch(
            "reticulumpi.rtlsdr.enumerate_devices",
            side_effect=[first, changed],
        ) as enumerate_mock:
            lease = claim_device("1", caller="ais_receiver", selector="index")

        assert enumerate_mock.call_count == 1
        assert lease.index == 1
        assert lease.canonical_id == "serial:FIRST"
        lease.release()

    def test_claim_bypasses_a_populated_enumeration_cache(self):
        old = "Found 1 device(s):\n  1: Test, Old, SN: OLD\n"
        patches = _mock_rtl_test(output=old)
        with patches[0], patches[1]:
            assert enumerate_devices() == [(1, "OLD")]

        new = "Found 1 device(s):\n  1: Test, New, SN: NEW\n"
        patches = _mock_rtl_test(output=new)
        with patches[0], patches[1] as run:
            lease = claim_device("1", caller="ais_receiver", selector="index")

        run.assert_called_once_with("/usr/bin/rtl_test")
        assert lease.index == 1
        assert lease.canonical_id == "serial:NEW"
        lease.release()

    def test_serial_claim_refreshes_a_stale_cached_index(self):
        old = "Found 1 device(s):\n  1: Test, Target, SN: TARGET\n"
        patches = _mock_rtl_test(output=old)
        with patches[0], patches[1]:
            assert enumerate_devices() == [(1, "TARGET")]

        moved = "Found 1 device(s):\n  0: Test, Target, SN: TARGET\n"
        patches = _mock_rtl_test(output=moved)
        with patches[0], patches[1] as run:
            lease = claim_device("TARGET", caller="ais_receiver", selector="serial")

        run.assert_called_once_with("/usr/bin/rtl_test")
        assert lease.index == 0
        assert lease.canonical_id == "serial:TARGET"
        lease.release()

    def test_legacy_caller_claim_also_forces_a_fresh_snapshot(self):
        with patch(
            "reticulumpi.rtlsdr.enumerate_devices",
            return_value=[(1, "CURRENT")],
        ) as enumerate_mock:
            assert resolve_device("1", caller="legacy", selector="index") == 1

        enumerate_mock.assert_called_once_with(force_refresh=True)
        release_device("1", caller="legacy", selector="index")

    def test_refresh_lease_uses_one_enumeration_snapshot(self):
        old = DeviceLease("1", "serial:OLD", 1, "ais_receiver")
        first = [(1, "FIRST")]
        changed = [(1, "CHANGED")]
        with patch(
            "reticulumpi.rtlsdr.enumerate_devices",
            side_effect=[first, changed],
        ) as enumerate_mock:
            refreshed = refresh_device_lease(
                old,
                "1",
                "ais_receiver",
                selector="index",
            )

        assert enumerate_mock.call_count == 1
        assert refreshed.index == 1
        assert refreshed.canonical_id == "serial:FIRST"
        refreshed.release()

    def test_device_lease_releases_canonical_claim(self):
        patches = _mock_rtl_test()
        with patches[0], patches[1]:
            lease = claim_device("1", caller="ais_receiver")
            assert lease.index == 1
            assert lease.canonical_id == "serial:07143901"
            lease.release()
            assert resolve_device("07143901", caller="spectrum_scanner") == 1

    def test_lease_metrics_count_claims_without_identifiers(self):
        patches = _mock_rtl_test()
        with patches[0], patches[1]:
            lease = claim_device("1", caller="ais_receiver")

        assert get_lease_metrics() == {"canonical_claims": 1}
        lease.release()
        assert get_lease_metrics() == {"canonical_claims": 0}

    def test_device_lease_releases_after_enumeration_disappears(self):
        patches = _mock_rtl_test()
        with patches[0], patches[1]:
            lease = claim_device("1", caller="ais_receiver")
        invalidate_cache()
        with patch("reticulumpi.rtlsdr.shutil.which", return_value=None):
            lease.release()
        reset_cache()
        patches = _mock_rtl_test()
        with patches[0], patches[1]:
            assert resolve_device("07143901", caller="spectrum_scanner") == 1

    def test_refresh_moves_claim_when_warm_cache_maps_index_to_new_serial(self):
        patches = _mock_rtl_test()
        with patches[0], patches[1]:
            lease = claim_device("1", caller="ais_receiver")

        replacement = "Found 2 device(s):\n  1: Test, Replacement, SN: 99999999\n"
        patches = _mock_rtl_test(output=replacement)
        with patches[0], patches[1]:
            refreshed = refresh_device_lease(lease, "1", "ais_receiver")
            assert refreshed.canonical_id == "serial:99999999"
            refreshed.release()

        reset_cache()
        patches = _mock_rtl_test()
        with patches[0], patches[1]:
            assert resolve_device("07143901", caller="spectrum_scanner") == 1

    def test_unresolved_then_known_same_index_conflicts_across_callers(self):
        known = [(1, "KNOWN")]
        with patch("reticulumpi.rtlsdr.enumerate_devices", side_effect=[[], known]):
            first = claim_device("1", caller="first", selector="index")
            with pytest.raises(DeviceBusyError) as exc_info:
                claim_device("KNOWN", caller="second", selector="serial")

        assert first.canonical_id == "index:1"
        assert exc_info.value.index == 1
        assert exc_info.value.canonical_id == "serial:KNOWN"
        first.release()

    def test_known_then_unresolved_same_index_conflicts_across_callers(self):
        known = [(1, "KNOWN")]
        with patch("reticulumpi.rtlsdr.enumerate_devices", side_effect=[known, []]):
            first = claim_device("KNOWN", caller="first", selector="serial")
            with pytest.raises(DeviceBusyError) as exc_info:
                claim_device("1", caller="second", selector="index")

        assert exc_info.value.index == 1
        assert exc_info.value.canonical_id == "index:1"
        first.release()

    def test_same_index_serial_reenumeration_conflicts_until_release(self):
        with patch(
            "reticulumpi.rtlsdr.enumerate_devices",
            side_effect=[[(1, "OLD")], [(1, "NEW")]],
        ):
            old = claim_device("OLD", caller="first", selector="serial")
            with pytest.raises(DeviceBusyError) as exc_info:
                claim_device("NEW", caller="second", selector="serial")

        assert exc_info.value.index == 1
        assert exc_info.value.canonical_id == "serial:NEW"
        old.release()

    def test_different_selection_same_caller_cannot_steal_claim(self):
        known = [(1, "KNOWN")]
        with patch("reticulumpi.rtlsdr.enumerate_devices", side_effect=[known, known]):
            lease = claim_device("1", caller="shared", selector="index")
            with pytest.raises(DeviceBusyError) as exc_info:
                claim_device("KNOWN", caller="shared", selector="serial")

        assert exc_info.value.canonical_id == "serial:KNOWN"
        lease.release()

    def test_same_selection_promotes_and_stale_lease_cannot_release_it(self):
        known = [(1, "KNOWN")]
        with patch(
            "reticulumpi.rtlsdr.enumerate_devices",
            side_effect=[[], known, known],
        ):
            provisional = claim_device("1", caller="owner", selector="index")
            promoted = refresh_device_lease(
                provisional,
                "1",
                "owner",
                selector="index",
            )
            provisional.release()
            with pytest.raises(DeviceBusyError):
                claim_device("KNOWN", caller="other", selector="serial")

        assert provisional.canonical_id == "index:1"
        assert promoted.canonical_id == "serial:KNOWN"
        assert get_lease_metrics() == {"canonical_claims": 1}
        promoted.release()

    def test_stale_lease_cannot_release_same_identity_refresh_aba(self):
        known = [(1, "KNOWN")]
        with patch(
            "reticulumpi.rtlsdr.enumerate_devices",
            side_effect=[known, known, known, known],
        ):
            old = claim_device("1", caller="owner", selector="index")
            current = refresh_device_lease(old, "1", "owner", selector="index")
            old.release()
            with pytest.raises(DeviceBusyError):
                claim_device("KNOWN", caller="other", selector="serial")
            current.release()
            replacement = claim_device("KNOWN", caller="other", selector="serial")

        replacement.release()

    def test_refresh_to_noncolliding_selection_releases_old_exact_claim(self):
        with patch(
            "reticulumpi.rtlsdr.enumerate_devices",
            side_effect=[[(0, "OLD")], [(1, "NEW")]],
        ):
            old = claim_device("OLD", caller="owner", selector="serial")
            new = refresh_device_lease(
                old,
                "NEW",
                "owner",
                selector="serial",
            )

        assert new.canonical_id == "serial:NEW"
        assert new.index == 1
        assert get_lease_metrics() == {"canonical_claims": 1}
        old.release()
        assert get_lease_metrics() == {"canonical_claims": 1}
        new.release()
        assert get_lease_metrics() == {"canonical_claims": 0}

    def test_refresh_token_authorizes_selector_change_on_same_device(self):
        known = [(1, "KNOWN")]
        with patch("reticulumpi.rtlsdr.enumerate_devices", side_effect=[known, known]):
            old = claim_device("1", caller="owner", selector="index")
            new = refresh_device_lease(
                old,
                "KNOWN",
                "owner",
                selector="serial",
            )

        assert new.canonical_id == "serial:KNOWN"
        assert new.index == 1
        assert get_lease_metrics() == {"canonical_claims": 1}
        old.release()
        assert get_lease_metrics() == {"canonical_claims": 1}
        new.release()
        assert get_lease_metrics() == {"canonical_claims": 0}

    def test_refresh_rejects_a_stale_replacement_token(self):
        known = [(1, "KNOWN")]
        with patch(
            "reticulumpi.rtlsdr.enumerate_devices",
            side_effect=[known, known, [(2, "NEW")]],
        ):
            stale = claim_device("1", caller="owner", selector="index")
            current = refresh_device_lease(stale, "1", "owner", selector="index")
            with pytest.raises(RuntimeError, match="stale RTL-SDR device lease"):
                refresh_device_lease(
                    stale,
                    "NEW",
                    "owner",
                    selector="serial",
                )

        assert get_lease_metrics() == {"canonical_claims": 1}
        current.release()

    def test_claim_repairs_a_stale_selection_registry_entry(self):
        selection = ("owner", "index", "1")
        with rtlsdr._claim_lock:
            rtlsdr._claim_selections[selection] = 123_456

        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=[(1, "KNOWN")]):
            lease = claim_device("1", caller="owner", selector="index")

        assert lease.canonical_id == "serial:KNOWN"
        with rtlsdr._claim_lock:
            assert rtlsdr._claim_selections[selection] == lease._claim_id
        lease.release()

    def test_refresh_rejects_a_lease_token_owned_by_another_caller(self):
        known = [(1, "KNOWN")]
        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=known):
            owner = claim_device("1", caller="owner", selector="index")
            forged = DeviceLease(
                owner.configured,
                owner.canonical_id,
                owner.index,
                "intruder",
                owner.selector,
                owner._claim_id,
            )
            with pytest.raises(RuntimeError, match="owned by another caller"):
                refresh_device_lease(
                    forged,
                    "1",
                    "intruder",
                    selector="index",
                )

        assert get_lease_metrics() == {"canonical_claims": 1}
        owner.release()

    def test_refresh_rejects_an_already_claimed_target_selection(self):
        known = [(0, "FIRST"), (1, "SECOND")]
        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=known):
            first = claim_device("0", caller="owner", selector="index")
            second = claim_device("1", caller="owner", selector="index")
            with pytest.raises(RuntimeError, match="different active lease"):
                refresh_device_lease(
                    first,
                    "1",
                    "owner",
                    selector="index",
                )

        assert get_lease_metrics() == {"canonical_claims": 2}
        first.release()
        second.release()

    def test_callerless_release_uses_recorded_auto_selection(self):
        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=[]):
            stale = claim_device("12345678", caller="owner", selector="auto")
        assert stale.canonical_id == "index:12345678"

        with patch(
            "reticulumpi.rtlsdr.enumerate_devices",
            side_effect=AssertionError("recorded force release must not re-enumerate"),
        ):
            release_device("12345678", selector="auto")

        assert get_lease_metrics() == {"canonical_claims": 0}
        stale.release()

    def test_callerless_release_resolves_and_force_releases_a_colliding_selection(self):
        known = [(1, "KNOWN")]
        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=known):
            serial_lease = claim_device("KNOWN", caller="owner", selector="serial")
            release_device("1", selector="index")

        assert get_lease_metrics() == {"canonical_claims": 0}
        serial_lease.release()

    def test_callerless_index_release_falls_back_when_resolution_rejects_index(self):
        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=[]):
            release_device("-1", selector="index")

        assert get_lease_metrics() == {"canonical_claims": 0}

    def test_callerless_invalid_index_release_is_a_noop(self):
        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=[]):
            release_device("not-an-index", selector="index")

        assert get_lease_metrics() == {"canonical_claims": 0}

    def test_callerless_missing_serial_release_is_a_noop(self):
        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=[]):
            release_device("MISSING", selector="serial")

        assert get_lease_metrics() == {"canonical_claims": 0}

    def test_matching_unversioned_lease_releases_the_current_claim(self):
        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=[(1, "KNOWN")]):
            versioned = claim_device("1", caller="owner", selector="index")

        legacy = DeviceLease(
            versioned.configured,
            versioned.canonical_id,
            versioned.index,
            versioned.caller,
            versioned.selector,
        )
        legacy.release()

        assert get_lease_metrics() == {"canonical_claims": 0}
        versioned.release()

    def test_process_cleanup_clears_claims_and_selection_registry(self):
        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=[(1, "KNOWN")]):
            lease = claim_device("1", caller="owner", selector="index")

        rtlsdr._cleanup_claims()

        assert get_lease_metrics() == {"canonical_claims": 0}
        with rtlsdr._claim_lock:
            assert rtlsdr._claim_selections == {}
        lease.release()

    def test_same_caller_no_error(self):
        patches = _mock_rtl_test()
        with patches[0], patches[1]:
            resolve_device("00000001", caller="adsb_radar")
            idx = resolve_device("00000001", caller="adsb_radar")
        assert idx == 0

    def test_release_then_reclaim(self):
        patches = _mock_rtl_test()
        with patches[0], patches[1]:
            resolve_device("00000001", caller="adsb_radar")
            release_device("00000001", caller="adsb_radar")
            idx = resolve_device("00000001", caller="spectrum_scanner")
        assert idx == 0

    def test_release_wrong_caller_is_noop(self):
        patches = _mock_rtl_test()
        with patches[0], patches[1]:
            resolve_device("00000001", caller="adsb_radar")
            release_device("00000001", caller="other_plugin")
            with pytest.raises(RuntimeError, match="already claimed"):
                resolve_device("00000001", caller="spectrum_scanner")


class TestConfiguredDevice:
    def test_serial_takes_precedence_and_remains_explicit(self):
        assert configured_device({"device_serial": "00000001", "device_index": "00000002"}) == (
            "00000001",
            "serial",
        )

    def test_zero_padded_index_remains_an_explicit_index(self):
        assert configured_device({"device_index": "00000001"}) == (
            "00000001",
            "index",
        )

    def test_caller_can_suppress_the_default_index(self):
        assert configured_device({}, default_index="") == ("", "index")


# ---------------------------------------------------------------------------
# Cache TTL
# ---------------------------------------------------------------------------


class TestCacheTTL:
    def test_cache_expires_after_ttl(self):
        import reticulumpi.rtlsdr as mod

        patches = _mock_rtl_test()
        with patches[0], patches[1]:
            devices1 = enumerate_devices()
            assert len(devices1) > 0

        alt_output = "Found 1 device(s):\n  0:  Test, Test, SN: 99999999\n"
        orig_ttl = mod._CACHE_TTL
        try:
            mod._CACHE_TTL = 0.0
            patches2 = _mock_rtl_test(output=alt_output)
            with patches2[0], patches2[1]:
                devices2 = enumerate_devices()
            assert any(s == "99999999" for _, s in devices2)
        finally:
            mod._CACHE_TTL = orig_ttl

    def test_cache_valid_within_ttl(self):
        import reticulumpi.rtlsdr as mod

        patches = _mock_rtl_test()
        with patches[0], patches[1]:
            devices1 = enumerate_devices()

        alt_output = "Found 1 device(s):\n  0:  Test, Test, SN: 99999999\n"
        orig_ttl = mod._CACHE_TTL
        try:
            mod._CACHE_TTL = 9999.0
            patches2 = _mock_rtl_test(output=alt_output)
            with patches2[0], patches2[1]:
                devices2 = enumerate_devices()
            assert devices1 == devices2
        finally:
            mod._CACHE_TTL = orig_ttl

    def test_invalidate_resets_cache_time(self):
        import reticulumpi.rtlsdr as mod

        patches = _mock_rtl_test()
        with patches[0], patches[1]:
            enumerate_devices()
        assert mod._cache_time > 0

        invalidate_cache()
        assert mod._cache is None
        assert mod._cache_time == 0.0
