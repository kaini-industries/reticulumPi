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

  // --- Section freshness tracking ---

  var _sectionUpdated = {};  // sectionId -> timestamp (seconds)
  var _STALE_THRESHOLD = 30; // seconds before marking stale

  function markUpdated(sectionId) {
    _sectionUpdated[sectionId] = Date.now() / 1000;
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

  /* ── Expose shared utilities for sub-modules ─────────────────────── */
  RPI.api = api;
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

  // Shared mutable object used by both mesh.js and lora.js
  RPI._reachScores = {};

  // --- Metrics ---

  var _metricHistory = { cpu: [], temp: [], mem: [], disk: [] };
  var _METRIC_HISTORY_MAX = 30;

  function pushMetricHistory(key, value) {
    if (value == null) return;
    var arr = _metricHistory[key];
    arr.push(value);
    if (arr.length > _METRIC_HISTORY_MAX) arr.shift();
  }

  function renderMetricSparkline(containerId, values) {
    var el = $(containerId);
    if (!el || !values || values.length < 2) { if (el) el.innerHTML = ''; return; }
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

  function fetchNode() {
    api('/api/node').then(function(r) {
      if (!r || !r.ok) return;
      var d = r.data;
      $('node-name').textContent = d.node_name || 'ReticulumPi';
      $('version').textContent = 'v' + (d.version || '?');
      $('identity-hash').textContent = d.identity_hash || '';
      uptimeStart = Date.now() / 1000 - (d.uptime || 0);
      startUptimeCounter();
    });
  }

  function fetchAll() {
    // ── Priority 1: Core panels (metrics, mesh, interfaces) ──────────

    api('/api/metrics').then(function(r) {
      if (r && r.ok) updateMetrics(r.data);
    });

    // Mesh nodes (server-side paginated) + summary
    if (RPI.fetchMeshNodes) RPI.fetchMeshNodes();
    if (RPI.fetchMeshSummary) RPI.fetchMeshSummary();

    // Peer telemetry
    api('/api/mesh/telemetry').then(function(r) {
      if (!r || !r.ok) return;
      if (RPI.cacheMeshPeers) RPI.cacheMeshPeers(r.data.peers);
      if (RPI.updatePeerTelemetry) RPI.updatePeerTelemetry(r.data.peers);
    });

    // Interfaces + LoRa diagnostics — fetch in parallel, merge when both arrive
    var _ifaceResult = null, _loraResult = null;
    function mergeIfaceLora() {
      if (_ifaceResult === null || _loraResult === null) return;
      if (RPI.updateLoraRadio) RPI.updateLoraRadio(_ifaceResult, _loraResult);
    }
    api('/api/interfaces').then(function(r) {
      if (!r || !r.ok) return;
      updateInterfaces(r.data.interfaces);
      if (RPI.updateLoraSignal) RPI.updateLoraSignal(r.data.interfaces);
      _ifaceResult = r.data.interfaces;
      mergeIfaceLora();
    });
    api('/api/lora').then(function(r) {
      _loraResult = (r && r.ok) ? r.data : {};
      mergeIfaceLora();
    });

    api('/api/plugins').then(function(r) {
      if (r && r.ok) updatePlugins(r.data.plugins, r.data.failed_plugins);
    });

    // ── Priority 2: Radios (Meshtastic, MeshCore) ────────────────────

    // Meshtastic — fetch status + nodes in parallel, merge when both arrive
    var _mshStatus = null, _mshNodes = null;
    function mergeMeshtastic() {
      if (_mshStatus === null || _mshNodes === null) return;
      if (RPI.updateMeshtastic) RPI.updateMeshtastic(_mshStatus, _mshNodes);
      if (RPI.updateMap) RPI.updateMap(_mshNodes);
    }
    api('/api/meshtastic/status').then(function(r) {
      _mshStatus = (r && r.ok) ? r.data : {};
      mergeMeshtastic();
    });
    api('/api/meshtastic/nodes').then(function(r) {
      _mshNodes = (r && r.ok) ? r.data.nodes : [];
      mergeMeshtastic();
    });
    api('/api/meshtastic/device').then(function(r) {
      if (r && r.ok && RPI.updateMeshtasticDevice) RPI.updateMeshtasticDevice(r.data);
    });
    api('/api/meshtastic/lora_neighbors').then(function(r) {
      if (r && r.ok) {
        if (RPI.updateLoraNeighbors) RPI.updateLoraNeighbors(r.data.neighbors);
        if (RPI.updateMapLoraNeighbors) RPI.updateMapLoraNeighbors(r.data.neighbors);
      }
    });

    // MeshCore — fetch status + contacts in parallel, merge when both arrive
    var _mcStatus = null, _mcContacts = null;
    function mergeMeshCore() {
      if (_mcStatus === null || _mcContacts === null) return;
      if (RPI.updateMeshCore) RPI.updateMeshCore(_mcStatus, _mcContacts);
    }
    api('/api/meshcore/status').then(function(r) {
      _mcStatus = (r && r.ok) ? r.data : {};
      mergeMeshCore();
    });
    api('/api/meshcore/contacts').then(function(r) {
      _mcContacts = (r && r.ok) ? r.data.contacts : [];
      mergeMeshCore();
      if (RPI.updateMapMeshCore) RPI.updateMapMeshCore(_mcContacts);
    });
    api('/api/meshcore/device').then(function(r) {
      if (r && r.ok && RPI.updateMeshCoreDevice) RPI.updateMeshCoreDevice(r.data);
    });
    api('/api/meshcore_observer/status').then(function(r) {
      if (r && r.ok && RPI.updateMeshCoreObserver) RPI.updateMeshCoreObserver(r.data);
    });

    // ── Priority 3: Secondary panels ─────────────────────────────────

    api('/api/alerts').then(function(r) {
      if (r && r.ok) updateAlerts(r.data);
    });

    api('/api/sensors').then(function(r) {
      if (!r || !r.ok) return;
      updateSensors(r.data.sensors);
      var sensorNames = Object.keys(r.data.sensors || {});
      if (sensorNames.length > 0) fetchSensorHistory(sensorNames);
    });

    api('/api/emergency').then(function(r) {
      if (r && r.ok) updateEmergency(r.data);
    });

    api('/api/files').then(function(r) {
      if (r && r.ok) updateSharedFiles(r.data.files);
    });

    // Transport + connectivity + routing
    api('/api/transport').then(function(r) {
      if (r && r.ok) updateTransport(r.data);
    });
    api('/api/connectivity').then(function(r) {
      if (r && r.ok) updateConnectivity(r.data);
    });
    api('/api/routing?per_page=0').then(function(r) {
      if (r && r.ok && RPI.updateRoutingSummary) RPI.updateRoutingSummary(r.data.summary);
    });

    // LoRa nodes panel
    if (RPI.fetchLoraReachability) RPI.fetchLoraReachability();

    // GPS telemetry
    api('/api/gps').then(function(r) {
      if (r && r.ok && RPI.updateGps) RPI.updateGps(r.data);
    });
  }

  // --- WebSocket ---

  function connectWS() {
    if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;

    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var url = proto + '//' + location.host + '/ws/metrics';
    try { ws = new WebSocket(url); } catch(e) { startPolling(); return; }

    ws.onopen = function() {
      reconnectDelay = 1000;
      setConnStatus('live');
      stopPolling();
      // Reset traffic rate tracking so we don't compute stale deltas
      _prevTraffic = {};
      prevIfaces = {};
    };

    ws.onmessage = function(ev) {
      try {
        var msg = JSON.parse(ev.data);
        if (msg.type === 'spectrum_history' && msg.data) {
          // Server pushes this once per WS connect as the initial waterfall
          // backfill.  Panels see the generation bump on their next update()
          // tick and bulk-paint from the store.
          if (RPI.spectrumCommon && RPI.spectrumCommon.historyStore) {
            RPI.spectrumCommon.historyStore.loadHistory(msg.data);
          }
          return;
        }
        if (msg.type === 'message' && msg.data) {
          if (RPI.onMessagingEvent) RPI.onMessagingEvent(msg.data);
          return;
        }
        if (msg.type === 'message_status' && msg.data) {
          if (RPI.onMessagingStatus) RPI.onMessagingStatus(msg.data);
          return;
        }
        if (msg.type === 'update' && msg.data) {
          // Maintain the shared spectrum history ring BEFORE panel updates
          // run, so both panels read a consistent snapshot.
          if (msg.data.spectrum
              && RPI.spectrumCommon && RPI.spectrumCommon.historyStore) {
            RPI.spectrumCommon.historyStore.ingestTick(msg.data.spectrum);
          }
          if (msg.data.metrics) updateMetrics(msg.data.metrics);
          if (msg.data.interfaces) {
            updateInterfaces(msg.data.interfaces);
            if (RPI.updateLoraRadio) RPI.updateLoraRadio(msg.data.interfaces, null);
          }
          if (msg.data.mesh) {
            if (msg.data.mesh.peers && RPI.cacheMeshPeers) RPI.cacheMeshPeers(msg.data.mesh.peers);
            if (RPI.updateMeshFromWS) RPI.updateMeshFromWS(msg.data.mesh);
          }
          if (msg.data.sensors) updateSensors(msg.data.sensors);
          if (msg.data.emergency) updateEmergency(msg.data.emergency);
          if (msg.data.transport) updateTransport(msg.data.transport);
          if (msg.data.connectivity) updateConnectivity(msg.data.connectivity);
          if (msg.data.routing && RPI.updateRoutingSummary) RPI.updateRoutingSummary(msg.data.routing);
          if (msg.data.meshtastic_device && RPI.updateMeshtasticDevice) RPI.updateMeshtasticDevice(msg.data.meshtastic_device);
          if (msg.data.meshtastic_lora_neighbors) {
            if (RPI.updateLoraNeighbors) RPI.updateLoraNeighbors(msg.data.meshtastic_lora_neighbors);
            if (RPI.updateMapLoraNeighbors) RPI.updateMapLoraNeighbors(msg.data.meshtastic_lora_neighbors);
          }
          if (msg.data.meshcore_status && RPI.updateMeshCore) RPI.updateMeshCore(msg.data.meshcore_status, msg.data.meshcore_contacts);
          if (msg.data.meshcore_contacts && RPI.updateMapMeshCore) RPI.updateMapMeshCore(msg.data.meshcore_contacts);
          if (msg.data.meshcore_device && RPI.updateMeshCoreDevice) RPI.updateMeshCoreDevice(msg.data.meshcore_device);
          if (msg.data.meshcore_observer && RPI.updateMeshCoreObserver) RPI.updateMeshCoreObserver(msg.data.meshcore_observer);
          if (msg.data.mesh_bridge && RPI.updateMeshBridge) RPI.updateMeshBridge(msg.data.mesh_bridge);
          if (msg.data.messaging) {
            if (RPI.updateMessagingLxmf) RPI.updateMessagingLxmf(msg.data.messaging);
            if (RPI.updateMessagingMqtt) RPI.updateMessagingMqtt(msg.data.messaging);
            if (RPI.updateMessagingLora) RPI.updateMessagingLora(msg.data.messaging);
            if (RPI.updateMessagingMeshcore) RPI.updateMessagingMeshcore(msg.data.messaging);
          }
          if (msg.data.space && RPI.space && RPI.space.update) RPI.space.update(msg.data.space);
          if (msg.data.spectrum && RPI.spectrum && RPI.spectrum.update) RPI.spectrum.update(msg.data.spectrum);
          if (msg.data.spectrum && RPI.loraSpectrum && RPI.loraSpectrum.update) RPI.loraSpectrum.update(msg.data);
          if (msg.data.gps && RPI.updateGps) RPI.updateGps(msg.data.gps);
          if (msg.data.adsb && RPI.adsb && RPI.adsb.update) RPI.adsb.update(msg.data.adsb);
        }
      } catch(e) { /* ignore parse errors */ }
    };

    ws.onclose = function() {
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
      fetchAll();
    }, 10000);
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  // --- Events ---

  $('logout-btn').addEventListener('click', function() {
    api('/api/auth/logout', {method: 'POST'}).finally(function() {
      window.location.href = '/login.html';
    });
  });

  // Collapsible section toggles
  ['plugins', 'telemetry', 'files', 'alerts', 'sensors', 'emergency'].forEach(function(name) {
    var toggle = $(name + '-toggle');
    var body = $(name + '-body');
    if (toggle && body) {
      toggle.addEventListener('click', function() {
        if (body.classList.contains('hidden')) {
          body.classList.remove('hidden');
          toggle.classList.add('open');
        } else {
          body.classList.add('hidden');
          toggle.classList.remove('open');
        }
      });
    }
  });

  $('config-toggle').addEventListener('click', function() {
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
  $('routing-table-body').addEventListener('click', function(ev) {
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
  $('routing-pagination').addEventListener('click', function(ev) {
    var btn = ev.target.closest('button[data-rt-page]');
    if (!btn || btn.disabled) return;
    var pg = parseInt(btn.getAttribute('data-rt-page'));
    if (pg && pg !== RPI._rtPage()) {
      RPI._setRtPage(pg);
      RPI.fetchRoutingTable();
    }
  });

  // Routing table toggle
  $('routing-table-toggle').addEventListener('click', function() {
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
  $('rt-search').addEventListener('input', function() {
    var timer = RPI._rtDebounceTimer();
    if (timer) clearTimeout(timer);
    var val = this.value;
    RPI._setRtDebounceTimer(setTimeout(function() {
      RPI._setRtSearch(val);
      RPI._setRtPage(1);
      RPI.fetchRoutingTable();
    }, 300));
  });

  $('rt-iface-filter').addEventListener('change', function() {
    RPI._setRtIfaceFilter(this.value);
    RPI._setRtPage(1);
    RPI.fetchRoutingTable();
  });

  $('rt-hops-filter').addEventListener('change', function() {
    RPI._setRtHopsFilter(this.value);
    RPI._setRtPage(1);
    RPI.fetchRoutingTable();
  });

  // Mesh filter tabs -- event delegation
  $('mesh-filter-bar').addEventListener('click', function(ev) {
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
  $('mesh-search').addEventListener('input', function() {
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
  $('mesh-show-more').addEventListener('click', function(ev) {
    var btn = ev.target.closest('[data-mesh-page]');
    if (!btn) return;
    var pg = parseInt(btn.getAttribute('data-mesh-page'));
    if (pg && pg !== RPI._meshPage()) {
      RPI._setMeshPage(pg);
      RPI.fetchMeshNodes();
    }
  });
  // Mesh table row clicks -- event delegation
  $('mesh-table').addEventListener('click', function(ev) {
    var row = ev.target.closest('tr[data-hash]');
    if (!row) return;
    var hash = row.getAttribute('data-hash');
    if (!hash) return;
    // Find the node in current data
    var nodes = RPI._meshNodes();
    var node = null;
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].destination_hash === hash) { node = nodes[i]; break; }
    }
    if (node) RPI.toggleNodeDetail(node, hash);
  });
  // LoRa table row clicks -- event delegation
  $('lora-table').addEventListener('click', function(ev) {
    var row = ev.target.closest('tr[data-lora-hash]');
    if (!row) return;
    var hash = row.getAttribute('data-lora-hash');
    if (!hash) return;
    var curHash = RPI._loraExpandedHash();
    RPI._setLoraExpandedHash(curHash === hash ? null : hash);
    RPI.updateLoraNodes(RPI._loraNodes());
  });
  $('peer-show-more').addEventListener('click', function() {
    var peers = Object.values(RPI._meshPeers());
    if (RPI._peerVisible() >= peers.length) {
      RPI._setPeerVisible(RPI._peerPageSize());  // collapse back
    } else {
      RPI._setPeerVisible(RPI._peerVisible() + RPI._peerPageSize());  // show next page
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
  $('meshtastic-show-more').addEventListener('click', function() {
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
  $('lora-neighbors-show-more').addEventListener('click', function() {
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
  $('meshcore-show-more').addEventListener('click', function() {
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
  $('restart-btn').addEventListener('click', doRestart);
  $('interfaces-table').addEventListener('change', function(ev) {
    var cb = ev.target;
    if (cb.tagName === 'INPUT' && cb.dataset.iface) {
      window._toggleIface(cb.dataset.iface);
    }
  });

  // LoRa announce mode -- event delegation (select is dynamically rendered)
  $('lora-section').addEventListener('change', function(ev) {
    if (ev.target.id === 'lora-announce-mode') {
      window._setLoraAnnounceMode(ev.target.value);
    }
  });
  fetchInterfacesConfig();

  // If we reached this page, the cookie is valid.
  fetchNode();
  fetchAll();
  connectWS();

  // Refresh plugins and interfaces periodically
  setInterval(fetchAll, 30000);

  // Refresh LoRa reachability scores every 60s
  setInterval(function() { if (RPI.fetchLoraReachability) RPI.fetchLoraReachability(); }, 60000);

})();
