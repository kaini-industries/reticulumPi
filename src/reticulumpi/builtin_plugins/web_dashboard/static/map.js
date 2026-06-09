/* ReticulumPi Dashboard -- Node Map module (Meshtastic + MeshCore + Reticulum) */
(function () {
  'use strict';
  var R = window.RPI;
  var api = R.api, $ = R.$, esc = R.esc;
  var formatTimeAgo = R.formatTimeAgo;

  var _map = null;           // Leaflet map instance
  var _markers = {};         // _key -> L.Marker (_key namespaces source)
  var _markerGroup = null;   // L.FeatureGroup for fitBounds
  var _mshNodes = [];        // Meshtastic nodes
  var _mcContacts = [];      // MeshCore contacts
  var _rnsPeers = [];        // Reticulum mesh peers
  var _filter = 'lora';      // 'all', 'lora', 'rns', or 'tracked'
  var _loraNeighborIds = {}; // {id: true} for Meshtastic LoRa neighbor filter
  var _initialFit = false;   // whether we've done the first fitBounds
  var _gpsPos = null;        // [lat, lng] from attached GPS unit
  var _gpsMarker = null;     // L.circleMarker for local GPS position
  var _gpsAccuracy = null;   // L.circle accuracy ring
  var _gpsFix = null;        // full fix object {lat, lon, hdop, alt_m}
  var _gpsLive = false;      // true after first live GPS fix (not cached)
  var _hasLiveData = false;  // true after first WS/API data arrives
  var _idbSaveTimer = null;  // debounce timer for IndexedDB writes
  var _trails = {};
  var _trailCache = null;
  var _trailHours = 24;
  var _trailsEnabled = false;
  var _trailRefreshTimer = null;
  var _trailFetching = false;

  // -- IndexedDB persistence for last-known positions ----------------------

  var IDB_NAME = 'rpi-positions';
  var IDB_STORE = 'nodes';
  var IDB_VERSION = 1;
  var IDB_SAVE_INTERVAL = 30000; // write at most every 30s
  var _idb = null;
  var _idbDirty = false;
  var _idbNodes = [];

  function _openIDB(cb) {
    if (_idb) { cb(_idb); return; }
    if (!window.indexedDB) return;
    var req = indexedDB.open(IDB_NAME, IDB_VERSION);
    req.onupgradeneeded = function (e) {
      var db = e.target.result;
      if (!db.objectStoreNames.contains(IDB_STORE)) {
        db.createObjectStore(IDB_STORE, { keyPath: '_key' });
      }
    };
    req.onsuccess = function (e) { _idb = e.target.result; cb(_idb); };
    req.onerror = function () {};
  }

  function _loadPositions() {
    _openIDB(function (db) {
      var tx = db.transaction(IDB_STORE, 'readonly');
      var store = tx.objectStore(IDB_STORE);
      var req = store.getAll();
      req.onsuccess = function () {
        var records = req.result;
        if (!records || !records.length || _hasLiveData) return;
        _idbNodes = records;
        _render();
      };
    });
  }

  function _scheduleSave() {
    _idbDirty = true;
    if (_idbSaveTimer) return;
    _idbSaveTimer = setTimeout(function () {
      _idbSaveTimer = null;
      if (!_idbDirty) return;
      _idbDirty = false;
      _savePositions();
    }, IDB_SAVE_INTERVAL);
  }

  function _savePositions() {
    _openIDB(function (db) {
      var nodes = _allNodes();
      var tx = db.transaction(IDB_STORE, 'readwrite');
      var store = tx.objectStore(IDB_STORE);
      store.clear();
      for (var i = 0; i < nodes.length; i++) {
        var n = nodes[i];
        if (!_hasValidPos(n)) continue;
        store.put({
          _key: n._key, source: n.source,
          latitude: n.latitude, longitude: n.longitude,
          long_name: n.long_name, short_name: n.short_name,
          last_heard: n.last_heard, id: n.id
        });
      }
    });
  }

  // -- GPS position localStorage cache -------------------------------------

  var GPS_CACHE_KEY = 'rpi-gps-pos';

  function _saveGpsCache(fix) {
    try {
      localStorage.setItem(GPS_CACHE_KEY, JSON.stringify({
        lat: fix.lat, lon: fix.lon, hdop: fix.hdop || null
      }));
    } catch (e) {}
  }

  function _loadGpsCache() {
    try {
      var raw = localStorage.getItem(GPS_CACHE_KEY);
      if (!raw) return;
      var cached = JSON.parse(raw);
      if (cached && cached.lat != null && cached.lon != null) {
        _gpsPos = [cached.lat, cached.lon];
        _gpsFix = cached;
      }
    } catch (e) {}
  }

  function _ensureGpsMarker(latlng, opacity) {
    if (!_map) return;
    if (!_gpsMarker) {
      _gpsMarker = L.circleMarker(latlng, {
        radius: 8,
        color: '#4285f4',
        fillColor: '#4285f4',
        fillOpacity: 0.9 * opacity,
        weight: 2,
        opacity: opacity
      }).addTo(_map);
      _gpsMarker.bindPopup('', { maxWidth: 240 });
    } else {
      _gpsMarker.setLatLng(latlng);
      _gpsMarker.setStyle({
        fillOpacity: 0.9 * opacity,
        opacity: opacity
      });
    }
    _updateGpsPopup();
  }

  function _ensureGpsAccuracy(latlng, hdop, opacity) {
    if (!_map) return;
    var radiusM = (hdop || 10) * 5;
    if (!_gpsAccuracy) {
      _gpsAccuracy = L.circle(latlng, {
        radius: radiusM,
        color: '#4285f4',
        weight: 1,
        opacity: 0.3 * opacity,
        fillColor: '#4285f4',
        fillOpacity: 0.08 * opacity,
        interactive: false
      }).addTo(_map);
    } else {
      _gpsAccuracy.setLatLng(latlng);
      _gpsAccuracy.setRadius(radiusM);
      _gpsAccuracy.setStyle({
        opacity: 0.3 * opacity,
        fillOpacity: 0.08 * opacity
      });
    }
  }

  function _updateGpsPopup() {
    if (!_gpsMarker || !_gpsFix) return;
    var h = '<div class="map-popup">';
    h += '<div class="map-popup-name">My Location';
    if (!_gpsLive) h += ' <span class="map-source-tag">Cached</span>';
    h += '</div><div class="map-popup-grid">';
    h += _popupRow('Position',
      Number(_gpsFix.lat).toFixed(5) + ', ' + Number(_gpsFix.lon).toFixed(5));
    if (_gpsFix.alt_m != null) {
      h += _popupRow('Altitude', Number(_gpsFix.alt_m).toFixed(1) + ' m');
    }
    if (_gpsFix.hdop != null) {
      h += _popupRow('HDOP', Number(_gpsFix.hdop).toFixed(1));
    }
    h += '</div></div>';
    _gpsMarker.setPopupContent(h);
  }

  function _updateGpsButton(hasFix) {
    var btn = $('map-center-gps');
    if (!btn) return;
    if (hasFix) {
      btn.classList.add('gps-has-fix');
    } else {
      btn.classList.remove('gps-has-fix');
    }
  }

  // -- Custom marker icons -------------------------------------------------

  var _iconDefault = null;
  var _iconSelf = null;
  var _iconMeshCore = null;
  var _iconReticulum = null;

  function _initIcons() {
    _iconDefault = new L.DivIcon({
      className: 'map-marker-meshtastic',
      iconSize: [14, 14],
      iconAnchor: [7, 7],
      popupAnchor: [0, -10]
    });

    _iconSelf = new L.DivIcon({
      className: 'map-marker-self',
      iconSize: [18, 18],
      iconAnchor: [9, 9],
      popupAnchor: [0, -12]
    });

    _iconMeshCore = new L.DivIcon({
      className: 'map-marker-meshcore',
      iconSize: [14, 14],
      iconAnchor: [7, 7],
      popupAnchor: [0, -10]
    });

    _iconReticulum = new L.DivIcon({
      className: 'map-marker-reticulum',
      iconSize: [14, 14],
      iconAnchor: [7, 7],
      popupAnchor: [0, -10]
    });
  }

  // -- Map initialization --------------------------------------------------

  var _tileErrors = 0;

  function _initMap() {
    if (_map) return;
    var container = $('map-container');
    if (!container || typeof L === 'undefined') return;

    _initIcons();

    _map = L.map(container, {
      zoomControl: true,
      attributionControl: true
    }).setView([39.8, -98.6], 4);   // default: center of US

    var tileMeta = document.querySelector('meta[name="rpi-tile-url"]');
    var tileUrl = (tileMeta && tileMeta.content) || 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png';
    var tl = L.tileLayer(tileUrl, {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 19
    }).addTo(_map);
    tl.on('tileerror', function () {
      _tileErrors++;
      if (_tileErrors === 1) {
        var el = $('map-stats');
        if (el) el.textContent += ' · tile load error';
      }
    });

    _markerGroup = L.featureGroup().addTo(_map);
  }

  // -- Popup HTML ----------------------------------------------------------

  // MeshCore contact type codes (from MeshCore protocol)
  var _MC_TYPE_LABELS = {
    1: 'Companion',
    2: 'Repeater',
    3: 'Room Server'
  };

  function _buildPopup(node) {
    var name = esc(node.long_name || node.short_name || '--');
    var h = '<div class="map-popup">';
    h += '<div class="map-popup-name">' + name;
    if (node.is_self) h += ' <span class="msh-self-tag">SELF</span>';
    if (node.source === 'meshcore') h += ' <span class="map-source-tag mc">MeshCore</span>';
    else if (node.source === 'reticulum') h += ' <span class="map-source-tag rns">Reticulum</span>';
    else h += ' <span class="map-source-tag msh">Meshtastic</span>';
    h += '</div>';
    h += '<div class="map-popup-grid">';
    if (node.source === 'meshcore') {
      var pk = String(node.public_key || '');
      var pkShort = pk ? (pk.slice(0, 8) + '…' + pk.slice(-4)) : '--';
      h += _popupRow('Key', '<span class="addr">' + esc(pkShort) + '</span>');
      h += _popupRow('Type', esc(_MC_TYPE_LABELS[node.type] || ('type ' + node.type)));
      h += _popupRow('Last Advert', formatTimeAgo(node.last_heard));
    } else if (node.source === 'reticulum') {
      var hash = String(node.id || '');
      var hashShort = hash.length > 12 ? (hash.slice(0, 8) + '…' + hash.slice(-4)) : hash;
      h += _popupRow('Hash', '<span class="addr">' + esc(hashShort) + '</span>');
      if (node.hops != null) h += _popupRow('Hops', String(node.hops));
      if (node.cpu != null) h += _popupRow('CPU', node.cpu.toFixed(1) + '%');
      if (node.mem != null) h += _popupRow('Mem', node.mem.toFixed(1) + '%');
      h += _popupRow('Last Seen', formatTimeAgo(node.last_heard));
    } else {
      h += _popupRow('ID', '<span class="addr">' + esc(String(node.id || '--')) + '</span>');
      var viaPills = _heardViaPills(node);
      if (viaPills) h += _popupRow('Heard via', viaPills);
      h += _popupRow('Hardware', esc(node.hw_model || '--'));
      if (node.snr != null) {
        h += _popupRow('SNR', node.snr.toFixed(1) + ' dB');
      }
      h += _popupRow('Last Heard', formatTimeAgo(node.last_heard));
    }
    h += _popupRow('Position', node.latitude.toFixed(5) + ', ' + node.longitude.toFixed(5));
    h += '</div></div>';
    return h;
  }

  function _heardViaPills(node) {
    var parts = [];
    if (node.via_lora) parts.push('<span class="map-source-tag lora">LoRa</span>');
    if (node.via_mqtt) parts.push('<span class="map-source-tag mqtt">MQTT</span>');
    return parts.length ? parts.join(' ') : '';
  }

  function _popupRow(label, value) {
    return '<div class="map-popup-item">'
      + '<span class="map-popup-label">' + label + '</span>'
      + '<span class="map-popup-value">' + value + '</span>'
      + '</div>';
  }

  // -- Source normalization ------------------------------------------------

  function _normalizeMeshtastic(n) {
    n.source = 'meshtastic';
    n._key = 'msh:' + n.id;
    return n;
  }

  function _normalizeMeshCore(c) {
    return {
      source: 'meshcore',
      _key: 'mc:' + c.public_key,
      id: c.public_key,
      public_key: c.public_key,
      long_name: c.name,
      short_name: c.name,
      latitude: c.latitude,
      longitude: c.longitude,
      last_heard: c.last_advert,
      type: c.type,
      flags: c.flags,
      is_self: false
    };
  }

  function _normalizeReticulum(p) {
    return {
      source: 'reticulum',
      _key: 'rns:' + p.destination_hash,
      id: p.destination_hash,
      long_name: p.name || p.destination_hash,
      short_name: p.name || '',
      latitude: p.lat,
      longitude: p.lon,
      last_heard: p.last_seen,
      hops: p.hops,
      cpu: p.cpu,
      mem: p.mem,
      temp: p.temp,
      is_self: false
    };
  }

  function _allNodes() {
    var out = [];
    for (var i = 0; i < _mshNodes.length; i++) {
      out.push(_normalizeMeshtastic(_mshNodes[i]));
    }
    for (var j = 0; j < _mcContacts.length; j++) {
      out.push(_normalizeMeshCore(_mcContacts[j]));
    }
    for (var k = 0; k < _rnsPeers.length; k++) {
      out.push(_normalizeReticulum(_rnsPeers[k]));
    }
    for (var n = 0; n < _idbNodes.length; n++) {
      out.push(_idbNodes[n]);
    }
    return out;
  }

  function _iconFor(node) {
    if (node.is_self) return _iconSelf;
    if (node.source === 'meshcore') return _iconMeshCore;
    if (node.source === 'reticulum') return _iconReticulum;
    return _iconDefault;
  }

  // -- Marker update logic -------------------------------------------------

  function _hasValidPos(n) {
    return n.latitude != null && n.longitude != null
      && !(n.latitude === 0 && n.longitude === 0);
  }

  function _render() {
    if (!_map) _initMap();
    if (!_map || !_markerGroup) return;

    var nodes = _allNodes();

    // Filter to nodes with valid position.  "lora" filter keeps
    // Meshtastic self/neighbors + ALL MeshCore contacts (MeshCore is
    // always LoRa).
    var withPos = [];
    var tIds = (_filter === 'tracked' && R.getTrackedNodeIds) ? R.getTrackedNodeIds() : null;
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (!_hasValidPos(n)) continue;
      if (_filter === 'lora') {
        var hasLoraData = false;
        for (var k in _loraNeighborIds) { hasLoraData = true; break; }
        var keep = (n.source === 'meshcore')
          || n.is_self
          || _loraNeighborIds[n.id]
          || (!hasLoraData && n.source === 'meshtastic');
        if (!keep) continue;
      } else if (_filter === 'rns') {
        if (n.source !== 'reticulum') continue;
      } else if (tIds) {
        if (!tIds[n.id] && !n.is_self) continue;
      }
      withPos.push(n);
    }

    // Update stats
    var totalWithPos = 0;
    for (var tp = 0; tp < nodes.length; tp++) {
      if (_hasValidPos(nodes[tp])) totalWithPos++;
    }
    var statsEl = $('map-stats');
    if (statsEl) {
      _tileErrors = 0;
      if (_filter === 'all') {
        statsEl.textContent = totalWithPos + ' of ' + nodes.length + ' with position';
      } else {
        statsEl.textContent = withPos.length + ' shown · ' + totalWithPos + ' with position';
      }
    }
    var countEl = $('map-count');
    if (countEl) {
      countEl.textContent = withPos.length + ' on map';
    }

    // Current keys for add/remove tracking
    var currentKeys = {};
    for (var j = 0; j < withPos.length; j++) {
      currentKeys[withPos[j]._key] = true;
    }

    // Remove stale markers
    for (var k in _markers) {
      if (!currentKeys[k]) {
        _markerGroup.removeLayer(_markers[k]);
        delete _markers[k];
      }
    }

    // Add or update markers
    for (var m = 0; m < withPos.length; m++) {
      var node = withPos[m];
      var icon = _iconFor(node);
      var existing = _markers[node._key];
      var fp = node.latitude + ',' + node.longitude + ',' + node.last_heard
        + ',' + (node.snr || '') + ',' + (node.long_name || '') + ',' + node.source;
      var opacity = _hasLiveData ? 1.0 : 0.5;
      if (existing) {
        if (existing._rpi_fp !== fp || existing._rpi_opacity !== opacity) {
          existing.setLatLng([node.latitude, node.longitude]);
          existing.setPopupContent(_buildPopup(node));
          existing.setIcon(icon);
          existing.setOpacity(opacity);
          existing._rpi_fp = fp;
          existing._rpi_opacity = opacity;
        }
      } else {
        var marker = L.marker([node.latitude, node.longitude], {
          icon: icon,
          opacity: opacity,
          title: node.long_name || node.short_name || node.id
        });
        marker.bindPopup(_buildPopup(node), { maxWidth: 280 });
        marker._rpi_fp = fp;
        marker._rpi_opacity = opacity;
        _markerGroup.addLayer(marker);
        _markers[node._key] = marker;
      }
    }

    if (!_initialFit) {
      if (_gpsPos) {
        _map.setView(_gpsPos, 13);
        _initialFit = true;
        setTimeout(function () { if (_map) _map.invalidateSize(); }, 100);
      } else if (_markerGroup.getLayers().length > 0) {
        var selfNode = null;
        for (var s = 0; s < withPos.length; s++) {
          if (withPos[s].is_self) { selfNode = withPos[s]; break; }
        }
        if (selfNode) {
          _map.setView([selfNode.latitude, selfNode.longitude], 13);
        } else {
          _map.fitBounds(_markerGroup.getBounds().pad(0.1));
        }
        _initialFit = true;
        setTimeout(function () { if (_map) _map.invalidateSize(); }, 100);
      }
    }
  }

  // -- Trail rendering ----------------------------------------------------

  function _clearTrails() {
    if (!_map) return;
    for (var k in _trails) {
      if (_trails[k]) _map.removeLayer(_trails[k]);
    }
    _trails = {};
  }

  function _fetchTrails() {
    if (!_map) return;
    var sources = R.getTrackedNodeSources ? R.getTrackedNodeSources() : {};
    var keys = [];
    for (var id in sources) {
      keys.push(sources[id] === 'meshcore' ? 'mc:' + id : 'msh:' + id);
    }
    if (!keys.length) { _clearTrails(); return; }
    if (_trailCache && (performance.now() - _trailCache.fetchedAt) < 15000
        && _trailCache.hours === _trailHours) {
      _renderTrails(_trailCache.data);
      return;
    }
    if (_trailFetching) return;
    _trailFetching = true;
    api('/api/node_tracker/history?nodes=' + encodeURIComponent(keys.join(','))
      + '&hours=' + _trailHours + '&limit=500').then(function (resp) {
      _trailFetching = false;
      if (resp && resp.ok && resp.data && resp.data.history) {
        _trailCache = { data: resp.data.history, fetchedAt: performance.now(), hours: _trailHours };
        _renderTrails(resp.data.history);
      } else {
        _clearTrails();
      }
    });
  }

  function _renderTrails(historyData) {
    _clearTrails();
    if (!_map) return;
    for (var nodeKey in historyData) {
      var points = historyData[nodeKey];
      if (!points || points.length < 2) continue;
      points.sort(function (a, b) { return a.timestamp - b.timestamp; });
      var color = nodeKey.indexOf('mc:') === 0 ? '#f59e0b' : '#67ea94';
      var group = L.layerGroup();
      var bucketSize = Math.max(1, Math.ceil(points.length / 4));
      var opacities = [0.2, 0.4, 0.6, 0.9];
      for (var b = 0; b < 4; b++) {
        var bStart = b * bucketSize;
        var bEnd = Math.min(bStart + bucketSize, points.length);
        if (bStart >= points.length) break;
        var segCoords = [];
        if (b > 0 && bStart > 0) {
          segCoords.push([points[bStart - 1].latitude, points[bStart - 1].longitude]);
        }
        for (var si = bStart; si < bEnd; si++) {
          segCoords.push([points[si].latitude, points[si].longitude]);
        }
        if (segCoords.length >= 2) {
          L.polyline(segCoords, { color: color, weight: 3, opacity: opacities[b] }).addTo(group);
        }
      }
      for (var j = 0; j < points.length; j++) {
        var p = points[j];
        var ts = new window.Date(p.timestamp * 1000).toLocaleString();
        var popupText = esc(p.name || nodeKey) + '<br>' + esc(ts);
        L.circleMarker([p.latitude, p.longitude], {
          radius: 3, color: color, fillColor: color, fillOpacity: 0.7, weight: 1
        }).bindPopup(popupText).addTo(group);
      }
      group.addTo(_map);
      _trails[nodeKey] = group;
    }
  }

  function _updateTrailRangeVisibility() {
    var rangeEl = document.getElementById('map-trail-range');
    if (rangeEl) {
      rangeEl.classList.toggle('hidden', !(_trailsEnabled && _filter === 'tracked'));
    }
  }

  function _startTrailRefresh() {
    if (_trailRefreshTimer) return;
    _trailRefreshTimer = setInterval(function () {
      if (_filter === 'tracked' && _trailsEnabled) _fetchTrails();
    }, 30000);
  }

  function _stopTrailRefresh() {
    if (_trailRefreshTimer) {
      clearInterval(_trailRefreshTimer);
      _trailRefreshTimer = null;
    }
  }

  // -- Public API ----------------------------------------------------------

  function updateMap(nodes) {
    _mshNodes = nodes || [];
    _hasLiveData = true;
    _idbNodes = [];
    if (!_map) _initMap();
    if (R.markUpdated) R.markUpdated('map-section');
    _render();
    _scheduleSave();
  }

  function updateMapMeshCore(contacts) {
    _mcContacts = contacts || [];
    _hasLiveData = true;
    _idbNodes = [];
    if (!_map) _initMap();
    if (R.markUpdated) R.markUpdated('map-section');
    _render();
    _scheduleSave();
  }

  function updateMapReticulum(peers) {
    _rnsPeers = peers || [];
    _hasLiveData = true;
    _idbNodes = [];
    if (!_map) _initMap();
    if (R.markUpdated) R.markUpdated('map-section');
    _render();
    _scheduleSave();
  }

  function updateMapLoraNeighbors(neighbors) {
    _loraNeighborIds = {};
    if (neighbors) {
      for (var i = 0; i < neighbors.length; i++) {
        if (neighbors[i].id) _loraNeighborIds[neighbors[i].id] = true;
      }
    }
    // Re-render if LoRa filter is active
    if (_filter === 'lora') _render();
  }

  function updateMapGps(fix) {
    if (!fix || fix.lat == null || fix.lon == null) return;
    if (!_map) _initMap();
    if (R.markUpdated) R.markUpdated('map-section');

    var latlng = [fix.lat, fix.lon];
    var wasLive = _gpsLive;
    var oldPos = _gpsPos;

    _gpsFix = fix;
    _gpsPos = latlng;
    _gpsLive = true;
    _saveGpsCache(fix);
    _updateGpsButton(true);

    _ensureGpsMarker(latlng, 1.0);
    _ensureGpsAccuracy(latlng, fix.hdop, 1.0);

    if (!oldPos) {
      if (_initialFit) {
        _map.flyTo(latlng, 13, { duration: 1.2 });
      } else {
        _map.setView(latlng, 13);
      }
      _initialFit = true;
    } else if (!wasLive) {
      _map.flyTo(latlng, 13, { duration: 1.2 });
    } else {
      var dLat = oldPos[0] - fix.lat, dLon = oldPos[1] - fix.lon;
      if (dLat * dLat + dLon * dLon > 0.0001) {
        _map.panTo(latlng, { animate: true });
      }
    }
  }

  // -- Filter tab wiring ---------------------------------------------------

  function _wireFilterTabs() {
    var allBtn = $('map-show-all');
    var loraBtn = $('map-show-lora');
    var rnsBtn = $('map-show-rns');
    var trackedBtn = $('map-show-tracked');
    var gpsBtn = $('map-center-gps');
    if (!allBtn || !loraBtn) return;

    function _clearActive() {
      allBtn.classList.remove('active');
      loraBtn.classList.remove('active');
      if (rnsBtn) rnsBtn.classList.remove('active');
      if (trackedBtn) trackedBtn.classList.remove('active');
    }

    allBtn.addEventListener('click', function () {
      _filter = 'all';
      _clearActive();
      allBtn.classList.add('active');
      _render();
      _clearTrails();
      _stopTrailRefresh();
      _updateTrailRangeVisibility();
    });
    loraBtn.addEventListener('click', function () {
      _filter = 'lora';
      _clearActive();
      loraBtn.classList.add('active');
      _render();
      _clearTrails();
      _stopTrailRefresh();
      _updateTrailRangeVisibility();
    });
    if (rnsBtn) {
      rnsBtn.addEventListener('click', function () {
        _filter = 'rns';
        _clearActive();
        rnsBtn.classList.add('active');
        _render();
        _clearTrails();
        _stopTrailRefresh();
        _updateTrailRangeVisibility();
      });
    }
    if (trackedBtn) {
      trackedBtn.addEventListener('click', function () {
        _filter = 'tracked';
        _clearActive();
        trackedBtn.classList.add('active');
        _render();
        if (_trailsEnabled) _fetchTrails(); else _clearTrails();
        _startTrailRefresh();
        _updateTrailRangeVisibility();
      });
    }
    if (gpsBtn) {
      gpsBtn.addEventListener('click', function () {
        if (_gpsPos && _map) {
          _map.flyTo(_gpsPos, 13, { duration: 0.8 });
        } else {
          gpsBtn.classList.remove('gps-no-fix');
          void gpsBtn.offsetWidth;
          gpsBtn.classList.add('gps-no-fix');
        }
      });
    }
  }
  _wireFilterTabs();

  var trailBtns = document.querySelectorAll('[data-trail-hours]');
  for (var tb = 0; tb < trailBtns.length; tb++) {
    trailBtns[tb].addEventListener('click', function () {
      var hrs = parseInt(this.getAttribute('data-trail-hours'), 10);
      for (var x = 0; x < trailBtns.length; x++) trailBtns[x].classList.remove('active');
      this.classList.add('active');
      R.setTrailHours(hrs);
    });
  }

  _loadGpsCache();
  if (_gpsPos && !_gpsLive) {
    _initMap();
    if (_map) {
      _ensureGpsMarker(_gpsPos, 0.5);
      if (_gpsFix) _ensureGpsAccuracy(_gpsPos, _gpsFix.hdop, 0.5);
    }
  }
  _loadPositions();

  R.refreshMapTrackedFilter = function () {
    if (_filter === 'tracked') {
      _render();
      if (_trailsEnabled) {
        _trailCache = null;
        _fetchTrails();
      }
    }
  };
  R.onTrailUpdate = function () {
    if (_filter === 'tracked' && _trailsEnabled) {
      _trailCache = null;
      _fetchTrails();
    }
  };
  R.toggleMapTrails = function (enabled) {
    _trailsEnabled = enabled;
    _updateTrailRangeVisibility();
    if (enabled && _filter === 'tracked') {
      _fetchTrails();
    } else {
      _stopTrailRefresh();
      _clearTrails();
    }
  };
  R.setTrailHours = function (hours) {
    _trailHours = hours;
    _trailCache = null;
    if (_trailsEnabled && _filter === 'tracked') _fetchTrails();
  };

  // -- Handle window resize (Leaflet needs invalidateSize) -----------------

  window.addEventListener('resize', function () {
    if (_map) {
      setTimeout(function () { _map.invalidateSize(); }, 200);
    }
  });

  // -- Expose to RPI namespace ---------------------------------------------

  R.updateMap = updateMap;
  R.updateMapMeshCore = updateMapMeshCore;
  R.updateMapReticulum = updateMapReticulum;
  R.updateMapLoraNeighbors = updateMapLoraNeighbors;
  R.updateMapGps = updateMapGps;
  R._mapInvalidate = function () {
    if (_map) setTimeout(function () { _map.invalidateSize(); }, 100);
  };
})();
