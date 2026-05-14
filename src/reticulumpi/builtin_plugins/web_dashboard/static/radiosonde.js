/* ReticulumPi Dashboard -- Radiosonde tracking panel */
(function () {
  'use strict';
  var R = window.RPI;
  if (!R) return;
  var $ = R.$, esc = R.esc;

  var _expanded = false;
  var _wired = false;

  function _fmtHM(s) {
    if (s == null) return '--';
    return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
  }

  function _wireToggle() {
    if (_wired) return;
    var toggle = $('radiosonde-toggle');
    var body = $('radiosonde-body');
    if (!toggle || !body) return;
    _wired = true;
    toggle.addEventListener('click', function () {
      _expanded = !_expanded;
      body.classList.toggle('hidden', !_expanded);
      var chev = toggle.querySelector('.chevron');
      if (chev) chev.textContent = _expanded ? '▾' : '▶';
    });
  }

  function _renderCards(sonde) {
    var el = $('radiosonde-cards');
    if (!el) return;
    if (!sonde) {
      el.innerHTML = '<div class="radiosonde-card"><span class="radiosonde-card-value">No sonde currently tracked</span></div>';
      return;
    }
    var cards = [
      { label: 'Sonde', value: esc(sonde.id || '--') + ' <small>' + esc(sonde.type || '') + '</small>' },
      { label: 'Altitude', value: sonde.alt_m != null ? sonde.alt_m.toLocaleString() + ' m' : '--' },
      { label: 'Temperature', value: sonde.temp_c != null ? sonde.temp_c.toFixed(1) + ' °C' : '--' },
      { label: 'Humidity', value: sonde.humidity_pct != null ? sonde.humidity_pct + ' %' : '--' },
      { label: 'Pressure', value: sonde.pressure_hpa != null ? sonde.pressure_hpa.toFixed(1) + ' hPa' : '--' },
      { label: 'Vert Speed', value: sonde.vel_v_ms != null ? sonde.vel_v_ms.toFixed(1) + ' m/s' : '--' },
      { label: 'Phase', value: esc(sonde.phase || '--') }
    ];
    var html = '';
    for (var i = 0; i < cards.length; i++) {
      html += '<div class="radiosonde-card">'
        + '<span class="radiosonde-card-label">' + esc(cards[i].label) + '</span>'
        + '<span class="radiosonde-card-value">' + cards[i].value + '</span>'
        + '</div>';
    }
    el.innerHTML = html;
  }

  function _renderChart(profile, burstAlt) {
    var canvas = $('radiosonde-chart');
    if (!canvas || !canvas.getContext) return;
    var ctx = canvas.getContext('2d');
    var w = canvas.width = canvas.parentElement ? canvas.parentElement.clientWidth || 300 : 300;
    var h = canvas.height = 160;
    ctx.clearRect(0, 0, w, h);

    if (!profile || profile.length < 2) {
      ctx.fillStyle = '#888';
      ctx.font = '12px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('Waiting for altitude data…', w / 2, h / 2);
      return;
    }

    var pad = { top: 14, right: 12, bottom: 22, left: 48 };
    var pw = w - pad.left - pad.right;
    var ph = h - pad.top - pad.bottom;

    var tMin = profile[0].ts, tMax = profile[profile.length - 1].ts;
    var aMin = Infinity, aMax = -Infinity;
    for (var i = 0; i < profile.length; i++) {
      if (profile[i].alt_m < aMin) aMin = profile[i].alt_m;
      if (profile[i].alt_m > aMax) aMax = profile[i].alt_m;
    }
    if (burstAlt != null && burstAlt > aMax) aMax = burstAlt;
    var tRange = tMax - tMin || 1, aRange = aMax - aMin || 1;
    aMin = Math.max(0, aMin - aRange * 0.05);
    aRange = (aMax + aRange * 0.05) - aMin;

    function tx(t) { return pad.left + ((t - tMin) / tRange) * pw; }
    function ty(alt) { return pad.top + ph - ((alt - aMin) / aRange) * ph; }

    // Y axis labels
    ctx.fillStyle = '#aaa'; ctx.font = '10px sans-serif'; ctx.textAlign = 'right';
    for (var s = 0; s <= 4; s++) {
      var av = aMin + (s / 4) * aRange, yy = ty(av);
      ctx.fillText((av / 1000).toFixed(1) + 'km', pad.left - 4, yy + 3);
      ctx.strokeStyle = '#333'; ctx.lineWidth = 0.5;
      ctx.beginPath(); ctx.moveTo(pad.left, yy); ctx.lineTo(w - pad.right, yy); ctx.stroke();
    }
    ctx.textAlign = 'center';
    ctx.fillText('start', tx(tMin), h - 4); ctx.fillText('now', tx(tMax), h - 4);

    // Draw altitude line, color by vertical speed
    ctx.lineWidth = 1.5;
    for (var j = 1; j < profile.length; j++) {
      var p0 = profile[j - 1], p1 = profile[j];
      var ascending = (p1.vel_v != null) ? p1.vel_v >= 0 : p1.alt_m >= p0.alt_m;
      ctx.strokeStyle = ascending ? '#4caf50' : '#e53935';
      ctx.beginPath();
      ctx.moveTo(tx(p0.ts), ty(p0.alt_m));
      ctx.lineTo(tx(p1.ts), ty(p1.alt_m));
      ctx.stroke();
    }

    // Burst altitude marker
    if (burstAlt != null) {
      var by = ty(burstAlt);
      ctx.setLineDash([4, 3]); ctx.strokeStyle = '#ff9800'; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(pad.left, by); ctx.lineTo(w - pad.right, by); ctx.stroke();
      ctx.setLineDash([]);
      // Circle on point closest to burst altitude
      var bestIdx = 0, bestDiff = Infinity;
      for (var b = 0; b < profile.length; b++) {
        var diff = Math.abs(profile[b].alt_m - burstAlt);
        if (diff < bestDiff) { bestDiff = diff; bestIdx = b; }
      }
      if (bestDiff < aRange * 0.05) {
        ctx.beginPath();
        ctx.arc(tx(profile[bestIdx].ts), ty(profile[bestIdx].alt_m), 4, 0, 2 * Math.PI);
        ctx.strokeStyle = '#ff9800'; ctx.lineWidth = 2; ctx.stroke();
      }
    }
  }

  function _renderNext(next) {
    var el = $('radiosonde-next');
    if (!el) return;
    if (!next) { el.textContent = ''; return; }
    el.textContent = 'Next window: ' + (next.label || '--')
      + ' in ' + _fmtHM(next.countdown_s);
  }

  function _renderHistory(sondes) {
    var tbody = $('radiosonde-history');
    if (!tbody) return;
    if (!sondes || sondes.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5">No recent sondes</td></tr>';
      return;
    }
    var html = '';
    var limit = Math.min(sondes.length, 10);
    for (var i = 0; i < limit; i++) {
      var s = sondes[i];
      html += '<tr>'
        + '<td>' + esc(s.id || '--') + '</td>'
        + '<td>' + esc(s.type || '--') + '</td>'
        + '<td>' + (s.burst_alt_m != null ? (s.burst_alt_m / 1000).toFixed(1) + ' km' : '--') + '</td>'
        + '<td>' + (s.frame_count != null ? s.frame_count : '--') + '</td>'
        + '<td>' + _fmtHM(s.duration_s) + '</td>'
        + '</tr>';
    }
    tbody.innerHTML = html;
  }

  R.updateRadiosonde = function (d) {
    if (!d) return;
    var section = $('radiosonde-section');
    if (!section) return;
    section.style.display = '';

    _wireToggle();

    // Status badge
    var badge = $('radiosonde-status');
    if (badge) badge.textContent = d.status || 'idle';

    // Telemetry cards
    _renderCards(d.active_sonde || null);

    // Altitude chart (only when expanded or no toggle yet)
    var body = $('radiosonde-body');
    if (!body || !body.classList.contains('hidden')) {
      var burstAlt = d.active_sonde ? d.active_sonde.burst_alt_m : null;
      _renderChart(d.altitude_profile, burstAlt);
    }

    // Next launch countdown (shown when no active sonde)
    if (!d.active_sonde && d.next_launch) {
      _renderNext(d.next_launch);
    } else {
      var nextEl = $('radiosonde-next');
      if (nextEl) nextEl.textContent = '';
    }

    // Recent sondes table
    _renderHistory(d.recent_sondes);
  };
})();
