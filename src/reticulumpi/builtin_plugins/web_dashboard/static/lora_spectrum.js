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
  var WF_ROWS = 200;

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
  var _lastBinSig = '';        // "<start>|<stop>|<bincount>" for slice change detection
  var _lastOverlaySig = '';    // "<region>|<freq>|<bw>|<preset>" for overlay rebuild
  var _lastData = null;

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

    if (_wfCanvas) {
      _wfCanvas.width = WF_COLS;
      _wfCanvas.height = WF_ROWS;
      _wfCtx = _wfCanvas.getContext('2d');
      _wfCtx.fillStyle = '#0a0d17';
      _wfCtx.fillRect(0, 0, WF_COLS, WF_ROWS);
      _wfCanvas.addEventListener('mousemove', _onHover);
      _wfCanvas.addEventListener('mouseleave', _onHoverLeave);
    }

    if (_toggle) _toggle.addEventListener('click', _onToggleClick);
    return true;
  }

  function _onToggleClick() {
    if (!_body) return;
    _expanded = _body.classList.contains('hidden');
    _body.classList.toggle('hidden');
    var chev = _toggle.querySelector('.chevron');
    if (chev) chev.innerHTML = _expanded ? '&#9662;' : '&#9656;';
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
    _region = name;
    _regionInfo = REGIONS[name];
    _regionSource = source;

    // Preset → configured MT channel (region-agnostic fallback)
    _modemPreset = (md && md.modem_preset) ? md.modem_preset : null;
    _mtChannelIdx = (name in DEFAULT_CHANNEL) ? DEFAULT_CHANNEL[name] : 0;
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
  // Returns { loIdx, hiIdx, loMhz, hiMhz, binStepKhz } or null if no overlap.
  function _clip(spec) {
    var bins = spec.bins_hz;
    if (!bins || bins.length === 0) return null;
    var loHz = _regionInfo.lo * 1e6, hiHz = _regionInfo.hi * 1e6;
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
    };
  }

  function _xPct(mhz, clip) {
    var span = clip.hiMhz - clip.loMhz;
    if (span <= 0) return 0;
    return ((mhz - clip.loMhz) / span) * 100;
  }

  // -- Meta strip ----------------------------------------------------------
  function _renderMeta(data, clip) {
    if (!_metaEl) return;
    _metaEl.innerHTML = '<span class="lora-meta-region">'
      + esc(_regionInfo.label) + '</span>';
  }

  // -- Line plot ----------------------------------------------------------
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

    var vbW = 800, vbH = 120;
    var pts = [];
    for (var k = 0, idx = clip.loIdx; idx <= clip.hiIdx; idx++, k++) {
      var p = powers[idx];
      if (p == null || !isFinite(p)) continue;
      var x = (k * (vbW - 1)) / Math.max(1, n - 1);
      var y = vbH - 1 - ((p - _scale.minDb) / (_scale.maxDb - _scale.minDb)) * (vbH - 2);
      if (y < 0) y = 0; else if (y > vbH - 1) y = vbH - 1;
      pts.push(x.toFixed(1) + ',' + y.toFixed(1));
    }

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
    _lineEl.appendChild(SC.svg('polyline', {
      points: pts.join(' '),
      fill: 'none', stroke: '#58a6ff', 'stroke-width': 1.2,
      'vector-effect': 'non-scaling-stroke',
    }));

    // MHz ticks
    var tickCount = 5;
    for (var t = 0; t <= tickCount; t++) {
      var frac = t / tickCount;
      var fx = frac * vbW;
      var fmhz = clip.loMhz + frac * (clip.hiMhz - clip.loMhz);
      _lineEl.appendChild(SC.svg('text', {
        x: fx, y: vbH - 2, fill: '#4a5570', 'font-size': 9,
        'text-anchor': t === 0 ? 'start' : (t === tickCount ? 'end' : 'middle'),
      }, fmhz.toFixed(1)));
    }
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
    }
    _lastSweepCount = sc;
  }

  // -- Overlays (RNode box, MT grid, reg edges) ---------------------------
  function _renderOverlay(data, clip) {
    if (!_overlayEl) return;
    var sig = [_region, _ourFreqHz, _ourBwHz, _modemPreset, _mtChannelIdx,
               clip ? clip.loMhz.toFixed(3) : '', clip ? clip.hiMhz.toFixed(3) : ''].join('|');
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
  // sweep age — but tailored to the LoRa view's overlays: prefer RNode-box
  // membership over MT channel proximity over off-band warning, since
  // that's the order most relevant to a node operator.
  function _onHover(ev) {
    if (!_lastData || !_lastData.spectrum) return;
    var spec = _lastData.spectrum;
    var clip = _clip(spec);
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

    // 2. Near a Meshtastic channel? Tolerance scales with bin step so it
    //    feels clicky without overlapping adjacent ticks.
    if (!bandLine) {
      var base = _regionInfo.base, stepMhz = _regionInfo.spacing / 1000;
      var tol = Math.max(stepMhz / 2, (clip.hiMhz - clip.loMhz) * 0.005);
      var idx = Math.round((freqMhz - base) / stepMhz);
      if (idx >= 0 && idx < _regionInfo.count) {
        var cmhz = base + idx * stepMhz;
        if (Math.abs(cmhz - freqMhz) <= tol) {
          var dflt = (idx === _mtChannelIdx) ? ' · default LongFast' : '';
          bandLine = '<strong>Meshtastic ch ' + idx + '</strong> @ '
                   + cmhz.toFixed(3) + ' MHz' + dflt;
        }
      }
    }

    // 3. Off the regulatory band edges entirely?
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

  // -- Orchestration ------------------------------------------------------
  function _renderAll(data) {
    var spec = data.spectrum || {};
    var clip = _clip(spec);
    // Handle slice-change (axis change) — flush waterfall + history
    var binSig = spec.freq_start_hz + '|' + spec.freq_stop_hz + '|' + (spec.bins_hz ? spec.bins_hz.length : 0);
    if (binSig !== _lastBinSig) {
      _lastBinSig = binSig;
      _lastSweepCount = 0;
      _lastRenderedSweep = 0;
      _scale.initialized = false;
      _lastOverlaySig = '';
      if (_wfCtx) {
        _wfCtx.fillStyle = '#0a0d17';
        _wfCtx.fillRect(0, 0, WF_COLS, WF_ROWS);
      }
    }

    _renderMeta(data, clip);
    _renderOverlay(data, clip);

    // Only redraw sweep-dependent bits when we have fresh sweep data.
    var sc = spec.sweep_count || 0;
    if (clip && sc > _lastRenderedSweep) {
      _renderLine(data, clip);
      _ingestNewSweeps(data, clip);
      _lastRenderedSweep = sc;
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
