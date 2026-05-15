/* ReticulumPi Dashboard — SDR Spectrum module
 *
 * Renders two synchronised views of rtl_power sweep data:
 *   • SVG line plot of the most recent sweep (power dB vs frequency)
 *   • HTML canvas waterfall — scrolls down by one row per new sweep
 *
 * The server snapshot includes only the LATEST sweep plus a small tail
 * of recent sweeps; the client maintains its own in-browser scrolling
 * waterfall history so the wire payload stays small even for wide
 * spans.  Sweep de-duplication uses the server-side ``sweep_count``.
 *
 * Colour ramp: a hand-rolled 6-stop "turbo"-style gradient (dark navy
 * → cyan → green → yellow → orange → red).  No external colour libs.
 */
(function () {
  'use strict';
  var R = window.RPI;
  if (!R) return;
  var $ = R.$, esc = R.esc;
  var SC = R.spectrumCommon;
  if (!SC) { /* common module not loaded — render nothing */ return; }

  // -- DOM handles (resolved on first data arrival) ------------------------
  var _section = null, _body = null, _toggle = null, _countEl = null;
  var _spanEl, _binsInfoEl, _gainEl, _sweepsEl, _statusEl, _freshnessEl;
  var _plotWrap, _lineEl, _wfCanvas, _wfCtx, _hoverEl, _scaleEl;
  var _bandsEl, _overlayEl, _legendEl, _legendToggleEl;

  // -- Runtime state -------------------------------------------------------
  var _expanded = false;
  var _lastSweepCount = 0;       // waterfall paint bookkeeping
  var _lastRenderedSweep = 0;    // line-plot redraw bookkeeping (separate
                                 // so the two can't drift relative to each
                                 // other after a bin-count reset).
  var _lastData = null;      // most recent snapshot (for redraw on resize/expand)
  var _minDb = -90, _maxDb = -30;  // running auto-scale for colour + line plot
  var _scaleInitialized = false;   // true once a real sweep has seeded _min/_maxDb
  var _lastBandSignature = '';     // "<start>|<stop>" of last band-strip render
  var _legendBuilt = false;        // one-time flag for legend DOM construction
  var _legendExpanded = false;
  var _placeholderEl = null;
  var _hasReceivedData = false;
  var _cachedBinsHz = null;
  var _css = {};

  // -- Zoom state -----------------------------------------------------------
  var _zoom = null;             // null = full band, [loMhz, hiMhz] = zoomed
  var _dragState = null;        // { startFrac, curFrac } during drag
  var _dragRafId = null;        // rAF handle for throttled drag preview
  var _peakHoldEnabled = false;
  var _peakHoldDb = null;       // Float64Array, one per bin
  var _zoomResetEl = null;
  var _peakHoldToggleEl = null;
  var _LS_ZOOM = 'rpi_spectrum_zoom';
  // Cursor into the shared historyStore — bumps on WS-hello reload or on
  // a bin-grid reset.  When it changes we wipe the canvas and bulk-paint
  // from the store; between bumps we just append live tail rows.
  var _lastStoreGen = -1;
  var _needsBulkPaint = false;

  // Waterfall canvas native dims — scaled up via CSS to fit panel width.
  var WF_ROWS = 256;
  var WF_COLS = 800;

  // -- Zoom helpers ---------------------------------------------------------
  function _clip(data, range) {
    var bins = data.bins_hz;
    if (!bins || bins.length === 0) return null;
    var lo = bins[0] / 1e6, hi = bins[bins.length - 1] / 1e6;
    var zoomed = false;
    if (range && range.length === 2) {
      lo = Math.max(lo, range[0]);
      hi = Math.min(hi, range[1]);
      if (hi <= lo) return null;
      zoomed = true;
    }
    var loHz = lo * 1e6, hiHz = hi * 1e6;
    var loIdx = -1, hiIdx = -1;
    for (var i = 0; i < bins.length; i++) {
      if (bins[i] >= loHz) { loIdx = i; break; }
    }
    for (var j = bins.length - 1; j >= 0; j--) {
      if (bins[j] <= hiHz) { hiIdx = j; break; }
    }
    if (loIdx < 0 || hiIdx < 0 || hiIdx < loIdx) return null;
    var binStepKhz = (bins.length > 1) ? (bins[1] - bins[0]) / 1000 : 0;
    return {
      loIdx: loIdx, hiIdx: hiIdx,
      loMhz: bins[loIdx] / 1e6,
      hiMhz: bins[hiIdx] / 1e6,
      binStepKhz: binStepKhz,
      zoomed: zoomed,
    };
  }

  function _setZoom(range, silent) {
    _zoom = range;
    _lastBandSignature = '';
    _peakHoldDb = null;
    try { localStorage.setItem(_LS_ZOOM, range ? JSON.stringify(range) : ''); } catch (e) {}
    if (silent) {
      _lastRenderedSweep = 0;
      if (_wfCtx) {
        _wfCtx.fillStyle = _css.wfBg || '#050810';
        _wfCtx.fillRect(0, 0, WF_COLS, WF_ROWS);
      }
      return;
    }
    if (_zoomResetEl) _zoomResetEl.style.display = range ? '' : 'none';
    if (_lastData) {
      var clip = _clip(_lastData, _zoom);
      _renderLine(_lastData, clip);
      _renderBands(_lastData, clip);
      _renderScale(_lastData, clip);
      _repaintWaterfallFromHistory(clip);
    }
  }

  function _repaintWaterfallFromHistory(clip) {
    if (!_wfCtx || !SC.historyStore) return;
    var rows = SC.historyStore.rows;
    if (!rows.length) return;
    var chron = new Array(rows.length);
    for (var i = 0; i < rows.length; i++) {
      chron[rows.length - 1 - i] = rows[i];
    }
    if (clip && clip.zoomed) {
      for (var k = 0; k < chron.length; k++) {
        if (chron[k]) chron[k] = chron[k].slice(clip.loIdx, clip.hiIdx + 1);
      }
    }
    SC.paintHistoryToCanvas(_wfCtx, _wfCanvas, chron, WF_COLS, WF_ROWS,
                            _minDb, _maxDb);
    _lastSweepCount = SC.historyStore.sweepCount;
  }

  // -- DOM setup -----------------------------------------------------------
  function _resizeCanvas() {
    if (!_wfCanvas || !_wfCtx) return false;
    var container = _wfCanvas.parentElement;
    if (!container) return false;
    var newCols = Math.max(400, Math.min(container.clientWidth, 1920));
    newCols = (newCols + 1) & ~1;
    if (newCols === WF_COLS && _wfCanvas.width === WF_COLS) return false;
    WF_COLS = newCols;
    _wfCanvas.width = WF_COLS;
    _wfCanvas.height = WF_ROWS;
    _wfCtx.fillStyle = '#050810';
    _wfCtx.fillRect(0, 0, WF_COLS, WF_ROWS);
    _needsBulkPaint = true;
    _lastSweepCount = 0;
    return true;
  }

  function _resolveDom() {
    if (_section) return true;
    _section = $('spectrum-section');
    if (!_section) return false;
    _body = $('spectrum-body');
    _toggle = $('spectrum-toggle');
    _countEl = $('spectrum-count');
    _spanEl = $('spectrum-span');
    _binsInfoEl = $('spectrum-bins-info');
    _gainEl = $('spectrum-gain');
    _sweepsEl = $('spectrum-sweeps');
    _statusEl = $('spectrum-status');
    _freshnessEl = $('spectrum-freshness');
    _lineEl = $('spectrum-line');
    _wfCanvas = $('spectrum-waterfall');
    _hoverEl = $('spectrum-hover');
    _scaleEl = $('spectrum-scale');
    _bandsEl = $('spectrum-bands');
    _overlayEl = $('spectrum-overlay');
    _legendEl = $('spectrum-legend');
    _legendToggleEl = $('spectrum-legend-toggle');
    _plotWrap = _wfCanvas ? _wfCanvas.parentNode : null;

    if (_wfCanvas) {
      _wfCtx = _wfCanvas.getContext('2d');
      _resizeCanvas();
      _wfCanvas.addEventListener('mousemove', _onHover);
      _wfCanvas.addEventListener('mouseleave', _onHoverLeave);
      var wfParent = _wfCanvas.parentElement;
      if (wfParent && typeof ResizeObserver !== 'undefined') {
        new ResizeObserver(function () {
          if (!_resizeCanvas()) return;
          var clip = _lastData ? _clip(_lastData, _zoom) : null;
          _bulkPaintFromStore(clip);
        }).observe(wfParent);
      }
    }

    if (_toggle) _toggle.addEventListener('click', _onToggleClick);
    if (_legendToggleEl) _legendToggleEl.addEventListener('click', _onLegendToggleClick);

    // Show section immediately with a placeholder message
    _section.style.display = '';
    _placeholderEl = document.createElement('div');
    _placeholderEl.className = 'spectrum-placeholder';
    _placeholderEl.textContent = 'Waiting for spectrum data…';
    if (_body) _body.insertBefore(_placeholderEl, _body.firstChild);

    // Cache CSS custom properties once
    var cs = getComputedStyle(document.documentElement);
    _css.wfBg  = cs.getPropertyValue('--wf-bg').trim()      || '#050810';
    _css.grid  = cs.getPropertyValue('--spec-grid').trim()   || '#0f1525';
    _css.label = cs.getPropertyValue('--spec-label').trim()  || '#3a4565';
    _css.cyan  = cs.getPropertyValue('--cyan').trim()        || '#00e5ff';
    _css.peakHold = cs.getPropertyValue('--lora-peak-hold').trim() || 'rgba(255,182,39,0.65)';

    // Zoom chip + peak hold toggle (injected by index.html)
    _zoomResetEl = $('spectrum-zoom-reset');
    _peakHoldToggleEl = $('spectrum-peakhold-toggle');

    // Drag-to-zoom on line plot
    if (_lineEl) _lineEl.addEventListener('mousedown', _onDragStart);
    window.addEventListener('mousemove', _onDragMove);
    window.addEventListener('mouseup', _onDragEnd);
    if (_plotWrap) _plotWrap.addEventListener('dblclick', _onDoubleClickReset);
    if (_zoomResetEl) _zoomResetEl.addEventListener('click', _onZoomChipClick);
    if (_peakHoldToggleEl) _peakHoldToggleEl.addEventListener('click', _onPeakHoldToggle);

    // Restore zoom from localStorage
    try {
      var zs = localStorage.getItem(_LS_ZOOM);
      if (zs) {
        var zp = JSON.parse(zs);
        if (Array.isArray(zp) && zp.length === 2
            && typeof zp[0] === 'number' && typeof zp[1] === 'number'
            && zp[1] > zp[0]) {
          _zoom = zp;
          if (_zoomResetEl) _zoomResetEl.style.display = '';
        }
      }
    } catch (e) {}

    return true;
  }

  function _onLegendToggleClick() {
    if (!_legendEl) return;
    _legendExpanded = _legendEl.classList.contains('hidden');
    _legendEl.classList.toggle('hidden');
    var chev = _legendToggleEl ? _legendToggleEl.querySelector('.chevron') : null;
    if (chev) chev.innerHTML = _legendExpanded ? '&#9662;' : '&#9656;';
  }

  function _onToggleClick() {
    if (!_body) return;
    _expanded = _body.classList.contains('hidden');
    _body.classList.toggle('hidden');
    var chev = _toggle.querySelector('.chevron');
    if (chev) chev.innerHTML = _expanded ? '&#9662;' : '&#9656;';
    if (_expanded && _lastData) {
      var clip = _clip(_lastData, _zoom);
      _renderMeta(_lastData);
      _lastBandSignature = '';
      _renderBands(_lastData, clip);
      _buildLegend();
      _renderLine(_lastData, clip);
    }
  }

  // -- Drag-to-zoom --------------------------------------------------------
  function _onDragStart(ev) {
    if (!_lineEl || !_lastData) return;
    if (ev.button !== 0) return;
    var rect = _lineEl.getBoundingClientRect();
    var frac = (ev.clientX - rect.left) / rect.width;
    if (frac < 0 || frac > 1) return;
    _dragState = { startFrac: frac, curFrac: frac };
    ev.preventDefault();
    _renderLine(_lastData, _clip(_lastData, _zoom));
  }
  function _onDragMove(ev) {
    if (!_dragState || !_lineEl) return;
    var rect = _lineEl.getBoundingClientRect();
    var frac = (ev.clientX - rect.left) / rect.width;
    if (frac < 0) frac = 0; else if (frac > 1) frac = 1;
    _dragState.curFrac = frac;
    if (!_dragRafId) {
      _dragRafId = requestAnimationFrame(function () {
        _dragRafId = null;
        if (_dragState && _lastData) _renderLine(_lastData, _clip(_lastData, _zoom));
      });
    }
  }
  function _onDragEnd(ev) {
    if (!_dragState) return;
    if (_dragRafId) { cancelAnimationFrame(_dragRafId); _dragRafId = null; }
    var start = _dragState.startFrac, end = _dragState.curFrac;
    _dragState = null;
    if (Math.abs(end - start) < 0.01) {
      if (_lastData) _renderLine(_lastData, _clip(_lastData, _zoom));
      return;
    }
    var curClip = _clip(_lastData, _zoom);
    if (!curClip) return;
    var span = curClip.hiMhz - curClip.loMhz;
    var a = Math.min(start, end), b = Math.max(start, end);
    var loMhz = curClip.loMhz + a * span;
    var hiMhz = curClip.loMhz + b * span;
    var minSpanMhz = (curClip.binStepKhz || 250) * 2 / 1000;
    if (hiMhz - loMhz < minSpanMhz) {
      if (_lastData) _renderLine(_lastData, curClip);
      return;
    }
    _setZoom([loMhz, hiMhz]);
  }
  function _onDoubleClickReset(ev) {
    if (!_zoom) return;
    ev.preventDefault();
    _setZoom(null);
  }
  function _onZoomChipClick() { _setZoom(null); }
  function _onPeakHoldToggle() {
    _peakHoldEnabled = !_peakHoldEnabled;
    _peakHoldDb = null;
    if (_peakHoldToggleEl) _peakHoldToggleEl.classList.toggle('active', _peakHoldEnabled);
    if (_lastData) {
      var clip = _clip(_lastData, _zoom);
      _renderLine(_lastData, clip);
    }
  }

  // -- Rendering -----------------------------------------------------------
  function _renderMeta(data) {
    if (!_spanEl) return;
    var startMhz = data.freq_start_hz / 1e6;
    var stopMhz = data.freq_stop_hz / 1e6;
    _spanEl.textContent = startMhz.toFixed(3) + ' – ' + stopMhz.toFixed(3) + ' MHz';
    var binCount = data.bins_hz ? data.bins_hz.length : 0;
    var binStep = binCount > 1 ? ((data.bins_hz[1] - data.bins_hz[0]) / 1000) : 0;
    _binsInfoEl.textContent = binCount + (binStep ? ' @ ' + binStep.toFixed(1) + ' kHz' : '');
    _gainEl.textContent = (data.gain_db == null) ? 'auto' : (data.gain_db.toFixed(1) + ' dB');
    // Sweeps counter + true time-since-last-sweep.  With wide spans
    // rtl_power can take 40+ s per sweep, so the global WS-tick freshness
    // indicator ("just now" every 2 s) is misleading here — surface the
    // real sweep age inline so the user can see the panel IS alive, just
    // slow.
    var sweepCount = data.sweep_count || 0;
    var ageTxt = '';
    if (data.last_sweep_at) {
      var ageSec = Date.now() / 1000 - data.last_sweep_at;
      if (ageSec >= 0) ageTxt = ' · ' + SC.formatAge(ageSec);
    }
    _sweepsEl.textContent = sweepCount + ageTxt;
    _statusEl.textContent = data.status + (data.error ? ' — ' + data.error : '');
    _statusEl.className = 'spectrum-status-' + data.status;
  }

  function _renderLine(data, clip) {
    if (!_lineEl || !data.latest_powers_db || !data.latest_powers_db.length) return;
    var allPowers = data.latest_powers_db;
    var allBins = data.bins_hz;

    // Slice to clip range when zoomed
    var powers, binsLine;
    if (clip && clip.zoomed) {
      powers = allPowers.slice(clip.loIdx, clip.hiIdx + 1);
      binsLine = allBins ? allBins.slice(clip.loIdx, clip.hiIdx + 1) : null;
    } else {
      powers = allPowers;
      binsLine = allBins;
    }
    var n = powers.length;

    // Auto-scale Y
    var mn = Infinity, mx = -Infinity;
    for (var i = 0; i < n; i++) {
      var v = powers[i];
      if (v == null || !isFinite(v)) continue;
      if (v < mn) mn = v;
      if (v > mx) mx = v;
    }
    if (!isFinite(mn) || !isFinite(mx)) return;
    var tgtMin = Math.floor(mn - 3);
    var tgtMax = Math.ceil(mx + 3);
    if (!_scaleInitialized) {
      _minDb = tgtMin;
      _maxDb = tgtMax;
      _scaleInitialized = true;
    } else {
      _minDb = _minDb * 0.7 + tgtMin * 0.3;
      _maxDb = _maxDb * 0.7 + tgtMax * 0.3;
    }
    if (_maxDb - _minDb < 10) _maxDb = _minDb + 10;

    var vbW = 800, vbH = 160;
    var pts = new Array(n);
    for (var j = 0; j < n; j++) {
      var p = powers[j];
      if (p == null || !isFinite(p)) { pts[j] = null; continue; }
      var x = (j * (vbW - 1)) / Math.max(1, n - 1);
      var y = vbH - 1 - ((p - _minDb) / (_maxDb - _minDb)) * (vbH - 2);
      if (y < 0) y = 0; if (y > vbH - 1) y = vbH - 1;
      pts[j] = x.toFixed(1) + ',' + y.toFixed(1);
    }

    SC.clear(_lineEl);

    // Grid lines
    var stepDb = 10;
    var firstTick = Math.ceil(_minDb / stepDb) * stepDb;
    for (var db = firstTick; db <= _maxDb; db += stepDb) {
      var gy = vbH - 1 - ((db - _minDb) / (_maxDb - _minDb)) * (vbH - 2);
      _lineEl.appendChild(SC.svg('line', {
        x1: 0, y1: gy, x2: vbW, y2: gy,
        stroke: _css.grid, 'stroke-width': 0.5,
      }));
      _lineEl.appendChild(SC.svg('text', {
        x: 4, y: gy - 2,
        fill: _css.label, 'font-size': 9,
      }, db.toFixed(0)));
    }

    // Main trace polyline
    var polyStr = pts.filter(function (p) { return p != null; }).join(' ');
    _lineEl.appendChild(SC.svg('polyline', {
      points: polyStr,
      fill: 'none',
      stroke: _css.cyan,
      'stroke-width': 1.2,
      'vector-effect': 'non-scaling-stroke',
    }));

    // Peak hold overlay
    if (_peakHoldEnabled) {
      if (!_peakHoldDb || _peakHoldDb.length !== n) {
        _peakHoldDb = new Float64Array(n);
        for (var pi = 0; pi < n; pi++) _peakHoldDb[pi] = -Infinity;
      }
      var peakPts = [];
      for (var pk = 0; pk < n; pk++) {
        var pv = powers[pk];
        if (pv != null && isFinite(pv) && pv > _peakHoldDb[pk]) _peakHoldDb[pk] = pv;
        var hv = _peakHoldDb[pk];
        if (!isFinite(hv)) continue;
        var px = (pk * (vbW - 1)) / Math.max(1, n - 1);
        var py = vbH - 1 - ((hv - _minDb) / (_maxDb - _minDb)) * (vbH - 2);
        if (py < 0) py = 0; if (py > vbH - 1) py = vbH - 1;
        peakPts.push(px.toFixed(1) + ',' + py.toFixed(1));
      }
      if (peakPts.length > 1) {
        _lineEl.appendChild(SC.svg('polyline', {
          points: peakPts.join(' '),
          fill: 'none',
          stroke: _css.peakHold,
          'stroke-width': 1,
          'stroke-dasharray': '4,3',
          'vector-effect': 'non-scaling-stroke',
        }));
      }
    }

    // Drag preview rectangle
    if (_dragState) {
      var dlo = Math.min(_dragState.startFrac, _dragState.curFrac);
      var dhi = Math.max(_dragState.startFrac, _dragState.curFrac);
      var dx = dlo * vbW, dw = (dhi - dlo) * vbW;
      if (dw > 1) {
        _lineEl.appendChild(SC.svg('rect', {
          x: dx, y: 0, width: dw, height: vbH,
          fill: 'rgba(0,229,255,0.08)',
          stroke: 'rgba(0,229,255,0.4)',
          'stroke-width': 1,
          'vector-effect': 'non-scaling-stroke',
        }));
      }
    }

    // Frequency tick labels
    var nb = binsLine ? binsLine.length : 0;
    var tickCount = 5;
    for (var t = 0; t <= tickCount; t++) {
      var frac = t / tickCount;
      var fx = frac * vbW;
      var fmhz;
      if (nb >= 2) {
        var binIdx = frac * (nb - 1);
        var lo = Math.floor(binIdx);
        var hi = Math.ceil(binIdx);
        if (lo === hi) {
          fmhz = binsLine[lo] / 1e6;
        } else {
          var w = binIdx - lo;
          fmhz = (binsLine[lo] * (1 - w) + binsLine[hi] * w) / 1e6;
        }
      } else {
        fmhz = data.freq_start_hz / 1e6
             + frac * ((data.freq_stop_hz - data.freq_start_hz) / 1e6);
      }
      _lineEl.appendChild(SC.svg('text', {
        x: fx, y: vbH - 2,
        fill: _css.label, 'font-size': 9,
        'text-anchor': t === 0 ? 'start' : (t === tickCount ? 'end' : 'middle'),
      }, fmhz.toFixed(1)));
    }
  }

  // -- Band ribbon + landmark overlay --------------------------------------
  // Layout responsibilities:
  //   - spectrum-bands:   contains <div.spectrum-band> rectangles for each
  //                       allocated band that overlaps the current span.
  //                       Percent left/width maps the frequency axis 1:1.
  //   - spectrum-overlay: contains <div.spectrum-landmark> dashed vertical
  //                       guides that span the full plot-wrap height so
  //                       the same freq is visible across band strip +
  //                       line plot + waterfall.
  // This is HTML/CSS rather than SVG so text labels don't get stretched
  // by the line-plot SVG's `preserveAspectRatio="none"` scaling.
  function _renderBands(data, clip) {
    if (!_bandsEl || !_overlayEl) return;
    var bins = data.bins_hz;
    if (!bins || bins.length < 2) return;
    var startMhz, stopMhz;
    if (clip && clip.zoomed) {
      startMhz = clip.loMhz;
      stopMhz  = clip.hiMhz;
    } else {
      startMhz = bins[0] / 1e6;
      stopMhz  = bins[bins.length - 1] / 1e6;
    }
    var spanMhz  = stopMhz - startMhz;
    if (spanMhz <= 0) return;

    // Skip rebuild if the axis hasn't changed — the band ribbon is purely
    // a function of (freq_start, freq_stop), not of the sweep data.
    var sig = startMhz.toFixed(3) + '|' + stopMhz.toFixed(3) + (_zoom ? '|z' : '');
    if (sig === _lastBandSignature) return;
    _lastBandSignature = sig;

    SC.clear(_bandsEl);
    SC.clear(_overlayEl);

    // Pass 1 — bands.
    for (var i = 0; i < SC.BANDS.length; i++) {
      var b = SC.BANDS[i];
      if (b.to < startMhz || b.from > stopMhz) continue;
      var visFrom = Math.max(b.from, startMhz);
      var visTo   = Math.min(b.to,   stopMhz);
      var leftPct  = ((visFrom - startMhz) / spanMhz) * 100;
      var widthPct = ((visTo   - visFrom)   / spanMhz) * 100;
      if (widthPct < 0.04) continue;  // < 4/10000 of viewport; not worth a div

      var cat = SC.CATEGORIES[b.cat] || SC.CATEGORIES.gap;
      var div = document.createElement('div');
      div.className = 'spectrum-band';
      div.style.left = leftPct.toFixed(3) + '%';
      div.style.width = widthPct.toFixed(3) + '%';
      div.style.background = SC.hexToRgba(cat.color, 0.3);
      div.style.borderLeftColor = cat.color;
      div.style.borderRightColor = cat.color;
      // Native browser tooltip.  Richer info available via hover crosshair
      // which uses the same lookup.
      div.title = b.name + '  (' + b.from + '–' + b.to + ' MHz)\n'
                + cat.name + ' — ' + b.desc;
      var span = document.createElement('span');
      span.className = 'spectrum-band-label';
      span.textContent = b.name;
      div.appendChild(span);
      _bandsEl.appendChild(div);
    }

    // Pass 2 — landmarks (narrow single-frequency markers).
    for (var j = 0; j < SC.LANDMARKS.length; j++) {
      var lm = SC.LANDMARKS[j];
      if (lm.mhz < startMhz || lm.mhz > stopMhz) continue;
      var lx = ((lm.mhz - startMhz) / spanMhz) * 100;
      var lmEl = document.createElement('div');
      lmEl.className = 'spectrum-landmark';
      lmEl.style.left = lx.toFixed(3) + '%';
      lmEl.title = lm.name + ' @ ' + lm.mhz + ' MHz — ' + lm.desc;
      var lbl = document.createElement('span');
      lbl.className = 'spectrum-landmark-label';
      lbl.textContent = lm.name;
      lmEl.appendChild(lbl);
      _overlayEl.appendChild(lmEl);
    }
  }

  // Legend is built once from the BANDS table so adding a row in the data
  // automatically updates the UI — no separate list to keep in sync.
  function _buildLegend() {
    if (_legendBuilt || !_legendEl) return;
    _legendBuilt = true;

    // Group band names under their category.
    var byCat = {};
    for (var i = 0; i < SC.BANDS.length; i++) {
      var cat = SC.BANDS[i].cat;
      if (!byCat[cat]) byCat[cat] = [];
      byCat[cat].push(SC.BANDS[i].name);
    }

    // Preserve CATEGORIES insertion order for stable visual layout.
    var keys = Object.keys(SC.CATEGORIES);
    for (var k = 0; k < keys.length; k++) {
      var key = keys[k];
      var cat = SC.CATEGORIES[key];
      var bands = byCat[key];
      if (!bands || !bands.length) continue;  // category unused in BANDS

      var row = document.createElement('div');
      row.className = 'spectrum-legend-row';

      var swatch = document.createElement('span');
      swatch.className = 'spectrum-legend-swatch';
      swatch.style.background = SC.hexToRgba(cat.color, 0.75);
      row.appendChild(swatch);

      var body = document.createElement('div');
      body.className = 'spectrum-legend-body';

      var catName = document.createElement('div');
      catName.className = 'spectrum-legend-cat';
      catName.textContent = cat.name;
      body.appendChild(catName);

      var bandList = document.createElement('div');
      bandList.className = 'spectrum-legend-bands';
      bandList.textContent = bands.join(' · ');
      body.appendChild(bandList);

      row.appendChild(body);
      _legendEl.appendChild(row);
    }
  }

  function _paintRowToCanvas(powers, clip) {
    if (!_wfCtx || !powers || !powers.length) return;
    var row = (clip && clip.zoomed) ? powers.slice(clip.loIdx, clip.hiIdx + 1) : powers;
    var n = row.length;
    if (!n) return;
    _wfCtx.drawImage(
      _wfCanvas,
      0, 0, WF_COLS, WF_ROWS - 1,
      0, 1, WF_COLS, WF_ROWS - 1
    );
    var img = _wfCtx.createImageData(WF_COLS, 1);
    var data = img.data;
    var lo = _minDb, hi = _maxDb;
    var range = hi - lo;
    if (range < 1) range = 1;
    for (var x = 0; x < WF_COLS; x++) {
      var srcF = (n > 1) ? (x * (n - 1)) / (WF_COLS - 1) : 0;
      var srcLo = Math.floor(srcF);
      var srcHi = Math.min(srcLo + 1, n - 1);
      var frac = srcF - srcLo;
      var pLo = row[srcLo], pHi = row[srcHi];
      var p;
      if (pLo == null || !isFinite(pLo)) p = pHi;
      else if (pHi == null || !isFinite(pHi)) p = pLo;
      else p = pLo + (pHi - pLo) * frac;
      var norm;
      if (p == null || !isFinite(p)) {
        norm = 0;
      } else {
        norm = (p - lo) / range;
        if (norm < 0) norm = 0; else if (norm > 1) norm = 1;
      }
      var rgb = SC.colorForNorm(norm);
      var off = x * 4;
      data[off] = rgb[0];
      data[off + 1] = rgb[1];
      data[off + 2] = rgb[2];
      data[off + 3] = 255;
    }
    _wfCtx.putImageData(img, 0, 0);
  }

  // Bulk-paint the shared historyStore ring onto the waterfall canvas.
  // Called whenever the store's generation cursor changes (WS hello, or
  // bin-grid reset).  Uses the shared helper so we do a single
  // putImageData instead of N scrolls.
  function _bulkPaintFromStore(clip) {
    if (!_wfCtx || !SC.historyStore) return;
    var rows = SC.historyStore.rows;
    if (!rows.length) return;
    var chron = new Array(rows.length);
    for (var i = 0; i < rows.length; i++) {
      chron[rows.length - 1 - i] = rows[i];
    }
    if (clip && clip.zoomed) {
      for (var k = 0; k < chron.length; k++) {
        if (chron[k]) chron[k] = chron[k].slice(clip.loIdx, clip.hiIdx + 1);
      }
    }
    SC.paintHistoryToCanvas(_wfCtx, _wfCanvas, chron, WF_COLS, WF_ROWS,
                            _minDb, _maxDb);
    _lastSweepCount = SC.historyStore.sweepCount;
  }

  function _ingestNewSweeps(data, clip) {
    var sc = data.sweep_count || 0;
    var delta = sc - _lastSweepCount;
    if (delta <= 0) return;
    var tail = data.waterfall_tail || [];
    if (!tail.length) { _lastSweepCount = sc; return; }
    var toDraw = Math.min(delta, tail.length);
    for (var i = tail.length - toDraw; i < tail.length; i++) {
      _paintRowToCanvas(tail[i], clip);
    }
    _lastSweepCount = sc;
  }

  // -- Hover crosshair -----------------------------------------------------
  function _onHover(ev) {
    if (!_lastData || !_lastData.bins_hz || !_lastData.bins_hz.length) return;
    var rect = _wfCanvas.getBoundingClientRect();
    var fracX = (ev.clientX - rect.left) / rect.width;
    var fracY = (ev.clientY - rect.top) / rect.height;
    if (fracX < 0 || fracX > 1 || fracY < 0 || fracY > 1) return;
    var clip = _clip(_lastData, _zoom);
    var bins = _lastData.bins_hz;
    var loIdx = (clip && clip.zoomed) ? clip.loIdx : 0;
    var hiIdx = (clip && clip.zoomed) ? clip.hiIdx : bins.length - 1;
    var n = hiIdx - loIdx + 1;
    var idx = loIdx + Math.min(n - 1, Math.max(0, Math.round(fracX * (n - 1))));
    var freqMhz = bins[idx] / 1e6;
    // Row age: the topmost pixel row (y=0) is the most recent sweep.
    // Prefer the server-supplied per-row timestamp over the old
    // rowIdx * sweep_seconds approximation — the latter drifts badly on
    // wide spans where each sweep takes longer than sweep_seconds, and
    // lies outright for rows from before a scanner restart.
    var rowIdx = Math.floor(fracY * WF_ROWS);
    var rowTs = SC.historyStore.rowTimestamps[rowIdx];
    var agoSec = (rowTs != null) ? (Date.now() / 1000 - rowTs) : null;
    // dB value: prefer the latest sweep's reading at that bin.
    var db = _lastData.latest_powers_db ? _lastData.latest_powers_db[idx] : null;
    var dbStr = (db != null && isFinite(db)) ? db.toFixed(1) + ' dB' : '—';
    // '—' when we hover over a blank pixel below the filled region (no row
    // stored), or when the backend didn't ship timestamps.  Honest silence
    // beats the old fake-age behaviour.
    var ageStr;
    if (agoSec == null) ageStr = '—';
    else if (rowIdx === 0 && agoSec < 2) ageStr = 'now';
    else ageStr = SC.formatAge(agoSec);

    // Resolve what the user is pointing at — a band, a nearby landmark,
    // or neither.  Landmark tolerance scales with the current span so it
    // feels "clicky" at wide views without being overeager when zoomed.
    var headerLine = freqMhz.toFixed(3) + ' MHz · ' + dbStr + ' · ' + ageStr;
    var bandLine = '';
    var visSpanMhz = (clip && clip.zoomed)
      ? (clip.hiMhz - clip.loMhz)
      : (_lastData.freq_stop_hz - _lastData.freq_start_hz) / 1e6;
    var lmTol = Math.max(0.5, visSpanMhz * 0.004);
    var lm = SC.findNearLandmark(freqMhz, lmTol);
    var band = SC.findBand(freqMhz);
    if (lm) {
      bandLine = '<strong>' + esc(lm.name) + '</strong> @ ' + lm.mhz.toFixed(3) + ' MHz — ' + esc(lm.desc);
    } else if (band) {
      var cat = SC.CATEGORIES[band.cat] || SC.CATEGORIES.gap;
      bandLine = '<strong>' + esc(band.name) + '</strong> · ' + esc(cat.name) + ' — ' + esc(band.desc);
    }
    // Build innerHTML only when we have structured content; otherwise stay
    // on textContent so an accidental markup character in freq/dB (which
    // shouldn't happen, but defence in depth) can't inject anything.
    if (bandLine) {
      _hoverEl.innerHTML = esc(headerLine) + '<div class="spectrum-hover-band">' + bandLine + '</div>';
    } else {
      _hoverEl.textContent = headerLine;
    }
    _hoverEl.style.display = 'block';
  }
  function _onHoverLeave() {
    if (_hoverEl) _hoverEl.style.display = 'none';
  }

  // -- Scale strip ----------------------------------------------------------
  var _lastScaleSweep = 0;
  var _lastScaleZoomed = false;
  function _renderScale(data, clip) {
    if (!_scaleEl) return;
    var sc = data.sweep_count || 0;
    var isZoomed = !!(clip && clip.zoomed);
    if (sc === _lastScaleSweep && isZoomed === _lastScaleZoomed) return;
    _lastScaleSweep = sc;
    _lastScaleZoomed = isZoomed;
    var parts = [];
    parts.push(_minDb.toFixed(0) + ' … ' + _maxDb.toFixed(0) + ' dB');
    var bins = data.bins_hz;
    if (bins && bins.length > 1) {
      var spanMhz;
      if (clip && clip.zoomed) {
        spanMhz = clip.hiMhz - clip.loMhz;
        parts.push(spanMhz.toFixed(1) + ' MHz span (zoomed)');
      } else {
        spanMhz = (bins[bins.length - 1] - bins[0]) / 1e6;
        parts.push(spanMhz.toFixed(1) + ' MHz span');
      }
      var binStep = (bins[1] - bins[0]) / 1000;
      var binCount = clip && clip.zoomed ? (clip.hiIdx - clip.loIdx + 1) : bins.length;
      parts.push(binCount + ' bins @ ' + binStep.toFixed(1) + ' kHz');
    }
    if (data.sweep_seconds) {
      parts.push('~' + data.sweep_seconds.toFixed(1) + 's/sweep');
    }
    _scaleEl.textContent = parts.join(' · ');
  }

  // -- Public entry point --------------------------------------------------
  function update(data) {
    if (!data) return;
    if (!_resolveDom()) return;

    // Handle unavailable / error states with placeholder
    if (data.status === 'unavailable') {
      if (_placeholderEl) {
        _placeholderEl.textContent = 'RTL-SDR scanner unavailable';
        _placeholderEl.style.display = '';
      }
      return;
    }
    if (data.status === 'error') {
      if (_placeholderEl) {
        _placeholderEl.textContent = 'Scanner error' + (data.error ? ' — ' + data.error : '');
        _placeholderEl.style.display = '';
      }
      return;
    }

    // Hide placeholder + expand body on first real data
    if (!_hasReceivedData && data.latest_powers_db && data.latest_powers_db.length) {
      _hasReceivedData = true;
      if (_placeholderEl) _placeholderEl.style.display = 'none';
      if (_body && _body.classList.contains('hidden')) {
        _body.classList.remove('hidden');
        _expanded = true;
        var chev = _toggle ? _toggle.querySelector('.chevron') : null;
        if (chev) chev.innerHTML = '&#9662;';
      }
    }

    // Shared historyStore generation cursor — bumps on WS-hello backfill
    // arrival and on bin-grid change.  Either event makes the current canvas
    // stale relative to the store, so wipe and re-arm a bulk paint that runs
    // after the next _renderLine (which seeds _minDb/_maxDb).
    var gen = SC.historyStore.generation;
    if (gen !== _lastStoreGen) {
      _lastStoreGen = gen;
      _lastSweepCount = 0;
      _lastRenderedSweep = 0;
      _scaleInitialized = false;
      _lastBandSignature = '';
      _needsBulkPaint = true;
      _setZoom(null, true);
    }

    if (data.bins_hz) {
      _cachedBinsHz = data.bins_hz;
    } else if (_cachedBinsHz) {
      data.bins_hz = _cachedBinsHz;
    } else if (SC.historyStore && SC.historyStore.binsHz) {
      _cachedBinsHz = SC.historyStore.binsHz;
      data.bins_hz = _cachedBinsHz;
    }
    _lastData = data;
    if (_body && _body.classList.contains('hidden')) return;
    var clip = _clip(data, _zoom);

    _renderPresets(data);
    _renderSwitchingOverlay(data);
    _renderMeta(data);
    _renderBands(data, clip);
    _buildLegend();

    var sc = data.sweep_count || 0;
    if (sc > _lastRenderedSweep) {
      _renderLine(data, clip);
      _lastRenderedSweep = sc;
      if (_needsBulkPaint) {
        _bulkPaintFromStore(clip);
        _needsBulkPaint = false;
      } else {
        _ingestNewSweeps(data, clip);
      }
    }

    _renderScale(data, clip);
    _renderChannelAnalysis(data.channel_analysis || null);

    if (_countEl) _countEl.textContent = data.sweep_count + ' sweeps';
    if (R.markUpdated) R.markUpdated('spectrum-section');
  }

  // -- Preset switching UI ---------------------------------------------------
  var _presetBar = null;
  var _presetBtns = {};
  var _switchingOverlay = null;
  var _switchingTimeout = null;
  var _channelGridEl = null;
  var _lastActivePreset = null;

  function _ensurePresetBar() {
    if (_presetBar) return;
    var meta = _section ? _section.querySelector('.spectrum-meta, .section-meta') : null;
    if (!meta) return;
    _presetBar = document.createElement('div');
    _presetBar.className = 'spectrum-preset-bar';
    meta.parentNode.insertBefore(_presetBar, meta.nextSibling);
  }

  function _renderPresets(data) {
    if (!data.available_presets) return;
    _ensurePresetBar();
    if (!_presetBar) return;
    var presets = data.available_presets;
    var active = data.active_preset;
    if (active === _lastActivePreset && Object.keys(_presetBtns).length === presets.length) return;
    _lastActivePreset = active;
    _presetBar.innerHTML = '';
    _presetBtns = {};
    for (var i = 0; i < presets.length; i++) {
      var p = presets[i];
      var btn = document.createElement('button');
      btn.className = p.name === active ? 'active' : '';
      btn.textContent = p.name.replace(/_/g, ' ');
      btn.title = (p.freq_start_mhz || '?') + ' – ' + (p.freq_stop_mhz || '?') + ' MHz';
      btn.dataset.preset = p.name;
      btn.addEventListener('click', _onPresetClick);
      _presetBar.appendChild(btn);
      _presetBtns[p.name] = btn;
    }
  }

  function _onPresetClick(e) {
    var name = e.target.dataset.preset;
    if (!name) return;
    // Disable buttons during switch
    Object.keys(_presetBtns).forEach(function (k) { _presetBtns[k].disabled = true; });
    if (R.ws && R.ws.readyState === WebSocket.OPEN) {
      R.ws.send(JSON.stringify({ action: 'spectrum_switch_preset', preset: name }));
    }
  }

  function _renderSwitchingOverlay(data) {
    if (data.switching || data.status === 'switching') {
      if (!_switchingOverlay && _body) {
        _switchingOverlay = document.createElement('div');
        _switchingOverlay.className = 'spectrum-switching-overlay';
        _switchingOverlay.style.cssText =
          'position:absolute;top:0;right:0;bottom:0;left:0;display:flex;align-items:center;justify-content:center;' +
          'background:rgba(0,0,0,0.6);color:#fff;font-size:1.1em;z-index:10;border-radius:6px;';
        _switchingOverlay.textContent = 'Switching preset…';
        _body.style.position = 'relative';
        _body.appendChild(_switchingOverlay);
        _switchingTimeout = setTimeout(function () {
          handlePresetError('Preset switch timed out');
        }, 15000);
      }
    } else if (_switchingOverlay) {
      if (_switchingTimeout) { clearTimeout(_switchingTimeout); _switchingTimeout = null; }
      _switchingOverlay.remove();
      _switchingOverlay = null;
      Object.keys(_presetBtns).forEach(function (k) { _presetBtns[k].disabled = false; });
    }
  }

  // -- LoRa channel grid (rendered when preset has channel_analysis) --------
  function _ensureChannelGrid() {
    if (_channelGridEl) return;
    if (!_body) return;
    _channelGridEl = document.createElement('div');
    _channelGridEl.className = 'lora-channel-grid-unified';
    _channelGridEl.style.cssText = 'margin-top:8px;display:none;';
    _body.appendChild(_channelGridEl);
  }

  function _renderChannelAnalysis(analysis) {
    _ensureChannelGrid();
    if (!_channelGridEl) return;
    if (!analysis || !analysis.channels) {
      _channelGridEl.style.display = 'none';
      return;
    }
    _channelGridEl.style.display = '';
    var chs = analysis.channels;
    var nf = analysis.noise_floor_db;

    // Summary bar
    var summary = '<div class="ch-summary" style="font-size:0.85em;margin-bottom:6px;">' +
      '<span>Noise floor: <b>' + (nf != null ? esc(nf.toFixed(1)) + ' dB' : '—') + '</b></span>' +
      ' · <span>Active: <b>' + esc(analysis.active_count + '/' + chs.length) + '</b></span>' +
      ' · <span>Threshold: ' + esc(analysis.threshold_db) + ' dB</span>';
    if (analysis.interference_flags && analysis.interference_flags.length) {
      summary += ' · <span style="color:#f44;">⚠ ' + esc(analysis.interference_flags.length) + ' interference</span>';
    }
    summary += '</div>';

    // Channel cells — uplink + downlink
    var ups = chs.filter(function (c) { return c.dir === 'up'; });
    var dns = chs.filter(function (c) { return c.dir === 'dn'; });

    var html = summary + '<div style="display:flex;gap:12px;flex-wrap:wrap;">';
    html += _buildChannelBlock('Uplink (' + ups.length + ')', ups, nf);
    html += _buildChannelBlock('Downlink (' + dns.length + ')', dns, nf);
    html += '</div>';
    _channelGridEl.innerHTML = html;
  }

  function _buildChannelBlock(title, channels, nf) {
    var html = '<div><div style="font-size:0.8em;color:#aaa;margin-bottom:2px;">' + esc(title) + '</div>';
    html += '<div style="display:flex;flex-wrap:wrap;gap:2px;">';
    for (var i = 0; i < channels.length; i++) {
      var c = channels[i];
      var color = '#333';
      if (c.active) color = '#2a6';
      else if (c.duty_pct > 5) color = '#a62';
      html += '<div title="Ch ' + esc(c.idx) + ' ' + esc(c.center_mhz) + ' MHz\n' +
        'Power: ' + (c.power_db != null ? esc(c.power_db) + ' dB' : '—') + '\n' +
        'Duty: ' + esc(c.duty_pct) + '%\nDetections: ' + esc(c.det_count) + '"' +
        ' style="width:8px;height:12px;background:' + color + ';border-radius:1px;"></div>';
    }
    html += '</div></div>';
    return html;
  }

  function handlePresetError(errorMsg) {
    if (_switchingTimeout) { clearTimeout(_switchingTimeout); _switchingTimeout = null; }
    Object.keys(_presetBtns).forEach(function (k) { _presetBtns[k].disabled = false; });
    if (_switchingOverlay && _switchingOverlay.parentNode) {
      _switchingOverlay.parentNode.removeChild(_switchingOverlay);
      _switchingOverlay = null;
    }
    if (_statusEl) _statusEl.textContent = 'Preset error: ' + errorMsg;
  }

  function handlePresetSwitched(data) {
    if (_switchingTimeout) { clearTimeout(_switchingTimeout); _switchingTimeout = null; }
    if (_switchingOverlay && _switchingOverlay.parentNode) {
      _switchingOverlay.parentNode.removeChild(_switchingOverlay);
      _switchingOverlay = null;
    }
    Object.keys(_presetBtns).forEach(function (k) {
      _presetBtns[k].disabled = false;
      _presetBtns[k].className = (k === data.preset) ? 'active' : '';
    });
  }

  R.spectrum = { update: update, handlePresetError: handlePresetError, handlePresetSwitched: handlePresetSwitched };
})();
