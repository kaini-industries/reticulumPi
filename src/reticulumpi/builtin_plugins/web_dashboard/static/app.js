/* ReticulumPi Dashboard — vanilla JS */
(function() {
  'use strict';

  var token = sessionStorage.getItem('token') || '';
  var ws = null;
  var reconnectDelay = 1000;
  var maxReconnect = 30000;
  var pollTimer = null;
  var uptimeStart = 0;
  var uptimeTimer = null;
  var prevIfaces = {};      // {name: {rxb, txb, time}} for rate calculation

  // --- Helpers ---

  function api(path, opts) {
    opts = opts || {};
    var headers = opts.headers || {};
    headers['Accept'] = 'application/json';
    if (token) headers['Authorization'] = 'Bearer ' + token;
    if (opts.body) headers['Content-Type'] = 'application/json';
    return fetch(path, {
      method: opts.method || 'GET',
      headers: headers,
      credentials: 'same-origin',
      body: opts.body ? JSON.stringify(opts.body) : undefined
    }).then(function(r) {
      if (r.status === 401) { window.location.href = '/login.html'; return null; }
      return r.json().catch(function() { return {ok: false, error: 'Invalid response'}; });
    }).catch(function() { return null; });
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

  // --- Rendering ---

  function setMetric(id, value, unit, warnAt, critAt) {
    var el = $(id);
    if (!el) return;
    if (value == null || value === undefined) {
      el.innerHTML = '--<span class="unit">' + unit + '</span>';
      el.className = 'value';
      return;
    }
    var display = (typeof value === 'number') ? value.toFixed(1) : value;
    el.innerHTML = esc(String(display)) + '<span class="unit">' + unit + '</span>';
    el.className = 'value ' + metricClass(value, warnAt, critAt);
  }

  function updateMetrics(metrics) {
    if (!metrics) return;
    setMetric('m-cpu', metrics.cpu_percent, '%', 70, 90);
    setMetric('m-temp', metrics.cpu_temp, '\u00B0C', 65, 80);
    setMetric('m-mem', metrics.memory_percent, '%', 70, 90);
    setMetric('m-disk', metrics.disk_percent, '%', 80, 95);
  }

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
          + '<td><span class="status-dot ' + dotClass + '"></span>' + statusText + '</td>'
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
          + '<td><span class="status-dot status-failed"></span>Failed</td>'
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

  function updateInterfaces(interfaces) {
    var tbody = $('interfaces-table');
    if (!tbody) return;
    if (!interfaces || interfaces.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4">No interfaces detected</td></tr>';
      $('iface-count').textContent = '0';
      return;
    }

    var now = Date.now() / 1000;
    var html = '';
    for (var i = 0; i < interfaces.length; i++) {
      var iface = interfaces[i];
      var online = iface.online !== false;
      var dotClass = online ? 'status-active' : 'status-inactive';

      var traffic = '';
      if (iface.rxb != null || iface.txb != null) {
        // Calculate rates from previous reading
        var rxRate = null, txRate = null;
        var prev = prevIfaces[iface.name];
        if (prev) {
          var dt = now - prev.time;
          if (dt > 0.5) {
            rxRate = (iface.rxb - prev.rxb) / dt;
            txRate = (iface.txb - prev.txb) / dt;
          }
        }

        traffic = 'RX: ' + formatBytes(iface.rxb);
        if (rxRate != null) traffic += ' (' + formatRate(rxRate) + ')';
        traffic += ' / TX: ' + formatBytes(iface.txb);
        if (txRate != null) traffic += ' (' + formatRate(txRate) + ')';

        // Store for next calculation
        prevIfaces[iface.name] = {rxb: iface.rxb, txb: iface.txb, time: now};
      }

      html += '<tr>'
        + '<td>' + esc(iface.name) + '</td>'
        + '<td>' + esc(iface.type) + '</td>'
        + '<td><span class="status-dot ' + dotClass + '"></span>' + (online ? 'Online' : 'Offline') + '</td>'
        + '<td>' + traffic + '</td>'
        + '</tr>';
    }

    tbody.innerHTML = html;
    $('iface-count').textContent = interfaces.length + ' active';
  }

  function setConnStatus(state) {
    var el = $('conn-status');
    if (!el) return;
    el.className = 'conn-status';
    if (state === 'live') { el.classList.add('conn-live'); el.textContent = 'live'; }
    else if (state === 'polling') { el.classList.add('conn-poll'); el.textContent = 'polling'; }
    else { el.classList.add('conn-off'); el.textContent = 'disconnected'; }
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
    // Plugins
    api('/api/plugins').then(function(r) {
      if (!r || !r.ok) return;
      updatePlugins(r.data.plugins, r.data.failed_plugins);
    });

    // Interfaces
    api('/api/interfaces').then(function(r) {
      if (!r || !r.ok) return;
      updateInterfaces(r.data.interfaces);
    });

    // Metrics
    api('/api/metrics').then(function(r) {
      if (!r || !r.ok) return;
      updateMetrics(r.data);
    });
  }

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

  // --- WebSocket ---

  function connectWS() {
    if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;

    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var url = proto + '//' + location.host + '/ws/metrics';
    if (token) url += '?token=' + encodeURIComponent(token);

    try { ws = new WebSocket(url); } catch(e) { startPolling(); return; }

    ws.onopen = function() {
      reconnectDelay = 1000;
      setConnStatus('live');
      stopPolling();
    };

    ws.onmessage = function(ev) {
      try {
        var msg = JSON.parse(ev.data);
        if (msg.type === 'update' && msg.data) {
          if (msg.data.metrics) updateMetrics(msg.data.metrics);
          if (msg.data.interfaces) updateInterfaces(msg.data.interfaces);
        }
      } catch(e) { /* ignore parse errors */ }
    };

    ws.onclose = function() {
      setConnStatus('disconnected');
      scheduleReconnect();
    };

    ws.onerror = function() {
      setConnStatus('disconnected');
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
    if (pollTimer) return;
    setConnStatus('polling');
    pollTimer = setInterval(function() {
      api('/api/metrics').then(function(r) {
        if (r && r.ok) updateMetrics(r.data);
      });
    }, 10000);
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  // --- Events ---

  $('logout-btn').addEventListener('click', function() {
    api('/api/auth/logout', {method: 'POST'}).finally(function() {
      sessionStorage.removeItem('token');
      window.location.href = '/login.html';
    });
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
  // If we reached this page, the cookie is valid.
  fetchNode();
  fetchAll();
  connectWS();

  // Refresh plugins and interfaces periodically
  setInterval(fetchAll, 30000);

})();
