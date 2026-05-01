/* ReticulumPi Dashboard — Chirp Spectrogram waterfall
 *
 * Renders a continuous scrolling waterfall of STFT rows from the
 * lora_chirp_viewer plugin's streaming rtl_sdr capture.  Each row is a
 * 1-D array of dB power values across frequency bins — identical in
 * shape to the spectrum waterfall but at chirp-level time resolution
 * (~31 ms per row at default settings).
 *
 * Data arrives via WebSocket:
 *   - 'chirp_waterfall_rows'   — batched new rows (base64 uint8)
 *   - 'chirp_waterfall_history'— full backfill on connect
 *
 * Uses spectrumCommon helpers: colorForNorm, paintRowToCanvas,
 * paintHistoryToCanvas, emaAutoScale, formatAge.
 */
(function () {
  'use strict';
  var R = window.RPI;
  if (!R) return;
  var $ = R.$, esc = R.esc;
  var SC = R.spectrumCommon;
  if (!SC) return;

  var SF_COLORS = {
    7:  '#ff6b9d', 8:  '#ff9f43', 9:  '#ffd93d',
    10: '#6bff6b', 11: '#43c9ff', 12: '#b67dff',
  };

  // -- DOM handles --------------------------------------------------------
  var _section, _freqSelect, _bwSelect, _sfToggle, _statusLabel;
  var _metaEl, _canvas, _ctx, _overlayEl, _hoverEl, _detIndicator, _pktIndicator;
  var _resolved = false;

  // -- Canvas dimensions --------------------------------------------------
  var _canvasW = 800, _canvasH = 512;

  // -- State --------------------------------------------------------------
  var _scale = { minDb: -90, maxDb: -30, initialized: false };
  var _lastMeta = null;         // most recent batch metadata
  var _rows = [];               // ring buffer of dB row arrays (newest first)
  var _rowTimestamps = [];      // parallel array of unix timestamps
  var _sweepCount = 0;
  var _lastRenderedSweep = 0;
  var MAX_HIST = 1024;

  // -- Detection state -----------------------------------------------------
  var _detections = [];          // ring buffer of detection payloads (newest first)
  var MAX_DETECTIONS = 256;
  var _detCount = 0;

  // -- Decoded packet state ------------------------------------------------
  var _packets = [];             // ring buffer of decoded packets (newest first)
  var MAX_PACKETS = 128;
  var _pktCount = 0;

  // -- Contextual hover state (populated from periodic updates) -----------
  var _ourFreqHz = null, _ourBwHz = null, _ourSf = null, _ourCr = null;
  var _channelAnalysis = null;
  var _band = null;              // { lo, hi, label } for current center freq

  var _css = {};

  // -- DOM setup ----------------------------------------------------------

  function _resolveDom() {
    if (_resolved) return true;
    _section = $('chirp-viewer-section');
    if (!_section) return false;
    _freqSelect = $('chirp-freq-select');
    _bwSelect = $('chirp-bw-select');
    _sfToggle = $('chirp-sf-toggle');
    _statusLabel = $('chirp-status-label');
    _metaEl = $('chirp-meta');
    _canvas = $('chirp-canvas');
    _overlayEl = $('chirp-overlay');
    _hoverEl = $('chirp-hover');
    _detIndicator = $('chirp-det-indicator');
    _pktIndicator = $('chirp-pkt-indicator');

    if (_canvas) {
      _ctx = _canvas.getContext('2d');
      _resizeCanvas();
      _canvas.addEventListener('mousemove', _onHover);
      _canvas.addEventListener('mouseleave', _onHoverLeave);
      var parent = _canvas.parentElement;
      if (parent && typeof ResizeObserver !== 'undefined') {
        new ResizeObserver(function () {
          _resizeCanvas();
          _repaintFull();
        }).observe(parent);
      }
    }

    // Cache CSS custom properties
    var cs = getComputedStyle(document.documentElement);
    _css.wfBg = cs.getPropertyValue('--wf-bg').trim() || '#050810';
    _css.grid = cs.getPropertyValue('--spec-grid').trim() || '#0f1525';
    _css.label = cs.getPropertyValue('--spec-label').trim() || '#3a4565';

    // Wire controls
    if (_freqSelect) {
      _populateFreqSelect();
      _freqSelect.addEventListener('change', _onParamChange);
    }
    if (_bwSelect) _bwSelect.addEventListener('change', function () {
      _populateFreqSelect();
      _onParamChange();
    });

    _resolved = true;
    return true;
  }

  function _resizeCanvas() {
    if (!_canvas || !_ctx) return;
    var container = _canvas.parentElement;
    if (!container) return;
    var newW = Math.max(400, Math.min(container.clientWidth, 1920));
    newW = (newW + 1) & ~1;
    if (newW === _canvasW && _canvas.width === _canvasW) return;
    _canvasW = newW;
    _canvas.width = _canvasW;
    _canvas.height = _canvasH;
    _ctx.fillStyle = _css.wfBg || '#050810';
    _ctx.fillRect(0, 0, _canvasW, _canvasH);
  }

  function _populateFreqSelect() {
    if (!_freqSelect) return;
    var prev = parseFloat(_freqSelect.value);
    _freqSelect.innerHTML = '';

    var bwHz = _bwSelect ? parseInt(_bwSelect.value) : 250000;
    var bwMhz = bwHz / 1e6;
    var regions = SC.LORA_REGIONS;
    var order = SC.LORA_REGION_ORDER;

    for (var ri = 0; ri < order.length; ri++) {
      var reg = regions[order[ri]];
      var span = reg.hi - reg.lo;
      var spacingMhz = reg.spacing / 1000;

      // Step: tile the band, cap at ~15 options per region, snap to channel grid
      var rawStep = Math.max(bwMhz, span / 15);
      var chInStep = Math.max(1, Math.round(rawStep / spacingMhz));
      var step = chInStep * spacingMhz;

      // Center-freq range that keeps the capture within band edges
      var first = reg.lo + bwMhz / 2;
      var last = reg.hi - bwMhz / 2;
      if (first > last) {
        first = last = (reg.lo + reg.hi) / 2;
      } else {
        var nearCh = Math.round((first - reg.base) / spacingMhz);
        if (nearCh < 0) nearCh = 0;
        first = reg.base + nearCh * spacingMhz;
      }

      var group = document.createElement('optgroup');
      group.label = reg.label;

      for (var f = first; f <= last + step * 0.01; f += step) {
        var mhz = Math.round(f * 10000) / 10000;
        var viewLo = mhz - bwMhz / 2;
        var viewHi = mhz + bwMhz / 2;
        var chLo = Math.max(0, Math.ceil((viewLo - reg.base) / spacingMhz));
        var chHi = Math.min(reg.count - 1, Math.floor((viewHi - reg.base) / spacingMhz));

        var opt = document.createElement('option');
        opt.value = mhz.toFixed(4);
        if (chLo >= 0 && chHi >= chLo && chHi < reg.count) {
          opt.textContent = (chLo === chHi)
            ? 'Ch ' + chLo + ' · ' + mhz.toFixed(3) + ' MHz'
            : 'Ch ' + chLo + '–' + chHi + ' · ' + mhz.toFixed(3) + ' MHz';
        } else {
          opt.textContent = mhz.toFixed(3) + ' MHz';
        }
        group.appendChild(opt);
      }

      _freqSelect.appendChild(group);
    }

    // Restore closest match to previous selection
    if (!isNaN(prev)) {
      var bestIdx = 0, bestDist = Infinity;
      for (var i = 0; i < _freqSelect.options.length; i++) {
        var d = Math.abs(parseFloat(_freqSelect.options[i].value) - prev);
        if (d < bestDist) { bestDist = d; bestIdx = i; }
      }
      if (bestDist < 5) _freqSelect.selectedIndex = bestIdx;
    }
  }

  // -- Parameter changes --------------------------------------------------

  function _onParamChange() {
    var freqMhz = _freqSelect ? parseFloat(_freqSelect.value) : null;
    var sampleRate = _bwSelect ? parseInt(_bwSelect.value) : null;
    var ws = R.ws;
    if (ws && ws.readyState === 1) {
      var cmd = { action: 'chirp_set_params' };
      if (freqMhz) cmd.freq_mhz = freqMhz;
      if (sampleRate) cmd.sample_rate = sampleRate;
      ws.send(JSON.stringify(cmd));
    }
    // Clear local state — new data will arrive with different bins
    _rows = [];
    _rowTimestamps = [];
    _sweepCount = 0;
    _lastRenderedSweep = 0;
    _scale.initialized = false;
    _detections = [];
    _detCount = 0;
    _packets = [];
    _pktCount = 0;
    _updateDetIndicator();
    if (_ctx) {
      _ctx.fillStyle = _css.wfBg || '#050810';
      _ctx.fillRect(0, 0, _canvasW, _canvasH);
    }
  }

  // -- Incoming data handlers ---------------------------------------------

  function handleWaterfallRows(batch) {
    if (!batch || !batch.rows_b64) return;
    if (!_resolveDom()) return;

    _lastMeta = batch;
    var cols = batch.cols || 256;
    var count = batch.count || 0;
    var dbMin = batch.db_min;
    var dbMax = batch.db_max;

    // Decode base64 uint8 → dB rows
    var raw = atob(batch.rows_b64);
    var bytes = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);

    var rng = (dbMax - dbMin) || 1;

    for (var r = 0; r < count; r++) {
      var offset = r * cols;
      if (offset + cols > bytes.length) break;
      var dbRow = new Array(cols);
      for (var c = 0; c < cols; c++) {
        dbRow[c] = dbMin + (bytes[offset + c] / 255) * rng;
      }

      // Update auto-scale
      var mn = Infinity, mx = -Infinity;
      for (var j = 0; j < cols; j++) {
        if (dbRow[j] < mn) mn = dbRow[j];
        if (dbRow[j] > mx) mx = dbRow[j];
      }
      SC.emaAutoScale(_scale, mn, mx);

      // Paint row to waterfall canvas
      SC.paintRowToCanvas(_ctx, _canvas, dbRow, _canvasW, _canvasH,
                          _scale.minDb, _scale.maxDb);

      // Store in history (newest first)
      _rows.unshift(dbRow);
      var ts = batch.timestamp
        ? batch.timestamp + r * ((batch.time_res_ms || 31) / 1000)
        : null;
      _rowTimestamps.unshift(ts);
      _sweepCount++;
    }

    // Trim history
    if (_rows.length > MAX_HIST) {
      _rows.length = MAX_HIST;
      _rowTimestamps.length = MAX_HIST;
    }

    _updateMeta(batch);
    _paintOverlays(batch);
    _updateStatus(batch);
  }

  function handleWaterfallHistory(data) {
    if (!data || !data.available) return;
    if (!_resolveDom()) return;

    _lastMeta = data;
    _rows = [];
    _rowTimestamps = [];
    _sweepCount = data.sweep_count || 0;

    var histRows = data.rows || [];
    var histTs = data.row_timestamps || [];

    // histRows is oldest→newest; we store newest-first
    for (var i = histRows.length - 1; i >= 0; i--) {
      _rows.push(histRows[i]);
      _rowTimestamps.push(i < histTs.length ? histTs[i] : null);
    }
    if (_rows.length > MAX_HIST) {
      _rows.length = MAX_HIST;
      _rowTimestamps.length = MAX_HIST;
    }

    _repaintFull();
    _updateMeta(data);
    _paintOverlays(data);
    _updateStatus(data);

    // Show section
    if (_section) _section.style.display = '';
  }

  function _repaintFull() {
    if (!_ctx || !_rows.length) return;
    _ctx.fillStyle = _css.wfBg || '#050810';
    _ctx.fillRect(0, 0, _canvasW, _canvasH);

    // Seed auto-scale from history
    for (var i = 0; i < Math.min(_rows.length, 10); i++) {
      var row = _rows[i];
      var mn = Infinity, mx = -Infinity;
      for (var j = 0; j < row.length; j++) {
        if (row[j] < mn) mn = row[j];
        if (row[j] > mx) mx = row[j];
      }
      SC.emaAutoScale(_scale, mn, mx);
    }

    // Build oldest→newest array for paintHistoryToCanvas
    var oldest = [];
    for (var k = _rows.length - 1; k >= 0; k--) {
      oldest.push(_rows[k]);
    }
    SC.paintHistoryToCanvas(_ctx, _canvas, oldest, _canvasW, _canvasH,
                            _scale.minDb, _scale.maxDb);
  }

  // -- Metadata strip -----------------------------------------------------

  function _updateMeta(m) {
    if (!_metaEl) return;
    if (!m) { _metaEl.style.display = 'none'; return; }
    _metaEl.style.display = '';

    var sr = m.sample_rate || 250000;
    var centerHz = m.freq_center_hz || 0;
    var freqLo = ((centerHz - sr / 2) / 1e6).toFixed(3);
    var freqHi = ((centerHz + sr / 2) / 1e6).toFixed(3);
    var timeRes = m.time_res_ms || 0;
    var freqRes = m.freq_res_hz || 0;
    var fftSize = m.fft_size || m.cols || 256;

    _metaEl.innerHTML =
      '<span>' + esc(freqLo) + ' – ' + esc(freqHi) + ' MHz</span>' +
      '<span>BW: ' + esc((sr / 1000).toFixed(0)) + ' kHz</span>' +
      '<span>Time: ' + esc(timeRes.toFixed(2)) + ' ms/row</span>' +
      '<span>Freq: ' + esc(freqRes.toFixed(0)) + ' Hz/bin</span>' +
      '<span>FFT: ' + esc(String(fftSize)) + '</span>' +
      '<span>dB: ' + esc(_scale.minDb.toFixed(0)) + ' – ' + esc(_scale.maxDb.toFixed(0)) + '</span>';
  }

  // -- Status label -------------------------------------------------------

  function _updateStatus(m) {
    if (!_statusLabel) return;
    if (!m) { _statusLabel.textContent = ''; return; }
    var sr = m.sample_rate || 250000;
    var centerMhz = ((m.freq_center_hz || 0) / 1e6).toFixed(3);
    _statusLabel.textContent = 'Streaming · ' + centerMhz + ' MHz · '
      + (sr / 1000).toFixed(0) + ' kHz';
  }

  // -- Overlays -----------------------------------------------------------

  function _paintOverlays(m) {
    if (!_overlayEl) return;
    _overlayEl.innerHTML = '';
    if (!m) return;

    var sr = m.sample_rate || 250000;
    var centerHz = m.freq_center_hz || 0;
    var loHz = centerHz - sr / 2;
    var hiHz = centerHz + sr / 2;
    var spanHz = hiHz - loHz;
    if (spanHz <= 0) return;

    // Channel boundary ticks (125 kHz LoRa channels)
    var chBw = 125000;
    var firstCh = Math.ceil(loHz / chBw) * chBw;
    for (var f = firstCh; f <= hiHz; f += chBw) {
      var frac = (f - loHz) / spanHz;
      var line = document.createElement('div');
      line.style.cssText =
        'position:absolute;top:0;bottom:0;left:' + (frac * 100).toFixed(3) + '%;' +
        'border-left:1px dashed rgba(255,255,255,0.12);pointer-events:none;';
      _overlayEl.appendChild(line);
    }

    // SF slope guides
    if (_sfToggle && _sfToggle.checked && m.sf_slopes) {
      _paintSfGuides(m);
    }
  }

  function _paintSfGuides(m) {
    if (!_overlayEl || !m.sf_slopes || !_canvas) return;
    var canvasRect = _canvas.getBoundingClientRect();
    var cW = canvasRect.width, cH = canvasRect.height;
    if (!cW || !cH) return;

    var cols = m.cols || m.fft_size || 256;
    var timeResMs = m.time_res_ms || 31;

    for (var si = 0; si < m.sf_slopes.length; si++) {
      var s = m.sf_slopes[si];
      var sf = s.sf;
      var tSymMs = s.t_symbol_ms;

      // How many display rows one symbol takes
      var rowsPerSym = tSymMs / timeResMs;
      var pixPerSym = rowsPerSym * (cH / _canvasH);
      // Full chirp sweeps the entire BW horizontally
      var angleDeg = Math.atan2(pixPerSym, cW) * (180 / Math.PI);

      var color = SF_COLORS[sf] || '#ffffff';
      var lineLen = Math.sqrt(cW * cW + pixPerSym * pixPerSym);

      var guide = document.createElement('div');
      guide.className = 'chirp-sf-line';
      guide.style.cssText =
        'left:0;bottom:0;width:' + lineLen.toFixed(0) + 'px;' +
        'border-color:' + color + ';' +
        'transform:rotate(-' + angleDeg.toFixed(1) + 'deg);';
      _overlayEl.appendChild(guide);

      var label = document.createElement('div');
      label.className = 'chirp-sf-label';
      label.style.cssText =
        'left:4px;bottom:' + (pixPerSym + 2).toFixed(0) + 'px;color:' + color + ';';
      label.textContent = 'SF' + sf;
      _overlayEl.appendChild(label);
    }
  }

  // -- Hover crosshair ----------------------------------------------------

  function _onHover(ev) {
    if (!_lastMeta || !_hoverEl) return;
    var m = _lastMeta;
    var rect = _canvas.getBoundingClientRect();
    var fracX = (ev.clientX - rect.left) / rect.width;
    var fracY = (ev.clientY - rect.top) / rect.height;
    if (fracX < 0 || fracX > 1 || fracY < 0 || fracY > 1) {
      _hoverEl.style.display = 'none';
      return;
    }

    var sr = m.sample_rate || 250000;
    var centerHz = m.freq_center_hz || 0;
    var loHz = centerHz - sr / 2;
    var hiHz = centerHz + sr / 2;
    var freqHz = loHz + fracX * (hiHz - loHz);
    var freqMhz = freqHz / 1e6;

    var rowIdx = Math.floor(fracY * _canvasH);
    var ts = (rowIdx < _rowTimestamps.length) ? _rowTimestamps[rowIdx] : null;
    var agoSec = (ts != null) ? (Date.now() / 1000 - ts) : null;
    var ageStr;
    if (agoSec == null) ageStr = '—';
    else if (rowIdx === 0 && agoSec < 2) ageStr = 'now';
    else ageStr = SC.formatAge(agoSec);

    var dbStr = '—';
    if (rowIdx < _rows.length) {
      var row = _rows[rowIdx];
      var cols = row.length;
      var binIdx = Math.min(cols - 1, Math.max(0, Math.round(fracX * (cols - 1))));
      var db = row[binIdx];
      if (db != null && isFinite(db)) dbStr = db.toFixed(1) + ' dB';
    }

    var headerLine = freqMhz.toFixed(3) + ' MHz · ' + dbStr + ' · ' + ageStr;
    var bandLine = SC.loraBandLine(freqMhz, {
      rnode: { freqHz: _ourFreqHz, bwHz: _ourBwHz, sf: _ourSf, cr: _ourCr },
      region: _band,
      interferenceFlags: _channelAnalysis && _channelAnalysis.interference_flags,
      esc: esc
    });

    if (bandLine) {
      _hoverEl.innerHTML = esc(headerLine)
        + '<div class="chirp-hover-band">' + bandLine + '</div>';
    } else {
      _hoverEl.textContent = headerLine;
    }
    _hoverEl.style.display = 'block';
  }

  function _onHoverLeave() {
    if (_hoverEl) _hoverEl.style.display = 'none';
  }

  // -- Status updates from periodic broadcast -----------------------------

  function handleUpdate(data) {
    if (!_resolveDom()) return;
    var cs = data.lora_chirp_viewer;
    if (cs && cs.chirp_status) {
      show();
      var st = cs.chirp_status;
      if (st.streaming && _statusLabel) {
        _statusLabel.textContent = 'Streaming · '
          + (st.freq_mhz || 0).toFixed(3) + ' MHz · '
          + ((st.sample_rate || 250000) / 1000).toFixed(0) + ' kHz';
      }
      _band = SC.findLoraRegion(st.freq_mhz || 0);
    }

    // RNode radio params — same scan as lora_spectrum._detectRNode
    _ourFreqHz = _ourBwHz = _ourSf = _ourCr = null;
    var ifaces = data.interfaces || [];
    for (var i = 0; i < ifaces.length; i++) {
      if (ifaces[i].type !== 'RNodeInterface') continue;
      var r = ifaces[i].radio || {};
      if (r.frequency == null) continue;
      _ourFreqHz = r.frequency;
      _ourBwHz = r.bandwidth;
      _ourSf = r.spreadingfactor;
      _ourCr = r.codingrate;
      break;
    }

    _channelAnalysis = (cs && cs.channel_analysis) ? cs.channel_analysis : null;
  }

  // -- Detection handling -------------------------------------------------

  function _paintDetectionMarker(det) {
    if (!_ctx || !_lastMeta) return;
    var sr = _lastMeta.sample_rate || 250000;
    var centerHz = _lastMeta.freq_center_hz || 0;
    var loHz = centerHz - sr / 2;
    var spanHz = sr;
    if (spanHz <= 0) return;

    var freqHz = (det.freq_center_hz || centerHz) + (det.freq_offset_hz || 0) - sr / 2;
    var fracX = (freqHz - loHz) / spanHz;
    if (fracX < 0 || fracX > 1) fracX = 0.5;

    var x = Math.round(fracX * _canvasW);
    var color = SF_COLORS[det.sf] || '#ffffff';

    _ctx.save();
    _ctx.globalAlpha = 0.85;
    _ctx.fillStyle = color;

    // Small triangle at the top of the canvas pointing down
    var sz = 6;
    _ctx.beginPath();
    _ctx.moveTo(x - sz, 0);
    _ctx.lineTo(x + sz, 0);
    _ctx.lineTo(x, sz * 1.5);
    _ctx.closePath();
    _ctx.fill();

    // Thin vertical line from the triangle
    _ctx.globalAlpha = 0.35;
    _ctx.strokeStyle = color;
    _ctx.lineWidth = 1;
    _ctx.beginPath();
    _ctx.moveTo(x, sz * 1.5);
    _ctx.lineTo(x, Math.min(30, _canvasH));
    _ctx.stroke();

    // SF label
    _ctx.globalAlpha = 0.9;
    _ctx.font = '9px monospace';
    _ctx.fillStyle = color;
    _ctx.fillText('SF' + det.sf, x + sz + 2, sz * 1.5);
    _ctx.restore();
  }

  function _updateDetIndicator() {
    if (!_detIndicator) return;
    if (_detCount === 0) {
      _detIndicator.style.display = 'none';
      return;
    }
    _detIndicator.style.display = '';
    var recent = _detections.length > 0 ? _detections[0] : null;
    var sfText = recent ? 'SF' + recent.sf : '';
    var snrText = recent ? recent.snr_db.toFixed(1) + ' dB' : '';
    _detIndicator.innerHTML =
      '<span class="det-count">' + esc(String(_detCount)) + ' detection' +
      (_detCount === 1 ? '' : 's') + '</span>' +
      (sfText ? ' <span class="det-latest" style="color:' +
        (SF_COLORS[recent.sf] || '#fff') + '">' +
        esc(sfText) + ' · ' + esc(snrText) + '</span>' : '');
  }

  function handleDetection(det) {
    if (!det) return;
    if (!_resolveDom()) return;
    _detections.unshift(det);
    if (_detections.length > MAX_DETECTIONS) _detections.length = MAX_DETECTIONS;
    _detCount++;
    _paintDetectionMarker(det);
    _updateDetIndicator();
  }

  function handleDetectionHistory(data) {
    if (!data || !data.length) return;
    if (!_resolveDom()) return;
    _detections = [];
    for (var i = data.length - 1; i >= 0; i--) {
      _detections.unshift(data[i]);
    }
    if (_detections.length > MAX_DETECTIONS) _detections.length = MAX_DETECTIONS;
    _detCount = data.length;
    _updateDetIndicator();
  }

  // -- Decoded packet indicator -------------------------------------------

  function _updatePktIndicator() {
    if (!_pktIndicator) return;
    if (_pktCount === 0) {
      _pktIndicator.style.display = 'none';
      return;
    }
    _pktIndicator.style.display = '';
    var p = _packets.length > 0 ? _packets[0] : null;
    var parts = '<span class="pkt-count">' + esc(String(_pktCount)) + ' packet' +
      (_pktCount === 1 ? '' : 's') + '</span>';
    if (p) {
      var crcTag = p.crc_ok === true ? 'CRC OK' : (p.crc_ok === false ? 'CRC FAIL' : 'no CRC');
      var color = p.crc_ok === true ? '#4f4' : (p.crc_ok === false ? '#f44' : '#aaa');
      parts += ' <span class="pkt-latest" style="color:' + (SF_COLORS[p.sf] || '#fff') + '">' +
        esc('SF' + p.sf + ' CR4/' + (4 + p.cr)) + '</span>' +
        ' <span style="color:' + color + '">' + esc(crcTag) + '</span>' +
        ' <span class="pkt-hex">' + esc((p.payload_hex || '').substring(0, 32)) +
        (p.payload_hex && p.payload_hex.length > 32 ? '...' : '') + '</span>';
    }
    _pktIndicator.innerHTML = parts;
  }

  function handlePacketDecoded(pkt) {
    if (!pkt) return;
    if (!_resolveDom()) return;
    _packets.unshift(pkt);
    if (_packets.length > MAX_PACKETS) _packets.length = MAX_PACKETS;
    _pktCount++;
    _updatePktIndicator();
  }

  // -- Public interface ---------------------------------------------------

  function show() {
    if (!_resolveDom()) return;
    _section.style.display = '';
  }

  function hide() {
    if (_section) _section.style.display = 'none';
  }

  function setFreqMhz(mhz) {
    if (!_freqSelect) return;
    var found = false;
    for (var i = 0; i < _freqSelect.options.length; i++) {
      if (Math.abs(parseFloat(_freqSelect.options[i].value) - mhz) < 0.05) {
        _freqSelect.selectedIndex = i;
        found = true;
        break;
      }
    }
    if (!found) {
      var opt = document.createElement('option');
      opt.value = mhz.toFixed(4);
      opt.textContent = mhz.toFixed(3) + ' MHz';
      _freqSelect.appendChild(opt);
      _freqSelect.value = opt.value;
    }
  }

  R.chirpSpectrogram = {
    show: show,
    hide: hide,
    handleUpdate: handleUpdate,
    handleChirpResult: function () {},  // legacy — no-op now
    handleWaterfallRows: handleWaterfallRows,
    handleWaterfallHistory: handleWaterfallHistory,
    handleDetection: handleDetection,
    handleDetectionHistory: handleDetectionHistory,
    handlePacketDecoded: handlePacketDecoded,
    setFreqMhz: setFreqMhz,
  };
})();
