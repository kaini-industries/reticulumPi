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

  function updateConnectivity(data) {
    if (!data || !$('connectivity-indicators')) return;

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

  function updateTransport(data) {
    var tbody = $('transport-table');
    if (!tbody) return;
    var primaries = data.primaries || [];
    var fallbacks = data.active_fallbacks || [];
    var all = primaries.concat(fallbacks.map(function(f) { f._fallback = true; return f; }));
    if (all.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4">No TCP transport hubs</td></tr>';
      $('transport-count').textContent = '0';
      return;
    }
    var html = '';
    for (var i = 0; i < all.length; i++) {
      var h = all[i];
      var label = esc(h.name || '--');
      if (h._fallback) label += ' <small>(fallback)</small>';
      var statusCls = h.online ? 'status-ok' : (h.reconnecting ? 'status-warn' : 'status-err');
      var statusTxt = h.online ? 'Online' : (h.reconnecting ? 'Reconnecting' : 'Offline');
      var rx = h.rxb != null ? formatBytes(h.rxb) : '--';
      var tx = h.txb != null ? formatBytes(h.txb) : '--';
      html += '<tr>'
        + '<td>' + label + '</td>'
        + '<td class="addr">' + esc(h.target_host || '--') + ':' + (h.target_port || '') + '</td>'
        + '<td><span class="' + statusCls + '">' + statusTxt + '</span></td>'
        + '<td>\u2191' + tx + ' \u2193' + rx + '</td>'
        + '</tr>';
    }
    tbody.innerHTML = html;
    var online = primaries.filter(function(p) { return p.online; }).length;
    $('transport-count').textContent = online + '/' + primaries.length + ' online';
  }

  function formatBytes(b) {
    if (b == null) return '--';
    if (b < 1024) return b + ' B';
    if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
    if (b < 1073741824) return (b / 1048576).toFixed(1) + ' MB';
    return (b / 1073741824).toFixed(2) + ' GB';
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

    // Transport hub health
    api('/api/transport').then(function(r) {
      if (!r || !r.ok) return;
      updateTransport(r.data);
    });

    // Connectivity health
    api('/api/connectivity').then(function(r) {
      if (!r || !r.ok) return;
      updateConnectivity(r.data);
    });

    // Routing summary (no full path table — that's fetched on demand)
    api('/api/routing?per_page=0').then(function(r) {
      if (!r || !r.ok) return;
      updateRoutingSummary(r.data.summary);
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
          if (msg.data.transport) updateTransport(msg.data.transport);
          if (msg.data.connectivity) updateConnectivity(msg.data.connectivity);
          if (msg.data.routing) updateRoutingSummary(msg.data.routing);
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

  // --- Routing ---

  var _rtPage = 1, _rtPerPage = 100, _rtSort = 'hops', _rtOrder = 'asc';
  var _rtSearch = '', _rtIfaceFilter = '', _rtHopsFilter = '';
  var _rtTableOpen = false;
  var _rtDebounceTimer = null;
  var _rtKnownInterfaces = [];

  function formatDuration(seconds) {
    if (seconds == null || seconds <= 0) return '--';
    if (seconds < 60) return Math.floor(seconds) + 's';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm';
    if (seconds < 86400) return Math.floor(seconds / 3600) + 'h ' + Math.floor((seconds % 3600) / 60) + 'm';
    return Math.floor(seconds / 86400) + 'd ' + Math.floor((seconds % 86400) / 3600) + 'h';
  }

  function hopColorClass(hops) {
    if (hops <= 1) return 'hop-color-1';
    if (hops <= 3) return 'hop-color-2';
    if (hops <= 6) return 'hop-color-3';
    return 'hop-color-4';
  }

  function truncHash(h) {
    if (!h) return '--';
    if (h.length <= 16) return h;
    return h.substring(0, 8) + '\u2026' + h.substring(h.length - 6);
  }

  // Bucket raw hop distribution into meaningful ranges
  function bucketHops(dist) {
    var buckets = [
      {label: 'Local', min: 0, max: 0, count: 0, cls: 'hc-near'},
      {label: '1-2', min: 1, max: 2, count: 0, cls: 'hc-near'},
      {label: '3-4', min: 3, max: 4, count: 0, cls: 'hc-mid'},
      {label: '5-6', min: 5, max: 6, count: 0, cls: 'hc-far'},
      {label: '7-10', min: 7, max: 10, count: 0, cls: 'hc-vfar'},
      {label: '11+', min: 11, max: 9999, count: 0, cls: 'hc-extreme'}
    ];
    var keys = Object.keys(dist);
    for (var i = 0; i < keys.length; i++) {
      var hop = Number(keys[i]);
      var cnt = dist[keys[i]];
      for (var b = 0; b < buckets.length; b++) {
        if (hop >= buckets[b].min && hop <= buckets[b].max) {
          buckets[b].count += cnt;
          break;
        }
      }
    }
    // Filter out empty buckets
    return buckets.filter(function(b) { return b.count > 0; });
  }

  function _setStatCard(id, value, cls) {
    var el = $(id);
    if (!el) return;
    var valEl = el.querySelector('.routing-stat-value');
    if (valEl) {
      valEl.textContent = value;
      valEl.className = 'routing-stat-value' + (cls ? ' ' + cls : '');
    }
  }

  function updateRoutingSummary(data) {
    if (!data || !$('routing-section')) return;

    // Transport identity bar
    var idEl = $('routing-identity');
    if (idEl) {
      var html = '';
      if (data.transport_id) {
        html += '<span><span class="ri-label">Transport </span><span class="ri-value">' + esc(truncHash(data.transport_id)) + '</span></span>';
      }
      if (data.transport_uptime) {
        html += '<span><span class="ri-label">Uptime </span><span class="ri-value">' + formatDuration(data.transport_uptime) + '</span></span>';
      }
      if (data.probe_responder) {
        html += '<span><span class="ri-label">Probe </span><span class="ri-value">' + esc(truncHash(data.probe_responder)) + '</span></span>';
      }
      idEl.innerHTML = html || '<span class="ri-label">Transport info not available</span>';
    }

    // Stat cards
    var pc = data.path_count || 0;
    _setStatCard('ri-paths', pc, pc > 100 ? 'rs-ok' : (pc > 0 ? 'rs-info' : 'rs-warn'));
    var lc = data.link_count || 0;
    _setStatCard('ri-links', lc, lc > 0 ? 'rs-info' : '');
    var rc = data.rate_limited_count || 0;
    _setStatCard('ri-rate', rc, rc > 0 ? 'rs-warn' : 'rs-ok');
    var bc = data.blackholed_count || 0;
    _setStatCard('ri-blackhole', bc, bc > 0 ? 'rs-warn' : 'rs-ok');

    // Charts
    renderHopChart(data.hop_distribution || {}, pc);
    renderIfaceChart(data.interface_distribution || {}, pc);

    // Update interface filter dropdown
    var ifaces = Object.keys(data.interface_distribution || {});
    if (ifaces.length !== _rtKnownInterfaces.length || ifaces.join() !== _rtKnownInterfaces.join()) {
      _rtKnownInterfaces = ifaces;
      var sel = $('rt-iface-filter');
      if (sel) {
        var curVal = sel.value;
        sel.innerHTML = '<option value="">All interfaces</option>';
        for (var i = 0; i < ifaces.length; i++) {
          var opt = document.createElement('option');
          opt.value = ifaces[i];
          opt.textContent = ifaces[i];
          sel.appendChild(opt);
        }
        sel.value = curVal;
      }
    }

    // Path freshness
    var freshEl = $('routing-freshness');
    if (freshEl && data.freshness) {
      var f = data.freshness;
      var fhtml = '';
      if (f.newest_age_s != null) fhtml += '<span class="rf-item"><span class="ri-label">Newest </span><span class="rf-val">' + formatDuration(f.newest_age_s) + '</span></span>';
      if (f.oldest_age_s != null) fhtml += '<span class="rf-item"><span class="ri-label">Oldest </span><span class="rf-val">' + formatDuration(f.oldest_age_s) + '</span></span>';
      if (f.avg_age_s != null) fhtml += '<span class="rf-item"><span class="ri-label">Avg </span><span class="rf-val">' + formatDuration(f.avg_age_s) + '</span></span>';
      if (f.expiring_soon > 0) fhtml += '<span class="rf-item" style="color:var(--yellow)"><span class="ri-label">Expiring soon </span><span class="rf-val">' + f.expiring_soon + '</span></span>';
      freshEl.innerHTML = fhtml;
    }

    // Routing diagnostics
    var diagEl = $('routing-diagnostics');
    if (diagEl) {
      var diags = data.diagnostics || [];
      if (diags.length === 0) {
        diagEl.innerHTML = '';
      } else {
        var dhtml = '';
        for (var i = 0; i < diags.length; i++) {
          var isCrit = diags[i].toLowerCase().indexOf('empty') >= 0
            || diags[i].toLowerCase().indexOf('single point') >= 0;
          dhtml += '<div class="issue' + (isCrit ? ' critical' : '') + '">\u26A0 ' + esc(diags[i]) + '</div>';
        }
        diagEl.innerHTML = dhtml;
      }
    }

    // Status badge in header
    var statusEl = $('routing-status');
    if (statusEl) {
      var diags = data.diagnostics || [];
      if (diags.length === 0 && pc > 0) {
        statusEl.textContent = pc + ' paths';
        statusEl.style.color = 'var(--green)';
      } else if (diags.length > 0) {
        statusEl.textContent = diags.length + ' issue(s)';
        statusEl.style.color = 'var(--yellow)';
      } else {
        statusEl.textContent = 'no data';
        statusEl.style.color = 'var(--text-muted)';
      }
    }

    // Path table info
    var infoEl = $('routing-table-info');
    if (infoEl) {
      infoEl.textContent = pc + ' paths in routing table';
    }
  }

  function renderHopChart(dist, total) {
    var el = $('hop-chart');
    if (!el) return;

    var buckets = bucketHops(dist);
    if (buckets.length === 0) {
      el.innerHTML = '<div style="color:var(--text-muted);font-size:0.75rem">No path data</div>';
      return;
    }

    var maxVal = 0;
    for (var i = 0; i < buckets.length; i++) {
      if (buckets[i].count > maxVal) maxVal = buckets[i].count;
    }
    total = total || maxVal;

    var html = '';
    for (var i = 0; i < buckets.length; i++) {
      var b = buckets[i];
      var pct = maxVal > 0 ? (b.count / maxVal * 100) : 0;
      var pctOfTotal = total > 0 ? Math.round(b.count / total * 100) : 0;
      html += '<div class="bar-row">'
        + '<div class="bar-label bar-label-hop">' + b.label + '</div>'
        + '<div class="bar-track"><div class="bar-fill ' + b.cls + '" style="width:' + pct + '%"></div></div>'
        + '<div class="bar-count">' + b.count + '</div>'
        + '<div class="bar-pct">' + pctOfTotal + '%</div>'
        + '</div>';
    }
    el.innerHTML = html;
  }

  function renderIfaceChart(dist, total) {
    var el = $('iface-chart');
    if (!el) return;

    var keys = Object.keys(dist);
    if (keys.length === 0) {
      el.innerHTML = '<div style="color:var(--text-muted);font-size:0.75rem">No path data</div>';
      return;
    }

    // Sort by count descending
    keys.sort(function(a, b) { return dist[b] - dist[a]; });

    var maxVal = dist[keys[0]] || 1;
    total = total || maxVal;

    var html = '';
    for (var i = 0; i < keys.length; i++) {
      var iface = keys[i];
      var count = dist[iface];
      var pct = maxVal > 0 ? (count / maxVal * 100) : 0;
      var pctOfTotal = total > 0 ? Math.round(count / total * 100) : 0;
      // Shorten interface name for display
      var shortName = iface
        .replace(/TCPInterface\[TCP Client\s*/g, '')
        .replace(/TCPInterface\[TCP Server\s*/g, 'Server ')
        .replace(/LocalInterface\[.*?\]/g, 'Local')
        .replace(/I2PInterface\[.*?\]/g, 'I2P')
        .replace(/\/.*/g, '')
        .replace(/\]$/g, '');
      // Pick color class based on type
      var colorCls = 'ic-default';
      if (iface.indexOf('I2P') >= 0) colorCls = 'ic-i2p';
      else if (iface.indexOf('Local') >= 0) colorCls = 'ic-local';

      html += '<div class="bar-row">'
        + '<div class="bar-label bar-label-iface" title="' + esc(iface) + '">' + esc(shortName) + '</div>'
        + '<div class="bar-track"><div class="bar-fill ' + colorCls + '" style="width:' + pct + '%"></div></div>'
        + '<div class="bar-count">' + count + '</div>'
        + '<div class="bar-pct">' + pctOfTotal + '%</div>'
        + '</div>';
    }
    el.innerHTML = html;
  }

  function fetchRoutingTable() {
    if (!_rtTableOpen) return;

    var params = '?page=' + _rtPage + '&per_page=' + _rtPerPage
      + '&sort=' + _rtSort + '&order=' + _rtOrder;
    if (_rtSearch) params += '&search=' + encodeURIComponent(_rtSearch);
    if (_rtIfaceFilter) params += '&interface=' + encodeURIComponent(_rtIfaceFilter);
    if (_rtHopsFilter) {
      if (_rtHopsFilter === '4') {
        params += '&min_hops=4';
      } else {
        params += '&min_hops=' + _rtHopsFilter + '&max_hops=' + _rtHopsFilter;
      }
    }

    api('/api/routing' + params).then(function(r) {
      if (!r || !r.ok) return;
      renderRoutingTable(r.data);
    });
  }

  function renderRoutingTable(data) {
    var tbody = $('routing-table-body');
    if (!tbody) return;

    var paths = data.paths || [];
    if (paths.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-muted)">No paths match filters</td></tr>';
      renderRoutingPagination(0, 0);
      return;
    }

    var html = '';
    for (var i = 0; i < paths.length; i++) {
      var p = paths[i];
      var hopCls = hopColorClass(p.hops || 0);
      var expCls = '';
      if (p.expires_in_s != null && p.expires_in_s < 600) expCls = ' style="color:var(--red)"';
      else if (p.expires_in_s != null && p.expires_in_s < 1800) expCls = ' style="color:var(--yellow)"';

      html += '<tr>'
        + '<td class="hash-cell" title="' + esc(p.hash || '') + '">' + esc(truncHash(p.hash)) + '</td>'
        + '<td class="' + hopCls + '">' + (p.hops != null ? p.hops : '--') + '</td>'
        + '<td class="hash-cell" title="' + esc(p.via || '') + '">' + esc(truncHash(p.via)) + '</td>'
        + '<td>' + esc(p.interface || '--') + '</td>'
        + '<td>' + formatDuration(p.age_s) + '</td>'
        + '<td' + expCls + '>' + formatDuration(p.expires_in_s) + '</td>'
        + '</tr>';
    }
    tbody.innerHTML = html;

    // Copy hash on click
    var cells = tbody.querySelectorAll('.hash-cell');
    for (var j = 0; j < cells.length; j++) {
      cells[j].addEventListener('click', function() {
        var full = this.getAttribute('title');
        if (full && navigator.clipboard) {
          navigator.clipboard.writeText(full);
          this.style.color = 'var(--green)';
          var self = this;
          setTimeout(function() { self.style.color = ''; }, 500);
        }
      });
    }

    renderRoutingPagination(data.pages || 1, data.page || 1);
  }

  function renderRoutingPagination(totalPages, currentPage) {
    var el = $('routing-pagination');
    if (!el) return;
    if (totalPages <= 1) { el.innerHTML = ''; return; }

    var html = '';
    // First / Prev
    html += '<button' + (currentPage <= 1 ? ' disabled' : '') + ' data-rt-page="1">&laquo;</button>';
    html += '<button' + (currentPage <= 1 ? ' disabled' : '') + ' data-rt-page="' + (currentPage - 1) + '">&lsaquo;</button>';

    // Page numbers (show current context)
    var start = Math.max(1, currentPage - 2);
    var end = Math.min(totalPages, currentPage + 2);
    for (var i = start; i <= end; i++) {
      html += '<button' + (i === currentPage ? ' class="active"' : '') + ' data-rt-page="' + i + '">' + i + '</button>';
    }

    // Next / Last
    html += '<button' + (currentPage >= totalPages ? ' disabled' : '') + ' data-rt-page="' + (currentPage + 1) + '">&rsaquo;</button>';
    html += '<button' + (currentPage >= totalPages ? ' disabled' : '') + ' data-rt-page="' + totalPages + '">&raquo;</button>';

    el.innerHTML = html;

    // Wire up page buttons
    var buttons = el.querySelectorAll('button[data-rt-page]');
    for (var j = 0; j < buttons.length; j++) {
      buttons[j].addEventListener('click', function() {
        var pg = parseInt(this.getAttribute('data-rt-page'));
        if (pg && pg !== _rtPage) {
          _rtPage = pg;
          fetchRoutingTable();
        }
      });
    }
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

  // Routing table toggle
  $('routing-table-toggle').addEventListener('click', function() {
    var wrapper = $('routing-table-wrapper');
    var btn = $('routing-table-toggle');
    if (_rtTableOpen) {
      wrapper.classList.add('hidden');
      btn.textContent = 'Show Path Table';
      _rtTableOpen = false;
    } else {
      wrapper.classList.remove('hidden');
      btn.textContent = 'Hide Path Table';
      _rtTableOpen = true;
      _rtPage = 1;
      fetchRoutingTable();
    }
  });

  // Routing table sort headers
  var rtSortHeaders = document.querySelectorAll('#routing-section th[data-rt-sort]');
  for (var si = 0; si < rtSortHeaders.length; si++) {
    (function(th) {
      th.addEventListener('click', function() {
        var key = th.getAttribute('data-rt-sort');
        if (_rtSort === key) {
          _rtOrder = _rtOrder === 'asc' ? 'desc' : 'asc';
        } else {
          _rtSort = key;
          _rtOrder = (key === 'hops') ? 'asc' : 'desc';
        }
        _rtPage = 1;
        fetchRoutingTable();
      });
    })(rtSortHeaders[si]);
  }

  // Routing table filters (debounced)
  $('rt-search').addEventListener('input', function() {
    clearTimeout(_rtDebounceTimer);
    var val = this.value;
    _rtDebounceTimer = setTimeout(function() {
      _rtSearch = val;
      _rtPage = 1;
      fetchRoutingTable();
    }, 300);
  });

  $('rt-iface-filter').addEventListener('change', function() {
    _rtIfaceFilter = this.value;
    _rtPage = 1;
    fetchRoutingTable();
  });

  $('rt-hops-filter').addEventListener('change', function() {
    _rtHopsFilter = this.value;
    _rtPage = 1;
    fetchRoutingTable();
  });

  // If we reached this page, the cookie is valid.
  fetchNode();
  fetchAll();
  connectWS();

  // Refresh plugins and interfaces periodically
  setInterval(fetchAll, 30000);

})();
