/* ReticulumPi Dashboard -- AIS vessel tracking panel */
(function () {
  'use strict';
  var R = window.RPI;
  if (!R) return;
  var $ = R.$, esc = R.esc;
  var formatTimeAgo = R.formatTimeAgo;

  var _expanded = false;
  var _wired = false;
  var _sortCol = 'last_seen';
  var _sortAsc = false;
  var _lastData = null;

  function _timeAgo(ts) {
    if (!ts) return '--';
    if (formatTimeAgo) return formatTimeAgo(ts);
    var s = Math.floor((Date.now() / 1000) - ts);
    return s < 60 ? s + 's ago' : Math.floor(s / 60) + 'm ago';
  }

  function _truncate(str, max) {
    if (!str) return '--';
    return str.length > max ? str.slice(0, max) + '…' : str;
  }

  function _sortVessels(vessels) {
    return vessels.slice().sort(function (a, b) {
      var va = a[_sortCol], vb = b[_sortCol];
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (typeof va === 'string') { va = va.toLowerCase(); vb = (vb || '').toLowerCase(); }
      if (va < vb) return _sortAsc ? -1 : 1;
      if (va > vb) return _sortAsc ? 1 : -1;
      return 0;
    });
  }

  function _renderTable(vessels) {
    var tbody = $('ais-table');
    if (!tbody) return;
    var sorted = _sortVessels(vessels);
    var html = '';
    for (var i = 0; i < sorted.length; i++) {
      var v = sorted[i];
      html += '<tr>'
        + '<td title="' + esc(v.name || '') + '">' + esc(_truncate(v.name, 20)) + '</td>'
        + '<td>' + esc(v.mmsi || '--') + '</td>'
        + '<td>' + esc(v.ship_type_desc || '--') + '</td>'
        + '<td>' + (v.sog_knots != null ? v.sog_knots.toFixed(1) : '--') + '</td>'
        + '<td>' + (v.cog_deg != null ? v.cog_deg.toFixed(0) + '°' : '--') + '</td>'
        + '<td>' + esc(v.nav_status_desc || '--') + '</td>'
        + '<td>' + _timeAgo(v.last_seen) + '</td>'
        + '</tr>';
    }
    tbody.innerHTML = html || '<tr><td colspan="7">No vessels</td></tr>';
  }

  function _wireSortHeaders() {
    var headers = document.querySelectorAll('#ais-table-wrap th[data-sort]');
    for (var i = 0; i < headers.length; i++) {
      (function (th) {
        th.addEventListener('click', function () {
          var col = th.getAttribute('data-sort');
          if (_sortCol === col) { _sortAsc = !_sortAsc; }
          else { _sortCol = col; _sortAsc = (col === 'name' || col === 'mmsi'); }
          var all = document.querySelectorAll('#ais-table-wrap th[data-sort]');
          for (var j = 0; j < all.length; j++) all[j].classList.remove('sort-asc', 'sort-desc');
          th.classList.add(_sortAsc ? 'sort-asc' : 'sort-desc');
          if (_lastData) _renderTable(_lastData.vessels || []);
        });
      })(headers[i]);
    }
  }

  function _wireToggle() {
    var toggle = $('ais-toggle'), body = $('ais-body');
    if (!toggle || !body) return;
    toggle.addEventListener('click', function () {
      _expanded = !_expanded;
      body.classList.toggle('hidden', !_expanded);
      var chev = toggle.querySelector('.chevron');
      if (chev) chev.textContent = _expanded ? '▾' : '▶';
      if (_expanded && _lastData) _renderTable(_lastData.vessels || []);
    });
  }

  R.updateAis = function (d) {
    if (!d) return;
    _lastData = d;
    var section = $('ais-section');
    if (!section) return;
    section.style.display = '';
    section.classList.remove('awaiting-data');

    if (!_wired) {
      _wireToggle();
      _wireSortHeaders();
      _wired = true;
    }

    var badge = $('ais-status');
    if (badge) badge.textContent = d.status || 'unknown';

    var vessels = d.vessels || [];
    var countEl = $('ais-count');
    if (countEl) countEl.textContent = vessels.length + (vessels.length !== 1 ? ' vessels' : ' vessel');

    var statsEl = $('ais-stats');
    if (statsEl) {
      var stats = d.stats || {};
      statsEl.textContent = (stats.unique_vessels_session || vessels.length) + ' vessels tracked, '
        + (stats.messages_total || 0) + ' messages decoded';
    }

    var body = $('ais-body');
    if (body && body.classList.contains('hidden')) return;
    _renderTable(vessels);
  };
})();
