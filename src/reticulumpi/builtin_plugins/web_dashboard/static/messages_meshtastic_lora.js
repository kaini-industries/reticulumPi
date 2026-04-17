/* ReticulumPi Dashboard — Meshtastic LoRa messages panel wrapper. */
(function () {
  'use strict';
  var R = window.RPI;
  if (!R || !R.createMessagesPanel) return;

  var panel = R.createMessagesPanel({
    rootId: 'msg-lora',
    sectionTitle: 'Meshtastic LoRa',
    transport: 'meshtastic',
    subTransport: 'lora',
    supportsChannels: true,
    broadcastLabel: 'Broadcast (LoRa)',
  });

  R.updateMessagingLora = panel.update;
})();
