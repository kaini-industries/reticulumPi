/* ReticulumPi Dashboard — Mesh Network module */
(function() {
  'use strict';
  var R = window.RPI;
  var api = R.api, $ = R.$, esc = R.esc;
  var formatUptime = R.formatUptime, formatTimeAgo = R.formatTimeAgo;
  var metricClass = R.metricClass, markUpdated = R.markUpdated;
  var formatBytes = R.formatBytes;

  // Mesh node state -- server-side pagination
  var _meshNodes = [];       // current page of nodes from server
  var _meshTotal = 0;        // total nodes on server
  var _meshPage = 1;
  var _meshPerPage = 25;
  var _meshPages = 1;
  var _meshPeers = {};       // destination_hash -> telemetry data
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
  var _di = R._di;
  var _diRaw = R._diRaw;

  var _meshSortMap = {
    'score': 'score',
    'hops': 'hops',
    'last_seen': 'last_seen',
    'announce_count': 'announce_count'
  };

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
        + '<span class="reach-factor-bar"><span class="reach-factor-fill" data-rpi-width="' + pct + '"></span></span>'
        + '<span class="reach-factor-val">' + f.points + '/' + f.max + '</span>'
        + '<span class="reach-factor-detail">' + esc(f.detail || '') + '</span>'
        + '</div>';
    }
    h += '</div>';
    return h;
  }

  function buildNodeDetailHTML(node) {
    var peer = _meshPeers[node.destination_hash];
    var reach = R._reachScores[node.destination_hash];
    var firstSeen = node.first_seen ? new Date(node.first_seen * 1000).toLocaleString() : '--';
    var lastSeen = node.last_seen ? formatTimeAgo(node.last_seen) : '--';

    var h = '';

    // Reachability section (show first if data available)
    if (reach) {
      h += '<div class="node-detail-section">Reachability</div>'
        + '<div class="node-detail-grid">'
        + _diRaw('Score', _reachBadgeHTML(reach.score, reach.label))
        + '</div>'
        + _reachFactorHTML(reach.factors);
    }

    h += '<div class="node-detail-section">Identity</div>'
      + '<div class="node-detail-grid">'
      + _di('Address', node.destination_hash || '--')
      + _di('Name', node.app_data || '--')
      + _di('App', (node.app_name || '--') + (node.aspects ? '.' + node.aspects : ''))
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
      if (peer.v) h += _di('Version', peer.v);
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
      var reach = R._reachScores[hash];
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
        R.applyCspDynamicStyles(td);
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
          R._reachScores[n.destination_hash] = {
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
          var sa = (R._reachScores[a.destination_hash] || {}).score || 0;
          var sb = (R._reachScores[b.destination_hash] || {}).score || 0;
          return _meshSortAsc ? sa - sb : sb - sa;
        });
      }
      renderMeshNodes();
    }, function() {
      // Network error fallback -- render without scores
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

  // -- Mesh Summary -------------------------------------------------------
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
      el.innerHTML = '<div class="dashboard-empty">No data</div>';
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
        + '<div class="bar-track"><div class="bar-fill ' + colorCls + '" data-rpi-width="' + pct + '"></div></div>'
        + '<div class="bar-count">' + fmtK(count) + '</div>'
        + '<div class="bar-pct">' + pctOfTotal + '%</div>'
        + '</div>';
    }
    el.innerHTML = html;
    R.applyCspDynamicStyles(el);
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
      el.innerHTML = '<div class="dashboard-empty">No data</div>';
      return;
    }
    var html = '';
    for (var i = 0; i < buckets.length; i++) {
      var b = buckets[i];
      var pct = maxVal > 0 ? (b.count / maxVal * 100) : 0;
      var pctOfTotal = total > 0 ? Math.round(b.count / total * 100) : 0;
      html += '<div class="bar-row">'
        + '<div class="bar-label bar-label-hop">' + b.label + '</div>'
        + '<div class="bar-track"><div class="bar-fill ' + b.cls + '" data-rpi-width="' + pct + '"></div></div>'
        + '<div class="bar-count">' + fmtK(b.count) + '</div>'
        + '<div class="bar-pct">' + pctOfTotal + '%</div>'
        + '</div>';
    }
    el.innerHTML = html;
    R.applyCspDynamicStyles(el);
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

  // Register public functions on namespace
  R.fetchMeshNodes = fetchMeshNodes;
  R.fetchMeshSummary = fetchMeshSummary;
  R.cacheMeshPeers = cacheMeshPeers;
  R.updateMeshFromWS = updateMeshFromWS;
  R.updatePeerTelemetry = updatePeerTelemetry;
  R.onMeshSort = onMeshSort;
  R.toggleNodeDetail = toggleNodeDetail;
  R.renderMeshNodes = renderMeshNodes;
  R._meshNodes = function() { return _meshNodes; };
  R._meshPeers = function() { return _meshPeers; };
  R._meshSearchTimer = function() { return _meshSearchTimer; };
  R._setMeshSearchTimer = function(v) { _meshSearchTimer = v; };
  R._meshView = function() { return _meshView; };
  R._setMeshView = function(v) { _meshView = v; };
  R._meshPage = function() { return _meshPage; };
  R._setMeshPage = function(v) { _meshPage = v; };
  R._meshSearch = function() { return _meshSearch; };
  R._setMeshSearch = function(v) { _meshSearch = v; };
  R._peerPageSize = function() { return _peerPageSize; };
  R._peerVisible = function() { return _peerVisible; };
  R._setPeerVisible = function(v) { _peerVisible = v; };
})();
