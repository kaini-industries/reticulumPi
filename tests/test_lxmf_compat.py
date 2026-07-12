"""Regression tests for LXMF construction on daemon lifecycle workers."""

from __future__ import annotations

import signal
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from reticulumpi import lxmf_compat


def test_signal_proxy_delegates_registration_on_main_thread():
    signal_module = MagicMock()
    signal_module.signal.return_value = "previous"
    proxy = lxmf_compat._ThreadAwareSignalProxy(signal_module)
    handler = MagicMock()

    assert proxy.signal(signal.SIGTERM, handler) == "previous"
    signal_module.signal.assert_called_once_with(signal.SIGTERM, handler)
    signal_module.getsignal.assert_not_called()
    signal_module.SIGTERM = signal.SIGTERM
    assert proxy.SIGTERM == signal.SIGTERM


def test_signal_proxy_preserves_application_handler_on_worker_thread():
    signal_module = MagicMock()
    signal_module.getsignal.return_value = "application-handler"
    proxy = lxmf_compat._ThreadAwareSignalProxy(signal_module)
    result = []

    worker = threading.Thread(
        target=lambda: result.append(proxy.signal(signal.SIGTERM, MagicMock())),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=1)

    assert result == ["application-handler"]
    signal_module.signal.assert_not_called()
    signal_module.getsignal.assert_called_once_with(signal.SIGTERM)


def test_install_is_idempotent_and_scoped_to_lxmf_router_module():
    signal_module = MagicMock()
    router_module = SimpleNamespace(signal=signal_module)
    with patch.object(lxmf_compat.importlib, "import_module", return_value=router_module) as load:
        lxmf_compat._install_thread_aware_signals()
        installed = router_module.signal
        lxmf_compat._install_thread_aware_signals()

    assert isinstance(installed, lxmf_compat._ThreadAwareSignalProxy)
    assert router_module.signal is installed
    assert load.call_args_list[0].args == ("LXMF.LXMRouter",)


def test_create_router_is_safe_from_worker_and_forwards_arguments():
    signal_module = MagicMock()
    signal_module.SIGTERM = signal.SIGTERM
    signal_module.getsignal.return_value = "application-handler"
    router_module = SimpleNamespace(signal=signal_module)
    created = object()
    factory = MagicMock(return_value=created)
    fake_lxmf = SimpleNamespace(LXMRouter=factory)
    results = []

    def construct():
        results.append(lxmf_compat.create_lxm_router(storagepath="/data/lxmf", autopeer=False))
        router_module.signal.signal(signal.SIGTERM, MagicMock())

    with (
        patch.object(lxmf_compat.importlib, "import_module", return_value=router_module),
        patch.dict(sys.modules, {"LXMF": fake_lxmf}),
    ):
        worker = threading.Thread(target=construct, daemon=True)
        worker.start()
        worker.join(timeout=1)

    assert not worker.is_alive()
    assert results == [created]
    factory.assert_called_once_with(storagepath="/data/lxmf", autopeer=False)
    signal_module.signal.assert_not_called()
    signal_module.getsignal.assert_called_once_with(signal.SIGTERM)
