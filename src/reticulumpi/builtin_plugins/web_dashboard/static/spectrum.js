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
  var _binCount = 0;         // current snapshot's bin count
  var _minDb = -90, _maxDb = -30;  // running auto-scale for colour + line plot
  var _scaleInitialized = false;   // true once a real sweep has seeded _min/_maxDb
  var _lastBandSignature = '';     // "<start>|<stop>" of last band-strip render
  var _legendBuilt = false;        // one-time flag for legend DOM construction
  var _legendExpanded = false;
  // History backfill state machine. The WS broadcast only carries the last
  // few sweeps to keep payloads small; on first sweep arrival we fire a
  // one-shot REST fetch for the plugin's full rolling buffer (capped by
  // its `waterfall_rows` config) so the panel opens populated rather than
  // accumulating ~16 s of pre-load history from the WS tail.
  // States: 'pending' → 'fetching' → 'ready' | 'failed' | 'abandoned'.
  // Live WS sweeps are deferred while 'fetching' to avoid out-of-order
  // paints; if the fetch outlasts ABANDON_MS we give up waiting and start
  // painting live so no sweeps fall through the 8-row tail window.
  var _historyState = 'pending';
  var _fetchStartedAt = 0;
  var _FETCH_ABANDON_MS = 4000;

  // Waterfall canvas native dims — scaled up via CSS to fit panel width.
  var WF_ROWS = 256;
  var WF_COLS = 800;

  // -- DOM setup -----------------------------------------------------------
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
      _wfCanvas.width = WF_COLS;
      _wfCanvas.height = WF_ROWS;
      _wfCtx = _wfCanvas.getContext('2d');
      // Paint a dim background so the section doesn't flash white pre-data.
      _wfCtx.fillStyle = '#0a0d17';
      _wfCtx.fillRect(0, 0, WF_COLS, WF_ROWS);
      _wfCanvas.addEventListener('mousemove', _onHover);
      _wfCanvas.addEventListener('mouseleave', _onHoverLeave);
    }

    if (_toggle) _toggle.addEventListener('click', _onToggleClick);
    if (_legendToggleEl) _legendToggleEl.addEventListener('click', _onLegendToggleClick);
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
      // Repaint in case the user just opened the panel for the first time.
      _renderMeta(_lastData);
      _lastBandSignature = '';  // force ribbon rebuild on expand
      _renderBands(_lastData);
      _buildLegend();
      _renderLine(_lastData);
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

  function _renderLine(data) {
    if (!_lineEl || !data.latest_powers_db || !data.latest_powers_db.length) return;
    var powers = data.latest_powers_db;
    var n = powers.length;

    // Auto-scale Y (noise-floor + 20 dB headroom).
    var mn = Infinity, mx = -Infinity;
    for (var i = 0; i < n; i++) {
      var v = powers[i];
      if (v == null || !isFinite(v)) continue;
      if (v < mn) mn = v;
      if (v > mx) mx = v;
    }
    if (!isFinite(mn) || !isFinite(mx)) return;
    // Compute a slightly padded target window.  On the very first render
    // we lock the window to the measurement — the hard-coded default
    // (-90..-30) is nowhere near the real noise floor of a 2 GHz span, so
    // smoothing toward it would cause a very visible "sliding" of the
    // line plot over several ticks.  Subsequent renders (triggered only
    // by a *new* sweep, see update()) use an EMA so sweep-to-sweep
    // fluctuations don't bounce the axis around.
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

    // Grid lines (every ~20 dB over the auto-scale window).
    var stepDb = 10;
    var firstTick = Math.ceil(_minDb / stepDb) * stepDb;
    for (var db = firstTick; db <= _maxDb; db += stepDb) {
      var gy = vbH - 1 - ((db - _minDb) / (_maxDb - _minDb)) * (vbH - 2);
      _lineEl.appendChild(SC.svg('line', {
        x1: 0, y1: gy, x2: vbW, y2: gy,
        stroke: '#1a2233', 'stroke-width': 0.5,
      }));
      _lineEl.appendChild(SC.svg('text', {
        x: 4, y: gy - 2,
        fill: '#4a5570', 'font-size': 9,
      }, db.toFixed(0)));
    }

    var polyStr = pts.filter(function (p) { return p != null; }).join(' ');
    _lineEl.appendChild(SC.svg('polyline', {
      points: polyStr,
      fill: 'none',
      stroke: '#58a6ff',
      'stroke-width': 1.2,
      'vector-effect': 'non-scaling-stroke',
    }));

    // Frequency tick labels along the bottom (a few evenly spaced).
    // Derive frequencies from the actual bin grid so labels line up with
    // the line/waterfall data — see _renderBands for why the configured
    // freq_start_hz/freq_stop_hz can't be trusted as the visual axis.
    // Interpolating by bin index also handles non-uniform spacing in
    // multi-segment rtl_power sweeps.
    var binsLine = data.bins_hz;
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
        fill: '#4a5570', 'font-size': 9,
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
  function _renderBands(data) {
    if (!_bandsEl || !_overlayEl) return;
    var bins = data.bins_hz;
    if (!bins || bins.length < 2) return;
    // Use the actual bin grid, not the configured span. rtl_power rounds the
    // requested span to the FFT bin grid (e.g. 88–108 MHz @ 25 kHz lands the
    // last bin at 107.975 MHz), and multi-segment sweeps can shift the
    // endpoints further. The waterfall + line plot are drawn from bins_hz,
    // so band positions must use the same axis or labels drift relative to
    // the data — the drift is ~0 at the low end and grows with frequency.
    var startMhz = bins[0] / 1e6;
    var stopMhz  = bins[bins.length - 1] / 1e6;
    var spanMhz  = stopMhz - startMhz;
    if (spanMhz <= 0) return;

    // Skip rebuild if the axis hasn't changed — the band ribbon is purely
    // a function of (freq_start, freq_stop), not of the sweep data.
    var sig = startMhz.toFixed(3) + '|' + stopMhz.toFixed(3);
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

  function _paintRowToCanvas(powers) {
    if (!_wfCtx || !powers || !powers.length) return;
    // Scroll existing waterfall down by 1 px, then paint new row on top.
    _wfCtx.drawImage(
      _wfCanvas,
      0, 0, WF_COLS, WF_ROWS - 1,
      0, 1, WF_COLS, WF_ROWS - 1
    );
    // Build a 1-row ImageData at WF_COLS wide; sample powers array.
    var img = _wfCtx.createImageData(WF_COLS, 1);
    var data = img.data;
    var n = powers.length;
    var lo = _minDb, hi = _maxDb;
    var range = hi - lo;
    if (range < 1) range = 1;
    for (var x = 0; x < WF_COLS; x++) {
      // Nearest-neighbour from power array; good enough visually and much
      // faster than linear interpolation here.
      var srcIdx = (n > 1) ? Math.floor((x * (n - 1)) / (WF_COLS - 1)) : 0;
      if (srcIdx < 0) srcIdx = 0; else if (srcIdx >= n) srcIdx = n - 1;
      var p = powers[srcIdx];
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

  // One-shot fetch of the plugin's full rolling waterfall buffer. Called
  // once per page load (or per bin-grid change) — see _historyState.
  // Painted rows go through _paintRowToCanvas oldest-first, so the oldest
  // history ends up deepest in the waterfall, matching live behaviour.
  function _fetchHistory() {
    if (_historyState !== 'pending') return;
    _historyState = 'fetching';
    _fetchStartedAt = Date.now();
    fetch('/api/spectrum/history', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (payload) {
        // If the main loop gave up waiting on us and started painting
        // live rows, applying history now would scroll those live rows
        // off and invert temporal order — skip the backfill silently.
        if (_historyState === 'abandoned') return;
        var d = (payload && payload.data) ? payload.data : null;
        if (!d || !d.available || !d.rows || !d.rows.length) {
          _historyState = 'failed';
          return;
        }
        // Defensive: if the bin grid changed during or after the fetch
        // (config reload, scanner restart), the historical rows won't
        // line up with the CURRENT axis — drop them and let the WS tail
        // fill in normally.  Compare against live `_binCount`, not a
        // value captured at fetch-start, so we stay correct even across
        // multiple re-arms.
        if (d.bin_count && _binCount && d.bin_count !== _binCount) {
          _historyState = 'failed';
          return;
        }
        for (var i = 0; i < d.rows.length; i++) {
          _paintRowToCanvas(d.rows[i]);
        }
        // Sync paint cursor to whatever sweep count history saw, so the
        // next WS update only paints rows newer than the backfill (and
        // doesn't double-paint anything we already drew).
        _lastSweepCount = d.sweep_count || 0;
        _historyState = 'ready';
      })
      .catch(function () {
        if (_historyState !== 'abandoned') _historyState = 'failed';
      });
  }

  function _ingestNewSweeps(data) {
    // Figure out how many sweeps we haven't seen yet; paint those from the
    // snapshot's waterfall_tail.  If we've missed more than tail.length
    // sweeps (e.g., client was paused), we just catch up to whatever's in
    // the tail — older missed sweeps are gone.
    var sc = data.sweep_count || 0;
    var delta = sc - _lastSweepCount;
    if (delta <= 0) return;
    var tail = data.waterfall_tail || [];
    if (!tail.length) { _lastSweepCount = sc; return; }
    var toDraw = Math.min(delta, tail.length);
    for (var i = tail.length - toDraw; i < tail.length; i++) {
      _paintRowToCanvas(tail[i]);
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
    var bins = _lastData.bins_hz;
    var n = bins.length;
    var idx = Math.min(n - 1, Math.max(0, Math.round(fracX * (n - 1))));
    var freqMhz = bins[idx] / 1e6;
    // Row age: the topmost pixel row (y=0) is the most recent sweep.
    var rowIdx = Math.floor(fracY * WF_ROWS);
    var agoSec = rowIdx * (_lastData.sweep_seconds || 2);
    // dB value: prefer the latest sweep's reading at that bin.
    var db = _lastData.latest_powers_db ? _lastData.latest_powers_db[idx] : null;
    var dbStr = (db != null && isFinite(db)) ? db.toFixed(1) + ' dB' : '—';
    var ageStr = rowIdx === 0 ? 'now' : (agoSec + 's ago');

    // Resolve what the user is pointing at — a band, a nearby landmark,
    // or neither.  Landmark tolerance scales with the current span so it
    // feels "clicky" at wide views without being overeager when zoomed.
    var headerLine = freqMhz.toFixed(3) + ' MHz · ' + dbStr + ' · ' + ageStr;
    var bandLine = '';
    var spanMhz = (_lastData.freq_stop_hz - _lastData.freq_start_hz) / 1e6;
    var lmTol = Math.max(0.5, spanMhz * 0.004);  // ~0.4% of span, min 0.5 MHz
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

  // -- Public entry point --------------------------------------------------
  function update(data) {
    if (!data) return;
    if (!_resolveDom()) return;

    // Reveal section + expand the first time any data arrives.
    if (_section.style.display === 'none') {
      _section.style.display = '';
      if (_body && _body.classList.contains('hidden')) {
        _body.classList.remove('hidden');
        _expanded = true;
        var chev = _toggle.querySelector('.chevron');
        if (chev) chev.innerHTML = '&#9662;';
      }
    }

    // Bin count changed?  That means the config was reconfigured —
    // wipe our waterfall history so the new frequency axis lines up.
    var newBinCount = data.bins_hz ? data.bins_hz.length : 0;
    if (newBinCount !== _binCount) {
      _binCount = newBinCount;
      _lastSweepCount = 0;
      _lastRenderedSweep = 0;
      _scaleInitialized = false;
      _lastBandSignature = '';  // force band ribbon rebuild at new axis
      // Re-arm history backfill: previous rows used the old bin grid and
      // would render at the wrong x positions on the new axis.
      _historyState = 'pending';
      if (_wfCtx) {
        _wfCtx.fillStyle = '#0a0d17';
        _wfCtx.fillRect(0, 0, WF_COLS, WF_ROWS);
      }
    }

    _lastData = data;

    // Meta (span, bin count, gain, sweep age) refreshes every tick so
    // "Xs ago" ticks up even on WS broadcasts that carry no new sweep.
    _renderMeta(data);

    // Band ribbon + legend only depend on the configured span, not on the
    // sweep data.  _renderBands no-ops if the axis hasn't changed, so
    // calling it every tick is cheap; _buildLegend is one-time.
    _renderBands(data);
    _buildLegend();

    // The WS broadcast runs ~every 2 s, but a wide-span rtl_power sweep
    // can take 40+ s.  If the sweep_count hasn't advanced, the payload is
    // byte-for-byte the same one we just rendered — re-running the line
    // plot every tick just makes the waveform appear to drift as the
    // auto-scale EMA converges on a fixed target.  Only redraw when
    // something has actually changed.
    var sc = data.sweep_count || 0;
    if (sc > _lastRenderedSweep) {
      // Always refresh the line plot — it's cheap and shows the latest
      // sweep regardless of waterfall backfill state.  Auto-scale runs
      // here, so _minDb/_maxDb are valid by the time history rows paint.
      _renderLine(data);
      _lastRenderedSweep = sc;
      // Kick the one-shot history fetch on first real sweep, then defer
      // live waterfall paints until it settles to keep row order clean.
      if (_historyState === 'pending') {
        _fetchHistory();
      }
      // If the fetch is taking too long, give up waiting and start
      // painting live — otherwise sweeps older than the 8-row tail get
      // lost while we hold out for the backfill.
      if (_historyState === 'fetching'
          && Date.now() - _fetchStartedAt > _FETCH_ABANDON_MS) {
        _historyState = 'abandoned';
      }
      if (_historyState !== 'pending' && _historyState !== 'fetching') {
        _ingestNewSweeps(data);
      }
    }

    if (_countEl) _countEl.textContent = data.sweep_count + ' sweeps';
    if (R.markUpdated) R.markUpdated('spectrum-section');
  }

  R.spectrum = { update: update };
})();
