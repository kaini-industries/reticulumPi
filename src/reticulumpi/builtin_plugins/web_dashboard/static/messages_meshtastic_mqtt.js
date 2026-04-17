/* ReticulumPi Dashboard — Meshtastic MQTT messages panel wrapper. */
(function () {
  'use strict';
  var R = window.RPI;
  if (!R || !R.createMessagesPanel) return;

  var panel = R.createMessagesPanel({
    rootId: 'msg-mqtt',
    sectionTitle: 'Meshtastic MQTT',
    transport: 'meshtastic',
    subTransport: 'mqtt',
    supportsChannels: true,
    broadcastLabel: 'Broadcast (MQTT)',
  });

  R.updateMessagingMqtt = panel.update;
})();
