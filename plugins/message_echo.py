"""Message Echo plugin - responds to incoming LXMF messages with an echo."""

import logging

import LXMF
import RNS

from reticulumpi.plugin_base import PluginBase

log = logging.getLogger(__name__)


class MessageEcho(PluginBase):
    """Listens for incoming LXMF messages and replies with an echo."""

    plugin_name = "message_echo"
    plugin_version = "1.0.0"

    def start(self) -> None:
        storage_path = self.config.get("storage_path", "/tmp/reticulumpi_lxmf")

        self.lxmf_router = LXMF.LXMRouter(storagepath=storage_path)
        self.local_lxmf_destination = self.lxmf_router.register_delivery_identity(
            self.identity,
            display_name=self.config.get("display_name", "ReticulumPi Echo"),
        )
        self.lxmf_router.register_delivery_callback(self._handle_message)

        self._active = True
        log.info(
            "LXMF Echo responder active at %s",
            RNS.prettyhexrep(self.local_lxmf_destination.hash),
        )

    def stop(self) -> None:
        self._active = False

    def _handle_message(self, message: LXMF.LXMessage) -> None:
        if not self._active:
            return
        try:
            sender = RNS.prettyhexrep(message.source_hash)
            content = message.content_as_string()
            log.info("Received LXMF message from %s: %s", sender, content[:100])

            reply = LXMF.LXMessage(
                self.local_lxmf_destination,
                message.source_hash,
                f"Echo: {content}",
                desired_method=LXMF.LXMessage.DIRECT,
            )
            self.lxmf_router.handle_outbound(reply)
            log.debug("Sent echo reply to %s", sender)
        except Exception:
            log.exception("Error handling LXMF message")
