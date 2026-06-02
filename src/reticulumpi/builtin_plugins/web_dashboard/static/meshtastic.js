/* ReticulumPi Dashboard — Meshtastic Gateway module */
(function() {
  'use strict';
  var R = window.RPI;
  var api = R.api, $ = R.$, esc = R.esc;
  var formatTimeAgo = R.formatTimeAgo;
  var _di = R._di;

  var _mshNodes = [];
  var _mshSortKey = 'last_heard';
  var _mshSortAsc = false;
  var _mshExpandedId = null;
  var _mshPageSize = 25;
  var _mshVisible = 25;
  var _mshConnected = false;
  var _fwWatchdog = null;

  function sortMeshtasticNodes(nodes, key, asc) {
    return nodes.slice().sort(function(a, b) {
      // Pin "Self" gateway to the top regardless of sort
      if (a.is_self && !b.is_self) return -1;
      if (!a.is_self && b.is_self) return 1;

      var va, vb;
      if (key === 'snr') {
        va = a.snr != null ? a.snr : -999;
        vb = b.snr != null ? b.snr : -999;
      } else if (key === 'last_heard') {
        va = a.last_heard || 0;
        vb = b.last_heard || 0;
      } else {
        return 0;
      }
      return asc ? va - vb : vb - va;
    });
  }

  function buildMeshtasticDetailHTML(node) {
    var h = '<div class="node-detail-section">Identity</div>'
      + '<div class="node-detail-grid">';
    h += _di('Node ID', esc(node.id || '--'));
    if (node.long_name) h += _di('Long Name', esc(node.long_name));
    if (node.short_name) h += _di('Short Name', esc(node.short_name));
    if (node.is_self) h += _di('Type', 'This gateway');
    h += '</div>';

    h += '<div class="node-detail-section">Radio</div>'
      + '<div class="node-detail-grid">';
    h += _di('Hardware', esc(node.hw_model || '--'));
    h += _di('SNR', node.snr != null ? node.snr.toFixed(1) + ' dB' : '--');
    if (node.last_heard) {
      var d = new Date(node.last_heard * 1000);
      h += _di('Last Heard', formatTimeAgo(node.last_heard));
      h += _di('Timestamp', d.toLocaleString());
    }
    h += '</div>';

    if (node.latitude != null && node.longitude != null) {
      h += '<div class="node-detail-section">Position</div>'
        + '<div class="node-detail-grid">'
        + _di('Latitude', node.latitude.toFixed(5))
        + _di('Longitude', node.longitude.toFixed(5))
        + '</div>';
    }

    return h;
  }

  function renderMeshtasticNodes() {
    var tbody = $('meshtastic-nodes-table');
    if (!tbody) return;
    var sorted = sortMeshtasticNodes(_mshNodes, _mshSortKey, _mshSortAsc);
    var total = sorted.length;

    if (total === 0) {
      tbody.innerHTML = '<tr><td colspan="5">' + (_mshConnected ? 'No nodes discovered yet' : 'Not connected') + '</td></tr>';
      var btn = $('meshtastic-show-more');
      if (btn) btn.style.display = 'none';
      return;
    }

    var limit = Math.min(_mshVisible, total);
    tbody.innerHTML = '';
    for (var i = 0; i < limit; i++) {
      var n = sorted[i];
      var nodeId = n.id || '';
      var name = n.long_name || n.short_name || '--';
      var hw = n.hw_model || '--';
      var snr = n.snr != null ? n.snr.toFixed(1) + ' dB' : '--';
      var heard = formatTimeAgo(n.last_heard);
      var isExpanded = (nodeId === _mshExpandedId);

      var tr = document.createElement('tr');
      if (isExpanded) tr.className = 'node-row-active';
      if (n.is_self) tr.className = (tr.className ? tr.className + ' ' : '') + 'msh-self-row';
      tr.setAttribute('data-msh-id', nodeId);
      tr.innerHTML =
          '<td>' + esc(name) + (n.is_self ? ' <span class="msh-self-tag">SELF</span>' : '') + '</td>'
        + '<td class="addr">' + esc(String(nodeId)) + '</td>'
        + '<td>' + esc(hw) + '</td>'
        + '<td>' + esc(snr) + '</td>'
        + '<td>' + heard + '</td>';
      tr.style.cursor = 'pointer';
      (function(node, id) {
        tr.addEventListener('click', function() { toggleMeshtasticDetail(node, id); });
      })(n, nodeId);
      tbody.appendChild(tr);

      if (isExpanded) {
        var detailTr = document.createElement('tr');
        detailTr.className = 'node-detail';
        detailTr.id = 'msh-detail-' + nodeId;
        var td = document.createElement('td');
        td.colSpan = 5;
        td.innerHTML = buildMeshtasticDetailHTML(n);
        detailTr.appendChild(td);
        tbody.appendChild(detailTr);
      }
    }

    updateMeshtasticSortIndicators();

    // Show/hide "show more" control
    var showMore = $('meshtastic-show-more');
    if (showMore) {
      var remaining = total - limit;
      if (remaining > 0) {
        showMore.style.display = '';
        showMore.textContent = 'Show more (' + remaining + ' remaining)';
      } else if (limit > _mshPageSize) {
        showMore.style.display = '';
        showMore.textContent = 'Show less';
      } else {
        showMore.style.display = 'none';
      }
    }
  }

  function toggleMeshtasticDetail(node, id) {
    if (_mshExpandedId === id) {
      _mshExpandedId = null;
    } else {
      _mshExpandedId = id;
    }
    renderMeshtasticNodes();
  }

  function onMeshtasticSort(key) {
    if (_mshSortKey === key) {
      _mshSortAsc = !_mshSortAsc;
    } else {
      _mshSortKey = key;
      _mshSortAsc = (key === 'snr');  // SNR default asc (worst first), last_heard desc
    }
    renderMeshtasticNodes();
  }

  function updateMeshtasticSortIndicators() {
    var headers = document.querySelectorAll('#meshtastic-section th[data-sort]');
    for (var i = 0; i < headers.length; i++) {
      var th = headers[i];
      var arrow = th.querySelector('.sort-arrow');
      if (th.getAttribute('data-sort') === _mshSortKey) {
        arrow.textContent = _mshSortAsc ? ' \u25B2' : ' \u25BC';
      } else {
        arrow.textContent = '';
      }
    }
  }

  function updateMeshtastic(status, nodes) {
    var section = $('meshtastic-section');
    if (!section) return;
    if (R.markUpdated) R.markUpdated('meshtastic-section');

    // Show unavailable state if plugin is not available
    if (!status || status.available === false) {
      $('meshtastic-status').textContent = 'not installed';
      $('meshtastic-status').className = 'count';
      $('meshtastic-status').style.color = 'var(--text-muted)';
      $('meshtastic-overview').innerHTML = '';
      $('meshtastic-nodes-table').innerHTML = '<tr><td colspan="5" class="text-muted">Meshtastic gateway plugin not enabled</td></tr>';
      return;
    }

    // Status badge
    var badge = $('meshtastic-status');
    var connected = status.connected;
    _mshConnected = connected;
    _fwWatchdog = status.firmware_watchdog || null;
    var hangDetected = _fwWatchdog && _fwWatchdog.hang_detected;
    var openFailing = _fwWatchdog && _fwWatchdog.consecutive_open_failures > 0;
    if (badge) {
      if (hangDetected) {
        badge.textContent = 'firmware hang';
        badge.className = 'count status-warn';
      } else if (openFailing) {
        badge.textContent = 'serial failing (' + _fwWatchdog.consecutive_open_failures + '/' + _fwWatchdog.open_failure_threshold + ')';
        badge.className = 'count status-warn';
      } else {
        badge.textContent = connected ? 'connected' : 'disconnected';
        badge.className = 'count ' + (connected ? 'status-ok' : 'status-err');
      }
    }

    // Overview stats
    var overview = $('meshtastic-overview');
    if (overview) {
      var mode = status.mode || 'serial';
      var stats = [];
      stats.push({label: 'Mode', value: mode.toUpperCase()});

      if (mode === 'mqtt') {
        stats.push({label: 'Broker', value: status.mqtt_broker || '--'});
        stats.push({label: 'Topic', value: status.mqtt_topic || '--'});
        if (status.node_id) {
          stats.push({label: 'Node ID', value: status.node_id});
        }
      } else {
        stats.push({label: 'Port', value: status.serial_port || '--'});
      }

      stats.push({label: 'Channel', value: '' + (status.meshtastic_channel != null ? status.meshtastic_channel : '--')});
      stats.push({label: 'Mesh \u2192 LXMF', value: '' + (status.msgs_mesh_to_lxmf || 0)});
      stats.push({label: 'LXMF \u2192 Mesh', value: '' + (status.msgs_lxmf_to_mesh || 0)});

      if (status.msgs_rate_limited > 0) {
        stats.push({label: 'Rate Limited', value: '' + status.msgs_rate_limited});
      }
      if (status.rate_limit_per_min) {
        stats.push({label: 'Rate Limit', value: status.rate_limit_per_min + '/min'});
      }
      stats.push({label: 'LXMF Recipients', value: '' + (status.lxmf_recipients || 0)});
      stats.push({label: 'Reconnects', value: '' + (status.connect_count || 0)});

      var html = '';
      for (var i = 0; i < stats.length; i++) {
        html += '<div class="meshtastic-stat">'
          + '<span class="msh-label">' + esc(stats[i].label) + '</span>'
          + '<span class="msh-value">' + esc(stats[i].value) + '</span>'
          + '</div>';
      }
      overview.innerHTML = html;
    }

    // Update nodes and re-render
    _mshNodes = nodes || [];
    renderMeshtasticNodes();
  }

  function meshtasticShowMore() {
    if (_mshVisible >= _mshNodes.length) {
      _mshVisible = _mshPageSize;  // collapse back
    } else {
      _mshVisible += _mshPageSize;  // show next page
    }
    renderMeshtasticNodes();
  }

  /* ── Device info card ──────────────────────────────────────────── */

  function formatBroadcastInterval(secs) {
    if (secs == null) return '--';
    if (secs >= 3600) {
      var h = secs / 3600;
      return (h === Math.floor(h) ? h : h.toFixed(1)) + 'h';
    }
    if (secs >= 60) {
      var m = secs / 60;
      return (m === Math.floor(m) ? m : m.toFixed(1)) + 'm';
    }
    return secs + 's';
  }

  function batteryDisplay(level) {
    if (level == null) return { text: '--', cls: '' };
    if (level > 100) return { text: 'Ext Power', cls: 'metric-ok' };
    if (level > 50) return { text: level + '%', cls: 'metric-ok' };
    if (level > 20) return { text: level + '%', cls: 'metric-warn' };
    return { text: level + '%', cls: 'metric-crit' };
  }

  function utilClass(val) {
    if (val == null) return '';
    if (val > 50) return 'metric-crit';
    if (val > 25) return 'metric-warn';
    return 'metric-ok';
  }

  function updateMeshtasticDevice(device) {
    var container = $('meshtastic-device-info');
    if (!container) return;

    // Not available — show notice or nothing
    if (!device || device.available === false) {
      if (device && device.message) {
        container.innerHTML = '<div class="msh-mode-notice">' + esc(device.message) + '</div>';
      } else {
        container.innerHTML = '';
      }
      return;
    }

    var html = '<div class="lora-radio-card">';

    // Header: name + status
    var name = device.long_name || 'Meshtastic Device';
    var nodeId = device.node_id || '';
    var statusCls = device.connected ? 'status-active' : 'status-failed';
    var statusText = device.connected ? 'Connected' : 'Disconnected';

    html += '<div class="lora-radio-header">'
      + '<span class="lora-radio-name">' + esc(name)
      + (nodeId ? ' <span style="color:var(--text-muted);font-weight:400">(' + esc(nodeId) + ')</span>' : '')
      + '</span>'
      + '<span class="lora-radio-status">'
      + '<span class="status-dot ' + statusCls + '"></span> ' + statusText
      + '</span>'
      + '</div>';

    // Identity params row: Firmware, Role, Hardware, Region, Preset
    var hasIdentity = device.firmware_version || device.role || device.hw_model
                   || device.region || device.modem_preset;
    if (hasIdentity) {
      html += '<div class="lora-radio-params">';
      if (device.firmware_version) {
        // Strip build hash if present (e.g. "2.7.15.567b8ea" -> "2.7.15")
        var fw = device.firmware_version;
        var fwParts = fw.split('.');
        if (fwParts.length > 3) fw = fwParts.slice(0, 3).join('.');
        html += '<div class="lora-param">'
          + '<span class="lora-param-label">Firmware</span>'
          + '<span class="lora-param-value">' + esc(fw) + '</span>'
          + '</div>';
      }
      if (device.role) {
        html += '<div class="lora-param">'
          + '<span class="lora-param-label">Role</span>'
          + '<span class="lora-param-value">' + esc(device.role) + '</span>'
          + '</div>';
      }
      if (device.hw_model) {
        html += '<div class="lora-param">'
          + '<span class="lora-param-label">Hardware</span>'
          + '<span class="lora-param-value">' + esc(device.hw_model) + '</span>'
          + '</div>';
      }
      if (device.region) {
        html += '<div class="lora-param">'
          + '<span class="lora-param-label">Region</span>'
          + '<span class="lora-param-value">' + esc(device.region) + '</span>'
          + '</div>';
      }
      if (device.modem_preset) {
        // Make preset more readable: LONG_FAST -> Long Fast
        var preset = device.modem_preset.replace(/_/g, ' ').replace(/\b\w/g, function(c) {
          return c.toUpperCase();
        }).replace(/\b\w+/g, function(w) {
          return w.charAt(0) + w.slice(1).toLowerCase();
        });
        html += '<div class="lora-param">'
          + '<span class="lora-param-label">Preset</span>'
          + '<span class="lora-param-value">' + esc(preset) + '</span>'
          + '</div>';
      }
      html += '</div>';
    }

    // Runtime metrics grid
    var hasMetrics = device.hop_limit != null || device.tx_power != null
                  || device.battery_level != null || device.channel_utilization != null
                  || device.node_info_broadcast_secs != null;
    if (hasMetrics) {
      html += '<div class="lora-radio-metrics">';

      if (device.hop_limit != null) {
        html += '<div class="lora-metric">'
          + '<span class="lora-metric-label">Hop Limit</span>'
          + '<span class="lora-metric-value">' + device.hop_limit + '</span>'
          + '</div>';
      }
      if (device.tx_power != null) {
        html += '<div class="lora-metric">'
          + '<span class="lora-metric-label">TX Power</span>'
          + '<span class="lora-metric-value">' + device.tx_power + ' dBm</span>'
          + '</div>';
      }
      if (device.node_info_broadcast_secs != null) {
        html += '<div class="lora-metric">'
          + '<span class="lora-metric-label">Broadcast</span>'
          + '<span class="lora-metric-value">' + formatBroadcastInterval(device.node_info_broadcast_secs) + '</span>'
          + '</div>';
      }

      var batt = batteryDisplay(device.battery_level);
      html += '<div class="lora-metric">'
        + '<span class="lora-metric-label">Battery</span>'
        + '<span class="lora-metric-value ' + batt.cls + '">' + batt.text + '</span>'
        + '</div>';

      if (device.voltage != null) {
        html += '<div class="lora-metric">'
          + '<span class="lora-metric-label">Voltage</span>'
          + '<span class="lora-metric-value">' + device.voltage.toFixed(2) + ' V</span>'
          + '</div>';
      }

      var chUtil = device.channel_utilization;
      html += '<div class="lora-metric">'
        + '<span class="lora-metric-label">Ch Util</span>'
        + '<span class="lora-metric-value ' + utilClass(chUtil) + '">'
        + (chUtil != null ? chUtil.toFixed(1) + '%' : '--') + '</span>'
        + '</div>';

      var airUtil = device.air_util_tx;
      html += '<div class="lora-metric">'
        + '<span class="lora-metric-label">Air Util TX</span>'
        + '<span class="lora-metric-value ' + utilClass(airUtil) + '">'
        + (airUtil != null ? airUtil.toFixed(1) + '%' : '--') + '</span>'
        + '</div>';

      html += '</div>';
    }

    // Firmware watchdog health bar
    if (_fwWatchdog && _fwWatchdog.enabled) {
      html += buildFirmwareWatchdogHTML(_fwWatchdog);
    }

    html += '<div class="lora-radio-actions">'
      + '<button class="msh-reboot-btn"'
      + (_rebootInProgress ? ' disabled' : '')
      + '>' + (_rebootInProgress ? 'Rebooting…' : 'Reboot Device') + '</button>'
      + '</div>';

    html += '</div>';
    container.innerHTML = html;

    var rebootBtn = container.querySelector('.msh-reboot-btn');
    if (rebootBtn) {
      rebootBtn.addEventListener('click', function() { RPI.rebootMeshtasticDevice(); });
    }
  }

  function buildFirmwareWatchdogHTML(wd) {
    var silence = wd.silence_seconds;
    var timeout = wd.silence_timeout || 300;
    var hangDetected = wd.hang_detected;
    var pct = silence != null ? Math.min(100, (silence / timeout) * 100) : 0;

    // Determine health state
    var state, stateLabel;
    if (hangDetected) {
      state = 'crit';
      stateLabel = 'Hang Detected';
    } else if (pct > 75) {
      state = 'warn';
      stateLabel = 'Degraded';
    } else {
      state = 'ok';
      stateLabel = 'Healthy';
    }

    var h = '<div class="fw-watchdog">';
    h += '<div class="fw-watchdog-header">'
      + '<span class="fw-watchdog-title">Firmware Health</span>'
      + '<span class="fw-watchdog-badge fw-' + state + '">' + stateLabel + '</span>'
      + '</div>';

    // Silence progress bar
    var silenceText = silence != null ? formatSilence(silence) : '--';
    h += '<div class="fw-silence-row">'
      + '<span class="fw-silence-label">Silence</span>'
      + '<div class="fw-bar-track">'
      + '<div class="fw-bar-fill fw-' + state + '" style="width:' + pct.toFixed(1) + '%"></div>'
      + '</div>'
      + '<span class="fw-silence-value">' + silenceText + ' / ' + formatSilence(timeout) + '</span>'
      + '</div>';

    // Stats row
    var stats = [];
    if (wd.total_hangs > 0) {
      stats.push({label: 'Hangs', value: '' + wd.total_hangs, cls: 'metric-crit'});
    }
    if (wd.total_resets > 0) {
      stats.push({label: 'Resets', value: '' + wd.total_resets, cls: 'metric-warn'});
    }
    if (wd.resets_last_hour > 0) {
      stats.push({
        label: 'Resets/hr',
        value: wd.resets_last_hour + '/' + (wd.max_resets_per_hour || '∞'),
        cls: wd.resets_last_hour >= (wd.max_resets_per_hour || 999) ? 'metric-crit' : 'metric-warn'
      });
    }
    if (wd.consecutive_open_failures > 0) {
      stats.push({
        label: 'Open Fails',
        value: wd.consecutive_open_failures + '/' + (wd.open_failure_threshold || 3),
        cls: wd.consecutive_open_failures >= (wd.open_failure_threshold || 3) ? 'metric-crit' : 'metric-warn'
      });
    }
    if (!wd.auto_reset) {
      stats.push({label: 'Auto-Reset', value: 'Off', cls: ''});
    }

    if (stats.length > 0) {
      h += '<div class="fw-stats">';
      for (var i = 0; i < stats.length; i++) {
        h += '<span class="fw-stat">'
          + '<span class="fw-stat-label">' + stats[i].label + '</span> '
          + '<span class="fw-stat-value ' + stats[i].cls + '">' + stats[i].value + '</span>'
          + '</span>';
      }
      h += '</div>';
    }

    h += '</div>';
    return h;
  }

  function formatSilence(secs) {
    if (secs == null) return '--';
    if (secs < 60) return secs + 's';
    var m = Math.floor(secs / 60);
    var s = secs % 60;
    return m + 'm' + (s > 0 ? s + 's' : '');
  }

  /* ── Device reboot ─────────────────────────────────────────────── */

  var _rebootInProgress = false;

  function rebootMeshtasticDevice() {
    if (_rebootInProgress) return;
    if (!confirm('Reboot the Meshtastic device? The radio will be offline briefly.')) return;
    _rebootInProgress = true;
    var btn = document.querySelector('.msh-reboot-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'Rebooting…'; }
    api('/api/meshtastic/device/reset', {method: 'POST', timeout: 15000}).then(function(r) {
      _rebootInProgress = false;
      if (btn) { btn.disabled = false; btn.textContent = 'Reboot Device'; }
      if (!r || !r.ok) {
        var reason = (r && r.error) ? r.error : 'Reset failed';
        alert('Device reset failed: ' + reason);
      }
    });
  }

  /* ── LoRa Neighbors ────────────────────────────────────────────── */

  var _loraNbrs = [];
  var _loraSortKey = 'last_heard';
  var _loraSortAsc = false;
  var _loraExpandedId = null;
  var _loraPageSize = 25;
  var _loraVisible = 25;

  function sortLoraNeighbors(nodes, key, asc) {
    return nodes.slice().sort(function(a, b) {
      var va, vb;
      if (key === 'snr') {
        va = a.snr != null ? a.snr : -999;
        vb = b.snr != null ? b.snr : -999;
      } else if (key === 'last_heard') {
        va = a.last_heard || 0;
        vb = b.last_heard || 0;
      } else if (key === 'hops_away') {
        va = a.hops_away != null ? a.hops_away : 999;
        vb = b.hops_away != null ? b.hops_away : 999;
      } else {
        return 0;
      }
      return asc ? va - vb : vb - va;
    });
  }

  function buildLoraNeighborDetailHTML(node) {
    var h = '<div class="node-detail-section">Identity</div>'
      + '<div class="node-detail-grid">';
    h += _di('Node ID', esc(node.id || '--'));
    if (node.long_name) h += _di('Long Name', esc(node.long_name));
    if (node.short_name) h += _di('Short Name', esc(node.short_name));
    h += '</div>';

    h += '<div class="node-detail-section">Radio</div>'
      + '<div class="node-detail-grid">';
    h += _di('Hardware', esc(node.hw_model || '--'));
    h += _di('Hops Away', node.hops_away != null ? '' + node.hops_away : '--');
    h += _di('SNR', node.snr != null ? node.snr.toFixed(1) + ' dB' : '--');
    if (node.last_heard) {
      h += _di('Last Heard', formatTimeAgo(node.last_heard));
      h += _di('Timestamp', new Date(node.last_heard * 1000).toLocaleString());
    }
    h += '</div>';

    if (node.latitude != null && node.longitude != null) {
      h += '<div class="node-detail-section">Position</div>'
        + '<div class="node-detail-grid">'
        + _di('Latitude', node.latitude.toFixed(5))
        + _di('Longitude', node.longitude.toFixed(5))
        + '</div>';
    }
    return h;
  }

  function renderLoraNeighbors() {
    var tbody = $('lora-neighbors-table');
    var container = $('meshtastic-lora-neighbors');
    if (!tbody || !container) return;

    if (_loraNbrs.length === 0) {
      container.style.display = 'none';
      var mqttH = $('meshtastic-mqtt-nodes-header');
      if (mqttH) mqttH.style.display = 'none';
      return;
    }
    container.style.display = '';
    var mqttHeader = $('meshtastic-mqtt-nodes-header');
    if (mqttHeader) mqttHeader.style.display = '';

    var sorted = sortLoraNeighbors(_loraNbrs, _loraSortKey, _loraSortAsc);
    var total = sorted.length;
    var limit = Math.min(_loraVisible, total);
    tbody.innerHTML = '';

    for (var i = 0; i < limit; i++) {
      var n = sorted[i];
      var nodeId = n.id || '';
      var name = n.long_name || n.short_name || '--';
      var hw = n.hw_model || '--';
      var hops = n.hops_away;
      var hopsHtml = hops != null
        ? '<span class="lora-hops-badge hops-' + Math.min(hops, 3) + '">' + hops + '</span>'
        : '--';
      var snr = n.snr != null ? n.snr.toFixed(1) + ' dB' : '--';
      var heard = formatTimeAgo(n.last_heard);
      var isExpanded = (nodeId === _loraExpandedId);

      var tr = document.createElement('tr');
      if (isExpanded) tr.className = 'node-row-active';
      tr.setAttribute('data-lora-nbr-id', nodeId);
      tr.innerHTML =
          '<td>' + esc(name) + '</td>'
        + '<td class="addr">' + esc(String(nodeId)) + '</td>'
        + '<td>' + esc(hw) + '</td>'
        + '<td>' + hopsHtml + '</td>'
        + '<td>' + esc(snr) + '</td>'
        + '<td>' + heard + '</td>';
      tr.style.cursor = 'pointer';
      (function(node, id) {
        tr.addEventListener('click', function() { toggleLoraNeighborDetail(node, id); });
      })(n, nodeId);
      tbody.appendChild(tr);

      if (isExpanded) {
        var detailTr = document.createElement('tr');
        detailTr.className = 'node-detail';
        detailTr.id = 'lora-nbr-detail-' + nodeId;
        var td = document.createElement('td');
        td.colSpan = 6;
        td.innerHTML = buildLoraNeighborDetailHTML(n);
        detailTr.appendChild(td);
        tbody.appendChild(detailTr);
      }
    }

    updateLoraSortIndicators();

    var showMore = $('lora-neighbors-show-more');
    if (showMore) {
      var remaining = total - limit;
      if (remaining > 0) {
        showMore.style.display = '';
        showMore.textContent = 'Show more (' + remaining + ' remaining)';
      } else if (limit > _loraPageSize) {
        showMore.style.display = '';
        showMore.textContent = 'Show less';
      } else {
        showMore.style.display = 'none';
      }
    }
  }

  function toggleLoraNeighborDetail(node, id) {
    _loraExpandedId = (_loraExpandedId === id) ? null : id;
    renderLoraNeighbors();
  }

  function onLoraSort(key) {
    if (_loraSortKey === key) {
      _loraSortAsc = !_loraSortAsc;
    } else {
      _loraSortKey = key;
      _loraSortAsc = (key === 'hops_away');
    }
    renderLoraNeighbors();
  }

  function updateLoraSortIndicators() {
    var headers = document.querySelectorAll('#meshtastic-lora-neighbors th[data-sort]');
    for (var i = 0; i < headers.length; i++) {
      var th = headers[i];
      var arrow = th.querySelector('.sort-arrow');
      var sortKey = th.getAttribute('data-sort').replace('lora-', '');
      if (sortKey === _loraSortKey) {
        arrow.textContent = _loraSortAsc ? ' \u25B2' : ' \u25BC';
      } else {
        arrow.textContent = '';
      }
    }
  }

  function loraNeighborsShowMore() {
    if (_loraVisible >= _loraNbrs.length) {
      _loraVisible = _loraPageSize;
    } else {
      _loraVisible += _loraPageSize;
    }
    renderLoraNeighbors();
  }

  function updateLoraNeighbors(neighbors) {
    _loraNbrs = neighbors || [];
    renderLoraNeighbors();
  }

  /* ── Expose to RPI namespace ─────────────────────────────────────── */
  R.updateMeshtastic = updateMeshtastic;
  R.updateMeshtasticDevice = updateMeshtasticDevice;
  R.onMeshtasticSort = onMeshtasticSort;
  R.meshtasticShowMore = meshtasticShowMore;
  R.updateLoraNeighbors = updateLoraNeighbors;
  R.onLoraSort = onLoraSort;
  R.loraNeighborsShowMore = loraNeighborsShowMore;
  R.rebootMeshtasticDevice = rebootMeshtasticDevice;

})();
