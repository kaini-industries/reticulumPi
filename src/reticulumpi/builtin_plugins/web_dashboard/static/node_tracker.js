(function () {
  'use strict';
  var R = window.RPI;
  if (!R) return;
  var $ = R.$, esc = R.esc, formatTimeAgo = R.formatTimeAgo;

  var LS_KEY = 'rpi_tracked_nodes';
  var _trackedIds = Object.create(null);
  var _latestData = Object.create(null);
  var _allKnownMsh = [];
  var _allKnownMc = [];
  var _dom = null;
  var _activeIdx = -1;
  var _renderPending = false;
  var _debounceTimer = 0;
  var _trailsEnabled = false;

  function _resolveDom() {
    if (_dom) return true;
    var section = $('node-tracker-section');
    if (!section) return false;
    _dom = {
      section: section,
      toggle: $('node-tracker-toggle'),
      body: $('node-tracker-body'),
      count: $('node-tracker-count'),
      search: $('node-tracker-search'),
      results: $('node-tracker-results'),
      chips: $('node-tracker-chips'),
      tbody: $('node-tracker-table-body')
    };
    _wireSearch();
    _dom.toggle.addEventListener('click', function () {
      _closeResults();
      if (_dom.search) _dom.search.value = '';
    });
    var trailCb = document.getElementById('node-tracker-trail-toggle');
    if (trailCb) {
      trailCb.checked = _trailsEnabled;
      trailCb.addEventListener('change', function () {
        _trailsEnabled = trailCb.checked;
        try {
          if (window.localStorage) localStorage.setItem('rpi_node_tracker_trails', _trailsEnabled ? 'true' : 'false');
        } catch (e) { /* */ }
        if (R.toggleMapTrails) R.toggleMapTrails(_trailsEnabled);
      });
    }
    return true;
  }

  function _wireSearch() {
    if (!_dom.search) return;
    _dom.search.addEventListener('input', function () {
      clearTimeout(_debounceTimer);
      _debounceTimer = setTimeout(function () {
        _filterResults(_dom.search.value);
      }, 120);
    });
    _dom.search.addEventListener('focus', function () {
      if (_dom.search.value) _filterResults(_dom.search.value);
    });
    _dom.search.addEventListener('keydown', function (e) {
      var items = _dom.results.querySelectorAll('.nt-result-item');
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        _activeIdx = Math.min(_activeIdx + 1, items.length - 1);
        _highlightItem(items);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        _activeIdx = Math.max(_activeIdx - 1, 0);
        _highlightItem(items);
      } else if (e.key === 'Enter') {
        e.preventDefault();
        if (_activeIdx >= 0 && items[_activeIdx]) {
          var id = items[_activeIdx].getAttribute('data-nt-id');
          if (id) _selectResult(id);
        }
      } else if (e.key === 'Escape') {
        _closeResults();
        _dom.search.blur();
      }
    });
    _dom.results.addEventListener('mousedown', function (e) {
      var el = e.target;
      while (el && el !== _dom.results) {
        if (el.getAttribute && el.getAttribute('data-nt-id')) {
          e.preventDefault();
          _selectResult(el.getAttribute('data-nt-id'));
          return;
        }
        el = el.parentNode;
      }
    });
    document.addEventListener('click', function (e) {
      if (_dom.results && !_dom.results.contains(e.target) && e.target !== _dom.search) {
        _closeResults();
      }
    });
  }

  function _filterResults(query) {
    if (!_dom.results) return;
    var q = (query || '').toLowerCase().trim();
    if (!q) { _closeResults(); return; }

    var matches = [];
    var sorted = _getSortedKnown();
    for (var i = 0; i < sorted.length && matches.length < 20; i++) {
      var n = sorted[i];
      if (!n.id || _trackedIds[n.id]) continue;
      var name = (n.long_name || n.short_name || '').toLowerCase();
      var id = n.id.toLowerCase();
      if (name.indexOf(q) !== -1 || id.indexOf(q) !== -1) {
        matches.push(n);
      }
    }

    if (!matches.length) {
      _dom.results.innerHTML = '<div class="nt-no-results">No matching nodes</div>';
    } else {
      var html = '';
      for (var j = 0; j < matches.length; j++) {
        var m = matches[j];
        var label = m.long_name || m.short_name || m.id;
        var src = m._source === 'meshcore' ? 'MC' : 'MSH';
        var srcClass = m._source === 'meshcore' ? 'svc-tag-mc' : 'svc-tag-msh';
        html += '<div class="nt-result-item" data-nt-id="' + esc(m.id) + '">'
          + esc(label) + '<span class="nt-result-id">' + esc(m.id) + '</span>'
          + ' <span class="svc-tag ' + srcClass + ' nt-source-badge">' + src + '</span>'
          + '</div>';
      }
      _dom.results.innerHTML = html;
    }

    _activeIdx = -1;
    _dom.results.classList.add('open');
  }

  function _highlightItem(items) {
    for (var i = 0; i < items.length; i++) {
      items[i].classList.toggle('nt-active', i === _activeIdx);
    }
    if (_activeIdx >= 0 && items[_activeIdx]) {
      items[_activeIdx].scrollIntoView({ block: 'nearest' });
    }
  }

  function _selectResult(id) {
    _addTrackedNode(id);
    _dom.search.value = '';
    _closeResults();
  }

  function _closeResults() {
    if (_dom.results) _dom.results.classList.remove('open');
    _activeIdx = -1;
  }

  function _getAllKnown() {
    return _allKnownMsh.concat(_allKnownMc);
  }

  function _getSortedKnown() {
    return _getAllKnown().sort(function (a, b) {
      var na = (a.long_name || a.short_name || a.id).toLowerCase();
      var nb = (b.long_name || b.short_name || b.id).toLowerCase();
      return na < nb ? -1 : na > nb ? 1 : 0;
    });
  }

  function _loadTracked() {
    try {
      var raw = window.localStorage ? localStorage.getItem(LS_KEY) : null;
      if (raw) {
        var arr = JSON.parse(raw);
        if (Array.isArray(arr)) {
          _trackedIds = Object.create(null);
          for (var i = 0; i < arr.length; i++) {
            if (typeof arr[i] === 'string' && arr[i]) _trackedIds[arr[i]] = true;
          }
        }
      }
    } catch (e) { if (typeof console !== 'undefined') console.warn('node_tracker: could not load tracked nodes', e); }
    try {
      var trailPref = window.localStorage ? localStorage.getItem('rpi_node_tracker_trails') : null;
      if (trailPref === 'true') _trailsEnabled = true;
    } catch (e) { /* ignore */ }
  }

  function _saveTracked() {
    try {
      var arr = Object.keys(_trackedIds);
      if (window.localStorage) localStorage.setItem(LS_KEY, JSON.stringify(arr));
    } catch (e) { /* localStorage unavailable */ }
  }

  function _addTrackedNode(id) {
    if (_trackedIds[id]) return;
    _trackedIds[id] = true;
    _saveTracked();
    _renderAll();
    if (R.refreshMapTrackedFilter) R.refreshMapTrackedFilter();
  }

  function _removeTrackedNode(id) {
    if (!_trackedIds[id]) return;
    delete _trackedIds[id];
    _saveTracked();
    _renderAll();
    if (R.refreshMapTrackedFilter) R.refreshMapTrackedFilter();
  }

  function _scheduleRender() {
    if (_renderPending) return;
    _renderPending = true;
    requestAnimationFrame(function () {
      _renderPending = false;
      _renderAll();
    });
  }

  function _renderAll() {
    if (!_resolveDom()) return;
    _renderChips();
    _renderTable();
    var count = Object.keys(_trackedIds).length;
    if (_dom.count) _dom.count.textContent = count ? count + ' tracked' : '';
  }

  function _renderChips() {
    if (!_dom.chips) return;
    var html = '';
    var ids = Object.keys(_trackedIds);
    for (var i = 0; i < ids.length; i++) {
      var id = ids[i];
      var d = _latestData[id];
      var name = (d && (d.long_name || d.short_name)) || id;
      var chipClass = (d && d._source === 'meshcore') ? 'nt-chip nt-chip-mc' : 'nt-chip';
      html += '<span class="' + chipClass + '">'
        + esc(name)
        + ' <button class="nt-chip-remove" data-nt-remove="' + esc(id) + '" title="Stop tracking">&times;</button>'
        + '</span>';
    }
    _dom.chips.innerHTML = html;
  }

  function _renderTable() {
    if (!_dom.tbody) return;
    var ids = Object.keys(_trackedIds);
    if (!ids.length) {
      _dom.tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text-muted);padding:1.5rem">Search for nodes above to start tracking</td></tr>';
      return;
    }
    var html = '';
    for (var i = 0; i < ids.length; i++) {
      var id = ids[i];
      var d = _latestData[id];
      var name = d ? (d.long_name || d.short_name || '--') : '--';
      var pos = '--';
      if (d && typeof d.latitude === 'number' && typeof d.longitude === 'number'
          && !(d.latitude === 0 && d.longitude === 0)) {
        pos = d.latitude.toFixed(5) + ', ' + d.longitude.toFixed(5);
      }
      var lastHeard = (d && d.last_heard) ? d.last_heard : null;
      var timeStr = lastHeard ? formatTimeAgo(lastHeard) : '--';
      var statusClass = 'nt-status-stale';
      if (lastHeard) {
        var age = (Date.now() / 1000) - lastHeard;
        if (age < 300) statusClass = 'nt-status-ok';
        else if (age < 1800) statusClass = 'nt-status-warn';
      }
      var src = (d && d._source === 'meshcore') ? 'MC' : 'MSH';
      var srcClass = (d && d._source === 'meshcore') ? 'svc-tag-mc' : 'svc-tag-msh';
      html += '<tr>'
        + '<td><span class="nt-status ' + statusClass + '"></span></td>'
        + '<td>' + esc(name) + ' <span class="svc-tag ' + srcClass + ' nt-source-badge">' + src + '</span></td>'
        + '<td style="font-family:var(--mono);font-size:0.8em">' + esc(id) + '</td>'
        + '<td>' + esc(pos) + '</td>'
        + '<td>' + esc(timeStr) + '</td>'
        + '</tr>';
    }
    _dom.tbody.innerHTML = html;
  }

  function _copyMshNode(n) {
    return {
      id: n.id,
      long_name: n.long_name,
      short_name: n.short_name,
      latitude: n.latitude,
      longitude: n.longitude,
      last_heard: n.last_heard,
      snr: n.snr,
      hw_model: n.hw_model,
      _source: 'meshtastic'
    };
  }

  function updateNodeTracker(mshNodes, loraNeighbors, mcContacts) {
    if (mshNodes) {
      var mshList = [];
      for (var i = 0; i < mshNodes.length; i++) {
        var n = mshNodes[i];
        if (!n.id) continue;
        var copy = _copyMshNode(n);
        _latestData[n.id] = copy;
        mshList.push(copy);
      }
      _allKnownMsh = mshList;
    }
    if (loraNeighbors) {
      var seen = Object.create(null);
      for (var s = 0; s < _allKnownMsh.length; s++) seen[_allKnownMsh[s].id] = true;
      for (var j = 0; j < loraNeighbors.length; j++) {
        var nb = loraNeighbors[j];
        if (!nb.id) continue;
        var nbCopy = _copyMshNode(nb);
        _latestData[nb.id] = nbCopy;
        if (!seen[nb.id]) { _allKnownMsh.push(nbCopy); seen[nb.id] = true; }
      }
    }
    if (mcContacts) {
      var mcList = [];
      for (var k = 0; k < mcContacts.length; k++) {
        var c = mcContacts[k];
        if (!c.public_key) continue;
        var normalized = {
          id: c.public_key,
          long_name: c.name,
          short_name: c.name,
          latitude: c.latitude,
          longitude: c.longitude,
          last_heard: c.last_advert,
          _source: 'meshcore'
        };
        _latestData[c.public_key] = normalized;
        mcList.push(normalized);
      }
      _allKnownMc = mcList;
    }
    var ldKeys = Object.keys(_latestData);
    if (ldKeys.length > 2000) {
      for (var p = 0; p < ldKeys.length; p++) {
        if (!_trackedIds[ldKeys[p]]) delete _latestData[ldKeys[p]];
      }
    }
    if (!_resolveDom()) return;
    _dom.section.style.display = '';
    _scheduleRender();
    R.markUpdated('node-tracker-section');
  }

  function getTrackedNodeIds() {
    var copy = Object.create(null);
    for (var k in _trackedIds) copy[k] = true;
    return copy;
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('[data-nt-remove]') : null;
    if (!btn) {
      var el = e.target;
      while (el && el !== document) {
        if (el.getAttribute && el.getAttribute('data-nt-remove')) { btn = el; break; }
        el = el.parentNode;
      }
    }
    if (btn) {
      var id = btn.getAttribute('data-nt-remove');
      if (id) _removeTrackedNode(id);
    }
  });

  _loadTracked();

  R.updateNodeTracker = updateNodeTracker;
  R.getTrackedNodeIds = getTrackedNodeIds;
  R.addTrackedNode = _addTrackedNode;
  R.removeTrackedNode = _removeTrackedNode;
  R.getTrackedNodeSources = function () {
    var result = {};
    for (var id in _trackedIds) {
      var d = _latestData[id];
      result[id] = (d && d._source) || 'meshtastic';
    }
    return result;
  };
})();
