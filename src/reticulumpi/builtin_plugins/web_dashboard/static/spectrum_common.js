/* ReticulumPi Dashboard — Spectrum common primitives
 *
 * Shared helpers used by BOTH the generic SDR spectrum panel
 * (`spectrum.js`) and the dedicated LoRa-band panel (`lora_spectrum.js`).
 *
 * Rule of thumb: if a helper is colour / geometry / reference-data that
 * both panels would need to keep exactly in sync, it lives here.  Panel
 * plumbing (DOM wiring, update loop, section-specific event handlers)
 * stays inside each panel's own IIFE.
 *
 * Exposes `window.RPI.spectrumCommon` with:
 *   - RAMP, colorForNorm(t)       turbo colormap
 *   - hexToRgba(hex, a)           colour helper for band fills
 *   - svg(tag, attrs, text), clear(node)  small SVG / DOM helpers
 *   - formatMhz(hz), formatAge(s) display helpers
 *   - BANDS, LANDMARKS, CATEGORIES  band reference tables
 *   - findBand(mhz), findNearLandmark(mhz, tol)  lookups
 *   - emaAutoScale(state, mn, mx)  EMA auto-scale for line plot Y-axis
 *   - paintRowToCanvas(ctx, canvas, powers, cols, rows, minDb, maxDb)
 *                                  scrolling-waterfall row painter
 */
(function () {
  'use strict';
  var R = window.RPI = window.RPI || {};
  if (R.spectrumCommon) return;  // already initialised

  var SVGNS = 'http://www.w3.org/2000/svg';

  // -- Turbo-style colour ramp (6 stops, normalized 0..1) ------------------
  // Computed from the usual Google Turbo control points, rounded.
  var RAMP = [
    [0.00,  30,  20,  80],
    [0.20,   0, 120, 200],
    [0.40,   0, 200, 140],
    [0.60, 220, 220,  40],
    [0.80, 250, 120,  30],
    [1.00, 220,  30,  30],
  ];

  function colorForNorm(t) {
    if (!(t >= 0)) t = 0;
    if (!(t <= 1)) t = 1;
    for (var i = 1; i < RAMP.length; i++) {
      if (t <= RAMP[i][0]) {
        var a = RAMP[i - 1], b = RAMP[i];
        var span = b[0] - a[0];
        var u = span > 0 ? (t - a[0]) / span : 0;
        var r  = a[1] + (b[1] - a[1]) * u;
        var g  = a[2] + (b[2] - a[2]) * u;
        var bl = a[3] + (b[3] - a[3]) * u;
        return [r | 0, g | 0, bl | 0];
      }
    }
    return [RAMP[RAMP.length - 1][1], RAMP[RAMP.length - 1][2], RAMP[RAMP.length - 1][3]];
  }

  // -- SVG / DOM helpers ---------------------------------------------------
  function svg(tag, attrs, text) {
    var el = document.createElementNS(SVGNS, tag);
    if (attrs) {
      for (var k in attrs) {
        if (Object.prototype.hasOwnProperty.call(attrs, k) && attrs[k] != null) {
          el.setAttribute(k, attrs[k]);
        }
      }
    }
    if (text != null) el.appendChild(document.createTextNode(text));
    return el;
  }
  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
  }

  // -- Colour helpers ------------------------------------------------------
  function hexToRgba(hex, alpha) {
    var h = hex.charAt(0) === '#' ? hex.substring(1) : hex;
    if (h.length === 3) h = h[0]+h[0]+h[1]+h[1]+h[2]+h[2];
    var r = parseInt(h.substring(0, 2), 16);
    var g = parseInt(h.substring(2, 4), 16);
    var b = parseInt(h.substring(4, 6), 16);
    return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
  }

  // -- Display helpers -----------------------------------------------------
  function formatMhz(hz) { return (hz / 1e6).toFixed(3); }
  function formatAge(seconds) {
    if (seconds == null || !isFinite(seconds)) return '';
    if (seconds < 2) return 'just now';
    if (seconds < 60) return Math.floor(seconds) + 's ago';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm ago';
    return Math.floor(seconds / 3600) + 'h ago';
  }

  // -- Band reference data -------------------------------------------------
  // Category -> {colour, human name}.  Colours were hand-picked from 11
  // hues wide apart on the wheel so adjacent bands stay distinguishable
  // against the dark background.
  var CATEGORIES = {
    ham:          { color: '#00e5ff', name: 'Amateur Radio' },
    broadcast:    { color: '#ff1744', name: 'Broadcast (FM / TV)' },
    aviation:     { color: '#ffb627', name: 'Aviation' },
    navigation:   { color: '#ffe14d', name: 'Navigation (GPS / VOR)' },
    satellite:    { color: '#bf5af2', name: 'Satellite L-band' },
    cellular:     { color: '#ff6d00', name: 'Cellular' },
    ism:          { color: '#00ff9f', name: 'ISM / Unlicensed' },
    publicsafety: { color: '#00bfa5', name: 'Public Safety / Business' },
    weather:      { color: '#40c4ff', name: 'Weather' },
    military:     { color: '#9c27b0', name: 'Military' },
    gap:          { color: '#2a2f42', name: 'Tuner gap' },
  };

  // Frequency allocations in MHz, ordered ascending.  Scope: ITU Region 2
  // (Americas) allocations that are observable inside the 52 MHz – 2.2 GHz
  // E4000 tuning window and interesting enough to surface in the ribbon.
  // Non-location-specific — local callsigns / repeater listings are
  // deliberately NOT baked in here (location-specific data belongs in a
  // future user-configurable layer, not hard-coded).
  var BANDS = [
    { from:   50,    to:   54,     cat: 'ham',          name: '6m',        desc: '6-meter ham (SSB/CW/FM, sporadic-E DX — "the magic band")' },
    { from:   54,    to:   88,     cat: 'broadcast',    name: 'VHF TV lo', desc: 'Legacy VHF TV ch 2–6 (mostly vacated)' },
    { from:   88,    to:  108,     cat: 'broadcast',    name: 'FM',        desc: 'FM broadcast (wideband FM, 200 kHz ch)' },
    { from:  108,    to:  118,     cat: 'navigation',   name: 'VOR/ILS',   desc: 'Aviation VOR navigation beacons (AM)' },
    { from:  118,    to:  137,     cat: 'aviation',     name: 'Airband',   desc: 'Aviation voice ATC (AM, 8.33/25 kHz)' },
    { from:  137,    to:  138,     cat: 'weather',      name: 'APT',       desc: 'NOAA APT weather satellites (FM)' },
    { from:  144,    to:  148,     cat: 'ham',          name: '2m',        desc: '2-meter ham (FM/SSB/packet/repeaters)' },
    { from:  148,    to:  174,     cat: 'publicsafety', name: 'VHF PS',    desc: 'Public safety, marine, NOAA weather radio' },
    { from:  174,    to:  216,     cat: 'broadcast',    name: 'VHF TV hi', desc: 'VHF TV ch 7–13 (ATSC)' },
    { from:  222,    to:  225,     cat: 'ham',          name: '1.25m',     desc: '1.25-meter ham (Americas only — FM repeaters ~224 MHz)' },
    { from:  225,    to:  400,     cat: 'military',     name: 'UHF mil',   desc: 'Military aviation & satcom (AM + digital)' },
    { from:  420,    to:  450,     cat: 'ham',          name: '70cm',      desc: '70cm ham (FM/DMR/D-STAR/P25)' },
    { from:  450,    to:  470,     cat: 'publicsafety', name: 'FRS/GMRS',  desc: 'Family & business radios, GMRS repeaters' },
    { from:  470,    to:  608,     cat: 'broadcast',    name: 'UHF TV',    desc: 'ATSC digital TV ch 14–36 (8VSB, 6 MHz)' },
    { from:  608,    to:  698,     cat: 'cellular',     name: '600 MHz',   desc: 'T-Mobile 5G n71 (ex-TV, repacked 2020)' },
    { from:  698,    to:  746,     cat: 'cellular',     name: '700 Lo',    desc: 'Lower 700 MHz — AT&T LTE B12/B17, US Cellular (UL 698–716, D/E 716–728, DL 728–746)' },
    { from:  746,    to:  757,     cat: 'cellular',     name: 'VZW 700',   desc: 'Verizon LTE B13 / 5G n13 downlink (746–756 MHz) — "beachfront" 700 MHz C block' },
    { from:  758,    to:  769,     cat: 'publicsafety', name: 'FirstNet',  desc: 'FirstNet LTE B14 downlink — nationwide encrypted public-safety broadband (AT&T-operated)' },
    { from:  769,    to:  775,     cat: 'publicsafety', name: 'PS NB',     desc: 'Public-safety narrowband downlink (P25, land-mobile radio for police/fire/EMS)' },
    { from:  776,    to:  806,     cat: 'cellular',     name: '700 UL',    desc: 'Upper 700 MHz uplink — Verizon B13 (777–787), FirstNet B14 (788–798), PS UL (799–805)' },
    { from:  806,    to:  824,     cat: 'cellular',     name: 'SMR 800',   desc: 'LTE B26 uplink — T-Mobile (ex-Sprint, formerly iDEN/Nextel)' },
    { from:  824,    to:  849,     cat: 'cellular',     name: 'Cell UL',   desc: 'Cellular 850 uplink (LTE B5)' },
    { from:  869,    to:  894,     cat: 'cellular',     name: 'Cell DL',   desc: 'Cellular 850 downlink (LTE B5)' },
    { from:  902,    to:  928,     cat: 'ism',          name: '33cm/ISM',  desc: 'LoRa, Meshtastic, Z-Wave + 33cm ham' },
    { from:  929,    to:  932,     cat: 'publicsafety', name: 'Paging',    desc: 'POCSAG / FLEX paging (mostly quiet)' },
    { from:  960,    to: 1215,     cat: 'aviation',     name: 'DME/SSR',   desc: 'Aviation DME / TACAN + 1090 ADS-B' },
    { from: 1101,    to: 1234,     cat: 'gap',          name: 'LO gap',    desc: 'E4000 tuner dead zone (expected)' },
    { from: 1240,    to: 1300,     cat: 'ham',          name: '23cm',      desc: '23cm ham (1296 MHz SSB/CW calling, ATV, D-STAR DD, EME)' },
    { from: 1525,    to: 1559,     cat: 'satellite',    name: 'Inmarsat',  desc: 'Inmarsat L-band (AERO / STD-C)' },
    { from: 1559,    to: 1610,     cat: 'navigation',   name: 'GNSS L1',   desc: 'GPS L1 / GLONASS / Galileo' },
    { from: 1616,    to: 1626.5,   cat: 'satellite',    name: 'Iridium',   desc: 'Iridium satellite voice / data' },
    { from: 1675,    to: 1710,     cat: 'weather',      name: 'GOES',      desc: 'L-band weather sats (HRIT / LRIT)' },
    { from: 1710,    to: 1755,     cat: 'cellular',     name: 'AWS UL',    desc: 'AWS-1 uplink (LTE B4 / B66)' },
    { from: 1850,    to: 1910,     cat: 'cellular',     name: 'PCS UL',    desc: 'PCS uplink (LTE B2 / n2)' },
    { from: 1930,    to: 1990,     cat: 'cellular',     name: 'PCS DL',    desc: 'PCS downlink (LTE B2 / n2)' },
    { from: 2110,    to: 2155,     cat: 'cellular',     name: 'AWS DL',    desc: 'AWS-1 downlink (LTE B4 / B66)' },
  ];

  // Single-frequency landmarks too narrow to render as bands.  Drawn as
  // dashed vertical guide lines spanning the full plot-wrap height.
  var LANDMARKS = [
    { mhz:  145.800, name: 'ISS',     desc: 'ISS VHF voice downlink' },
    { mhz:  162.475, name: 'NOAA WX', desc: 'NOAA Weather Radio (7 channels 162.400–162.550)' },
    { mhz: 1090.000, name: 'ADS-B',   desc: 'Aircraft ADS-B transponder (1090 MHz, 1 Mbps PPM)' },
    { mhz: 1575.420, name: 'GPS L1',  desc: 'GPS L1 C/A carrier' },
  ];

  // Find the band covering a given frequency, or null.  Bands are ordered
  // but can overlap at boundaries; first match wins (which is fine — the
  // data has no meaningful overlaps, only abutting edges).
  function findBand(mhz) {
    for (var i = 0; i < BANDS.length; i++) {
      var b = BANDS[i];
      if (mhz >= b.from && mhz <= b.to) return b;
    }
    return null;
  }

  // Find the nearest landmark within `tolMhz` of the given frequency, or
  // null.  Used to augment hover tooltip when the user is hovering close
  // to a named single-frequency point (ADS-B, GPS L1, etc.).
  function findNearLandmark(mhz, tolMhz) {
    var best = null, bestD = Infinity;
    for (var i = 0; i < LANDMARKS.length; i++) {
      var d = Math.abs(LANDMARKS[i].mhz - mhz);
      if (d < bestD) { bestD = d; best = LANDMARKS[i]; }
    }
    return (best && bestD <= tolMhz) ? best : null;
  }

  // -- EMA auto-scale for line plot Y axis ---------------------------------
  // On the very first render we lock the window to the measurement — the
  // hard-coded default (-90..-30) is nowhere near the real noise floor of
  // a 2 GHz span, so smoothing toward it would cause a very visible
  // "sliding" of the line plot over several ticks.  Subsequent renders
  // use a 0.7/0.3 EMA so sweep-to-sweep fluctuations don't bounce the
  // axis around.
  //
  // `state` is an object of shape `{ minDb, maxDb, initialized }` that is
  // mutated in place.  Callers own the state across render calls.
  function emaAutoScale(state, mn, mx) {
    var tgtMin = Math.floor(mn - 3);
    var tgtMax = Math.ceil(mx + 3);
    if (!state.initialized) {
      state.minDb = tgtMin;
      state.maxDb = tgtMax;
      state.initialized = true;
    } else {
      state.minDb = state.minDb * 0.7 + tgtMin * 0.3;
      state.maxDb = state.maxDb * 0.7 + tgtMax * 0.3;
    }
    if (state.maxDb - state.minDb < 10) state.maxDb = state.minDb + 10;
  }

  // -- Waterfall row painter -----------------------------------------------
  // Scrolls the existing canvas down by 1 px, then paints a new row at
  // row 0 by sampling `powers` (nearest-neighbour) across `cols` pixels.
  // Colour-mapped through the turbo ramp using the caller-supplied dB
  // scale (so the caller can hold their own auto-scale state).
  function paintRowToCanvas(ctx, canvas, powers, cols, rows, minDb, maxDb) {
    if (!ctx || !powers || !powers.length) return;
    ctx.drawImage(canvas, 0, 0, cols, rows - 1, 0, 1, cols, rows - 1);
    var img = ctx.createImageData(cols, 1);
    var data = img.data;
    var n = powers.length;
    var lo = minDb, hi = maxDb;
    var range = hi - lo;
    if (range < 1) range = 1;
    for (var x = 0; x < cols; x++) {
      // Nearest-neighbour from power array; good enough visually and much
      // faster than linear interpolation here.
      var srcIdx = (n > 1) ? Math.floor((x * (n - 1)) / (cols - 1)) : 0;
      if (srcIdx < 0) srcIdx = 0; else if (srcIdx >= n) srcIdx = n - 1;
      var p = powers[srcIdx];
      var norm;
      if (p == null || !isFinite(p)) {
        norm = 0;
      } else {
        norm = (p - lo) / range;
        if (norm < 0) norm = 0; else if (norm > 1) norm = 1;
      }
      var rgb = colorForNorm(norm);
      var off = x * 4;
      data[off]     = rgb[0];
      data[off + 1] = rgb[1];
      data[off + 2] = rgb[2];
      data[off + 3] = 255;
    }
    ctx.putImageData(img, 0, 0);
  }

  // -- Bulk waterfall repaint ----------------------------------------------
  // Paints many rows at once in a single putImageData call.  Avoids N
  // drawImage() scrolls from repeated paintRowToCanvas() calls (each scroll
  // is a full-canvas copy — 256 of them during a zoom was ~100ms of jank).
  // `rows` is oldest→newest; newest lands at y=0.
  function paintHistoryToCanvas(ctx, canvas, rows, cols, maxRows, minDb, maxDb) {
    if (!ctx || !rows || !rows.length) return;
    var img = ctx.createImageData(cols, maxRows);
    var data = img.data;
    var lo = minDb, hi = maxDb;
    var range = hi - lo;
    if (range < 1) range = 1;
    var count = rows.length < maxRows ? rows.length : maxRows;
    // Newest at y=0 → iterate newest first
    for (var r = 0; r < count; r++) {
      var powers = rows[rows.length - 1 - r];
      if (!powers || !powers.length) continue;
      var n = powers.length;
      var rowOff = r * cols * 4;
      for (var x = 0; x < cols; x++) {
        var srcIdx = (n > 1) ? Math.floor((x * (n - 1)) / (cols - 1)) : 0;
        if (srcIdx < 0) srcIdx = 0; else if (srcIdx >= n) srcIdx = n - 1;
        var p = powers[srcIdx];
        var norm;
        if (p == null || !isFinite(p)) {
          norm = 0;
        } else {
          norm = (p - lo) / range;
          if (norm < 0) norm = 0; else if (norm > 1) norm = 1;
        }
        var rgb = colorForNorm(norm);
        var off = rowOff + x * 4;
        data[off]     = rgb[0];
        data[off + 1] = rgb[1];
        data[off + 2] = rgb[2];
        data[off + 3] = 255;
      }
    }
    ctx.putImageData(img, 0, 0);
  }

  // -- Shared spectrum history store ---------------------------------------
  // Both spectrum panels (SDR full-band + LoRa region-zoom) consume the same
  // rtl_power sweep stream, so the full-bin history buffer lives here rather
  // than duplicated in each panel.  The server pushes the buffer as a
  // "spectrum_history" WS message on connect; subsequent tail rows ride on
  // the regular update broadcast.
  //
  // rows[0] is the NEWEST sweep; rows[len-1] is the oldest kept.
  // rowTimestamps is a parallel array (same ordering, same length as rows)
  // of wall-clock seconds captured when the sweep flushed on the server.
  // Entries are `null` when the backend doesn't ship timestamps (older
  // server during a rolling upgrade) — panels render '—' for those rows
  // rather than falling back to a drift-prone rowIdx * sweep_seconds.
  // generation bumps on load / reset / bin-grid change — panels diff against
  // their last-seen generation to decide when to wipe canvas and bulk-paint.
  var MAX_HIST_ROWS = 256;
  var historyStore = {
    rows: [],
    rowTimestamps: [],
    sweepCount: 0,
    binCount: 0,
    generation: 0,

    loadHistory: function (payload) {
      this.rows = [];
      this.rowTimestamps = [];
      this.sweepCount = 0;
      this.binCount = 0;
      if (payload && payload.available && payload.rows && payload.rows.length) {
        var times = payload.row_timestamps || [];
        for (var i = 0; i < payload.rows.length; i++) {
          var row = payload.rows[i];
          if (!row || !row.length) continue;
          this.rows.unshift(row.slice());
          // Push the matching timestamp (server ships rows oldest-first, so
          // times[i] aligns with rows[i]).  Fall back to null when absent.
          var ts = (i < times.length) ? times[i] : null;
          this.rowTimestamps.unshift((ts != null) ? ts : null);
        }
        if (this.rows.length > MAX_HIST_ROWS) this.rows.length = MAX_HIST_ROWS;
        if (this.rowTimestamps.length > MAX_HIST_ROWS) {
          this.rowTimestamps.length = MAX_HIST_ROWS;
        }
        this.sweepCount = payload.sweep_count || 0;
        this.binCount = payload.bin_count
          || (this.rows[0] ? this.rows[0].length : 0);
      }
      this.generation += 1;
    },

    ingestTick: function (spec) {
      if (!spec) return;
      var binCount = spec.bins_hz ? spec.bins_hz.length : 0;
      if (binCount > 0 && binCount !== this.binCount) {
        // Grid change — rows from before no longer align to the new axis.
        this.rows = [];
        this.rowTimestamps = [];
        this.binCount = binCount;
        this.sweepCount = 0;
        this.generation += 1;
      }
      var sc = spec.sweep_count || 0;
      if (sc <= this.sweepCount) return;
      var tail = spec.waterfall_tail || [];
      var tailTimes = spec.waterfall_tail_times || [];
      if (!tail.length) { this.sweepCount = sc; return; }
      var delta = sc - this.sweepCount;
      var toDraw = delta < tail.length ? delta : tail.length;
      for (var i = tail.length - toDraw; i < tail.length; i++) {
        var row = tail[i];
        if (!row || !row.length) continue;
        this.rows.unshift(row.slice());
        var ts = (i < tailTimes.length) ? tailTimes[i] : null;
        this.rowTimestamps.unshift((ts != null) ? ts : null);
        if (this.rows.length > MAX_HIST_ROWS) this.rows.length = MAX_HIST_ROWS;
        if (this.rowTimestamps.length > MAX_HIST_ROWS) {
          this.rowTimestamps.length = MAX_HIST_ROWS;
        }
      }
      this.sweepCount = sc;
    },

    reset: function () {
      this.rows = [];
      this.rowTimestamps = [];
      this.sweepCount = 0;
      this.binCount = 0;
      this.generation += 1;
    },
  };

  R.spectrumCommon = {
    RAMP: RAMP,
    colorForNorm: colorForNorm,
    svg: svg,
    clear: clear,
    hexToRgba: hexToRgba,
    formatMhz: formatMhz,
    formatAge: formatAge,
    CATEGORIES: CATEGORIES,
    BANDS: BANDS,
    LANDMARKS: LANDMARKS,
    findBand: findBand,
    findNearLandmark: findNearLandmark,
    emaAutoScale: emaAutoScale,
    paintRowToCanvas: paintRowToCanvas,
    paintHistoryToCanvas: paintHistoryToCanvas,
    historyStore: historyStore,
  };
})();
