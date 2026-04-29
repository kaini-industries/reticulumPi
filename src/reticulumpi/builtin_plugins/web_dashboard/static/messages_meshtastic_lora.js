/* ReticulumPi Dashboard — Meshtastic messages panel wrapper. */
(function () {
  'use strict';
  var R = window.RPI;
  if (!R || !R.createMessagesPanel) return;

  var panel = R.createMessagesPanel({
    rootId: 'msg-lora',
    sectionTitle: 'Meshtastic',
    transport: 'meshtastic',
    subTransport: 'lora',
    supportsChannels: true,
    broadcastLabel: 'Broadcast',
  });

  R.updateMessagingLora = panel.update;
})();
