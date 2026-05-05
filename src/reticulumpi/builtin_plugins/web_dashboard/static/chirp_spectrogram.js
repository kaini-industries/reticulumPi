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
  var SYNC_COLORS = { 0x34: '#ffffff', 0x12: '#00e5ff', 0x2B: '#6bff6b' };
  var SYNC_NAMES  = { 0x34: 'LoRaWAN', 0x12: 'MeshCore', 0x2B: 'Meshtastic' };

  // -- DOM handles --------------------------------------------------------
  var _section, _freqSelect, _freqInput, _bwSelect, _sfToggle, _statusLabel;
  var _metaEl, _canvas, _ctx, _overlayEl, _hoverEl, _detIndicator, _pktIndicator;
  var _pktDetail, _pktTbody;
  var _resolved = false;

  // -- Canvas dimensions --------------------------------------------------
  var _canvasW = 800, _canvasH = 400;
  var _detCanvas, _detCtx;
  var _detRowOffset = 0;

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
  var _rateHistory = [];         // last N rate samples for sparkline
  var MAX_RATE_SAMPLES = 12;

  // -- Decoded packet state ------------------------------------------------
  var _packets = [];             // ring buffer of decoded packets (newest first)
  var MAX_PACKETS = 128;
  var _pktCount = 0;
  var _pktExpanded = false;
  var _highlightedPktKey = null;

  // -- Contextual hover state (populated from periodic updates) -----------
  var _ourFreqHz = null, _ourBwHz = null, _ourSf = null, _ourCr = null;
  var _channelAnalysis = null;
  var _band = null;              // { lo, hi, label } for current center freq
  var _noiseFloorDb = null;      // most recent noise floor from status
  var _filterChannel = null;     // {centerMhz, bwKhz, idx} or null

  var _css = {};

  // -- DOM setup ----------------------------------------------------------

  function _resolveDom() {
    if (_resolved) return true;
    _section = $('chirp-viewer-section');
    if (!_section) return false;
    _freqSelect = $('chirp-freq-select');
    _freqInput = $('chirp-freq-input');
    _bwSelect = $('chirp-bw-select');
    _sfToggle = $('chirp-sf-toggle');
    _statusLabel = $('chirp-status-label');
    _metaEl = $('chirp-meta');
    _canvas = $('chirp-canvas');
    _overlayEl = $('chirp-overlay');
    _hoverEl = $('chirp-hover');
    _detIndicator = $('chirp-det-indicator');
    _pktIndicator = $('chirp-pkt-indicator');
    _pktDetail = $('chirp-pkt-detail');
    _pktTbody = $('chirp-pkt-tbody');

    if (_pktIndicator) {
      _pktIndicator.addEventListener('click', _togglePktDetail);
    }

    _detCanvas = $('chirp-det-canvas');
    if (_detCanvas) {
      _detCtx = _detCanvas.getContext('2d');
    }

    if (_canvas) {
      _ctx = _canvas.getContext('2d');
      _resizeCanvas();
      _canvas.addEventListener('mousemove', _onHover);
      _canvas.addEventListener('mouseleave', _onHoverLeave);
      _canvas.addEventListener('click', _onDetClick);
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
      _freqSelect.addEventListener('change', function () {
        var prev = _freqSelect.querySelector('[data-custom]');
        if (prev) prev.remove();
        _freqSelect.classList.remove('chirp-select--custom');
        if (_freqInput) _freqInput.value = parseFloat(_freqSelect.value).toFixed(3);
        _onParamChange();
      });
    }
    if (_freqInput) {
      _freqInput.addEventListener('change', function () {
        _syncSelectToInput();
        _onParamChange();
      });
      _freqInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); _freqInput.blur(); }
      });
    }
    if (_bwSelect) _bwSelect.addEventListener('change', function () {
      _populateFreqSelect();
      _syncSelectToInput();
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
    if (_detCanvas) {
      _detCanvas.width = _canvasW;
      _detCanvas.height = _canvasH;
    }
  }

  function _populateFreqSelect() {
    if (!_freqSelect) return;
    var prev = _freqInput ? parseFloat(_freqInput.value) : parseFloat(_freqSelect.value);
    _freqSelect.innerHTML = '';

    var bwHz = _bwSelect ? parseInt(_bwSelect.value) : 250000;
    var bwMhz = bwHz / 1e6;

    // Mesh network presets (off-grid frequencies)
    var meshPresets = [
      { mhz: 906.875,  label: 'LongFast 250k · 906.875' },
      { mhz: 912.8125, label: 'LongModerate 125k · 912.813' },
      { mhz: 905.3125, label: 'LongSlow 125k · 905.313' },
      { mhz: 905.0313, label: 'VLongSlow 62.5k · 905.031' },
      { mhz: 913.125,  label: 'MediumFast 250k · 913.125' },
      { mhz: 914.875,  label: 'MediumSlow 250k · 914.875' },
      { mhz: 918.875,  label: 'ShortFast 250k · 918.875' },
      { mhz: 920.625,  label: 'ShortSlow 250k · 920.625' },
      { mhz: 926.75,   label: 'ShortTurbo 500k · 926.750' },
    ];
    var meshGroup = document.createElement('optgroup');
    meshGroup.label = 'Meshtastic US';
    for (var mi = 0; mi < meshPresets.length; mi++) {
      var mp = meshPresets[mi];
      var mopt = document.createElement('option');
      mopt.value = mp.mhz.toFixed(4);
      mopt.textContent = mp.label;
      meshGroup.appendChild(mopt);
    }
    _freqSelect.appendChild(meshGroup);

    var mcGroup = document.createElement('optgroup');
    mcGroup.label = 'MeshCore US';
    var mcOpt = document.createElement('option');
    mcOpt.value = '910.5250';
    mcOpt.textContent = 'Default 62.5k · 910.525';
    mcGroup.appendChild(mcOpt);
    _freqSelect.appendChild(mcGroup);

    var rnGroup = document.createElement('optgroup');
    rnGroup.label = 'Reticulum RNode';
    var rnOpt = document.createElement('option');
    rnOpt.value = '915.0000';
    rnOpt.textContent = 'Default 125k · 915.000';
    rnGroup.appendChild(rnOpt);
    _freqSelect.appendChild(rnGroup);

    // LoRaWAN channel grid regions
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
    if (_freqInput && !_freqInput.value && _freqSelect.value) {
      _freqInput.value = parseFloat(_freqSelect.value).toFixed(3);
    }
  }

  function _syncSelectToInput() {
    if (!_freqSelect || !_freqInput) return;
    var mhz = parseFloat(_freqInput.value);
    if (isNaN(mhz)) return;
    // Remove any previous "Custom" placeholder
    var prev = _freqSelect.querySelector('[data-custom]');
    if (prev) prev.remove();
    for (var i = 0; i < _freqSelect.options.length; i++) {
      if (Math.abs(parseFloat(_freqSelect.options[i].value) - mhz) < 0.05) {
        _freqSelect.selectedIndex = i;
        _freqSelect.classList.remove('chirp-select--custom');
        return;
      }
    }
    // No preset match — show "Custom" so the dropdown doesn't mislead
    var opt = document.createElement('option');
    opt.value = mhz.toFixed(4);
    opt.textContent = '• ' + mhz.toFixed(3) + ' MHz';
    opt.setAttribute('data-custom', '1');
    _freqSelect.insertBefore(opt, _freqSelect.firstChild);
    _freqSelect.selectedIndex = 0;
    _freqSelect.classList.add('chirp-select--custom');
  }

  // -- Parameter changes --------------------------------------------------

  function _onParamChange() {
    var freqMhz = _freqInput ? parseFloat(_freqInput.value)
      : (_freqSelect ? parseFloat(_freqSelect.value) : null);
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
    _detRowOffset = 0;
    _packets = [];
    _pktCount = 0;
    _pktExpanded = false;
    if (_pktDetail) _pktDetail.style.display = 'none';
    if (_pktTbody) _pktTbody.innerHTML = '';
    if (_pktIndicator) _pktIndicator.classList.remove('open');
    _updateDetIndicator();
    if (_ctx) {
      _ctx.fillStyle = _css.wfBg || '#050810';
      _ctx.fillRect(0, 0, _canvasW, _canvasH);
    }
    if (_detCtx) {
      _detCtx.clearRect(0, 0, _canvasW, _canvasH);
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
      _detRowOffset++;
    }

    // Trim history
    if (_rows.length > MAX_HIST) {
      _rows.length = MAX_HIST;
      _rowTimestamps.length = MAX_HIST;
    }

    _repaintDetections();
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
    _repaintDetections();
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

    var timeResMs = m.time_res_ms || 31;
    var bws = m.detection_bws || [_LORA_BW_HZ];
    var slopesMap = m.sf_slopes;

    // sf_slopes may be a dict keyed by BW or a flat array (legacy)
    var slopesByBw = {};
    if (Array.isArray(slopesMap)) {
      slopesByBw[_LORA_BW_HZ] = slopesMap;
    } else {
      slopesByBw = slopesMap;
    }

    for (var bi = 0; bi < bws.length; bi++) {
      var bw = bws[bi];
      var slopes = slopesByBw[bw];
      if (!slopes) continue;
      var isDimmed = bi > 0;

    for (var si = 0; si < slopes.length; si++) {
      var s = slopes[si];
      var sf = s.sf;
      var tSymMs = s.t_symbol_ms;

      var rowsPerSym = tSymMs / timeResMs;
      var pixPerSym = rowsPerSym * (cH / _canvasH);
      var bwFrac = bw / (m.sample_rate || 250000);
      var chirpWidthPx = cW * bwFrac;
      var angleDeg = Math.atan2(pixPerSym, chirpWidthPx) * (180 / Math.PI);

      var color = SF_COLORS[sf] || '#ffffff';
      var lineLen = Math.sqrt(chirpWidthPx * chirpWidthPx + pixPerSym * pixPerSym);

      var opacity = isDimmed ? '0.4' : '0.7';

      var guide = document.createElement('div');
      guide.className = 'chirp-sf-line';
      guide.style.cssText =
        'left:0;bottom:0;width:' + lineLen.toFixed(0) + 'px;' +
        'border-color:' + color + ';opacity:' + opacity + ';' +
        'transform:rotate(-' + angleDeg.toFixed(1) + 'deg);';
      _overlayEl.appendChild(guide);

      var bwKhz = bw / 1000;
      var bwStr = bws.length > 1 ? ' ' + bwKhz + 'k' : '';
      var label = document.createElement('div');
      label.className = 'chirp-sf-label';
      label.style.cssText =
        'left:4px;bottom:' + (pixPerSym + 2).toFixed(0) + 'px;color:' + color +
        ';opacity:' + opacity + ';';
      label.textContent = 'SF' + sf + bwStr;
      _overlayEl.appendChild(label);
    }
    } // end bws loop
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
      _hoverEl.classList.remove('det-tooltip');
      _highlightPktRow(null);
      return;
    }

    var canvasX = Math.round(fracX * _canvasW);
    var canvasY = Math.round(fracY * _canvasH);
    var hitDet = _findDetectionNear(canvasX, canvasY);
    if (hitDet) {
      _showDetectionTooltip(hitDet);
      return;
    }
    _hoverEl.classList.remove('det-tooltip');
    _highlightPktRow(null);

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
    if (_hoverEl) {
      _hoverEl.style.display = 'none';
      _hoverEl.classList.remove('det-tooltip');
    }
    _highlightPktRow(null);
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
      if (st.noise_floor_db != null && st.noise_floor_db > -120) {
        _noiseFloorDb = st.noise_floor_db;
        _updateDetIndicator();
      }
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

  var _LORA_BW_HZ = 125000;

  function _detActualFreqHz(det) {
    var centerHz = det.freq_center_hz || (_lastMeta ? _lastMeta.freq_center_hz : 0);
    if (!centerHz) return null;
    var offset = det.freq_offset_hz || 0;
    var bw = det.detection_bw || _LORA_BW_HZ;
    if (offset > bw / 2) offset -= bw;
    return centerHz + offset;
  }

  function _detFreqToX(det) {
    if (!_lastMeta) return -1;
    var sr = _lastMeta.sample_rate || 250000;
    var centerHz = _lastMeta.freq_center_hz || 0;
    var loHz = centerHz - sr / 2;
    var spanHz = sr;
    if (spanHz <= 0) return -1;
    var freqHz = _detActualFreqHz(det);
    if (freqHz == null) return -1;
    var fracX = (freqHz - loHz) / spanHz;
    if (fracX < 0 || fracX > 1) return -1;
    return Math.round(fracX * _canvasW);
  }

  function _linkDetectionToPacket(det) {
    det._pkt = null;
    for (var i = 0; i < _packets.length; i++) {
      var p = _packets[i];
      if (p.timestamp === det.timestamp && p.sf === det.sf) {
        det._pkt = p;
        return;
      }
    }
  }

  function _relinkAllDetections() {
    for (var i = 0; i < _detections.length; i++) _linkDetectionToPacket(_detections[i]);
  }

  function _paintDetectionMarkerAt(ctx, det, y, alphaScale) {
    var x = _detFreqToX(det);
    if (x < 0) return;
    var color = SF_COLORS[det.sf] || '#ffffff';
    var sz = 10;
    var pkt = det._pkt;
    var a = alphaScale || 1.0;

    ctx.save();

    // Full-height vertical line
    ctx.globalAlpha = 0.55 * a;
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.shadowColor = color;
    ctx.shadowBlur = 6;
    ctx.beginPath();
    ctx.moveTo(x, y + sz * 1.4);
    ctx.lineTo(x, _canvasH);
    ctx.stroke();
    ctx.shadowBlur = 0;

    // Triangle fill
    ctx.globalAlpha = 0.9 * a;
    ctx.fillStyle = color;
    ctx.shadowColor = color;
    ctx.shadowBlur = 8;
    ctx.beginPath();
    ctx.moveTo(x - sz, y);
    ctx.lineTo(x + sz, y);
    ctx.lineTo(x, y + sz * 1.4);
    ctx.closePath();
    ctx.fill();
    ctx.shadowBlur = 0;

    // Sync word outline on triangle
    if (pkt && pkt.sync_word != null) {
      ctx.strokeStyle = SYNC_COLORS[pkt.sync_word] || '#888';
      ctx.lineWidth = 2;
      ctx.globalAlpha = 1.0 * a;
      ctx.beginPath();
      ctx.moveTo(x - sz, y);
      ctx.lineTo(x + sz, y);
      ctx.lineTo(x, y + sz * 1.4);
      ctx.closePath();
      ctx.stroke();
    }

    // CRC badge
    var badgeOffset = 0;
    if (pkt) {
      var crcColor = pkt.crc_ok === true ? '#4f4'
                   : pkt.crc_ok === false ? '#f44' : '#888';
      ctx.globalAlpha = 0.9 * a;
      ctx.fillStyle = crcColor;
      ctx.beginPath();
      ctx.arc(x + sz + 6, y + sz * 0.7, 4, 0, 2 * Math.PI);
      ctx.fill();
      badgeOffset = 14;
    }

    // Label: SF + frequency + SNR
    ctx.globalAlpha = 1.0 * a;
    ctx.font = 'bold 11px monospace';
    ctx.fillStyle = color;
    var freqHz = _detActualFreqHz(det);
    var freqStr = freqHz != null ? ' ' + (freqHz / 1e6).toFixed(2) : '';
    var detBwKhz = (det.detection_bw || 125000) / 1000;
    var bwStr = detBwKhz !== 125 ? ' ' + detBwKhz + 'k' : '';
    var label = 'SF' + det.sf + bwStr + freqStr;
    var snr = det.snr_db != null ? ' ' + det.snr_db.toFixed(1) + 'dB' : '';
    var fullLabel = label + snr;
    var textW = ctx.measureText(fullLabel).width;
    var labelX = x + sz + 4 + badgeOffset;
    var labelY = y + sz * 1.2;
    if (labelX + textW > _canvasW) labelX = x - sz - 4 - textW;
    ctx.fillText(fullLabel, labelX, labelY);

    ctx.restore();
  }

  function _repaintDetections() {
    if (!_detCtx) return;
    _detCtx.clearRect(0, 0, _canvasW, _canvasH);
    for (var i = _detections.length - 1; i >= 0; i--) {
      var det = _detections[i];
      if (det._arrivalRow == null) continue;
      if (!_detMatchesFilter(det)) continue;
      var y = _detRowOffset - det._arrivalRow;
      if (y < 0 || y >= _canvasH) continue;
      var ageFrac = _canvasH > 0 ? Math.max(0.25, 1.0 - y / _canvasH) : 1.0;
      var conf = det.confidence != null ? 0.4 + 0.6 * det.confidence : 1.0;
      _paintDetectionMarkerAt(_detCtx, det, y, ageFrac * conf);
    }
  }

  function _findDetectionNear(canvasX, canvasY) {
    var hitRadius = 18;
    var best = null, bestDist = Infinity;
    for (var i = 0; i < _detections.length; i++) {
      var det = _detections[i];
      if (det._arrivalRow == null) continue;
      var markerY = _detRowOffset - det._arrivalRow;
      if (markerY < 0 || markerY >= _canvasH) continue;
      var dy = canvasY - markerY;
      if (Math.abs(dy) > hitRadius) continue;
      var markerX = _detFreqToX(det);
      if (markerX < 0) continue;
      var dx = canvasX - markerX;
      var dist = Math.sqrt(dx * dx + dy * dy);
      if (dist < hitRadius && dist < bestDist) {
        bestDist = dist;
        best = det;
      }
    }
    return best;
  }

  function _showDetectionTooltip(det) {
    if (!_hoverEl) return;
    var lines = [];
    var freqHz = _detActualFreqHz(det);
    if (freqHz) lines.push((freqHz / 1e6).toFixed(3) + ' MHz');
    lines.push('SF' + det.sf + ' · ' + det.snr_db.toFixed(1) + ' dB SNR');

    var pkt = det._pkt;
    if (pkt) {
      var syncName = SYNC_NAMES[pkt.sync_word] || ('0x' + (pkt.sync_word || 0).toString(16));
      lines.push('Sync: ' + syncName);
      lines.push('CR 4/' + (4 + (pkt.cr || 0)));
      var crcStr = pkt.crc_ok === true ? 'CRC OK'
                 : pkt.crc_ok === false ? 'CRC FAIL' : 'no CRC';
      if (pkt.errors_corrected) crcStr += ' (' + pkt.errors_corrected + ' corrected)';
      lines.push(crcStr);
      if (pkt.payload_len != null) lines.push(pkt.payload_len + ' bytes');
      if (pkt.payload_hex) {
        var hex = pkt.payload_hex.length > 48
          ? pkt.payload_hex.substring(0, 48) + '…'
          : pkt.payload_hex;
        lines.push(hex);
      }
    } else {
      lines.push('(no decoded packet)');
    }

    var age = det.timestamp ? SC.formatAge(Date.now() / 1000 - det.timestamp) : '';
    if (age) lines.push(age);

    _hoverEl.innerHTML = lines.map(function (l) { return esc(l); }).join('<br>');
    _hoverEl.classList.add('det-tooltip');
    _hoverEl.style.display = 'block';

    if (pkt) {
      _highlightPktRow(pkt.timestamp + ':' + pkt.sf);
    } else {
      _highlightPktRow(null);
    }
  }

  function _highlightPktRow(key) {
    if (key === _highlightedPktKey) return;
    if (_highlightedPktKey && _pktTbody) {
      var prev = _pktTbody.querySelector('[data-pkt-key="' + _highlightedPktKey + '"]');
      if (prev) prev.classList.remove('pkt-highlight');
    }
    _highlightedPktKey = key;
    if (!key || !_pktTbody) return;
    var row = _pktTbody.querySelector('[data-pkt-key="' + key + '"]');
    if (row) row.classList.add('pkt-highlight');
  }

  function _onDetClick(ev) {
    var rect = _canvas.getBoundingClientRect();
    var fracX = (ev.clientX - rect.left) / rect.width;
    var fracY = (ev.clientY - rect.top) / rect.height;
    var canvasX = Math.round(fracX * _canvasW);
    var canvasY = Math.round(fracY * _canvasH);
    var det = _findDetectionNear(canvasX, canvasY);
    if (!det || !det._pkt) return;
    if (!_pktExpanded) _togglePktDetail();
    var key = det._pkt.timestamp + ':' + det._pkt.sf;
    _highlightPktRow(key);
    if (_pktTbody) {
      var row = _pktTbody.querySelector('[data-pkt-key="' + key + '"]');
      if (row) row.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }

  function _timestampToRow(ts) {
    if (!ts || !_rowTimestamps.length) return _detRowOffset;
    var best = 0, bestDist = Infinity;
    for (var i = 0; i < _rowTimestamps.length; i++) {
      if (_rowTimestamps[i] == null) continue;
      var d = Math.abs(_rowTimestamps[i] - ts);
      if (d < bestDist) { bestDist = d; best = i; }
    }
    return _detRowOffset - best;
  }

  function _detMatchesFilter(det) {
    if (!_filterChannel) return true;
    var freqHz = _detActualFreqHz(det);
    if (freqHz == null) return true;
    var freqMhz = freqHz / 1e6;
    var lo = _filterChannel.centerMhz - _filterChannel.bwKhz / 2000;
    var hi = _filterChannel.centerMhz + _filterChannel.bwKhz / 2000;
    return freqMhz >= lo && freqMhz <= hi;
  }

  function _filteredDetections() {
    if (!_filterChannel) return _detections;
    var out = [];
    for (var i = 0; i < _detections.length; i++) {
      if (_detMatchesFilter(_detections[i])) out.push(_detections[i]);
    }
    return out;
  }

  function setFilterChannel(info) {
    _filterChannel = info;
    _repaintDetections();
    _updateDetIndicator();
  }

  function _clearFilter() {
    _filterChannel = null;
    if (R.loraSpectrum && R.loraSpectrum.clearChannelSelection) {
      R.loraSpectrum.clearChannelSelection();
    }
    _repaintDetections();
    _updateDetIndicator();
  }

  function _detectionRate() {
    if (_detections.length < 2) return null;
    var now = Date.now() / 1000;
    var windowSec = 60;
    var count = 0;
    for (var i = 0; i < _detections.length; i++) {
      if (_detections[i].timestamp && (now - _detections[i].timestamp) <= windowSec) {
        count++;
      } else {
        break;
      }
    }
    if (count < 1) return null;
    var oldest = _detections[Math.min(count - 1, _detections.length - 1)].timestamp;
    var span = now - oldest;
    if (span < 1) return null;
    return (count / span) * 60;
  }

  function _buildSparkline(values) {
    if (values.length < 2) return '';
    var w = 60, h = 16;
    var max = 0;
    for (var i = 0; i < values.length; i++) { if (values[i] > max) max = values[i]; }
    if (max <= 0) return '';
    var pts = [];
    for (var j = 0; j < values.length; j++) {
      var x = (j / (values.length - 1)) * w;
      var y = h - (values[j] / max) * (h - 2) - 1;
      pts.push(x.toFixed(1) + ',' + y.toFixed(1));
    }
    return ' <svg class="det-sparkline" width="' + w + '" height="' + h +
      '" viewBox="0 0 ' + w + ' ' + h + '">' +
      '<polyline points="' + pts.join(' ') + '" fill="none" stroke="var(--accent,#43c9ff)" ' +
      'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  }

  function _buildSfChips(detections) {
    var counts = {};
    for (var i = 0; i < detections.length; i++) {
      var sf = detections[i].sf;
      counts[sf] = (counts[sf] || 0) + 1;
    }
    if (detections.length === 0) return '';
    var html = '<div class="det-sf-chips">';
    var sfs = [7, 8, 9, 10, 11, 12];
    for (var j = 0; j < sfs.length; j++) {
      var s = sfs[j];
      var n = counts[s] || 0;
      if (n === 0) continue;
      var color = SF_COLORS[s] || '#fff';
      html += '<span class="det-sf-chip" style="border-color:' + color +
        ';color:' + color + '">SF' + s +
        ' <span class="det-sf-chip-n">×' + n + '</span></span>';
    }
    html += '</div>';
    return html;
  }

  function _updateDetIndicator() {
    if (!_detIndicator) return;
    var dets = _filteredDetections();
    var count = dets.length;
    if (count === 0 && !_filterChannel) {
      _detIndicator.style.display = 'none';
      return;
    }
    _detIndicator.style.display = '';

    var filterHtml = '';
    if (_filterChannel) {
      filterHtml = '<span class="det-filter-chip" id="det-filter-clear">Ch ' +
        esc(String(_filterChannel.idx)) + ' · ' +
        esc(_filterChannel.centerMhz.toFixed(3)) + ' MHz ✕</span>';
    }

    if (count === 0) {
      _detIndicator.innerHTML = filterHtml +
        '<span class="det-count">0 detections</span>';
      return;
    }

    var recent = dets[0];
    var sfText = 'SF' + recent.sf;
    var snrText = recent.snr_db.toFixed(1) + ' dB';
    var rate = _detectionRate();
    if (rate != null) {
      _rateHistory.push(rate);
      if (_rateHistory.length > MAX_RATE_SAMPLES) _rateHistory.shift();
    }
    var rateText = rate != null ? rate.toFixed(1) + '/min' : '';
    var totalCount = _filterChannel ? _detections.length : _detCount;
    var countLabel = String(count) + (totalCount > count ? ' of ' + totalCount : '') +
      ' detection' + (count === 1 ? '' : 's');
    var nfHtml = '';
    if (_noiseFloorDb != null) {
      var nfColor = _noiseFloorDb > -30 ? '#f44' : _noiseFloorDb > -50 ? '#ff9f43' : '#6bff6b';
      nfHtml = ' <span class="det-nf" style="color:' + nfColor + '">NF ' +
        esc(_noiseFloorDb.toFixed(0) + ' dB') + '</span>';
    }
    _detIndicator.innerHTML = filterHtml +
      '<span class="det-count">' + esc(countLabel) + '</span>' +
      ' <span class="det-latest" style="color:' +
        (SF_COLORS[recent.sf] || '#fff') + '">' +
        esc(sfText) + ' · ' + esc(snrText) + '</span>' +
      (rateText ? ' <span class="det-rate">' + esc(rateText) + '</span>' : '') +
      _buildSparkline(_rateHistory) +
      nfHtml +
      _buildSfChips(dets);

    var clearBtn = document.getElementById('det-filter-clear');
    if (clearBtn) clearBtn.addEventListener('click', _clearFilter);
  }

  function handleDetection(det) {
    if (!det) return;
    if (!_resolveDom()) return;
    det._arrivalRow = _detRowOffset;
    _linkDetectionToPacket(det);
    _detections.unshift(det);
    if (_detections.length > MAX_DETECTIONS) _detections.length = MAX_DETECTIONS;
    _detCount++;
    _repaintDetections();
    _updateDetIndicator();
    if (_detIndicator) {
      _detIndicator.classList.remove('det-flash');
      void _detIndicator.offsetWidth;
      _detIndicator.classList.add('det-flash');
    }
  }

  function handleDetectionHistory(data) {
    if (!data || !data.length) return;
    if (!_resolveDom()) return;
    _detections = [];
    for (var i = data.length - 1; i >= 0; i--) {
      var det = data[i];
      det._arrivalRow = _timestampToRow(det.timestamp);
      _detections.unshift(det);
    }
    if (_detections.length > MAX_DETECTIONS) _detections.length = MAX_DETECTIONS;
    _detCount = data.length;
    _relinkAllDetections();
    _updateDetIndicator();
    _repaintDetections();
  }

  // -- Decoded packet indicator -------------------------------------------

  function _updatePktIndicator() {
    if (!_pktIndicator) return;
    if (_pktCount === 0) {
      _pktIndicator.style.display = 'none';
      return;
    }
    _pktIndicator.style.display = '';
    _pktIndicator.classList.add('expandable');
    var p = _packets.length > 0 ? _packets[0] : null;
    var parts = '<span class="pkt-chevron">▶</span>' +
      '<span class="pkt-count">' + esc(String(_pktCount)) + ' packet' +
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
    var linked = false;
    for (var i = 0; i < _detections.length; i++) {
      if (!_detections[i]._pkt && _detections[i].timestamp === pkt.timestamp && _detections[i].sf === pkt.sf) {
        _detections[i]._pkt = pkt;
        linked = true;
        break;
      }
    }
    if (linked) _repaintDetections();
    _updatePktIndicator();
    if (_pktExpanded && _pktTbody) {
      var now = Date.now() / 1000;
      var row = _pktRow(pkt, now);
      _pktTbody.insertBefore(row, _pktTbody.firstChild);
      if (_pktTbody.children.length > MAX_PACKETS) {
        _pktTbody.removeChild(_pktTbody.lastChild);
      }
    }
  }

  function handlePacketHistory(data) {
    if (!data || !data.length) return;
    if (!_resolveDom()) return;
    _packets = [];
    for (var i = data.length - 1; i >= 0; i--) {
      _packets.push(data[i]);
    }
    if (_packets.length > MAX_PACKETS) _packets.length = MAX_PACKETS;
    _pktCount = data.length;
    _relinkAllDetections();
    _updatePktIndicator();
    if (_pktExpanded) _rebuildPktTable();
  }

  function _togglePktDetail() {
    _pktExpanded = !_pktExpanded;
    if (_pktIndicator) {
      _pktIndicator.classList.toggle('open', _pktExpanded);
    }
    if (_pktDetail) {
      _pktDetail.style.display = _pktExpanded ? '' : 'none';
    }
    if (_pktExpanded) _rebuildPktTable();
  }

  function _rebuildPktTable() {
    if (!_pktTbody) return;
    _pktTbody.innerHTML = '';
    var now = Date.now() / 1000;
    for (var i = 0; i < _packets.length; i++) {
      _pktTbody.appendChild(_pktRow(_packets[i], now));
    }
  }

  function _pktRow(p, now) {
    var tr = document.createElement('tr');
    if (p.timestamp != null && p.sf != null) {
      tr.setAttribute('data-pkt-key', p.timestamp + ':' + p.sf);
    }
    var age = (p.timestamp != null) ? SC.formatAge(now - p.timestamp) : '--';
    var sfColor = SF_COLORS[p.sf] || '#fff';
    var crText = 'CR4/' + (4 + (p.cr || 0));
    var crcText, crcClass;
    if (p.crc_ok === true) { crcText = 'OK'; crcClass = 'crc-ok'; }
    else if (p.crc_ok === false) { crcText = 'FAIL'; crcClass = 'crc-fail'; }
    else { crcText = '--'; crcClass = 'crc-none'; }
    var hex = p.payload_hex || '';
    var hexShort = hex.length > 48 ? hex.substring(0, 48) + '…' : hex;

    tr.innerHTML =
      '<td>' + esc(age) + '</td>' +
      '<td style="color:' + sfColor + '">SF' + esc(String(p.sf)) + '</td>' +
      '<td>' + esc(crText) + '</td>' +
      '<td class="' + crcClass + '">' + esc(crcText) + '</td>' +
      '<td>' + esc(String(p.payload_len != null ? p.payload_len : '')) + '</td>' +
      '<td class="pkt-hex-cell" title="' + esc(hex) + '">' + esc(hexShort) + '</td>';
    return tr;
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
    if (!_resolveDom()) return;
    if (_freqInput) _freqInput.value = mhz.toFixed(3);
    _syncSelectToInput();
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
    handlePacketHistory: handlePacketHistory,
    setFreqMhz: setFreqMhz,
    setFilterChannel: setFilterChannel,
  };
})();
