/* ReticulumPi Dashboard — LoRa Spectrum panel
 *
 * A project-aware zoom of the generic SDR sweep onto the LoRa ISM band.
 * Unlike the generic spectrum panel, this one overlays what matters for a
 * Reticulum node:
 *
 *   • Our RNode's configured RX channel (translucent rect).
 *   • A region-defined channel grid (250/500 kHz ticks).
 *   • Regulatory band edges (dashed lines).
 *   • Hover crosshair: frequency / dB / sweep age, plus context for whatever
 *     overlay (RNode TX window, off-band edge) is under cursor.
 *
 * Region selection priority: localStorage override (panel dropdown) >
 * server config (plugins.web_dashboard.lora_region in config.yaml) > US
 * default.  There is no auto-detection from live data.
 *
 * When a dedicated `lora_scanner` plugin is running, this panel uses its
 * high-resolution data directly.  Otherwise falls back to clipping the
 * wideband `spectrum_scanner` data.  Shared primitives (turbo colormap,
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
  // LoRa ISM bands.  Base, spacing (kHz), count define the channel grid
  // rendered as overlay ticks.  Only the six commonly observed regions;
  // unknown configured values fall back to US.
  var REGIONS = {
    US:     { lo: 902.0, hi: 928.0,   base: 902.8625, spacing: 250, count: 108, label: 'US 902–928' },
    EU_868: { lo: 863.0, hi: 870.0,   base: 864.125,  spacing: 250, count: 24,  label: 'EU 868' },
    EU_433: { lo: 433.0, hi: 434.7,   base: 433.175,  spacing: 250, count: 6,   label: 'EU 433' },
    CN:     { lo: 470.0, hi: 510.0,   base: 470.125,  spacing: 250, count: 160, label: 'CN 470–510' },
    JP:     { lo: 920.8, hi: 927.8,   base: 921.875,  spacing: 500, count: 15,  label: 'JP 920–928' },
    ANZ:    { lo: 915.0, hi: 928.0,   base: 916.375,  spacing: 250, count: 52,  label: 'ANZ 915–928' },
  };
  var REGION_ORDER = ['US', 'EU_868', 'EU_433', 'CN', 'JP', 'ANZ'];
  var LS_KEY = 'rpi_lora_region';

  // -- Constants ----------------------------------------------------------
  var WF_COLS = 800;
  var WF_ROWS = 256;

  // -- DOM handles (resolved lazily on first data tick) --------------------
  var _section = null, _body = null, _toggle = null, _countEl = null;
  var _metaEl, _lineEl, _wfCanvas, _wfCtx, _overlayEl, _hoverEl, _scaleEl, _bandsEl;
  var _placeholderEl = null;
  var _hasReceivedData = false;

  // -- Runtime state -------------------------------------------------------
  var _expanded = false;
  var _region = 'US', _regionInfo = REGIONS.US;
  // Priority: _userRegion (dropdown, localStorage) > _configRegion
  // (/api/config) > 'US' default.  Populated on init.
  var _userRegion = null;
  var _configRegion = null;
  var _regionSource = 'default';   // 'user' | 'config' | 'default'
  var _regionSelectEl = null;
  var _ourFreqHz = null, _ourBwHz = null, _ourSf = null, _ourCr = null;
  var _lastSweepCount = 0;
  var _lastRenderedSweep = 0;
  var _scale = { minDb: -90, maxDb: -30, initialized: false };
  // Waterfall history lives in spectrumCommon — either `historyStore`
  // (shared wideband) or `loraHistoryStore` (dedicated scanner).  The
  // `_store()` helper picks the right one based on `_dedicatedMode`.
  var _lastStoreGen = -1;
  var _needsBulkPaint = false;
  var _lastOverlaySig = '';    // "<region>|<freq>|<bw>|<zoom>" for overlay rebuild
  var _lastData = null;
  // Zoom: null = full clipped region; else [loMhz, hiMhz] user-selected window.
  var _zoom = null;
  var _dragState = null;       // {startFrac, curFrac, rectEl} during a drag
  var _dragRafId = null;
  var _zoomResetEl = null;     // "Reset zoom" chip
  var _presetBtns = {};        // {rnode, full} button handles
  var _dedicatedMode = false;
  function _store() { return _dedicatedMode ? SC.loraHistoryStore : SC.historyStore; }

  var _peakHoldEnabled = false;
  var _peakHoldDb = null;      // per-bin max dB, aligned with spec.bins_hz

  // -- Energy cluster detection state ----------------------------------------
  var _clusterEmaDb = null;
  var _clusterTracked = [];
  var _clusterIdCounter = 0;
  var _clusterEnabled = true;
  var _clusterVisibleSig = '';
  var CLUSTER_EMA_ALPHA = 0.15;
  var CLUSTER_LIFT_DB = 2;
  var CLUSTER_GAP = 2;
  var CLUSTER_MIN_BINS = 3;
  var CLUSTER_APPEAR = 3;
  var CLUSTER_DISAPPEAR = 5;

  // -- Channel grid state ---------------------------------------------------
  var _chGridWrap = null, _chGridUp = null, _chGridDn = null;
  var _chDetailEl = null;
  var _chCells = [];           // DOM elements for all channel cells
  var _chGridBuilt = false;
  var _selectedChannel = null;
  var _chTooltipEl = null;
  var _channelPowerHistory = null;

  // -- Stats panel state ----------------------------------------------------
  var _statsPanelEl = null, _nfTrendEl = null, _chUtilEl = null, _chTimelineEl = null;
  var _nfTrendSig = '', _chUtilSig = '', _chTimelineSig = '';

  // -- CSS custom property cache (read once in _resolveDom) -----------------
  var _css = {};

  // -- Waterfall scale controls ---------------------------------------------
  var _wfScaleManual = false;
  var _wfScaleMin = -100;
  var _wfScaleMax = -30;
  var _LS_WF_SCALE = 'rpi_lora_wf_scale';
  function _zoomKey() { return 'rpi_lora_zoom_' + _region; }

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
    _wfCtx.fillStyle = _css.wfBg || '#050810';
    _wfCtx.fillRect(0, 0, WF_COLS, WF_ROWS);
    _needsBulkPaint = true;
    _lastRenderedSweep = 0;
    return true;
  }

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

    if (_wfCanvas) {
      _wfCtx = _wfCanvas.getContext('2d');
      _resizeCanvas();
      _wfCanvas.addEventListener('mousemove', _onHover);
      _wfCanvas.addEventListener('mouseleave', _onHoverLeave);
      var wfParent = _wfCanvas.parentElement;
      if (wfParent && typeof ResizeObserver !== 'undefined') {
        new ResizeObserver(function () {
          if (!_resizeCanvas()) return;
          if (_lastData) {
            var clip = _clip(_lastData.spectrum || {}, _zoom);
            if (clip) _repaintWaterfallFromHistory(clip);
          }
        }).observe(wfParent);
      }
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

    // Channel grid + stats panel DOM handles
    _chGridWrap = $('lora-channel-grid-wrap');
    _chGridUp = $('lora-channel-grid-up');
    _chGridDn = $('lora-channel-grid-dn');
    _chDetailEl = $('lora-channel-detail');
    _statsPanelEl = $('lora-stats-panel');
    _nfTrendEl = $('lora-nf-trend');
    _chUtilEl = $('lora-ch-util');
    _chTimelineEl = $('lora-ch-timeline');

    // Cache CSS custom properties once for use in SVG renderers
    var cs = getComputedStyle(document.documentElement);
    _css.wfBg      = cs.getPropertyValue('--wf-bg').trim()         || '#050810';
    _css.grid      = cs.getPropertyValue('--spec-grid').trim()     || '#0f1525';
    _css.label     = cs.getPropertyValue('--spec-label').trim()    || '#3a4565';
    _css.sublabel  = cs.getPropertyValue('--spec-sublabel').trim() || '#5a6785';
    _css.statText  = cs.getPropertyValue('--spec-stat-text').trim()|| '#8a9ab5';
    _css.peakHold  = cs.getPropertyValue('--lora-peak-hold').trim()  || 'rgba(255,182,39,0.65)';
    _css.cwMarker  = cs.getPropertyValue('--lora-cw-marker').trim()  || 'rgba(220,53,69,0.8)';
    _css.noiseLine = cs.getPropertyValue('--lora-noise-line').trim() || 'rgba(240,160,64,0.5)';
    _css.cyan      = cs.getPropertyValue('--cyan').trim()            || '#00e5ff';

    // Restore toggle state from localStorage
    _loadWfScaleState();
    try {
      var cv = localStorage.getItem('rpi_lora_clusters');
      if (cv === '0') _clusterEnabled = false;
    } catch (e) {}

    if (_toggle) _toggle.addEventListener('click', _onToggleClick);

    // Show section immediately with a placeholder until data arrives.
    if (_section.style.display === 'none') {
      _section.style.display = '';
      _placeholderEl = document.createElement('div');
      _placeholderEl.className = 'lora-spectrum-placeholder';
      _placeholderEl.textContent = 'Waiting for spectrum data…';
      if (_body) _body.insertBefore(_placeholderEl, _body.firstChild);
    }

    // Region bootstrap: synchronous localStorage load + async config fetch.
    // `_resolveRegion()` runs twice — once now (with user-or-default) and
    // again when `/api/config` lands (which may promote 'default' → 'config').
    _loadUserRegion();
    _resolveRegion();

    // Restore zoom AFTER region is resolved so _zoomKey() returns the
    // correct region-scoped key.  Validate against region bounds.
    try {
      var zs = localStorage.getItem(_zoomKey());
      if (zs) {
        var zp = JSON.parse(zs);
        if (Array.isArray(zp) && zp.length === 2
            && typeof zp[0] === 'number' && typeof zp[1] === 'number'
            && zp[1] > zp[0]
            && zp[0] < _regionInfo.hi && zp[1] > _regionInfo.lo) {
          _zoom = zp;
        }
      }
    } catch (e) {}

    _fetchServerConfig();

    return true;
  }

  function _onToggleClick() {
    if (!_body) return;
    _expanded = _body.classList.contains('hidden');
    _body.classList.toggle('hidden');
    var chev = _toggle.querySelector('.chevron');
    if (chev) chev.innerHTML = _expanded ? '&#9662;' : '&#9656;';
    // Collapse/expand preserves state — the waterfall canvas, peak-hold,
    // zoom, and the shared history store all stay intact so reopening
    // resumes where the user left off.  Full-band / zoom-chip is one
    // click away if they want a fresh view.
    if (_expanded && _lastData) _renderAll(_lastData);
  }

  // -- Region selection ---------------------------------------------------
  // No auto-detection from live data — region comes from explicit sources
  // only.  Priority: user override (localStorage) > server config > US.
  function _loadUserRegion() {
    try {
      var v = window.localStorage ? window.localStorage.getItem(LS_KEY) : null;
      if (v && REGIONS[v]) { _userRegion = v; return; }
    } catch (e) { /* localStorage unavailable — fall through */ }
    _userRegion = null;
  }

  function _saveUserRegion(name) {
    try {
      if (!window.localStorage) return;
      if (name && REGIONS[name]) window.localStorage.setItem(LS_KEY, name);
      else window.localStorage.removeItem(LS_KEY);
    } catch (e) { /* quota or disabled — best-effort */ }
  }

  function _fetchServerConfig() {
    fetch('/api/config', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (payload) {
        var d = (payload && payload.data) ? payload.data : null;
        var cfg = d && d.plugins && d.plugins.web_dashboard;
        var cr = cfg && cfg.lora_region;
        _configRegion = (cr && REGIONS[cr]) ? cr : null;
        _resolveRegion();
      })
      .catch(function () { /* keep default */ });
  }

  // Recompute _region from current priority chain and re-render if a panel
  // is already live.
  function _resolveRegion() {
    var name, source;
    if (_userRegion && REGIONS[_userRegion]) {
      name = _userRegion; source = 'user';
    } else if (_configRegion && REGIONS[_configRegion]) {
      name = _configRegion; source = 'config';
    } else {
      name = 'US'; source = 'default';
    }
    var regionChanged = (name !== _region);
    var sourceChanged = (source !== _regionSource);
    _region = name;
    _regionInfo = REGIONS[name];
    _regionSource = source;
    if (!_lastData) return;   // nothing to re-render yet
    _metaStructSig = '';  // force meta rebuild so dropdown reflects state
    if (regionChanged) {
      // Region change is a SOFT reset: the bin grid is unchanged (rtl_power
      // still scans the same frequencies), we're just clipping to a
      // different slice.  Try to restore a saved zoom for the NEW region;
      // fall back to null (full band).  Peak-hold stays aligned (per-bin
      // index is unchanged).  The non-silent _setZoom path re-renders and
      // repaints the waterfall against the new clip.
      var savedZoom = null;
      try {
        var zs = localStorage.getItem(_zoomKey());
        if (zs) {
          var zp = JSON.parse(zs);
          if (Array.isArray(zp) && zp.length === 2
              && typeof zp[0] === 'number' && typeof zp[1] === 'number'
              && zp[1] > zp[0]
              && zp[0] < _regionInfo.hi && zp[1] > _regionInfo.lo) {
            savedZoom = zp;
          }
        }
      } catch (e) {}
      _setZoom(savedZoom, /*silent=*/false);
    } else if (sourceChanged) {
      _renderAll(_lastData);
    }
    if (_countEl) _countEl.textContent = _regionInfo.label;
  }

  function _onRegionSelectChange(ev) {
    var val = ev.target.value;
    if (val === '__auto__') {
      _userRegion = null;
      _saveUserRegion(null);
    } else if (REGIONS[val]) {
      _userRegion = val;
      _saveUserRegion(val);
    }
    _resolveRegion();
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
    // Zoom-RNode is only useful when the RNode's carrier sits inside the
    // currently-selected region — otherwise the computed window collapses
    // to an invalid range (lo > hi) and clipping returns null.
    var rnodeInRegion = hasRNode
      && (_ourFreqHz / 1e6) >= _regionInfo.lo
      && (_ourFreqHz / 1e6) <= _regionInfo.hi;
    var structSig = [
      _region,
      _regionSource,
      hasRNode ? '1' : '0',
      rnodeInRegion ? '1' : '0',
      _zoom ? '1' : '0',
      _peakHoldEnabled ? '1' : '0',
      _clusterEnabled ? '1' : '0',
    ].join('|');

    if (structSig !== _metaStructSig) {
      _metaStructSig = structSig;
      var optsHtml = '<option value="__auto__"'
        + (_regionSource !== 'user' ? ' selected' : '')
        + '>Auto (' + esc(_regionSource === 'config' ? 'config' : 'default')
        + ': ' + esc(REGIONS[_regionSource === 'config' && _configRegion ? _configRegion : 'US'].label)
        + ')</option>';
      for (var ri = 0; ri < REGION_ORDER.length; ri++) {
        var rn = REGION_ORDER[ri];
        optsHtml += '<option value="' + rn + '"'
          + (_regionSource === 'user' && _userRegion === rn ? ' selected' : '')
          + '>' + esc(REGIONS[rn].label) + '</option>';
      }
      _metaEl.innerHTML = ''
        + '<label class="lora-meta-region-picker" title="LoRa region — used to clip the SDR sweep and draw the channel grid.  Picking a region overrides the server-side default; choose Auto to fall back to it.">'
        +   '<span class="lora-meta-region-label">Region</span>'
        +   '<select class="lora-meta-region-select" data-role="region-select">'
        +     optsHtml
        +   '</select>'
        + '</label>'
        + '<span class="lora-zoom-presets">'
        +   '<button class="lora-zoom-preset" data-preset="rnode"'
        +     (rnodeInRegion ? '' : ' disabled')
        +     ' title="' + esc(
              hasRNode && !rnodeInRegion
                ? 'RNode carrier ' + (_ourFreqHz / 1e6).toFixed(3)
                  + ' MHz is outside ' + _regionInfo.label
                : 'Zoom to the RNode TX/RX window'
            ) + '">Zoom RNode</button>'
        +   '<button class="lora-zoom-preset" data-preset="full"'
        +     (_zoom ? '' : ' disabled') + '>Full band</button>'
        + '</span>'
        + '<button class="lora-peakhold-toggle' + (_peakHoldEnabled ? ' active' : '')
        +   '" title="Peak-hold trace (max per bin since enable)">Peak hold</button>'
        + '<button class="lora-cluster-toggle' + (_clusterEnabled ? ' active' : '')
        +   '" title="Auto-detect RF energy clusters">Clusters</button>'
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
      var clBtn = _metaEl.querySelector('.lora-cluster-toggle');
      if (clBtn) clBtn.addEventListener('click', _onClusterToggle);
      _regionSelectEl = _metaEl.querySelector('[data-role="region-select"]');
      if (_regionSelectEl) _regionSelectEl.addEventListener('change', _onRegionSelectChange);

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
    if (spec.sweep_count) {
      var sweepInfo = '#' + spec.sweep_count;
      if (spec.sweep_seconds > 0) sweepInfo += ' (' + (1 / spec.sweep_seconds).toFixed(1) + '/s)';
      parts.push(sweepInfo);
    }
    _scaleEl.innerHTML = parts.join(' · ');
  }

  // -- Reset-zoom chip -----------------------------------------------------
  function _renderZoomChip(clip) {
    if (!_zoomResetEl) return;
    _zoomResetEl.style.display = (_zoom && clip) ? '' : 'none';
  }

  // -- Energy cluster detection --------------------------------------------

  function _updateClusterEma(powers) {
    if (!_clusterEmaDb || _clusterEmaDb.length !== powers.length) {
      _clusterEmaDb = new Array(powers.length);
      for (var i = 0; i < powers.length; i++) _clusterEmaDb[i] = null;
    }
    var a = CLUSTER_EMA_ALPHA;
    for (var i = 0; i < powers.length; i++) {
      var v = powers[i];
      if (v == null || !isFinite(v)) continue;
      var prev = _clusterEmaDb[i];
      _clusterEmaDb[i] = (prev == null) ? v : a * v + (1 - a) * prev;
    }
  }

  function _detectClusters(spec, clip) {
    if (!_clusterEmaDb || !clip) return [];
    var bins = spec.bins_hz;
    if (!bins || !bins.length) return [];
    var lo = clip.loIdx, hi = clip.hiIdx;
    var n = hi - lo + 1;
    if (n < 5) return [];

    // Smoothed local baseline — wide moving average so individual energy
    // bands get averaged out, leaving only the broad spectral shape.
    var halfW = Math.max(8, Math.floor(n / 10));
    var baseline = new Array(n);
    for (var i = 0; i < n; i++) {
      var sum = 0, cnt = 0;
      var jLo = Math.max(lo, lo + i - halfW);
      var jHi = Math.min(hi, lo + i + halfW);
      for (var j = jLo; j <= jHi; j++) {
        var bv = _clusterEmaDb[j];
        if (bv != null && isFinite(bv)) { sum += bv; cnt++; }
      }
      baseline[i] = cnt > 0 ? sum / cnt : null;
    }

    // A bin is "hot" when it exceeds its local baseline by CLUSTER_LIFT_DB.
    var clusters = [];
    var runStart = -1, gap = 0, peakDb = -Infinity;
    var wSum = 0, wFreq = 0;
    for (var i = 0; i < n; i++) {
      var idx = lo + i;
      var v = _clusterEmaDb[idx];
      var bl = baseline[i];
      var hot = (v != null && isFinite(v) && bl != null && v > bl + CLUSTER_LIFT_DB);
      if (hot) {
        if (runStart < 0) runStart = idx;
        gap = 0;
        var lin = Math.pow(10, v / 10);
        wSum += lin;
        wFreq += lin * bins[idx];
        if (v > peakDb) peakDb = v;
      } else if (runStart >= 0) {
        gap++;
        if (gap > CLUSTER_GAP) {
          var runEnd = idx - gap;
          if (runEnd - runStart + 1 >= CLUSTER_MIN_BINS) {
            clusters.push({
              loMhz: bins[runStart] / 1e6,
              hiMhz: bins[runEnd] / 1e6,
              centerMhz: (wSum > 0) ? (wFreq / wSum) / 1e6 : bins[Math.floor((runStart + runEnd) / 2)] / 1e6,
              peakDb: peakDb
            });
          }
          runStart = -1; gap = 0; peakDb = -Infinity; wSum = 0; wFreq = 0;
        }
      }
    }
    if (runStart >= 0) {
      var runEnd = hi - gap;
      if (runEnd - runStart + 1 >= CLUSTER_MIN_BINS) {
        clusters.push({
          loMhz: bins[runStart] / 1e6,
          hiMhz: bins[runEnd] / 1e6,
          centerMhz: (wSum > 0) ? (wFreq / wSum) / 1e6 : bins[Math.floor((runStart + runEnd) / 2)] / 1e6,
          peakDb: peakDb
        });
      }
    }
    return clusters;
  }

  function _trackClusters(raw) {
    for (var ti = 0; ti < _clusterTracked.length; ti++) {
      _clusterTracked[ti]._matched = false;
    }
    for (var ri = 0; ri < raw.length; ri++) {
      var rc = raw[ri];
      var best = null, bestDist = Infinity;
      for (var ti = 0; ti < _clusterTracked.length; ti++) {
        var tc = _clusterTracked[ti];
        if (tc._matched) continue;
        if (rc.centerMhz >= tc.loMhz && rc.centerMhz <= tc.hiMhz) {
          var d = Math.abs(rc.centerMhz - tc.centerMhz);
          if (d < bestDist) { best = tc; bestDist = d; }
        }
      }
      if (best) {
        best.loMhz = rc.loMhz;
        best.hiMhz = rc.hiMhz;
        best.centerMhz = rc.centerMhz;
        best.peakDb = rc.peakDb;
        best.age++;
        best.missCount = 0;
        best._matched = true;
        if (!best.visible && best.age >= CLUSTER_APPEAR) best.visible = true;
      } else {
        _clusterTracked.push({
          id: _clusterIdCounter++,
          loMhz: rc.loMhz, hiMhz: rc.hiMhz,
          centerMhz: rc.centerMhz, peakDb: rc.peakDb,
          age: 1, missCount: 0, visible: false, _matched: true
        });
      }
    }
    for (var ti = _clusterTracked.length - 1; ti >= 0; ti--) {
      var tc = _clusterTracked[ti];
      if (!tc._matched) {
        tc.missCount++;
        if (tc.missCount >= CLUSTER_DISAPPEAR) {
          _clusterTracked.splice(ti, 1);
        }
      }
    }
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

    _updateClusterEma(powers);
    if (_clusterEnabled) {
      var prevSig = _clusterVisibleSig;
      var rawC = _detectClusters(spec, clip);
      _trackClusters(rawC);
      var sigParts = [];
      for (var ci = 0; ci < _clusterTracked.length; ci++) {
        if (_clusterTracked[ci].visible) sigParts.push(_clusterTracked[ci].centerMhz.toFixed(1));
      }
      _clusterVisibleSig = sigParts.join(',');
      if (_clusterVisibleSig !== prevSig) _lastOverlaySig = '';
    }

    var vbW = 800, vbH = 120;
    SC.clear(_lineEl);

    // dB grid
    var step = 10;
    var first = Math.ceil(_scale.minDb / step) * step;
    for (var db = first; db <= _scale.maxDb; db += step) {
      var gy = vbH - 1 - ((db - _scale.minDb) / (_scale.maxDb - _scale.minDb)) * (vbH - 2);
      _lineEl.appendChild(SC.svg('line', {
        x1: 0, y1: gy, x2: vbW, y2: gy, stroke: _css.grid, 'stroke-width': 0.5,
      }));
      _lineEl.appendChild(SC.svg('text', {
        x: 4, y: gy - 2, fill: _css.label, 'font-size': 9,
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
          stroke: _css.peakHold,
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
          fill: 'rgba(0, 229, 255, 0.55)',
          stroke: _css.cyan,
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
        fill: 'none', stroke: _css.cyan, 'stroke-width': 1.2,
        'vector-effect': 'non-scaling-stroke',
      }));
    }

    // Active channel markers from channel_analysis.
    var ca = spec.channel_analysis;
    if (ca && ca.channels) {
      var viewSpanMhz = clip.hiMhz - clip.loMhz;
      for (var ci = 0; ci < ca.channels.length; ci++) {
        var ch = ca.channels[ci];
        if (!ch.active) continue;
        var cmhz = ch.center_mhz;
        if (cmhz < clip.loMhz || cmhz > clip.hiMhz) continue;
        var cx = ((cmhz - clip.loMhz) / viewSpanMhz) * vbW;
        var markerColor = ch.dir === 'dn' ? '#00e5ff' : '#ff6b9d';
        // Small inverted triangle at top
        var tw = 4, th = 5;
        _lineEl.appendChild(SC.svg('polygon', {
          points: (cx - tw / 2).toFixed(1) + ',0 '
            + (cx + tw / 2).toFixed(1) + ',0 '
            + cx.toFixed(1) + ',' + th,
          fill: markerColor, opacity: '0.8',
        }));
        // Channel number label
        _lineEl.appendChild(SC.svg('text', {
          x: cx, y: th + 7, fill: markerColor, 'font-size': 7,
          'text-anchor': 'middle', opacity: '0.7',
        }, String(ch.idx)));
      }
    }

    // Interference markers on line plot.
    if (ca && ca.interference_flags) {
      for (var ii = 0; ii < ca.interference_flags.length; ii++) {
        var flag = ca.interference_flags[ii];
        if (flag.type === 'cw' && flag.freq_mhz != null) {
          var cfMhz = flag.freq_mhz;
          if (cfMhz >= clip.loMhz && cfMhz <= clip.hiMhz) {
            var cfx = ((cfMhz - clip.loMhz) / (clip.hiMhz - clip.loMhz)) * vbW;
            // Red dashed vertical line
            _lineEl.appendChild(SC.svg('line', {
              x1: cfx, y1: 0, x2: cfx, y2: vbH,
              stroke: 'rgba(220,53,69,0.5)', 'stroke-width': 0.8,
              'stroke-dasharray': '3,2',
            }));
            // Red diamond marker
            var dw = 3;
            var dcy = 14;
            _lineEl.appendChild(SC.svg('polygon', {
              points: cfx.toFixed(1) + ',' + (dcy - dw)
                + ' ' + (cfx + dw).toFixed(1) + ',' + dcy
                + ' ' + cfx.toFixed(1) + ',' + (dcy + dw)
                + ' ' + (cfx - dw).toFixed(1) + ',' + dcy,
              fill: _css.cwMarker,
            }));
            _lineEl.appendChild(SC.svg('text', {
              x: cfx, y: dcy + dw + 8, fill: '#dc3545', 'font-size': 7,
              'text-anchor': 'middle',
            }, 'CW'));
          }
        }
        if (flag.type === 'noise_elevated' && flag.current_db != null) {
          var nfLineY = vbH - 1 - ((flag.current_db - _scale.minDb) / span) * (vbH - 2);
          _lineEl.appendChild(SC.svg('line', {
            x1: 0, y1: nfLineY, x2: vbW, y2: nfLineY,
            stroke: _css.noiseLine, 'stroke-width': 0.8,
            'stroke-dasharray': '4,2',
          }));
          if (flag.baseline_db != null) {
            var blY = vbH - 1 - ((flag.baseline_db - _scale.minDb) / span) * (vbH - 2);
            _lineEl.appendChild(SC.svg('line', {
              x1: 0, y1: blY, x2: vbW, y2: blY,
              stroke: 'rgba(90,103,133,0.5)', 'stroke-width': 0.6,
              'stroke-dasharray': '2,3',
            }));
          }
        }
      }
    }

    // Selected channel highlight band on line plot
    if (_selectedChannel != null && ca && ca.channels) {
      var selCh = ca.channels[_selectedChannel];
      if (selCh) {
        var selLoMhz = selCh.center_mhz - (selCh.bw_khz / 2000);
        var selHiMhz = selCh.center_mhz + (selCh.bw_khz / 2000);
        if (selHiMhz > clip.loMhz && selLoMhz < clip.hiMhz) {
          var selX1 = Math.max(0, ((selLoMhz - clip.loMhz) / (clip.hiMhz - clip.loMhz)) * vbW);
          var selX2 = Math.min(vbW, ((selHiMhz - clip.loMhz) / (clip.hiMhz - clip.loMhz)) * vbW);
          _lineEl.appendChild(SC.svg('rect', {
            x: selX1, y: 0, width: selX2 - selX1, height: vbH,
            fill: 'rgba(0,229,255,0.1)', stroke: 'rgba(0,229,255,0.3)',
            'stroke-width': 0.6, 'stroke-dasharray': '3,2',
          }));
        }
      }
    }

    // MHz ticks
    var tickCount = 5;
    for (var t = 0; t <= tickCount; t++) {
      var frac = t / tickCount;
      var fx = frac * vbW;
      var fmhz = clip.loMhz + frac * (clip.hiMhz - clip.loMhz);
      _lineEl.appendChild(SC.svg('text', {
        x: fx, y: vbH - 2, fill: _css.label, 'font-size': 9,
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
        fill: 'rgba(0, 229, 255, 0.15)',
        stroke: '#00e5ff',
        'stroke-width': 1,
        'stroke-dasharray': '3,2',
      }));
    }
  }

  // -- Waterfall ingest ---------------------------------------------------
  // The shared store has already absorbed the new rows for us (app.js runs
  // `historyStore.ingestTick()` before dispatching to panels), so we just
  // paint the same tail rows into our canvas — sliced to the region clip.
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
      var sliced = row.slice(clip.loIdx, clip.hiIdx + 1);
      SC.paintRowToCanvas(_wfCtx, _wfCanvas, sliced, WF_COLS, WF_ROWS,
                          _scale.minDb, _scale.maxDb);
    }
    _lastSweepCount = sc;
  }

  // -- Overlays (RNode box, MT grid, reg edges, clusters) -----------------
  function _renderOverlay(data, clip) {
    if (!_overlayEl) return;
    var sig = [_region, _ourFreqHz, _ourBwHz,
               clip ? clip.loMhz.toFixed(3) : '', clip ? clip.hiMhz.toFixed(3) : '',
               _zoom ? 'Z' : 'F', _selectedChannel,
               _clusterEnabled ? _clusterVisibleSig : ''].join('|');
    if (sig === _lastOverlaySig) return;
    _lastOverlaySig = sig;

    SC.clear(_overlayEl);
    if (!clip) return;

    // Regulatory edges — dashed lines at band lo/hi if inside viewport.
    _drawEdge(clip, _regionInfo.lo, 'lora-reg-edge');
    _drawEdge(clip, _regionInfo.hi, 'lora-reg-edge');

    // Energy cluster overlays (rendered before RNode so RNode draws on top).
    if (_clusterEnabled) {
      for (var ci = 0; ci < _clusterTracked.length; ci++) {
        var cl = _clusterTracked[ci];
        if (!cl.visible) continue;
        if (cl.hiMhz < clip.loMhz || cl.loMhz > clip.hiMhz) continue;
        var cLo = Math.max(cl.loMhz, clip.loMhz);
        var cHi = Math.min(cl.hiMhz, clip.hiMhz);
        var cLeft = _xPct(cLo, clip);
        var cWidth = _xPct(cHi, clip) - cLeft;
        var cBox = document.createElement('div');
        cBox.className = 'lora-energy-cluster';
        cBox.style.left = cLeft.toFixed(3) + '%';
        cBox.style.width = Math.max(0.2, cWidth).toFixed(3) + '%';
        var cLbl = document.createElement('span');
        cLbl.className = 'lora-energy-cluster-label';
        cLbl.textContent = cl.centerMhz.toFixed(3) + ' MHz';
        cBox.appendChild(cLbl);
        _overlayEl.appendChild(cBox);
      }
    }

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
        if (_ourCr) lblTxt += ' · CR 4/' + _ourCr;
        var lblBox = document.createElement('span');
        lblBox.className = 'lora-rnode-box-label';
        lblBox.textContent = lblTxt;
        box.appendChild(lblBox);
        _overlayEl.appendChild(box);
      }
    }

    // Selected channel highlight on waterfall
    if (_selectedChannel != null) {
      var spec = data.spectrum || {};
      var ca = spec.channel_analysis;
      if (ca && ca.channels && ca.channels[_selectedChannel]) {
        var selCh = ca.channels[_selectedChannel];
        var sLoMhz = selCh.center_mhz - (selCh.bw_khz / 2000);
        var sHiMhz = selCh.center_mhz + (selCh.bw_khz / 2000);
        if (sHiMhz > clip.loMhz && sLoMhz < clip.hiMhz) {
          var sLeftPct = _xPct(Math.max(sLoMhz, clip.loMhz), clip);
          var sWidthPct = _xPct(Math.min(sHiMhz, clip.hiMhz), clip) - sLeftPct;
          var selBox = document.createElement('div');
          selBox.className = 'lora-selected-ch-box';
          selBox.style.left = sLeftPct.toFixed(3) + '%';
          selBox.style.width = Math.max(0.2, sWidthPct).toFixed(3) + '%';
          _overlayEl.appendChild(selBox);
        }
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

    // Row 0 = newest sweep; older rows below.  Prefer the server-supplied
    // per-row timestamp over rowIdx * sweep_seconds — the latter drifts on
    // wide spans whose sweeps take longer than sweep_seconds, and lies
    // outright across a scanner restart.  Display '—' when we hover over
    // a blank pixel below the filled region, or when the backend didn't
    // ship timestamps.
    var rowIdx = Math.floor(fracY * WF_ROWS);
    var rowTs = _store().rowTimestamps[rowIdx];
    var agoSec = (rowTs != null) ? (Date.now() / 1000 - rowTs) : null;
    var ageStr;
    if (agoSec == null) ageStr = '—';
    else if (rowIdx === 0 && agoSec < 2) ageStr = 'now';
    else ageStr = SC.formatAge(agoSec);

    var headerLine = freqMhz.toFixed(3) + ' MHz · ' + dbStr + ' · ' + ageStr;
    var bandLine = SC.loraBandLine(freqMhz, {
      rnode: { freqHz: _ourFreqHz, bwHz: _ourBwHz, sf: _ourSf, cr: _ourCr },
      region: _regionInfo,
      interferenceFlags: spec.channel_analysis && spec.channel_analysis.interference_flags,
      esc: esc
    });

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
    try { localStorage.setItem(_zoomKey(), range ? JSON.stringify(range) : ''); } catch (e) {}
    if (silent) {
      // Hard reset path — called on a generation bump (bin grid changed or
      // WS hello backfill arrived), so peak-hold (per-bin index) no longer
      // aligns to the new axis.  Dropping _lastRenderedSweep re-arms the
      // bulk paint branch on the next tick.
      _peakHoldDb = null;
      _clusterEmaDb = null;
      _clusterTracked = [];
      _clusterVisibleSig = '';
      _lastRenderedSweep = 0;
      if (_wfCtx) {
        _wfCtx.fillStyle = _css.wfBg || '#050810';
        _wfCtx.fillRect(0, 0, WF_COLS, WF_ROWS);
      }
      return;
    }
    // User-initiated zoom — bins/time don't change, so peak-hold and the
    // shared history store remain valid.  Re-render first so `_scale`
    // (EMA-smoothed) reflects the NEW visible window, then repaint the
    // waterfall from history against that fresh scale; otherwise the
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
  // Reads from the shared historyStore (`spectrumCommon.historyStore.rows`),
  // which is newest-first — convert to oldest-first for the painter.
  function _repaintWaterfallFromHistory(clip) {
    if (!_wfCtx) return;
    _wfCtx.fillStyle = _css.wfBg || '#050810';
    _wfCtx.fillRect(0, 0, WF_COLS, WF_ROWS);
    var rows = _store().rows;
    if (!clip || !rows || !rows.length) return;
    var sliced = new Array(rows.length);
    for (var i = 0; i < rows.length; i++) {
      var full = rows[rows.length - 1 - i];
      sliced[i] = (full && full.length)
        ? full.slice(clip.loIdx, clip.hiIdx + 1)
        : null;
    }
    SC.paintHistoryToCanvas(_wfCtx, _wfCanvas, sliced, WF_COLS, WF_ROWS,
                            _scale.minDb, _scale.maxDb);
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
    if (!_dragRafId) {
      _dragRafId = requestAnimationFrame(function () {
        _dragRafId = null;
        if (_dragState && _lastData) _renderLine(_lastData, _clip(_lastData.spectrum || {}, _zoom));
      });
    }
  }
  function _onDragEnd(ev) {
    if (!_dragState) return;
    if (_dragRafId) { cancelAnimationFrame(_dragRafId); _dragRafId = null; }
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
      // Bail if the RNode carrier sits outside the selected region — the
      // window collapses to lo > hi and renders blank.
      if (fMhz < _regionInfo.lo || fMhz > _regionInfo.hi) return;
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

  function _onClusterToggle() {
    _clusterEnabled = !_clusterEnabled;
    _lastOverlaySig = '';
    try { localStorage.setItem('rpi_lora_clusters', _clusterEnabled ? '1' : '0'); } catch (e) {}
    if (_lastData) _renderAll(_lastData);
  }

  function _loadWfScaleState() {
    try {
      var v = window.localStorage ? window.localStorage.getItem(_LS_WF_SCALE) : null;
      if (!v) return;
      var o = JSON.parse(v);
      _wfScaleManual = !!o.manual;
      if (typeof o.min === 'number') _wfScaleMin = o.min;
      if (typeof o.max === 'number') _wfScaleMax = o.max;
    } catch (e) { /* ignore */ }
  }
  function _saveWfScaleState() {
    try {
      if (!window.localStorage) return;
      window.localStorage.setItem(_LS_WF_SCALE, JSON.stringify({
        manual: _wfScaleManual, min: _wfScaleMin, max: _wfScaleMax,
      }));
    } catch (e) { /* ignore */ }
  }

  // -- Channel activity grid -------------------------------------------------
  function _buildChannelGrid(channels) {
    if (!_chGridUp || !_chGridDn || _chGridBuilt) return;
    _chCells = [];
    SC.clear(_chGridUp);
    SC.clear(_chGridDn);
    for (var i = 0; i < channels.length; i++) {
      var ch = channels[i];
      var cell = document.createElement('div');
      cell.className = 'lora-ch-cell lora-ch-idle';
      cell.textContent = String(ch.idx);
      cell.setAttribute('data-ch-idx', ch.idx);
      cell.addEventListener('click', _onChCellClick);
      cell.addEventListener('mouseenter', _onChCellEnter);
      cell.addEventListener('mouseleave', _onChCellLeave);
      if (ch.dir === 'dn') _chGridDn.appendChild(cell);
      else _chGridUp.appendChild(cell);
      _chCells.push(cell);
    }
    _chGridBuilt = true;
  }

  function _renderChannelGrid(channelAnalysis) {
    if (!channelAnalysis || !channelAnalysis.channels) {
      if (_chGridWrap) _chGridWrap.style.display = 'none';
      return;
    }
    var channels = channelAnalysis.channels;
    if (!_chGridBuilt) _buildChannelGrid(channels);
    if (_chGridWrap) _chGridWrap.style.display = '';

    var nowSec = Date.now() / 1000;
    for (var i = 0; i < channels.length && i < _chCells.length; i++) {
      var ch = channels[i];
      var cell = _chCells[i];
      var duty = ch.duty_pct || 0;
      var cls = 'lora-ch-cell';
      if (duty < 1) cls += ' lora-ch-idle';
      else if (duty < 10) cls += ' lora-ch-low';
      else if (duty < 25) cls += ' lora-ch-med';
      else cls += ' lora-ch-high';
      if (_selectedChannel === ch.idx) cls += ' selected';
      if (ch.last_active_at) {
        var age = nowSec - ch.last_active_at;
        if (age < 30) cls += ' lora-ch-recent';
      }
      cell.className = cls;
    }
  }

  function _onChCellClick(ev) {
    var idx = parseInt(ev.currentTarget.getAttribute('data-ch-idx'), 10);
    if (_selectedChannel === idx) {
      _selectedChannel = null;
      if (_chDetailEl) _chDetailEl.style.display = 'none';
    } else {
      _selectedChannel = idx;
      _showChannelDetail(idx);
      // Prefill chirp viewer frequency when a channel is selected
      if (R.chirpSpectrogram && R.chirpSpectrogram.setFreqMhz && _lastData && _lastData.spectrum) {
        var _ca = _lastData.spectrum.channel_analysis;
        if (_ca && _ca.channels && _ca.channels[idx]) {
          R.chirpSpectrogram.setFreqMhz(_ca.channels[idx].center_mhz);
        }
      }
    }
    if (_lastData) _renderAll(_lastData);
  }

  function _onChCellEnter(ev) {
    if (!_lastData || !_lastData.spectrum) return;
    var ca = _lastData.spectrum.channel_analysis;
    if (!ca || !ca.channels) return;
    var idx = parseInt(ev.currentTarget.getAttribute('data-ch-idx'), 10);
    var ch = ca.channels[idx];
    if (!ch) return;
    if (!_chTooltipEl) {
      _chTooltipEl = document.createElement('div');
      _chTooltipEl.className = 'lora-ch-tooltip';
      document.body.appendChild(_chTooltipEl);
    }
    var lines = [
      'Ch ' + ch.idx + ' · ' + ch.center_mhz.toFixed(3) + ' MHz · ' + ch.bw_khz + ' kHz ' + (ch.dir === 'dn' ? 'DN' : 'UP'),
      'Power: ' + (ch.power_db != null ? ch.power_db.toFixed(1) + ' dB' : '—'),
      'Avg: ' + (ch.avg_db != null ? ch.avg_db.toFixed(1) + ' dB' : '—')
        + ' · Peak: ' + (ch.peak_db != null ? ch.peak_db.toFixed(1) + ' dB' : '—'),
      'Duty: ' + ch.duty_pct.toFixed(1) + '% · Det: ' + ch.det_count,
    ];
    _chTooltipEl.innerHTML = lines.map(esc).join('<br>');
    var r = ev.currentTarget.getBoundingClientRect();
    _chTooltipEl.style.left = (r.right + 4) + 'px';
    _chTooltipEl.style.top = (r.top) + 'px';
    _chTooltipEl.style.display = 'block';
  }

  function _onChCellLeave() {
    if (_chTooltipEl) _chTooltipEl.style.display = 'none';
  }

  function _showChannelDetail(idx) {
    if (!_chDetailEl || !_lastData || !_lastData.spectrum) return;
    var ca = _lastData.spectrum.channel_analysis;
    if (!ca || !ca.channels || !ca.channels[idx]) return;
    var ch = ca.channels[idx];
    _chDetailEl.style.display = '';
    _chDetailEl.innerHTML = ''
      + '<span class="lora-channel-detail-close" data-action="close">&times;</span>'
      + '<div class="lora-channel-detail-header">'
      +   'Ch ' + esc(String(ch.idx)) + ' · '
      +   ch.center_mhz.toFixed(3) + ' MHz · '
      +   ch.bw_khz + ' kHz ' + (ch.dir === 'dn' ? 'Downlink' : 'Uplink')
      + '</div>'
      + 'Power: ' + (ch.power_db != null ? ch.power_db.toFixed(1) + ' dB' : '—')
      + ' · Avg: ' + (ch.avg_db != null ? ch.avg_db.toFixed(1) + ' dB' : '—')
      + ' · Peak: ' + (ch.peak_db != null ? ch.peak_db.toFixed(1) + ' dB' : '—')
      + '<br>Duty cycle: ' + ch.duty_pct.toFixed(1) + '%'
      + ' · Detections: ' + ch.det_count;
    var sc = (_lastData.spectrum && _lastData.spectrum.sweep_count) ? _lastData.spectrum.sweep_count : 0;
    if (sc > 0 && ch.det_count > 0) {
      _chDetailEl.innerHTML += ' (' + (ch.det_count / sc * 100).toFixed(1) + '% of sweeps)';
    }
    var closeBtn = _chDetailEl.querySelector('[data-action="close"]');
    if (closeBtn) closeBtn.addEventListener('click', function () {
      _selectedChannel = null;
      _chDetailEl.style.display = 'none';
      if (_lastData) _renderAll(_lastData);
    });
  }

  // -- Interference alerts in meta strip ------------------------------------
  function _renderAlerts(channelAnalysis) {
    var existing = _metaEl ? _metaEl.querySelector('.lora-alert-strip') : null;
    if (existing) existing.remove();
    if (!channelAnalysis || !_metaEl) return;
    var flags = channelAnalysis.interference_flags;
    if (!flags || !flags.length) return;
    var strip = document.createElement('span');
    strip.className = 'lora-alert-strip';
    for (var i = 0; i < flags.length; i++) {
      var f = flags[i];
      var badge = document.createElement('span');
      badge.className = 'lora-alert-badge ' + (f.type === 'cw' ? 'cw' : 'noise');
      var dot = document.createElement('span');
      dot.className = 'lora-alert-dot';
      badge.appendChild(dot);
      var txt;
      if (f.type === 'cw') {
        txt = 'CW @ ' + (f.freq_mhz != null ? f.freq_mhz.toFixed(3) : '?') + ' MHz';
      } else if (f.type === 'noise_elevated') {
        txt = 'Noise +' + (f.delta_db != null ? f.delta_db.toFixed(1) : '?') + ' dB';
      } else {
        txt = f.type;
      }
      badge.appendChild(document.createTextNode(txt));
      strip.appendChild(badge);
    }
    _metaEl.appendChild(strip);
  }

  // -- Stats panel: noise floor trend sparkline -----------------------------
  function _renderNfTrend(channelAnalysis) {
    if (!_nfTrendEl) return;
    if (!channelAnalysis || !channelAnalysis.noise_floor_trend
        || channelAnalysis.noise_floor_trend.length < 2) {
      _nfTrendSig = '';
      _nfTrendEl.innerHTML = '';
      return;
    }
    var trend = channelAnalysis.noise_floor_trend;
    var baseline = channelAnalysis.noise_baseline_db;
    var nfNow = channelAnalysis.noise_floor_db;
    var isElevated = false;
    var flags = channelAnalysis.interference_flags || [];
    for (var fi = 0; fi < flags.length; fi++) {
      if (flags[fi].type === 'noise_elevated') { isElevated = true; break; }
    }

    var sig = trend.length + '|' + trend[trend.length - 1].db + '|' + baseline + '|' + isElevated;
    if (sig === _nfTrendSig) return;
    _nfTrendSig = sig;

    var w = 380, h = 55, pad = 20;
    var vals = [];
    for (var i = 0; i < trend.length; i++) vals.push(trend[i].db);
    var mn = Infinity, mx = -Infinity;
    for (var j = 0; j < vals.length; j++) {
      if (vals[j] < mn) mn = vals[j];
      if (vals[j] > mx) mx = vals[j];
    }
    if (baseline != null) { mn = Math.min(mn, baseline); mx = Math.max(mx, baseline); }
    var span = mx - mn;
    if (span < 2) { mn -= 1; mx += 1; span = mx - mn; }

    var pts = [];
    for (var k = 0; k < vals.length; k++) {
      var x = pad + (k / (vals.length - 1)) * (w - pad * 2);
      var y = h - 5 - ((vals[k] - mn) / span) * (h - 12);
      pts.push(x.toFixed(1) + ',' + y.toFixed(1));
    }
    var lineColor = isElevated ? '#ffc107' : _css.cyan;
    var fillColor = isElevated ? 'rgba(255,193,7,0.08)' : 'rgba(0,229,255,0.08)';

    var svg = '<svg viewBox="0 0 ' + w + ' ' + h + '" style="width:100%;height:' + h + 'px">';
    // Fill area
    var fillPts = pts.join(' ')
      + ' ' + (pad + (w - pad * 2)).toFixed(1) + ',' + (h - 5)
      + ' ' + pad.toFixed(1) + ',' + (h - 5);
    svg += '<polygon points="' + fillPts + '" fill="' + fillColor + '"/>';
    // Baseline dashed line
    if (baseline != null) {
      var by = h - 5 - ((baseline - mn) / span) * (h - 12);
      svg += '<line x1="' + pad + '" y1="' + by.toFixed(1)
        + '" x2="' + (w - pad) + '" y2="' + by.toFixed(1)
        + '" stroke="' + _css.sublabel + '" stroke-dasharray="3,2" stroke-width="0.8"/>';
      svg += '<text x="' + (w - pad + 2) + '" y="' + (by + 3).toFixed(1)
        + '" fill="' + _css.sublabel + '" font-size="7">baseline</text>';
    }
    // Line
    svg += '<polyline points="' + pts.join(' ')
      + '" fill="none" stroke="' + lineColor + '" stroke-width="1.2"/>';
    // Labels
    svg += '<text x="' + pad + '" y="' + (h - 0) + '" fill="' + _css.label + '" font-size="7">older</text>';
    svg += '<text x="' + (w - pad) + '" y="' + (h - 0) + '" fill="' + _css.label + '" font-size="7" text-anchor="end">now</text>';
    if (nfNow != null) {
      svg += '<text x="' + (w / 2) + '" y="8" fill="' + _css.statText + '" font-size="8" text-anchor="middle">Noise floor: '
        + nfNow.toFixed(1) + ' dB</text>';
    }
    svg += '</svg>';

    _nfTrendEl.innerHTML = '<div class="lora-stats-label">Noise Floor Trend</div>' + svg;
  }

  // -- Stats panel: channel utilization bar chart ---------------------------
  function _renderChUtil(channelAnalysis) {
    if (!_chUtilEl) return;
    if (!channelAnalysis || !channelAnalysis.channels) {
      _chUtilSig = '';
      _chUtilEl.innerHTML = '';
      return;
    }
    var channels = channelAnalysis.channels;
    var dutySum = 0;
    for (var di = 0; di < channels.length; di++) dutySum += Math.round((channels[di].duty_pct || 0) * 10);
    var sig = channels.length + '|' + dutySum;
    if (sig === _chUtilSig) return;
    _chUtilSig = sig;

    var w = 380, barH = 2.5;
    var h = channels.length * barH + 20;
    var pad = 25;

    var svg = '<svg viewBox="0 0 ' + w + ' ' + h + '" style="width:100%;height:' + Math.round(h) + 'px">';
    svg += '<text x="' + (w / 2) + '" y="8" fill="' + _css.statText + '" font-size="8" text-anchor="middle">Channel Duty Cycle %</text>';
    for (var i = 0; i < channels.length; i++) {
      var ch = channels[i];
      var duty = ch.duty_pct || 0;
      var y = 14 + i * barH;
      var barW = Math.max(0, (duty / 100) * (w - pad - 10));
      var color;
      if (duty < 1) color = '#1a1f35';
      else if (duty < 10) color = 'rgba(40, 167, 69, 0.6)';
      else if (duty < 25) color = 'rgba(255, 193, 7, 0.6)';
      else color = 'rgba(220, 53, 69, 0.7)';
      svg += '<rect x="' + pad + '" y="' + y.toFixed(1) + '" width="' + barW.toFixed(1)
        + '" height="' + (barH - 0.5).toFixed(1) + '" fill="' + color + '" rx="0.5"/>';
      if (i % 8 === 0) {
        svg += '<text x="' + (pad - 2) + '" y="' + (y + barH).toFixed(1)
          + '" fill="' + _css.label + '" font-size="6" text-anchor="end">' + ch.idx + '</text>';
      }
    }
    svg += '</svg>';

    _chUtilEl.innerHTML = '<div class="lora-stats-label">Channel Utilization</div>' + svg;
  }

  // -- Stats panel: per-channel time series ---------------------------------
  function _renderChTimeline(idx) {
    if (!_chTimelineEl || !_lastData || !_lastData.spectrum) return;
    var ca = _lastData.spectrum.channel_analysis;
    if (!ca || !ca.channels || !ca.channels[idx]) {
      _chTimelineSig = '';
      _chTimelineEl.style.display = 'none';
      return;
    }
    var ch = ca.channels[idx];
    var hist = _lastData._channelPowerHistory;
    if (!hist || !hist[idx] || hist[idx].length < 2) {
      _chTimelineSig = '';
      _chTimelineEl.style.display = '';
      var count = (hist && hist[idx]) ? hist[idx].length : 0;
      _chTimelineEl.innerHTML = '<div class="lora-stats-label">Ch ' + ch.idx
        + ' Time Series</div><div style="color:' + _css.sublabel
        + ';font-size:0.7rem">Collecting data (' + count + '/2 samples)...</div>';
      return;
    }
    var entries = hist[idx];
    var lastEntry = entries[entries.length - 1];
    var sig = idx + '|' + entries.length + '|' + (lastEntry ? lastEntry[1] : '');
    if (sig === _chTimelineSig) return;
    _chTimelineSig = sig;
    var w = 780, h = 100, pad = 30;
    var mn = Infinity, mx = -Infinity;
    for (var i = 0; i < entries.length; i++) {
      var v = entries[i][1];
      if (v != null) { if (v < mn) mn = v; if (v > mx) mx = v; }
    }
    var nf = ca.noise_floor_db;
    if (nf != null) { mn = Math.min(mn, nf); mx = Math.max(mx, nf); }
    var span = mx - mn;
    if (span < 3) { mn -= 1.5; mx += 1.5; span = mx - mn; }

    var svg = '<svg viewBox="0 0 ' + w + ' ' + h + '" style="width:100%;height:' + h + 'px">';
    svg += '<text x="' + (w / 2) + '" y="10" fill="' + _css.statText + '" font-size="8" text-anchor="middle">'
      + 'Ch ' + ch.idx + ' · ' + ch.center_mhz.toFixed(3) + ' MHz · Duty ' + ch.duty_pct.toFixed(1) + '%</text>';
    // Noise floor reference
    if (nf != null) {
      var nfY = h - 8 - ((nf - mn) / span) * (h - 22);
      svg += '<line x1="' + pad + '" y1="' + nfY.toFixed(1) + '" x2="' + (w - 10)
        + '" y2="' + nfY.toFixed(1) + '" stroke="' + _css.sublabel + '" stroke-dasharray="3,2" stroke-width="0.7"/>';
    }
    // Power line
    var pts = [];
    for (var j = 0; j < entries.length; j++) {
      var pv = entries[j][1];
      if (pv == null) continue;
      var x = pad + (j / (entries.length - 1)) * (w - pad - 10);
      var y = h - 8 - ((pv - mn) / span) * (h - 22);
      pts.push(x.toFixed(1) + ',' + y.toFixed(1));
    }
    if (pts.length) {
      svg += '<polyline points="' + pts.join(' ')
        + '" fill="none" stroke="' + _css.cyan + '" stroke-width="1"/>';
    }
    svg += '</svg>';
    _chTimelineEl.style.display = '';
    _chTimelineEl.innerHTML = '<div class="lora-stats-label">Channel Time Series</div>' + svg;
  }

  // -- Render stats panel (calls sub-renderers) -----------------------------
  function _renderStatsPanel(data) {
    if (!_statsPanelEl) return;
    var spec = data.spectrum || {};
    var ca = spec.channel_analysis;
    if (!ca) {
      _statsPanelEl.style.display = 'none';
      return;
    }
    _statsPanelEl.style.display = '';
    _renderNfTrend(ca);
    _renderChUtil(ca);
    if (_selectedChannel != null) _renderChTimeline(_selectedChannel);
  }

  // -- Orchestration ------------------------------------------------------
  function _renderAll(data) {
    var spec = data.spectrum || {};
    var clip = _clip(spec, _zoom);

    // Shared historyStore generation cursor — bumps on WS-hello backfill
    // arrival and on bin-grid change.  Either event invalidates the per-bin
    // peak-hold and the current waterfall canvas, so drop them and re-arm
    // a bulk paint for the next sweep.  _setZoom(silent=true) does the
    // canvas wipe + peak-hold clear + _lastRenderedSweep reset.
    var gen = _store().generation;
    if (gen !== _lastStoreGen) {
      _lastStoreGen = gen;
      _lastSweepCount = 0;
      _scale.initialized = false;
      _needsBulkPaint = true;
      _setZoom(null, /*silent=*/true);
      clip = _clip(spec, _zoom);
    }

    _renderMeta(data, clip);
    _renderOverlay(data, clip);
    _renderScale(data, clip);
    _renderZoomChip(clip);

    var ca = spec.channel_analysis || null;
    _renderChannelGrid(ca);
    _renderAlerts(ca);
    _renderStatsPanel(data);

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
      if (_needsBulkPaint) {
        _repaintWaterfallFromHistory(clip);
        _needsBulkPaint = false;
        _lastSweepCount = _store().sweepCount;
      } else {
        _ingestNewSweeps(data, clip);
      }
    }
  }

  // -- Public entry point -------------------------------------------------
  function update(data) {
    if (!data) return;
    if (!_resolveDom()) return;

    // Prefer dedicated lora_scanner / lora_chirp_viewer data; fall back to wideband spectrum.
    var _loraSnap = data.lora_scanner || data.lora_chirp_viewer;
    if (_loraSnap && _loraSnap.bins_hz && _loraSnap.bins_hz.length) {
      _dedicatedMode = true;
      data = Object.assign({}, data, { spectrum: _loraSnap });
    } else {
      _dedicatedMode = false;
    }

    // Show/hide chirp viewer section when lora_chirp_viewer plugin is active
    if (data.lora_chirp_viewer && R.chirpSpectrogram) {
      R.chirpSpectrogram.show();
      R.chirpSpectrogram.handleUpdate(data);
    }
    if (!data.spectrum) return;

    // Show scanner status in placeholder when not yet producing sweeps
    var specStatus = data.spectrum.status;
    if (specStatus === 'unavailable' || specStatus === 'error') {
      if (_placeholderEl) {
        _placeholderEl.textContent = specStatus === 'unavailable'
          ? 'RTL-SDR scanner unavailable'
          : 'Scanner error: ' + (data.spectrum.error || 'unknown');
        _placeholderEl.style.display = '';
      }
      return;
    }

    // Expand + remove placeholder the first time real data arrives.
    if (!_hasReceivedData) {
      _hasReceivedData = true;
      if (_placeholderEl) { _placeholderEl.style.display = 'none'; }
      if (_body && _body.classList.contains('hidden')) {
        _body.classList.remove('hidden');
        _expanded = true;
        var chev = _toggle ? _toggle.querySelector('.chevron') : null;
        if (chev) chev.innerHTML = '&#9662;';
      }
    }

    // Carry channel power history from WS backfill for time-series drill-down
    if (_channelPowerHistory) {
      data._channelPowerHistory = _channelPowerHistory;
    }

    _lastData = data;
    _detectRNode(data);

    _renderAll(data);

    if (_countEl) {
      _countEl.textContent = _regionInfo.label;
    }
    if (R.markUpdated) R.markUpdated('lora-spectrum-section');
  }

  R.loraSpectrum = {
    update: update,
    loadChannelHistory: function (hist) {
      _channelPowerHistory = hist;
      var _CPH_MAX = 256;
      if (_channelPowerHistory) {
        for (var k in _channelPowerHistory) {
          if (_channelPowerHistory[k] && _channelPowerHistory[k].length > _CPH_MAX) {
            _channelPowerHistory[k] = _channelPowerHistory[k].slice(-_CPH_MAX);
          }
        }
      }
    },
  };
})();
