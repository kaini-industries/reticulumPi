/* ReticulumPi Dashboard — ADS-B Radar module
 *
 * Renders a live aircraft map + sortable table from dump1090 / SBS data
 * pushed over the WebSocket.  Each broadcast tick carries a snapshot of
 * all currently-tracked aircraft; this module diffs against its local
 * state to add, update, and remove markers and table rows.
 *
 * Aircraft photos come from the planespotters.net public API (CSP-allowed
 * by the server).  The map uses the shared Leaflet vendor already loaded
 * for the Node Map section.
 */
(function () {
  'use strict';
  var R = window.RPI;
  if (!R) return;
  var $ = R.$, esc = R.esc;
  var formatTimeAgo = R.formatTimeAgo;

  // -- DOM handles ----------------------------------------------------------
  var _section = null, _body = null, _toggle = null, _countEl = null;
  var _statsEl, _mapContainer, _tableBody;

  // -- Map state ------------------------------------------------------------
  var _map = null;
  var _markerGroup = null;
  var _markers = {};        // icao -> L.Marker
  var _receiverMarker = null;
  var _initialFit = false;

  // -- Runtime state --------------------------------------------------------
  var _expanded = false;
  var _lastData = null;
  var _sortCol = 'altitude';
  var _sortAsc = false;
  var _photoCache = {};     // icao -> {url, fetched}

  // -- Altitude color bands (feet) ------------------------------------------
  function _altColor(alt) {
    if (alt == null) return '#888';
    if (alt < 10000) return '#4caf50';
    if (alt < 25000) return '#ff9800';
    if (alt < 35000) return '#f57c00';
    return '#e53935';
  }

  function _altBand(alt) {
    if (alt == null) return '--';
    if (alt < 10000) return 'Low';
    if (alt < 25000) return 'Medium';
    if (alt < 35000) return 'High';
    return 'Very High';
  }

  // -- Emergency squawk detection -------------------------------------------
  var _EMERGENCY_SQUAWKS = { '7500': 'Hijack', '7600': 'Radio Fail', '7700': 'Emergency' };

  function _isEmergency(squawk) {
    return squawk && _EMERGENCY_SQUAWKS[squawk];
  }

  // -- Aircraft icon (rotatable plane SVG) ----------------------------------
  function _makeIcon(track, alt, squawk) {
    var rotation = (track != null) ? track : 0;
    var color = _isEmergency(squawk) ? '#ff1744' : _altColor(alt);
    var size = _isEmergency(squawk) ? 28 : 22;
    var html = '<svg viewBox="0 0 24 24" width="' + size + '" height="' + size + '" '
      + 'style="transform:rotate(' + rotation + 'deg)">'
      + '<path d="M12 2 L10 9 L3 12 L10 13 L10 20 L8 22 L12 21 L16 22 L14 20 L14 13 L21 12 L14 9 Z" '
      + 'fill="' + color + '" stroke="#000" stroke-width="0.8" opacity="0.9"/>'
      + '</svg>';
    return L.divIcon({
      className: 'adsb-marker',
      html: html,
      iconSize: [size, size],
      iconAnchor: [size / 2, size / 2],
      popupAnchor: [0, -size / 2]
    });
  }

  // -- Popup HTML -----------------------------------------------------------
  function _buildPopup(ac) {
    var name = esc(ac.callsign || ac.icao);
    var h = '<div class="adsb-popup">';
    h += '<div class="adsb-popup-name">' + name;
    if (_isEmergency(ac.squawk)) {
      h += ' <span class="adsb-emergency-tag">' + esc(_EMERGENCY_SQUAWKS[ac.squawk]) + '</span>';
    }
    h += '</div>';
    h += '<div class="adsb-popup-grid">';
    h += _row('ICAO', '<span class="addr">' + esc(ac.icao) + '</span>');
    if (ac.callsign) h += _row('Callsign', esc(ac.callsign));
    if (ac.altitude != null) h += _row('Altitude', ac.altitude.toLocaleString() + ' ft (' + _altBand(ac.altitude) + ')');
    if (ac.ground_speed != null) h += _row('Speed', ac.ground_speed.toFixed(0) + ' kts');
    if (ac.track != null) h += _row('Track', ac.track.toFixed(0) + '°');
    if (ac.vertical_rate != null) h += _row('V/S', ac.vertical_rate.toLocaleString() + ' ft/min');
    if (ac.squawk) h += _row('Squawk', esc(ac.squawk));
    if (ac.distance_nm != null) h += _row('Distance', ac.distance_nm.toFixed(1) + ' nm');
    h += _row('Last Seen', formatTimeAgo(ac.last_seen));
    h += _row('Messages', String(ac.message_count));
    h += '</div>';

    // Photo placeholder
    h += '<div class="adsb-popup-photo" id="adsb-photo-' + esc(ac.icao) + '">';
    var cached = _photoCache[ac.icao];
    if (cached && cached.url) {
      h += '<img src="' + esc(cached.url) + '" alt="Aircraft photo" />';
    } else if (cached && cached.url === null) {
      // Already checked, no photo
    } else {
      h += '<span class="adsb-photo-loading">Loading photo…</span>';
    }
    h += '</div>';
    h += '</div>';
    return h;
  }

  function _row(label, value) {
    return '<div class="adsb-popup-item">'
      + '<span class="adsb-popup-label">' + label + '</span>'
      + '<span class="adsb-popup-value">' + value + '</span>'
      + '</div>';
  }

  // -- Photo fetch (planespotters.net) --------------------------------------
  function _fetchPhoto(icao) {
    if (_photoCache[icao]) return;
    _photoCache[icao] = { url: undefined, fetched: false };
    var url = 'https://api.planespotters.net/pub/photos/hex/' + encodeURIComponent(icao);
    var xhr = new XMLHttpRequest();
    xhr.open('GET', url);
    xhr.onload = function () {
      try {
        var resp = JSON.parse(xhr.responseText);
        var photos = resp.photos || [];
        var thumbUrl = (photos[0] && photos[0].thumbnail_large && photos[0].thumbnail_large.src)
          || (photos[0] && photos[0].thumbnail && photos[0].thumbnail.src)
          || null;
        _photoCache[icao] = { url: thumbUrl, fetched: true };
        var el = document.getElementById('adsb-photo-' + icao);
        if (el && thumbUrl) {
          el.innerHTML = '<img src="' + esc(thumbUrl) + '" alt="Aircraft photo" />';
        } else if (el) {
          el.innerHTML = '';
        }
      } catch (e) {
        _photoCache[icao] = { url: null, fetched: true };
      }
    };
    xhr.onerror = function () {
      _photoCache[icao] = { url: null, fetched: true };
    };
    xhr.send();
  }

  // -- Map initialization ---------------------------------------------------
  function _initMap() {
    if (_map) return;
    if (!_mapContainer) return;

    _map = L.map(_mapContainer, {
      zoomControl: true,
      attributionControl: true
    }).setView([39.8, -98.6], 4);

    var tileMeta = document.querySelector('meta[name="rpi-tile-url"]');
    var tileUrl = (tileMeta && tileMeta.content) || 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png';
    L.tileLayer(tileUrl, {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 19
    }).addTo(_map);

    _markerGroup = L.featureGroup().addTo(_map);
  }

  // -- DOM setup ------------------------------------------------------------
  function _resolveDom() {
    if (_section) return true;
    _section = $('adsb-section');
    if (!_section) return false;
    _body = $('adsb-body');
    _toggle = $('adsb-toggle');
    _countEl = $('adsb-count');
    _statsEl = $('adsb-stats');
    _mapContainer = $('adsb-map-container');
    _tableBody = $('adsb-table-body');
    return true;
  }

  function _wireToggle() {
    if (!_toggle || !_body) return;
    _toggle.addEventListener('click', function () {
      _expanded = !_expanded;
      _body.classList.toggle('hidden', !_expanded);
      var chev = _toggle.querySelector('.chevron');
      if (chev) chev.textContent = _expanded ? '▾' : '▶';
      if (_expanded && !_map && _mapContainer) _initMap();
      if (_expanded && _map) {
        setTimeout(function () { _map.invalidateSize(); }, 200);
      }
      if (_expanded && _lastData) _render(_lastData);
    });
  }

  function _wireSortHeaders() {
    var headers = document.querySelectorAll('#adsb-table th[data-sort]');
    for (var i = 0; i < headers.length; i++) {
      (function (th) {
        th.addEventListener('click', function () {
          var col = th.getAttribute('data-sort');
          if (_sortCol === col) {
            _sortAsc = !_sortAsc;
          } else {
            _sortCol = col;
            _sortAsc = (col === 'callsign' || col === 'icao');
          }
          // Update sort indicators
          var all = document.querySelectorAll('#adsb-table th[data-sort]');
          for (var j = 0; j < all.length; j++) {
            all[j].classList.remove('sort-asc', 'sort-desc');
          }
          th.classList.add(_sortAsc ? 'sort-asc' : 'sort-desc');
          if (_lastData) _renderTable(_lastData.aircraft || []);
        });
      })(headers[i]);
    }
  }

  // -- Render ---------------------------------------------------------------
  function _render(data) {
    if (!_section) return;
    var aircraft = data.aircraft || [];
    var stats = data.stats || {};

    // Show section
    _section.style.display = '';

    // Stats bar
    if (_statsEl) {
      var parts = [];
      parts.push('<strong>' + aircraft.length + '</strong>&nbsp;aircraft');
      parts.push(stats.total_messages ? stats.total_messages.toLocaleString() + ' msgs' : '');
      parts.push(stats.aircraft_seen_total ? stats.aircraft_seen_total + ' seen total' : '');
      if (stats.receiver_lat != null && stats.receiver_lon != null) {
        parts.push('Rx: ' + stats.receiver_lat.toFixed(4) + ', ' + stats.receiver_lon.toFixed(4));
      }
      if (data.status && data.status !== 'running') {
        var label = esc(data.status);
        if (data.error) label += ': ' + esc(data.error);
        parts.push('<span class="adsb-status-' + esc(data.status) + '">' + label + '</span>');
      }
      _statsEl.innerHTML = parts.filter(Boolean).join(' &middot; ');
    }

    // Count badge
    if (_countEl) {
      _countEl.textContent = aircraft.length + ' aircraft';
    }

    // Map markers
    if (_map && _markerGroup) {
      _renderMap(aircraft, stats);
    }

    // Table
    _renderTable(aircraft);
  }

  function _renderMap(aircraft, stats) {
    var currentKeys = {};
    for (var i = 0; i < aircraft.length; i++) {
      var ac = aircraft[i];
      if (ac.latitude == null || ac.longitude == null) continue;
      currentKeys[ac.icao] = true;

      var icon = _makeIcon(ac.track, ac.altitude, ac.squawk);
      var existing = _markers[ac.icao];
      if (existing) {
        existing.setLatLng([ac.latitude, ac.longitude]);
        existing.setIcon(icon);
        if (existing.getPopup() && existing.getPopup().isOpen()) {
          existing.setPopupContent(_buildPopup(ac));
        }
      } else {
        var marker = L.marker([ac.latitude, ac.longitude], {
          icon: icon,
          title: ac.callsign || ac.icao
        });
        marker.bindPopup(_buildPopup(ac), { maxWidth: 320 });
        marker.on('popupopen', (function (icao) {
          return function () { _fetchPhoto(icao); };
        })(ac.icao));
        _markerGroup.addLayer(marker);
        _markers[ac.icao] = marker;
      }
    }

    // Remove stale markers
    for (var k in _markers) {
      if (!currentKeys[k]) {
        _markerGroup.removeLayer(_markers[k]);
        delete _markers[k];
      }
    }

    // Receiver marker
    if (stats.receiver_lat != null && stats.receiver_lon != null) {
      if (!_receiverMarker) {
        _receiverMarker = L.marker([stats.receiver_lat, stats.receiver_lon], {
          icon: L.divIcon({
            className: 'adsb-receiver-marker',
            iconSize: [16, 16],
            iconAnchor: [8, 8]
          }),
          title: 'Receiver',
          zIndexOffset: -100
        });
        _markerGroup.addLayer(_receiverMarker);
      } else {
        _receiverMarker.setLatLng([stats.receiver_lat, stats.receiver_lon]);
      }
    }

    // Fit bounds on first load
    if (!_initialFit && _markerGroup.getLayers().length > 0) {
      _map.fitBounds(_markerGroup.getBounds().pad(0.15));
      _initialFit = true;
    }
  }

  function _renderTable(aircraft) {
    if (!_tableBody) return;
    var sorted = aircraft.slice().sort(function (a, b) {
      var va = a[_sortCol], vb = b[_sortCol];
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (typeof va === 'string') va = va.toLowerCase();
      if (typeof vb === 'string') vb = vb.toLowerCase();
      if (va < vb) return _sortAsc ? -1 : 1;
      if (va > vb) return _sortAsc ? 1 : -1;
      return 0;
    });

    var html = '';
    for (var i = 0; i < sorted.length; i++) {
      var ac = sorted[i];
      var emergency = _isEmergency(ac.squawk);
      var cls = emergency ? ' class="adsb-emergency-row"' : '';
      html += '<tr' + cls + ' data-icao="' + esc(ac.icao) + '">';
      html += '<td>' + esc(ac.callsign || '--') + '</td>';
      html += '<td class="addr">' + esc(ac.icao) + '</td>';
      html += '<td>' + (ac.altitude != null ? ac.altitude.toLocaleString() : '--') + '</td>';
      html += '<td>' + (ac.ground_speed != null ? ac.ground_speed.toFixed(0) : '--') + '</td>';
      html += '<td>' + (ac.track != null ? ac.track.toFixed(0) + '°' : '--') + '</td>';
      html += '<td>' + (ac.squawk ? esc(ac.squawk) : '--');
      if (emergency) html += ' <span class="adsb-emergency-tag">' + esc(_EMERGENCY_SQUAWKS[ac.squawk]) + '</span>';
      html += '</td>';
      html += '<td>' + (ac.distance_nm != null ? ac.distance_nm.toFixed(1) : '--') + '</td>';
      html += '<td>' + formatTimeAgo(ac.last_seen) + '</td>';
      html += '</tr>';
    }
    _tableBody.innerHTML = html;

    // Wire click-to-center
    var rows = _tableBody.querySelectorAll('tr[data-icao]');
    for (var j = 0; j < rows.length; j++) {
      (function (row) {
        row.addEventListener('click', function () {
          var icao = row.getAttribute('data-icao');
          var m = _markers[icao];
          if (m && _map) {
            _map.setView(m.getLatLng(), Math.max(_map.getZoom(), 10));
            m.openPopup();
          }
        });
      })(rows[j]);
    }
  }

  // -- Health badge & sparkline ---------------------------------------------
  function _healthBadge(data) {
    var s = data.stats || {};
    var status = data.status || 'unknown';
    var cls = 'adsb-badge ';
    var label = status;
    if (status === 'running') { cls += 'adsb-badge-ok'; label = 'Healthy'; }
    else if (status === 'exhausted') { cls += 'adsb-badge-err adsb-pulse'; label = 'Exhausted'; }
    else if (status === 'restarting') { cls += 'adsb-badge-warn'; label = 'Restarting'; }
    else if (status === 'error') { cls += 'adsb-badge-err'; label = 'Error'; }
    else { cls += 'adsb-badge-off'; }

    var rates = s.msg_rate_history || [];
    var rate = rates.length ? rates[rates.length - 1] : 0;

    var parts = [];
    parts.push('<span class="' + cls + '">' + esc(label) + '</span>');
    parts.push('<span class="adsb-stat">' + rate.toFixed(1) + ' msg/s</span>');
    if (s.restart_count > 0) {
      parts.push('<span class="adsb-stat adsb-stat-warn">restarts: ' + s.restart_count + '</span>');
    }
    if (s.dongle_uptime != null) {
      var m = Math.floor(s.dongle_uptime / 60);
      parts.push('<span class="adsb-stat">up ' + (m >= 60 ? Math.floor(m / 60) + 'h ' + (m % 60) + 'm' : m + 'm') + '</span>');
    }
    parts.push(_sparkline(rates));
    return parts.join(' ');
  }

  function _sparkline(values) {
    if (!values || values.length < 2) return '';
    var w = 120, h = 28, pad = 2;
    var max = 0;
    for (var i = 0; i < values.length; i++) { if (values[i] > max) max = values[i]; }
    if (max === 0) max = 1;
    var pts = [];
    for (var j = 0; j < values.length; j++) {
      var x = pad + (j / (values.length - 1)) * (w - 2 * pad);
      var y = h - pad - ((values[j] / max) * (h - 2 * pad));
      pts.push(x.toFixed(1) + ',' + y.toFixed(1));
    }
    var last = values[values.length - 1];
    var color = last > 5 ? '#4caf50' : last > 1 ? '#ff9800' : '#e53935';
    return '<svg class="adsb-sparkline" width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '">'
      + '<polyline fill="none" stroke="' + color + '" stroke-width="1.5" points="' + pts.join(' ') + '"/>'
      + '</svg>';
  }

  // -- Public API -----------------------------------------------------------
  function update(data) {
    _lastData = data;
    if (!_resolveDom()) return;

    // Show/hide section based on status
    if (data.status === 'unavailable' && (!data.aircraft || data.aircraft.length === 0)) {
      _section.style.display = 'none';
      return;
    }
    _section.style.display = '';

    if (!_toggle._wired) {
      _wireToggle();
      _wireSortHeaders();
      _wireControls();
      _toggle._wired = true;
    }

    var aircraft = data.aircraft || [];
    if (_countEl) {
      _countEl.textContent = aircraft.length + ' aircraft';
    }

    if (_statsEl) {
      _statsEl.innerHTML = _healthBadge(data);
    }

    if (_body && _body.classList.contains('hidden')) return;
    _render(data);
  }

  // -- Map controls ---------------------------------------------------------
  function _wireControls() {
    var centerBtn = $('adsb-center-receiver');
    if (centerBtn) {
      centerBtn.addEventListener('click', function () {
        if (_lastData && _lastData.stats &&
            _lastData.stats.receiver_lat != null &&
            _lastData.stats.receiver_lon != null && _map) {
          _map.setView(
            [_lastData.stats.receiver_lat, _lastData.stats.receiver_lon], 11
          );
        }
      });
    }
  }

  // -- Handle resize --------------------------------------------------------
  window.addEventListener('resize', function () {
    if (_map) {
      setTimeout(function () { _map.invalidateSize(); }, 200);
    }
  });

  // -- Expose ---------------------------------------------------------------
  R.adsb = { update: update };
})();
