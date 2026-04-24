/* ReticulumPi Dashboard — Mesh Bridge control panel */
(function () {
  'use strict';
  var R = window.RPI;
  if (!R) return;
  var api = R.api, $ = R.$;

  var _dom = null;
  var _pendingAction = false;

  function _resolveDom() {
    if (_dom) return true;
    var section = $('mesh-bridge-section');
    if (!section) return false;
    _dom = {
      section: section,
      pill: $('mesh-bridge-status-pill'),
      toggle: $('mesh-bridge-toggle'),
      toggleLabel: $('mesh-bridge-toggle-label'),
      banner: $('mesh-bridge-auto-pause-banner'),
      bannerReason: $('mesh-bridge-auto-pause-reason'),
      resumeBtn: $('mesh-bridge-resume-btn'),
      stats: $('mesh-bridge-stats'),
    };
    return true;
  }

  function _setPill(label, cls) {
    if (!_dom.pill) return;
    _dom.pill.textContent = label;
    _dom.pill.className = 'mesh-bridge-status-pill ' + cls;
  }

  function _renderStats(stats) {
    if (!_dom.stats || !stats) { return; }
    var relayed = (stats.msgs_relayed_mesh_to_core || 0)
                + (stats.msgs_relayed_core_to_mesh || 0);
    var filterTotal = (stats.msgs_dropped_filter || 0)
                    + (stats.msgs_dropped_position_share || 0)
                    + (stats.msgs_dropped_tapback || 0);
    var parts = [
      'Relayed: ' + relayed,
      'Loop: ' + (stats.msgs_dropped_loop || 0),
      'Dedup: ' + (stats.msgs_dropped_dedup || 0),
      'Paused: ' + (stats.msgs_dropped_paused || 0),
      'No pair: ' + (stats.msgs_dropped_no_pair || 0),
      'Filter: ' + filterTotal,
      'Grace: ' + (stats.msgs_dropped_startup_grace || 0),
      'Send failed: ' + (stats.msgs_dropped_send_failed || 0),
    ];
    _dom.stats.textContent = parts.join(' \u00b7 ');
  }

  function _render(status) {
    if (!_resolveDom()) return;
    if (_pendingAction) return;
    if (!status || status.available === false) {
      _dom.section.style.display = 'none';
      return;
    }
    _dom.section.style.display = '';
    if (!status.config_enabled) {
      _setPill('Disabled', 'disabled');
      _dom.toggle.disabled = true;
      _dom.toggle.checked = false;
      _dom.toggleLabel.textContent = 'Plugin disabled in config';
      _dom.banner.classList.add('hidden');
      _renderStats(status.stats);
      return;
    }
    _dom.toggle.disabled = false;
    _dom.toggle.checked = !!status.running;
    if (status.running) {
      _setPill('Relaying', 'running');
      _dom.toggleLabel.textContent = 'Relaying';
      _dom.banner.classList.add('hidden');
    } else {
      _setPill('Paused', 'paused');
      _dom.toggleLabel.textContent = 'Paused';
      if (status.auto_paused_reason) {
        _dom.banner.classList.remove('hidden');
        _dom.bannerReason.textContent =
          'Paused automatically \u2014 ' + status.auto_paused_reason;
      } else {
        _dom.banner.classList.add('hidden');
      }
    }
    _renderStats(status.stats);
  }

  function refresh() {
    api('/api/mesh_bridge/status').then(function (r) {
      if (!r) return;
      if (r.ok && r.data) _render(r.data);
    });
  }

  function _setRunning(running) {
    _pendingAction = true;
    api('/api/mesh_bridge/running', {
      method: 'POST',
      body: { running: running },
    }).then(function (r) {
      _pendingAction = false;
      if (r && r.ok && r.data) _render(r.data);
      else refresh();
    });
  }

  function _init() {
    if (!_resolveDom()) {
      setTimeout(_init, 100);
      return;
    }
    _dom.toggle.addEventListener('change', function () {
      _setRunning(_dom.toggle.checked);
    });
    _dom.resumeBtn.addEventListener('click', function () {
      _setRunning(true);
    });
    refresh();
  }

  R.updateMeshBridge = _render;
  R.refreshMeshBridge = refresh;
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _init);
  } else {
    _init();
  }
})();
