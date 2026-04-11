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
  var configIfaces = {};    // {name: {enabled, type, properties}} from config file
  var pendingRestart = false;
  var lastLiveIfaces = [];  // last live interfaces from RNS

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
    markUpdated('metrics-grid');
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

      // Toggle switch (no inline handler — CSP blocks inline scripts)
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

  // Mesh node state — server-side pagination
  var _meshNodes = [];       // current page of nodes from server
  var _meshTotal = 0;        // total nodes on server
  var _meshPage = 1;
  var _meshPerPage = 25;
  var _meshPages = 1;
  var _meshPeers = {};       // destination_hash -> telemetry data
  var _reachScores = {};     // destination_hash -> {score, label, factors}
  var _localServices = [];   // local plugin destinations (0-hop)
  var _meshSortKey = 'score';
  var _meshSortAsc = false;
  var _meshExpandedHash = null;
  var _meshVersion = 0;      // tracks WS mesh version for change detection
  var _meshSearch = '';
  var _meshView = '';        // current view preset (hubs/nearby/recent/lxmf/nomadnet/'')
  var _meshSummary = null;   // cached summary data from /api/mesh/summary
  var _meshSearchTimer = null;
  var _peerPageSize = 12;
  var _peerVisible = 12;

  // Sorting is now server-side. This map translates UI sort keys to API params.
  var _meshSortMap = {
    'score': 'score',
    'hops': 'hops',
    'last_seen': 'last_seen',
    'announce_count': 'announce_count'
  };

  function _di(label, value, cls) {
    return '<div class="node-detail-item">'
      + '<span class="node-detail-label">' + label + '</span>'
      + '<span class="node-detail-value' + (cls ? ' ' + cls : '') + '">' + value + '</span>'
      + '</div>';
  }

  function _reachBadgeHTML(score, label) {
    var cls = 'reach-' + (label || 'unlikely').toLowerCase();
    return '<span class="reach-badge ' + cls + '">' + score + '</span>'
      + ' <span class="reach-label ' + cls + '">' + esc(label) + '</span>';
  }

  function _reachFactorHTML(factors) {
    if (!factors) return '';
    var h = '<div class="reach-factors">';
    var names = { path: 'Path', freshness: 'Freshness', hops: 'Hops', announce: 'Announce', relay: 'Relay' };
    var order = ['path', 'freshness', 'hops', 'announce', 'relay'];
    for (var i = 0; i < order.length; i++) {
      var key = order[i];
      var f = factors[key];
      if (!f) continue;
      var pct = f.max > 0 ? Math.round(f.points / f.max * 100) : 0;
      h += '<div class="reach-factor">'
        + '<span class="reach-factor-name">' + (names[key] || key) + '</span>'
        + '<span class="reach-factor-bar"><span class="reach-factor-fill" style="width:' + pct + '%"></span></span>'
        + '<span class="reach-factor-val">' + f.points + '/' + f.max + '</span>'
        + '<span class="reach-factor-detail">' + esc(f.detail || '') + '</span>'
        + '</div>';
    }
    h += '</div>';
    return h;
  }

  function buildNodeDetailHTML(node) {
    var peer = _meshPeers[node.destination_hash];
    var reach = _reachScores[node.destination_hash];
    var firstSeen = node.first_seen ? new Date(node.first_seen * 1000).toLocaleString() : '--';
    var lastSeen = node.last_seen ? formatTimeAgo(node.last_seen) : '--';

    var h = '';

    // Reachability section (show first if data available)
    if (reach) {
      h += '<div class="node-detail-section">Reachability</div>'
        + '<div class="node-detail-grid">'
        + _di('Score', _reachBadgeHTML(reach.score, reach.label))
        + '</div>'
        + _reachFactorHTML(reach.factors);
    }

    h += '<div class="node-detail-section">Identity</div>'
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

  function _updateMeshCount() {
    var el = $('mesh-count');
    if (!el) return;
    if (_localServices.length) {
      el.textContent = _localServices.length + ' local'
        + (_meshTotal > 0 ? ' + ' + _meshTotal + ' remote' : '');
    } else {
      el.textContent = _meshTotal > 0 ? _meshTotal + ' nodes' : '0';
    }
  }

  function renderMeshNodes() {
    var tbody = $('mesh-table');
    if (!tbody) return;
    var nodes = _meshNodes;
    if (!nodes || nodes.length === 0) {
      tbody.innerHTML = '';
      // Still render local services even when no remote nodes exist
      for (var li = 0; li < _localServices.length; li++) {
        var ls = _localServices[li];
        var ltr = document.createElement('tr');
        ltr.className = 'local-service-row';
        ltr.innerHTML =
            '<td class="reach-col"><span class="local-badge">LOCAL</span></td>'
          + '<td class="addr">' + esc(ls.destination_hash || '--') + '</td>'
          + '<td class="col-truncate">' + esc(ls.plugin_name || '--') + '</td>'
          + '<td>' + esc(ls.app_name || '--') + (ls.aspects ? '.' + esc(ls.aspects) : '') + '</td>'
          + '<td>0</td><td>--</td><td>--</td>';
        tbody.appendChild(ltr);
      }
      if (_localServices.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7">' + (_meshTotal > 0 ? 'No nodes match filters' : 'No nodes discovered yet') + '</td></tr>';
      }
      _updateMeshCount();
      var showMore = $('mesh-show-more');
      if (showMore) showMore.style.display = 'none';
      if (_localServices.length === 0) return;
      updateMeshSortIndicators();
      return;
    }

    // Build rows using event delegation for clicks (no per-row listeners)
    tbody.innerHTML = '';

    // Pinned local service rows at the top
    for (var li = 0; li < _localServices.length; li++) {
      var ls = _localServices[li];
      var ltr = document.createElement('tr');
      ltr.className = 'local-service-row';
      ltr.innerHTML =
          '<td class="reach-col"><span class="local-badge">LOCAL</span></td>'
        + '<td class="addr">' + esc(ls.destination_hash || '--') + '</td>'
        + '<td class="col-truncate">' + esc(ls.plugin_name || '--') + '</td>'
        + '<td>' + esc(ls.app_name || '--') + (ls.aspects ? '.' + esc(ls.aspects) : '') + '</td>'
        + '<td>0</td><td>--</td><td>--</td>';
      tbody.appendChild(ltr);
    }
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var hash = node.destination_hash || '';
      var ago = node.last_seen ? formatTimeAgo(node.last_seen) : '--';
      var isExpanded = (hash === _meshExpandedHash);

      // Reachability score for this node
      var reach = _reachScores[hash];
      var reachCell = '--';
      if (reach) {
        var cls = 'reach-' + (reach.label || 'unlikely').toLowerCase();
        reachCell = '<span class="reach-badge ' + cls + '">' + reach.score + '</span>';
      }

      var tr = document.createElement('tr');
      if (isExpanded) tr.className = 'node-row-active';
      tr.setAttribute('data-hash', hash);
      tr.innerHTML =
          '<td class="reach-col">' + reachCell + '</td>'
        + '<td class="addr">' + esc(hash || '--') + '</td>'
        + '<td class="col-truncate" title="' + esc(node.app_data || '') + '">' + esc(node.app_data || '--') + '</td>'
        + '<td>' + esc(node.app_name || '--') + (node.aspects ? '.' + esc(node.aspects) : '') + '</td>'
        + '<td>' + (node.hops != null ? node.hops : '--') + '</td>'
        + '<td>' + ago + '</td>'
        + '<td>' + (node.announce_count || 0) + '</td>';
      tr.style.cursor = 'pointer';
      tbody.appendChild(tr);

      if (isExpanded) {
        var detailTr = document.createElement('tr');
        detailTr.className = 'node-detail';
        detailTr.id = 'node-detail-' + hash;
        var td = document.createElement('td');
        td.colSpan = 7;
        td.innerHTML = buildNodeDetailHTML(node);
        detailTr.appendChild(td);
        tbody.appendChild(detailTr);
      }
    }
    _updateMeshCount();
    updateMeshSortIndicators();

    // Pagination controls (replaces "show more" button)
    var showMore = $('mesh-show-more');
    if (showMore) {
      if (_meshPages > 1) {
        var pHtml = '';
        if (_meshPage > 1) pHtml += '<span class="mesh-page-btn" data-mesh-page="' + (_meshPage - 1) + '">&lsaquo; Prev</span> ';
        pHtml += 'Page ' + _meshPage + ' of ' + _meshPages;
        if (_meshPage < _meshPages) pHtml += ' <span class="mesh-page-btn" data-mesh-page="' + (_meshPage + 1) + '">Next &rsaquo;</span>';
        showMore.innerHTML = pHtml;
        showMore.style.display = '';
      } else {
        showMore.style.display = 'none';
      }
    }
  }

  function toggleNodeDetail(node, hash) {
    if (_meshExpandedHash === hash) {
      _meshExpandedHash = null;
    } else {
      _meshExpandedHash = hash;
    }
    renderMeshNodes();
  }

  function fetchMeshNodes() {
    var sortField = _meshSortMap[_meshSortKey] || 'last_seen';
    var order = _meshSortAsc ? 'asc' : 'desc';
    var params = '?page=' + _meshPage + '&per_page=' + _meshPerPage
      + '&sort=' + sortField + '&order=' + order;
    if (_meshSearch) params += '&search=' + encodeURIComponent(_meshSearch);
    if (_meshView) params += '&view=' + encodeURIComponent(_meshView);

    api('/api/mesh/nodes' + params).then(function(r) {
      if (!r || !r.ok) return;
      markUpdated('mesh-section');
      _meshNodes = r.data.nodes || [];
      _meshTotal = r.data.total || _meshNodes.length;
      _meshPage = r.data.page || 1;
      _meshPages = r.data.pages || 1;
      _localServices = r.data.local_services || [];
      // Update count immediately (lightweight, no table rebuild)
      _updateMeshCount();
      // Single render after reachability scores arrive
      fetchReachabilityForVisible();
    });
  }

  function fetchReachabilityForVisible() {
    if (_meshNodes.length === 0) { renderMeshNodes(); return; }
    var hashes = _meshNodes.map(function(n) { return n.destination_hash; }).join(',');
    api('/api/reachability?hashes=' + encodeURIComponent(hashes)).then(function(r) {
      if (!r || !r.ok) { renderMeshNodes(); return; }
      var nodes = r.data.nodes || [];
      for (var i = 0; i < nodes.length; i++) {
        var n = nodes[i];
        if (n.destination_hash) {
          _reachScores[n.destination_hash] = {
            score: n.score,
            label: n.label,
            factors: n.factors
          };
        }
      }
      // Re-sort the current page by full reachability score so the
      // displayed reach badges appear in the correct visual order.
      // The server used a proxy score (3 factors) for pagination
      // boundaries; the full score (5 factors) is the display truth.
      if (_meshSortKey === 'score' && _meshNodes.length > 1) {
        _meshNodes.sort(function(a, b) {
          var sa = (_reachScores[a.destination_hash] || {}).score || 0;
          var sb = (_reachScores[b.destination_hash] || {}).score || 0;
          return _meshSortAsc ? sa - sb : sb - sa;
        });
      }
      renderMeshNodes();
    }, function() {
      // Network error fallback — render without scores
      renderMeshNodes();
    });
  }

  function updateMeshFromWS(meshData) {
    if (!meshData) return;
    markUpdated('mesh-section');

    // Update total count from WS summary
    if (meshData.known_nodes != null) {
      _meshTotal = meshData.known_nodes;
      _updateMeshCount();
    }

    // If server reports new version (data changed), re-fetch current page
    var refetching = false;
    if (meshData.version != null && meshData.version !== _meshVersion) {
      _meshVersion = meshData.version;
      fetchMeshNodes();
      refetching = true;
    }

    // Update any visible nodes from recent_announces delta
    // Skip if we're already refetching (avoids redundant render)
    if (!refetching && meshData.recent_announces && meshData.recent_announces.length > 0) {
      var announceMap = {};
      for (var i = 0; i < meshData.recent_announces.length; i++) {
        var a = meshData.recent_announces[i];
        announceMap[a.destination_hash] = a;
      }
      // Patch matching nodes in current page
      var patched = false;
      for (var j = 0; j < _meshNodes.length; j++) {
        var update = announceMap[_meshNodes[j].destination_hash];
        if (update) {
          for (var key in update) {
            _meshNodes[j][key] = update[key];
          }
          patched = true;
        }
      }
      if (patched) renderMeshNodes();
    }

    // Live summary update from WS
    if (meshData.summary) {
      _meshSummary = meshData.summary;
      renderMeshSummary();
    }
  }

  // ── Mesh Summary ──────────────────────────────────────────────────
  function fetchMeshSummary() {
    api('/api/mesh/summary').then(function(r) {
      if (!r || !r.ok) return;
      _meshSummary = r.data;
      renderMeshSummary();
    });
  }

  function _setMeshStat(id, value, cls) {
    var el = $(id);
    if (!el) return;
    var valEl = el.querySelector('.mesh-stat-value');
    if (valEl) {
      valEl.textContent = value;
      valEl.className = 'mesh-stat-value' + (cls ? ' ' + cls : '');
    }
  }

  function renderMeshSummary() {
    if (!_meshSummary) return;
    var s = _meshSummary;

    // Total nodes
    _setMeshStat('ms-total', fmtK(s.total_nodes || 0), s.total_nodes > 0 ? 'ms-ok' : '');

    // Active in last hour
    var active = s.activity_stats ? (s.activity_stats.last_1h || 0) : 0;
    _setMeshStat('ms-active', fmtK(active), active > 10 ? 'ms-ok' : active > 0 ? 'ms-warn' : '');

    // Nearby (<=4 hops)
    var nearby = s.nearby || 0;
    _setMeshStat('ms-nearby', fmtK(nearby), nearby > 10 ? 'ms-ok' : nearby > 0 ? 'ms-info' : '');

    // New in 24h
    var newN = s.growth ? (s.growth.last_24h || 0) : 0;
    _setMeshStat('ms-new', newN > 0 ? '+' + fmtK(newN) : '0', newN > 0 ? 'ms-info' : '');

    // Charts
    renderMeshAppChart(s.app_breakdown || {});
    renderMeshHopChart(s.hop_distribution || {});
  }

  function fmtK(n) {
    if (n >= 10000) return (n / 1000).toFixed(1) + 'k';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
    return '' + n;
  }

  function renderMeshAppChart(breakdown) {
    var el = $('mesh-app-chart');
    if (!el) return;
    var keys = Object.keys(breakdown);
    if (keys.length === 0) {
      el.innerHTML = '<div style="color:var(--text-muted);font-size:0.75rem">No data</div>';
      return;
    }
    // Sort by count descending
    keys.sort(function(a, b) { return breakdown[b] - breakdown[a]; });
    var maxVal = breakdown[keys[0]] || 1;
    var total = 0;
    for (var i = 0; i < keys.length; i++) total += breakdown[keys[i]];

    var labelMap = {
      'lxmf': 'LXMF', 'nomadnetwork': 'NomadNet', 'reticulumpi': 'ReticulumPi',
      'sideband': 'Sideband', 'styrene': 'Styrene', '': 'Unclassified'
    };
    var clsMap = {
      'lxmf': 'ac-lxmf', 'nomadnetwork': 'ac-nomadnet', 'reticulumpi': 'ac-rpi',
      'sideband': 'ac-sideband', 'styrene': 'ac-styrene'
    };
    var html = '';
    for (var i = 0; i < keys.length; i++) {
      var key = keys[i];
      var count = breakdown[key];
      var pct = maxVal > 0 ? (count / maxVal * 100) : 0;
      var pctOfTotal = total > 0 ? Math.round(count / total * 100) : 0;
      var label = labelMap[key] || esc(key || 'Other');
      var colorCls = clsMap[key] || 'ac-other';
      html += '<div class="bar-row">'
        + '<div class="bar-label bar-label-app">' + label + '</div>'
        + '<div class="bar-track"><div class="bar-fill ' + colorCls + '" style="width:' + pct + '%"></div></div>'
        + '<div class="bar-count">' + fmtK(count) + '</div>'
        + '<div class="bar-pct">' + pctOfTotal + '%</div>'
        + '</div>';
    }
    el.innerHTML = html;
  }

  function renderMeshHopChart(dist) {
    var el = $('mesh-hop-chart');
    if (!el) return;
    var buckets = [
      {label: 'Local', key: '0', cls: 'hc-near'},
      {label: '1-3', key: '1-3', cls: 'hc-near'},
      {label: '4-10', key: '4-10', cls: 'hc-mid'},
      {label: '11-50', key: '11-50', cls: 'hc-far'},
      {label: '51+', key: '51+', cls: 'hc-extreme'},
    ];
    var maxVal = 0, total = 0;
    for (var i = 0; i < buckets.length; i++) {
      var c = dist[buckets[i].key] || 0;
      buckets[i].count = c;
      if (c > maxVal) maxVal = c;
      total += c;
    }
    // Filter empty buckets
    buckets = buckets.filter(function(b) { return b.count > 0; });
    if (buckets.length === 0) {
      el.innerHTML = '<div style="color:var(--text-muted);font-size:0.75rem">No data</div>';
      return;
    }
    var html = '';
    for (var i = 0; i < buckets.length; i++) {
      var b = buckets[i];
      var pct = maxVal > 0 ? (b.count / maxVal * 100) : 0;
      var pctOfTotal = total > 0 ? Math.round(b.count / total * 100) : 0;
      html += '<div class="bar-row">'
        + '<div class="bar-label bar-label-hop">' + b.label + '</div>'
        + '<div class="bar-track"><div class="bar-fill ' + b.cls + '" style="width:' + pct + '%"></div></div>'
        + '<div class="bar-count">' + fmtK(b.count) + '</div>'
        + '<div class="bar-pct">' + pctOfTotal + '%</div>'
        + '</div>';
    }
    el.innerHTML = html;
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
    _meshPage = 1;  // reset to first page on sort change
    fetchMeshNodes();
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
      var showMore = $('peer-show-more');
      if (showMore) showMore.style.display = 'none';
      return;
    }
    var total = peers.length;
    var limit = Math.min(_peerVisible, total);
    var html = '';
    for (var i = 0; i < limit; i++) {
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
    $('telemetry-count').textContent = total + ' peers';

    // Show/hide "show more" control
    var showMore = $('peer-show-more');
    if (showMore) {
      var remaining = total - limit;
      if (remaining > 0) {
        showMore.style.display = '';
        showMore.textContent = 'Show more (' + remaining + ' remaining)';
      } else if (limit > _peerPageSize) {
        showMore.style.display = '';
        showMore.textContent = 'Show less';
      } else {
        showMore.style.display = 'none';
      }
    }
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

  // ── Sensor rendering ────────────────────────────────────

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

  // --- Messaging Hub ---

  var _messages = [];
  var _msgTransports = [];
  var _msgContacts = [];
  var _msgSectionVisible = false;

  function fetchMessages() {
    var f = $('msg-transport-filter');
    var d = $('msg-direction-filter');
    var params = '?limit=100';
    if (f && f.value) params += '&transport=' + encodeURIComponent(f.value);
    if (d && d.value) params += '&direction=' + encodeURIComponent(d.value);
    api('/api/messages' + params).then(function(r) {
      if (!r || !r.ok) return;
      _messages = r.data.messages || [];
      renderMessages();
    });
  }

  function fetchTransports() {
    api('/api/messages/transports').then(function(r) {
      var section = $('messaging-section');
      if (!r || !r.ok) {
        // API unavailable — show disabled state
        if (section) {
          $('msg-count').textContent = 'unavailable';
          $('msg-count').style.color = 'var(--text-muted)';
        }
        return;
      }
      _msgTransports = r.data.transports || [];
      updateTransportDropdowns();
      if (section && _msgTransports.length > 0) {
        _msgSectionVisible = true;
        $('msg-count').textContent = '';
        $('msg-count').style.color = '';
      } else if (section) {
        $('msg-count').textContent = 'no transports';
        $('msg-count').style.color = 'var(--text-muted)';
      }
    });
  }

  function fetchContacts(transport) {
    var params = transport ? '?transport=' + encodeURIComponent(transport) : '';
    api('/api/messages/contacts' + params).then(function(r) {
      if (!r || !r.ok) return;
      _msgContacts = r.data.contacts || [];
      updateRecipientDropdown();
    });
  }

  function updateTransportDropdowns() {
    // Filter dropdown
    var filter = $('msg-transport-filter');
    if (filter) {
      var curFilter = filter.value;
      // Keep the "All" option, rebuild the rest
      var opts = '<option value="">All Transports</option>';
      for (var i = 0; i < _msgTransports.length; i++) {
        var t = _msgTransports[i];
        opts += '<option value="' + esc(t.name) + '">' + esc(t.display);
        if (!t.available) opts += ' (offline)';
        opts += '</option>';
      }
      filter.innerHTML = opts;
      filter.value = curFilter;
    }
    // Send transport dropdown
    var send = $('msg-send-transport');
    if (send) {
      var curSend = send.value;
      var sendOpts = '';
      for (var j = 0; j < _msgTransports.length; j++) {
        var s = _msgTransports[j];
        sendOpts += '<option value="' + esc(s.name) + '"';
        if (!s.available) sendOpts += ' disabled';
        sendOpts += '>' + esc(s.display);
        if (s.address) sendOpts += ' (' + esc(s.address.substring(0, 12)) + '...)';
        sendOpts += '</option>';
      }
      send.innerHTML = sendOpts;
      if (curSend) send.value = curSend;
    }
  }

  function updateRecipientDropdown() {
    var sel = $('msg-send-dest');
    if (!sel) return;
    var curVal = sel.value;
    var transport = $('msg-send-transport') ? $('msg-send-transport').value : '';
    var html = '<option value="">Select recipient...</option>';
    if (transport === 'meshtastic') {
      html += '<option value="broadcast">Broadcast (all)</option>';
    }
    for (var i = 0; i < _msgContacts.length; i++) {
      var c = _msgContacts[i];
      if (transport && c.transport !== transport) continue;
      html += '<option value="' + esc(c.id) + '">[' + esc(c.transport) + '] '
              + esc(c.name) + '</option>';
    }
    sel.innerHTML = html;
    sel.value = curVal;
  }

  function renderMessages() {
    var chat = $('msg-chat');
    if (!chat) return;

    // Count badge
    var count = $('msg-count');
    if (count) count.textContent = _messages.length > 0 ? _messages.length : '';

    if (_messages.length === 0) {
      chat.innerHTML = '';
      return;
    }

    // Messages come newest-first from API; reverse to show chronological
    var sorted = _messages.slice().reverse();
    var html = '';
    for (var i = 0; i < sorted.length; i++) {
      var m = sorted[i];
      var isSent = m.direction === 'sent';
      var cls = 'msg-bubble ' + (isSent ? 'sent' : 'received');
      var badge = '<span class="msg-transport-badge ' + esc(m.transport) + '">'
                  + esc(m.transport) + '</span>';

      var senderLabel = '';
      if (isSent) {
        senderLabel = 'You';
        if (m.to_name) senderLabel += ' → ' + esc(m.to_name);
        else if (m.to_id && m.to_id !== 'self') senderLabel += ' → ' + esc(m.to_id);
      } else {
        senderLabel = m.from_name ? esc(m.from_name) : (m.from_id ? esc(m.from_id) : '?');
      }

      var timeStr = m.timestamp ? formatTimeAgo(m.timestamp) : '';

      html += '<div class="' + cls + '">'
            + '<div class="msg-meta">' + badge + ' <span>' + senderLabel + '</span>'
            + '<span>' + timeStr + '</span></div>'
            + '<div class="msg-text">' + esc(m.text) + '</div>'
            + '</div>';
    }
    // Sticky scroll: only auto-scroll if user is already near the bottom
    var wasAtBottom = (chat.scrollTop + chat.clientHeight >= chat.scrollHeight - 40);
    chat.innerHTML = html;
    if (wasAtBottom) chat.scrollTop = chat.scrollHeight;
  }

  function sendMessage() {
    var transportEl = $('msg-send-transport');
    var destEl = $('msg-send-dest');
    var textEl = $('msg-send-text');
    var btn = $('msg-send-btn');
    if (!transportEl || !destEl || !textEl) return;

    var transport = transportEl.value;
    var dest = destEl.value;
    var text = textEl.value.trim();

    if (!transport || !text || !dest) return;

    btn.disabled = true;
    showMsgFeedback('Sending...', '');

    api('/api/messages/send', {
      method: 'POST',
      body: { transport: transport, text: text, destination: dest }
    }).then(function(r) {
      if (!r) { showMsgFeedback('Network error', 'error'); return; }
      if (!r.ok) { showMsgFeedback(r.error || 'Send failed', 'error'); return; }
      var d = r.data;
      if (!d.sent) {
        showMsgFeedback('Not sent: ' + (d.reason || 'unknown'), 'error');
        return;
      }
      textEl.value = '';
      updateMsgByteCount();
      var note = d.truncated ? 'Sent (truncated)' : 'Sent';
      showMsgFeedback(note, 'ok');
      fetchMessages();
    }).finally(function() {
      btn.disabled = false;
    });
  }

  function showMsgFeedback(text, cls) {
    var el = $('msg-feedback');
    if (!el) return;
    el.textContent = text;
    el.className = 'msg-feedback' + (cls ? ' ' + cls : '');
    if (cls === 'ok') {
      setTimeout(function() {
        if (el.textContent === text) el.textContent = '';
      }, 3000);
    }
  }

  function updateMsgByteCount() {
    var textEl = $('msg-send-text');
    var byteEl = $('msg-byte-count');
    var btn = $('msg-send-btn');
    if (!textEl || !byteEl) return;
    var text = textEl.value;
    var bytes = new TextEncoder().encode(text).length;
    var transport = $('msg-send-transport') ? $('msg-send-transport').value : '';

    // Only show byte count for Meshtastic (237 byte MTU)
    if (transport === 'meshtastic') {
      byteEl.textContent = bytes + '/237';
      byteEl.className = 'msg-byte-count' + (bytes > 237 ? ' over' : bytes > 200 ? ' near' : '');
    } else {
      byteEl.textContent = '';
      byteEl.className = 'msg-byte-count';
    }

    // Enable send when there's text and a destination
    var dest = $('msg-send-dest') ? $('msg-send-dest').value : '';
    if (btn) btn.disabled = !text.trim() || !dest;
  }

  function updateMessaging(data) {
    if (!data || !data.messages || data.messages.length === 0) return;
    // Merge new messages, avoiding duplicates by id
    var existing = {};
    for (var i = 0; i < _messages.length; i++) {
      existing[_messages[i].id] = true;
    }
    var added = false;
    for (var j = 0; j < data.messages.length; j++) {
      var m = data.messages[j];
      if (!existing[m.id]) {
        _messages.unshift(m); // Add to front (newest first)
        added = true;
      }
    }
    // Keep max 200 in memory
    if (_messages.length > 200) _messages = _messages.slice(0, 200);
    if (added) renderMessages();
    // Update transport availability
    if (data.transports) {
      _msgTransports = data.transports;
      updateTransportDropdowns();
    }
  }

  // --- Meshtastic Gateway ---

  var _mshNodes = [];
  var _mshSortKey = 'last_heard';
  var _mshSortAsc = false;
  var _mshExpandedId = null;
  var _mshPageSize = 25;
  var _mshVisible = 25;
  var _mshConnected = false;

  function sortMeshtasticNodes(nodes, key, asc) {
    return nodes.slice().sort(function(a, b) {
      // Pin "Self" gateway to the top regardless of sort
      if (a.is_self && !b.is_self) return -1;
      if (!a.is_self && b.is_self) return 1;

      var va, vb;
      if (key === 'snr') {
        va = a.snr != null ? a.snr : -999;
        vb = b.snr != null ? b.snr : -999;
      } else if (key === 'last_heard') {
        va = a.last_heard || 0;
        vb = b.last_heard || 0;
      } else {
        return 0;
      }
      return asc ? va - vb : vb - va;
    });
  }

  function buildMeshtasticDetailHTML(node) {
    var h = '<div class="node-detail-section">Identity</div>'
      + '<div class="node-detail-grid">';
    h += _di('Node ID', esc(node.id || '--'));
    if (node.long_name) h += _di('Long Name', esc(node.long_name));
    if (node.short_name) h += _di('Short Name', esc(node.short_name));
    if (node.is_self) h += _di('Type', 'This gateway');
    h += '</div>';

    h += '<div class="node-detail-section">Radio</div>'
      + '<div class="node-detail-grid">';
    h += _di('Hardware', esc(node.hw_model || '--'));
    h += _di('SNR', node.snr != null ? node.snr.toFixed(1) + ' dB' : '--');
    if (node.last_heard) {
      var d = new Date(node.last_heard * 1000);
      h += _di('Last Heard', formatTimeAgo(node.last_heard));
      h += _di('Timestamp', d.toLocaleString());
    }
    h += '</div>';

    if (node.latitude != null && node.longitude != null) {
      h += '<div class="node-detail-section">Position</div>'
        + '<div class="node-detail-grid">'
        + _di('Latitude', node.latitude.toFixed(5))
        + _di('Longitude', node.longitude.toFixed(5))
        + '</div>';
    }

    return h;
  }

  function renderMeshtasticNodes() {
    var tbody = $('meshtastic-nodes-table');
    if (!tbody) return;
    var sorted = sortMeshtasticNodes(_mshNodes, _mshSortKey, _mshSortAsc);
    var total = sorted.length;

    if (total === 0) {
      tbody.innerHTML = '<tr><td colspan="5">' + (_mshConnected ? 'No nodes discovered yet' : 'Not connected') + '</td></tr>';
      var btn = $('meshtastic-show-more');
      if (btn) btn.style.display = 'none';
      return;
    }

    var limit = Math.min(_mshVisible, total);
    tbody.innerHTML = '';
    for (var i = 0; i < limit; i++) {
      var n = sorted[i];
      var nodeId = n.id || '';
      var name = n.long_name || n.short_name || '--';
      var hw = n.hw_model || '--';
      var snr = n.snr != null ? n.snr.toFixed(1) + ' dB' : '--';
      var heard = formatTimeAgo(n.last_heard);
      var isExpanded = (nodeId === _mshExpandedId);

      var tr = document.createElement('tr');
      if (isExpanded) tr.className = 'node-row-active';
      if (n.is_self) tr.className = (tr.className ? tr.className + ' ' : '') + 'msh-self-row';
      tr.setAttribute('data-msh-id', nodeId);
      tr.innerHTML =
          '<td>' + esc(name) + (n.is_self ? ' <span class="msh-self-tag">SELF</span>' : '') + '</td>'
        + '<td class="addr">' + esc(String(nodeId)) + '</td>'
        + '<td>' + esc(hw) + '</td>'
        + '<td>' + esc(snr) + '</td>'
        + '<td>' + heard + '</td>';
      tr.style.cursor = 'pointer';
      (function(node, id) {
        tr.addEventListener('click', function() { toggleMeshtasticDetail(node, id); });
      })(n, nodeId);
      tbody.appendChild(tr);

      if (isExpanded) {
        var detailTr = document.createElement('tr');
        detailTr.className = 'node-detail';
        detailTr.id = 'msh-detail-' + nodeId;
        var td = document.createElement('td');
        td.colSpan = 5;
        td.innerHTML = buildMeshtasticDetailHTML(n);
        detailTr.appendChild(td);
        tbody.appendChild(detailTr);
      }
    }

    updateMeshtasticSortIndicators();

    // Show/hide "show more" control
    var showMore = $('meshtastic-show-more');
    if (showMore) {
      var remaining = total - limit;
      if (remaining > 0) {
        showMore.style.display = '';
        showMore.textContent = 'Show more (' + remaining + ' remaining)';
      } else if (limit > _mshPageSize) {
        showMore.style.display = '';
        showMore.textContent = 'Show less';
      } else {
        showMore.style.display = 'none';
      }
    }
  }

  function toggleMeshtasticDetail(node, id) {
    if (_mshExpandedId === id) {
      _mshExpandedId = null;
    } else {
      _mshExpandedId = id;
    }
    renderMeshtasticNodes();
  }

  function onMeshtasticSort(key) {
    if (_mshSortKey === key) {
      _mshSortAsc = !_mshSortAsc;
    } else {
      _mshSortKey = key;
      _mshSortAsc = (key === 'snr');  // SNR default asc (worst first), last_heard desc
    }
    renderMeshtasticNodes();
  }

  function updateMeshtasticSortIndicators() {
    var headers = document.querySelectorAll('#meshtastic-section th[data-sort]');
    for (var i = 0; i < headers.length; i++) {
      var th = headers[i];
      var arrow = th.querySelector('.sort-arrow');
      if (th.getAttribute('data-sort') === _mshSortKey) {
        arrow.textContent = _mshSortAsc ? ' \u25B2' : ' \u25BC';
      } else {
        arrow.textContent = '';
      }
    }
  }

  function updateMeshtastic(status, nodes) {
    var section = $('meshtastic-section');
    if (!section) return;

    // Show unavailable state if plugin is not available
    if (!status || status.available === false) {
      $('meshtastic-status').textContent = 'not installed';
      $('meshtastic-status').className = 'count';
      $('meshtastic-status').style.color = 'var(--text-muted)';
      $('meshtastic-overview').innerHTML = '';
      $('meshtastic-nodes-table').innerHTML = '<tr><td colspan="5" class="text-muted">Meshtastic gateway plugin not enabled</td></tr>';
      return;
    }

    // Status badge
    var badge = $('meshtastic-status');
    var connected = status.connected;
    _mshConnected = connected;
    if (badge) {
      badge.textContent = connected ? 'connected' : 'disconnected';
      badge.className = 'count ' + (connected ? 'status-ok' : 'status-err');
    }

    // Overview stats
    var overview = $('meshtastic-overview');
    if (overview) {
      var mode = status.mode || 'serial';
      var stats = [];
      stats.push({label: 'Mode', value: mode.toUpperCase()});

      if (mode === 'mqtt') {
        stats.push({label: 'Broker', value: status.mqtt_broker || '--'});
        stats.push({label: 'Topic', value: status.mqtt_topic || '--'});
        if (status.node_id) {
          stats.push({label: 'Node ID', value: status.node_id});
        }
      } else {
        stats.push({label: 'Port', value: status.serial_port || '--'});
      }

      stats.push({label: 'Channel', value: '' + (status.meshtastic_channel != null ? status.meshtastic_channel : '--')});
      stats.push({label: 'Mesh \u2192 LXMF', value: '' + (status.msgs_mesh_to_lxmf || 0)});
      stats.push({label: 'LXMF \u2192 Mesh', value: '' + (status.msgs_lxmf_to_mesh || 0)});

      if (status.msgs_rate_limited > 0) {
        stats.push({label: 'Rate Limited', value: '' + status.msgs_rate_limited});
      }
      if (status.rate_limit_per_min) {
        stats.push({label: 'Rate Limit', value: status.rate_limit_per_min + '/min'});
      }
      stats.push({label: 'LXMF Recipients', value: '' + (status.lxmf_recipients || 0)});
      stats.push({label: 'Reconnects', value: '' + (status.connect_count || 0)});

      var html = '';
      for (var i = 0; i < stats.length; i++) {
        html += '<div class="meshtastic-stat">'
          + '<span class="msh-label">' + esc(stats[i].label) + '</span>'
          + '<span class="msh-value">' + esc(stats[i].value) + '</span>'
          + '</div>';
      }
      overview.innerHTML = html;
    }

    // Update nodes and re-render
    _mshNodes = nodes || [];
    renderMeshtasticNodes();
  }

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
    if (tcpDisabled) countText += ' (not connected — no TCP interfaces enabled)';
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

  // (formatBytes and formatRate defined above — single definition)

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
    if (state === 'live') { el.classList.add('conn-live'); el.textContent = 'live'; el.title = 'WebSocket connected — updates every 5s'; }
    else if (state === 'polling') { el.classList.add('conn-poll'); el.textContent = 'polling (10s)'; el.title = 'WebSocket down — polling every 10s'; }
    else { el.classList.add('conn-off'); el.textContent = 'disconnected'; el.title = 'No connection to dashboard server'; }
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
      updateLoraRadio(r.data.interfaces);
      updateLoraSignal(r.data.interfaces);
    });

    // Metrics
    api('/api/metrics').then(function(r) {
      if (!r || !r.ok) return;
      updateMetrics(r.data);
    });

    // Mesh nodes (server-side paginated) + summary
    fetchMeshNodes();
    fetchMeshSummary();

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
      // Fetch history for sparklines
      var sensorNames = Object.keys(r.data.sensors || {});
      if (sensorNames.length > 0) fetchSensorHistory(sensorNames);
    });

    // Emergency broadcasts
    api('/api/emergency').then(function(r) {
      if (!r || !r.ok) return;
      updateEmergency(r.data);
    });

    // Messaging hub
    fetchTransports();
    fetchMessages();
    fetchContacts();

    // Meshtastic gateway
    api('/api/meshtastic/status').then(function(statusRes) {
      var status = (statusRes && statusRes.ok) ? statusRes.data : null;
      api('/api/meshtastic/nodes').then(function(nodesRes) {
        var nodes = (nodesRes && nodesRes.ok) ? nodesRes.data.nodes : [];
        updateMeshtastic(status, nodes);
      });
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

    // LoRa nodes panel (uses reachability with interface filter — smaller than full score)
    fetchLoraReachability();
  }

  function fetchLoraReachability() {
    // Use /api/paths which runs rnpath -t -j to get REAL interface names
    // (not LocalInterface). Filter for RNodeInterface to show LoRa peers.
    // Falls back to 0-hop filter from /api/reachability if /api/paths fails.
    api('/api/paths?interface=RNode').then(function(r) {
      if (!r || !r.ok) {
        _fetchLoraFallback();
        return;
      }
      markUpdated('lora-section');
      var paths = r.data.paths || [];
      var loraNodes = [];
      for (var i = 0; i < paths.length; i++) {
        var p = paths[i];
        loraNodes.push({
          destination_hash: '<' + (p.hash || '') + '>',
          app_name: p.app_name || '',
          aspects: p.aspects || '',
          app_data: p.app_data || '',
          hops: p.hops,
          last_seen: p.timestamp,
          first_seen: p.first_seen || null,
          announce_count: p.announce_count || 0,
          score: p.score != null ? p.score : null,
          label: p.label || '',
          factors: p.factors || null,
          interface: p['interface'] || ''
        });
      }
      updateLoraNodes(loraNodes);

      // Update count from interface summary
      var summary = r.data.by_interface || {};
      var rnodeCount = 0;
      for (var iface in summary) {
        if (iface.indexOf('RNode') !== -1) rnodeCount += summary[iface];
      }
      $('lora-count').textContent = rnodeCount;
    });
  }

  function _fetchLoraFallback() {
    api('/api/reachability?per_page=50').then(function(r) {
      if (!r || !r.ok) return;
      var nodes = r.data.nodes || [];
      var loraNodes = [];
      for (var i = 0; i < nodes.length; i++) {
        var n = nodes[i];
        if (n.destination_hash) {
          _reachScores[n.destination_hash] = {
            score: n.score, label: n.label, factors: n.factors
          };
          if (n.hops === 0) loraNodes.push(n);
        }
      }
      updateLoraNodes(loraNodes);
    });
  }

  function updateLoraRadio(interfaces) {
    var container = $('lora-radio-info');
    if (!container) return;

    // Find RNode interfaces
    var rnodes = [];
    for (var i = 0; i < interfaces.length; i++) {
      if (interfaces[i].type === 'RNodeInterface') {
        rnodes.push(interfaces[i]);
      }
    }

    if (rnodes.length === 0) {
      container.innerHTML = '';
      return;
    }

    var html = '';
    for (var r = 0; r < rnodes.length; r++) {
      var iface = rnodes[r];
      var radio = iface.radio || {};
      var online = iface.online !== false;
      var statusCls = online ? 'status-active' : 'status-failed';
      var statusText = online ? 'Online' : 'Offline';

      // Extract label from name like "RNodeInterface[RNode LoRa Interface/dev/ttyACM1]"
      var ifName = iface.name || 'RNode';
      var nameMatch = ifName.match(/\[([^\]\/]+)/);
      var label = nameMatch ? nameMatch[1] : ifName;

      html += '<div class="lora-radio-card">';

      // Header row: name + status
      html += '<div class="lora-radio-header">'
        + '<span class="lora-radio-name">' + esc(label) + '</span>'
        + '<span class="lora-radio-status">'
        + '<span class="status-dot ' + statusCls + '"></span> ' + statusText
        + '</span>'
        + '</div>';

      // Radio parameters row
      var hasRadio = radio.frequency || radio.bandwidth || radio.spreadingfactor;
      if (hasRadio) {
        html += '<div class="lora-radio-params">';
        if (radio.frequency) {
          var mhz = (radio.frequency / 1000000).toFixed(1);
          html += '<div class="lora-param">'
            + '<span class="lora-param-label">Freq</span>'
            + '<span class="lora-param-value">' + mhz + '</span>'
            + '<span class="lora-param-unit">MHz</span>'
            + '</div>';
        }
        if (radio.bandwidth) {
          var bwKhz = (radio.bandwidth / 1000).toFixed(0);
          html += '<div class="lora-param">'
            + '<span class="lora-param-label">BW</span>'
            + '<span class="lora-param-value">' + bwKhz + '</span>'
            + '<span class="lora-param-unit">kHz</span>'
            + '</div>';
        }
        if (radio.spreadingfactor) {
          html += '<div class="lora-param">'
            + '<span class="lora-param-label">SF</span>'
            + '<span class="lora-param-value">' + radio.spreadingfactor + '</span>'
            + '</div>';
        }
        if (radio.codingrate) {
          html += '<div class="lora-param">'
            + '<span class="lora-param-label">CR</span>'
            + '<span class="lora-param-value">4/' + radio.codingrate + '</span>'
            + '</div>';
        }
        if (radio.txpower != null) {
          html += '<div class="lora-param">'
            + '<span class="lora-param-label">TX</span>'
            + '<span class="lora-param-value">' + radio.txpower + '</span>'
            + '<span class="lora-param-unit">dBm</span>'
            + '</div>';
        }
        if (iface.bitrate) {
          var br = iface.bitrate >= 1000 ? (iface.bitrate / 1000).toFixed(1) + ' kbps'
                 : iface.bitrate + ' bps';
          html += '<div class="lora-param">'
            + '<span class="lora-param-label">Rate</span>'
            + '<span class="lora-param-value">' + br + '</span>'
            + '</div>';
        }
        html += '</div>';
      }

      // Runtime metrics row
      var hasMetrics = iface.airtime_short != null || iface.channel_load_short != null
                    || iface.noise_floor != null || iface.rxb > 0 || iface.txb > 0;
      if (hasMetrics) {
        html += '<div class="lora-radio-metrics">';

        // Link budget & SNR margin (computed from txpower + noise floor)
        if (radio.txpower != null && iface.noise_floor != null) {
          var linkBudget = radio.txpower - iface.noise_floor;
          var lbClass = linkBudget > 130 ? 'metric-ok' : linkBudget > 110 ? 'metric-warn' : 'metric-crit';
          html += '<div class="lora-metric">'
            + '<span class="lora-metric-label">Link Budget</span>'
            + '<span class="lora-metric-value ' + lbClass + '">' + linkBudget + ' dB</span>'
            + '</div>';
        }

        // Airtime (values from RNS are already percentages, e.g. 1.54 = 1.54%)
        if (iface.airtime_short != null) {
          var atS = iface.airtime_short.toFixed(1);
          var atL = iface.airtime_long != null ? iface.airtime_long.toFixed(1) : '--';
          var atClass = iface.airtime_short > 25 ? 'metric-crit'
                      : iface.airtime_short > 10 ? 'metric-warn' : 'metric-ok';
          // Trend indicator: short vs long
          var atTrend = '';
          if (iface.airtime_long != null && iface.airtime_long > 0) {
            var atRatio = iface.airtime_short / iface.airtime_long;
            if (atRatio > 1.3) atTrend = ' <span class="trend-up" title="Rising">\u2197</span>';
            else if (atRatio < 0.7) atTrend = ' <span class="trend-down" title="Falling">\u2198</span>';
          }
          html += '<div class="lora-metric">'
            + '<span class="lora-metric-label">Airtime' + atTrend + '</span>'
            + '<div class="lora-bar-wrap">'
            + '<span class="lora-metric-value ' + atClass + '">' + atS + '%</span>'
            + _loraBar(iface.airtime_short, 50)
            + '</div>'
            + '<span class="lora-metric-sub">long: ' + atL + '%</span>'
            + '</div>';
        }

        // Channel load (values from RNS are already percentages)
        if (iface.channel_load_short != null) {
          var clS = iface.channel_load_short.toFixed(1);
          var clL = iface.channel_load_long != null ? iface.channel_load_long.toFixed(1) : '--';
          var clClass = iface.channel_load_short > 50 ? 'metric-crit'
                      : iface.channel_load_short > 20 ? 'metric-warn' : 'metric-ok';
          var clTrend = '';
          if (iface.channel_load_long != null && iface.channel_load_long > 0) {
            var clRatio = iface.channel_load_short / iface.channel_load_long;
            if (clRatio > 1.3) clTrend = ' <span class="trend-up" title="Rising">\u2197</span>';
            else if (clRatio < 0.7) clTrend = ' <span class="trend-down" title="Falling">\u2198</span>';
          }
          html += '<div class="lora-metric">'
            + '<span class="lora-metric-label">Channel Load' + clTrend + '</span>'
            + '<div class="lora-bar-wrap">'
            + '<span class="lora-metric-value ' + clClass + '">' + clS + '%</span>'
            + _loraBar(iface.channel_load_short, 100)
            + '</div>'
            + '<span class="lora-metric-sub">long: ' + clL + '%</span>'
            + '</div>';
        }

        // Noise floor
        if (iface.noise_floor != null) {
          var nf = iface.noise_floor;
          var nfClass = nf > -90 ? 'metric-crit' : nf > -100 ? 'metric-warn' : 'metric-ok';
          html += '<div class="lora-metric">'
            + '<span class="lora-metric-label">Noise Floor</span>'
            + '<span class="lora-metric-value ' + nfClass + '">' + nf + ' dBm</span>'
            + '</div>';
        }

        // Interference (always show when available, not just non-zero)
        if (iface.interference != null) {
          var intVal = iface.interference;
          var intClass = intVal > -80 ? 'metric-crit' : intVal > -95 ? 'metric-warn' : 'metric-ok';
          html += '<div class="lora-metric">'
            + '<span class="lora-metric-label">Interference</span>'
            + '<span class="lora-metric-value ' + intClass + '">' + intVal + ' dBm</span>'
            + '</div>';
        }

        // SNR margin (noise floor vs interference — how much room above noise)
        if (iface.noise_floor != null && iface.interference != null) {
          var snrMargin = iface.interference - iface.noise_floor;
          var snrClass = snrMargin > 20 ? 'metric-crit' : snrMargin > 10 ? 'metric-warn' : 'metric-ok';
          html += '<div class="lora-metric">'
            + '<span class="lora-metric-label">SNR Margin</span>'
            + '<span class="lora-metric-value ' + snrClass + '">' + snrMargin + ' dB</span>'
            + '</div>';
        }

        // Traffic
        if (iface.txb > 0 || iface.rxb > 0) {
          html += '<div class="lora-metric">'
            + '<span class="lora-metric-label">TX / RX</span>'
            + '<span class="lora-metric-value">' + formatBytes(iface.txb || 0) + ' / ' + formatBytes(iface.rxb || 0) + '</span>'
            + '</div>';
        }

        // Announce queue
        if (iface.announce_queue != null) {
          var aqClass = iface.announce_queue > 10 ? 'metric-warn' : '';
          html += '<div class="lora-metric">'
            + '<span class="lora-metric-label">Announce Queue</span>'
            + '<span class="lora-metric-value ' + aqClass + '">' + iface.announce_queue + '</span>'
            + '</div>';
        }

        // Held announces (backpressure indicator)
        if (iface.held_announces != null && iface.held_announces > 0) {
          html += '<div class="lora-metric">'
            + '<span class="lora-metric-label">Held Announces</span>'
            + '<span class="lora-metric-value metric-warn">' + iface.held_announces + '</span>'
            + '</div>';
        }

        // Battery (show whenever available)
        if (iface.battery_state != null || iface.battery_percent != null) {
          var batPct = iface.battery_percent;
          var batState = iface.battery_state;
          // 0% with no draining state typically means external/USB power (no battery)
          var onExtPower = (batPct === 0 && batState !== 3);
          var batParts = [];
          var batClass = '';
          if (onExtPower) {
            batParts.push('External Power');
          } else {
            if (batState != null) {
              var batStates = {0: 'Unknown', 1: 'Charging', 2: 'Charged', 3: 'Draining'};
              batParts.push(batStates[batState] || 'State ' + batState);
            }
            if (batPct != null) batParts.push(batPct + '%');
            batClass = batPct != null && batPct < 20 ? 'metric-crit'
                     : batPct != null && batPct < 50 ? 'metric-warn' : '';
          }
          html += '<div class="lora-metric">'
            + '<span class="lora-metric-label">Battery</span>'
            + '<span class="lora-metric-value ' + batClass + '">' + esc(batParts.join(' \u2022 ')) + '</span>'
            + '</div>';
        }

        html += '</div>';
      }

      html += '</div>';
    }

    container.innerHTML = html;
    markUpdated('lora-section');
  }

  function _loraBar(pct, maxPct) {
    var w = Math.min(100, (pct / maxPct) * 100).toFixed(0);
    var cls = pct > (maxPct * 0.5) ? 'bar-crit' : pct > (maxPct * 0.2) ? 'bar-warn' : 'bar-ok';
    return '<span class="lora-bar"><span class="lora-bar-fill ' + cls + '" style="width:' + w + '%"></span></span>';
  }

  var _loraNodes = [];
  var _loraExpandedHash = null;
  var _loraSignal = { rssi: null, snr: null }; // from interface stats

  function updateLoraSignal(interfaces) {
    // Extract last RSSI/SNR from the RNode interface for the Signal column
    if (!interfaces) return;
    for (var i = 0; i < interfaces.length; i++) {
      var iface = interfaces[i];
      if (iface.name && iface.name.indexOf('RNode') !== -1) {
        if (iface.noise_floor != null) _loraSignal.noise = iface.noise_floor;
        if (iface.interference_last_dbm != null) _loraSignal.rssi = iface.interference_last_dbm;
        if (iface.rxb != null) _loraSignal.rxb = iface.rxb;
        break;
      }
    }
  }

  function updateLoraNodes(nodes) {
    var section = $('lora-section');
    var tbody = $('lora-table');
    var countEl = $('lora-count');
    if (!section || !tbody) return;

    _loraNodes = nodes;
    countEl.textContent = nodes.length;

    if (nodes.length === 0) {
      tbody.innerHTML = '<tr><td colspan="8">No LoRa peers discovered yet</td></tr>';
      return;
    }

    tbody.innerHTML = '';
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      var hash = n.destination_hash || '';
      var shortHash = hash.length > 14 ? hash.slice(1, 13) + '\u2026' : hash;
      var name = n.app_data || '';
      if (!name && n.app_name) {
        name = n.app_name + (n.aspects ? '.' + n.aspects : '');
      }
      name = name || '--';
      var appFull = n.app_name ? n.app_name + (n.aspects ? '.' + n.aspects : '') : '--';
      var hops = n.hops != null ? n.hops : '--';
      var ago = n.last_seen ? formatTimeAgo(n.last_seen) : '--';
      var annCount = n.announce_count || 0;
      var score = n.score != null ? n.score : 0;
      var cls = score >= 80 ? 'reach-high' : score >= 60 ? 'reach-good'
              : score >= 40 ? 'reach-fair' : score >= 20 ? 'reach-low' : 'reach-unlikely';
      var isExpanded = (hash === _loraExpandedHash);

      // Signal column — show interface RSSI if we have RX data
      var sigHtml = '--';
      if (_loraSignal.rssi != null && _loraSignal.rxb > 0) {
        var rssiCls = _loraSignal.rssi > -80 ? 'metric-ok' : _loraSignal.rssi > -100 ? 'metric-warn' : 'metric-crit';
        sigHtml = '<span class="' + rssiCls + '">' + _loraSignal.rssi + ' dBm</span>';
      }

      var tr = document.createElement('tr');
      if (isExpanded) tr.className = 'node-row-active';
      tr.setAttribute('data-lora-hash', hash);
      tr.style.cursor = 'pointer';
      tr.innerHTML =
          '<td class="reach-col"><span class="reach-badge ' + cls + '">' + score + '</span></td>'
        + '<td class="addr" title="' + esc(hash) + '">' + esc(shortHash) + '</td>'
        + '<td class="col-truncate" title="' + esc(n.app_data || '') + '">' + esc(name) + '</td>'
        + '<td>' + esc(appFull) + '</td>'
        + '<td>' + hops + '</td>'
        + '<td>' + sigHtml + '</td>'
        + '<td>' + ago + '</td>'
        + '<td>' + annCount + '</td>';
      tbody.appendChild(tr);

      // Expanded detail row
      if (isExpanded) {
        var detailTr = document.createElement('tr');
        detailTr.className = 'node-detail';
        var td = document.createElement('td');
        td.colSpan = 8;
        td.innerHTML = _buildLoraDetailHTML(n);
        detailTr.appendChild(td);
        tbody.appendChild(detailTr);
      }
    }
  }

  function _buildLoraDetailHTML(node) {
    var h = '';

    // Reachability
    if (node.score != null && node.factors) {
      h += '<div class="node-detail-section">Reachability</div>'
        + '<div class="node-detail-grid">'
        + _di('Score', _reachBadgeHTML(node.score, node.label))
        + '</div>'
        + _reachFactorHTML(node.factors);
    }

    // Identity
    h += '<div class="node-detail-section">Identity</div>'
      + '<div class="node-detail-grid">'
      + _di('Address', esc(node.destination_hash || '--'))
      + _di('Name', esc(node.app_data || '--'))
      + _di('App', esc(node.app_name || '--') + (node.aspects ? '.' + esc(node.aspects) : ''))
      + '</div>';

    // Network
    var firstSeen = node.first_seen ? new Date(node.first_seen * 1000).toLocaleString() : '--';
    var lastSeen = node.last_seen ? formatTimeAgo(node.last_seen) : '--';
    h += '<div class="node-detail-section">Network</div>'
      + '<div class="node-detail-grid">'
      + _di('Hops', node.hops != null ? node.hops : '--')
      + _di('First Seen', firstSeen)
      + _di('Last Seen', lastSeen)
      + _di('Announces', node.announce_count || 0)
      + _di('Interface', esc(node.interface || '--'))
      + '</div>';

    // Signal
    if (_loraSignal.rssi != null || _loraSignal.noise != null) {
      h += '<div class="node-detail-section">Signal</div>'
        + '<div class="node-detail-grid">';
      if (_loraSignal.rssi != null) h += _di('Last RSSI', _loraSignal.rssi + ' dBm');
      if (_loraSignal.noise != null) h += _di('Noise Floor', _loraSignal.noise + ' dBm');
      if (_loraSignal.rssi != null && _loraSignal.noise != null) {
        h += _di('SNR Margin', (_loraSignal.rssi - _loraSignal.noise) + ' dB');
      }
      h += '</div>';
    }

    return h;
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
      // Reset traffic rate tracking so we don't compute stale deltas
      _prevTraffic = {};
      prevIfaces = {};
    };

    ws.onmessage = function(ev) {
      try {
        var msg = JSON.parse(ev.data);
        if (msg.type === 'update' && msg.data) {
          if (msg.data.metrics) updateMetrics(msg.data.metrics);
          if (msg.data.interfaces) {
            updateInterfaces(msg.data.interfaces);
            updateLoraRadio(msg.data.interfaces);
          }
          if (msg.data.mesh) {
            if (msg.data.mesh.peers) cacheMeshPeers(msg.data.mesh.peers);
            updateMeshFromWS(msg.data.mesh);
          }
          if (msg.data.sensors) updateSensors(msg.data.sensors);
          if (msg.data.emergency) updateEmergency(msg.data.emergency);
          if (msg.data.transport) updateTransport(msg.data.transport);
          if (msg.data.connectivity) updateConnectivity(msg.data.connectivity);
          if (msg.data.routing) updateRoutingSummary(msg.data.routing);
          if (msg.data.messaging) updateMessaging(msg.data.messaging);
        }
      } catch(e) { /* ignore parse errors */ }
    };

    ws.onclose = function() {
      scheduleReconnect();
    };

    ws.onerror = function() {
      // onerror is always followed by onclose — no action needed here
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

  // --- Routing ---

  var _rtPage = 1, _rtPerPage = 100, _rtSort = 'hops', _rtOrder = 'asc';
  var _rtSearch = '', _rtIfaceFilter = '', _rtHopsFilter = '';
  var _rtTableOpen = false;
  var _rtDebounceTimer = null;
  var _rtKnownInterfaces = [];
  var _rtAutoRefresh = null;

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
    markUpdated('routing-section');

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
    // Hash-cell click-to-copy is handled by event delegation (see _initRoutingDelegation)

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
    // Page button clicks handled by event delegation (see _initRoutingDelegation)
  }

  // --- Events ---

  $('logout-btn').addEventListener('click', function() {
    api('/api/auth/logout', {method: 'POST'}).finally(function() {
      sessionStorage.removeItem('token');
      window.location.href = '/login.html';
    });
  });

  // Plugins section collapsible toggle
  $('plugins-toggle').addEventListener('click', function() {
    var header = $('plugins-toggle');
    var body = $('plugins-body');
    if (body.classList.contains('hidden')) {
      body.classList.remove('hidden');
      header.classList.add('open');
    } else {
      body.classList.add('hidden');
      header.classList.remove('open');
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
        onMeshSort(th.getAttribute('data-sort'));
      });
    })(sortHeaders[i]);
  }

  // Routing table — event delegation (replaces per-render listener binding)
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
    if (pg && pg !== _rtPage) {
      _rtPage = pg;
      fetchRoutingTable();
    }
  });

  // Routing table toggle
  $('routing-table-toggle').addEventListener('click', function() {
    var wrapper = $('routing-table-wrapper');
    var btn = $('routing-table-toggle');
    if (_rtTableOpen) {
      wrapper.classList.add('hidden');
      btn.textContent = 'Show Path Table';
      _rtTableOpen = false;
      if (_rtAutoRefresh) { clearInterval(_rtAutoRefresh); _rtAutoRefresh = null; }
    } else {
      wrapper.classList.remove('hidden');
      btn.textContent = 'Hide Path Table';
      _rtTableOpen = true;
      _rtPage = 1;
      fetchRoutingTable();
      _rtAutoRefresh = setInterval(fetchRoutingTable, 15000);
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

  // Mesh filter tabs — event delegation
  $('mesh-filter-bar').addEventListener('click', function(ev) {
    var tab = ev.target.closest('[data-mesh-view]');
    if (!tab) return;
    var view = tab.getAttribute('data-mesh-view');
    // Update active state
    var tabs = document.querySelectorAll('.mesh-tab');
    for (var i = 0; i < tabs.length; i++) tabs[i].classList.remove('active');
    tab.classList.add('active');
    // Cancel pending search if switching views
    if (_meshSearchTimer) { clearTimeout(_meshSearchTimer); _meshSearchTimer = null; }
    _meshView = view;
    _meshPage = 1;
    fetchMeshNodes();
  });

  // Mesh search — debounced input
  $('mesh-search').addEventListener('input', function() {
    var input = this;
    if (_meshSearchTimer) clearTimeout(_meshSearchTimer);
    _meshSearchTimer = setTimeout(function() {
      _meshSearch = input.value.trim();
      _meshPage = 1;
      fetchMeshNodes();
    }, 300);
  });

  // Mesh pagination — event delegation for page buttons
  $('mesh-show-more').addEventListener('click', function(ev) {
    var btn = ev.target.closest('[data-mesh-page]');
    if (!btn) return;
    var pg = parseInt(btn.getAttribute('data-mesh-page'));
    if (pg && pg !== _meshPage) {
      _meshPage = pg;
      fetchMeshNodes();
    }
  });
  // Mesh table row clicks — event delegation
  $('mesh-table').addEventListener('click', function(ev) {
    var row = ev.target.closest('tr[data-hash]');
    if (!row) return;
    var hash = row.getAttribute('data-hash');
    if (!hash) return;
    // Find the node in current data
    var node = null;
    for (var i = 0; i < _meshNodes.length; i++) {
      if (_meshNodes[i].destination_hash === hash) { node = _meshNodes[i]; break; }
    }
    if (node) toggleNodeDetail(node, hash);
  });
  // LoRa table row clicks — event delegation
  $('lora-table').addEventListener('click', function(ev) {
    var row = ev.target.closest('tr[data-lora-hash]');
    if (!row) return;
    var hash = row.getAttribute('data-lora-hash');
    if (!hash) return;
    _loraExpandedHash = (_loraExpandedHash === hash) ? null : hash;
    updateLoraNodes(_loraNodes);
  });
  $('peer-show-more').addEventListener('click', function() {
    var peers = Object.values(_meshPeers);
    if (_peerVisible >= peers.length) {
      _peerVisible = _peerPageSize;  // collapse back
    } else {
      _peerVisible += _peerPageSize;  // show next page
    }
    updatePeerTelemetry(peers);
  });

  // Wire up messaging hub controls
  $('msg-send-btn').addEventListener('click', sendMessage);
  $('msg-send-text').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') sendMessage();
  });
  $('msg-send-text').addEventListener('input', updateMsgByteCount);
  $('msg-transport-filter').addEventListener('change', fetchMessages);
  $('msg-direction-filter').addEventListener('change', fetchMessages);
  $('msg-send-transport').addEventListener('change', function() {
    fetchContacts($('msg-send-transport').value);
    updateMsgByteCount();
  });
  $('msg-send-dest').addEventListener('change', updateMsgByteCount);

  // Wire up sortable Meshtastic table headers
  var mshSortHeaders = document.querySelectorAll('#meshtastic-section th[data-sort]');
  for (var mi = 0; mi < mshSortHeaders.length; mi++) {
    (function(th) {
      th.addEventListener('click', function() {
        onMeshtasticSort(th.getAttribute('data-sort'));
      });
    })(mshSortHeaders[mi]);
  }
  $('meshtastic-show-more').addEventListener('click', function() {
    if (_mshVisible >= _mshNodes.length) {
      _mshVisible = _mshPageSize;  // collapse back
    } else {
      _mshVisible += _mshPageSize;  // show next page
    }
    renderMeshtasticNodes();
  });

  // Interface management — event delegation (CSP blocks inline handlers)
  $('restart-btn').addEventListener('click', doRestart);
  $('interfaces-table').addEventListener('change', function(ev) {
    var cb = ev.target;
    if (cb.tagName === 'INPUT' && cb.dataset.iface) {
      window._toggleIface(cb.dataset.iface);
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
  setInterval(fetchLoraReachability, 60000);

})();
