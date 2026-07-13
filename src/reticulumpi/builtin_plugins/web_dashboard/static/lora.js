/* ReticulumPi Dashboard — LoRa module */
(function() {
  'use strict';
  var R = window.RPI;
  var api = R.api, $ = R.$, esc = R.esc;
  var formatTimeAgo = R.formatTimeAgo, formatBytes = R.formatBytes;
  var markUpdated = R.markUpdated;

  var _di = R._di;
  var _diRaw = R._diRaw;

  var _currentLoraAnnounceMode = 'all';
  var _loraNodes = [];
  var _loraExpandedHash = null;
  var _loraSignal = { rssi: null, snr: null }; // from interface stats

  function _reachBadgeHTML(score, label) {
    var cls = 'reach-' + (label || 'unlikely').toLowerCase();
    return '<span class="reach-badge ' + cls + '">' + score + '</span>'
      + ' <span class="reach-label ' + cls + '">' + esc(label) + '</span>';
  }

  function _reachFactorHTML(factors) {
    if (!factors) return '';
    var h = '<div class="reach-factors">';
    var names = { path: 'Path', freshness: 'Freshness', hops: 'Hops', announce: 'Announce', relay: 'Relay' };
    var order = ['path', 'freshness', 'hops', 'announce', 'relay'];
    for (var i = 0; i < order.length; i++) {
      var key = order[i];
      var f = factors[key];
      if (!f) continue;
      var pct = f.max > 0 ? Math.round(f.points / f.max * 100) : 0;
      h += '<div class="reach-factor">'
        + '<span class="reach-factor-name">' + (names[key] || key) + '</span>'
        + '<span class="reach-factor-bar"><span class="reach-factor-fill" data-rpi-width="' + pct + '"></span></span>'
        + '<span class="reach-factor-val">' + f.points + '/' + f.max + '</span>'
        + '<span class="reach-factor-detail">' + esc(f.detail || '') + '</span>'
        + '</div>';
    }
    h += '</div>';
    return h;
  }

  function fetchLoraReachability() {
    // Use /api/paths which runs rnpath -t -j to get REAL interface names
    // (not LocalInterface). Filter for RNodeInterface to show LoRa peers.
    // Falls back to 0-hop filter from /api/reachability if /api/paths fails.
    api('/api/paths?interface=RNode').then(function(r) {
      if (!r || !r.ok) {
        _fetchLoraFallback();
        return;
      }
      markUpdated('lora-section');
      var paths = r.data.paths || [];
      var loraNodes = [];
      for (var i = 0; i < paths.length; i++) {
        var p = paths[i];
        loraNodes.push({
          destination_hash: '<' + (p.hash || '') + '>',
          app_name: p.app_name || '',
          aspects: p.aspects || '',
          app_data: p.app_data || '',
          hops: p.hops,
          last_seen: p.timestamp,
          first_seen: p.first_seen || null,
          announce_count: p.announce_count || 0,
          score: p.score != null ? p.score : null,
          label: p.label || '',
          factors: p.factors || null,
          interface: p['interface'] || ''
        });
      }
      updateLoraNodes(loraNodes);

      // Update count from interface summary
      var summary = r.data.by_interface || {};
      var rnodeCount = 0;
      for (var iface in summary) {
        if (iface.indexOf('RNode') !== -1) rnodeCount += summary[iface];
      }
      $('lora-count').textContent = rnodeCount;
    });
  }

  function _fetchLoraFallback() {
    api('/api/reachability?per_page=50').then(function(r) {
      if (!r || !r.ok) return;
      var nodes = r.data.nodes || [];
      var loraNodes = [];
      for (var i = 0; i < nodes.length; i++) {
        var n = nodes[i];
        if (n.destination_hash) {
          R._reachScores[n.destination_hash] = {
            score: n.score, label: n.label, factors: n.factors
          };
          if (n.hops === 0) loraNodes.push(n);
        }
      }
      updateLoraNodes(loraNodes);
    });
  }

  function updateLoraRadio(interfaces, loraDiag) {
    var container = $('lora-radio-info');
    if (!container) return;

    if (loraDiag && loraDiag.announce_mode) {
      _currentLoraAnnounceMode = loraDiag.announce_mode.current || _currentLoraAnnounceMode;
    }
    if (!interfaces) return;

    // Find RNode interfaces
    var rnodes = [];
    for (var i = 0; i < interfaces.length; i++) {
      if (interfaces[i].type === 'RNodeInterface') {
        rnodes.push(interfaces[i]);
      }
    }

    if (rnodes.length === 0) {
      container.innerHTML = '';
      return;
    }

    var html = '';
    for (var r = 0; r < rnodes.length; r++) {
      var iface = rnodes[r];
      var radio = iface.radio || {};
      var online = iface.online !== false;
      var statusCls = online ? 'status-active' : 'status-failed';
      var statusText = online ? 'Online' : 'Offline';

      // Extract label from name like "RNodeInterface[RNode LoRa Interface/dev/ttyACM1]"
      var ifName = iface.name || 'RNode';
      var nameMatch = ifName.match(/\[([^\]\/]+)/);
      var label = nameMatch ? nameMatch[1] : ifName;

      html += '<div class="lora-radio-card">';

      // Header row: name + status
      html += '<div class="lora-radio-header">'
        + '<span class="lora-radio-name">' + esc(label) + '</span>'
        + '<span class="lora-radio-status">'
        + '<span class="status-dot ' + statusCls + '"></span> ' + statusText
        + '</span>'
        + '</div>';

      // Radio parameters row
      var hasRadio = radio.frequency || radio.bandwidth || radio.spreadingfactor;
      if (hasRadio) {
        html += '<div class="lora-radio-params">';
        if (radio.frequency) {
          var mhz = (radio.frequency / 1000000).toFixed(1);
          html += '<div class="lora-param">'
            + '<span class="lora-param-label">Freq</span>'
            + '<span class="lora-param-value">' + mhz + '</span>'
            + '<span class="lora-param-unit">MHz</span>'
            + '</div>';
        }
        if (radio.bandwidth) {
          var bwKhz = (radio.bandwidth / 1000).toFixed(0);
          html += '<div class="lora-param">'
            + '<span class="lora-param-label">BW</span>'
            + '<span class="lora-param-value">' + bwKhz + '</span>'
            + '<span class="lora-param-unit">kHz</span>'
            + '</div>';
        }
        if (radio.spreadingfactor) {
          html += '<div class="lora-param">'
            + '<span class="lora-param-label">SF</span>'
            + '<span class="lora-param-value">' + radio.spreadingfactor + '</span>'
            + '</div>';
        }
        if (radio.codingrate) {
          html += '<div class="lora-param">'
            + '<span class="lora-param-label">CR</span>'
            + '<span class="lora-param-value">4/' + radio.codingrate + '</span>'
            + '</div>';
        }
        if (radio.txpower != null) {
          html += '<div class="lora-param">'
            + '<span class="lora-param-label">TX</span>'
            + '<span class="lora-param-value">' + radio.txpower + '</span>'
            + '<span class="lora-param-unit">dBm</span>'
            + '</div>';
        }
        if (iface.bitrate) {
          var br = iface.bitrate >= 1000 ? (iface.bitrate / 1000).toFixed(1) + ' kbps'
                 : iface.bitrate + ' bps';
          html += '<div class="lora-param">'
            + '<span class="lora-param-label">Rate</span>'
            + '<span class="lora-param-value">' + br + '</span>'
            + '</div>';
        }
        html += '</div>';
      }

      // Runtime metrics row
      var hasMetrics = iface.airtime_short != null || iface.channel_load_short != null
                    || iface.noise_floor != null || iface.rxb > 0 || iface.txb > 0;
      if (hasMetrics) {
        html += '<div class="lora-radio-metrics">';

        // Link budget & SNR margin (computed from txpower + noise floor)
        if (radio.txpower != null && iface.noise_floor != null) {
          var linkBudget = radio.txpower - iface.noise_floor;
          var lbClass = linkBudget > 130 ? 'metric-ok' : linkBudget > 110 ? 'metric-warn' : 'metric-crit';
          html += '<div class="lora-metric">'
            + '<span class="lora-metric-label">Link Budget</span>'
            + '<span class="lora-metric-value ' + lbClass + '">' + linkBudget + ' dB</span>'
            + '</div>';
        }

        // Airtime (values from RNS are already percentages, e.g. 1.54 = 1.54%)
        if (iface.airtime_short != null) {
          var atS = iface.airtime_short.toFixed(1);
          var atL = iface.airtime_long != null ? iface.airtime_long.toFixed(1) : '--';
          var atClass = iface.airtime_short > 25 ? 'metric-crit'
                      : iface.airtime_short > 10 ? 'metric-warn' : 'metric-ok';
          // Trend indicator: short vs long
          var atTrend = '';
          if (iface.airtime_long != null && iface.airtime_long > 0) {
            var atRatio = iface.airtime_short / iface.airtime_long;
            if (atRatio > 1.3) atTrend = ' <span class="trend-up" title="Rising">\u2197</span>';
            else if (atRatio < 0.7) atTrend = ' <span class="trend-down" title="Falling">\u2198</span>';
          }
          html += '<div class="lora-metric">'
            + '<span class="lora-metric-label">Airtime' + atTrend + '</span>'
            + '<div class="lora-bar-wrap">'
            + '<span class="lora-metric-value ' + atClass + '">' + atS + '%</span>'
            + _loraBar(iface.airtime_short, 50)
            + '</div>'
            + '<span class="lora-metric-sub">long: ' + atL + '%</span>'
            + '</div>';
        }

        // Channel load (values from RNS are already percentages)
        if (iface.channel_load_short != null) {
          var clS = iface.channel_load_short.toFixed(1);
          var clL = iface.channel_load_long != null ? iface.channel_load_long.toFixed(1) : '--';
          var clClass = iface.channel_load_short > 50 ? 'metric-crit'
                      : iface.channel_load_short > 20 ? 'metric-warn' : 'metric-ok';
          var clTrend = '';
          if (iface.channel_load_long != null && iface.channel_load_long > 0) {
            var clRatio = iface.channel_load_short / iface.channel_load_long;
            if (clRatio > 1.3) clTrend = ' <span class="trend-up" title="Rising">\u2197</span>';
            else if (clRatio < 0.7) clTrend = ' <span class="trend-down" title="Falling">\u2198</span>';
          }
          html += '<div class="lora-metric">'
            + '<span class="lora-metric-label">Channel Load' + clTrend + '</span>'
            + '<div class="lora-bar-wrap">'
            + '<span class="lora-metric-value ' + clClass + '">' + clS + '%</span>'
            + _loraBar(iface.channel_load_short, 100)
            + '</div>'
            + '<span class="lora-metric-sub">long: ' + clL + '%</span>'
            + '</div>';
        }

        // Noise floor
        if (iface.noise_floor != null) {
          var nf = iface.noise_floor;
          var nfClass = nf > -90 ? 'metric-crit' : nf > -100 ? 'metric-warn' : 'metric-ok';
          html += '<div class="lora-metric">'
            + '<span class="lora-metric-label">Noise Floor</span>'
            + '<span class="lora-metric-value ' + nfClass + '">' + nf + ' dBm</span>'
            + '</div>';
        }

        // Interference (always show when available, not just non-zero)
        if (iface.interference != null) {
          var intVal = iface.interference;
          var intClass = intVal > -80 ? 'metric-crit' : intVal > -95 ? 'metric-warn' : 'metric-ok';
          html += '<div class="lora-metric">'
            + '<span class="lora-metric-label">Interference</span>'
            + '<span class="lora-metric-value ' + intClass + '">' + intVal + ' dBm</span>'
            + '</div>';
        }

        // SNR margin (noise floor vs interference -- how much room above noise)
        if (iface.noise_floor != null && iface.interference != null) {
          var snrMargin = iface.interference - iface.noise_floor;
          var snrClass = snrMargin > 20 ? 'metric-crit' : snrMargin > 10 ? 'metric-warn' : 'metric-ok';
          html += '<div class="lora-metric">'
            + '<span class="lora-metric-label">SNR Margin</span>'
            + '<span class="lora-metric-value ' + snrClass + '">' + snrMargin + ' dB</span>'
            + '</div>';
        }

        // Traffic
        if (iface.txb > 0 || iface.rxb > 0) {
          html += '<div class="lora-metric">'
            + '<span class="lora-metric-label">TX / RX</span>'
            + '<span class="lora-metric-value">' + formatBytes(iface.txb || 0) + ' / ' + formatBytes(iface.rxb || 0) + '</span>'
            + '</div>';
        }

        // Held announces (backpressure indicator)
        if (iface.held_announces != null && iface.held_announces > 0) {
          html += '<div class="lora-metric">'
            + '<span class="lora-metric-label">Held Announces</span>'
            + '<span class="lora-metric-value metric-warn">' + iface.held_announces + '</span>'
            + '</div>';
        }

        html += '</div>';  // end lora-radio-metrics (radio stats row)

        // --- Second row: Announce Queue + Announce Mode + Battery ---
        html += '<div class="lora-radio-metrics">';

        // Announce queue
        if (iface.announce_queue != null) {
          var aqClass = iface.announce_queue > 10 ? 'metric-warn' : '';
          html += '<div class="lora-metric">'
            + '<span class="lora-metric-label">Announce Queue</span>'
            + '<span class="lora-metric-value ' + aqClass + '">' + iface.announce_queue + '</span>'
            + '</div>';
        }

        // Announce mode (from /api/lora diagnostics, or cached from last fetch)
        {
          var modeLabels = {all: 'All', local_priority: 'Local Priority', silent: 'Silent', unknown: 'Unknown'};
          var modes = ['all', 'local_priority', 'silent'];
          var curMode = _currentLoraAnnounceMode;
          if (loraDiag && loraDiag.announce_mode) {
            curMode = loraDiag.announce_mode.current || curMode;
            _currentLoraAnnounceMode = curMode;
            if (loraDiag.announce_mode.available) modes = loraDiag.announce_mode.available;
          }
          html += '<div class="lora-metric">'
            + '<span class="lora-metric-label">Announce Mode</span>'
            + '<select id="lora-announce-mode" class="lora-mode-select">';
          for (var mi = 0; mi < modes.length; mi++) {
            var m = modes[mi];
            var sel = m === curMode ? ' selected' : '';
            html += '<option value="' + m + '"' + sel + '>' + (modeLabels[m] || m) + '</option>';
          }
          html += '</select>'
            + '</div>';
        }

        // Battery (show whenever available)
        if (iface.battery_state != null || iface.battery_percent != null) {
          var batPct = iface.battery_percent;
          var batState = iface.battery_state;
          // 0% with no draining state typically means external/USB power (no battery)
          var onExtPower = (batPct === 0 && batState !== 3);
          var batParts = [];
          var batClass = '';
          if (onExtPower) {
            batParts.push('External Power');
          } else {
            if (batState != null) {
              var batStates = {0: 'Unknown', 1: 'Charging', 2: 'Charged', 3: 'Draining'};
              batParts.push(batStates[batState] || 'State ' + batState);
            }
            if (batPct != null) batParts.push(batPct + '%');
            batClass = batPct != null && batPct < 20 ? 'metric-crit'
                     : batPct != null && batPct < 50 ? 'metric-warn' : '';
          }
          html += '<div class="lora-metric">'
            + '<span class="lora-metric-label">Battery</span>'
            + '<span class="lora-metric-value ' + batClass + '">' + esc(batParts.join(' \u2022 ')) + '</span>'
            + '</div>';
        }

        html += '</div>';  // end lora-radio-metrics (announce + battery row)
      }

      html += '</div>';
    }

    container.innerHTML = html;
    R.applyCspDynamicStyles(container);
    markUpdated('lora-section');
  }

  function _loraBar(pct, maxPct) {
    var w = Math.min(100, (pct / maxPct) * 100).toFixed(0);
    var cls = pct > (maxPct * 0.5) ? 'bar-crit' : pct > (maxPct * 0.2) ? 'bar-warn' : 'bar-ok';
    return '<span class="lora-bar"><span class="lora-bar-fill ' + cls + '" data-rpi-width="' + w + '"></span></span>';
  }

  function updateLoraSignal(interfaces) {
    // Extract last RSSI/SNR from the RNode interface for the Signal column
    if (!interfaces) return;
    for (var i = 0; i < interfaces.length; i++) {
      var iface = interfaces[i];
      if (iface.name && iface.name.indexOf('RNode') !== -1) {
        if (iface.noise_floor != null) _loraSignal.noise = iface.noise_floor;
        if (iface.interference_last_dbm != null) _loraSignal.rssi = iface.interference_last_dbm;
        if (iface.rxb != null) _loraSignal.rxb = iface.rxb;
        break;
      }
    }
  }

  function updateLoraNodes(nodes) {
    var section = $('lora-section');
    var tbody = $('lora-table');
    var countEl = $('lora-count');
    if (!section || !tbody) return;

    _loraNodes = nodes;
    countEl.textContent = nodes.length;

    if (nodes.length === 0) {
      tbody.innerHTML = '<tr><td colspan="8">No LoRa peers discovered yet</td></tr>';
      return;
    }

    tbody.innerHTML = '';
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      var hash = n.destination_hash || '';
      var shortHash = hash.length > 14 ? hash.slice(1, 13) + '\u2026' : hash;
      var name = n.app_data || '';
      if (!name && n.app_name) {
        name = n.app_name + (n.aspects ? '.' + n.aspects : '');
      }
      name = name || '--';
      var appFull = n.app_name ? n.app_name + (n.aspects ? '.' + n.aspects : '') : '--';
      var hops = n.hops != null ? n.hops : '--';
      var ago = n.last_seen ? formatTimeAgo(n.last_seen) : '--';
      var annCount = n.announce_count || 0;
      var score = n.score != null ? n.score : 0;
      var cls = score >= 80 ? 'reach-high' : score >= 60 ? 'reach-good'
              : score >= 40 ? 'reach-fair' : score >= 20 ? 'reach-low' : 'reach-unlikely';
      var isExpanded = (hash === _loraExpandedHash);

      // Signal column -- show interface RSSI if we have RX data
      var sigHtml = '--';
      if (_loraSignal.rssi != null && _loraSignal.rxb > 0) {
        var rssiCls = _loraSignal.rssi > -80 ? 'metric-ok' : _loraSignal.rssi > -100 ? 'metric-warn' : 'metric-crit';
        sigHtml = '<span class="' + rssiCls + '">' + _loraSignal.rssi + ' dBm</span>';
      }

      var tr = document.createElement('tr');
      if (isExpanded) tr.className = 'node-row-active';
      tr.setAttribute('data-lora-hash', hash);
      tr.style.cursor = 'pointer';
      tr.innerHTML =
          '<td class="reach-col"><span class="reach-badge ' + cls + '">' + score + '</span></td>'
        + '<td class="addr" title="' + esc(hash) + '">' + esc(shortHash) + '</td>'
        + '<td class="col-truncate" title="' + esc(n.app_data || '') + '">' + esc(name) + '</td>'
        + '<td>' + esc(appFull) + '</td>'
        + '<td>' + hops + '</td>'
        + '<td>' + sigHtml + '</td>'
        + '<td>' + ago + '</td>'
        + '<td>' + annCount + '</td>';
      tbody.appendChild(tr);

      // Expanded detail row
      if (isExpanded) {
        var detailTr = document.createElement('tr');
        detailTr.className = 'node-detail';
        var td = document.createElement('td');
        td.colSpan = 8;
        td.innerHTML = _buildLoraDetailHTML(n);
        R.applyCspDynamicStyles(td);
        detailTr.appendChild(td);
        tbody.appendChild(detailTr);
      }
    }
  }

  function _buildLoraDetailHTML(node) {
    var h = '';

    // Reachability
    if (node.score != null && node.factors) {
      h += '<div class="node-detail-section">Reachability</div>'
        + '<div class="node-detail-grid">'
        + _diRaw('Score', _reachBadgeHTML(node.score, node.label))
        + '</div>'
        + _reachFactorHTML(node.factors);
    }

    // Identity
    h += '<div class="node-detail-section">Identity</div>'
      + '<div class="node-detail-grid">'
      + _di('Address', node.destination_hash || '--')
      + _di('Name', node.app_data || '--')
      + _di('App', (node.app_name || '--') + (node.aspects ? '.' + node.aspects : ''))
      + '</div>';

    // Network
    var firstSeen = node.first_seen ? new Date(node.first_seen * 1000).toLocaleString() : '--';
    var lastSeen = node.last_seen ? formatTimeAgo(node.last_seen) : '--';
    h += '<div class="node-detail-section">Network</div>'
      + '<div class="node-detail-grid">'
      + _di('Hops', node.hops != null ? node.hops : '--')
      + _di('First Seen', firstSeen)
      + _di('Last Seen', lastSeen)
      + _di('Announces', node.announce_count || 0)
      + _di('Interface', node.interface || '--')
      + '</div>';

    // Signal
    if (_loraSignal.rssi != null || _loraSignal.noise != null) {
      h += '<div class="node-detail-section">Signal</div>'
        + '<div class="node-detail-grid">';
      if (_loraSignal.rssi != null) h += _di('Last RSSI', _loraSignal.rssi + ' dBm');
      if (_loraSignal.noise != null) h += _di('Noise Floor', _loraSignal.noise + ' dBm');
      if (_loraSignal.rssi != null && _loraSignal.noise != null) {
        h += _di('SNR Margin', (_loraSignal.rssi - _loraSignal.noise) + ' dB');
      }
      h += '</div>';
    }

    return h;
  }

  // Register public functions on namespace
  R.fetchLoraReachability = fetchLoraReachability;
  R.updateLoraRadio = updateLoraRadio;
  R.updateLoraSignal = updateLoraSignal;
  R.updateLoraNodes = updateLoraNodes;
  R._loraNodes = function() { return _loraNodes; };
  R._loraExpandedHash = function() { return _loraExpandedHash; };
  R._setLoraExpandedHash = function(v) { _loraExpandedHash = v; };
  R._currentLoraAnnounceMode = function() { return _currentLoraAnnounceMode; };
  R._setCurrentLoraAnnounceMode = function(v) { _currentLoraAnnounceMode = v; };
})();
