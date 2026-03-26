"""Message Echo plugin - responds to incoming LXMF messages with an echo."""

import os
import threading

import LXMF
import RNS

from reticulumpi.plugin_base import PluginBase


class MessageEcho(PluginBase):
    """Listens for incoming LXMF messages and replies with an echo."""

    plugin_name = "message_echo"
    plugin_description = "Responds to incoming LXMF messages with an echo reply"
    plugin_version = "1.0.0"

    def start(self) -> None:
        self._lock = threading.Lock()
        default_storage = os.path.expanduser("~/.local/share/reticulumpi/lxmf")
        storage_path = self.config.get("storage_path", default_storage)

        self.lxmf_router = LXMF.LXMRouter(storagepath=storage_path)
        self.local_lxmf_destination = self.lxmf_router.register_delivery_identity(
            self.identity,
            display_name=self.config.get("display_name", "ReticulumPi Echo"),
        )
        self.lxmf_router.register_delivery_callback(self._handle_message)

        self._active = True
        self.log.info(
            "LXMF Echo responder active at %s",
            RNS.prettyhexrep(self.local_lxmf_destination.hash),
        )

    def stop(self) -> None:
        self._active = False
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
