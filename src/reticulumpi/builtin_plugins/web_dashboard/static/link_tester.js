(function () {
  'use strict';
  var R = window.RPI;
  if (!R) return;
  var api = R.api, $ = R.$, esc = R.esc;

  var _section, _body, _toggle, _pill;
  var _summaryEl, _rttChart, _sigChart, _logBody;
  var _startBtn, _stopBtn, _clearBtn, _targetInput, _countInput;
  var _history = [];
  var _lastStatus = {};

  function _init() {
    if (_section) return true;
    _section = $('link-tester-section');
    if (!_section) return false;
    _body = $('link-tester-body');
    _toggle = $('link-tester-toggle');
    _pill = $('link-tester-pill');
    _summaryEl = $('link-tester-summary');
    _rttChart = $('link-tester-rtt-chart');
    _sigChart = $('link-tester-signal-chart');
    _logBody = $('link-tester-log-body');
    _startBtn = $('link-tester-start-btn');
    _stopBtn = $('link-tester-stop-btn');
    _clearBtn = $('link-tester-clear-btn');
    _targetInput = $('link-tester-target');
    _countInput = $('link-tester-count');

    if (_startBtn) _startBtn.addEventListener('click', _onStart);
    if (_stopBtn) _stopBtn.addEventListener('click', _onStop);
    if (_clearBtn) _clearBtn.addEventListener('click', _onClear);
    if (_toggle && _body && !_toggle.dataset.rpiLinkTesterDisclosureBound) {
      _toggle.dataset.rpiLinkTesterDisclosureBound = 'true';
      _toggle.addEventListener('click', function () {
        _setExpanded(_body.classList.contains('hidden') || _body.hidden);
      });
      _setExpanded(!_body.classList.contains('hidden') && !_body.hidden);
    }
    return true;
  }

  function _setExpanded(expanded) {
    if (!_toggle || !_body) return;
    _body.classList.toggle('hidden', !expanded);
    _body.hidden = !expanded;
    _toggle.classList.toggle('open', expanded);
    _toggle.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    _toggle.title = expanded ? 'Click to collapse' : 'Click to expand';
    var chevron = _toggle.querySelector('.chevron');
    if (chevron) chevron.innerHTML = expanded ? '&#9662;' : '&#9656;';
  }

  function _onStart() {
    var body = {};
    var target = _targetInput ? _targetInput.value.trim() : '';
    var count = _countInput ? parseInt(_countInput.value, 10) : 20;
    if (target) body.target = target;
    if (count > 0) body.count = count;
    api('/api/link_tester/start', {
      method: 'POST',
      json: body,
    });
  }

  function _onStop() {
    api('/api/link_tester/stop', {method: 'POST'});
  }

  function _onClear() {
    api('/api/link_tester/clear', {method: 'POST'});
    _history = [];
    _renderAll();
  }

  // ── Update handlers ─────────────────────────────────────────────

  function _update(data) {
    if (!data || !data.available) return;
    if (!_section) _init();
    if (!_section) return;
    _section.style.display = '';

    _lastStatus = data;
    if (data.results && data.results.length) {
      var seen = {};
      for (var i = 0; i < _history.length; i++) seen[_history[i].seq] = true;
      for (var j = 0; j < data.results.length; j++) {
        if (!seen[data.results[j].seq]) _history.push(data.results[j]);
      }
    }
    _renderAll();
  }

  function _loadHistory(data) {
    if (!data || !data.available) return;
    if (!_section) _init();
    if (!_section) return;
    _section.style.display = '';
    if (data.results) _history = data.results;
    _lastStatus = data;
    _renderAll();
  }

  // ── Rendering ───────────────────────────────────────────────────

  function _renderAll() {
    _renderPill();
    _renderControls();
    _renderSummary();
    _renderRttChart();
    _renderSignalChart();
    _renderLog();
  }

  function _renderPill() {
    if (!_pill) return;
    var s = _lastStatus;
    if (s.test_running) {
      _pill.textContent = 'Testing';
      _pill.className = 'link-tester-pill active';
    } else if (s.connected) {
      _pill.textContent = 'Connected';
      _pill.className = 'link-tester-pill connected';
    } else {
      _pill.textContent = s.status || 'Offline';
      _pill.className = 'link-tester-pill offline';
    }
  }

  function _renderControls() {
    if (_startBtn) _startBtn.disabled = !!_lastStatus.test_running || !_lastStatus.connected;
    if (_stopBtn) _stopBtn.disabled = !_lastStatus.test_running;
    if (_targetInput && !_targetInput.value && _lastStatus.target) {
      _targetInput.value = _lastStatus.target;
    }
  }

  function _renderSummary() {
    if (!_summaryEl) return;
    var st = _lastStatus.stats;
    if (!st || !st.sent) {
      _summaryEl.textContent = 'No data yet';
      return;
    }
    var parts = [
      'Sent: ' + st.sent,
      'Acked: ' + st.acked,
      'Lost: ' + st.lost + ' (' + st.loss_pct + '%)',
    ];
    if (st.rtt_avg != null) parts.push('RTT: ' + st.rtt_avg + 'ms');
    if (st.rssi_avg != null) parts.push('RSSI: ' + st.rssi_avg + ' dBm');
    if (st.snr_avg != null) parts.push('SNR: ' + st.snr_avg + ' dB');
    _summaryEl.textContent = parts.join(' · ');
  }

  function _renderRttChart() {
    if (!_rttChart) return;
    var W = 800, H = 200, pad = 30;
    var acked = [];
    var lost = [];
    for (var i = 0; i < _history.length; i++) {
      var r = _history[i];
      if (r.status === 'ack' && r.rtt_ms != null) acked.push({idx: i, val: r.rtt_ms});
      else lost.push({idx: i});
    }
    if (!acked.length && !lost.length) {
      _rttChart.innerHTML = '<text x="400" y="100" text-anchor="middle" fill="#6a7b95" font-size="12">No data</text>';
      return;
    }

    var maxRtt = 100;
    for (var a = 0; a < acked.length; a++) {
      if (acked[a].val > maxRtt) maxRtt = acked[a].val;
    }
    maxRtt = Math.ceil(maxRtt * 1.15);
    var n = _history.length;

    var html = '';
    html += '<line x1="' + pad + '" y1="' + (H - pad) + '" x2="' + (W - 10) + '" y2="' + (H - pad) + '" stroke="#2a3545" stroke-width="1"/>';
    html += '<line x1="' + pad + '" y1="10" x2="' + pad + '" y2="' + (H - pad) + '" stroke="#2a3545" stroke-width="1"/>';
    html += '<text x="' + (W / 2) + '" y="' + (H - 5) + '" text-anchor="middle" fill="#6a7b95" font-size="9">Probe #</text>';
    html += '<text x="5" y="' + (H / 2) + '" text-anchor="middle" fill="#6a7b95" font-size="9" transform="rotate(-90,5,' + (H / 2) + ')">RTT (ms)</text>';

    var gridLines = 4;
    for (var g = 0; g <= gridLines; g++) {
      var gy = (H - pad) - g * ((H - pad - 10) / gridLines);
      var gv = Math.round(g * maxRtt / gridLines);
      html += '<line x1="' + pad + '" y1="' + gy + '" x2="' + (W - 10) + '" y2="' + gy + '" stroke="#1e2a38" stroke-width="0.5"/>';
      html += '<text x="' + (pad - 3) + '" y="' + (gy + 3) + '" text-anchor="end" fill="#6a7b95" font-size="8">' + gv + '</text>';
    }

    var pts = [];
    for (var ai = 0; ai < acked.length; ai++) {
      var x = pad + (acked[ai].idx / Math.max(1, n - 1)) * (W - pad - 10);
      var y = (H - pad) - (acked[ai].val / maxRtt) * (H - pad - 10);
      pts.push(x.toFixed(1) + ',' + y.toFixed(1));
    }
    if (pts.length > 1) {
      html += '<polyline points="' + pts.join(' ') + '" fill="none" stroke="#00e5ff" stroke-width="1.5" vector-effect="non-scaling-stroke"/>';
    }
    for (var pi = 0; pi < pts.length; pi++) {
      html += '<circle cx="' + pts[pi].split(',')[0] + '" cy="' + pts[pi].split(',')[1] + '" r="2.5" fill="#00e5ff"/>';
    }

    for (var li = 0; li < lost.length; li++) {
      var lx = pad + (lost[li].idx / Math.max(1, n - 1)) * (W - pad - 10);
      html += '<circle cx="' + lx.toFixed(1) + '" cy="15" r="3" fill="#ff4444" opacity="0.8"/>';
      html += '<line x1="' + lx.toFixed(1) + '" y1="18" x2="' + lx.toFixed(1) + '" y2="' + (H - pad) + '" stroke="#ff4444" stroke-width="0.5" opacity="0.2"/>';
    }

    _rttChart.innerHTML = html;
  }

  function _renderSignalChart() {
    if (!_sigChart) return;
    var W = 800, H = 200, pad = 30;
    var rssiPts = [], snrPts = [];
    for (var i = 0; i < _history.length; i++) {
      var r = _history[i];
      if (r.status !== 'ack') continue;
      var x = pad + (i / Math.max(1, _history.length - 1)) * (W - pad - 10);
      if (r.rssi != null) rssiPts.push({x: x, val: r.rssi});
      if (r.snr != null) snrPts.push({x: x, val: r.snr});
    }

    if (!rssiPts.length && !snrPts.length) {
      _sigChart.innerHTML = '<text x="400" y="100" text-anchor="middle" fill="#6a7b95" font-size="12">No data</text>';
      return;
    }

    var html = '';
    html += '<line x1="' + pad + '" y1="' + (H - pad) + '" x2="' + (W - 10) + '" y2="' + (H - pad) + '" stroke="#2a3545" stroke-width="1"/>';
    html += '<line x1="' + pad + '" y1="10" x2="' + pad + '" y2="' + (H - pad) + '" stroke="#2a3545" stroke-width="1"/>';

    if (rssiPts.length) {
      var rssiMin = -130, rssiMax = -40;
      for (var ri = 0; ri < rssiPts.length; ri++) {
        if (rssiPts[ri].val < rssiMin) rssiMin = rssiPts[ri].val;
        if (rssiPts[ri].val > rssiMax) rssiMax = rssiPts[ri].val;
      }
      rssiMin = Math.floor(rssiMin - 5);
      rssiMax = Math.ceil(rssiMax + 5);
      var rssiSpan = rssiMax - rssiMin || 1;

      html += '<text x="5" y="' + (H / 2) + '" text-anchor="middle" fill="#6495ed" font-size="9" transform="rotate(-90,5,' + (H / 2) + ')">RSSI (dBm)</text>';

      var rPts = [];
      for (var rj = 0; rj < rssiPts.length; rj++) {
        var ry = (H - pad) - ((rssiPts[rj].val - rssiMin) / rssiSpan) * (H - pad - 10);
        rPts.push(rssiPts[rj].x.toFixed(1) + ',' + ry.toFixed(1));
      }
      if (rPts.length > 1) {
        html += '<polyline points="' + rPts.join(' ') + '" fill="none" stroke="#6495ed" stroke-width="1.5" vector-effect="non-scaling-stroke"/>';
      }
      for (var rk = 0; rk < rPts.length; rk++) {
        html += '<circle cx="' + rPts[rk].split(',')[0] + '" cy="' + rPts[rk].split(',')[1] + '" r="2" fill="#6495ed"/>';
      }

      for (var rg = 0; rg <= 3; rg++) {
        var rgy = (H - pad) - rg * ((H - pad - 10) / 3);
        var rgv = Math.round(rssiMin + rg * rssiSpan / 3);
        html += '<text x="' + (pad - 3) + '" y="' + (rgy + 3) + '" text-anchor="end" fill="#6495ed" font-size="8">' + rgv + '</text>';
      }
    }

    if (snrPts.length) {
      var snrMin = -5, snrMax = 15;
      for (var si = 0; si < snrPts.length; si++) {
        if (snrPts[si].val < snrMin) snrMin = snrPts[si].val;
        if (snrPts[si].val > snrMax) snrMax = snrPts[si].val;
      }
      snrMin = Math.floor(snrMin - 2);
      snrMax = Math.ceil(snrMax + 2);
      var snrSpan = snrMax - snrMin || 1;

      html += '<text x="' + (W - 5) + '" y="' + (H / 2) + '" text-anchor="middle" fill="#50c878" font-size="9" transform="rotate(90,' + (W - 5) + ',' + (H / 2) + ')">SNR (dB)</text>';

      var sPts = [];
      for (var sj = 0; sj < snrPts.length; sj++) {
        var sy = (H - pad) - ((snrPts[sj].val - snrMin) / snrSpan) * (H - pad - 10);
        sPts.push(snrPts[sj].x.toFixed(1) + ',' + sy.toFixed(1));
      }
      if (sPts.length > 1) {
        html += '<polyline points="' + sPts.join(' ') + '" fill="none" stroke="#50c878" stroke-width="1.5" stroke-dasharray="4,2" vector-effect="non-scaling-stroke"/>';
      }
      for (var sk = 0; sk < sPts.length; sk++) {
        html += '<circle cx="' + sPts[sk].split(',')[0] + '" cy="' + sPts[sk].split(',')[1] + '" r="2" fill="#50c878"/>';
      }

      for (var sg = 0; sg <= 3; sg++) {
        var sgy = (H - pad) - sg * ((H - pad - 10) / 3);
        var sgv = (snrMin + sg * snrSpan / 3).toFixed(1);
        html += '<text x="' + (W - 12) + '" y="' + (sgy + 3) + '" text-anchor="end" fill="#50c878" font-size="8">' + sgv + '</text>';
      }
    }

    _sigChart.innerHTML = html;
  }

  function _renderLog() {
    if (!_logBody) return;
    var rows = '';
    var start = Math.max(0, _history.length - 50);
    for (var i = _history.length - 1; i >= start; i--) {
      var r = _history[i];
      var cls = r.status === 'lost' ? ' class="lt-lost"' : '';
      var t = r.time ? new Date(r.time * 1000) : null;
      var ts = t ? (t.getHours() + ':' + ('0' + t.getMinutes()).slice(-2) + ':' + ('0' + t.getSeconds()).slice(-2)) : '--';
      rows += '<tr' + cls + '>'
        + '<td>' + r.seq + '</td>'
        + '<td>' + esc(ts) + '</td>'
        + '<td>' + (r.rtt_ms != null ? r.rtt_ms.toFixed(0) + ' ms' : '--') + '</td>'
        + '<td>' + (r.rssi != null ? r.rssi + ' dBm' : '--') + '</td>'
        + '<td>' + (r.snr != null ? r.snr + ' dB' : '--') + '</td>'
        + '<td>' + esc(r.status) + '</td>'
        + '</tr>';
    }
    _logBody.innerHTML = rows;
  }

  // ── Register ────────────────────────────────────────────────────

  R.initLinkTesterFeature = _init;
  R.updateLinkTester = _update;
  R.linkTesterHistoryLoad = _loadHistory;
})();
