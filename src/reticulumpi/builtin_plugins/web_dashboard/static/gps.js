/* ReticulumPi Dashboard -- GPS telemetry panel */
(function () {
  'use strict';
  var R = window.RPI;
  var $ = R.$, esc = R.esc;
  var formatTimeAgo = R.formatTimeAgo, markUpdated = R.markUpdated;

  var _map = null;
  var _marker = null;
  var _accuracyCircle = null;
  var _didInitFit = false;

  var _sortKey = 'snr';
  var _sortAsc = false;
  var _lastSats = [];
  var _lastFix = null;

  // ── Leaflet map (lazy init) ───────────────────────────────────────────

  function _initMap() {
    if (_map) return true;
    var c = $('gps-map');
    if (!c || !window.L) return false;
    _map = L.map(c, {
      zoomControl: true,
      attributionControl: true
    }).setView([20, 0], 2);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 19
    }).addTo(_map);
    return true;
  }

  function _updateMap(fix) {
    if (fix == null || fix.lat == null || fix.lon == null) return;
    if (!_initMap()) return;
    var latlng = [fix.lat, fix.lon];
    if (!_marker) {
      _marker = L.marker(latlng).addTo(_map);
    } else {
      _marker.setLatLng(latlng);
    }
    var popup = '<div class="map-popup">'
      + '<div class="map-popup-name">GPS fix</div>'
      + '<div>Lat: ' + fix.lat.toFixed(5) + '</div>'
      + '<div>Lon: ' + fix.lon.toFixed(5) + '</div>';
    if (fix.alt_m != null) popup += '<div>Alt: ' + fix.alt_m.toFixed(1) + ' m</div>';
    if (fix.hdop != null) popup += '<div>HDOP: ' + fix.hdop.toFixed(2) + '</div>';
    popup += '</div>';
    _marker.bindPopup(popup);
    if (!_didInitFit) {
      _map.setView(latlng, 12);
      _didInitFit = true;
    } else {
      // Only recenter if marker moved significantly (>~10m) to avoid jitter
      var cur = _map.getCenter();
      var dLat = cur.lat - fix.lat, dLon = cur.lng - fix.lon;
      if (dLat * dLat + dLon * dLon > 0.0001) {
        _map.panTo(latlng, { animate: true });
      }
    }
    // Invalidate size in case the panel was hidden on first render
    setTimeout(function () { if (_map) _map.invalidateSize(); }, 50);
  }

  // ── Sky plot (SVG) ────────────────────────────────────────────────────
  //
  //   Polar projection: center = zenith (90° elevation), radius = horizon (0°).
  //   For each satellite we compute:
  //     r = (90 - elevation) / 90 * RADIUS
  //     x = r * sin(azimuth),  y = -r * cos(azimuth)   (North = up)

  var SKY_RADIUS = 100;

  function _snrClass(snr) {
    if (snr == null || isNaN(snr)) return 'snr-none';
    if (snr < 20) return 'snr-low';
    if (snr < 35) return 'snr-mid';
    return 'snr-hi';
  }

  function _skyRings() {
    // Static ring + cardinal scaffold; drawn once then left alone.
    var svg = $('gps-sky');
    if (!svg || svg.dataset.scaffolded === '1') return;
    var ns = 'http://www.w3.org/2000/svg';
    var parts = [];
    // Elevation rings at 0, 30, 60
    [30, 60, 90].forEach(function (el) {
      var r = (90 - el) / 90 * SKY_RADIUS;
      if (r <= 0) return;
      var c = document.createElementNS(ns, 'circle');
      c.setAttribute('cx', 0); c.setAttribute('cy', 0);
      c.setAttribute('r', r);
      c.setAttribute('class', 'sky-ring');
      parts.push(c);
    });
    // Outer horizon
    var horizon = document.createElementNS(ns, 'circle');
    horizon.setAttribute('cx', 0); horizon.setAttribute('cy', 0);
    horizon.setAttribute('r', SKY_RADIUS);
    horizon.setAttribute('class', 'sky-horizon');
    parts.push(horizon);
    // Cardinal labels
    [['N', 0, -SKY_RADIUS - 4],
     ['E',  SKY_RADIUS + 4, 0],
     ['S',  0,  SKY_RADIUS + 10],
     ['W', -SKY_RADIUS - 4, 0]].forEach(function (d) {
      var t = document.createElementNS(ns, 'text');
      t.setAttribute('x', d[1]); t.setAttribute('y', d[2]);
      t.setAttribute('class', 'sky-cardinal');
      t.setAttribute('text-anchor', 'middle');
      t.textContent = d[0];
      parts.push(t);
    });
    var scaffold = document.createElementNS(ns, 'g');
    scaffold.setAttribute('class', 'sky-scaffold');
    for (var i = 0; i < parts.length; i++) scaffold.appendChild(parts[i]);
    svg.appendChild(scaffold);
    // Satellite-plot group (we replace its children on each update)
    var plot = document.createElementNS(ns, 'g');
    plot.setAttribute('id', 'gps-sky-plot');
    svg.appendChild(plot);
    svg.dataset.scaffolded = '1';
  }

  function _renderSkyPlot(sats) {
    _skyRings();
    var plot = $('gps-sky-plot');
    if (!plot) return;
    while (plot.firstChild) plot.removeChild(plot.firstChild);
    var ns = 'http://www.w3.org/2000/svg';
    for (var i = 0; i < sats.length; i++) {
      var s = sats[i];
      if (s.elevation_deg == null || s.azimuth_deg == null) continue;
      var r = (90 - s.elevation_deg) / 90 * SKY_RADIUS;
      if (r < 0) r = 0;
      var rad = s.azimuth_deg * Math.PI / 180;
      var x = r * Math.sin(rad);
      var y = -r * Math.cos(rad);
      var g = document.createElementNS(ns, 'g');
      g.setAttribute('class', 'sat-group' + (s.in_use ? ' sat-in-use' : ''));
      if (s.in_use) {
        var ring = document.createElementNS(ns, 'circle');
        ring.setAttribute('cx', x.toFixed(2));
        ring.setAttribute('cy', y.toFixed(2));
        ring.setAttribute('r', 8);
        ring.setAttribute('class', 'sat-use-ring');
        g.appendChild(ring);
      }
      var c = document.createElementNS(ns, 'circle');
      c.setAttribute('cx', x.toFixed(2));
      c.setAttribute('cy', y.toFixed(2));
      c.setAttribute('r', 5);
      c.setAttribute('class', 'sat-dot ' + _snrClass(s.snr_db));
      var snrTxt = s.snr_db != null ? s.snr_db + ' dB' : 'no SNR';
      var useTxt = s.in_use ? ' — in fix' : '';
      c.appendChild(document.createElementNS(ns, 'title')).textContent =
        'PRN ' + s.prn + ' — elev ' + s.elevation_deg + '°, azim ' + s.azimuth_deg + '°, ' + snrTxt + useTxt;
      var t = document.createElementNS(ns, 'text');
      t.setAttribute('x', x.toFixed(2));
      t.setAttribute('y', (y + 3).toFixed(2));
      t.setAttribute('class', 'sat-label');
      t.setAttribute('text-anchor', 'middle');
      t.textContent = s.prn;
      g.appendChild(c);
      g.appendChild(t);
      plot.appendChild(g);
    }
  }

  // ── Sortable sat table ────────────────────────────────────────────────

  function _sortSats(sats) {
    var key = _sortKey, asc = _sortAsc;
    return sats.slice().sort(function (a, b) {
      var va, vb;
      if (key === 'prn') { va = a.prn || 0; vb = b.prn || 0; }
      else if (key === 'elevation') { va = a.elevation_deg || 0; vb = b.elevation_deg || 0; }
      else if (key === 'azimuth') { va = a.azimuth_deg || 0; vb = b.azimuth_deg || 0; }
      else if (key === 'in_use') { va = a.in_use ? 1 : 0; vb = b.in_use ? 1 : 0; }
      else { va = a.snr_db == null ? -1 : a.snr_db; vb = b.snr_db == null ? -1 : b.snr_db; }
      return asc ? va - vb : vb - va;
    });
  }

  function _updateSortIndicators() {
    var headers = document.querySelectorAll('#gps-section th[data-sort]');
    for (var i = 0; i < headers.length; i++) {
      var th = headers[i];
      var k = th.getAttribute('data-sort').replace('gps-', '');
      var arrow = th.querySelector('.sort-arrow');
      if (!arrow) continue;
      if (k === _sortKey) arrow.textContent = _sortAsc ? ' ▲' : ' ▼';
      else arrow.textContent = '';
    }
  }

  function _renderSatTable(sats) {
    var tbody = $('gps-sats-table');
    if (!tbody) return;
    var sorted = _sortSats(sats);
    var html = '';
    for (var i = 0; i < sorted.length; i++) {
      var s = sorted[i];
      var rowCls = s.in_use ? ' class="sat-in-use"' : '';
      var usedCell = s.in_use
        ? '<span class="sat-used-badge" title="Contributing to position fix">✓</span>'
        : '<span class="sat-unused">–</span>';
      html += '<tr' + rowCls + '>'
        + '<td>' + esc(String(s.prn)) + '</td>'
        + '<td>' + (s.elevation_deg != null ? s.elevation_deg + '°' : '--') + '</td>'
        + '<td>' + (s.azimuth_deg != null ? s.azimuth_deg + '°' : '--') + '</td>'
        + '<td class="' + _snrClass(s.snr_db) + '">'
        + (s.snr_db != null ? s.snr_db + ' dB' : '--')
        + '</td>'
        + '<td class="sat-used-cell">' + usedCell + '</td>'
        + '</tr>';
    }
    tbody.innerHTML = html || '<tr><td colspan="5" class="table-empty">No satellites in view</td></tr>';
    _updateSortIndicators();
  }

  function onGpsSort(key) {
    if (_sortKey === key) _sortAsc = !_sortAsc;
    else { _sortKey = key; _sortAsc = false; }
    _renderSatTable(_lastSats);
  }

  // ── Overview render ───────────────────────────────────────────────────

  function _fmt(val, suffix, digits) {
    if (val == null || (typeof val === 'number' && isNaN(val))) return '--';
    if (typeof val === 'number' && digits != null) return val.toFixed(digits) + (suffix || '');
    return String(val) + (suffix || '');
  }

  var FIX_QUALITY_LABEL = {
    0: 'none', 1: 'GPS', 2: 'DGPS', 3: 'PPS', 4: 'RTK', 5: 'FRTK', 6: 'dead-reckon'
  };
  var FIX_TYPE_LABEL = { 1: 'no fix', 2: '2D', 3: '3D' };

  function _renderOverview(snap, fix) {
    var stats = [];
    var fixLabel = '--';
    if (fix) {
      var ft = FIX_TYPE_LABEL[fix.fix_type] || null;
      var fq = FIX_QUALITY_LABEL[fix.fix_quality] || null;
      fixLabel = ft || fq || (snap.have_fix ? 'fix' : 'no fix');
    } else if (snap.connected) {
      fixLabel = 'searching';
    } else {
      fixLabel = 'disconnected';
    }
    stats.push({ label: 'Fix', value: fixLabel });
    if (fix && fix.satellites_used != null) {
      stats.push({ label: 'Sats (used / visible)',
                   value: fix.satellites_used + ' / ' + (snap.satellites_in_view_count || 0) });
    } else {
      stats.push({ label: 'Sats visible', value: String(snap.satellites_in_view_count || 0) });
    }
    if (fix) {
      if (fix.lat != null) stats.push({ label: 'Latitude',  value: _fmt(fix.lat, '°', 5) });
      if (fix.lon != null) stats.push({ label: 'Longitude', value: _fmt(fix.lon, '°', 5) });
      if (fix.alt_m != null) stats.push({ label: 'Altitude', value: _fmt(fix.alt_m, ' m', 1) });
      if (fix.speed_kn != null) {
        stats.push({ label: 'Speed',
                     value: _fmt(fix.speed_kn, ' kn', 1)
                          + ' (' + _fmt(fix.speed_kn * 1.852, ' km/h', 1) + ')' });
      }
      if (fix.heading_deg != null) stats.push({ label: 'Heading', value: _fmt(fix.heading_deg, '°', 0) });
      if (fix.hdop != null) stats.push({ label: 'HDOP', value: _fmt(fix.hdop, '', 2) });
      if (fix.pdop != null) stats.push({ label: 'PDOP', value: _fmt(fix.pdop, '', 2) });
      if (fix.vdop != null) stats.push({ label: 'VDOP', value: _fmt(fix.vdop, '', 2) });
      if (fix.utc_time || fix.utc_date) {
        stats.push({ label: 'UTC',
                     value: (fix.utc_date ? fix.utc_date + ' ' : '') + (fix.utc_time || '') });
      }
      if (fix.timestamp) stats.push({ label: 'Last fix', value: formatTimeAgo(fix.timestamp) });
    }
    stats.push({ label: 'Port', value: esc(snap.serial_port || '--') });
    stats.push({ label: 'Baud', value: String(snap.baudrate || '--') });
    stats.push({ label: 'Msgs in', value: String(snap.msgs_received || 0) });
    if (snap.reconnect_failures) {
      stats.push({ label: 'Reconnects', value: String(snap.reconnect_failures) });
    }

    var html = '';
    for (var i = 0; i < stats.length; i++) {
      html += '<div class="gps-stat">'
        + '<span class="gps-label">' + esc(stats[i].label) + '</span>'
        + '<span class="gps-value">' + stats[i].value + '</span>'
        + '</div>';
    }
    $('gps-overview').innerHTML = html;
  }

  // ── Status badge ──────────────────────────────────────────────────────

  function _renderBadge(snap, fix) {
    var el = $('gps-status');
    if (!el) return;
    if (!snap.connected) {
      el.textContent = 'disconnected';
      el.className = 'count status-err';
      return;
    }
    if (!snap.have_fix || !fix) {
      el.textContent = 'searching';
      el.className = 'count status-warn';
      return;
    }
    var sats = fix.satellites_used != null ? fix.satellites_used : '?';
    el.textContent = (FIX_TYPE_LABEL[fix.fix_type] || 'fix') + ' · ' + sats + ' sat';
    el.className = 'count status-ok';
  }

  // ── Public entry point ────────────────────────────────────────────────

  function updateGps(snap) {
    var section = $('gps-section');
    if (!section) return;
    if (!snap || snap.available === false) {
      section.style.display = 'none';
      return;
    }
    section.style.display = '';
    markUpdated('gps-section');
    var fix = snap.last_fix || null;
    _lastFix = (fix && fix.lat != null && fix.lon != null) ? fix : null;
    _renderBadge(snap, fix);
    _renderOverview(snap, fix);
    _lastSats = snap.satellites_in_view || [];
    _renderSatTable(_lastSats);
    _renderSkyPlot(_lastSats);
    if (fix) _updateMap(fix);
  }

  function getLastGpsFix() {
    return _lastFix;
  }

  RPI.updateGps = updateGps;
  RPI.onGpsSort = onGpsSort;
  RPI.getLastGpsFix = getLastGpsFix;
})();
