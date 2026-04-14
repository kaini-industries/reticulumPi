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
    if (badge) {
      badge.textContent = connected ? 'connected' : 'disconnected';
      badge.className = 'count ' + (connected ? 'status-ok' : 'status-err');
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

  /* ── Expose to RPI namespace ─────────────────────────────────────── */
  R.updateMeshtastic = updateMeshtastic;
  R.onMeshtasticSort = onMeshtasticSort;
  R.meshtasticShowMore = meshtasticShowMore;

})();
