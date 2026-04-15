/* ReticulumPi Dashboard -- Meshtastic Node Map module */
(function () {
  'use strict';
  var R = window.RPI;
  var api = R.api, $ = R.$, esc = R.esc;
  var formatTimeAgo = R.formatTimeAgo;

  var _map = null;           // Leaflet map instance
  var _markers = {};         // nodeId -> L.Marker
  var _markerGroup = null;   // L.FeatureGroup for fitBounds
  var _nodes = [];           // cached node data
  var _filter = 'all';       // 'all' or 'lora'
  var _loraNeighborIds = {}; // {id: true} for LoRa neighbor filter
  var _initialFit = false;   // whether we've done the first fitBounds

  // -- Custom marker icons -------------------------------------------------

  var _iconDefault = null;
  var _iconSelf = null;

  function _initIcons() {
    L.Icon.Default.imagePath = '/static/vendor/images/';

    _iconDefault = new L.Icon({
      iconUrl: '/static/vendor/images/marker-icon.png',
      iconRetinaUrl: '/static/vendor/images/marker-icon-2x.png',
      shadowUrl: '/static/vendor/images/marker-shadow.png',
      iconSize: [25, 41],
      iconAnchor: [12, 41],
      popupAnchor: [1, -34],
      shadowSize: [41, 41]
    });

    _iconSelf = new L.DivIcon({
      className: 'map-marker-self',
      iconSize: [18, 18],
      iconAnchor: [9, 9],
      popupAnchor: [0, -12]
    });
  }

  // -- Map initialization --------------------------------------------------

  function _initMap() {
    if (_map) return;
    var container = $('map-container');
    if (!container) return;

    _initIcons();

    _map = L.map(container, {
      zoomControl: true,
      attributionControl: true
    }).setView([39.8, -98.6], 4);   // default: center of US

    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 19
    }).addTo(_map);

    _markerGroup = L.featureGroup().addTo(_map);
  }

  // -- Popup HTML ----------------------------------------------------------

  function _buildPopup(node) {
    var name = esc(node.long_name || node.short_name || '--');
    var h = '<div class="map-popup">';
    h += '<div class="map-popup-name">' + name;
    if (node.is_self) h += ' <span class="msh-self-tag">SELF</span>';
    h += '</div>';
    h += '<div class="map-popup-grid">';
    h += _popupRow('ID', '<span class="addr">' + esc(String(node.id || '--')) + '</span>');
    h += _popupRow('Hardware', esc(node.hw_model || '--'));
    if (node.snr != null) {
      h += _popupRow('SNR', node.snr.toFixed(1) + ' dB');
    }
    h += _popupRow('Last Heard', formatTimeAgo(node.last_heard));
    h += _popupRow('Position', node.latitude.toFixed(5) + ', ' + node.longitude.toFixed(5));
    h += '</div></div>';
    return h;
  }

  function _popupRow(label, value) {
    return '<div class="map-popup-item">'
      + '<span class="map-popup-label">' + label + '</span>'
      + '<span class="map-popup-value">' + value + '</span>'
      + '</div>';
  }

  // -- Marker update logic -------------------------------------------------

  function _hasValidPos(n) {
    return n.latitude != null && n.longitude != null
      && !(n.latitude === 0 && n.longitude === 0);
  }

  function _updateMarkers(nodes) {
    if (!_map || !_markerGroup) return;

    // Filter to nodes with valid position
    var withPos = [];
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (!_hasValidPos(n)) continue;
      if (_filter === 'lora') {
        if (n.is_self || _loraNeighborIds[n.id]) withPos.push(n);
      } else {
        withPos.push(n);
      }
    }

    // Update stats
    var statsEl = $('map-stats');
    if (statsEl) {
      statsEl.textContent = withPos.length + ' of ' + nodes.length + ' nodes with position';
    }
    var countEl = $('map-count');
    if (countEl) {
      countEl.textContent = withPos.length + ' on map';
    }

    // Current IDs for add/remove tracking
    var currentIds = {};
    for (var i = 0; i < withPos.length; i++) {
      currentIds[withPos[i].id] = true;
    }

    // Remove stale markers
    for (var id in _markers) {
      if (!currentIds[id]) {
        _markerGroup.removeLayer(_markers[id]);
        delete _markers[id];
      }
    }

    // Add or update markers
    for (var i = 0; i < withPos.length; i++) {
      var n = withPos[i];
      var existing = _markers[n.id];
      if (existing) {
        existing.setLatLng([n.latitude, n.longitude]);
        existing.setPopupContent(_buildPopup(n));
        existing.setIcon(n.is_self ? _iconSelf : _iconDefault);
      } else {
        var marker = L.marker([n.latitude, n.longitude], {
          icon: n.is_self ? _iconSelf : _iconDefault,
          title: n.long_name || n.short_name || n.id
        });
        marker.bindPopup(_buildPopup(n), { maxWidth: 280 });
        _markerGroup.addLayer(marker);
        _markers[n.id] = marker;
      }
    }

    // Fit bounds only on first load
    if (!_initialFit && _markerGroup.getLayers().length > 0) {
      _map.fitBounds(_markerGroup.getBounds().pad(0.1));
      _initialFit = true;
    }
  }

  // -- Public API ----------------------------------------------------------

  function updateMap(nodes) {
    _nodes = nodes || [];
    if (!_map) _initMap();
    _updateMarkers(_nodes);
  }

  function updateMapLoraNeighbors(neighbors) {
    _loraNeighborIds = {};
    if (neighbors) {
      for (var i = 0; i < neighbors.length; i++) {
        if (neighbors[i].id) _loraNeighborIds[neighbors[i].id] = true;
      }
    }
    // Re-render if LoRa filter is active
    if (_filter === 'lora' && _nodes.length > 0) {
      _updateMarkers(_nodes);
    }
  }

  // -- Filter tab wiring ---------------------------------------------------

  function _wireFilterTabs() {
    var allBtn = $('map-show-all');
    var loraBtn = $('map-show-lora');
    if (!allBtn || !loraBtn) return;

    allBtn.addEventListener('click', function () {
      _filter = 'all';
      allBtn.classList.add('active');
      loraBtn.classList.remove('active');
      _updateMarkers(_nodes);
    });
    loraBtn.addEventListener('click', function () {
      _filter = 'lora';
      loraBtn.classList.add('active');
      allBtn.classList.remove('active');
      _updateMarkers(_nodes);
    });
  }
  _wireFilterTabs();

  // -- Handle window resize (Leaflet needs invalidateSize) -----------------

  window.addEventListener('resize', function () {
    if (_map) {
      setTimeout(function () { _map.invalidateSize(); }, 200);
    }
  });

  // -- Expose to RPI namespace ---------------------------------------------

  R.updateMap = updateMap;
  R.updateMapLoraNeighbors = updateMapLoraNeighbors;
})();
