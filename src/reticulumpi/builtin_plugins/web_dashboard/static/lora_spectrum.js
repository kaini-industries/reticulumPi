/* ReticulumPi Dashboard — LoRa Spectrum panel
 *
 * A project-aware zoom of the generic SDR sweep onto the LoRa ISM band.
 * Unlike the generic spectrum panel, this one overlays what matters for a
 * Reticulum / Meshtastic / MeshCore node:
 *
 *   • Our RNode's configured RX channel (translucent rect).
 *   • The Meshtastic LongFast channel grid (one tick per channel, configured
 *     channel highlighted and labeled).
 *   • Regulatory band edges (dashed lines; warning badge if RNode is off-band).
 *   • Hover crosshair: frequency / dB / sweep age, plus context for whatever
 *     overlay (RNode TX window, MT channel, off-band edge) is under cursor.
 *
 * Shares rtl_power data with `spectrum.js` — no second SDR stream; this
 * panel just clips `bins_hz` / `latest_powers_db` / `waterfall_tail` to the
 * detected region's band window.  Shared primitives (turbo colormap,
 * waterfall painter, SVG/DOM helpers, EMA auto-scale) come from
 * `window.RPI.spectrumCommon`.
 */
(function () {
  'use strict';
  var R = window.RPI;
  if (!R) return;
  var $ = R.$, esc = R.esc;
  var SC = R.spectrumCommon;
  if (!SC) return;  // common module must load first

  // -- Region / band reference --------------------------------------------
  // LoRa ISM bands per Meshtastic's LoRaConfig.RegionCode enum.  Base,
  // spacing (kHz), count from the Meshtastic `RegionInfo` tables
  // (github.com/meshtastic/firmware).  Only the six commonly observed
  // regions — others fall back to US with an advisory.
  var REGIONS = {
    US:     { lo: 902.0, hi: 928.0,   base: 902.8625, spacing: 250, count: 108, label: 'US 902–928' },
    EU_868: { lo: 863.0, hi: 870.0,   base: 864.125,  spacing: 250, count: 24,  label: 'EU 868' },
    EU_433: { lo: 433.0, hi: 434.7,   base: 433.175,  spacing: 250, count: 6,   label: 'EU 433' },
    CN:     { lo: 470.0, hi: 510.0,   base: 470.125,  spacing: 250, count: 160, label: 'CN 470–510' },
    JP:     { lo: 920.8, hi: 927.8,   base: 921.875,  spacing: 500, count: 15,  label: 'JP 920–928' },
    ANZ:    { lo: 915.0, hi: 928.0,   base: 916.375,  spacing: 250, count: 52,  label: 'ANZ 915–928' },
  };

  // Meshtastic LongFast default channel per region (from firmware).  Other
  // presets use the same grid; we just highlight a different index.  For
  // presets where we don't have a hard-coded default, we fall back to the
  // region's LongFast channel.
  var DEFAULT_CHANNEL = {
    US: 20, EU_868: 8, EU_433: 0, CN: 0, JP: 0, ANZ: 20,
  };

  // -- Constants ----------------------------------------------------------
  var WF_COLS = 800;
  var WF_ROWS = 256;

  // -- DOM handles (resolved lazily on first data tick) --------------------
  var _section = null, _body = null, _toggle = null, _countEl = null;
  var _metaEl, _lineEl, _wfCanvas, _wfCtx, _overlayEl, _hoverEl, _scaleEl, _bandsEl;

  // -- Runtime state -------------------------------------------------------
  var _expanded = false;
  var _region = 'US', _regionInfo = REGIONS.US;
  var _regionSource = 'default';
  var _modemPreset = null;
  var _mtChannelIdx = null;    // configured MT channel index, or null
  var _ourFreqHz = null, _ourBwHz = null, _ourSf = null, _ourCr = null;
  var _lastSweepCount = 0;
  var _lastRenderedSweep = 0;
  var _scale = { minDb: -90, maxDb: -30, initialized: false };
  // History backfill state machine — mirrors spectrum.js.  The WS broadcast
  // only carries the last few sweeps, so on first sweep arrival we fetch the
  // plugin's full rolling buffer and paint it oldest→newest.  Without this
  // the waterfall takes WF_ROWS seconds to fill from empty on every reload.
  // States: 'pending' → 'fetching' → 'ready' | 'failed' | 'abandoned'.
  var _historyState = 'pending';
  var _fetchStartedAt = 0;
  var _FETCH_ABANDON_MS = 4000;
  var _lastBinSig = '';        // "<start>|<stop>|<bincount>" for slice change detection
  var _lastOverlaySig = '';    // "<region>|<freq>|<bw>|<preset>|<zoom>" for overlay rebuild
  var _lastData = null;
  // Zoom: null = full clipped region; else [loMhz, hiMhz] user-selected window.
  var _zoom = null;
  var _dragState = null;       // {startFrac, curFrac, rectEl} during a drag
  var _zoomResetEl = null;     // "Reset zoom" chip
  var _presetBtns = {};        // {rnode, full} button handles

  // PR2 state — activity column, peak-hold trace, peers strip
  var _activityCanvas = null, _activityCtx = null;
  var _lastIfaceRxb = null, _lastIfaceTxb = null;
  var _pendingActivity = null; // 'tx' | 'rx' | null — flushed to newest painted row
  var _activityRows = new Array(WF_ROWS);  // [0] = newest; values 'tx' | 'rx' | null
  var _peakHoldEnabled = false;
  var _peakHoldDb = null;      // per-bin max dB, aligned with spec.bins_hz
  // Ring buffer of full-bin dB arrays per painted sweep, [0]=newest, capped
  // at WF_ROWS.  Kept so zoom changes can repaint the waterfall at the new
  // axis instead of wiping history.  Cleared on bin-grid / region change
  // since indices would no longer align.
  var _historyDb = [];

  // -- DOM setup -----------------------------------------------------------
  function _resolveDom() {
    if (_section) return true;
    _section = $('lora-spectrum-section');
    if (!_section) return false;
    _body = $('lora-spectrum-body');
    _toggle = $('lora-spectrum-toggle');
    _countEl = $('lora-spectrum-count');
    _metaEl = $('lora-spectrum-meta');
    _bandsEl = $('lora-spectrum-bands');
    _lineEl = $('lora-spectrum-line');
    _wfCanvas = $('lora-spectrum-waterfall');
    _overlayEl = $('lora-spectrum-overlay');
    _hoverEl = $('lora-spectrum-hover');
    _scaleEl = $('lora-spectrum-scale');
    _zoomResetEl = $('lora-spec-zoom-reset');
    _activityCanvas = $('lora-spec-activity-col');
    if (_activityCanvas) _activityCtx = _activityCanvas.getContext('2d');

    if (_wfCanvas) {
      _wfCanvas.width = WF_COLS;
      _wfCanvas.height = WF_ROWS;
      _wfCtx = _wfCanvas.getContext('2d');
      _wfCtx.fillStyle = '#0a0d17';
      _wfCtx.fillRect(0, 0, WF_COLS, WF_ROWS);
      _wfCanvas.addEventListener('mousemove', _onHover);
      _wfCanvas.addEventListener('mouseleave', _onHoverLeave);
    }

    if (_lineEl) {
      _lineEl.addEventListener('mousedown', _onDragStart);
      // mousemove/up on window so drag still works if the cursor leaves the SVG.
      window.addEventListener('mousemove', _onDragMove);
      window.addEventListener('mouseup', _onDragEnd);
    }
    var plotWrap = _body ? _body.querySelector('.lora-spectrum-plot-wrap') : null;
    if (plotWrap) plotWrap.addEventListener('dblclick', _onDoubleClickReset);
    if (_zoomResetEl) _zoomResetEl.addEventListener('click', _onZoomChipClick);

    if (_toggle) _toggle.addEventListener('click', _onToggleClick);
    return true;
  }

  function _onToggleClick() {
    if (!_body) return;
    _expanded = _body.classList.contains('hidden');
    _body.classList.toggle('hidden');
    var chev = _toggle.querySelector('.chevron');
    if (chev) chev.innerHTML = _expanded ? '&#9662;' : '&#9656;';
    if (!_expanded) {
      // Collapsing — reset zoom so reopening gives a clean, oriented view.
      _setZoom(null, /*silent=*/true);
    }
    if (_expanded && _lastData) _renderAll(_lastData);
  }

  // -- Region detection ---------------------------------------------------
  function _pickRegionFromFreq(freqHz) {
    if (freqHz == null || !isFinite(freqHz)) return null;
    var mhz = freqHz / 1e6;
    // Prefer ANZ over US when the freq clearly lands in the ANZ-only slice,
    // but since 915–928 is inside both, default to US.  Users can always
    // override via Meshtastic region.
    for (var name in REGIONS) {
      if (!Object.prototype.hasOwnProperty.call(REGIONS, name)) continue;
      var r = REGIONS[name];
      if (mhz >= r.lo && mhz <= r.hi) {
        // When both US and ANZ qualify, prefer US unless the freq is in the
        // exclusively-ANZ slice (none in practice — 915–928 is a proper
        // subset of US).  So return the first match in enum iteration order.
        return name;
      }
    }
    return null;
  }

  function _detectRegion(data) {
    var name = null, source = 'default';
    var md = data.meshtastic_device;
    if (md && md.region && REGIONS[md.region]) {
      name = md.region;
      source = 'meshtastic';
    } else {
      var ifaces = data.interfaces || [];
      for (var i = 0; i < ifaces.length; i++) {
        if (ifaces[i].type !== 'RNodeInterface') continue;
        var radio = ifaces[i].radio || {};
        var f = radio.frequency;
        var hit = _pickRegionFromFreq(f);
        if (hit) { name = hit; source = 'rnode'; break; }
      }
    }
    if (!name) { name = 'US'; source = 'default'; }
    var regionChanged = (name !== _region);
    _region = name;
    _regionInfo = REGIONS[name];
    _regionSource = source;

    // Preset → configured MT channel (region-agnostic fallback)
    _modemPreset = (md && md.modem_preset) ? md.modem_preset : null;
    _mtChannelIdx = (name in DEFAULT_CHANNEL) ? DEFAULT_CHANNEL[name] : 0;

    if (regionChanged) {
      // Zoom bounds are in MHz against the old region; drop them.  Also
      // flushes the activity ring + peak-hold (stale against the new band).
      _setZoom(null, /*silent=*/true);
    }
  }

  function _detectRNode(data) {
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
      return;
    }
  }

  // -- Clipping / geometry ------------------------------------------------
  // Returns { loIdx, hiIdx, loMhz, hiMhz, binStepKhz, zoomed } or null if no
  // overlap.  ``range`` defaults to the full region band; when supplied
  // (e.g. from user zoom), constrains the view to [loMhz, hiMhz].  The
  // result is the intersection of the region and the zoom, clamped to
  // actual bin centres we have data for.
  function _clip(spec, range) {
    var bins = spec.bins_hz;
    if (!bins || bins.length === 0) return null;
    var rLo = _regionInfo.lo, rHi = _regionInfo.hi;
    if (range && range.length === 2) {
      rLo = Math.max(rLo, range[0]);
      rHi = Math.min(rHi, range[1]);
      if (rHi <= rLo) return null;
    }
    var loHz = rLo * 1e6, hiHz = rHi * 1e6;
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
      zoomed: !!range,
    };
  }

  function _xPct(mhz, clip) {
    var span = clip.hiMhz - clip.loMhz;
    if (span <= 0) return 0;
    return ((mhz - clip.loMhz) / span) * 100;
  }

  // -- Stats helpers -------------------------------------------------------
  function _median(arr) {
    if (!arr || !arr.length) return null;
    var copy = arr.slice().sort(function (a, b) { return a - b; });
    var m = copy.length >> 1;
    return (copy.length & 1) ? copy[m] : (copy[m - 1] + copy[m]) / 2;
  }
  // Returns { noiseFloor, peakDb, peakMhz } over the visible clipped range.
  function _computeStats(spec, clip) {
    var powers = spec.latest_powers_db;
    if (!powers || !powers.length || !clip) return null;
    var vals = [];
    var peak = -Infinity, peakIdx = -1;
    for (var i = clip.loIdx; i <= clip.hiIdx; i++) {
      var v = powers[i];
      if (v == null || !isFinite(v)) continue;
      vals.push(v);
      if (v > peak) { peak = v; peakIdx = i; }
    }
    if (!vals.length) return null;
    var bins = spec.bins_hz;
    return {
      noiseFloor: _median(vals),
      peakDb: peak,
      peakMhz: (bins && peakIdx >= 0) ? bins[peakIdx] / 1e6 : null,
    };
  }

  function _airtimeClass(v) {
    if (v == null) return '';
    if (v > 25) return 'lora-stat-crit';
    if (v > 10) return 'lora-stat-warn';
    return 'lora-stat-ok';
  }

  // -- Meta strip ----------------------------------------------------------
  // Structural shell (region label, buttons, layout cells) is rebuilt only
  // when an enable-state changes; per-tick updates touch text values and
  // CSS classes on cached refs.  Keeps click handlers alive across ticks
  // so a mid-flight click doesn't get swallowed by an innerHTML swap.
  var _metaStructSig = '';
  var _metaRefs = null;

  function _renderMeta(data, clip) {
    if (!_metaEl) return;
    var spec = data.spectrum || {};
    var stats = _computeStats(spec, clip);
    var diag = data.lora_diagnostics || {};

    var hasRNode = _ourFreqHz != null && _ourBwHz != null;
    var structSig = [
      _regionInfo.label,
      hasRNode ? '1' : '0',
      _zoom ? '1' : '0',
      _peakHoldEnabled ? '1' : '0',
    ].join('|');

    if (structSig !== _metaStructSig) {
      _metaStructSig = structSig;
      _metaEl.innerHTML = ''
        + '<span class="lora-meta-region">' + esc(_regionInfo.label) + '</span>'
        + '<span class="lora-zoom-presets">'
        +   '<button class="lora-zoom-preset" data-preset="rnode"'
        +     (hasRNode ? '' : ' disabled') + '>Zoom RNode</button>'
        +   '<button class="lora-zoom-preset" data-preset="full"'
        +     (_zoom ? '' : ' disabled') + '>Full band</button>'
        + '</span>'
        + '<button class="lora-peakhold-toggle' + (_peakHoldEnabled ? ' active' : '')
        +   '" title="Peak-hold trace (max per bin since enable)">Peak hold</button>'
        + '<span class="lora-stats-strip">'
        +   '<span class="lora-stat"><span class="lora-stat-label">Noise</span>'
        +     '<span class="lora-stat-value" data-role="noise">—</span></span>'
        +   '<span class="lora-stat"><span class="lora-stat-label">Peak</span>'
        +     '<span class="lora-stat-value" data-role="peak">—</span></span>'
        +   '<span class="lora-stat-rnode-group" title="Reticulum RNode diagnostics">'
        +     '<span class="lora-stat-group-label">RNode</span>'
        +     '<span class="lora-stat"><span class="lora-stat-label">Airtime</span>'
        +       '<span class="lora-stat-value" data-role="airtime">—</span></span>'
        +     '<span class="lora-stat-dot" data-role="online" title="—"></span>'
        +   '</span>'
        + '</span>';

      _presetBtns = {};
      var btns = _metaEl.querySelectorAll('.lora-zoom-preset');
      for (var i = 0; i < btns.length; i++) {
        _presetBtns[btns[i].getAttribute('data-preset')] = btns[i];
        btns[i].addEventListener('click', _onPresetClick);
      }
      var phBtn = _metaEl.querySelector('.lora-peakhold-toggle');
      if (phBtn) phBtn.addEventListener('click', _onPeakHoldToggle);

      _metaRefs = {
        noise: _metaEl.querySelector('[data-role="noise"]'),
        peak: _metaEl.querySelector('[data-role="peak"]'),
        airtime: _metaEl.querySelector('[data-role="airtime"]'),
        online: _metaEl.querySelector('[data-role="online"]'),
      };
    }

    if (!_metaRefs) return;
    var nfTxt = (stats && stats.noiseFloor != null)
      ? stats.noiseFloor.toFixed(1) + ' dB' : '—';
    var peakTxt = (stats && isFinite(stats.peakDb))
      ? (stats.peakDb.toFixed(1) + ' dB @ '
         + (stats.peakMhz != null ? stats.peakMhz.toFixed(3) + ' MHz' : '—'))
      : '—';
    var airtime = diag.airtime_short;
    var atTxt = (airtime != null) ? airtime.toFixed(1) + '%' : '—';
    var atClass = _airtimeClass(airtime);
    var onlineCls, onlineTxt;
    if (diag.online === true) { onlineCls = 'lora-stat-online'; onlineTxt = 'online'; }
    else if (diag.online === false) { onlineCls = 'lora-stat-offline'; onlineTxt = 'offline'; }
    else { onlineCls = 'lora-stat-unknown'; onlineTxt = '—'; }

    if (_metaRefs.noise) _metaRefs.noise.textContent = nfTxt;
    if (_metaRefs.peak) _metaRefs.peak.textContent = peakTxt;
    if (_metaRefs.airtime) {
      _metaRefs.airtime.textContent = atTxt;
      _metaRefs.airtime.className = 'lora-stat-value ' + atClass;
    }
    if (_metaRefs.online) {
      _metaRefs.online.className = 'lora-stat-dot ' + onlineCls;
      _metaRefs.online.title = onlineTxt;
    }
  }

  // -- Scale strip (below waterfall) ---------------------------------------
  function _renderScale(data, clip) {
    if (!_scaleEl) return;
    var spec = data.spectrum || {};
    var parts = [];
    if (_scale.initialized) {
      parts.push(_scale.minDb.toFixed(0) + ' / ' + _scale.maxDb.toFixed(0) + ' dB');
    }
    if (clip) {
      var span = clip.hiMhz - clip.loMhz;
      var nbins = clip.hiIdx - clip.loIdx + 1;
      parts.push(span.toFixed(2) + ' MHz · ' + nbins + ' bins');
      if (clip.zoomed) {
        parts.push('<span class="lora-spec-resolution-note">'
          + 'zoom: ' + nbins + ' bins / '
          + (clip.binStepKhz ? clip.binStepKhz.toFixed(0) : '—')
          + ' kHz step — scanner-limited</span>');
      }
    }
    if (spec.sweep_seconds) {
      parts.push(spec.sweep_seconds + 's/sweep');
    }
    _scaleEl.innerHTML = parts.join(' · ');
  }

  // -- Reset-zoom chip -----------------------------------------------------
  function _renderZoomChip(clip) {
    if (!_zoomResetEl) return;
    _zoomResetEl.style.display = (_zoom && clip) ? '' : 'none';
  }

  // -- Line plot ----------------------------------------------------------
  // ``pts`` polyline when bin count is dense; step-rect bars at deep zoom so
  // the rendering is honest about the scanner's 250 kHz bin resolution.
  var STEP_BAR_THRESHOLD = 40;
  function _renderLine(data, clip) {
    if (!_lineEl || !clip) { if (_lineEl) SC.clear(_lineEl); return; }
    var spec = data.spectrum;
    var powers = spec.latest_powers_db;
    if (!powers || !powers.length) return;
    var n = clip.hiIdx - clip.loIdx + 1;
    var mn = Infinity, mx = -Infinity;
    for (var i = clip.loIdx; i <= clip.hiIdx; i++) {
      var v = powers[i];
      if (v == null || !isFinite(v)) continue;
      if (v < mn) mn = v;
      if (v > mx) mx = v;
    }
    if (!isFinite(mn) || !isFinite(mx)) return;
    SC.emaAutoScale(_scale, mn, mx);

    // Peak-hold accumulator — per-bin max across sweeps since enable/reset.
    // Track across ALL bins (not just the visible clip), so zooming out
    // after a narrow-band watch reveals the peaks we accumulated in the
    // previously-invisible parts of the band.  O(bin_count) per sweep is
    // cheap; typically 100–300 bins for LoRa.
    if (_peakHoldEnabled) {
      if (!_peakHoldDb || _peakHoldDb.length !== powers.length) {
        _peakHoldDb = new Array(powers.length);
      }
      for (var ph = 0; ph < powers.length; ph++) {
        var pv = powers[ph];
        if (pv == null || !isFinite(pv)) continue;
        var prev = _peakHoldDb[ph];
        if (prev == null || pv > prev) _peakHoldDb[ph] = pv;
      }
    }

    var vbW = 800, vbH = 120;
    SC.clear(_lineEl);

    // dB grid
    var step = 10;
    var first = Math.ceil(_scale.minDb / step) * step;
    for (var db = first; db <= _scale.maxDb; db += step) {
      var gy = vbH - 1 - ((db - _scale.minDb) / (_scale.maxDb - _scale.minDb)) * (vbH - 2);
      _lineEl.appendChild(SC.svg('line', {
        x1: 0, y1: gy, x2: vbW, y2: gy, stroke: '#1a2233', 'stroke-width': 0.5,
      }));
      _lineEl.appendChild(SC.svg('text', {
        x: 4, y: gy - 2, fill: '#4a5570', 'font-size': 9,
      }, db.toFixed(0)));
    }

    var span = _scale.maxDb - _scale.minDb;

    // Peak-hold trace — dashed gold, rendered behind the live line.
    if (_peakHoldEnabled && _peakHoldDb) {
      var pkPts = [];
      for (var pk = 0, pi = clip.loIdx; pi <= clip.hiIdx; pi++, pk++) {
        var pd = _peakHoldDb[pi];
        if (pd == null || !isFinite(pd)) continue;
        var pkX = (pk * (vbW - 1)) / Math.max(1, n - 1);
        var pkY = vbH - 1 - ((pd - _scale.minDb) / span) * (vbH - 2);
        if (pkY < 0) pkY = 0; else if (pkY > vbH - 1) pkY = vbH - 1;
        pkPts.push(pkX.toFixed(1) + ',' + pkY.toFixed(1));
      }
      if (pkPts.length) {
        _lineEl.appendChild(SC.svg('polyline', {
          points: pkPts.join(' '),
          fill: 'none',
          stroke: 'rgba(240, 200, 80, 0.65)',
          'stroke-width': 0.9,
          'stroke-dasharray': '2,2',
          'vector-effect': 'non-scaling-stroke',
        }));
      }
    }

    if (n < STEP_BAR_THRESHOLD) {
      // Step-rect bars — one rect per visible bin.
      var barW = vbW / n;
      for (var bk = 0, bi = clip.loIdx; bi <= clip.hiIdx; bi++, bk++) {
        var bp = powers[bi];
        if (bp == null || !isFinite(bp)) continue;
        var bh = ((bp - _scale.minDb) / span) * (vbH - 2);
        if (bh < 0) bh = 0; else if (bh > vbH - 2) bh = vbH - 2;
        var by = vbH - 1 - bh;
        _lineEl.appendChild(SC.svg('rect', {
          x: (bk * barW).toFixed(2), y: by.toFixed(2),
          width: Math.max(0.5, barW - 0.5).toFixed(2),
          height: bh.toFixed(2),
          fill: 'rgba(88, 166, 255, 0.55)',
          stroke: '#58a6ff',
          'stroke-width': 0.8,
        }));
      }
    } else {
      // Dense polyline — unchanged behaviour.
      var pts = [];
      for (var k = 0, idx = clip.loIdx; idx <= clip.hiIdx; idx++, k++) {
        var p = powers[idx];
        if (p == null || !isFinite(p)) continue;
        var x = (k * (vbW - 1)) / Math.max(1, n - 1);
        var y = vbH - 1 - ((p - _scale.minDb) / span) * (vbH - 2);
        if (y < 0) y = 0; else if (y > vbH - 1) y = vbH - 1;
        pts.push(x.toFixed(1) + ',' + y.toFixed(1));
      }
      _lineEl.appendChild(SC.svg('polyline', {
        points: pts.join(' '),
        fill: 'none', stroke: '#58a6ff', 'stroke-width': 1.2,
        'vector-effect': 'non-scaling-stroke',
      }));
    }

    // MHz ticks
    var tickCount = 5;
    for (var t = 0; t <= tickCount; t++) {
      var frac = t / tickCount;
      var fx = frac * vbW;
      var fmhz = clip.loMhz + frac * (clip.hiMhz - clip.loMhz);
      _lineEl.appendChild(SC.svg('text', {
        x: fx, y: vbH - 2, fill: '#4a5570', 'font-size': 9,
        'text-anchor': t === 0 ? 'start' : (t === tickCount ? 'end' : 'middle'),
      }, fmhz.toFixed(3)));
    }

    // Live drag preview rect (if dragging right now).
    if (_dragState) {
      var a = Math.min(_dragState.startFrac, _dragState.curFrac);
      var b = Math.max(_dragState.startFrac, _dragState.curFrac);
      var rx = a * vbW, rw = (b - a) * vbW;
      _lineEl.appendChild(SC.svg('rect', {
        x: rx.toFixed(1), y: 0, width: Math.max(1, rw).toFixed(1), height: vbH,
        fill: 'rgba(88, 166, 255, 0.15)',
        stroke: '#58a6ff',
        'stroke-width': 1,
        'stroke-dasharray': '3,2',
      }));
    }
  }

  // -- History backfill ---------------------------------------------------
  // One-shot fetch of the plugin's full rolling waterfall buffer, sliced to
  // the current region/zoom clip.  Called once per page load (and re-armed
  // on bin-grid change).  Paints oldest→newest so the oldest history ends
  // up deepest in the waterfall, matching live behaviour.
  function _fetchHistory(clip, expectedBinCount) {
    if (_historyState !== 'pending') return;
    if (!clip || !_wfCtx) return;
    _historyState = 'fetching';
    _fetchStartedAt = Date.now();
    fetch('/api/spectrum/history', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (payload) {
        // If the main loop gave up waiting on us and started painting live
        // rows, applying history now would invert temporal order.
        if (_historyState === 'abandoned') return;
        var d = (payload && payload.data) ? payload.data : null;
        if (!d || !d.available || !d.rows || !d.rows.length) {
          _historyState = 'failed';
          return;
        }
        // Defensive: bin grid may have changed mid-fetch (config reload,
        // scanner restart) — historical rows would no longer line up with
        // the current axis.  Let WS tail fill in normally.
        if (d.bin_count && expectedBinCount && d.bin_count !== expectedBinCount) {
          _historyState = 'failed';
          return;
        }
        for (var i = 0; i < d.rows.length; i++) {
          var row = d.rows[i];
          if (!row || !row.length) continue;
          var sliced = row.slice(clip.loIdx, clip.hiIdx + 1);
          SC.paintRowToCanvas(_wfCtx, _wfCanvas, sliced, WF_COLS, WF_ROWS,
                              _scale.minDb, _scale.maxDb);
          // Preserve the full-bin row so zoom changes can repaint without
          // losing history.  Iterating oldest-first + unshifting puts the
          // newest backfilled row at [0], matching the live-ingest convention.
          _historyDb.unshift(row.slice());
          if (_historyDb.length > WF_ROWS) _historyDb.length = WF_ROWS;
        }
        _lastSweepCount = d.sweep_count || 0;
        _historyState = 'ready';
      })
      .catch(function () {
        if (_historyState !== 'abandoned') _historyState = 'failed';
      });
  }

  // -- Waterfall ingest ---------------------------------------------------
  function _ingestNewSweeps(data, clip) {
    var spec = data.spectrum;
    var sc = spec.sweep_count || 0;
    var delta = sc - _lastSweepCount;
    if (delta <= 0) return;
    var tail = spec.waterfall_tail || [];
    if (!tail.length) { _lastSweepCount = sc; return; }
    var toDraw = Math.min(delta, tail.length);
    for (var i = tail.length - toDraw; i < tail.length; i++) {
      var row = tail[i];
      if (!row || !row.length) continue;
      // Slice to region
      var sliced = row.slice(clip.loIdx, clip.hiIdx + 1);
      SC.paintRowToCanvas(_wfCtx, _wfCanvas, sliced, WF_COLS, WF_ROWS,
                          _scale.minDb, _scale.maxDb);
      // Preserve the full-bin row so we can repaint at a different zoom
      // window later without losing waterfall history.
      _historyDb.unshift(row.slice());
      if (_historyDb.length > WF_ROWS) _historyDb.length = WF_ROWS;
      // Activity is observed at WS tick granularity; attribute only to the
      // newest painted row to avoid smearing a single event over multiple
      // sweeps when the tick covers > 1 sweep.
      var isNewest = (i === tail.length - 1);
      _ingestActivityRow(isNewest ? _pendingActivity : null);
    }
    _pendingActivity = null;
    _renderActivityCol();
    _lastSweepCount = sc;
  }

  // -- Overlays (RNode box, MT grid, reg edges) ---------------------------
  function _renderOverlay(data, clip) {
    if (!_overlayEl) return;
    var sig = [_region, _ourFreqHz, _ourBwHz, _modemPreset, _mtChannelIdx,
               clip ? clip.loMhz.toFixed(3) : '', clip ? clip.hiMhz.toFixed(3) : '',
               _zoom ? 'Z' : 'F'].join('|');
    if (sig === _lastOverlaySig) return;
    _lastOverlaySig = sig;

    SC.clear(_overlayEl);
    if (!clip) return;

    // Regulatory edges — dashed lines at band lo/hi if inside viewport.
    _drawEdge(clip, _regionInfo.lo, 'lora-reg-edge');
    _drawEdge(clip, _regionInfo.hi, 'lora-reg-edge');

    // Meshtastic channel grid: iterate up to 256 channels max (safety),
    // stop when we exit the viewport.
    var base = _regionInfo.base, stepMhz = _regionInfo.spacing / 1000;
    var maxIdx = Math.min(_regionInfo.count, 256);
    for (var i = 0; i < maxIdx; i++) {
      var cmhz = base + i * stepMhz;
      if (cmhz < clip.loMhz) continue;
      if (cmhz > clip.hiMhz) break;
      var tick = document.createElement('div');
      tick.className = 'lora-mt-tick';
      tick.style.left = _xPct(cmhz, clip).toFixed(3) + '%';
      _overlayEl.appendChild(tick);
    }

    // RNode highlight rectangle.
    if (_ourFreqHz != null && _ourBwHz != null) {
      var fMhz = _ourFreqHz / 1e6;
      var bwMhz = _ourBwHz / 1e6;
      var lo = fMhz - bwMhz / 2, hi = fMhz + bwMhz / 2;
      // Only render if any part overlaps the viewport.
      if (hi > clip.loMhz && lo < clip.hiMhz) {
        var clampedLo = Math.max(lo, clip.loMhz);
        var clampedHi = Math.min(hi, clip.hiMhz);
        var leftPct = _xPct(clampedLo, clip);
        var widthPct = _xPct(clampedHi, clip) - leftPct;
        var box = document.createElement('div');
        box.className = 'lora-rnode-box';
        box.style.left = leftPct.toFixed(3) + '%';
        box.style.width = Math.max(0.2, widthPct).toFixed(3) + '%';
        var lblTxt = fMhz.toFixed(3) + ' MHz';
        if (_ourBwHz) lblTxt += ' · BW ' + (_ourBwHz / 1000).toFixed(0) + 'k';
        if (_ourSf) lblTxt += ' · SF ' + _ourSf;
        var lblBox = document.createElement('span');
        lblBox.className = 'lora-rnode-box-label';
        lblBox.textContent = lblTxt;
        box.appendChild(lblBox);
        _overlayEl.appendChild(box);
      }
    }
  }

  function _drawEdge(clip, mhz, klass) {
    if (mhz < clip.loMhz || mhz > clip.hiMhz) return;
    var e = document.createElement('div');
    e.className = klass;
    e.style.left = _xPct(mhz, clip).toFixed(3) + '%';
    _overlayEl.appendChild(e);
  }

  // -- Hover crosshair ----------------------------------------------------
  // Same UX as the SDR spectrum panel — show frequency, dB at that bin, and
  // sweep age — plus a second line when inside our RNode's TX/RX window or
  // off the regulatory band edges.
  function _onHover(ev) {
    if (!_lastData || !_lastData.spectrum) return;
    var spec = _lastData.spectrum;
    var clip = _clip(spec, _zoom);
    if (!clip || !_hoverEl) return;

    var rect = _wfCanvas.getBoundingClientRect();
    var fracX = (ev.clientX - rect.left) / rect.width;
    var fracY = (ev.clientY - rect.top) / rect.height;
    if (fracX < 0 || fracX > 1 || fracY < 0 || fracY > 1) return;

    var freqMhz = clip.loMhz + fracX * (clip.hiMhz - clip.loMhz);

    // dB value at the nearest bin in the clipped slice.
    var n = clip.hiIdx - clip.loIdx + 1;
    var binIdx = clip.loIdx + Math.min(n - 1, Math.max(0, Math.round(fracX * (n - 1))));
    var powers = spec.latest_powers_db;
    var db = (powers && powers[binIdx] != null && isFinite(powers[binIdx])) ? powers[binIdx] : null;
    var dbStr = (db != null) ? db.toFixed(1) + ' dB' : '—';

    // Row 0 = newest sweep; each row down = one sweep_seconds older.
    var rowIdx = Math.floor(fracY * WF_ROWS);
    var agoSec = rowIdx * (spec.sweep_seconds || 2);
    var ageStr = rowIdx === 0 ? 'now' : (agoSec + 's ago');

    var headerLine = freqMhz.toFixed(3) + ' MHz · ' + dbStr + ' · ' + ageStr;
    var bandLine = '';

    // 1. Inside our RNode's TX/RX window?
    if (_ourFreqHz != null && _ourBwHz != null) {
      var fMhz = _ourFreqHz / 1e6;
      var bwMhz = _ourBwHz / 1e6;
      if (freqMhz >= fMhz - bwMhz / 2 && freqMhz <= fMhz + bwMhz / 2) {
        var rnLbl = '<strong>RNode</strong> · ' + fMhz.toFixed(3) + ' MHz';
        if (_ourBwHz) rnLbl += ' · BW ' + (_ourBwHz / 1000).toFixed(0) + 'k';
        if (_ourSf)   rnLbl += ' · SF ' + _ourSf;
        bandLine = rnLbl;
      }
    }

    // 2. Off the regulatory band edges entirely?
    if (!bandLine && (freqMhz < _regionInfo.lo || freqMhz > _regionInfo.hi)) {
      bandLine = '<strong>Out of band</strong> for ' + esc(_regionInfo.label);
    }

    if (bandLine) {
      _hoverEl.innerHTML = esc(headerLine)
        + '<div class="lora-spectrum-hover-band">' + bandLine + '</div>';
    } else {
      _hoverEl.textContent = headerLine;
    }
    _hoverEl.style.display = 'block';
  }

  function _onHoverLeave() {
    if (_hoverEl) _hoverEl.style.display = 'none';
  }

  // -- Zoom interaction ---------------------------------------------------
  // Click-and-drag horizontally on the line plot to select a freq range.
  // The SVG handles events cleanly; the waterfall canvas is reserved for
  // the hover crosshair.  On release, commit _zoom and re-render.
  function _setZoom(range, silent) {
    _zoom = range;
    _lastOverlaySig = '';  // overlay bounds depend on zoom
    if (silent) {
      // Hard reset path — called when the bin grid / region changed, so the
      // dB history and per-view caches no longer align to the new axis.
      _historyDb = [];
      _resetActivity();
      _peakHoldDb = null;
      _lastRenderedSweep = 0;
      if (_wfCtx) {
        _wfCtx.fillStyle = '#0a0d17';
        _wfCtx.fillRect(0, 0, WF_COLS, WF_ROWS);
      }
      return;
    }
    // User-initiated zoom — bins/time don't change, so activity rows,
    // peak-hold, and dB history all remain valid.  Re-render first so
    // `_scale` (EMA-smoothed) reflects the NEW visible window, then repaint
    // the waterfall from history against that fresh scale; otherwise the
    // historical rows would render with the pre-zoom min/max colour range.
    if (_lastData) {
      _renderAll(_lastData);
      var clip = _clip(_lastData.spectrum || {}, _zoom);
      if (clip) _repaintWaterfallFromHistory(clip);
    }
  }

  // Redraw every stored sweep row into the waterfall at the supplied clip.
  // Uses the bulk paintHistoryToCanvas helper so we do ONE putImageData
  // instead of N drawImage-scrolls (a WF_ROWS-deep history is ~256 rows
  // and the old path triggered 256 full-canvas copies per zoom).
  function _repaintWaterfallFromHistory(clip) {
    if (!_wfCtx) return;
    _wfCtx.fillStyle = '#0a0d17';
    _wfCtx.fillRect(0, 0, WF_COLS, WF_ROWS);
    if (!clip || !_historyDb.length) return;
    var sliced = new Array(_historyDb.length);
    for (var i = 0; i < _historyDb.length; i++) {
      var full = _historyDb[i];
      sliced[i] = (full && full.length)
        ? full.slice(clip.loIdx, clip.hiIdx + 1)
        : null;
    }
    SC.paintHistoryToCanvas(_wfCtx, _wfCanvas, sliced, WF_COLS, WF_ROWS,
                            _scale.minDb, _scale.maxDb);
  }

  // -- Activity column (waterfall left edge) -------------------------------
  function _resetActivity() {
    for (var i = 0; i < _activityRows.length; i++) _activityRows[i] = null;
    _lastIfaceRxb = null;
    _lastIfaceTxb = null;
    _pendingActivity = null;
    _renderActivityCol();
  }

  function _renderActivityCol() {
    if (!_activityCtx || !_activityCanvas) return;
    var w = _activityCanvas.width, h = _activityCanvas.height;
    _activityCtx.clearRect(0, 0, w, h);
    for (var i = 0; i < _activityRows.length && i < h; i++) {
      var v = _activityRows[i];
      if (v === 'tx') _activityCtx.fillStyle = 'rgba(240, 173, 78, 0.95)';
      else if (v === 'rx') _activityCtx.fillStyle = 'rgba(88, 166, 255, 0.85)';
      else continue;
      _activityCtx.fillRect(0, i, w, 1);
    }
  }

  function _ingestActivityRow(mark) {
    for (var i = _activityRows.length - 1; i > 0; i--) {
      _activityRows[i] = _activityRows[i - 1];
    }
    _activityRows[0] = mark || null;
  }

  // Watches the RNodeInterface byte counters across WS ticks.  Positive
  // txb delta → 'tx'; else positive rxb delta → 'rx'.  The value is
  // attributed to the next painted waterfall row.
  //
  // `_pendingActivity` is sticky across ticks that see no delta: ticks
  // come ~2 s apart but sweeps can be slower, so a TX that happens in a
  // no-sweep tick would otherwise be wiped by the next idle tick before
  // _ingestNewSweeps consumes it.  The consumer is responsible for
  // clearing.  Priority: TX > RX > null.
  function _observeIfaceTraffic(data) {
    var ifaces = data.interfaces || [];
    var rxb = null, txb = null;
    for (var i = 0; i < ifaces.length; i++) {
      if (ifaces[i].type !== 'RNodeInterface') continue;
      rxb = ifaces[i].rxb;
      txb = ifaces[i].txb;
      break;
    }
    if (rxb == null && txb == null) {
      // Iface gone — drop stale counters and any pending mark.
      _pendingActivity = null;
      _lastIfaceRxb = null;
      _lastIfaceTxb = null;
      return;
    }
    var dRx = (_lastIfaceRxb != null && rxb != null) ? (rxb - _lastIfaceRxb) : 0;
    var dTx = (_lastIfaceTxb != null && txb != null) ? (txb - _lastIfaceTxb) : 0;
    if (dRx < 0) dRx = 0;  // counter reset (iface restart)
    if (dTx < 0) dTx = 0;
    if (dTx > 0) _pendingActivity = 'tx';
    else if (dRx > 0 && _pendingActivity !== 'tx') _pendingActivity = 'rx';
    // else: leave _pendingActivity alone — may be 'tx' or 'rx' from an
    // earlier tick, awaiting the next sweep paint.
    _lastIfaceRxb = rxb;
    _lastIfaceTxb = txb;
  }

  function _onDragStart(ev) {
    if (!_lineEl || !_lastData) return;
    if (ev.button !== 0) return;  // left-click only
    var rect = _lineEl.getBoundingClientRect();
    var frac = (ev.clientX - rect.left) / rect.width;
    if (frac < 0 || frac > 1) return;
    _dragState = { startFrac: frac, curFrac: frac };
    ev.preventDefault();
    _renderLine(_lastData, _clip(_lastData.spectrum || {}, _zoom));
  }
  function _onDragMove(ev) {
    if (!_dragState || !_lineEl) return;
    var rect = _lineEl.getBoundingClientRect();
    var frac = (ev.clientX - rect.left) / rect.width;
    if (frac < 0) frac = 0; else if (frac > 1) frac = 1;
    _dragState.curFrac = frac;
    _renderLine(_lastData, _clip(_lastData.spectrum || {}, _zoom));
  }
  function _onDragEnd(ev) {
    if (!_dragState) return;
    var start = _dragState.startFrac, end = _dragState.curFrac;
    _dragState = null;
    if (Math.abs(end - start) < 0.01) {
      // Treated as a click, not a drag — just rerender to drop preview.
      if (_lastData) _renderLine(_lastData, _clip(_lastData.spectrum || {}, _zoom));
      return;
    }
    // Map drag fracs to MHz within the CURRENT clip (which may be zoomed).
    var spec = _lastData ? _lastData.spectrum : null;
    if (!spec) return;
    var curClip = _clip(spec, _zoom);
    if (!curClip) return;
    var span = curClip.hiMhz - curClip.loMhz;
    var a = Math.min(start, end), b = Math.max(start, end);
    var loMhz = curClip.loMhz + a * span;
    var hiMhz = curClip.loMhz + b * span;
    // Enforce a minimum span (2 bin widths) to avoid degenerate zoom.
    var minSpanMhz = (curClip.binStepKhz || 250) * 2 / 1000;
    if (hiMhz - loMhz < minSpanMhz) {
      if (_lastData) _renderLine(_lastData, curClip);
      return;
    }
    _setZoom([loMhz, hiMhz]);
  }

  function _onPresetClick(ev) {
    var btn = ev.currentTarget;
    var which = btn.getAttribute('data-preset');
    if (btn.disabled) return;
    if (which === 'full') { _setZoom(null); return; }
    if (which === 'rnode') {
      if (_ourFreqHz == null || _ourBwHz == null) return;
      var fMhz = _ourFreqHz / 1e6;
      var bwMhz = _ourBwHz / 1e6;
      var pad = 5 * bwMhz;
      _setZoom([
        Math.max(_regionInfo.lo, fMhz - pad),
        Math.min(_regionInfo.hi, fMhz + pad),
      ]);
      return;
    }
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
    if (_lastData) _renderAll(_lastData);
  }

  // -- Orchestration ------------------------------------------------------
  function _renderAll(data) {
    var spec = data.spectrum || {};
    var clip = _clip(spec, _zoom);
    // Handle slice-change (axis change) — bin indices, dB range, and history
    // all become stale under a new scanner config.
    var binSig = spec.freq_start_hz + '|' + spec.freq_stop_hz + '|' + (spec.bins_hz ? spec.bins_hz.length : 0);
    if (binSig !== _lastBinSig) {
      _lastBinSig = binSig;
      _lastSweepCount = 0;
      _scale.initialized = false;
      // Re-arm history backfill: previous rows used the old bin grid and
      // no longer align to the current axis.
      _historyState = 'pending';
      _fetchStartedAt = 0;
      // Drops zoom bounds (MHz against the old axis), history buffer,
      // activity rows, peak-hold, and wipes the waterfall canvas.
      _setZoom(null, /*silent=*/true);
      clip = _clip(spec, _zoom);
    }

    _renderMeta(data, clip);
    _renderOverlay(data, clip);
    _renderScale(data, clip);
    _renderZoomChip(clip);
    // Track iface rx/tx deltas every tick so _pendingActivity is set before
    // we paint sweep rows below.
    _observeIfaceTraffic(data);

    // Always re-render the line plot so zoom changes (drag, preset buttons,
    // double-click reset) take effect immediately — not only on the next
    // sweep arrival.  Cheap SVG update over ~20 nodes.
    if (clip) _renderLine(data, clip);

    // Ingest new sweep rows into the waterfall only when there's fresh data
    // so we don't duplicate history.  Auto-scale just ran in _renderLine, so
    // _scale is valid by the time history rows paint below.
    var sc = spec.sweep_count || 0;
    if (clip && sc > _lastRenderedSweep) {
      _lastRenderedSweep = sc;
      // Kick the one-shot history fetch on first real sweep.
      if (_historyState === 'pending') {
        _fetchHistory(clip, spec.bins_hz ? spec.bins_hz.length : 0);
      }
      // If the fetch is taking too long, give up waiting and start painting
      // live — otherwise sweeps older than the tail window get lost.
      if (_historyState === 'fetching'
          && Date.now() - _fetchStartedAt > _FETCH_ABANDON_MS) {
        _historyState = 'abandoned';
      }
      if (_historyState !== 'pending' && _historyState !== 'fetching') {
        _ingestNewSweeps(data, clip);
      }
    }
  }

  // -- Public entry point -------------------------------------------------
  function update(data) {
    if (!data) return;
    if (!_resolveDom()) return;
    if (!data.spectrum) return;   // nothing to show without the scanner

    // Reveal section + expand the first time any data arrives.
    if (_section.style.display === 'none') {
      _section.style.display = '';
      if (_body && _body.classList.contains('hidden')) {
        _body.classList.remove('hidden');
        _expanded = true;
        var chev = _toggle ? _toggle.querySelector('.chevron') : null;
        if (chev) chev.innerHTML = '&#9662;';
      }
    }

    _lastData = data;
    _detectRegion(data);
    _detectRNode(data);
    _renderAll(data);

    if (_countEl) {
      _countEl.textContent = _regionInfo.label;
    }
    if (R.markUpdated) R.markUpdated('lora-spectrum-section');
  }

  R.loraSpectrum = { update: update };
})();
