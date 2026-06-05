(function() {
  'use strict';
  var R = window.RPI;
  if (!R) return;

  var specWs = null;
  var reconnectDelay = 1000;
  var maxReconnect = 30000;
  var _closing = false;
  var statusEl = document.getElementById('spec-conn-status');

  function setStatus(cls, label) {
    if (!statusEl) return;
    statusEl.className = 'conn-status conn-' + cls;
    var lbl = statusEl.querySelector('.conn-label');
    if (lbl) lbl.textContent = label;
  }

  function connect() {
    if (specWs && (specWs.readyState === WebSocket.CONNECTING || specWs.readyState === WebSocket.OPEN)) return;
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var url = proto + '//' + location.host + '/ws/spectrum';
    try { specWs = new WebSocket(url); } catch(e) { setStatus('err', 'error'); return; }
    R.spectrumWs = specWs;

    specWs.onopen = function() {
      reconnectDelay = 1000;
      setStatus('live', 'live');
    };

    specWs.onmessage = function(ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch(e) { return; }

      if (msg.type === 'pong') return;

      if (msg.type === 'spectrum_history' && msg.data) {
        if (R.spectrumCommon && R.spectrumCommon.historyStore) {
          R.spectrumCommon.historyStore.loadHistory(msg.data);
        }
        return;
      }
      if (msg.type === 'lora_scanner_history' && msg.data) {
        if (R.spectrumCommon && R.spectrumCommon.loraHistoryStore) {
          R.spectrumCommon.loraHistoryStore.loadHistory(msg.data);
        }
        if (msg.data.channel_power_history && R.loraSpectrum && R.loraSpectrum.loadChannelHistory) {
          R.loraSpectrum.loadChannelHistory(msg.data.channel_power_history);
        }
        return;
      }
      if (msg.type === 'link_tester_history' && msg.data) {
        if (R.linkTesterHistoryLoad) R.linkTesterHistoryLoad(msg.data);
        return;
      }
      if (msg.type === 'spectrum_preset_switched') {
        if (R.spectrum && R.spectrum.handlePresetSwitched) {
          R.spectrum.handlePresetSwitched(msg);
        }
        return;
      }
      if (msg.type === 'spectrum_preset_error') {
        if (R.spectrum && R.spectrum.handlePresetError) {
          R.spectrum.handlePresetError(msg.error || 'Preset switch failed');
        }
        return;
      }

      if (msg.type === 'update' && msg.data) {
        var d = msg.data;
        if (d.spectrum && R.spectrumCommon && R.spectrumCommon.historyStore) {
          R.spectrumCommon.historyStore.ingestTick(d.spectrum);
        }
        if (d.lora_scanner && R.spectrumCommon && R.spectrumCommon.loraHistoryStore) {
          R.spectrumCommon.loraHistoryStore.ingestTick(d.lora_scanner);
        }
        requestAnimationFrame(function() {
          if (d.spectrum && R.spectrum && R.spectrum.update) R.spectrum.update(d.spectrum);
          if ((d.spectrum || d.lora_scanner) && R.loraSpectrum && R.loraSpectrum.update) R.loraSpectrum.update(d);
          if (d.link_tester && R.updateLinkTester) R.updateLinkTester(d.link_tester);
        });
      }
    };

    specWs.onclose = function(ev) {
      R.spectrumWs = null;
      if (_closing) return;
      if (ev.code === 4001) {
        setStatus('err', 'auth expired');
        window.location.href = '/login.html';
        return;
      }
      if (ev.code === 4002) {
        setStatus('err', 'max clients');
        return;
      }
      setStatus('wait', 'reconnecting…');
      var jitter = reconnectDelay * (0.5 + Math.random() * 0.5);
      setTimeout(function() {
        reconnectDelay = Math.min(reconnectDelay * 2, maxReconnect);
        connect();
      }, jitter);
    };
  }

  if (R.onWsReady) {
    R.onWsReady(connect);
  } else {
    connect();
  }

  window.addEventListener('pagehide', function() {
    _closing = true;
    if (specWs && specWs.readyState === WebSocket.OPEN) specWs.close(1000);
  });

  var ltToggle = document.getElementById('link-tester-toggle');
  var ltBody = document.getElementById('link-tester-body');
  if (ltToggle && ltBody) {
    ltToggle.addEventListener('click', function() {
      var hidden = ltBody.classList.toggle('hidden');
      var chev = ltToggle.querySelector('.chevron');
      if (chev) chev.textContent = hidden ? '▸' : '▾';
    });
  }
})();
