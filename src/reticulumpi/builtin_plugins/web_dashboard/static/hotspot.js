/* ReticulumPi Dashboard -- Wi-Fi Hotspot panel */
(function () {
  'use strict';
  var R = window.RPI;
  var $ = R.$, esc = R.esc;

  function _fmtBytes(b) {
    if (b == null) return '--';
    if (b < 1024) return b + ' B';
    if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
    if (b < 1073741824) return (b / 1048576).toFixed(1) + ' MB';
    return (b / 1073741824).toFixed(2) + ' GB';
  }

  function _fmtDuration(s) {
    if (s == null) return '--';
    var d = Math.floor(s / 86400);
    var h = Math.floor((s % 86400) / 3600);
    var m = Math.floor((s % 3600) / 60);
    if (d > 0) return d + 'd ' + h + 'h';
    if (h > 0) return h + 'h ' + m + 'm';
    return m + 'm';
  }

  function _fmtIdle(ms) {
    if (ms == null) return '';
    if (ms < 30000) return '<span style="color:var(--ok)">(active)</span>';
    var sec = Math.floor(ms / 1000);
    var h = Math.floor(sec / 3600);
    var m = Math.floor((sec % 3600) / 60);
    var label = h > 0 ? h + 'h ' + m + 'm' : m + 'm';
    return '<span style="color:var(--warn)">(idle ' + label + ')</span>';
  }

  R.updateHotspot = function (d, portalData) {
    if (!d) return;
    var el = $('hotspot-section');
    if (!el) return;
    el.style.display = '';
    el.classList.remove('awaiting-data');

    var badge = $('hotspot-status');
    if (badge) {
      badge.textContent = d.active
        ? (d.client_count + ' client' + (d.client_count !== 1 ? 's' : ''))
        : 'inactive';
    }

    var hi;

    hi = $('hi-status');
    if (hi) {
      hi.textContent = 'Status: ' + (d.active ? 'Active' : 'Inactive');
      hi.className = 'conn-indicator ' + (d.active ? 'ci-ok' : 'ci-err');
    }

    hi = $('hi-ssid');
    if (hi) {
      hi.textContent = 'SSID: ' + (d.ssid || '--');
      hi.className = 'conn-indicator ci-info';
    }

    hi = $('hi-channel');
    if (hi) {
      var chText = 'Ch ' + (d.channel || '--');
      if (d.frequency) chText += ' (' + d.frequency + ' MHz)';
      hi.textContent = chText;
      hi.className = 'conn-indicator ci-info';
    }

    hi = $('hi-security');
    if (hi) {
      hi.textContent = d.security || '--';
      hi.className = 'conn-indicator ' + (d.security && d.security !== 'Open' ? 'ci-ok' : 'ci-warn');
    }

    hi = $('hi-ip');
    if (hi) {
      hi.textContent = d.ip || '--';
      hi.className = 'conn-indicator ci-info';
    }

    hi = $('hi-clients');
    if (hi) {
      var cc = d.client_count || 0;
      hi.textContent = cc + ' client' + (cc !== 1 ? 's' : '');
      hi.className = 'conn-indicator ' + (cc > 0 ? 'ci-ok' : 'ci-info');
    }

    hi = $('hi-portal-status');
    if (hi) {
      if (portalData) {
        hi.style.display = '';
        hi.textContent = 'Portal: ' + (portalData.portal_active ? 'Active' : 'Inactive');
        hi.className = 'conn-indicator ' + (portalData.portal_active ? 'ci-ok' : 'ci-info');
      } else {
        hi.style.display = 'none';
      }
    }

    hi = $('hi-portal-mode');
    if (hi) {
      if (portalData) {
        hi.style.display = '';
        hi.textContent = 'Mode: ' + (portalData.mode || '--');
        var modeClass = portalData.mode === 'off' ? 'ci-warn'
                      : portalData.mode === 'always' ? 'ci-ok'
                      : 'ci-info';
        hi.className = 'conn-indicator ' + modeClass;
      } else {
        hi.style.display = 'none';
      }
    }

    hi = $('hi-portal-requests');
    if (hi) {
      if (portalData) {
        hi.style.display = '';
        var rc = portalData.requests_served || 0;
        hi.textContent = rc + ' portal req' + (rc !== 1 ? 's' : '');
        hi.className = 'conn-indicator ci-info';
      } else {
        hi.style.display = 'none';
      }
    }

    var wrap = $('hotspot-clients-wrap');
    var tbody = $('hotspot-clients-table');
    if (tbody && d.clients && d.clients.length > 0) {
      if (wrap) wrap.style.display = '';
      var html = '';
      for (var i = 0; i < d.clients.length; i++) {
        var c = d.clients[i];
        var name = c.hostname || c.mac;
        var traffic = _fmtBytes(c.rx_bytes) + ' / ' + _fmtBytes(c.tx_bytes);
        html += '<tr>'
          + '<td>' + esc(name) + '</td>'
          + '<td>' + esc(c.ip || '--') + '</td>'
          + '<td>' + _fmtDuration(c.connected_time) + ' ' + _fmtIdle(c.inactive_time_ms) + '</td>'
          + '<td>' + esc(traffic) + '</td>'
          + '</tr>';
      }
      tbody.innerHTML = html;
    } else {
      if (wrap) wrap.style.display = 'none';
      if (tbody) tbody.innerHTML = '';
    }
  };
})();
