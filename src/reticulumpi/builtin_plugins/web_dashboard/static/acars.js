/* ReticulumPi Dashboard — ACARS message feed module */
(function () {
  'use strict';
  var R = window.RPI;
  if (!R) return;
  var $ = R.$, esc = R.esc;

  var MAX_ROWS = 50;

  // -- DOM handles --
  var _section = null, _body = null, _toggle = null;
  var _statusEl, _countEl, _statsEl, _tableBody;

  // -- State --
  var _expanded = false;
  var _wired = false;

  function _resolveDom() {
    if (_section) return true;
    _section  = $('acars-section');
    if (!_section) return false;
    _toggle   = $('acars-toggle');
    _body     = $('acars-body');
    _statusEl = $('acars-status');
    _countEl  = $('acars-count');
    _statsEl  = $('acars-stats');
    _tableBody = $('acars-table');
    return true;
  }

  function _wireToggle() {
    if (_wired || !_toggle || !_body) return;
    _wired = true;
    _toggle.addEventListener('click', function () {
      _expanded = !_expanded;
      _body.classList.toggle('hidden', !_expanded);
      var chev = _toggle.querySelector('.chevron');
      if (chev) chev.innerHTML = _expanded ? '&#9662;' : '&#9656;';
    });
  }

  function _fmtTime(epoch) {
    if (!epoch) return '--';
    var d = new Date(epoch * 1000);
    var h = d.getHours(), m = d.getMinutes(), s = d.getSeconds();
    return (h < 10 ? '0' : '') + h + ':' +
           (m < 10 ? '0' : '') + m + ':' +
           (s < 10 ? '0' : '') + s;
  }

  function _truncate(text, max) {
    if (!text) return '';
    return text.length > max ? text.slice(0, max) + '…' : text;
  }

  function _renderTable(messages) {
    if (!_tableBody) return;
    var list = messages || [];
    if (list.length > MAX_ROWS) list = list.slice(0, MAX_ROWS);
    var html = '';
    for (var i = 0; i < list.length; i++) {
      var m = list[i];
      var cls = m.error ? ' class="acars-error"' : '';
      var full = m.text || '';
      html += '<tr' + cls + '>'
        + '<td>' + esc(_fmtTime(m.timestamp)) + '</td>'
        + '<td>' + esc(m.flight || '--') + '</td>'
        + '<td>' + esc(m.tail || '--') + '</td>'
        + '<td>' + (m.freq_mhz != null ? m.freq_mhz.toFixed(3) : '--') + '</td>'
        + '<td title="' + esc(m.label_desc || '') + '">' + esc(m.label || '--') + '</td>'
        + '<td title="' + esc(full) + '">' + esc(_truncate(full, 60)) + '</td>'
        + '</tr>';
    }
    _tableBody.innerHTML = html;
  }

  R.updateAcars = function (d) {
    if (!d) return;
    if (!_resolveDom()) return;
    _wireToggle();

    _section.style.display = '';

    // Status badge
    if (_statusEl) _statusEl.textContent = d.status || 'unknown';

    // Count badge
    var stats = d.stats || {};
    if (_countEl) {
      _countEl.textContent = (stats.messages_total || 0) + ' msgs';
    }

    // Stats line
    if (_statsEl) {
      _statsEl.textContent = (stats.messages_total || 0) + ' messages, '
        + (stats.unique_flights_today || 0) + ' flights, '
        + (stats.unique_tails_today || 0) + ' aircraft today';
    }

    // Table (only render when expanded)
    if (_body && !_body.classList.contains('hidden')) {
      _renderTable(d.recent_messages);
    }
  };
})();
