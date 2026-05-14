/* ReticulumPi Dashboard -- NOAA Weather Radio SAME alert module */
(function () {
  'use strict';
  var R = window.RPI;
  if (!R) return;
  var $ = R.$, esc = R.esc;

  var _expanded = false;
  var _bound = false;

  var SEV_CLASS = {
    extreme: 'weather-alert-extreme',
    severe: 'weather-alert-severe',
    moderate: 'weather-alert-moderate',
    minor: 'weather-alert-moderate'
  };

  function _formatLocal(epoch) {
    if (!epoch) return '--';
    var d = new Date(epoch * 1000);
    var mo = d.getMonth() + 1;
    var day = d.getDate();
    var h = d.getHours();
    var m = d.getMinutes();
    return mo + '/' + day + ' '
      + (h < 10 ? '0' : '') + h + ':'
      + (m < 10 ? '0' : '') + m;
  }

  function _countdown(purge_ts) {
    if (!purge_ts) return '';
    var delta = Math.floor(purge_ts - Date.now() / 1000);
    if (delta <= 0) return 'expired';
    var h = Math.floor(delta / 3600);
    var m = Math.floor((delta % 3600) / 60);
    if (h > 0) return h + 'h ' + m + 'm remaining';
    return m + 'm remaining';
  }

  function _bindToggle() {
    if (_bound) return;
    _bound = true;
    var toggle = $('weather-alert-toggle');
    var body = $('weather-alert-body');
    if (toggle && body) {
      toggle.addEventListener('click', function () {
        _expanded = !_expanded;
        body.classList.toggle('hidden', !_expanded);
        var chev = toggle.querySelector('.chevron');
        if (chev) chev.textContent = _expanded ? '▾' : '▶';
      });
    }
  }

  R.updateWeatherAlert = function (d) {
    if (!d) return;

    // -- Banner ---------------------------------------------------------------
    var banner = $('weather-alert-banner');
    if (banner) {
      var a = d.active_alert;
      if (a) {
        var areas = (a.fips_names || []).join(', ');
        var text = esc(a.event_desc || a.event_code || 'Alert');
        if (areas) text += ' — ' + esc(areas);
        var cd = _countdown(a.purge_ts);
        if (cd) text += ' (' + esc(cd) + ')';
        banner.innerHTML = text;
        banner.className = 'weather-alert-banner '
          + (SEV_CLASS[a.severity] || 'weather-alert-moderate');
        banner.style.display = '';
      } else {
        banner.style.display = 'none';
      }
    }

    // -- Section --------------------------------------------------------------
    var section = $('weather-alert-section');
    if (!section) return;
    section.style.display = '';
    section.classList.remove('awaiting-data');
    _bindToggle();

    // Status badge
    var badge = $('weather-alert-status');
    if (badge) badge.textContent = d.status || 'idle';

    // Stats
    var statsEl = $('weather-alert-stats');
    if (statsEl && d.stats) {
      statsEl.textContent = 'Total alerts: ' + (d.stats.alerts_total || 0);
    }

    // History table
    var tbody = $('weather-alert-table');
    if (!tbody) return;
    var history = d.alert_history || [];
    var limit = Math.min(history.length, 20);
    var html = '';
    for (var i = 0; i < limit; i++) {
      var h = history[i];
      html += '<tr>'
        + '<td>' + esc(_formatLocal(h.issued_ts)) + '</td>'
        + '<td>' + esc(h.event_desc || h.event_code || '--') + '</td>'
        + '<td>' + esc(h.severity || '--') + '</td>'
        + '<td>' + esc((h.fips_names || []).join(', ') || '--') + '</td>'
        + '</tr>';
    }
    tbody.innerHTML = html;
  };
})();
