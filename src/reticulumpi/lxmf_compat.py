"""LXMF construction compatible with daemonized plugin lifecycle workers.

LXMF registers process signal handlers from ``LXMRouter.__init__``. Python
permits that only on the main thread, while ReticulumPi intentionally runs
plugin lifecycle calls on daemon workers so a hung plugin cannot prevent
interpreter exit. ReticulumPi owns SIGINT/SIGTERM handling itself, so worker
routers retain LXMF's atexit cleanup without replacing process handlers.
"""

from __future__ import annotations

import importlib
import threading
from types import ModuleType
from typing import Any


_INSTALL_LOCK = threading.Lock()


class _ThreadAwareSignalProxy:
    """Delegate signal registration only when invoked on the main thread."""

    def __init__(self, signal_module: ModuleType) -> None:
        self._signal_module = signal_module

    def signal(self, signal_number: int, handler: Any) -> Any:
        if threading.current_thread() is threading.main_thread():
            return self._signal_module.signal(signal_number, handler)
        # The application-level handler owns shutdown. Match signal.signal's
        # useful return value without attempting a forbidden worker mutation.
        return self._signal_module.getsignal(signal_number)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._signal_module, name)


def _install_thread_aware_signals() -> None:
    """Patch only LXMF's module-local signal reference, once and atomically."""

    router_module = importlib.import_module("LXMF.LXMRouter")
    with _INSTALL_LOCK:
        current = router_module.signal
        if isinstance(current, _ThreadAwareSignalProxy):
            return
        router_module.signal = _ThreadAwareSignalProxy(current)


def create_lxm_router(*args: Any, **kwargs: Any) -> Any:
    """Construct an ``LXMF.LXMRouter`` safely from any lifecycle thread."""

    import LXMF

    # Preserve the public factory before importing the implementation module.
    # Besides respecting test/integration wrappers, this avoids importlib
    # replacing a deliberately wrapped package attribute during first use.
    router_factory = LXMF.LXMRouter
    _install_thread_aware_signals()
    return router_factory(*args, **kwargs)
