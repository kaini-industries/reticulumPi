/* ReticulumPi Dashboard — MeshCore Gateway module */
(function() {
  'use strict';
  var R = window.RPI;
  var api = R.api, $ = R.$, esc = R.esc;
  var formatTimeAgo = R.formatTimeAgo;
  var _di = R._di;

  var _mcContacts = [];
  var _mcSortKey = 'last_advert';
  var _mcSortAsc = false;
  var _mcExpandedId = null;
  var _mcPageSize = 25;
  var _mcVisible = 25;
  var _mcConnected = false;

  /* ── Contact type labels ─────────────────────────────────────────── */

  var CONTACT_TYPES = {
    0: 'Unknown',
    1: 'Client',
    2: 'Repeater',
    3: 'Room',
    4: 'Sensor',
  };

  function contactTypeLabel(type) {
    return CONTACT_TYPES[type] || 'Type ' + type;
  }

  /* ── Sorting ─────────────────────────────────────────────────────── */

  function sortContacts(contacts, key, asc) {
    return contacts.slice().sort(function(a, b) {
      var va, vb;
      if (key === 'last_advert') {
        va = a.last_advert || 0;
        vb = b.last_advert || 0;
      } else if (key === 'out_path_len') {
        va = a.out_path_len != null && a.out_path_len >= 0 ? a.out_path_len : 999;
        vb = b.out_path_len != null && b.out_path_len >= 0 ? b.out_path_len : 999;
      } else if (key === 'name') {
        va = (a.name || '').toLowerCase();
        vb = (b.name || '').toLowerCase();
        if (va < vb) return asc ? -1 : 1;
        if (va > vb) return asc ? 1 : -1;
        return 0;
      } else {
        return 0;
      }
      return asc ? va - vb : vb - va;
    });
  }

  /* ── Detail panel ────────────────────────────────────────────────── */

  function buildContactDetailHTML(contact) {
    var h = '<div class="node-detail-section">Identity</div>'
      + '<div class="node-detail-grid">';
    h += _di('Public Key', '<span class="addr">' + esc(contact.public_key || '--') + '</span>');
    if (contact.name) h += _di('Name', esc(contact.name));
    h += _di('Type', esc(contactTypeLabel(contact.type)));
    if (contact.flags != null) h += _di('Flags', '0x' + contact.flags.toString(16));
    h += '</div>';

    h += '<div class="node-detail-section">Radio</div>'
      + '<div class="node-detail-grid">';
    var pathLen = contact.out_path_len;
    h += _di('Path Length', pathLen != null && pathLen >= 0 ? '' + pathLen + ' hop' + (pathLen !== 1 ? 's' : '') : 'unknown');
    if (contact.last_advert) {
      h += _di('Last Advert', formatTimeAgo(contact.last_advert));
      h += _di('Timestamp', new Date(contact.last_advert * 1000).toLocaleString());
    }
    h += '</div>';

    if (contact.latitude && contact.longitude
        && contact.latitude !== 0 && contact.longitude !== 0) {
      h += '<div class="node-detail-section">Position</div>'
        + '<div class="node-detail-grid">'
        + _di('Latitude', contact.latitude.toFixed(5))
        + _di('Longitude', contact.longitude.toFixed(5))
        + '</div>';
    }

    return h;
  }

  /* ── Render contacts table ───────────────────────────────────────── */

  function renderContacts() {
    var tbody = $('meshcore-contacts-table');
    if (!tbody) return;
    var sorted = sortContacts(_mcContacts, _mcSortKey, _mcSortAsc);
    var total = sorted.length;

    if (total === 0) {
      tbody.innerHTML = '<tr><td colspan="5">'
        + (_mcConnected ? 'No contacts discovered yet' : 'Not connected')
        + '</td></tr>';
      var btn = $('meshcore-show-more');
      if (btn) btn.style.display = 'none';
      return;
    }

    var limit = Math.min(_mcVisible, total);
    tbody.innerHTML = '';
    for (var i = 0; i < limit; i++) {
      var c = sorted[i];
      var pk = c.public_key || '';
      var name = c.name || pk.substring(0, 12);
      var typeLbl = contactTypeLabel(c.type);
      var pathLen = c.out_path_len;
      var pathHtml = pathLen != null && pathLen >= 0
        ? '<span class="mc-hops-badge hops-' + Math.min(pathLen, 3) + '">' + pathLen + '</span>'
        : '--';
      var heard = formatTimeAgo(c.last_advert);
      var isExpanded = (pk === _mcExpandedId);

      var tr = document.createElement('tr');
      if (isExpanded) tr.className = 'node-row-active';
      tr.setAttribute('data-mc-id', pk);
      tr.innerHTML =
          '<td>' + esc(name) + '</td>'
        + '<td class="addr">' + esc(pk.substring(0, 16)) + '</td>'
        + '<td>' + esc(typeLbl) + '</td>'
        + '<td>' + pathHtml + '</td>'
        + '<td>' + heard + '</td>';
      tr.style.cursor = 'pointer';
      (function(contact, id) {
        tr.addEventListener('click', function() { toggleContactDetail(contact, id); });
      })(c, pk);
      tbody.appendChild(tr);

      if (isExpanded) {
        var detailTr = document.createElement('tr');
        detailTr.className = 'node-detail';
        detailTr.id = 'mc-detail-' + pk;
        var td = document.createElement('td');
        td.colSpan = 5;
        td.innerHTML = buildContactDetailHTML(c);
        detailTr.appendChild(td);
        tbody.appendChild(detailTr);
      }
    }

    updateSortIndicators();

    // Show/hide "show more" control
    var showMore = $('meshcore-show-more');
    if (showMore) {
      var remaining = total - limit;
      if (remaining > 0) {
        showMore.style.display = '';
        showMore.textContent = 'Show more (' + remaining + ' remaining)';
      } else if (limit > _mcPageSize) {
        showMore.style.display = '';
        showMore.textContent = 'Show less';
      } else {
        showMore.style.display = 'none';
      }
    }
  }

  function toggleContactDetail(contact, id) {
    _mcExpandedId = (_mcExpandedId === id) ? null : id;
    renderContacts();
  }

  function onMeshCoreSort(key) {
    if (_mcSortKey === key) {
      _mcSortAsc = !_mcSortAsc;
    } else {
      _mcSortKey = key;
      _mcSortAsc = (key === 'out_path_len' || key === 'name');
    }
    renderContacts();
  }

  function updateSortIndicators() {
    var headers = document.querySelectorAll('#meshcore-section th[data-sort]');
    for (var i = 0; i < headers.length; i++) {
      var th = headers[i];
      var arrow = th.querySelector('.sort-arrow');
      if (!arrow) continue;
      if (th.getAttribute('data-sort') === _mcSortKey) {
        arrow.textContent = _mcSortAsc ? ' \u25B2' : ' \u25BC';
      } else {
        arrow.textContent = '';
      }
    }
  }

  /* ── Main update (called from app.js on init + WebSocket) ────────── */

  function updateMeshCore(status, contacts) {
    var section = $('meshcore-section');
    if (!section) return;

    // Plugin not available
    if (!status || status.available === false) {
      $('meshcore-status').textContent = 'not installed';
      $('meshcore-status').className = 'count';
      $('meshcore-status').style.color = 'var(--text-muted)';
      $('meshcore-overview').innerHTML = '';
      $('meshcore-contacts-table').innerHTML = '<tr><td colspan="5" class="text-muted">MeshCore gateway plugin not enabled</td></tr>';
      return;
    }

    // Status badge
    var badge = $('meshcore-status');
    var connected = status.connected;
    _mcConnected = connected;
    if (badge) {
      badge.textContent = connected ? 'connected' : 'disconnected';
      badge.className = 'count ' + (connected ? 'status-ok' : 'status-err');
    }

    // Overview stats
    var overview = $('meshcore-overview');
    if (overview) {
      var stats = [];
      stats.push({label: 'Port', value: status.serial_port || '--'});
      if (status.firmware) stats.push({label: 'Firmware', value: status.firmware});
      if (status.model) stats.push({label: 'Model', value: status.model});
      stats.push({label: 'Contacts', value: '' + (status.contacts || 0)});
      stats.push({label: 'Received', value: '' + (status.msgs_received || 0)});
      stats.push({label: 'Sent', value: '' + (status.msgs_sent || 0)});
      if (status.msgs_rate_limited > 0) {
        stats.push({label: 'Rate Limited', value: '' + status.msgs_rate_limited});
      }
      stats.push({label: 'Reconnects', value: '' + (status.connect_count || 0)});

      var html = '';
      for (var i = 0; i < stats.length; i++) {
        html += '<div class="meshcore-stat">'
          + '<span class="mc-label">' + esc(stats[i].label) + '</span>'
          + '<span class="mc-value">' + esc(stats[i].value) + '</span>'
          + '</div>';
      }
      overview.innerHTML = html;
    }

    // Update contacts and re-render
    if (contacts) _mcContacts = contacts;
    renderContacts();
  }

  /* ── Device info card ────────────────────────────────────────────── */

  function updateMeshCoreDevice(device) {
    var container = $('meshcore-device-info');
    if (!container) return;

    if (!device || device.available === false) {
      if (device && device.message) {
        container.innerHTML = '<div class="msh-mode-notice">' + esc(device.message) + '</div>';
      } else {
        container.innerHTML = '';
      }
      return;
    }

    // Only show device card when connected and we have real device info
    if (!device.connected || !device.ver) {
      container.innerHTML = '';
      return;
    }

    var html = '<div class="lora-radio-card">';

    // Header: model + status
    var name = device.model || 'MeshCore Device';
    var statusCls = device.connected ? 'status-active' : 'status-failed';
    var statusText = device.connected ? 'Connected' : 'Disconnected';

    html += '<div class="lora-radio-header">'
      + '<span class="lora-radio-name">' + esc(name) + '</span>'
      + '<span class="lora-radio-status">'
      + '<span class="status-dot ' + statusCls + '"></span> ' + statusText
      + '</span>'
      + '</div>';

    // Params row: firmware, serial port
    html += '<div class="lora-radio-params">';
    if (device.ver) {
      html += '<div class="lora-param">'
        + '<span class="lora-param-label">Firmware</span>'
        + '<span class="lora-param-value">' + esc(device.ver) + '</span>'
        + '</div>';
    }
    if (device.serial_port) {
      html += '<div class="lora-param">'
        + '<span class="lora-param-label">Port</span>'
        + '<span class="lora-param-value">' + esc(device.serial_port) + '</span>'
        + '</div>';
    }
    html += '</div>';

    html += '</div>';
    container.innerHTML = html;
  }

  /* ── Pagination ──────────────────────────────────────────────────── */

  function meshcoreShowMore() {
    if (_mcVisible >= _mcContacts.length) {
      _mcVisible = _mcPageSize;  // collapse back
    } else {
      _mcVisible += _mcPageSize;  // show next page
    }
    renderContacts();
  }

  /* ── Observer status card ─────────────────────────────────────────── */

  function updateMeshCoreObserver(obs) {
    var container = $('meshcore-observer-info');
    if (!container) return;

    if (!obs || obs.available === false || !obs.active) {
      container.innerHTML = '';
      return;
    }

    var mqttOk = obs.mqtt_connected;
    var statusCls = mqttOk ? 'status-active' : 'status-failed';
    var statusText = mqttOk ? 'MQTT Connected' : 'MQTT Disconnected';

    var html = '<div class="lora-radio-card">';
    html += '<div class="lora-radio-header">'
      + '<span class="lora-radio-name">LetsMesh Observer'
      + ' <span class="mc-observer-tag">' + esc(obs.iata || '---') + '</span>'
      + '</span>'
      + '<span class="lora-radio-status">'
      + '<span class="status-dot ' + statusCls + '"></span> ' + statusText
      + '</span>'
      + '</div>';

    html += '<div class="lora-radio-params">';
    html += '<div class="lora-param">'
      + '<span class="lora-param-label">Captured</span>'
      + '<span class="lora-param-value">' + (obs.packets_captured || 0) + '</span>'
      + '</div>';
    html += '<div class="lora-param">'
      + '<span class="lora-param-label">Published</span>'
      + '<span class="lora-param-value">' + (obs.packets_published || 0) + '</span>'
      + '</div>';
    if (obs.packets_failed > 0) {
      html += '<div class="lora-param">'
        + '<span class="lora-param-label">Failed</span>'
        + '<span class="lora-param-value">' + obs.packets_failed + '</span>'
        + '</div>';
    }
    if (obs.signing_mode && obs.signing_mode !== 'unknown') {
      html += '<div class="lora-param">'
        + '<span class="lora-param-label">Signing</span>'
        + '<span class="lora-param-value">' + esc(obs.signing_mode) + '</span>'
        + '</div>';
    }
    if (obs.last_packet_time) {
      html += '<div class="lora-param">'
        + '<span class="lora-param-label">Last Packet</span>'
        + '<span class="lora-param-value">' + formatTimeAgo(obs.last_packet_time) + '</span>'
        + '</div>';
    }
    html += '</div></div>';

    container.innerHTML = html;
  }

  /* ── Expose to RPI namespace ─────────────────────────────────────── */
  R.updateMeshCore = updateMeshCore;
  R.updateMeshCoreDevice = updateMeshCoreDevice;
  R.updateMeshCoreObserver = updateMeshCoreObserver;
  R.onMeshCoreSort = onMeshCoreSort;
  R.meshcoreShowMore = meshcoreShowMore;

})();
