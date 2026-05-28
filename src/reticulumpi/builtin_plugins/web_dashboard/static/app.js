/* ReticulumPi Dashboard — vanilla JS */
(function() {
  'use strict';

  /* ── Shared namespace ─────────────────────────────────────────────── */
  var RPI = window.RPI = {};

  var token = '';
  var ws = null;
  var reconnectDelay = 1000;
  var maxReconnect = 30000;
  var pollTimer = null;
  var uptimeStart = 0;
  var uptimeTimer = null;
  var prevIfaces = {};      // {name: {rxb, txb, time}} for rate calculation
  var configIfaces = {};    // {name: {enabled, type, properties}} from config file
  var pendingRestart = false;
  var lastLiveIfaces = [];  // last live interfaces from RNS
  var _wsFirstTick = false;
  var _wsReadyCallbacks = [];

  // --- Helpers ---

  function api(path, opts) {
    opts = opts || {};
    var headers = opts.headers || {};
    headers['Accept'] = 'application/json';
    headers['X-Requested-With'] = 'XMLHttpRequest';
    if (token) headers['Authorization'] = 'Bearer ' + token;
    if (opts.body) headers['Content-Type'] = 'application/json';
    var timeoutMs = opts.timeout || 10000;
    var ctrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var timer = ctrl ? setTimeout(function() { try { ctrl.abort(); } catch (e) {} }, timeoutMs) : null;
    return fetch(path, {
      method: opts.method || 'GET',
      headers: headers,
      credentials: 'same-origin',
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      signal: ctrl ? ctrl.signal : undefined
    }).then(function(r) {
      if (timer) clearTimeout(timer);
      if (r.status === 401) { window.location.href = '/login.html'; return null; }
      return r.json().catch(function() { return {ok: false, error: 'Invalid response'}; });
    }).catch(function() { if (timer) clearTimeout(timer); return null; });
  }

  function apiRetry(path, opts, maxRetries) {
    maxRetries = maxRetries || 2;
    return api(path, opts).then(function(r) {
      if (r !== null || maxRetries <= 0) return r;
      return new Promise(function(resolve) {
        setTimeout(function() {
          resolve(apiRetry(path, opts, maxRetries - 1));
        }, 500);
      });
    });
  }

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  function formatUptime(seconds) {
    if (!seconds || seconds < 0) return '--';
    var d = Math.floor(seconds / 86400);
    var h = Math.floor((seconds % 86400) / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    var s = Math.floor(seconds % 60);
    if (d > 0) return d + 'd ' + h + 'h ' + m + 'm';
    if (h > 0) return h + 'h ' + m + 'm ' + s + 's';
    return m + 'm ' + s + 's';
  }

  function formatBytes(b) {
    if (b == null) return '--';
    if (b < 1024) return b + ' B';
    if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
    if (b < 1073741824) return (b / 1048576).toFixed(1) + ' MB';
    return (b / 1073741824).toFixed(2) + ' GB';
  }

  function formatRate(bytesPerSec) {
    if (bytesPerSec == null || bytesPerSec < 0) return '--';
    if (bytesPerSec < 1) return '0 B/s';
    if (bytesPerSec < 1024) return bytesPerSec.toFixed(0) + ' B/s';
    if (bytesPerSec < 1048576) return (bytesPerSec / 1024).toFixed(1) + ' KB/s';
    return (bytesPerSec / 1048576).toFixed(2) + ' MB/s';
  }

  function metricClass(value, warn, crit) {
    if (value == null) return '';
    if (value >= crit) return 'metric-crit';
    if (value >= warn) return 'metric-warn';
    return 'metric-ok';
  }

  // --- Panel visibility for WS update gating ---

  function isPanelVisible(bodyId) {
    var body = document.getElementById(bodyId);
    if (!body) return true;
    if (!body.classList.contains('hidden')) return true;
    var secId = bodyId.replace(/-body$/, '-section');
    var sec = document.getElementById(secId);
    return sec ? sec.style.display === 'none' : false;
  }
  RPI.isPanelVisible = isPanelVisible;

  var _stash = {};
  var _sectionOnExpand = {};

  // --- Section freshness tracking ---

  var _sectionUpdated = {};  // sectionId -> timestamp (seconds)
  var _STALE_THRESHOLD = 30; // seconds before marking stale
  var _GLOBAL_STALE_THRESHOLD = 60;
  var _lastWsUpdate = 0;
  var _lastHttpUpdate = 0;
  var _lastSnapshotRequest = 0;
  var _tabHiddenSince = 0;
  var _TAB_STALE_SECONDS = 30;

  function markUpdated(sectionId) {
    _sectionUpdated[sectionId] = Date.now() / 1000;
    var sec = document.getElementById(sectionId);
    if (sec) sec.classList.remove('awaiting-data');
  }

  function _refreshFreshness() {
    var now = Date.now() / 1000;
    for (var id in _sectionUpdated) {
      var el = document.querySelector('#' + id + ' .freshness');
      if (!el) continue;
      var age = Math.floor(now - _sectionUpdated[id]);
      if (age < 2) { el.textContent = 'just now'; el.className = 'freshness'; }
      else if (age < 60) { el.textContent = age + 's ago'; el.className = 'freshness' + (age >= _STALE_THRESHOLD ? ' stale' : ''); }
      else { el.textContent = Math.floor(age / 60) + 'm ago'; el.className = 'freshness stale'; }
    }
  }
  setInterval(_refreshFreshness, 2000);

  function _showStaleBanner() {
    var el = document.getElementById('stale-banner');
    if (el) el.style.display = 'flex';
  }

  function _hideStaleBanner() {
    var el = document.getElementById('stale-banner');
    if (el && el.style.display !== 'none') el.style.display = 'none';
  }

  function _requestSnapshot() {
    var now = Date.now() / 1000;
    if (now - _lastSnapshotRequest < 10) return;
    _lastSnapshotRequest = now;
    if (RPI.ws && RPI.ws.readyState === WebSocket.OPEN) {
      RPI.ws.send(JSON.stringify({action: 'request_snapshot'}));
    }
  }

  function _manualRefresh() {
    var now = Date.now() / 1000;
    if (now - _lastSnapshotRequest < 10) return;
    _lastSnapshotRequest = now;
    var btn = $('stale-refresh-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'Refreshing…'; }
    if (RPI.ws && RPI.ws.readyState === WebSocket.OPEN) {
      RPI.ws.send(JSON.stringify({action: 'request_snapshot'}));
    } else {
      fetchCritical();
      fetchSecondary();
      fetchWsUncovered();
      connectWS();
    }
    setTimeout(function() {
      if (btn) { btn.disabled = false; btn.textContent = 'Refresh'; }
    }, 3000);
  }

  function _checkStaleness() {
    var latest = Math.max(_lastWsUpdate, _lastHttpUpdate);
    if (!latest) return;
    var age = (Date.now() / 1000) - latest;
    if (age > _GLOBAL_STALE_THRESHOLD) _showStaleBanner();
    else if (age < _STALE_THRESHOLD) _hideStaleBanner();
  }
  setInterval(_checkStaleness, 10000);

  document.addEventListener('visibilitychange', function() {
    if (document.hidden) {
      _tabHiddenSince = Date.now() / 1000;
    } else if (_tabHiddenSince > 0) {
      var away = (Date.now() / 1000) - _tabHiddenSince;
      _tabHiddenSince = 0;
      if (away >= _TAB_STALE_SECONDS) {
        _checkStaleness();
        _requestSnapshot();
      }
    }
  });

  // --- Rendering ---

  var _prevMetricValues = {};

  function flashCard(card) {
    if (!card) return;
    card.classList.remove('flash');
    void card.offsetWidth;
    card.classList.add('flash');
  }

  function setMetric(id, value, unit, warnAt, critAt) {
    var el = $(id);
    if (!el) return;
    var card = el.closest('.metric-card');
    var ringId = id.replace('m-', 'ring-');
    var ring = $(ringId);

    if (value == null || value === undefined) {
      el.innerHTML = '--<span class="unit">' + unit + '</span>';
      el.className = 'value';
      if (card) card.className = 'metric-card';
      if (ring) ring.style.setProperty('--pct', '0');
      return;
    }
    var display = (typeof value === 'number') ? value.toFixed(1) : value;
    el.innerHTML = esc(String(display)) + '<span class="unit">' + unit + '</span>';
    var cls = metricClass(value, warnAt, critAt);
    el.className = 'value ' + cls;
    if (card) card.className = 'metric-card ' + cls;

    if (ring) {
      var pct = Math.min(100, Math.max(0, value));
      ring.style.setProperty('--pct', pct);
    }

    var prevVal = _prevMetricValues[id];
    _prevMetricValues[id] = display;
    if (prevVal !== undefined && prevVal !== display && card) {
      flashCard(card);
    }
  }

  function formatTimeAgo(timestamp) {
    if (!timestamp) return '--';
    var seconds = Math.floor(Date.now() / 1000 - timestamp);
    if (seconds < 0) seconds = 0;
    if (seconds < 60) return seconds + 's ago';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm ago';
    if (seconds < 86400) return Math.floor(seconds / 3600) + 'h ago';
    return Math.floor(seconds / 86400) + 'd ago';
  }

  // Shared detail-item builder used by mesh.js, lora.js, and meshtastic.js
  function _di(label, value, cls) {
    return '<div class="node-detail-item">'
      + '<span class="node-detail-label">' + label + '</span>'
      + '<span class="node-detail-value' + (cls ? ' ' + cls : '') + '">' + value + '</span>'
      + '</div>';
  }

  function onWsReady(fn) {
    if (_wsFirstTick) { fn(); return; }
    _wsReadyCallbacks.push(fn);
  }

  /* ── Expose shared utilities for sub-modules ─────────────────────── */
  RPI.api = api;
  RPI.apiRetry = apiRetry;
  RPI.$ = $;
  RPI.esc = esc;
  RPI.formatUptime = formatUptime;
  RPI.formatBytes = formatBytes;
  RPI.formatRate = formatRate;
  RPI.metricClass = metricClass;
  RPI.markUpdated = markUpdated;
  RPI.setMetric = setMetric;
  RPI.formatTimeAgo = formatTimeAgo;
  RPI._di = _di;
  RPI.onWsReady = onWsReady;

  // Shared mutable object used by both mesh.js and lora.js
  RPI._reachScores = {};

  // --- Metrics ---

  var _metricHistory = { cpu: [], temp: [], mem: [], disk: [], ws_latency: [], ws_msgrate: [], ws_clients: [] };
  var _METRIC_HISTORY_MAX = 30;
  var _wsLatency = null;
  var _wsMsgRateWindow = [];
  var _wsPingTimer = null;
  var _wsMaxClients = 10;

  function pushMetricHistory(key, value) {
    if (value == null) return;
    var arr = _metricHistory[key];
    arr.push(value);
    if (arr.length > _METRIC_HISTORY_MAX) arr.shift();
  }

  function renderMetricSparkline(containerId, values) {
    var el = $(containerId);
    if (!el || !values || values.length < 2) { if (el) el.innerHTML = ''; return; }
    var sig = values.length + ':' + values[values.length - 1].toFixed(1);
    if (el._lastSig === sig) return;
    el._lastSig = sig;
    var min = Infinity, max = -Infinity;
    for (var i = 0; i < values.length; i++) {
      if (values[i] < min) min = values[i];
      if (values[i] > max) max = values[i];
    }
    var range = max - min || 1;
    var w = 160, h = 24, pad = 2;
    var points = [];
    for (var j = 0; j < values.length; j++) {
      var x = (j / (values.length - 1)) * w;
      var y = h - pad - ((values[j] - min) / range) * (h - pad * 2);
      points.push(x.toFixed(1) + ',' + y.toFixed(1));
    }
    var polyline = points.join(' ');
    var area = polyline + ' ' + w + ',' + h + ' 0,' + h;
    el.innerHTML = '<svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none">'
      + '<polygon class="spark-area" points="' + area + '"/>'
      + '<polyline class="spark-line" points="' + polyline + '"/>'
      + '</svg>';
  }

  function updateHeaderHealth(metrics) {
    var header = document.querySelector('.header');
    if (!header) return;
    var vals = [
      metricClass(metrics.cpu_percent, 70, 90),
      metricClass(metrics.cpu_temp, 65, 80),
      metricClass(metrics.memory_percent, 70, 90),
      metricClass(metrics.disk_percent, 80, 95)
    ];
    var color;
    if (vals.indexOf('metric-crit') >= 0) color = 'var(--red)';
    else if (vals.indexOf('metric-warn') >= 0) color = 'var(--yellow)';
    else color = 'var(--green)';
    header.style.setProperty('--health-color', color);
  }

  function updateMetrics(metrics) {
    if (!metrics) return;
    markUpdated('metrics-grid');
    setMetric('m-cpu', metrics.cpu_percent, '%', 70, 90);
    setMetric('m-temp', metrics.cpu_temp, '\u00B0C', 65, 80);
    setMetric('m-mem', metrics.memory_percent, '%', 70, 90);
    setMetric('m-disk', metrics.disk_percent, '%', 80, 95);

    pushMetricHistory('cpu', metrics.cpu_percent);
    pushMetricHistory('temp', metrics.cpu_temp);
    pushMetricHistory('mem', metrics.memory_percent);
    pushMetricHistory('disk', metrics.disk_percent);
    renderMetricSparkline('spark-cpu', _metricHistory.cpu);
    renderMetricSparkline('spark-temp', _metricHistory.temp);
    renderMetricSparkline('spark-mem', _metricHistory.mem);
    renderMetricSparkline('spark-disk', _metricHistory.disk);

    updateHeaderHealth(metrics);
  }

  function updateWsStats(wsStats) {
    if (wsStats) {
      _wsMaxClients = wsStats.max_clients || 10;
      var clients = wsStats.clients;
      var clientWarn = Math.round(_wsMaxClients * 0.7);
      var clientCrit = Math.round(_wsMaxClients * 0.9);
      setMetric('m-ws-clients', clients, '', clientWarn, clientCrit);
      var clientRing = $('ring-ws-clients');
      if (clientRing) clientRing.style.setProperty('--pct', Math.min(100, (clients / _wsMaxClients) * 100));
      pushMetricHistory('ws_clients', clients);
      renderMetricSparkline('spark-ws-clients', _metricHistory.ws_clients);
    }

    if (_wsLatency !== null) {
      setMetric('m-ws-latency', _wsLatency, 'ms', 200, 400);
      var latRing = $('ring-ws-latency');
      if (latRing) latRing.style.setProperty('--pct', Math.min(100, (_wsLatency / 500) * 100));
      pushMetricHistory('ws_latency', _wsLatency);
      renderMetricSparkline('spark-ws-latency', _metricHistory.ws_latency);
    }

    var now = Date.now();
    while (_wsMsgRateWindow.length && _wsMsgRateWindow[0] < now - 10000) _wsMsgRateWindow.shift();
    var rate = _wsMsgRateWindow.length / 10;
    var rateDisplay = rate.toFixed(1);
    var rateEl = $('m-ws-msgrate');
    if (rateEl) rateEl.innerHTML = esc(rateDisplay) + '<span class="unit">/s</span>';
    var rateCard = document.getElementById('card-ws-msgrate');
    if (rateCard) rateCard.className = 'metric-card ' + (rate < 0.05 ? 'metric-warn' : 'metric-ok');
    var rateRing = $('ring-ws-msgrate');
    if (rateRing) rateRing.style.setProperty('--pct', Math.min(100, (rate / 2) * 100));
    pushMetricHistory('ws_msgrate', rate);
    renderMetricSparkline('spark-ws-msgrate', _metricHistory.ws_msgrate);
  }

  // --- Plugins ---

  function updatePlugins(plugins, failedPlugins) {
    var tbody = $('plugins-table');
    if (!tbody) return;
    var html = '';
    var count = 0;

    if (plugins) {
      var names = Object.keys(plugins).sort();
      count = names.length;
      for (var i = 0; i < names.length; i++) {
        var name = names[i];
        var p = plugins[name];
        var st = p.status || {};
        var active = st.active;
        var dotClass = active ? 'status-active' : 'status-inactive';
        var statusText = active ? 'Active' : 'Stopped';

        // Build details from status keys
        var details = [];
        if (st.web_url) details.push('URL: ' + st.web_url);
        if (st.pid) details.push('PID: ' + st.pid);
        if (st.restart_count > 0) details.push('Restarts: ' + st.restart_count);

        var addr = p.address || '';

        html += '<tr>'
          + '<td>' + esc(name) + '</td>'
          + '<td>' + esc(p.version || '--') + '</td>'
          + '<td><span class="status-dot ' + dotClass + '" title="' + statusText + '"></span></td>'
          + '<td class="addr">' + esc(addr || '--') + '</td>'
          + '<td>' + esc(details.join(', ') || p.description || '') + '</td>'
          + '</tr>';
      }
    }

    // Failed plugins
    if (failedPlugins && failedPlugins.length > 0) {
      for (var j = 0; j < failedPlugins.length; j++) {
        var fp = failedPlugins[j];
        html += '<tr>'
          + '<td>' + esc(fp.name) + '</td>'
          + '<td>--</td>'
          + '<td><span class="status-dot status-failed" title="Failed"></span></td>'
          + '<td>--</td>'
          + '<td>' + esc(fp.error) + '</td>'
          + '</tr>';
        count++;
      }
    }

    tbody.innerHTML = html;
    $('plugin-count').textContent = count + ' total';

    // Failed alert
    var alertEl = $('failed-alert');
    if (failedPlugins && failedPlugins.length > 0) {
      $('failed-list').textContent = failedPlugins.map(function(f) { return f.name + ': ' + f.error; }).join('; ');
      alertEl.classList.remove('hidden');
    } else {
      alertEl.classList.add('hidden');
    }
  }

  // --- Interfaces ---

  function updateInterfaces(interfaces) {
    var tbody = $('interfaces-table');
    if (!tbody) return;
    markUpdated('interfaces-section');
    lastLiveIfaces = interfaces || [];

    // Build merged list: union of config interfaces and live interfaces
    var merged = [];
    var liveByName = {};
    for (var i = 0; i < lastLiveIfaces.length; i++) {
      // Live interface names from rnsd look like "TCPInterface[TCP Client beleth/host:port]"
      // Extract the label portion for matching against config names
      var raw = lastLiveIfaces[i].name || '';
      var label = _extractIfaceLabel(raw);
      liveByName[label] = lastLiveIfaces[i];
    }

    // Start with config interfaces (preserves config order, shows disabled ones)
    var seen = {};
    var configNames = Object.keys(configIfaces);
    for (var c = 0; c < configNames.length; c++) {
      var cname = configNames[c];
      var cfg = configIfaces[cname];
      var live = liveByName[cname] || null;
      merged.push({name: cname, cfg: cfg, live: live});
      seen[cname] = true;
    }
    // Add any live interfaces not in config (shouldn't happen, but be safe)
    for (var lname in liveByName) {
      if (!seen[lname]) {
        merged.push({name: lname, cfg: null, live: liveByName[lname]});
      }
    }

    if (merged.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5">No interfaces detected</td></tr>';
      $('iface-count').textContent = '0';
      return;
    }

    var now = Date.now() / 1000;
    var html = '';
    var activeCount = 0;
    for (var m = 0; m < merged.length; m++) {
      var entry = merged[m];
      var isEnabled = entry.cfg ? entry.cfg.enabled : true;
      var isLive = entry.live != null;
      var online = isLive && entry.live.online !== false;
      var rowClass = isEnabled ? '' : ' class="row-disabled"';

      // Toggle switch (no inline handler -- CSP blocks inline scripts)
      var toggleHtml = '';
      if (entry.cfg) {
        var checked = isEnabled ? ' checked' : '';
        toggleHtml = '<label class="toggle-switch">'
          + '<input type="checkbox"' + checked + ' data-iface="' + esc(entry.name) + '">'
          + '<span class="toggle-slider"></span>'
          + '</label>';
      }

      // Status
      var statusHtml;
      if (!isEnabled) {
        statusHtml = '<span class="text-muted">Disabled</span>';
      } else if (isLive) {
        var dotClass = online ? 'status-active' : 'status-inactive';
        statusHtml = '<span class="status-dot ' + dotClass + '"></span>' + (online ? 'Online' : 'Offline');
        activeCount++;
      } else {
        statusHtml = '<span class="text-muted">Not active</span>';
      }

      // Traffic
      var traffic = '';
      if (isLive && (entry.live.rxb != null || entry.live.txb != null)) {
        var rxRate = null, txRate = null;
        var prev = prevIfaces[entry.name];
        if (prev) {
          var dt = now - prev.time;
          if (dt > 0.5) {
            rxRate = (entry.live.rxb - prev.rxb) / dt;
            txRate = (entry.live.txb - prev.txb) / dt;
          }
        }
        traffic = 'RX: ' + formatBytes(entry.live.rxb);
        if (rxRate != null) traffic += ' (' + formatRate(rxRate) + ')';
        traffic += ' / TX: ' + formatBytes(entry.live.txb);
        if (txRate != null) traffic += ' (' + formatRate(txRate) + ')';
        prevIfaces[entry.name] = {rxb: entry.live.rxb, txb: entry.live.txb, time: now};
      } else if (!isEnabled) {
        traffic = '<span class="text-muted">\u2014</span>';
      }

      // Type from config or live
      var ifaceType = (entry.cfg ? entry.cfg.type : '') || (entry.live ? entry.live.type : '');

      html += '<tr' + rowClass + '>'
        + '<td>' + toggleHtml + '</td>'
        + '<td>' + esc(entry.name) + '</td>'
        + '<td>' + esc(ifaceType) + '</td>'
        + '<td>' + statusHtml + '</td>'
        + '<td>' + traffic + '</td>'
        + '</tr>';
    }

    tbody.innerHTML = html;
    var total = configNames.length || lastLiveIfaces.length;
    $('iface-count').textContent = activeCount + '/' + total;
  }

  function _extractIfaceLabel(rnsName) {
    // "TCPInterface[TCP Client beleth/host:port]" -> "TCP Client beleth"
    // "AutoInterface[Auto Discovery Interface]" -> "Auto Discovery Interface"
    var m = rnsName.match(/\[([^\]\/]+)/);
    if (m) return m[1].trim();
    return rnsName;
  }

  function fetchInterfacesConfig() {
    api('/api/interfaces/config').then(function(r) {
      if (!r || !r.ok) return;
      configIfaces = {};
      var ifaces = r.data.interfaces || [];
      for (var i = 0; i < ifaces.length; i++) {
        configIfaces[ifaces[i].name] = ifaces[i];
      }
      // Re-render with merged data
      updateInterfaces(lastLiveIfaces);
    });
  }

  window._toggleIface = function(name) {
    // Optimistic update so WebSocket re-renders don't revert the toggle
    var prev = configIfaces[name] ? configIfaces[name].enabled : true;
    if (configIfaces[name]) configIfaces[name].enabled = !prev;
    pendingRestart = true;
    $('restart-banner').classList.remove('hidden');
    updateInterfaces(lastLiveIfaces);

    api('/api/interfaces/' + encodeURIComponent(name) + '/toggle', {method: 'POST'})
      .then(function(r) {
        if (!r || !r.ok) {
          // Revert on failure
          if (configIfaces[name]) configIfaces[name].enabled = prev;
          updateInterfaces(lastLiveIfaces);
          alert('Toggle failed: ' + (r ? r.error : 'no response'));
          return;
        }
        // Confirm with server state
        if (configIfaces[name]) configIfaces[name].enabled = r.data.enabled;
      });
  };

  window._setLoraAnnounceMode = function(mode) {
    var sel = $('lora-announce-mode');
    if (sel) sel.disabled = true;
    api('/api/lora/announce_mode', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mode: mode})
    }).then(function(r) {
      if (!r || !r.ok) {
        alert('Failed to set announce mode: ' + (r ? (r.error || 'unknown error') : 'no response'));
        // Revert select
        if (sel) { sel.value = RPI._currentLoraAnnounceMode(); sel.disabled = false; }
        return;
      }
      RPI._setCurrentLoraAnnounceMode(mode);
      if (sel) sel.disabled = false;
      // rnsd was restarted -- data will refresh on next poll cycle
    });
  };

  function doRestart() {
    if (!confirm('Restart rnsd and reticulumpi? The dashboard will be briefly unavailable.')) return;
    var btn = $('restart-btn');
    btn.disabled = true;
    btn.textContent = 'Restarting\u2026';
    api('/api/services/restart', {method: 'POST'}).then(function() {
      startRestartWatcher();
    });
  }

  function startRestartWatcher() {
    var attempts = 0;
    var maxAttempts = 30;
    var check = setInterval(function() {
      attempts++;
      fetch('/api/status', {credentials: 'same-origin'})
        .then(function(r) {
          if (r.ok) {
            clearInterval(check);
            pendingRestart = false;
            $('restart-banner').classList.add('hidden');
            var btn = $('restart-btn');
            btn.disabled = false;
            btn.textContent = 'Restart Services';
            window.location.reload();
          }
        })
        .catch(function() { /* still down */ });
      if (attempts >= maxAttempts) {
        clearInterval(check);
        var btn = $('restart-btn');
        btn.textContent = 'Restart timed out \u2014 refresh page';
        btn.disabled = false;
      }
    }, 3000);
  }

  // --- Alerts ---

  function updateAlerts(alertData) {
    var el = $('alerts-info');
    if (!el) return;
    if (!alertData || alertData.message === 'alert_system plugin not available') {
      el.textContent = 'Alert system not enabled';
      $('alerts-count').textContent = '';
      return;
    }
    var html = 'Alerts sent: ' + (alertData.alerts_sent || 0);
    if (alertData.last_alert) {
      html += ' | Last: ' + esc(alertData.last_alert.message || '')
        + ' (' + formatTimeAgo(alertData.last_alert.time) + ')';
    }
    html += ' | Recipients: ' + (alertData.recipients || 0);
    el.innerHTML = html;
    $('alerts-count').textContent = (alertData.alerts_sent || 0) + ' sent';
  }

  // --- Shared Files ---

  function updateSharedFiles(files) {
    var tbody = $('files-table');
    if (!tbody) return;
    if (!files || files.length === 0) {
      tbody.innerHTML = '<tr><td colspan="3" class="text-muted">No shared files</td></tr>';
      $('files-count').textContent = '0';
      return;
    }
    var html = '';
    for (var i = 0; i < files.length; i++) {
      var f = files[i];
      html += '<tr>'
        + '<td>' + esc(f.name) + '</td>'
        + '<td>' + formatBytes(f.size) + '</td>'
        + '<td>' + (f.modified ? formatTimeAgo(f.modified) : '--') + '</td>'
        + '</tr>';
    }
    tbody.innerHTML = html;
    $('files-count').textContent = files.length + ' files';
  }

  // -- Sensor rendering ---------------------------------------------------

  var _sensorHistory = {};   // { "sensorName:field": [value, value, ...] }
  var _sensorHistoryMax = 60;

  // Unit & threshold metadata per reading field name
  var SENSOR_FIELDS = {
    temperature: { unit: '\u00b0C', precision: 1,
      thresh: function(v) { return v < 10 ? 'sv-cold' : v < 35 ? 'sv-ok' : v < 45 ? 'sv-warm' : 'sv-hot'; }
    },
    humidity: { unit: '%', precision: 1,
      thresh: function(v) { return v < 25 ? 'sv-dry' : v < 65 ? 'sv-ok' : v < 80 ? 'sv-wet' : 'sv-damp'; }
    },
    pressure: { unit: ' hPa', precision: 1, thresh: function() { return ''; } },
    voltage: { unit: ' V', precision: 2, thresh: function() { return ''; } },
    current: { unit: ' A', precision: 3, thresh: function() { return ''; } },
    power: { unit: ' W', precision: 1, thresh: function() { return ''; } },
    quality: { unit: '', precision: 0, thresh: function() { return ''; } }
  };

  function sensorFieldMeta(key) {
    if (SENSOR_FIELDS[key]) return SENSOR_FIELDS[key];
    // Auto-detect from key name
    if (/temp/i.test(key)) return SENSOR_FIELDS.temperature;
    if (/humid/i.test(key)) return SENSOR_FIELDS.humidity;
    if (/press/i.test(key)) return SENSOR_FIELDS.pressure;
    if (/volt/i.test(key)) return SENSOR_FIELDS.voltage;
    return { unit: '', precision: 2, thresh: function() { return ''; } };
  }

  function buildSparkline(values) {
    if (!values || values.length < 2) return '';
    var min = Infinity, max = -Infinity;
    for (var i = 0; i < values.length; i++) {
      if (values[i] < min) min = values[i];
      if (values[i] > max) max = values[i];
    }
    var range = max - min || 1;
    var w = 200, h = 36, pad = 2;
    var points = [];
    for (var j = 0; j < values.length; j++) {
      var x = (j / (values.length - 1)) * w;
      var y = h - pad - ((values[j] - min) / range) * (h - pad * 2);
      points.push(x.toFixed(1) + ',' + y.toFixed(1));
    }
    var polyline = points.join(' ');
    // Area: close the path to the bottom
    var area = polyline + ' ' + w + ',' + h + ' 0,' + h;
    var last = values[values.length - 1];
    var lastX = w;
    var lastY = h - pad - ((last - min) / range) * (h - pad * 2);
    return '<div class="sensor-sparkline">'
      + '<svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none">'
      + '<defs><linearGradient id="spark-grad" x1="0" y1="0" x2="0" y2="1">'
      + '<stop offset="0%" stop-color="var(--accent)"/>'
      + '<stop offset="100%" stop-color="transparent"/>'
      + '</linearGradient></defs>'
      + '<polygon class="spark-area" points="' + area + '"/>'
      + '<polyline class="spark-line" points="' + polyline + '"/>'
      + '<circle class="spark-dot" cx="' + lastX.toFixed(1) + '" cy="' + lastY.toFixed(1) + '" r="2.5"/>'
      + '</svg></div>';
  }

  function fetchSensorHistory(sensorNames) {
    for (var i = 0; i < sensorNames.length; i++) {
      (function(name) {
        api('/api/sensors/history?sensor=' + encodeURIComponent(name) + '&limit=' + _sensorHistoryMax)
          .then(function(r) {
            if (!r || !r.ok || !r.data.history) return;
            // History comes newest-first; group by reading field and reverse
            var byField = {};
            var hist = r.data.history;
            for (var j = hist.length - 1; j >= 0; j--) {
              var key = name + ':' + hist[j].reading;
              if (!byField[key]) byField[key] = [];
              byField[key].push(hist[j].value);
            }
            for (var k in byField) {
              _sensorHistory[k] = byField[k];
            }
            // Re-render with sparklines now available
            if (_lastSensorData) renderSensorCards(_lastSensorData);
          });
      })(sensorNames[i]);
    }
  }

  var _lastSensorData = null;

  function updateSensors(sensors) {
    var grid = $('sensors-grid');
    if (!grid) return;
    markUpdated('sensors-section');
    if (!sensors || Object.keys(sensors).length === 0) {
      grid.innerHTML = '<div class="sensor-card"><div class="sensor-name text-muted">No sensor plugins active</div></div>';
      $('sensors-count').textContent = '';
      return;
    }
    // Track history from live updates
    var names = Object.keys(sensors);
    for (var i = 0; i < names.length; i++) {
      var reading = sensors[names[i]];
      if (reading.error) continue;
      var fields = Object.keys(reading);
      for (var j = 0; j < fields.length; j++) {
        if (fields[j] === 'timestamp') continue;
        var hk = names[i] + ':' + fields[j];
        if (!_sensorHistory[hk]) _sensorHistory[hk] = [];
        _sensorHistory[hk].push(reading[fields[j]]);
        if (_sensorHistory[hk].length > _sensorHistoryMax) {
          _sensorHistory[hk] = _sensorHistory[hk].slice(-_sensorHistoryMax);
        }
      }
    }
    _lastSensorData = sensors;
    renderSensorCards(sensors);
  }

  function renderSensorCards(sensors) {
    var grid = $('sensors-grid');
    if (!grid) return;
    var names = Object.keys(sensors);
    var html = '';
    for (var i = 0; i < names.length; i++) {
      var name = names[i];
      var reading = sensors[name];
      var hasError = !!reading.error;

      html += '<div class="sensor-card' + (hasError ? ' sensor-error-card' : '') + '">';

      // Header: name + driver badge
      html += '<div class="sensor-card-header">'
        + '<span class="sensor-name">' + esc(name.replace(/_/g, ' ')) + '</span>'
        + '</div>';

      if (hasError) {
        html += '<div class="sensor-readings"><div class="sensor-reading">'
          + '<div class="sensor-reading-value">' + esc(reading.error) + '</div>'
          + '</div></div>';
      } else {
        // Reading values
        html += '<div class="sensor-readings">';
        var fields = Object.keys(reading);
        var primaryField = null;
        for (var j = 0; j < fields.length; j++) {
          var k = fields[j];
          if (k === 'timestamp') continue;
          var v = reading[k];
          if (typeof v !== 'number') continue;
          var meta = sensorFieldMeta(k);
          var cls = meta.thresh(v);
          if (!primaryField) primaryField = k;
          html += '<div class="sensor-reading">'
            + '<div class="sensor-reading-value ' + cls + '">'
            + v.toFixed(meta.precision)
            + '<span class="sensor-unit">' + esc(meta.unit) + '</span>'
            + '</div>'
            + '<div class="sensor-reading-label">' + esc(k) + '</div>'
            + '</div>';
        }
        html += '</div>';

        // Sparkline for primary field
        var hk = name + ':' + primaryField;
        if (_sensorHistory[hk] && _sensorHistory[hk].length >= 2) {
          html += buildSparkline(_sensorHistory[hk]);
        }
      }

      // Freshness
      if (reading.timestamp) {
        var age = (Date.now() / 1000) - reading.timestamp;
        var stale = age > 300;
        html += '<div class="sensor-meta">'
          + '<span class="' + (stale ? 'sensor-stale' : '') + '">'
          + (stale ? '\u26a0 ' : '') + formatTimeAgo(reading.timestamp)
          + '</span></div>';
      }

      html += '</div>';
    }
    grid.innerHTML = html;
    $('sensors-count').textContent = names.length + (names.length === 1 ? ' sensor' : ' sensors');
  }

  // --- Emergency Broadcasts ---

  var PRIORITY_NAMES = {0: 'INFO', 1: 'WARNING', 2: 'CRITICAL', 3: 'EMERGENCY'};
  var PRIORITY_CLASSES = {0: '', 1: 'warn', 2: 'crit', 3: 'crit'};

  function updateEmergency(data) {
    var tbody = $('emergency-table');
    if (!tbody) return;
    markUpdated('emergency-section');
    var messages = data.messages || [];
    if (messages.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" class="text-muted">No emergency broadcasts</td></tr>';
      $('emergency-count').textContent = '0';
      return;
    }
    var html = '';
    for (var i = 0; i < messages.length; i++) {
      var m = messages[i];
      var pName = PRIORITY_NAMES[m.priority] || 'UNKNOWN';
      var pClass = PRIORITY_CLASSES[m.priority] || '';
      html += '<tr>'
        + '<td><span class="' + pClass + '">' + esc(pName) + '</span></td>'
        + '<td>' + esc(m.message || '') + '</td>'
        + '<td>' + esc(m.origin_name || m.origin || 'Unknown') + '</td>'
        + '<td>' + formatTimeAgo(m.timestamp) + '</td>'
        + '<td>' + (m.ttl || 0) + '</td>'
        + '</tr>';
    }
    tbody.innerHTML = html;
    $('emergency-count').textContent = messages.length + ' messages';
  }

  // --- Connectivity Health ---

  function updateConnectivity(data) {
    if (!data || !$('connectivity-indicators')) return;
    markUpdated('connectivity-section');

    // rnsd indicator
    var ciRnsd = $('ci-rnsd');
    if (ciRnsd) {
      var rnsdOk = data.rnsd_reachable;
      ciRnsd.textContent = 'rnsd: ' + (rnsdOk ? 'UP' : 'DOWN');
      ciRnsd.className = 'conn-indicator ' + (rnsdOk ? 'ci-ok' : 'ci-err');
    }

    // I2P indicator
    var ciI2p = $('ci-i2p');
    if (ciI2p) {
      var i2pSt = data.i2p_status || 'unknown';
      var i2pPeers = data.i2p_peers || 0;
      var i2pCls = 'ci-info';
      var i2pTxt = 'I2P: ' + i2pSt;
      if (i2pSt === 'ok') { i2pCls = 'ci-ok'; i2pTxt = 'I2P: OK' + (i2pPeers > 0 ? ' (' + i2pPeers + ' RNS peers)' : ''); }
      else if (i2pSt === 'connected') { i2pCls = 'ci-ok'; i2pTxt = 'I2P: ' + i2pPeers + ' peers'; }
      else if (i2pSt === 'firewalled') { i2pCls = 'ci-info'; i2pTxt = 'I2P: firewalled (NAT)'; }
      else if (i2pSt === 'bootstrapping') { i2pCls = 'ci-info'; i2pTxt = 'I2P: bootstrapping'; }
      else if (i2pSt === 'testing') { i2pCls = 'ci-info'; i2pTxt = 'I2P: testing'; }
      else if (i2pSt === 'sam_unreachable') { i2pCls = 'ci-err'; i2pTxt = 'I2P: SAM down'; }
      ciI2p.textContent = i2pTxt;
      ciI2p.className = 'conn-indicator ' + i2pCls;
    }

    // SAM indicator
    var ciSam = $('ci-sam');
    if (ciSam) {
      var samOk = data.sam_reachable;
      ciSam.textContent = 'SAM: ' + (samOk ? 'OK' : 'DOWN');
      ciSam.className = 'conn-indicator ' + (samOk ? 'ci-ok' : 'ci-err');
    }

    // Interfaces indicator
    var ciIfaces = $('ci-ifaces');
    if (ciIfaces) {
      var on = data.interfaces_online || 0;
      var total = data.interfaces_total || 0;
      var ifCls = on === total && total > 0 ? 'ci-ok' : (on > 0 ? 'ci-warn' : 'ci-err');
      ciIfaces.textContent = 'Interfaces: ' + on + '/' + total;
      ciIfaces.className = 'conn-indicator ' + ifCls;
    }

    // Paths indicator
    var ciPaths = $('ci-paths');
    if (ciPaths) {
      var pc = data.path_count || 0;
      var pathCls = pc > 100 ? 'ci-ok' : (pc > 0 ? 'ci-info' : 'ci-warn');
      ciPaths.textContent = 'Paths: ~' + pc;
      ciPaths.className = 'conn-indicator ' + pathCls;
    }

    // Issues list
    var issuesEl = $('connectivity-issues');
    if (issuesEl) {
      var issues = data.issues || [];
      if (issues.length === 0) {
        issuesEl.innerHTML = '';
      } else {
        var html = '';
        for (var i = 0; i < issues.length; i++) {
          var isCritical = issues[i].toLowerCase().indexOf('unreachable') >= 0
            || issues[i].toLowerCase().indexOf('all') >= 0;
          html += '<div class="issue' + (isCritical ? ' critical' : '') + '">\u26A0 ' + esc(issues[i]) + '</div>';
        }
        issuesEl.innerHTML = html;
      }
    }

    // Overall status
    var statusEl = $('connectivity-status');
    if (statusEl) {
      var issues = data.issues || [];
      if (issues.length === 0) {
        statusEl.textContent = 'healthy';
        statusEl.style.color = 'var(--green)';
      } else {
        statusEl.textContent = issues.length + ' issue(s)';
        statusEl.style.color = 'var(--yellow)';
      }
    }
  }

  // --- Messaging Hub --- (code in messages.js module)

  // --- Transport Hubs ---

  // Track previous traffic values for rate calculation (keyed by hub address)
  // Each entry stores {rxb, txb, time} so rates are always computed from
  // the *same* update source's last sample, avoiding WS/polling race conditions.
  var _prevTraffic = {};

  function updateTransport(data) {
    var tbody = $('transport-table');
    if (!tbody) return;
    markUpdated('transport-section');
    var primaries = data.primaries || [];
    var fallbacks = data.active_fallbacks || [];
    var ad = data.auto_discovery || {};
    var poolHubs = ad.connected || [];
    var tcpDisabled = data.tcp_disabled || false;

    var all = primaries
      .concat(fallbacks.map(function(f) { f._tag = 'fallback'; return f; }))
      .concat(poolHubs.map(function(p) { p._tag = 'pool'; p.online = true; return p; }));

    if (all.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" class="text-muted">No TCP transport hubs configured</td></tr>';
      $('transport-count').textContent = '0';
      _updatePoolStatus(ad);
      return;
    }

    var now = Date.now() / 1000;

    var html = '';
    for (var i = 0; i < all.length; i++) {
      var h = all[i];
      var key = (h.target_host || '') + ':' + (h.target_port || '');
      var label = esc(h.name || '--');
      if (h._tag === 'fallback') label += ' <small>(fallback)</small>';
      if (h._tag === 'pool') label += ' <small>(pool)</small>';
      var statusCls = h.online ? 'status-ok' : (h.reconnecting ? 'status-warn' : 'status-err');
      var statusTxt = h.online ? 'Online' : (h.reconnecting ? 'Reconnecting' : 'Offline');
      if (tcpDisabled) {
        statusCls = 'text-muted';
        statusTxt = h.online ? 'Reachable' : 'Unreachable';
      }
      var probeTxt = ' <small class="text-muted">(TCP probe only)</small>';

      var txRate = '--', rxRate = '--';
      if (h.rxb != null && h.txb != null) {
        var prev = _prevTraffic[key];
        if (prev && prev.time) {
          var dt = now - prev.time;
          if (dt > 0.5) {
            var txPerSec = Math.max(0, (h.txb - prev.txb) / dt);
            var rxPerSec = Math.max(0, (h.rxb - prev.rxb) / dt);
            txRate = formatRate(txPerSec);
            rxRate = formatRate(rxPerSec);
          } else {
            txRate = prev.lastTx || '...';
            rxRate = prev.lastRx || '...';
          }
        } else {
          txRate = '...';
          rxRate = '...';
        }
        _prevTraffic[key] = { rxb: h.rxb, txb: h.txb, time: now, lastTx: txRate, lastRx: rxRate };
      }

      html += '<tr>'
        + '<td>' + label + '</td>'
        + '<td class="addr">' + esc(h.target_host || '--') + ':' + (h.target_port || '') + '</td>'
        + '<td><span class="' + statusCls + '">' + statusTxt + '</span>' + probeTxt + '</td>'
        + '<td>\u2191' + txRate + ' \u2193' + rxRate + '</td>'
        + '</tr>';
    }
    tbody.innerHTML = html;
    var online = primaries.filter(function(p) { return p.online; }).length;
    var countText = online + '/' + primaries.length + ' primary';
    if (poolHubs.length > 0) countText += ' + ' + poolHubs.length + ' pool';
    if (tcpDisabled) countText += ' (not connected \u2014 no TCP interfaces enabled)';
    $('transport-count').textContent = countText;
    _updatePoolStatus(ad);
  }

  function _updatePoolStatus(ad) {
    var el = $('pool-status');
    if (!el) return;
    if (!ad.enabled) { el.style.display = 'none'; return; }
    var connected = (ad.connected || []).length;
    var target = ad.target_connections || 0;
    var pool = ad.pool_size || 0;
    var cooldowns = ad.cooldowns ? Object.keys(ad.cooldowns).length : 0;
    var parts = ['Auto-discovery: ' + connected + '/' + target + ' target'];
    parts.push(pool + ' in pool');
    if (cooldowns > 0) parts.push(cooldowns + ' in cooldown');
    el.textContent = parts.join(' \u00b7 ');
    el.style.display = 'block';
  }

  // --- Connection status ---

  function setConnStatus(state) {
    var el = $('conn-status');
    if (!el) return;
    var label = el.querySelector('.conn-label');
    el.className = 'conn-status';
    if (state === 'live') {
      el.classList.add('conn-live');
      if (label) label.textContent = 'live';
      el.title = 'WebSocket connected \u2014 updates every 5s';
    } else if (state === 'polling') {
      el.classList.add('conn-poll');
      if (label) label.textContent = 'polling (10s)';
      el.title = 'WebSocket down \u2014 polling every 10s';
    } else {
      el.classList.add('conn-off');
      if (label) label.textContent = 'disconnected';
      el.title = 'No connection to dashboard server';
    }
  }

  var _offgridActive = false;
  var _lastOnlineState = null;

  function updateOffgridState(enabled) {
    _offgridActive = !!enabled;
    var toggle = document.getElementById('offgrid-toggle');
    var sw = document.getElementById('offgrid-switch');
    if (toggle) {
      if (_offgridActive) toggle.classList.add('active');
      else toggle.classList.remove('active');
    }
    if (sw) sw.checked = _offgridActive;
    var banner = document.getElementById('internet-status-banner');
    if (banner) {
      if (_offgridActive) {
        banner.style.display = 'block';
        banner.textContent = 'Off Grid Mode Active — internet disabled';
        banner.classList.add('offgrid-active');
      } else {
        banner.classList.remove('offgrid-active');
        banner.textContent = 'Internet Unavailable — some features are limited';
        if (_lastOnlineState === true) {
          banner.style.display = 'none';
        }
      }
    }
  }

  function initOffgridToggle() {
    var sw = document.getElementById('offgrid-switch');
    if (!sw) return;
    sw.addEventListener('change', function() {
      var enabled = sw.checked;
      sw.disabled = true;
      var reenableTimer = setTimeout(function() { sw.disabled = false; }, 5000);
      function onResponse() {
        clearTimeout(reenableTimer);
        sw.disabled = false;
      }
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({action: 'set_offgrid_mode', enabled: enabled}));
      } else {
        api('/api/offgrid', {method: 'POST', body: {enabled: enabled}}).then(onResponse);
      }
    });
  }

  function updateInternetStatus(info) {
    var online, wanIp, lanIp, forceOffline;
    if (typeof info === 'boolean') {
      online = info;
      wanIp = null;
      lanIp = null;
      forceOffline = false;
    } else if (info && typeof info === 'object') {
      online = info.online;
      wanIp = info.wan_ip || null;
      lanIp = info.lan_ip || null;
      forceOffline = !!info.force_offline;
    } else {
      return;
    }

    _lastOnlineState = online;
    updateOffgridState(forceOffline);

    var banner = document.getElementById('internet-status-banner');
    if (banner && !forceOffline) {
      banner.style.display = online ? 'none' : 'block';
    }

    var badge = $('inet-status');
    if (badge) {
      badge.className = 'badge badge-inet ' + (online ? 'inet-online' : 'inet-offline');
      var lbl = badge.querySelector('.inet-label');
      if (lbl) lbl.textContent = online ? 'online' : 'offline';
      badge.title = 'Internet: ' + (online ? 'online' : 'offline');
    }

    var wanEl = $('wan-ip');
    if (wanEl) wanEl.textContent = wanIp || '';
    var lanEl = $('lan-ip');
    if (lanEl) lanEl.textContent = lanIp || '';
  }

  // --- Config ---

  function fetchConfig() {
    api('/api/config').then(function(r) {
      if (!r || !r.ok) return;
      $('config-content').textContent = JSON.stringify(r.data, null, 2);
    });
  }

  // --- Uptime counter ---

  function startUptimeCounter() {
    if (uptimeTimer) clearInterval(uptimeTimer);
    uptimeTimer = setInterval(function() {
      var elapsed = Date.now() / 1000 - uptimeStart;
      $('uptime').textContent = 'uptime: ' + formatUptime(elapsed);
    }, 1000);
    // Immediate update
    var elapsed = Date.now() / 1000 - uptimeStart;
    $('uptime').textContent = 'uptime: ' + formatUptime(elapsed);
  }

  // --- Data fetching ---

  var _nodeLoaded = false;
  function fetchNode() {
    if (!$('node-name')) return;
    apiRetry('/api/node').then(function(r) {
      if (!r || !r.ok) return;
      _nodeLoaded = true;
      var d = r.data;
      $('node-name').textContent = d.node_name || 'ReticulumPi';
      $('version').textContent = 'v' + (d.version || '?');
      $('identity-hash').textContent = d.identity_hash || '';
      uptimeStart = Date.now() / 1000 - (d.uptime || 0);
      startUptimeCounter();
    });
  }

  // ── Tiered fetch: Critical (above-fold), Secondary (WS-covered
  //    fallback), WsUncovered (always needed), Deferred (on expand) ──

  function fetchCritical() {
    apiRetry('/api/metrics').then(function(r) {
      if (r && r.ok) { _lastHttpUpdate = Date.now() / 1000; updateMetrics(r.data); }
    });

    var _ifaceResult = null, _loraResult = null;
    function mergeIfaceLora() {
      if (_ifaceResult === null || _loraResult === null) return;
      if (RPI.updateLoraRadio) RPI.updateLoraRadio(_ifaceResult, _loraResult);
    }
    apiRetry('/api/interfaces').then(function(r) {
      if (!r || !r.ok) return;
      updateInterfaces(r.data.interfaces);
      if (RPI.updateLoraSignal) RPI.updateLoraSignal(r.data.interfaces);
      _ifaceResult = r.data.interfaces;
      mergeIfaceLora();
    });
    apiRetry('/api/lora').then(function(r) {
      _loraResult = (r && r.ok) ? r.data : {};
      mergeIfaceLora();
    });

    apiRetry('/api/plugins').then(function(r) {
      if (r && r.ok) updatePlugins(r.data.plugins, r.data.failed_plugins);
    });

    // Map + visible-section data — fetch immediately alongside metrics
    var _mshStatus = null, _mshNodes = null;
    function mergeMeshtastic() {
      if (_mshStatus === null || _mshNodes === null) return;
      if (RPI.updateMeshtastic) RPI.updateMeshtastic(_mshStatus, _mshNodes);
      if (RPI.updateMap) RPI.updateMap(_mshNodes);
    }
    apiRetry('/api/meshtastic/status').then(function(r) {
      _mshStatus = (r && r.ok) ? r.data : {};
      mergeMeshtastic();
    });
    apiRetry('/api/meshtastic/nodes').then(function(r) {
      _mshNodes = (r && r.ok) ? r.data.nodes : [];
      mergeMeshtastic();
    });
    apiRetry('/api/meshtastic/device').then(function(r) {
      if (r && r.ok && RPI.updateMeshtasticDevice) RPI.updateMeshtasticDevice(r.data);
    });
    apiRetry('/api/meshtastic/lora_neighbors').then(function(r) {
      if (r && r.ok) {
        if (RPI.updateLoraNeighbors) RPI.updateLoraNeighbors(r.data.neighbors);
        if (RPI.updateMapLoraNeighbors) RPI.updateMapLoraNeighbors(r.data.neighbors);
      }
    });

    var _mcStatus = null, _mcContacts = null;
    function mergeMeshCore() {
      if (_mcStatus === null || _mcContacts === null) return;
      if (RPI.updateMeshCore) RPI.updateMeshCore(_mcStatus, _mcContacts);
    }
    apiRetry('/api/meshcore/status').then(function(r) {
      _mcStatus = (r && r.ok) ? r.data : {};
      mergeMeshCore();
    });
    apiRetry('/api/meshcore/contacts').then(function(r) {
      _mcContacts = (r && r.ok) ? r.data.contacts : [];
      mergeMeshCore();
      if (RPI.updateMapMeshCore) RPI.updateMapMeshCore(_mcContacts);
    });

    apiRetry('/api/gps').then(function(r) {
      if (r && r.ok && RPI.updateGps) RPI.updateGps(r.data);
      if (r && r.ok && r.data.last_fix && RPI.updateMapGps) RPI.updateMapGps(r.data.last_fix);
    });

    apiRetry('/api/adsb').then(function(r) {
      if (r && r.ok && RPI.adsb && RPI.adsb.update) RPI.adsb.update(r.data);
    });
  }

  function fetchSecondary() {
    api('/api/mesh/telemetry').then(function(r) {
      if (!r || !r.ok) return;
      if (RPI.cacheMeshPeers) RPI.cacheMeshPeers(r.data.peers);
      if (RPI.updatePeerTelemetry) RPI.updatePeerTelemetry(r.data.peers);
      if (RPI.updateMapReticulum) RPI.updateMapReticulum(r.data.peers);
    });
    api('/api/meshcore/device').then(function(r) {
      if (r && r.ok && RPI.updateMeshCoreDevice) RPI.updateMeshCoreDevice(r.data);
    });
    api('/api/meshcore_observer/status').then(function(r) {
      if (r && r.ok && RPI.updateMeshCoreObserver) RPI.updateMeshCoreObserver(r.data);
    });
    api('/api/transport').then(function(r) {
      if (r && r.ok) updateTransport(r.data);
    });
    api('/api/connectivity').then(function(r) {
      if (r && r.ok) updateConnectivity(r.data);
    });
    api('/api/routing?per_page=0').then(function(r) {
      if (r && r.ok && RPI.updateRoutingSummary) RPI.updateRoutingSummary(r.data.summary);
    });
  }

  function fetchWsUncovered() {
    if (RPI.fetchMeshNodes) RPI.fetchMeshNodes();
    if (RPI.fetchMeshSummary) RPI.fetchMeshSummary();
    if (RPI.fetchLoraReachability) RPI.fetchLoraReachability();
  }

  // --- WebSocket ---

  function _applyUpdate(d) {
    if (d.internet !== undefined) {
      updateInternetStatus(d.internet);
    }
    if (d.metrics) updateMetrics(d.metrics);
    updateWsStats(d.ws_stats || null);
    if (d.interfaces) {
      updateInterfaces(d.interfaces);
      if (RPI.updateLoraRadio) RPI.updateLoraRadio(d.interfaces, null);
    }
    if (d.mesh) {
      if (d.mesh.peers && RPI.cacheMeshPeers) RPI.cacheMeshPeers(d.mesh.peers);
      if (d.mesh.peers && RPI.updateMapReticulum) RPI.updateMapReticulum(d.mesh.peers);
      if (RPI.updateMeshFromWS) RPI.updateMeshFromWS(d.mesh);
    }
    if (d.sensors) {
      _stash.sensors = d.sensors;
      if (isPanelVisible('sensors-body')) updateSensors(d.sensors);
    }
    if (d.emergency) {
      _stash.emergency = d.emergency;
      if (isPanelVisible('emergency-body')) updateEmergency(d.emergency);
    }
    if (d.transport) updateTransport(d.transport);
    if (d.connectivity) updateConnectivity(d.connectivity);
    if (d.routing && RPI.updateRoutingSummary) RPI.updateRoutingSummary(d.routing);
    if (d.meshtastic_device && RPI.updateMeshtasticDevice) RPI.updateMeshtasticDevice(d.meshtastic_device);
    if (d.meshtastic_nodes) {
      if (RPI.updateMeshtastic) RPI.updateMeshtastic(d.meshtastic_status || {}, d.meshtastic_nodes);
      if (RPI.updateMap) RPI.updateMap(d.meshtastic_nodes);
    }
    if (d.meshtastic_lora_neighbors) {
      if (RPI.updateLoraNeighbors) RPI.updateLoraNeighbors(d.meshtastic_lora_neighbors);
      if (RPI.updateMapLoraNeighbors) RPI.updateMapLoraNeighbors(d.meshtastic_lora_neighbors);
    }
    if (d.meshtastic_nodes || d.meshtastic_lora_neighbors || d.meshcore_contacts) {
      if (RPI.updateNodeTracker) RPI.updateNodeTracker(d.meshtastic_nodes || null, d.meshtastic_lora_neighbors || null, d.meshcore_contacts || null);
    }
    if (d.meshcore_status && RPI.updateMeshCore) RPI.updateMeshCore(d.meshcore_status, d.meshcore_contacts);
    if (d.meshcore_contacts && RPI.updateMapMeshCore) RPI.updateMapMeshCore(d.meshcore_contacts);
    if (d.meshcore_device && RPI.updateMeshCoreDevice) RPI.updateMeshCoreDevice(d.meshcore_device);
    if (d.meshcore_observer && RPI.updateMeshCoreObserver) RPI.updateMeshCoreObserver(d.meshcore_observer);
    if (d.mesh_bridge && RPI.updateMeshBridge) RPI.updateMeshBridge(d.mesh_bridge);
    if (d.messaging) {
      if (RPI.updateMessagingLxmf) RPI.updateMessagingLxmf(d.messaging);
      if (RPI.updateMqttFeed) RPI.updateMqttFeed(d.messaging);
      if (RPI.updateMessagingLora) RPI.updateMessagingLora(d.messaging);
      if (RPI.updateMessagingMeshcore) RPI.updateMessagingMeshcore(d.messaging);
    }
    if (d.space && RPI.space && RPI.space.update) RPI.space.update(d.space);
    if (d.gps && RPI.updateGps) RPI.updateGps(d.gps);
    if (d.gps && d.gps.last_fix && RPI.updateMapGps) RPI.updateMapGps(d.gps.last_fix);
    if (d.adsb && RPI.adsb && RPI.adsb.update) RPI.adsb.update(d.adsb);
    if (d.ntp && RPI.updateNtp) RPI.updateNtp(d.ntp);
    if (d.hotspot) {
      _stash.hotspot = d.hotspot;
      if (RPI.updateHotspot) RPI.updateHotspot(d.hotspot, _stash.captive_portal || null);
    }
    if (d.captive_portal) {
      _stash.captive_portal = d.captive_portal;
      if (_stash.hotspot && isPanelVisible('hotspot-body') && RPI.updateHotspot) {
        RPI.updateHotspot(_stash.hotspot, d.captive_portal);
      }
    }
    if (d.fm_receiver && RPI.updateRadio) RPI.updateRadio(d.fm_receiver);
    if (d.link_tester && RPI.updateLinkTester) RPI.updateLinkTester(d.link_tester);
    if (d.weather_alert && RPI.updateWeatherAlert) RPI.updateWeatherAlert(d.weather_alert);
    if (d.ais && RPI.updateAis) RPI.updateAis(d.ais);
    if (d.acars && RPI.updateAcars) RPI.updateAcars(d.acars);
    if (d.radiosonde && RPI.updateRadiosonde) RPI.updateRadiosonde(d.radiosonde);
    if (d.noaa_apt && RPI.updateNoaa) RPI.updateNoaa(d.noaa_apt);
  }

  function connectWS() {
    if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;

    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var url = proto + '//' + location.host + '/ws/metrics';
    try { ws = new WebSocket(url); } catch(e) { startPolling(); return; }
    RPI.ws = ws;

    ws.onopen = function() {
      reconnectDelay = 1000;
      setConnStatus('live');
      _hideStaleBanner();
      _tabHiddenSince = 0;
      stopPolling();
      // Reset traffic rate tracking so we don't compute stale deltas
      _prevTraffic = {};
      prevIfaces = {};
      if (!_nodeLoaded) fetchNode();
      if (_wsPingTimer) clearInterval(_wsPingTimer);
      _wsPingTimer = setInterval(function() {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({action: 'ping', ts: Date.now()}));
        }
      }, 5000);
    };

    ws.onmessage = function(ev) {
      var _now = Date.now();
      _wsMsgRateWindow.push(_now);
      while (_wsMsgRateWindow.length && _wsMsgRateWindow[0] < _now - 10000) _wsMsgRateWindow.shift();
      try {
        var msg = JSON.parse(ev.data);
        if (msg.type === 'pong' && msg.ts) {
          _wsLatency = Date.now() - msg.ts;
          return;
        }
        if (msg.type && msg.type.indexOf('radio_') === 0) {
          if (RPI.onRadioResponse) RPI.onRadioResponse(msg);
          return;
        }
        if (msg.type === 'message' && msg.data) {
          _lastWsUpdate = Date.now() / 1000;
          if (RPI.onMessagingEvent) RPI.onMessagingEvent(msg.data);
          if (RPI.onMqttFeedMessage) RPI.onMqttFeedMessage(msg.data);
          return;
        }
        if (msg.type === 'message_status' && msg.data) {
          _lastWsUpdate = Date.now() / 1000;
          if (RPI.onMessagingStatus) RPI.onMessagingStatus(msg.data);
          return;
        }
        if (msg.type === 'reaction' && msg.data) {
          _lastWsUpdate = Date.now() / 1000;
          if (RPI.onMessagingReaction) RPI.onMessagingReaction(msg.data);
          return;
        }
        if (msg.type === 'internet_status' && msg.data) {
          updateInternetStatus(msg.data);
          return;
        }
        if (msg.type === 'offgrid_mode_changed' && msg.data) {
          updateOffgridState(msg.data.enabled);
          return;
        }
        if (msg.type === 'offgrid_mode_set') {
          updateOffgridState(msg.enabled);
          var _sw = document.getElementById('offgrid-switch');
          if (_sw) _sw.disabled = false;
          return;
        }
        if (msg.type === 'update' && msg.data) {
          _lastWsUpdate = Date.now() / 1000;
          if (!_wsFirstTick) {
            _wsFirstTick = true;
            for (var _wi = 0; _wi < _wsReadyCallbacks.length; _wi++) _wsReadyCallbacks[_wi]();
            _wsReadyCallbacks = [];
          }
          // Batch DOM updates into the next animation frame.
          RPI._pendingUpdate = msg.data;
          if (!RPI._rafPending) {
            RPI._rafPending = true;
            requestAnimationFrame(function() {
              RPI._rafPending = false;
              var d = RPI._pendingUpdate;
              if (!d) return;
              _applyUpdate(d);
              _checkStaleness();
            });
          }
        }
      } catch(e) { /* ignore parse errors */ }
    };

    ws.onclose = function() {
      _wsFirstTick = false;
      if (_wsPingTimer) { clearInterval(_wsPingTimer); _wsPingTimer = null; }
      _wsLatency = null;
      _wsMsgRateWindow = [];
      setMetric('m-ws-latency', null, 'ms');
      setMetric('m-ws-msgrate', null, '/s');
      setMetric('m-ws-clients', null, '');
      if (RPI.onMessagingConnectionLost) RPI.onMessagingConnectionLost();
      scheduleReconnect();
    };

    ws.onerror = function() {
      // onerror is always followed by onclose -- no action needed here
    };
  }

  function scheduleReconnect() {
    startPolling();
    setTimeout(function() {
      reconnectDelay = Math.min(reconnectDelay * 2, maxReconnect);
      connectWS();
    }, reconnectDelay);
  }

  // --- Polling fallback ---

  function startPolling() {
    // Always update status so it shows "polling" even if timer already exists
    // (prevents stuck "disconnected" when WS reconnect keeps failing)
    setConnStatus('polling');
    if (pollTimer) return;
    pollTimer = setInterval(function() {
      fetchCritical();
      fetchSecondary();
      fetchWsUncovered();
    }, 10000);
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  // --- Events ---

  var _el;
  if (_el = $('stale-refresh-btn')) _el.addEventListener('click', function() {
    _manualRefresh();
  });
  if (_el = $('logout-btn')) _el.addEventListener('click', function() {
    api('/api/auth/logout', {method: 'POST'}).finally(function() {
      window.location.href = '/login.html';
    });
  });

  // Collapsible section toggles with deferred-fetch on first expand
  var _sectionFirstExpand = {};
  function registerDeferredSection(name, fn) { _sectionFirstExpand[name] = fn; }
  RPI.registerDeferredSection = registerDeferredSection;

  ['plugins', 'telemetry', 'files', 'alerts', 'sensors', 'emergency', 'mesh-bridge-section', 'hotspot', 'node-tracker'].forEach(function(name) {
    var toggle = $(name + '-toggle');
    var body = $(name + '-body');
    if (toggle && body) {
      toggle.addEventListener('click', function() {
        if (body.classList.contains('hidden')) {
          body.classList.remove('hidden');
          toggle.classList.add('open');
          if (_sectionOnExpand[name]) _sectionOnExpand[name]();
          if (_sectionFirstExpand[name]) {
            _sectionFirstExpand[name]();
            delete _sectionFirstExpand[name];
          }
        } else {
          body.classList.add('hidden');
          toggle.classList.remove('open');
        }
      });
    }
  });

  if (_el = $('config-toggle')) _el.addEventListener('click', function() {
    var content = $('config-content');
    var btn = $('config-toggle');
    if (content.classList.contains('hidden')) {
      content.classList.remove('hidden');
      btn.textContent = 'Hide';
      fetchConfig();
    } else {
      content.classList.add('hidden');
      btn.textContent = 'Show';
    }
  });

  // --- Init ---
  // Auth is handled by the server middleware (cookie-based).
  // Wire up sortable mesh table headers
  var sortHeaders = document.querySelectorAll('#mesh-section th[data-sort]');
  for (var i = 0; i < sortHeaders.length; i++) {
    (function(th) {
      th.addEventListener('click', function() {
        RPI.onMeshSort(th.getAttribute('data-sort'));
      });
    })(sortHeaders[i]);
  }

  // Routing table -- event delegation (replaces per-render listener binding)
  // Hash cell click-to-copy
  if (_el = $('routing-table-body')) _el.addEventListener('click', function(ev) {
    var cell = ev.target.closest('.hash-cell');
    if (!cell) return;
    var full = cell.getAttribute('title');
    if (full && navigator.clipboard) {
      navigator.clipboard.writeText(full);
      cell.style.color = 'var(--green)';
      setTimeout(function() { cell.style.color = ''; }, 500);
    }
  });
  // Pagination button clicks
  if (_el = $('routing-pagination')) _el.addEventListener('click', function(ev) {
    var btn = ev.target.closest('button[data-rt-page]');
    if (!btn || btn.disabled) return;
    var pg = parseInt(btn.getAttribute('data-rt-page'));
    if (pg && pg !== RPI._rtPage()) {
      RPI._setRtPage(pg);
      RPI.fetchRoutingTable();
    }
  });

  // Routing table toggle
  if (_el = $('routing-table-toggle')) _el.addEventListener('click', function() {
    var wrapper = $('routing-table-wrapper');
    var btn = $('routing-table-toggle');
    if (RPI._rtTableOpen()) {
      wrapper.classList.add('hidden');
      btn.textContent = 'Show Path Table';
      RPI._setRtTableOpen(false);
      var autoRef = RPI._rtAutoRefresh();
      if (autoRef) { clearInterval(autoRef); RPI._setRtAutoRefresh(null); }
    } else {
      wrapper.classList.remove('hidden');
      btn.textContent = 'Hide Path Table';
      RPI._setRtTableOpen(true);
      RPI._setRtPage(1);
      RPI.fetchRoutingTable();
      RPI._setRtAutoRefresh(setInterval(function() { RPI.fetchRoutingTable(); }, 15000));
    }
  });

  // Routing table sort headers
  var rtSortHeaders = document.querySelectorAll('#routing-section th[data-rt-sort]');
  for (var si = 0; si < rtSortHeaders.length; si++) {
    (function(th) {
      th.addEventListener('click', function() {
        var key = th.getAttribute('data-rt-sort');
        if (RPI._rtSort() === key) {
          RPI._setRtOrder(RPI._rtOrder() === 'asc' ? 'desc' : 'asc');
        } else {
          RPI._setRtSort(key);
          RPI._setRtOrder((key === 'hops') ? 'asc' : 'desc');
        }
        RPI._setRtPage(1);
        RPI.fetchRoutingTable();
      });
    })(rtSortHeaders[si]);
  }

  // Routing table filters (debounced)
  if (_el = $('rt-search')) _el.addEventListener('input', function() {
    var timer = RPI._rtDebounceTimer();
    if (timer) clearTimeout(timer);
    var val = this.value;
    RPI._setRtDebounceTimer(setTimeout(function() {
      RPI._setRtSearch(val);
      RPI._setRtPage(1);
      RPI.fetchRoutingTable();
    }, 300));
  });

  if (_el = $('rt-iface-filter')) _el.addEventListener('change', function() {
    RPI._setRtIfaceFilter(this.value);
    RPI._setRtPage(1);
    RPI.fetchRoutingTable();
  });

  if (_el = $('rt-hops-filter')) _el.addEventListener('change', function() {
    RPI._setRtHopsFilter(this.value);
    RPI._setRtPage(1);
    RPI.fetchRoutingTable();
  });

  // Mesh filter tabs -- event delegation
  if (_el = $('mesh-filter-bar')) _el.addEventListener('click', function(ev) {
    var tab = ev.target.closest('[data-mesh-view]');
    if (!tab) return;
    var view = tab.getAttribute('data-mesh-view');
    // Update active state
    var tabs = document.querySelectorAll('.mesh-tab');
    for (var i = 0; i < tabs.length; i++) tabs[i].classList.remove('active');
    tab.classList.add('active');
    // Cancel pending search if switching views
    var timer = RPI._meshSearchTimer();
    if (timer) { clearTimeout(timer); RPI._setMeshSearchTimer(null); }
    RPI._setMeshView(view);
    RPI._setMeshPage(1);
    RPI.fetchMeshNodes();
  });

  // Mesh search -- debounced input
  if (_el = $('mesh-search')) _el.addEventListener('input', function() {
    var input = this;
    var timer = RPI._meshSearchTimer();
    if (timer) clearTimeout(timer);
    RPI._setMeshSearchTimer(setTimeout(function() {
      RPI._setMeshSearch(input.value.trim());
      RPI._setMeshPage(1);
      RPI.fetchMeshNodes();
    }, 300));
  });

  // Mesh pagination -- event delegation for page buttons
  if (_el = $('mesh-show-more')) _el.addEventListener('click', function(ev) {
    var btn = ev.target.closest('[data-mesh-page]');
    if (!btn) return;
    var pg = parseInt(btn.getAttribute('data-mesh-page'));
    if (pg && pg !== RPI._meshPage()) {
      RPI._setMeshPage(pg);
      RPI.fetchMeshNodes();
    }
  });
  // Mesh table row clicks -- event delegation
  if (_el = $('mesh-table')) _el.addEventListener('click', function(ev) {
    var row = ev.target.closest('tr[data-hash]');
    if (!row) return;
    var hash = row.getAttribute('data-hash');
    if (!hash) return;
    var nodes = RPI._meshNodes();
    var node = null;
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].destination_hash === hash) { node = nodes[i]; break; }
    }
    if (node) RPI.toggleNodeDetail(node, hash);
  });
  // LoRa table row clicks -- event delegation
  if (_el = $('lora-table')) _el.addEventListener('click', function(ev) {
    var row = ev.target.closest('tr[data-lora-hash]');
    if (!row) return;
    var hash = row.getAttribute('data-lora-hash');
    if (!hash) return;
    var curHash = RPI._loraExpandedHash();
    RPI._setLoraExpandedHash(curHash === hash ? null : hash);
    RPI.updateLoraNodes(RPI._loraNodes());
  });
  if (_el = $('peer-show-more')) _el.addEventListener('click', function() {
    var peers = Object.values(RPI._meshPeers());
    if (RPI._peerVisible() >= peers.length) {
      RPI._setPeerVisible(RPI._peerPageSize());
    } else {
      RPI._setPeerVisible(RPI._peerVisible() + RPI._peerPageSize());
    }
    RPI.updatePeerTelemetry(peers);
  });

  // Messaging hub controls wired in messages.js module

  // Wire up sortable Meshtastic MQTT table headers (exclude lora- prefixed)
  var mshSortHeaders = document.querySelectorAll('#meshtastic-section th[data-sort]:not([data-sort^="lora-"])');
  for (var mi = 0; mi < mshSortHeaders.length; mi++) {
    (function(th) {
      th.addEventListener('click', function() {
        RPI.onMeshtasticSort(th.getAttribute('data-sort'));
      });
    })(mshSortHeaders[mi]);
  }
  if (_el = $('meshtastic-show-more')) _el.addEventListener('click', function() {
    if (RPI.meshtasticShowMore) RPI.meshtasticShowMore();
  });

  // Wire up sortable LoRa neighbors table headers
  var loraSortHeaders = document.querySelectorAll('#meshtastic-lora-neighbors th[data-sort]');
  for (var li = 0; li < loraSortHeaders.length; li++) {
    (function(th) {
      th.addEventListener('click', function() {
        var key = th.getAttribute('data-sort').replace('lora-', '');
        RPI.onLoraSort(key);
      });
    })(loraSortHeaders[li]);
  }
  if (_el = $('lora-neighbors-show-more')) _el.addEventListener('click', function() {
    if (RPI.loraNeighborsShowMore) RPI.loraNeighborsShowMore();
  });

  // Wire up sortable MeshCore contacts table headers
  var mcSortHeaders = document.querySelectorAll('#meshcore-section th[data-sort]');
  for (var mci = 0; mci < mcSortHeaders.length; mci++) {
    (function(th) {
      th.addEventListener('click', function() {
        RPI.onMeshCoreSort(th.getAttribute('data-sort'));
      });
    })(mcSortHeaders[mci]);
  }
  if (_el = $('meshcore-show-more')) _el.addEventListener('click', function() {
    if (RPI.meshcoreShowMore) RPI.meshcoreShowMore();
  });

  // Wire up sortable GPS satellites table headers
  var gpsSortHeaders = document.querySelectorAll('#gps-section th[data-sort]');
  for (var gi = 0; gi < gpsSortHeaders.length; gi++) {
    (function(th) {
      th.addEventListener('click', function() {
        var key = th.getAttribute('data-sort').replace('gps-', '');
        if (RPI.onGpsSort) RPI.onGpsSort(key);
      });
    })(gpsSortHeaders[gi]);
  }

  // Interface management -- event delegation (CSP blocks inline handlers)
  if (_el = $('restart-btn')) _el.addEventListener('click', doRestart);
  if (_el = $('interfaces-table')) _el.addEventListener('change', function(ev) {
    var cb = ev.target;
    if (cb.tagName === 'INPUT' && cb.dataset.iface) {
      window._toggleIface(cb.dataset.iface);
    }
  });

  // LoRa announce mode -- event delegation (select is dynamically rendered)
  if (_el = $('lora-section')) _el.addEventListener('change', function(ev) {
    if (ev.target.id === 'lora-announce-mode') {
      window._setLoraAnnounceMode(ev.target.value);
    }
  });
  fetchInterfacesConfig();

  // Render from WS stash when a collapsed section is re-expanded
  _sectionOnExpand.sensors = function() { if (_stash.sensors) updateSensors(_stash.sensors); };
  _sectionOnExpand.emergency = function() { if (_stash.emergency) updateEmergency(_stash.emergency); };
  _sectionOnExpand.hotspot = function() {
    if (_stash.hotspot && RPI.updateHotspot) RPI.updateHotspot(_stash.hotspot, _stash.captive_portal || null);
  };

  // Register deferred fetches for collapsed sections
  registerDeferredSection('alerts', function() {
    api('/api/alerts').then(function(r) { if (r && r.ok) updateAlerts(r.data); });
  });
  registerDeferredSection('sensors', function() {
    if (_lastSensorData) return;
    api('/api/sensors').then(function(r) {
      if (!r || !r.ok) return;
      updateSensors(r.data.sensors);
      var names = Object.keys(r.data.sensors || {});
      if (names.length > 0) fetchSensorHistory(names);
    });
  });
  registerDeferredSection('emergency', function() {
    api('/api/emergency').then(function(r) { if (r && r.ok) updateEmergency(r.data); });
  });
  registerDeferredSection('files', function() {
    api('/api/files').then(function(r) { if (r && r.ok) updateSharedFiles(r.data.files); });
  });

  // If we reached this page, the cookie is valid.
  initOffgridToggle();
  fetchNode();
  connectWS();

  // WS delivers a full initial snapshot covering metrics, interfaces,
  // transport, connectivity, routing, meshtastic, meshcore, gps, adsb, etc.
  // Only fall back to HTTP if WS hasn't delivered data within 2s.
  var _criticalFallbackFired = false;
  var _criticalFallback = setTimeout(function() {
    _criticalFallbackFired = true;
    fetchCritical();
    setTimeout(fetchSecondary, 500);
  }, 2000);

  // Once WS is ready, cancel HTTP fallback and fetch only WS-uncovered data.
  var _wsUncoveredTimer = setTimeout(fetchWsUncovered, 3000);
  onWsReady(function() {
    clearTimeout(_criticalFallback);
    clearTimeout(_wsUncoveredTimer);
    if (!_criticalFallbackFired) {
      // Full plugin detail and LoRa config are not in the WS snapshot
      apiRetry('/api/plugins').then(function(r) {
        if (r && r.ok) updatePlugins(r.data.plugins, r.data.failed_plugins);
      });
      apiRetry('/api/lora').then(function(r) {
        if (r && r.ok && RPI.updateLoraRadio) RPI.updateLoraRadio(null, r.data);
      });
    }
    fetchWsUncovered();
  });

  // Periodic refresh: only poll WS-uncovered data when WS is live
  setInterval(function() {
    if (!_wsFirstTick) {
      fetchCritical();
      fetchSecondary();
    }
    fetchWsUncovered();
  }, 30000);

})();
