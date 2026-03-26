"""Abstract base class for all reticulumPi plugins."""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from reticulumpi.app import ReticulumPiApp


class PluginBase(ABC):
    """Base class all reticulumPi plugins must inherit from.

    Subclasses must set `plugin_name` and `plugin_version` as class attributes,
    and implement the `start()` and `stop()` methods.
    """

    plugin_name: str = "unnamed"
    plugin_version: str = "0.0.0"

    def __init__(self, app: "ReticulumPiApp", plugin_config: dict[str, Any]):
        self.app = app
        self.config = plugin_config
        self.rns = app.reticulum
        self.identity = app.identity
        self._active = False

    @abstractmethod
    def start(self) -> None:
        """Called when the app starts. Create destinations, register handlers, start threads."""

    @abstractmethod
    def stop(self) -> None:
        """Called on shutdown. Clean up resources, deregister handlers."""

    def get_status(self) -> dict[str, Any]:
        """Return status info for monitoring. Override for richer status."""
        return {"active": self._active}
