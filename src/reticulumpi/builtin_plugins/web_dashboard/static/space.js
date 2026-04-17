/* ReticulumPi Dashboard -- Space Tracker module
 *
 * Consumes the "space" key of the periodic WebSocket broadcast and renders:
 *   - summary tiles (sat count, overhead count, Kp index, next launch)
 *   - leaflet map with sub-satellite points and observer marker
 *   - upcoming-launch table
 *
 * All rendering is throttled to avoid work when the section is collapsed.
 */
(function () {
  'use strict';
  var R = window.RPI;
  if (!R) return;
  var $ = R.$, esc = R.esc;

  // -- DOM handles (resolved on first data arrival) ------------------------
  var _section = null;
  var _body = null;
  var _toggle = null;
  var _countEl = null;
  var _satCountEl, _overheadEl, _overheadNamesEl, _groupsSubEl;
  var _kpEl, _kpTimeEl;
  var _nextLaunchEl, _nextLaunchWhenEl;
  var _launchTableEl;

  // -- Map state -----------------------------------------------------------
  var _map = null;
  var _satLayer = null;
  var _observerMarker = null;
  var _satMarkers = {};     // name -> L.CircleMarker
  var _mapVisible = false;

  // -- Collapse state ------------------------------------------------------
  var _expanded = false;

  // -- Helpers -------------------------------------------------------------
  function _resolveDom() {
    if (_section) return true;
    _section = $('space-section');
    if (!_section) return false;
    _body = $('space-body');
    _toggle = $('space-toggle');
    _countEl = $('space-count');
    _satCountEl = $('space-sat-count');
    _overheadEl = $('space-overhead');
    _overheadNamesEl = $('space-overhead-names');
    _groupsSubEl = $('space-groups-sub');
    _kpEl = $('space-kp');
    _kpTimeEl = $('space-kp-time');
    _nextLaunchEl = $('space-next-launch');
    _nextLaunchWhenEl = $('space-next-launch-when');
    _launchTableEl = $('space-launch-table');

    if (_toggle) {
      _toggle.addEventListener('click', _onToggleClick);
    }
    return true;
  }

  function _onToggleClick() {
    _expanded = !_expanded;
    _body.classList.toggle('hidden', !_expanded);
    var chev = _toggle.querySelector('.chevron');
    if (chev) chev.innerHTML = _expanded ? '&#9662;' : '&#9656;';
    if (_expanded) {
      _initMapIfNeeded();
      // Leaflet needs a size refresh after being unhidden
      setTimeout(function () { if (_map) _map.invalidateSize(); }, 30);
    }
  }

  function _initMapIfNeeded() {
    if (_map) return;
    if (typeof L === 'undefined') return;
    var container = $('space-map-container');
    if (!container) return;
    _map = L.map(container, {
      zoomControl: true,
      attributionControl: true,
      worldCopyJump: true
    }).setView([20, 0], 2);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; OpenStreetMap',
      maxZoom: 6
    }).addTo(_map);
    _satLayer = L.layerGroup().addTo(_map);
    _mapVisible = true;
  }

  // -- Time formatting -----------------------------------------------------
  function _formatCountdown(iso) {
    if (!iso) return '';
    var target = Date.parse(iso);
    if (isNaN(target)) return iso;
    var delta = Math.floor((target - Date.now()) / 1000);
    var sign = delta < 0 ? '+' : '-';
    delta = Math.abs(delta);
    var d = Math.floor(delta / 86400); delta -= d * 86400;
    var h = Math.floor(delta / 3600);  delta -= h * 3600;
    var m = Math.floor(delta / 60);
    if (d > 0) return 'T' + sign + d + 'd ' + h + 'h';
    if (h > 0) return 'T' + sign + h + 'h ' + m + 'm';
    return 'T' + sign + m + 'm';
  }

  // -- Main update entry point (called from app.js on each WS message) ----
  function update(space) {
    if (!space) return;
    if (!_resolveDom()) return;

    _section.style.display = '';

    var groups = space.tle_groups || {};
    var totalSats = 0;
    var groupNames = [];
    for (var g in groups) {
      if (Object.prototype.hasOwnProperty.call(groups, g)) {
        totalSats += groups[g];
        groupNames.push(g + ' (' + groups[g] + ')');
      }
    }
    if (_satCountEl) _satCountEl.textContent = totalSats;
    if (_groupsSubEl) _groupsSubEl.textContent = groupNames.join(', ');
    if (_countEl) _countEl.textContent = totalSats;

    var positions = space.positions;
    var objects = (positions && positions.objects) || [];
    var overhead = 0;
    var overheadNames = [];
    for (var i = 0; i < objects.length; i++) {
      if (typeof objects[i].el === 'number' && objects[i].el >= 10) {
        overhead++;
        if (overheadNames.length < 3) overheadNames.push(objects[i].name);
      }
    }
    if (_overheadEl) {
      _overheadEl.textContent = space.observer ? overhead : '—';
    }
    if (_overheadNamesEl) {
      _overheadNamesEl.textContent = space.observer
        ? (overheadNames.join(', ') || 'none')
        : '(no observer location)';
    }

    var weather = space.weather;
    if (_kpEl) {
      _kpEl.textContent = weather && weather.kp != null
        ? weather.kp.toFixed(1)
        : '—';
    }
    if (_kpTimeEl) {
      _kpTimeEl.textContent = weather && weather.time_tag
        ? weather.time_tag.replace('T', ' ').slice(0, 16) + ' UTC'
        : '';
    }

    var launches = space.launches || [];
    var nextLaunch = launches[0];
    if (_nextLaunchEl) {
      _nextLaunchEl.textContent = nextLaunch ? (nextLaunch.name || '—') : '—';
    }
    if (_nextLaunchWhenEl) {
      _nextLaunchWhenEl.textContent = nextLaunch
        ? (_formatCountdown(nextLaunch.net) + '  ·  ' + (nextLaunch.provider || ''))
        : '';
    }

    if (_launchTableEl) {
      var rows = '';
      for (var j = 0; j < launches.length; j++) {
        var L_ = launches[j];
        rows += '<tr>' +
          '<td>' + esc(_formatCountdown(L_.net)) + '</td>' +
          '<td>' + esc(L_.name || '—') + '</td>' +
          '<td>' + esc(L_.provider || '') + '</td>' +
          '<td>' + esc(L_.pad || '') + '</td>' +
          '</tr>';
      }
      _launchTableEl.innerHTML = rows || '<tr><td colspan="4">No upcoming launches loaded</td></tr>';
    }

    // Only touch the map when it's visible — heavy work otherwise wasted
    if (_expanded && _map) {
      _renderMap(objects, space.observer);
    }
  }

  function _renderMap(objects, observer) {
    if (!_map || !_satLayer) return;

    // Observer marker
    if (observer && observer.lat != null && observer.lon != null) {
      if (!_observerMarker) {
        _observerMarker = L.circleMarker([observer.lat, observer.lon], {
          radius: 7,
          color: '#ff5a1f',
          fillColor: '#ff8c42',
          fillOpacity: 0.9,
          weight: 2
        }).addTo(_map).bindPopup('Observer');
      } else {
        _observerMarker.setLatLng([observer.lat, observer.lon]);
      }
    }

    // Diff-update satellite markers
    var seen = {};
    for (var i = 0; i < objects.length; i++) {
      var o = objects[i];
      if (o.lat == null || o.lon == null) continue;
      seen[o.name] = true;
      var existing = _satMarkers[o.name];
      if (existing) {
        existing.setLatLng([o.lat, o.lon]);
      } else {
        var m = L.circleMarker([o.lat, o.lon], {
          radius: 3,
          color: '#6aa9ff',
          fillColor: '#6aa9ff',
          fillOpacity: 0.8,
          weight: 1
        });
        m.bindPopup(_satPopup(o));
        m.addTo(_satLayer);
        _satMarkers[o.name] = m;
      }
    }

    // Evict markers for objects no longer in the snapshot
    for (var name in _satMarkers) {
      if (!seen[name]) {
        _satLayer.removeLayer(_satMarkers[name]);
        delete _satMarkers[name];
      }
    }
  }

  function _satPopup(o) {
    var h = '<div class="map-popup">';
    h += '<div class="map-popup-name">' + esc(o.name) + '</div>';
    h += '<div class="map-popup-grid">';
    h += '<div><b>Lat/Lon:</b> ' + o.lat.toFixed(2) + ', ' + o.lon.toFixed(2) + '</div>';
    if (o.alt_km != null) h += '<div><b>Alt:</b> ' + o.alt_km.toFixed(0) + ' km</div>';
    if (o.el != null) h += '<div><b>Elev:</b> ' + o.el.toFixed(1) + '&deg;</div>';
    if (o.az != null) h += '<div><b>Az:</b> ' + o.az.toFixed(1) + '&deg;</div>';
    h += '</div></div>';
    return h;
  }

  // Public surface
  R.space = {
    update: update
  };
})();
