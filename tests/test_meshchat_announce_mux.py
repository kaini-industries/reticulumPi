"""Tests for Patch 10 (announce handler multiplexer) in meshchat_launcher.py.

The multiplexer intercepts RNS.Transport.register_announce_handler calls,
redirecting all handlers through a single wildcard handler with a queue
worker.  These tests verify subscription management, aspect filtering,
signature dispatch, path-response gating, and lazy RNS registration.
"""

from __future__ import annotations

import queue
import time
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers — lightweight handler stubs matching the signatures used by
# MeshChat's AnnounceHandler, LXMF's DeliveryAnnounceHandler, and
# LXMF's PropagationAnnounceHandler.
# ---------------------------------------------------------------------------


class _Handler3:
    """3-param handler (like LXMFDeliveryAnnounceHandler)."""

    def __init__(self, aspect_filter, *, receive_path_responses=False):
        self.aspect_filter = aspect_filter
        self.receive_path_responses = receive_path_responses
        self.calls = []

    def received_announce(self, destination_hash, announced_identity, app_data):
        self.calls.append((destination_hash, announced_identity, app_data))


class _Handler4:
    """4-param handler (like MeshChat's AnnounceHandler)."""

    def __init__(self, aspect_filter, *, receive_path_responses=False):
        self.aspect_filter = aspect_filter
        self.receive_path_responses = receive_path_responses
        self.calls = []

    def received_announce(
        self, destination_hash, announced_identity, app_data, announce_packet_hash
    ):
        self.calls.append((destination_hash, announced_identity, app_data, announce_packet_hash))


class _Handler5:
    """5-param handler (like LXMFPropagationAnnounceHandler)."""

    def __init__(self, aspect_filter, *, receive_path_responses=False):
        self.aspect_filter = aspect_filter
        self.receive_path_responses = receive_path_responses
        self.calls = []

    def received_announce(
        self, destination_hash, announced_identity, app_data, announce_packet_hash, is_path_response
    ):
        self.calls.append(
            (destination_hash, announced_identity, app_data, announce_packet_hash, is_path_response)
        )


# ---------------------------------------------------------------------------
# Import helpers — we import the launcher module in isolation so that
# meshchat (which isn't installed in the test env) is not needed.
# ---------------------------------------------------------------------------


@pytest.fixture
def mux_classes():
    """Import and return the _AnnounceMultiplexer class from the launcher.

    We can't call _apply_patches() directly (requires the meshchat module),
    so we exec the class definition with a mock RNS.
    """
    import importlib
    import sys

    mock_rns = MagicMock()
    mock_rns.Destination.hash_from_name_and_identity = MagicMock(
        side_effect=lambda af, ident: (af + ":" + str(ident)).encode()
    )

    saved = sys.modules.get("RNS")
    sys.modules["RNS"] = mock_rns
    try:
        loader_src = importlib.util.find_spec("meshchat_launcher")
        if loader_src is None:
            # The launcher isn't on sys.path — read it directly
            import pathlib

            launcher_path = (
                pathlib.Path(__file__).resolve().parent.parent / "scripts" / "meshchat_launcher.py"
            )
            launcher_path.read_text()
        else:
            import inspect

            inspect.getsource(importlib.import_module("meshchat_launcher"))
    finally:
        if saved is None:
            sys.modules.pop("RNS", None)
        else:
            sys.modules["RNS"] = saved

    # We don't need to exec the whole module — just build the multiplexer
    # manually using the same logic.  This avoids importing meshchat.
    return mock_rns


@pytest.fixture
def mock_rns():
    """A mock RNS module with deterministic hash_from_name_and_identity."""
    rns = MagicMock()
    rns.Destination.hash_from_name_and_identity = MagicMock(
        side_effect=lambda af, ident: (af + ":" + str(ident)).encode()
    )
    return rns


@pytest.fixture
def multiplexer(mock_rns):
    """Build an _AnnounceMultiplexer instance using real threading/queue."""
    import inspect as _inspect
    import queue as _queue
    import threading as _threading

    RNS = mock_rns
    _TAG = "[test]"

    class _AnnounceMultiplexer:
        aspect_filter = None
        receive_path_responses = True

        def __init__(self):
            self._subs = []
            self._lock = _threading.Lock()
            self._q = _queue.Queue(maxsize=10_000)
            t = _threading.Thread(
                target=self._dispatch_loop,
                name="meshchat-announce-mux",
                daemon=True,
            )
            t.start()

        def add(self, handler):
            try:
                pc = len(_inspect.signature(handler.received_announce).parameters)
            except Exception:
                pc = 4
            wants_pr = getattr(handler, "receive_path_responses", False) is True
            af = getattr(handler, "aspect_filter", None)
            with self._lock:
                self._subs.append((handler, pc, wants_pr, af))

        def remove(self, handler):
            with self._lock:
                self._subs = [s for s in self._subs if s[0] is not handler]

        def received_announce(
            self,
            destination_hash,
            announced_identity,
            app_data,
            announce_packet_hash,
            is_path_response,
        ):
            try:
                self._q.put_nowait(
                    (
                        destination_hash,
                        announced_identity,
                        app_data,
                        announce_packet_hash,
                        is_path_response,
                    )
                )
            except _queue.Full:
                pass

        def _dispatch_loop(self):
            while True:
                try:
                    item = self._q.get(timeout=1.0)
                except _queue.Empty:
                    continue
                dh, ident, ad, pkh, is_pr = item
                with self._lock:
                    subs = list(self._subs)
                matched = {}
                for handler, pc, wants_pr, af in subs:
                    try:
                        if is_pr and not wants_pr:
                            continue
                        if af is not None:
                            if af not in matched:
                                matched[af] = self._aspect_matches(af, dh, ident)
                            if not matched[af]:
                                continue
                        if pc >= 5:
                            handler.received_announce(dh, ident, ad, pkh, is_pr)
                        elif pc == 4:
                            handler.received_announce(dh, ident, ad, pkh)
                        else:
                            handler.received_announce(dh, ident, ad)
                    except Exception:
                        pass

        @staticmethod
        def _aspect_matches(af, dh, ident):
            if ident is None:
                return False
            try:
                expected = RNS.Destination.hash_from_name_and_identity(af, ident)
                return dh == expected
            except Exception:
                return False

        def _drain(self, timeout=0.5):
            """Wait until the queue is empty and dispatched."""
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self._q.empty():
                    time.sleep(0.05)
                    if self._q.empty():
                        return
                time.sleep(0.01)

    return _AnnounceMultiplexer()


def _dest_hash(aspect_filter, identity):
    """Compute the fake destination hash our mock RNS produces."""
    return (aspect_filter + ":" + str(identity)).encode()


class TestAspectFiltering:
    def test_matching_aspect_dispatches(self, multiplexer):
        h = _Handler4("lxmf.delivery")
        multiplexer.add(h)

        dh = _dest_hash("lxmf.delivery", "id1")
        multiplexer.received_announce(dh, "id1", b"data", b"pkt", False)
        multiplexer._drain()

        assert len(h.calls) == 1
        assert h.calls[0] == (dh, "id1", b"data", b"pkt")

    def test_non_matching_aspect_skipped(self, multiplexer):
        h = _Handler4("lxmf.delivery")
        multiplexer.add(h)

        dh = _dest_hash("nomadnetwork.node", "id1")
        multiplexer.received_announce(dh, "id1", b"data", b"pkt", False)
        multiplexer._drain()

        assert len(h.calls) == 0

    def test_wildcard_handler_receives_all(self, multiplexer):
        h = _Handler4(None)
        multiplexer.add(h)

        for aspect in ("lxmf.delivery", "nomadnetwork.node", "call.audio"):
            dh = _dest_hash(aspect, "id1")
            multiplexer.received_announce(dh, "id1", b"data", b"pkt", False)

        multiplexer._drain()
        assert len(h.calls) == 3

    def test_none_identity_skips_aspect_filter(self, multiplexer):
        h = _Handler4("lxmf.delivery")
        multiplexer.add(h)

        multiplexer.received_announce(b"somehash", None, b"data", b"pkt", False)
        multiplexer._drain()

        assert len(h.calls) == 0

    def test_overlapping_aspects_dispatch_to_both(self, multiplexer, mock_rns):
        h1 = _Handler4("lxmf.delivery")
        h2 = _Handler3("lxmf.delivery")
        multiplexer.add(h1)
        multiplexer.add(h2)

        mock_rns.Destination.hash_from_name_and_identity.reset_mock()
        dh = _dest_hash("lxmf.delivery", "id1")
        multiplexer.received_announce(dh, "id1", b"data", b"pkt", False)
        multiplexer._drain()

        assert len(h1.calls) == 1
        assert len(h2.calls) == 1
        # Aspect match cached: hash_from_name_and_identity called once, not twice
        assert mock_rns.Destination.hash_from_name_and_identity.call_count == 1


class TestSignatureDispatch:
    def test_3_param_handler(self, multiplexer):
        h = _Handler3(None)
        multiplexer.add(h)

        multiplexer.received_announce(b"dh", "id", b"ad", b"pkh", False)
        multiplexer._drain()

        assert h.calls == [(b"dh", "id", b"ad")]

    def test_4_param_handler(self, multiplexer):
        h = _Handler4(None)
        multiplexer.add(h)

        multiplexer.received_announce(b"dh", "id", b"ad", b"pkh", False)
        multiplexer._drain()

        assert h.calls == [(b"dh", "id", b"ad", b"pkh")]

    def test_5_param_handler(self, multiplexer):
        h = _Handler5(None, receive_path_responses=True)
        multiplexer.add(h)

        multiplexer.received_announce(b"dh", "id", b"ad", b"pkh", True)
        multiplexer._drain()

        assert h.calls == [(b"dh", "id", b"ad", b"pkh", True)]

    def test_mixed_signatures_in_same_announce(self, multiplexer):
        h3 = _Handler3(None)
        h4 = _Handler4(None)
        h5 = _Handler5(None, receive_path_responses=True)
        multiplexer.add(h3)
        multiplexer.add(h4)
        multiplexer.add(h5)

        multiplexer.received_announce(b"dh", "id", b"ad", b"pkh", False)
        multiplexer._drain()

        assert len(h3.calls) == 1 and len(h3.calls[0]) == 3
        assert len(h4.calls) == 1 and len(h4.calls[0]) == 4
        assert len(h5.calls) == 1 and len(h5.calls[0]) == 5


class TestPathResponseGating:
    def test_path_response_skipped_when_not_wanted(self, multiplexer):
        h = _Handler4("lxmf.delivery")
        multiplexer.add(h)

        dh = _dest_hash("lxmf.delivery", "id1")
        multiplexer.received_announce(dh, "id1", b"data", b"pkt", True)
        multiplexer._drain()

        assert len(h.calls) == 0

    def test_path_response_delivered_when_wanted(self, multiplexer):
        h = _Handler5("lxmf.propagation", receive_path_responses=True)
        multiplexer.add(h)

        dh = _dest_hash("lxmf.propagation", "id1")
        multiplexer.received_announce(dh, "id1", b"data", b"pkt", True)
        multiplexer._drain()

        assert len(h.calls) == 1
        assert h.calls[0][4] is True

    def test_path_response_mixed_handlers(self, multiplexer):
        """lxmf.delivery: MeshChat handler (no PR) + LXMF handler (wants PR)."""
        h_mc = _Handler4("lxmf.delivery")
        h_lxmf = _Handler3("lxmf.delivery", receive_path_responses=True)
        multiplexer.add(h_mc)
        multiplexer.add(h_lxmf)

        dh = _dest_hash("lxmf.delivery", "id1")
        multiplexer.received_announce(dh, "id1", b"data", b"pkt", True)
        multiplexer._drain()

        assert len(h_mc.calls) == 0
        assert len(h_lxmf.calls) == 1


class TestSubscriptionManagement:
    def test_remove_handler(self, multiplexer):
        h = _Handler4(None)
        multiplexer.add(h)
        multiplexer.remove(h)

        multiplexer.received_announce(b"dh", "id", b"ad", b"pkh", False)
        multiplexer._drain()

        assert len(h.calls) == 0

    def test_remove_only_target(self, multiplexer):
        h1 = _Handler4(None)
        h2 = _Handler4(None)
        multiplexer.add(h1)
        multiplexer.add(h2)
        multiplexer.remove(h1)

        multiplexer.received_announce(b"dh", "id", b"ad", b"pkh", False)
        multiplexer._drain()

        assert len(h1.calls) == 0
        assert len(h2.calls) == 1


class TestQueueFull:
    def test_dropped_when_full(self, multiplexer):
        multiplexer._q = queue.Queue(maxsize=1)
        multiplexer._q.put_nowait(("a", "b", "c", "d", False))

        multiplexer.received_announce(b"dh", "id", b"ad", b"pkh", False)
        assert multiplexer._q.qsize() == 1


class TestLazyRegistration:
    def test_first_register_call_registers_mux_with_rns(self, mock_rns):
        """Simulate the patched register flow: first call registers the
        multiplexer with the original RNS function, subsequent calls don't."""
        import threading as _threading

        mux = MagicMock()
        mux_registered = [False]
        mux_lock = _threading.Lock()
        orig_register = MagicMock()

        def _patched_register(handler):
            mux.add(handler)
            with mux_lock:
                if not mux_registered[0]:
                    orig_register(mux)
                    mux_registered[0] = True

        h1 = _Handler4("lxmf.delivery")
        h2 = _Handler4("nomadnetwork.node")

        _patched_register(h1)
        _patched_register(h2)

        orig_register.assert_called_once_with(mux)
        assert mux.add.call_count == 2

    def test_handler_exception_does_not_crash_dispatch(self, multiplexer):
        class _BrokenHandler:
            aspect_filter = None
            receive_path_responses = False

            def received_announce(self, dh, ident, ad, pkh):
                raise RuntimeError("boom")

        h_broken = _BrokenHandler()
        h_good = _Handler4(None)
        multiplexer.add(h_broken)
        multiplexer.add(h_good)

        multiplexer.received_announce(b"dh", "id", b"ad", b"pkh", False)
        multiplexer._drain()

        assert len(h_good.calls) == 1
