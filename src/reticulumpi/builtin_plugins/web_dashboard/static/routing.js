/* ReticulumPi Dashboard — Routing module */
(function() {
  'use strict';
  var R = window.RPI;
  var api = R.api, $ = R.$, esc = R.esc;
  var markUpdated = R.markUpdated;

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
    var rtc = data.rate_tracked_count || 0;
    _setStatCard('ri-tracked', rtc, '');
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
      if (f.expiring_soon > 0) fhtml += '<span class="rf-item text-warn"><span class="ri-label">Expiring soon </span><span class="rf-val">' + f.expiring_soon + '</span></span>';
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
      el.innerHTML = '<div class="dashboard-empty">No path data</div>';
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
        + '<div class="bar-track"><div class="bar-fill ' + b.cls + '" data-rpi-width="' + pct + '"></div></div>'
        + '<div class="bar-count">' + b.count + '</div>'
        + '<div class="bar-pct">' + pctOfTotal + '%</div>'
        + '</div>';
    }
    el.innerHTML = html;
    R.applyCspDynamicStyles(el);
  }

  function renderIfaceChart(dist, total) {
    var el = $('iface-chart');
    if (!el) return;

    var keys = Object.keys(dist);
    if (keys.length === 0) {
      el.innerHTML = '<div class="dashboard-empty">No path data</div>';
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
        + '<div class="bar-track"><div class="bar-fill ' + colorCls + '" data-rpi-width="' + pct + '"></div></div>'
        + '<div class="bar-count">' + count + '</div>'
        + '<div class="bar-pct">' + pctOfTotal + '%</div>'
        + '</div>';
    }
    el.innerHTML = html;
    R.applyCspDynamicStyles(el);
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
      tbody.innerHTML = '<tr><td colspan="6" class="dashboard-empty-cell">No paths match filters</td></tr>';
      renderRoutingPagination(0, 0);
      return;
    }

    var html = '';
    for (var i = 0; i < paths.length; i++) {
      var p = paths[i];
      var hopCls = hopColorClass(p.hops || 0);
      var expCls = '';
      if (p.expires_in_s != null && p.expires_in_s < 600) expCls = ' class="text-danger"';
      else if (p.expires_in_s != null && p.expires_in_s < 1800) expCls = ' class="text-warn"';

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

  // Register public functions on namespace
  R.updateRoutingSummary = updateRoutingSummary;
  R.fetchRoutingTable = fetchRoutingTable;
  R._rtPage = function() { return _rtPage; };
  R._setRtPage = function(v) { _rtPage = v; };
  R._rtSort = function() { return _rtSort; };
  R._setRtSort = function(v) { _rtSort = v; };
  R._rtOrder = function() { return _rtOrder; };
  R._setRtOrder = function(v) { _rtOrder = v; };
  R._rtSearch = function() { return _rtSearch; };
  R._setRtSearch = function(v) { _rtSearch = v; };
  R._rtIfaceFilter = function() { return _rtIfaceFilter; };
  R._setRtIfaceFilter = function(v) { _rtIfaceFilter = v; };
  R._rtHopsFilter = function() { return _rtHopsFilter; };
  R._setRtHopsFilter = function(v) { _rtHopsFilter = v; };
  R._rtTableOpen = function() { return _rtTableOpen; };
  R._setRtTableOpen = function(v) { _rtTableOpen = v; };
  R._rtDebounceTimer = function() { return _rtDebounceTimer; };
  R._setRtDebounceTimer = function(v) { _rtDebounceTimer = v; };
  R._rtAutoRefresh = function() { return _rtAutoRefresh; };
  R._setRtAutoRefresh = function(v) { _rtAutoRefresh = v; };
})();
