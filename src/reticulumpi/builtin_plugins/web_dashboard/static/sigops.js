/* ReticulumPi Dashboard -- Signal Operations panel */
(function () {
  'use strict';
  var R = window.RPI;
  if (!R) return;
  var $ = R.$, esc = R.esc;
  var formatTimeAgo = R.formatTimeAgo;

  var _expanded = false;
  var _contactsExpanded = false;
  var _ismExpanded = false;
  var _correlationsExpanded = false;
  var _wired = false;
  var _sortCol = 'last_seen';
  var _sortAsc = false;
  var _filterType = '';
  var _lastData = null;
  var _lastIsm = null;

  var TYPE_COLORS = {
    aircraft: '#2196f3',
    vessel: '#4caf50',
    balloon: '#9c27b0',
    mesh_peer: '#ff9800',
    ism_device: '#00bcd4',
    weather: '#f44336',
    unknown: '#9e9e9e'
  };

  function _timeAgo(ts) {
    if (!ts) return '--';
    if (formatTimeAgo) return formatTimeAgo(ts);
    var s = Math.floor((Date.now() / 1000) - ts);
    return s < 60 ? s + 's ago' : Math.floor(s / 60) + 'm ago';
  }

  function _typeLabel(t) {
    if (!t) return 'unknown';
    return t.replace(/_/g, ' ');
  }

  function _typeDot(t) {
    var c = TYPE_COLORS[t] || TYPE_COLORS.unknown;
    return '<span class="sigops-dot" style="background:' + c + '"></span>';
  }

  function _sortContacts(contacts) {
    return contacts.slice().sort(function (a, b) {
      var va = a[_sortCol], vb = b[_sortCol];
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (typeof va === 'string') { va = va.toLowerCase(); vb = (vb || '').toLowerCase(); }
      if (va < vb) return _sortAsc ? -1 : 1;
      if (va > vb) return _sortAsc ? 1 : -1;
      return 0;
    });
  }

  // ── Stats bar ─────────────────────────────────────────────────────

  function _renderStats(data) {
    var el = $('sigops-stats');
    if (!el) return;
    var s = data.stats || {};
    var contacts = data.contacts || [];
    var signals = data.active_signals || [];
    var parts = [];
    parts.push('<b>' + contacts.length + '</b> contacts');
    parts.push('<b>' + signals.length + '</b> active signals');
    parts.push('<b>' + (s.observations_persisted || 0) + '</b> observations');
    parts.push('<b>' + (s.correlations_total || 0) + '</b> correlations');
    el.innerHTML = parts.join(' &middot; ');
    var tag = $('sigops-status-tag');
    if (tag) tag.textContent = contacts.length ? contacts.length + ' contacts' : '';
  }

  // ── Contacts table ────────────────────────────────────────────────

  function _renderContacts(data) {
    var tbody = $('sigops-contacts-table');
    if (!tbody) return;
    var contacts = data.contacts || [];
    if (_filterType) {
      contacts = contacts.filter(function (c) { return c.contact_type === _filterType; });
    }
    var sorted = _sortContacts(contacts);
    var html = '';
    for (var i = 0; i < sorted.length; i++) {
      var c = sorted[i];
      html += '<tr>'
        + '<td>' + _typeDot(c.contact_type) + esc(_typeLabel(c.contact_type)) + '</td>'
        + '<td title="' + esc(c.identifier || '') + '">' + esc(c.display_name || c.identifier || '--') + '</td>'
        + '<td>' + (c.observation_count || 0) + '</td>'
        + '<td>' + (c.sources ? esc(c.sources.join(', ')) : '--') + '</td>'
        + '<td>' + (c.distance_nm != null ? c.distance_nm.toFixed(1) + ' nm' : '--') + '</td>'
        + '<td>' + _timeAgo(c.last_seen) + '</td>'
        + '</tr>';
    }
    tbody.innerHTML = html || '<tr><td colspan="6">No contacts</td></tr>';
    var cnt = $('sigops-contacts-count');
    if (cnt) cnt.textContent = contacts.length;
  }

  // ── ISM devices table ─────────────────────────────────────────────

  function _renderIsm(data) {
    var tbody = $('sigops-ism-table');
    if (!tbody) return;
    var devices = data.devices || [];
    var html = '';
    for (var i = 0; i < devices.length; i++) {
      var d = devices[i];
      var reading = '';
      if (d.temperature_C != null) reading += d.temperature_C.toFixed(1) + '°C';
      if (d.humidity != null) reading += (reading ? ' / ' : '') + Number(d.humidity) + '%';
      if (d.battery_ok != null) reading += (reading ? ' / ' : '') + (d.battery_ok ? 'Batt OK' : 'LOW');
      html += '<tr>'
        + '<td>' + esc(d.model || '--') + '</td>'
        + '<td>' + esc(d.id != null ? String(d.id) : '--') + '</td>'
        + '<td>' + (d.channel != null ? esc(String(d.channel)) : '--') + '</td>'
        + '<td>' + (reading || '--') + '</td>'
        + '<td>' + (d.message_count || 0) + '</td>'
        + '<td>' + _timeAgo(d.last_seen) + '</td>'
        + '</tr>';
    }
    tbody.innerHTML = html || '<tr><td colspan="6">No ISM devices</td></tr>';
    var cnt = $('sigops-ism-count');
    if (cnt) cnt.textContent = devices.length;
  }

  // ── Correlations feed ─────────────────────────────────────────────

  function _renderCorrelations(data) {
    var el = $('sigops-correlations-list');
    if (!el) return;
    var evts = data.events || [];
    if (!evts.length) {
      el.innerHTML = '<div class="sigops-empty">No correlation events yet</div>';
      return;
    }
    var html = '';
    for (var i = 0; i < Math.min(evts.length, 20); i++) {
      var e = evts[i];
      html += '<div class="sigops-corr-item">'
        + '<span class="sigops-corr-time">' + _timeAgo(e.timestamp) + '</span> '
        + '<span class="sigops-corr-type">' + esc(e.event_type || '') + '</span> '
        + '<span>' + esc(e.description || '') + '</span>'
        + '</div>';
    }
    el.innerHTML = html;
  }

  // ── Type filter ───────────────────────────────────────────────────

  function _renderTypeFilter(data) {
    var el = $('sigops-type-filter');
    if (!el) return;
    var existing = {};
    for (var k = 0; k < el.children.length; k++) {
      if (el.children[k].value) existing[el.children[k].value] = true;
    }
    var contacts = data.contacts || [];
    for (var i = 0; i < contacts.length; i++) {
      var t = contacts[i].contact_type;
      if (t && !existing[t]) {
        var opt = document.createElement('option');
        opt.value = t;
        opt.textContent = _typeLabel(t);
        el.appendChild(opt);
        existing[t] = true;
      }
    }
  }

  // ── Wiring ────────────────────────────────────────────────────────

  function _wireEvents() {
    if (_wired) return;
    _wired = true;

    var toggle = $('sigops-toggle');
    if (toggle) toggle.addEventListener('click', function () {
      _expanded = !_expanded;
      var body = $('sigops-body');
      if (body) body.classList.toggle('hidden', !_expanded);
      var chev = toggle.querySelector('.chevron');
      if (chev) chev.innerHTML = _expanded ? '&#9662;' : '&#9656;';
      if (_expanded && _lastData) _fullUpdate();
    });

    var ctoggle = $('sigops-contacts-toggle');
    if (ctoggle) ctoggle.addEventListener('click', function () {
      _contactsExpanded = !_contactsExpanded;
      var body = $('sigops-contacts-body');
      if (body) body.classList.toggle('hidden', !_contactsExpanded);
      var chev = ctoggle.querySelector('.chevron');
      if (chev) chev.innerHTML = _contactsExpanded ? '&#9662;' : '&#9656;';
    });

    var itoggle = $('sigops-ism-toggle');
    if (itoggle) itoggle.addEventListener('click', function () {
      _ismExpanded = !_ismExpanded;
      var body = $('sigops-ism-body');
      if (body) body.classList.toggle('hidden', !_ismExpanded);
      var chev = itoggle.querySelector('.chevron');
      if (chev) chev.innerHTML = _ismExpanded ? '&#9662;' : '&#9656;';
    });

    var cortoggle = $('sigops-correlations-toggle');
    if (cortoggle) cortoggle.addEventListener('click', function () {
      _correlationsExpanded = !_correlationsExpanded;
      var body = $('sigops-correlations-body');
      if (body) body.classList.toggle('hidden', !_correlationsExpanded);
      var chev = cortoggle.querySelector('.chevron');
      if (chev) chev.innerHTML = _correlationsExpanded ? '&#9662;' : '&#9656;';
    });

    var filter = $('sigops-type-filter');
    if (filter) filter.addEventListener('change', function () {
      _filterType = filter.value;
      if (_lastData) _renderContacts(_lastData);
    });

    // Sort headers
    var headers = document.querySelectorAll('#sigops-contacts-body th[data-sort]');
    for (var i = 0; i < headers.length; i++) {
      headers[i].addEventListener('click', (function (col) {
        return function () {
          if (_sortCol === col) _sortAsc = !_sortAsc;
          else { _sortCol = col; _sortAsc = false; }
          if (_lastData) _renderContacts(_lastData);
        };
      })(headers[i].getAttribute('data-sort')));
    }
  }

  function _fullUpdate() {
    if (!_lastData) return;
    _renderStats(_lastData);
    _renderContacts(_lastData);
    _renderCorrelations({events: (_lastData.recent_correlations || [])});
    _renderTypeFilter(_lastData);
    if (_lastIsm) _renderIsm(_lastIsm);
  }

  // ── Public update (called from app.js WS handler) ─────────────────

  function _update(data) {
    var section = $('sigops-section');
    if (!section) return;
    section.style.display = '';
    section.classList.remove('awaiting-data');
    _wireEvents();
    _lastData = data;
    if (_expanded) _fullUpdate();
    else _renderStats(data);
  }

  function _updateIsm(data) {
    _lastIsm = data;
    var wrap = $('sigops-ism-wrap');
    if (wrap) wrap.style.display = '';
    if (_expanded && _ismExpanded) _renderIsm(data);
    var cnt = $('sigops-ism-count');
    if (cnt) cnt.textContent = (data.devices || []).length;
  }

  R.sigops = {
    update: _update,
    updateIsm: _updateIsm
  };
})();
