/* ReticulumPi Dashboard — Meshcore messages panel wrapper. */
(function () {
  'use strict';
  var R = window.RPI;
  if (!R || !R.createMessagesPanel) return;

  var panel = R.createMessagesPanel({
    rootId: 'msg-meshcore',
    sectionTitle: 'Meshcore',
    transport: 'meshcore',
    subTransport: null,
    supportsChannels: false,
    broadcastLabel: 'Broadcast (Meshcore)',
  });

  R.updateMessagingMeshcore = panel.update;
})();
