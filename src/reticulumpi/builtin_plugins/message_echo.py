"""Message Echo plugin - responds to incoming LXMF messages with an echo."""

import os
import threading

import LXMF
import RNS
import RNS.vendor.umsgpack as umsgpack

from reticulumpi.plugin_base import PluginBase


class _PropagationAnnounceHandler:
    """RNS announce handler that auto-selects the nearest LXMF propagation node."""

    def __init__(self, plugin: "MessageEcho"):
        self.aspect_filter = "lxmf.propagation"
        self._plugin = plugin

    def received_announce(self, destination_hash, announced_identity, app_data):
        self._plugin._handle_propagation_announce(destination_hash, announced_identity, app_data)


class MessageEcho(PluginBase):
    """Listens for incoming LXMF messages and replies with an echo."""

    plugin_name = "message_echo"
    plugin_description = "Responds to incoming LXMF messages with an echo reply"
    plugin_version = "1.0.0"

    # NomadNet config directories to write propagation node selection to
    _NOMADNET_CONFIG_DIRS = ["~/.nomadnet", "~/.nomadnet-tui"]

    def start(self) -> None:
        self._lock = threading.Lock()
        default_storage = "~/.local/share/reticulumpi/lxmf"
        storage_path = os.path.expanduser(self.config.get("storage_path", default_storage))

        self.lxmf_router = LXMF.LXMRouter(storagepath=storage_path)
        self.local_lxmf_destination = self.lxmf_router.register_delivery_identity(
            self.identity,
            display_name=self.config.get("display_name", "ReticulumPi Echo"),
        )
        self.lxmf_router.register_delivery_callback(self._handle_message)

        # Auto-select the nearest LXMF propagation node for store-and-forward
        self._best_propagation_hops = RNS.Transport.PATHFINDER_M + 1
        self._propagation_handler = _PropagationAnnounceHandler(self)
        RNS.Transport.register_announce_handler(self._propagation_handler)

        self._active = True
        self.log.info(
            "LXMF Echo responder active at %s",
            RNS.prettyhexrep(self.local_lxmf_destination.hash),
        )

    def stop(self) -> None:
        self._active = False
        RNS.Transport.deregister_announce_handler(self._propagation_handler)
        self.lxmf_router.register_delivery_callback(None)
        self._join_threads()

    def _handle_message(self, message: LXMF.LXMessage) -> None:
        with self._lock:
            if not self._active:
                return
            try:
                sender = RNS.prettyhexrep(message.source_hash)
                content = message.content_as_string()
                self.log.info("Received LXMF message from %s: %s", sender, content[:100])

                reply = LXMF.LXMessage(
                    self.local_lxmf_destination,
                    message.source,
                    f"Echo: {content}",
                    desired_method=LXMF.LXMessage.DIRECT,
                )
                self.lxmf_router.handle_outbound(reply)
                self.log.debug("Sent echo reply to %s", sender)
            except Exception:
                self.log.exception("Error handling LXMF message")

    def _handle_propagation_announce(self, destination_hash, announced_identity, app_data):
        """Auto-select the nearest active propagation node."""
        try:
            if not app_data:
                return

            from LXMF import pn_announce_data_is_valid

            if not pn_announce_data_is_valid(app_data):
                return

            data = umsgpack.unpackb(app_data)
            # data format: [bool enabled, str name, bool active]
            if not (len(data) >= 3 and data[2] is True):
                return

            hops = RNS.Transport.hops_to(destination_hash)
            if hops < self._best_propagation_hops:
                self._best_propagation_hops = hops
                self.lxmf_router.set_outbound_propagation_node(destination_hash)
                self.log.info(
                    "Auto-selected propagation node %s (%d hops)",
                    RNS.prettyhexrep(destination_hash),
                    hops,
                )
                self._write_nomadnet_propagation_node(destination_hash)
        except Exception:
            self.log.exception("Error handling propagation node announce")

    def _write_nomadnet_propagation_node(self, node_hash):
        """Write propagation node to NomadNet peersettings so daemon/TUI use it too."""
        for config_dir in self._NOMADNET_CONFIG_DIRS:
            storage_dir = os.path.expanduser(f"{config_dir}/storage")
            if not os.path.isdir(storage_dir):
                continue
            path = os.path.join(storage_dir, "peersettings")
            try:
                if os.path.exists(path):
                    with open(path, "rb") as f:
                        settings = umsgpack.unpackb(f.read())
                else:
                    settings = {}
                settings["propagation_node"] = node_hash
                tmp_path = f"{path}.tmp"
                with open(tmp_path, "wb") as f:
                    f.write(umsgpack.packb(settings))
                os.replace(tmp_path, path)
                self.log.debug("Wrote propagation node to %s", path)
            except Exception:
                self.log.debug("Could not write propagation node to %s", path)
