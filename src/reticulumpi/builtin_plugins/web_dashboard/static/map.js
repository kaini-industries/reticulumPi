/* ReticulumPi Dashboard -- Node Map module (Meshtastic + MeshCore) */
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
  var _filter = 'all';       // 'all' or 'lora'
  var _loraNeighborIds = {}; // {id: true} for Meshtastic LoRa neighbor filter
  var _initialFit = false;   // whether we've done the first fitBounds

  // -- Custom marker icons -------------------------------------------------

  var _iconDefault = null;
  var _iconSelf = null;
  var _iconMeshCore = null;

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
    else h += ' <span class="map-source-tag msh">Meshtastic</span>';
    h += '</div>';
    h += '<div class="map-popup-grid">';
    if (node.source === 'meshcore') {
      var pk = String(node.public_key || '');
      var pkShort = pk ? (pk.slice(0, 8) + '…' + pk.slice(-4)) : '--';
      h += _popupRow('Key', '<span class="addr">' + esc(pkShort) + '</span>');
      h += _popupRow('Type', esc(_MC_TYPE_LABELS[node.type] || ('type ' + node.type)));
      h += _popupRow('Last Advert', formatTimeAgo(node.last_heard));
    } else {
      h += _popupRow('ID', '<span class="addr">' + esc(String(node.id || '--')) + '</span>');
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

  function _allNodes() {
    var out = [];
    for (var i = 0; i < _mshNodes.length; i++) {
      out.push(_normalizeMeshtastic(_mshNodes[i]));
    }
    for (var j = 0; j < _mcContacts.length; j++) {
      out.push(_normalizeMeshCore(_mcContacts[j]));
    }
    return out;
  }

  function _iconFor(node) {
    if (node.is_self) return _iconSelf;
    if (node.source === 'meshcore') return _iconMeshCore;
    return _iconDefault;
  }

  // -- Marker update logic -------------------------------------------------

  function _hasValidPos(n) {
    return n.latitude != null && n.longitude != null
      && !(n.latitude === 0 && n.longitude === 0);
  }

  function _render() {
    if (!_map || !_markerGroup) return;

    var nodes = _allNodes();

    // Filter to nodes with valid position.  "lora" filter keeps
    // Meshtastic self/neighbors + ALL MeshCore contacts (MeshCore is
    // always LoRa).
    var withPos = [];
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (!_hasValidPos(n)) continue;
      if (_filter === 'lora') {
        var keep = (n.source === 'meshcore')
          || n.is_self
          || _loraNeighborIds[n.id];
        if (!keep) continue;
      }
      withPos.push(n);
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
      if (existing) {
        existing.setLatLng([node.latitude, node.longitude]);
        existing.setPopupContent(_buildPopup(node));
        existing.setIcon(icon);
      } else {
        var marker = L.marker([node.latitude, node.longitude], {
          icon: icon,
          title: node.long_name || node.short_name || node.id
        });
        marker.bindPopup(_buildPopup(node), { maxWidth: 280 });
        _markerGroup.addLayer(marker);
        _markers[node._key] = marker;
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
    _mshNodes = nodes || [];
    if (!_map) _initMap();
    _render();
  }

  function updateMapMeshCore(contacts) {
    _mcContacts = contacts || [];
    if (!_map) _initMap();
    _render();
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

  // -- Filter tab wiring ---------------------------------------------------

  function _wireFilterTabs() {
    var allBtn = $('map-show-all');
    var loraBtn = $('map-show-lora');
    if (!allBtn || !loraBtn) return;

    allBtn.addEventListener('click', function () {
      _filter = 'all';
      allBtn.classList.add('active');
      loraBtn.classList.remove('active');
      _render();
    });
    loraBtn.addEventListener('click', function () {
      _filter = 'lora';
      loraBtn.classList.add('active');
      allBtn.classList.remove('active');
      _render();
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
  R.updateMapMeshCore = updateMapMeshCore;
  R.updateMapLoraNeighbors = updateMapLoraNeighbors;
})();
