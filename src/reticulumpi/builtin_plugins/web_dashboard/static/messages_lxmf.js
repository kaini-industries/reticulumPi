/* ReticulumPi Dashboard — LXMF messages panel wrapper. */
(function () {
  'use strict';
  var R = window.RPI;
  if (!R || !R.createMessagesPanel) return;

  var panel = R.createMessagesPanel({
    rootId: 'msg-lxmf',
    sectionTitle: 'LXMF',
    transport: 'lxmf',
    subTransport: null,
    supportsChannels: false,
  });

  R.updateMessagingLxmf = panel.update;
})();
