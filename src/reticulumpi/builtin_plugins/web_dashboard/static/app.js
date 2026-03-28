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

  // Mesh node sorting state
  var _meshNodes = [];
  var _meshPeers = {};  // destination_hash -> telemetry data
  var _meshSortKey = 'hops';
  var _meshSortAsc = true;
  var _meshExpandedHash = null;

  function sortMeshNodes(nodes, key, asc) {
    return nodes.slice().sort(function(a, b) {
      var va, vb;
      if (key === 'hops') {
        va = a.hops != null ? a.hops : 9999;
        vb = b.hops != null ? b.hops : 9999;
      } else if (key === 'last_seen') {
        va = a.last_seen || 0;
        vb = b.last_seen || 0;
      } else if (key === 'announce_count') {
        va = a.announce_count || 0;
        vb = b.announce_count || 0;
      } else {
        return 0;
      }
      return asc ? va - vb : vb - va;
    });
  }

  function _di(label, value, cls) {
    return '<div class="node-detail-item">'
      + '<span class="node-detail-label">' + label + '</span>'
      + '<span class="node-detail-value' + (cls ? ' ' + cls : '') + '">' + value + '</span>'
      + '</div>';
  }

  function buildNodeDetailHTML(node) {
    var peer = _meshPeers[node.destination_hash];
    var firstSeen = node.first_seen ? new Date(node.first_seen * 1000).toLocaleString() : '--';
    var lastSeen = node.last_seen ? formatTimeAgo(node.last_seen) : '--';

    var h = '<div class="node-detail-section">Identity</div>'
      + '<div class="node-detail-grid">'
      + _di('Address', esc(node.destination_hash || '--'))
      + _di('Name', esc(node.app_data || '--'))
      + _di('App', esc(node.app_name || '--') + (node.aspects ? '.' + esc(node.aspects) : ''))
      + '</div>'
      + '<div class="node-detail-section">Network</div>'
      + '<div class="node-detail-grid">'
      + _di('Hops', node.hops != null ? node.hops : '--')
      + _di('First Seen', firstSeen)
      + _di('Last Seen', lastSeen)
      + _di('Announces', node.announce_count || 0)
      + '</div>';

    if (peer) {
      h += '<div class="node-detail-section">Telemetry</div>'
        + '<div class="node-detail-grid">';
      if (peer.cpu != null) h += _di('CPU', peer.cpu.toFixed(1) + '%', metricClass(peer.cpu, 70, 90));
      if (peer.temp != null) h += _di('Temperature', peer.temp.toFixed(1) + '\u00B0C', metricClass(peer.temp, 65, 80));
      if (peer.mem != null) h += _di('Memory', peer.mem.toFixed(1) + '%', metricClass(peer.mem, 70, 90));
      if (peer.disk != null) h += _di('Disk', peer.disk.toFixed(1) + '%', metricClass(peer.disk, 80, 95));
      if (peer.uptime != null) h += _di('Uptime', formatUptime(peer.uptime));
      if (peer.v) h += _di('Version', esc(peer.v));
      if (peer.plugins != null) h += _di('Plugins', peer.plugins);
      h += '</div>';
    }

    return h;
  }

  function renderMeshNodes(nodes) {
    var tbody = $('mesh-table');
    if (!tbody) return;
    if (!nodes || nodes.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6">No nodes discovered yet</td></tr>';
      $('mesh-count').textContent = '0';
      return;
    }
    // Build rows, preserving expanded state
    tbody.innerHTML = '';
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var hash = node.destination_hash || '';
      var ago = node.last_seen ? formatTimeAgo(node.last_seen) : '--';
      var isExpanded = (hash === _meshExpandedHash);

      var tr = document.createElement('tr');
      if (isExpanded) tr.className = 'node-row-active';
      tr.setAttribute('data-hash', hash);
      tr.innerHTML =
          '<td class="addr">' + esc(hash || '--') + '</td>'
        + '<td class="col-truncate" title="' + esc(node.app_data || '') + '">' + esc(node.app_data || '--') + '</td>'
        + '<td>' + esc(node.app_name || '--') + (node.aspects ? '.' + esc(node.aspects) : '') + '</td>'
        + '<td>' + (node.hops != null ? node.hops : '--') + '</td>'
        + '<td>' + ago + '</td>'
        + '<td>' + (node.announce_count || 0) + '</td>';
      tr.style.cursor = 'pointer';
      (function(n, h) {
        tr.addEventListener('click', function() { toggleNodeDetail(n, h); });
      })(node, hash);
      tbody.appendChild(tr);

      if (isExpanded) {
        var detailTr = document.createElement('tr');
        detailTr.className = 'node-detail';
        detailTr.id = 'node-detail-' + hash;
        var td = document.createElement('td');
        td.colSpan = 6;
        td.innerHTML = buildNodeDetailHTML(node);
        detailTr.appendChild(td);
        tbody.appendChild(detailTr);
      }
    }
    $('mesh-count').textContent = nodes.length + ' nodes';
    updateMeshSortIndicators();
  }

  function toggleNodeDetail(node, hash) {
    if (_meshExpandedHash === hash) {
      _meshExpandedHash = null;
    } else {
      _meshExpandedHash = hash;
    }
    var sorted = sortMeshNodes(_meshNodes, _meshSortKey, _meshSortAsc);
    renderMeshNodes(sorted);
  }

  function updateMeshNodes(nodes) {
    _meshNodes = nodes || [];
    var sorted = sortMeshNodes(_meshNodes, _meshSortKey, _meshSortAsc);
    renderMeshNodes(sorted);
  }

  function cacheMeshPeers(peers) {
    _meshPeers = {};
    if (!peers) return;
    for (var i = 0; i < peers.length; i++) {
      var p = peers[i];
      if (p.destination_hash) _meshPeers[p.destination_hash] = p;
    }
  }

  function onMeshSort(key) {
    if (_meshSortKey === key) {
      _meshSortAsc = !_meshSortAsc;
    } else {
      _meshSortKey = key;
      _meshSortAsc = (key === 'hops');  // hops default asc, others desc
    }
    var sorted = sortMeshNodes(_meshNodes, _meshSortKey, _meshSortAsc);
    renderMeshNodes(sorted);
  }

  function updateMeshSortIndicators() {
    var headers = document.querySelectorAll('#mesh-section th[data-sort]');
    for (var i = 0; i < headers.length; i++) {
      var th = headers[i];
      var arrow = th.querySelector('.sort-arrow');
      if (th.getAttribute('data-sort') === _meshSortKey) {
        arrow.textContent = _meshSortAsc ? ' \u25B2' : ' \u25BC';
      } else {
        arrow.textContent = '';
      }
    }
  }

  function updatePeerTelemetry(peers) {
    var grid = $('peer-metrics-grid');
    if (!grid) return;
    if (!peers || peers.length === 0) {
      grid.innerHTML = '<div class="config-content">No peer telemetry received yet</div>';
      $('telemetry-count').textContent = '0';
      return;
    }
    var html = '';
    for (var i = 0; i < peers.length; i++) {
      var p = peers[i];
      var name = p.name || p.destination_hash || 'Unknown';
      var hops = p.hops != null ? p.hops + ' hops' : '';
      html += '<div class="metric-card">'
        + '<div class="label">' + esc(name) + (hops ? ' <small>(' + hops + ')</small>' : '') + '</div>'
        + '<div class="peer-stats">';
      if (p.cpu != null) html += '<span class="' + metricClass(p.cpu, 70, 90) + '">CPU: ' + p.cpu.toFixed(1) + '%</span> ';
      if (p.temp != null) html += '<span class="' + metricClass(p.temp, 65, 80) + '">Temp: ' + p.temp.toFixed(1) + '\u00B0C</span> ';
      if (p.mem != null) html += '<span class="' + metricClass(p.mem, 70, 90) + '">Mem: ' + p.mem.toFixed(1) + '%</span> ';
      if (p.disk != null) html += '<span class="' + metricClass(p.disk, 80, 95) + '">Disk: ' + p.disk.toFixed(1) + '%</span>';
      if (p.uptime != null) html += ' <small>' + formatUptime(p.uptime) + '</small>';
      html += '</div></div>';
    }
    grid.innerHTML = html;
    $('telemetry-count').textContent = peers.length + ' peers';
  }

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

  function updateSharedFiles(files) {
    var tbody = $('files-table');
    if (!tbody) return;
    if (!files || files.length === 0) {
      tbody.innerHTML = '<tr><td colspan="3">No shared files</td></tr>';
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

  function updateSensors(sensors) {
    var grid = $('sensors-grid');
    if (!grid) return;
    if (!sensors || Object.keys(sensors).length === 0) {
      grid.innerHTML = '<div class="metric-card"><div class="label">No sensor data</div></div>';
      $('sensors-count').textContent = '0';
      return;
    }
    var html = '';
    var names = Object.keys(sensors);
    for (var i = 0; i < names.length; i++) {
      var name = names[i];
      var reading = sensors[name];
      html += '<div class="metric-card">'
        + '<div class="label">' + esc(name) + '</div>'
        + '<div class="peer-stats">';
      if (reading.error) {
        html += '<span class="warn">' + esc(reading.error) + '</span>';
      } else {
        var keys = Object.keys(reading);
        for (var j = 0; j < keys.length; j++) {
          var k = keys[j];
          if (k === 'timestamp') continue;
          var v = reading[k];
          if (typeof v === 'number') {
            html += '<span>' + esc(k) + ': ' + v.toFixed(2) + '</span> ';
          }
        }
      }
      html += '</div></div>';
    }
    grid.innerHTML = html;
    $('sensors-count').textContent = names.length + ' sensors';
  }

  var PRIORITY_NAMES = {0: 'INFO', 1: 'WARNING', 2: 'CRITICAL', 3: 'EMERGENCY'};
  var PRIORITY_CLASSES = {0: '', 1: 'warn', 2: 'crit', 3: 'crit'};

  function updateEmergency(data) {
    var tbody = $('emergency-table');
    if (!tbody) return;
    var messages = data.messages || [];
    if (messages.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5">No emergency broadcasts</td></tr>';
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

  function formatTimeAgo(timestamp) {
    if (!timestamp) return '--';
    var seconds = Math.floor(Date.now() / 1000 - timestamp);
    if (seconds < 0) seconds = 0;
    if (seconds < 60) return seconds + 's ago';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm ago';
    if (seconds < 86400) return Math.floor(seconds / 3600) + 'h ago';
    return Math.floor(seconds / 86400) + 'd ago';
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

    // Mesh nodes
    api('/api/mesh/nodes').then(function(r) {
      if (!r || !r.ok) return;
      updateMeshNodes(r.data.nodes);
    });

    // Peer telemetry
    api('/api/mesh/telemetry').then(function(r) {
      if (!r || !r.ok) return;
      cacheMeshPeers(r.data.peers);
      updatePeerTelemetry(r.data.peers);
    });

    // Alerts
    api('/api/alerts').then(function(r) {
      if (!r || !r.ok) return;
      updateAlerts(r.data);
    });

    // Shared files
    api('/api/files').then(function(r) {
      if (!r || !r.ok) return;
      updateSharedFiles(r.data.files);
    });

    // Sensors
    api('/api/sensors').then(function(r) {
      if (!r || !r.ok) return;
      updateSensors(r.data.sensors);
    });

    // Emergency broadcasts
    api('/api/emergency').then(function(r) {
      if (!r || !r.ok) return;
      updateEmergency(r.data);
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
          if (msg.data.mesh) {
            if (msg.data.mesh.peers) cacheMeshPeers(msg.data.mesh.peers);
            if (msg.data.mesh.nodes) updateMeshNodes(msg.data.mesh.nodes);
          }
          if (msg.data.sensors) updateSensors(msg.data.sensors);
          if (msg.data.emergency) updateEmergency(msg.data.emergency);
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
  // Wire up sortable mesh table headers
  var sortHeaders = document.querySelectorAll('#mesh-section th[data-sort]');
  for (var i = 0; i < sortHeaders.length; i++) {
    (function(th) {
      th.addEventListener('click', function() {
        onMeshSort(th.getAttribute('data-sort'));
      });
    })(sortHeaders[i]);
  }

  // If we reached this page, the cookie is valid.
  fetchNode();
  fetchAll();
  connectWS();

  // Refresh plugins and interfaces periodically
  setInterval(fetchAll, 30000);

})();
