/* ReticulumPi Dashboard — FM/AM Radio tuner module
 *
 * Streams live audio from the fm_receiver plugin via HTTP chunked WAV,
 * decoded and played through the Web Audio API.  The WebSocket broadcast
 * provides tuner state (frequency, mode, signal level) for the UI.
 */
(function () {
  'use strict';
  var R = window.RPI;
  if (!R) return;
  var $ = R.$, esc = R.esc;

  // -- DOM handles (resolved on first data) ---------------------------------
  var _section, _body, _toggle;
  var _freqDisplay, _freqInput, _freqGoBtn, _bandLabel, _deadZoneEl;
  var _modeBar, _modeBtns;
  var _gainSlider, _gainAuto, _gainValue;
  var _squelchSlider, _squelchValue;
  var _volumeSlider, _volumeValue;
  var _playBtn;
  var _signalBar, _signalDb;
  var _vuCanvas, _vuCtx;
  var _fftCanvas, _fftCtx;
  var _presetsEl;
  var _statusBadge;
  var _feedbackEl;
  var _preemptBanner, _lockCb, _lockIcon;
  var _favBtn, _favSection, _favList;
  var _dialSvg, _bandSelector, _sparklineEl, _vuDbfs;
  var _recBtn, _recTimer, _recSection, _recList;

  // -- Favorites state -----------------------------------------------------
  var _favorites = [];
  var _favoritesLoaded = false;

  // -- Recording state ----------------------------------------------------
  var _isRecording = false;
  var _recStartTime = null;
  var _recTimerInterval = null;
  var _recordings = [];
  var _recordingsLoaded = false;

  // -- VU peak hold --------------------------------------------------------
  var _vuPeak = 0;

  // -- State ----------------------------------------------------------------
  var _expanded = false;
  var _resolved = false;
  var _lastData = null;
  var _presets = null;
  var _presetsBuilt = false;

  // -- Pending action tracking ----------------------------------------------
  var _pendingAction = null;
  var _pendingTimeout = null;
  var _feedbackTimer = null;

  // -- Audio state ----------------------------------------------------------
  var _audioCtx = null;
  var _analyser = null;
  var _gainNode = null;
  var _playing = false;
  var _abortCtrl = null;
  var _nextStartTime = 0;
  var _animFrame = null;
  var _outputRate = 32000;
  var _freqMin = 52;
  var _freqMax = 2200;

  // -- Render throttling ---------------------------------------------------
  var _frameCount = 0;
  var _vuGrad = null;
  var _fftGrad = null;
  var _vuData = null;
  var _fftData = null;

  // -- Audio reconnect -----------------------------------------------------
  var _reconnectAttempts = 0;
  var _reconnectTimer = null;
  var _MAX_RECONNECT = 10;

  // -- Band labels ----------------------------------------------------------
  function _bandFor(mhz) {
    if (mhz < 30) return 'HF';
    if (mhz < 300) return 'VHF';
    if (mhz < 1000) return 'UHF';
    if (mhz < 2000) return 'L-band';
    return 'S-band';
  }

  // -- DOM resolution -------------------------------------------------------
  function _resolveDom() {
    if (_resolved) return true;
    _section = $('radio-section');
    if (!_section) return false;
    _body = $('radio-body');
    _toggle = $('radio-toggle');
    _statusBadge = $('radio-status-badge');
    _freqDisplay = $('radio-freq-display');
    _freqInput = $('radio-freq-input');
    _freqGoBtn = $('radio-freq-go');
    _bandLabel = $('radio-band-label');
    _deadZoneEl = $('radio-dead-zone');
    _feedbackEl = $('radio-feedback');
    _modeBar = $('radio-mode-bar');
    _gainSlider = $('radio-gain');
    _gainAuto = $('radio-gain-auto');
    _gainValue = $('radio-gain-value');
    _squelchSlider = $('radio-squelch');
    _squelchValue = $('radio-squelch-value');
    _volumeSlider = $('radio-volume');
    _volumeValue = $('radio-volume-value');
    _playBtn = $('radio-play-btn');
    _signalBar = $('radio-signal-bar');
    _signalDb = $('radio-signal-db');
    _vuCanvas = $('radio-vu-canvas');
    _fftCanvas = $('radio-fft-canvas');
    _presetsEl = $('radio-presets');
    _preemptBanner = $('radio-preemption-banner');
    _lockCb = $('radio-lock-cb');
    _lockIcon = $('radio-lock-icon');
    _favBtn = $('radio-fav-btn');
    _favSection = $('radio-favorites');
    _favList = $('radio-favorites-list');
    _dialSvg = $('radio-dial-svg');
    _bandSelector = $('radio-band-selector');
    _sparklineEl = $('radio-signal-sparkline');
    _vuDbfs = $('radio-vu-dbfs');
    _recBtn = $('radio-rec-btn');
    _recTimer = $('radio-rec-timer');
    _recSection = $('radio-recordings');
    _recList = $('radio-recordings-list');
    if (_vuCanvas) _vuCtx = _vuCanvas.getContext('2d');
    if (_fftCanvas) _fftCtx = _fftCanvas.getContext('2d');

    _modeBtns = _modeBar ? _modeBar.querySelectorAll('.radio-mode-btn') : [];

    _bindEvents();
    _buildBandSelector();
    _restoreLocals();
    _resolved = true;
    _section.style.display = '';
    return true;
  }

  // -- Event binding --------------------------------------------------------
  function _bindEvents() {
    if (_toggle) _toggle.addEventListener('click', function () {
      _expanded = !_expanded;
      if (_body) _body.classList.toggle('hidden', !_expanded);
      var chev = _toggle.querySelector('.chevron');
      if (chev) chev.innerHTML = _expanded ? '&#9662;' : '&#9656;';
    });

    if (_playBtn) _playBtn.addEventListener('click', _onPlayStop);

    if (_freqGoBtn) _freqGoBtn.addEventListener('click', _onFreqGo);
    if (_freqInput) {
      _freqInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') _onFreqGo();
      });
      var _scrollTune = _debounce(function () { _onFreqGo(); }, 200);
      _freqInput.addEventListener('wheel', function (e) {
        e.preventDefault();
        var step = _lastData && _lastData.mode === 'wbfm' ? 0.1 : 0.025;
        var cur = parseFloat(_freqInput.value) || (_lastData ? _lastData.frequency_mhz : 95.5);
        var next = cur + (e.deltaY < 0 ? step : -step);
        next = Math.max(_freqMin, Math.min(_freqMax, Math.round(next * 1000) / 1000));
        _freqInput.value = next;
        _scrollTune();
      });
    }

    for (var i = 0; i < _modeBtns.length; i++) {
      _modeBtns[i].addEventListener('click', function () {
        var mode = this.getAttribute('data-mode');
        var btn = this;
        for (var j = 0; j < _modeBtns.length; j++) {
          _modeBtns[j].classList.remove('active');
          _modeBtns[j].classList.remove('pending');
        }
        btn.classList.add('active');
        btn.classList.add('pending');
        _setPending('tune');
        _sendWs({ action: 'radio_tune', frequency_mhz: _lastData ? _lastData.frequency_mhz : 95.5, mode: mode });
      });
    }

    if (_gainSlider) _gainSlider.addEventListener('input', _debounce(function () {
      if (_gainAuto && _gainAuto.checked) return;
      var v = parseFloat(_gainSlider.value);
      if (_gainValue) _gainValue.textContent = v.toFixed(1) + ' dB';
      _setPending('gain');
      _sendWs({ action: 'radio_gain', gain_db: v });
    }, 300));
    if (_gainAuto) _gainAuto.addEventListener('change', function () {
      if (_gainAuto.checked) {
        if (_gainValue) _gainValue.textContent = 'auto';
        _sendWs({ action: 'radio_gain', gain_db: null });
        if (_gainSlider) _gainSlider.disabled = true;
      } else {
        if (_gainSlider) _gainSlider.disabled = false;
        var v = parseFloat(_gainSlider.value);
        if (_gainValue) _gainValue.textContent = v.toFixed(1) + ' dB';
        _sendWs({ action: 'radio_gain', gain_db: v });
      }
      _setPending('gain');
    });

    if (_squelchSlider) _squelchSlider.addEventListener('input', _debounce(function () {
      var v = parseInt(_squelchSlider.value, 10);
      if (_squelchValue) _squelchValue.textContent = v;
      _setPending('squelch');
      _sendWs({ action: 'radio_squelch', level: v });
    }, 300));

    var _sendVolWs = _debounce(function () {
      var v = parseInt(_volumeSlider.value, 10);
      _sendWs({ action: 'radio_volume', volume: v / 100 });
    }, 300);
    if (_volumeSlider) _volumeSlider.addEventListener('input', function () {
      var v = parseInt(_volumeSlider.value, 10);
      if (_volumeValue) _volumeValue.textContent = v + '%';
      if (_gainNode) _gainNode.gain.value = v / 100;
      _sendVolWs();
    });

    var _displayScrollFreq = null;
    var _displayScrollTune = _debounce(function () {
      if (_displayScrollFreq !== null) {
        _setPending('tune');
        _sendWs({ action: 'radio_tune', frequency_mhz: _displayScrollFreq });
      }
    }, 200);
    if (_freqDisplay) _freqDisplay.addEventListener('wheel', function (e) {
      e.preventDefault();
      var step = _lastData && _lastData.mode === 'wbfm' ? 0.1 : 0.025;
      var cur = _displayScrollFreq !== null ? _displayScrollFreq : (_lastData ? _lastData.frequency_mhz : 95.5);
      var next = cur + (e.deltaY < 0 ? step : -step);
      next = Math.max(_freqMin, Math.min(_freqMax, Math.round(next * 1000) / 1000));
      _displayScrollFreq = next;
      if (_freqDisplay) _freqDisplay.textContent = next.toFixed(3);
      if (_bandLabel) _bandLabel.textContent = _bandFor(next);
      _displayScrollTune();
    });

    if (_lockCb) _lockCb.addEventListener('change', function () {
      var action = _lockCb.checked ? 'radio_lock' : 'radio_unlock';
      _sendWs({ action: action });
    });

    _bindDialClick();

    if (_favBtn) _favBtn.addEventListener('click', _onFavToggle);

    if (_recBtn) _recBtn.addEventListener('click', _onRecordToggle);

    if (_recList) _recList.addEventListener('click', function (e) {
      var del = e.target.closest('.radio-rec-delete');
      if (del && del.dataset.name) {
        if (del.dataset.confirm !== 'yes') {
          del.dataset.confirm = 'yes';
          del.textContent = 'Sure?';
          setTimeout(function () { del.dataset.confirm = ''; del.textContent = '×'; }, 3000);
          return;
        }
        del.dataset.confirm = '';
        del.textContent = '×';
        R.api('/api/radio/recordings/' + encodeURIComponent(del.dataset.name), { method: 'DELETE' }).then(function () {
          _showFeedback('Recording deleted');
          _loadRecordings(true);
        }).catch(function () {
          _showFeedback('Delete failed');
        });
        return;
      }
    });

    if (_favList) _favList.addEventListener('click', function (e) {
      var btn = e.target.closest('.radio-fav-item');
      var del = e.target.closest('.radio-fav-delete');
      if (del) {
        var id = del.dataset.id;
        if (id) _sendWs({ action: 'radio_remove_favorite', favorite_id: id });
        return;
      }
      if (btn && btn.dataset.id) {
        _sendWs({ action: 'radio_tune_favorite', favorite_id: btn.dataset.id });
      }
    });

  }

  // -- Helpers --------------------------------------------------------------
  function _sendWs(msg) {
    if (R.ws && R.ws.readyState === 1) {
      R.ws.send(JSON.stringify(msg));
    }
  }

  function _debounce(fn, ms) {
    var t;
    return function () {
      clearTimeout(t);
      t = setTimeout(fn, ms);
    };
  }

  function _setPending(action) {
    _pendingAction = action;
    clearTimeout(_pendingTimeout);
    _pendingTimeout = setTimeout(_clearPending, 5000);
  }

  function _clearPending() {
    _pendingAction = null;
    clearTimeout(_pendingTimeout);
    _pendingTimeout = null;
    if (_playBtn) _playBtn.classList.remove('pending');
    if (_freqGoBtn) _freqGoBtn.classList.remove('pending');
    if (_modeBtns) {
      for (var i = 0; i < _modeBtns.length; i++) {
        _modeBtns[i].classList.remove('pending');
      }
    }
  }

  function _showFeedback(text) {
    if (!_feedbackEl) return;
    _feedbackEl.textContent = text;
    _feedbackEl.classList.remove('hidden');
    clearTimeout(_feedbackTimer);
    _feedbackTimer = setTimeout(function () {
      _feedbackEl.classList.add('hidden');
    }, 4000);
  }

  // -- localStorage persistence ---------------------------------------------
  function _saveLocal(key, val) {
    try { localStorage.setItem('rpi_radio_' + key, JSON.stringify(val)); }
    catch (e) {}
  }
  function _loadLocal(key) {
    try { var v = localStorage.getItem('rpi_radio_' + key); return v !== null ? JSON.parse(v) : undefined; }
    catch (e) { return undefined; }
  }
  function _persistLocals(data) {
    if (!data) return;
    _saveLocal('freq', data.frequency_mhz);
    _saveLocal('volume', data.volume);
    _saveLocal('mode', data.mode);
    _saveLocal('gain', data.gain_db);
    _saveLocal('squelch', data.squelch_level);
  }
  function _restoreLocals() {
    var vol = _loadLocal('volume');
    if (typeof vol === 'number' && _volumeSlider) {
      _volumeSlider.value = Math.round(vol * 100);
      if (_volumeValue) _volumeValue.textContent = Math.round(vol * 100) + '%';
    }
    var freq = _loadLocal('freq');
    if (typeof freq === 'number') {
      if (_freqInput) _freqInput.value = freq.toFixed(3);
      if (_freqDisplay) _freqDisplay.textContent = freq.toFixed(3);
      if (_bandLabel) _bandLabel.textContent = _bandFor(freq);
    }
    var mode = _loadLocal('mode');
    if (typeof mode === 'string' && _modeBtns) {
      for (var i = 0; i < _modeBtns.length; i++) {
        _modeBtns[i].classList.toggle('active', _modeBtns[i].getAttribute('data-mode') === mode);
      }
    }
    var gain = _loadLocal('gain');
    if (_gainSlider) {
      if (gain !== null && gain !== undefined && typeof gain === 'number') {
        _gainSlider.value = gain;
        _gainSlider.disabled = false;
        if (_gainAuto) _gainAuto.checked = false;
        if (_gainValue) _gainValue.textContent = gain.toFixed(1) + ' dB';
      } else {
        _gainSlider.disabled = true;
        if (_gainAuto) _gainAuto.checked = true;
        if (_gainValue) _gainValue.textContent = 'auto';
      }
    }
    var squelch = _loadLocal('squelch');
    if (typeof squelch === 'number' && _squelchSlider) {
      _squelchSlider.value = squelch;
      if (_squelchValue) _squelchValue.textContent = squelch;
    }
  }

  function _revertTuneState() {
    if (!_lastData) return;
    if (_freqDisplay) _freqDisplay.textContent = (_lastData.frequency_mhz || 0).toFixed(3);
    if (_bandLabel) _bandLabel.textContent = _bandFor(_lastData.frequency_mhz || 0);
    if (_freqInput && document.activeElement !== _freqInput) {
      _freqInput.value = (_lastData.frequency_mhz || 0).toFixed(3);
    }
    if (_modeBtns) {
      for (var i = 0; i < _modeBtns.length; i++) {
        var btn = _modeBtns[i];
        btn.classList.toggle('active', btn.getAttribute('data-mode') === _lastData.mode);
      }
    }
  }

  // -- Favorites ------------------------------------------------------------
  function _onFavToggle() {
    if (!_lastData) return;
    var freq = _lastData.frequency_mhz;
    var existing = _findFavorite(freq);
    if (existing) {
      _sendWs({ action: 'radio_remove_favorite', favorite_id: existing.id });
    } else {
      var label = freq.toFixed(3) + ' MHz';
      _sendWs({
        action: 'radio_add_favorite',
        label: label,
        frequency_mhz: freq,
        mode: _lastData.mode || 'wbfm',
      });
    }
  }

  function _findFavorite(freqMhz) {
    for (var i = 0; i < _favorites.length; i++) {
      if (Math.abs(_favorites[i].frequency_mhz - freqMhz) < 0.001) return _favorites[i];
    }
    return null;
  }

  function _updateFavStar() {
    if (!_favBtn || !_lastData) return;
    var found = _findFavorite(_lastData.frequency_mhz);
    _favBtn.innerHTML = found ? '&#9733;' : '&#9734;';
    _favBtn.classList.toggle('radio-fav-active', !!found);
  }

  function _renderFavorites() {
    if (!_favList || !_favSection) return;
    if (_favorites.length === 0) {
      _favSection.style.display = 'none';
      return;
    }
    _favSection.style.display = '';
    var html = '';
    for (var i = 0; i < _favorites.length; i++) {
      var f = _favorites[i];
      html += '<div class="radio-fav-item" data-id="' + esc(f.id) + '">' +
              '<span class="radio-fav-label">' + esc(f.label || f.frequency_mhz.toFixed(3)) + '</span>' +
              '<span class="radio-fav-freq">' + f.frequency_mhz.toFixed(3) + '</span>' +
              '<button class="radio-fav-delete" data-id="' + esc(f.id) + '">&times;</button>' +
              '</div>';
    }
    _favList.innerHTML = html;
  }

  function _loadFavorites() {
    if (_favoritesLoaded) return;
    _favoritesLoaded = true;
    R.api('/api/radio/favorites').then(function (r) {
      if (r && r.ok && Array.isArray(r.data)) {
        _favorites = r.data;
        _renderFavorites();
        _updateFavStar();
      }
    });
  }

  // -- Recording ------------------------------------------------------------
  function _onRecordToggle() {
    if (_isRecording) {
      _sendWs({ action: 'radio_record_stop' });
    } else {
      _sendWs({ action: 'radio_record_start' });
    }
  }

  function _updateRecBtn(recording) {
    if (!_recBtn) return;
    _isRecording = recording;
    if (recording) {
      _recBtn.innerHTML = '&#9679; Rec';
      _recBtn.classList.add('recording');
      _recBtn.disabled = false;
      _startRecTimer();
    } else {
      _recBtn.innerHTML = '&#9679; Rec';
      _recBtn.classList.remove('recording');
      _stopRecTimer();
    }
  }

  function _updateRecBtnEnabled(isPlaying) {
    if (!_recBtn) return;
    _recBtn.disabled = !isPlaying && !_isRecording;
  }

  function _startRecTimer() {
    if (_recTimerInterval) return;
    _recStartTime = _recStartTime || Date.now();
    if (_recTimer) _recTimer.classList.remove('hidden');
    _recTimerInterval = setInterval(function () {
      if (!_recTimer) return;
      var elapsed = Math.floor((Date.now() - _recStartTime) / 1000);
      var m = Math.floor(elapsed / 60);
      var s = elapsed % 60;
      _recTimer.textContent = (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
    }, 500);
  }

  function _stopRecTimer() {
    if (_recTimerInterval) { clearInterval(_recTimerInterval); _recTimerInterval = null; }
    _recStartTime = null;
    if (_recTimer) { _recTimer.classList.add('hidden'); _recTimer.textContent = '00:00'; }
  }

  function _renderRecordings() {
    if (!_recList || !_recSection) return;
    if (_recordings.length === 0) {
      _recSection.style.display = 'none';
      return;
    }
    _recSection.style.display = '';
    var html = '';
    for (var i = _recordings.length - 1; i >= 0; i--) {
      var r = _recordings[i];
      var dur = r.duration_seconds || 0;
      var m = Math.floor(dur / 60);
      var s = Math.round(dur % 60);
      var durStr = m + ':' + (s < 10 ? '0' : '') + s;
      var sizeStr = r.size_bytes > 1048576
        ? (r.size_bytes / 1048576).toFixed(1) + ' MB'
        : Math.round(r.size_bytes / 1024) + ' KB';
      html += '<div class="radio-rec-item">' +
        '<span class="radio-rec-item-info">' + esc(r.filename) +
        '<span class="radio-rec-meta">' + durStr + ' &middot; ' + sizeStr + '</span></span>' +
        '<a class="radio-rec-download" href="/api/radio/recordings/' +
        encodeURIComponent(r.filename) + '">&#8681;</a>' +
        '<button class="radio-rec-delete" data-name="' + esc(r.filename) +
        '">&times;</button></div>';
    }
    _recList.innerHTML = html;
  }

  function _loadRecordings(force) {
    if (_recordingsLoaded && !force) return;
    _recordingsLoaded = true;
    R.api('/api/radio/recordings').then(function (r) {
      if (r && r.ok && Array.isArray(r.data)) {
        _recordings = r.data;
        _renderRecordings();
      }
    });
  }

  // -- Band selector (4C) ---------------------------------------------------
  var _BANDS = [
    { label: 'FM', lo: 87.5, hi: 108.0, mode: 'wbfm', def: 95.5 },
    { label: 'Air', lo: 108.0, hi: 137.0, mode: 'am', def: 121.5 },
    { label: 'Marine', lo: 156.0, hi: 162.025, mode: 'fm', def: 156.8 },
    { label: 'WX', lo: 162.4, hi: 162.55, mode: 'fm', def: 162.4 },
    { label: '2m', lo: 144.0, hi: 148.0, mode: 'fm', def: 146.52 },
    { label: '70cm', lo: 420.0, hi: 450.0, mode: 'fm', def: 446.0 },
    { label: 'GMRS', lo: 462.5, hi: 467.7, mode: 'fm', def: 462.5625 },
    { label: 'ISM 433', lo: 433.05, hi: 434.79, mode: 'fm', def: 433.92 },
    { label: 'ISM 915', lo: 902.0, hi: 928.0, mode: 'fm', def: 915.0 },
  ];

  function _buildBandSelector() {
    if (!_bandSelector) return;
    var html = '';
    for (var i = 0; i < _BANDS.length; i++) {
      var b = _BANDS[i];
      html += '<button class="radio-band-btn" data-idx="' + i + '">' + esc(b.label) + '</button>';
    }
    _bandSelector.innerHTML = html;
    _bandSelector.addEventListener('click', function (e) {
      var btn = e.target.closest('.radio-band-btn');
      if (!btn) return;
      var b = _BANDS[parseInt(btn.dataset.idx, 10)];
      if (b) {
        _setPending('tune');
        _sendWs({ action: 'radio_tune', frequency_mhz: b.def, mode: b.mode });
      }
    });
  }

  function _updateBandSelector(freqMhz) {
    if (!_bandSelector) return;
    var btns = _bandSelector.querySelectorAll('.radio-band-btn');
    for (var i = 0; i < btns.length; i++) {
      var b = _BANDS[i];
      btns[i].classList.toggle('active', freqMhz >= b.lo && freqMhz <= b.hi);
    }
  }

  // -- Tuner dial (4D) ----------------------------------------------------
  var _dialBand = null;

  function _updateDial(freqMhz) {
    if (!_dialSvg) return;
    var band = null;
    for (var i = 0; i < _BANDS.length; i++) {
      if (freqMhz >= _BANDS[i].lo && freqMhz <= _BANDS[i].hi) { band = _BANDS[i]; break; }
    }
    if (!band) {
      band = { lo: Math.max(_freqMin, freqMhz - 10), hi: Math.min(_freqMax, freqMhz + 10) };
    }

    var w = 400, h = 28;
    var range = band.hi - band.lo || 1;
    var pos = ((freqMhz - band.lo) / range) * w;
    pos = Math.max(0, Math.min(w, pos));

    var tickCount = Math.min(10, Math.max(3, Math.floor(range / 0.5)));
    var tickStep = range / tickCount;
    var svg = '<rect x="0" y="0" width="' + w + '" height="' + h + '" fill="#0a0e1a" rx="3"/>';

    for (var t = 0; t <= tickCount; t++) {
      var tf = band.lo + t * tickStep;
      var tx = (t / tickCount) * w;
      svg += '<line x1="' + tx + '" y1="0" x2="' + tx + '" y2="8" stroke="#2a3a5c" stroke-width="1"/>';
      if (t % 2 === 0 || tickCount <= 5) {
        svg += '<text x="' + tx + '" y="18" fill="#6a7a9c" font-size="7" text-anchor="middle">' +
               tf.toFixed(1) + '</text>';
      }
    }

    svg += '<line x1="' + pos + '" y1="0" x2="' + pos + '" y2="' + h +
           '" stroke="#00e5ff" stroke-width="2"/>';
    svg += '<polygon points="' + (pos - 3) + ',0 ' + (pos + 3) + ',0 ' + pos + ',5" fill="#00e5ff"/>';

    _dialSvg.innerHTML = svg;
    _dialBand = band;
  }

  function _bindDialClick() {
    if (!_dialSvg) return;
    _dialSvg.addEventListener('click', function (e) {
      if (!_dialBand) return;
      var rect = _dialSvg.getBoundingClientRect();
      var x = e.clientX - rect.left;
      var frac = x / rect.width;
      var freq = _dialBand.lo + frac * (_dialBand.hi - _dialBand.lo);
      freq = Math.max(_freqMin, Math.min(_freqMax, Math.round(freq * 1000) / 1000));
      _setPending('tune');
      _sendWs({ action: 'radio_tune', frequency_mhz: freq });
    });
  }

  // -- Signal sparkline (4A) -----------------------------------------------
  function _renderSparkline(history) {
    if (!_sparklineEl || !history || history.length < 2) {
      if (_sparklineEl) _sparklineEl.innerHTML = '';
      return;
    }
    if (R.renderMetricSparkline) {
      R.renderMetricSparkline(_sparklineEl.id, history);
      return;
    }
    var w = 160, h = 24;
    var max = 1;
    for (var i = 0; i < history.length; i++) {
      if (history[i] > max) max = history[i];
    }
    var pts = '';
    var step = w / (history.length - 1);
    for (var j = 0; j < history.length; j++) {
      var x = j * step;
      var y = h - (history[j] / max) * (h - 2);
      pts += x.toFixed(1) + ',' + y.toFixed(1) + ' ';
    }
    _sparklineEl.innerHTML = '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '">' +
      '<polyline points="' + pts + '" fill="none" stroke="#00e5ff" stroke-width="1.2" stroke-opacity="0.7"/>' +
      '</svg>';
  }

  // -- Actions --------------------------------------------------------------
  function _onPlayStop() {
    if (_playing) {
      _stopAudio();
      if (_playBtn) {
        _playBtn.innerHTML = 'Stopping…';
        _playBtn.classList.add('pending');
      }
      _setPending('stop');
      _sendWs({ action: 'radio_stop' });
    } else {
      if (_playBtn) {
        _playBtn.innerHTML = 'Starting…';
        _playBtn.classList.add('pending');
      }
      _setPending('play');
      _sendWs({ action: 'radio_play' });
    }
  }

  function _onFreqGo() {
    var val = parseFloat(_freqInput.value);
    if (isNaN(val)) return;
    val = Math.max(_freqMin, Math.min(_freqMax, val));
    if (_freqDisplay) _freqDisplay.textContent = val.toFixed(3);
    if (_bandLabel) _bandLabel.textContent = _bandFor(val);
    if (_freqGoBtn) _freqGoBtn.classList.add('pending');
    _setPending('tune');
    var mode = null;
    if (_lastData) mode = _lastData.mode;
    _sendWs({ action: 'radio_tune', frequency_mhz: val, mode: mode });
  }

  // -- Audio streaming via Web Audio API ------------------------------------
  function _startAudio() {
    var savedAttempts = _reconnectAttempts;
    if (_audioCtx) _stopAudio();
    _reconnectAttempts = savedAttempts;
    _playing = true;

    try {
      _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    } catch (e) {
      _playing = false;
      return;
    }
    if (_audioCtx.state === 'suspended') {
      _audioCtx.resume().catch(function () {
        _showFeedback('Browser blocked audio — click Play again');
        _stopAudio();
      });
    }

    _analyser = _audioCtx.createAnalyser();
    _analyser.fftSize = 256;
    _gainNode = _audioCtx.createGain();
    _gainNode.gain.value = _volumeSlider ? parseInt(_volumeSlider.value, 10) / 100 : 0.75;
    _analyser.connect(_gainNode);
    _gainNode.connect(_audioCtx.destination);

    _nextStartTime = _audioCtx.currentTime + 0.5;
    _abortCtrl = new AbortController();

    var token = '';
    var cookies = document.cookie.split(';');
    for (var i = 0; i < cookies.length; i++) {
      var c = cookies[i].trim();
      if (c.indexOf('session=') === 0) { token = c.substring(8); break; }
    }

    fetch('/api/radio/audio', {
      credentials: 'same-origin',
      signal: _abortCtrl.signal,
      headers: token ? { 'Authorization': 'Bearer ' + token } : {},
    }).then(function (resp) {
      if (!resp.ok || !resp.body) { _stopAudio(); return; }
      var reader = resp.body.getReader();
      var headerSkipped = false;
      var leftover = new Uint8Array(0);

      function pump() {
        reader.read().then(function (result) {
          if (result.done || !_playing) return;
          var incoming = result.value;

          _reconnectAttempts = 0;
          if (_playBtn) _playBtn.classList.remove('reconnecting');

          if (!headerSkipped) {
            incoming = incoming.slice(44);
            headerSkipped = true;
            var rate = _lastData ? _lastData.output_rate_hz : 32000;
            _outputRate = rate;
          }

          var combined;
          if (leftover.length > 0) {
            combined = new Uint8Array(leftover.length + incoming.length);
            combined.set(leftover);
            combined.set(incoming, leftover.length);
          } else {
            combined = incoming;
          }

          var usable = combined.length - (combined.length % 2);
          if (usable > 0) {
            _decodeAndQueue(combined.slice(0, usable));
          }
          leftover = usable < combined.length ? combined.slice(usable) : new Uint8Array(0);

          pump();
        }).catch(function () { _scheduleReconnect(); });
      }
      pump();
    }).catch(function () { _scheduleReconnect(); });

    _animFrame = requestAnimationFrame(_renderLoop);
    _updatePlayBtn(true);
  }

  function _scheduleReconnect() {
    if (!_playing || _reconnectAttempts >= _MAX_RECONNECT) return;
    _reconnectAttempts++;
    var delay = Math.min(30000, 1000 * Math.pow(2, _reconnectAttempts));
    _showFeedback('Reconnecting audio (' + _reconnectAttempts + '/' + _MAX_RECONNECT + ')…');
    if (_playBtn) _playBtn.classList.add('reconnecting');
    clearTimeout(_reconnectTimer);
    _reconnectTimer = setTimeout(function () {
      if (_playing) _startAudio();
    }, delay);
  }

  function _stopAudio() {
    _playing = false;
    clearTimeout(_reconnectTimer);
    _reconnectTimer = null;
    _reconnectAttempts = 0;
    if (_abortCtrl) { _abortCtrl.abort(); _abortCtrl = null; }
    if (_audioCtx) {
      try { _audioCtx.close(); } catch (e) {}
      _audioCtx = null;
      _analyser = null;
      _gainNode = null;
    }
    if (_animFrame) { cancelAnimationFrame(_animFrame); _animFrame = null; }
    _vuData = null; _fftData = null; _vuGrad = null; _fftGrad = null;
    if (_playBtn) _playBtn.classList.remove('reconnecting');
    _updatePlayBtn(false);
  }

  function _decodeAndQueue(uint8data) {
    if (!_audioCtx || _audioCtx.state === 'closed') return;
    var sampleCount = uint8data.length / 2;
    if (sampleCount < 1) return;

    var buf;
    try {
      buf = _audioCtx.createBuffer(1, sampleCount, _outputRate);
    } catch (e) { return; }
    var ch = buf.getChannelData(0);
    var view = new DataView(uint8data.buffer, uint8data.byteOffset, uint8data.byteLength);
    for (var i = 0; i < sampleCount; i++) {
      ch[i] = view.getInt16(i * 2, true) / 32768.0;
    }

    var src = _audioCtx.createBufferSource();
    src.buffer = buf;
    src.connect(_analyser);

    var now = _audioCtx.currentTime;
    if (_nextStartTime < now) _nextStartTime = now + 0.05;
    try { src.start(_nextStartTime); } catch (e) { return; }
    _nextStartTime += buf.duration;
  }

  // -- Visualizations (throttled to ~20fps) ----------------------------------
  var _tabHidden = false;
  document.addEventListener('visibilitychange', function () {
    _tabHidden = document.hidden;
    if (!_tabHidden && _playing && !_animFrame) {
      _animFrame = requestAnimationFrame(_renderLoop);
    }
  });

  function _renderLoop() {
    if (!_playing || _tabHidden) { _animFrame = null; return; }
    _frameCount++;
    if (_frameCount % 3 === 0 && _expanded) {
      _renderVu();
      _renderFft();
    }
    _animFrame = requestAnimationFrame(_renderLoop);
  }

  function _renderVu() {
    if (!_vuCtx || !_analyser) return;
    var w = _vuCanvas.width, h = _vuCanvas.height;
    if (!_vuData || _vuData.length !== _analyser.fftSize) {
      _vuData = new Uint8Array(_analyser.fftSize);
    }
    _analyser.getByteTimeDomainData(_vuData);
    var sum = 0;
    for (var i = 0; i < _vuData.length; i++) {
      var v = (_vuData[i] - 128) / 128;
      sum += v * v;
    }
    var rms = Math.sqrt(sum / _vuData.length);
    var level = Math.min(1, rms * 3);

    _vuCtx.fillStyle = '#0a0e1a';
    _vuCtx.fillRect(0, 0, w, h);
    var barW = Math.round(level * w);
    if (barW > 0) {
      if (!_vuGrad) {
        _vuGrad = _vuCtx.createLinearGradient(0, 0, w, 0);
        _vuGrad.addColorStop(0, '#00e5ff');
        _vuGrad.addColorStop(0.6, '#00ff88');
        _vuGrad.addColorStop(0.85, '#ffb627');
        _vuGrad.addColorStop(1, '#ff1744');
      }
      _vuCtx.fillStyle = _vuGrad;
      _vuCtx.fillRect(0, 2, barW, h - 4);
    }
    if (level > _vuPeak) _vuPeak = level;
    else _vuPeak *= 0.98;
    if (_vuPeak > 0.01) {
      var peakX = Math.round(_vuPeak * w);
      _vuCtx.fillStyle = '#ffffff';
      _vuCtx.fillRect(peakX - 1, 1, 2, h - 2);
    }
    if (_vuDbfs) {
      var dbfs = level > 0.001 ? (20 * Math.log10(level)).toFixed(0) : '--';
      _vuDbfs.textContent = dbfs !== '--' ? dbfs + ' dB' : '--';
    }
  }

  function _renderFft() {
    if (!_fftCtx || !_analyser) return;
    var w = _fftCanvas.width, h = _fftCanvas.height;
    var bins = _analyser.frequencyBinCount;
    if (!_fftData || _fftData.length !== bins) {
      _fftData = new Uint8Array(bins);
    }
    _analyser.getByteFrequencyData(_fftData);

    _fftCtx.fillStyle = '#0a0e1a';
    _fftCtx.fillRect(0, 0, w, h);

    var barW = Math.max(1, Math.floor(w / bins));
    if (!_fftGrad) {
      _fftGrad = _fftCtx.createLinearGradient(0, h, 0, 0);
      _fftGrad.addColorStop(0, '#00e5ff');
      _fftGrad.addColorStop(0.5, '#00ff88');
      _fftGrad.addColorStop(0.8, '#ffb627');
      _fftGrad.addColorStop(1, '#ff1744');
    }
    _fftCtx.fillStyle = _fftGrad;

    for (var i = 0; i < bins; i++) {
      var barH = Math.round((_fftData[i] / 255) * h);
      _fftCtx.fillRect(i * barW, h - barH, barW - 1, barH);
    }
  }

  // -- Signal meter ---------------------------------------------------------
  function _renderSignalMeter(db) {
    if (!_signalBar || !_signalDb) return;
    var norm = Math.max(0, Math.min(1, (db + 90) / 70));
    var pct = Math.round(norm * 100);
    _signalBar.style.width = pct + '%';
    if (norm < 0.3) _signalBar.style.background = '#00e5ff';
    else if (norm < 0.6) _signalBar.style.background = '#00ff88';
    else if (norm < 0.8) _signalBar.style.background = '#ffb627';
    else _signalBar.style.background = '#ff1744';
    _signalDb.textContent = db > -85 ? db.toFixed(1) + ' dB' : '-- dB';
  }

  // -- Play button ----------------------------------------------------------
  function _updatePlayBtn(isPlaying) {
    if (!_playBtn) return;
    _playBtn.classList.remove('pending');
    if (isPlaying) {
      _playBtn.innerHTML = '&#9632; Stop';
      _playBtn.classList.add('playing');
    } else {
      _playBtn.innerHTML = '&#9654; Play';
      _playBtn.classList.remove('playing');
    }
  }

  // -- Presets panel --------------------------------------------------------
  function _buildPresets(presetData) {
    if (!_presetsEl || _presetsBuilt) return;
    _presets = presetData;
    _presetsBuilt = true;

    var html = '<div class="radio-preset-tabs">';
    var names = Object.keys(presetData);
    for (var i = 0; i < names.length; i++) {
      var name = names[i];
      var p = presetData[name];
      html += '<div class="radio-preset-group">';
      html += '<div class="radio-preset-label">' + esc(p.label || name) + '</div>';
      var freqs = p.frequencies || [];
      for (var j = 0; j < freqs.length; j++) {
        var f = freqs[j];
        html += '<button class="radio-preset-btn" data-freq="' + esc(f.freq_mhz + '') +
                '" data-mode="' + esc(p.mode || 'fm') + '">' +
                esc(f.label || f.freq_mhz + '') + '</button>';
      }
      html += '</div>';
    }
    html += '</div>';
    _presetsEl.innerHTML = html;

    _presetsEl.addEventListener('click', function (e) {
      var btn = e.target.closest('.radio-preset-btn');
      if (!btn) return;
      var freq = parseFloat(btn.dataset.freq);
      var mode = btn.dataset.mode || null;
      if (!isNaN(freq)) {
        if (_freqDisplay) _freqDisplay.textContent = freq.toFixed(3);
        if (_bandLabel) _bandLabel.textContent = _bandFor(freq);
        _setPending('tune');
        _sendWs({ action: 'radio_tune', frequency_mhz: freq, mode: mode });
      }
    });
  }

  // -- WebSocket update handler ---------------------------------------------
  function update(data) {
    if (!data) return;
    if (!_resolveDom()) return;
    _lastData = data;

    if (data.freq_min_mhz) _freqMin = data.freq_min_mhz;
    if (data.freq_max_mhz) _freqMax = data.freq_max_mhz;
    if (_freqInput) {
      _freqInput.min = _freqMin;
      _freqInput.max = _freqMax;
    }

    if (_pendingAction !== 'tune') {
      if (_freqDisplay) _freqDisplay.textContent = (data.frequency_mhz || 0).toFixed(3);
      if (_bandLabel) _bandLabel.textContent = _bandFor(data.frequency_mhz || 0);
      if (_freqInput && document.activeElement !== _freqInput) {
        _freqInput.value = (data.frequency_mhz || 0).toFixed(3);
      }

      if (_deadZoneEl) {
        if (data.dead_zone_warning) {
          _deadZoneEl.textContent = data.dead_zone_warning + ' — try a different frequency';
          _deadZoneEl.title = 'Your tuner has a coverage gap in this range. Reception may be poor or absent.';
          _deadZoneEl.classList.remove('hidden');
        } else {
          _deadZoneEl.classList.add('hidden');
        }
      }

      if (_modeBtns) {
        for (var i = 0; i < _modeBtns.length; i++) {
          var btn = _modeBtns[i];
          btn.classList.toggle('active', btn.getAttribute('data-mode') === data.mode);
        }
      }
    }

    if (_pendingAction !== 'gain') {
      if (_gainSlider && document.activeElement !== _gainSlider) {
        if (data.gain_db !== null && data.gain_db !== undefined) {
          _gainSlider.value = data.gain_db;
          _gainSlider.disabled = false;
          if (_gainAuto) _gainAuto.checked = false;
          if (_gainValue) _gainValue.textContent = data.gain_db.toFixed(1) + ' dB';
        } else {
          _gainSlider.disabled = true;
          if (_gainAuto) _gainAuto.checked = true;
          if (_gainValue) _gainValue.textContent = 'auto';
        }
      }
    }

    if (_pendingAction !== 'squelch') {
      if (_squelchSlider && document.activeElement !== _squelchSlider) {
        _squelchSlider.value = data.squelch_level || 0;
        if (_squelchValue) _squelchValue.textContent = data.squelch_level || '0';
      }
    }

    if (_volumeSlider && document.activeElement !== _volumeSlider) {
      var vol = Math.round((data.volume || 0) * 100);
      _volumeSlider.value = vol;
      if (_volumeValue) _volumeValue.textContent = vol + '%';
    }

    _renderSignalMeter(data.signal_db || -90);
    _updateBandSelector(data.frequency_mhz || 0);
    _updateDial(data.frequency_mhz || 0);
    _renderSparkline(data.signal_history);
    if (data.output_rate_hz) _outputRate = data.output_rate_hz;

    if (_pendingAction !== 'play' && _pendingAction !== 'stop') {
      if (data.playing && !_playing) {
        _startAudio();
      } else if (!data.playing && _playing) {
        _stopAudio();
      }
      _updatePlayBtn(data.playing);
    }

    _updateRecBtnEnabled(data.playing);
    if (data.recording && data.recording.active) {
      if (!_isRecording) {
        _recStartTime = Date.now() - (data.recording.duration_seconds || 0) * 1000;
        _updateRecBtn(true);
      }
    } else if (_isRecording) {
      _updateRecBtn(false);
      _loadRecordings(true);
    }

    if (_statusBadge) {
      var st = data.status || 'stopped';
      var validSt = ['stopped','starting','playing','paused','error','restarting','unavailable'];
      if (validSt.indexOf(st) < 0) st = 'stopped';
      _statusBadge.textContent = st;
      _statusBadge.className = 'count radio-status-' + st;
    }

    if (_preemptBanner) {
      if (data.dongle_active === false && data.preempted_by) {
        var label = data.preempted_by_label || data.preempted_by;
        var remaining = '';
        if (data.preempted_until_ts) {
          var secs = Math.max(0, Math.round(data.preempted_until_ts - Date.now() / 1000));
          if (secs > 0) remaining = ' (' + Math.floor(secs / 60) + 'm ' + (secs % 60) + 's remaining)';
        }
        _preemptBanner.textContent = 'Paused — ' + label + remaining;
        _preemptBanner.style.display = '';
        _section.classList.add('radio-paused');
      } else {
        _preemptBanner.style.display = 'none';
        _section.classList.remove('radio-paused');
      }
    }

    if (_lockCb) _lockCb.checked = !!data.locked;
    if (_lockIcon) _lockIcon.style.display = data.locked ? '' : 'none';

    if (!_pendingAction) _persistLocals(data);
    _updateFavStar();

    if (!_presetsBuilt) {
      R.api('/api/radio/presets').then(function (r) {
        if (r && r.ok) _buildPresets(r.data);
        else _presetsBuilt = false;
      }).catch(function () { _presetsBuilt = false; });
    }
    _loadFavorites();
    _loadRecordings();
  }

  // -- WebSocket command response handler -----------------------------------
  function _onRadioResponse(msg) {
    if (msg.type === 'radio_error') {
      if (_pendingAction === 'play') {
        _updatePlayBtn(false);
      } else if (_pendingAction === 'stop') {
        _updatePlayBtn(_playing);
      } else if (_pendingAction === 'tune') {
        _revertTuneState();
      } else if (_pendingAction === 'gain' && _lastData) {
        if (_gainSlider) {
          if (_lastData.gain_db !== null && _lastData.gain_db !== undefined) {
            _gainSlider.value = _lastData.gain_db;
            if (_gainValue) _gainValue.textContent = _lastData.gain_db.toFixed(1) + ' dB';
          } else {
            if (_gainValue) _gainValue.textContent = 'auto';
          }
        }
      } else if (_pendingAction === 'squelch' && _lastData) {
        if (_squelchSlider) _squelchSlider.value = _lastData.squelch_level || 0;
        if (_squelchValue) _squelchValue.textContent = _lastData.squelch_level || '0';
      } else if (_pendingAction === 'volume' && _lastData) {
        var v = Math.round((_lastData.volume || 0) * 100);
        if (_volumeSlider) _volumeSlider.value = v;
        if (_volumeValue) _volumeValue.textContent = v + '%';
      }
      if (_playing && (_pendingAction === 'play')) _stopAudio();
      _clearPending();
      _showFeedback(msg.error || 'Action failed');
      return;
    }

    if (msg.type === 'radio_tuned') {
      _clearPending();
      if (msg.frequency_mhz !== undefined) {
        if (_freqDisplay) _freqDisplay.textContent = msg.frequency_mhz.toFixed(3);
        if (_bandLabel) _bandLabel.textContent = _bandFor(msg.frequency_mhz);
        if (_freqInput && document.activeElement !== _freqInput) {
          _freqInput.value = msg.frequency_mhz.toFixed(3);
        }
      }
      if (_freqDisplay) {
        _freqDisplay.classList.remove('radio-freq-flash');
        void _freqDisplay.offsetWidth;
        _freqDisplay.classList.add('radio-freq-flash');
      }
      if (_playing) {
        setTimeout(_startAudio, 400);
      }
      return;
    }

    if (msg.type === 'radio_play') {
      _clearPending();
      if (msg.status === 'starting') {
        _startAudio();
      } else {
        if (_playing) _stopAudio();
        if (msg.status === 'error' && msg.error) _showFeedback(msg.error);
        else if (msg.status === 'already_playing') _updatePlayBtn(true);
      }
      return;
    }

    if (msg.type === 'radio_stop') {
      _clearPending();
      _stopAudio();
      return;
    }

    if (msg.type === 'radio_gain' || msg.type === 'radio_squelch' || msg.type === 'radio_volume') {
      _clearPending();
    }

    if (msg.type === 'radio_lock' || msg.type === 'radio_unlock') {
      if (_lockCb) _lockCb.checked = !!msg.locked;
      if (_lockIcon) _lockIcon.style.display = msg.locked ? '' : 'none';
      if (msg.error) _showFeedback(msg.error);
    }

    if (msg.type === 'radio_record_started') {
      _recStartTime = Date.now();
      _updateRecBtn(true);
      _showFeedback('Recording started');
      return;
    }
    if (msg.type === 'radio_record_stopped') {
      _updateRecBtn(false);
      _loadRecordings(true);
      _showFeedback('Recording saved');
      return;
    }

    if (msg.type === 'radio_favorite_added') {
      _favorites.push(msg);
      _renderFavorites();
      _updateFavStar();
    }
    if (msg.type === 'radio_favorite_removed') {
      _favorites = _favorites.filter(function (f) { return f.id !== msg.id; });
      _renderFavorites();
      _updateFavStar();
    }
  }

  R.onRadioResponse = _onRadioResponse;
  R.updateRadio = update;
})();
