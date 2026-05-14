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
    if (_vuCanvas) _vuCtx = _vuCanvas.getContext('2d');
    if (_fftCanvas) _fftCtx = _fftCanvas.getContext('2d');

    _modeBtns = _modeBar ? _modeBar.querySelectorAll('.radio-mode-btn') : [];

    _bindEvents();
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
      _freqInput.addEventListener('wheel', function (e) {
        e.preventDefault();
        var step = _lastData && _lastData.mode === 'wbfm' ? 0.1 : 0.025;
        var cur = parseFloat(_freqInput.value) || (_lastData ? _lastData.frequency_mhz : 95.5);
        var next = cur + (e.deltaY < 0 ? step : -step);
        next = Math.max(_freqMin, Math.min(_freqMax, Math.round(next * 1000) / 1000));
        _freqInput.value = next;
        _onFreqGo();
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

    if (_volumeSlider) _volumeSlider.addEventListener('input', function () {
      var v = parseInt(_volumeSlider.value, 10);
      if (_volumeValue) _volumeValue.textContent = v + '%';
      if (_gainNode) _gainNode.gain.value = v / 100;
      _sendWs({ action: 'radio_volume', volume: v / 100 });
    });

    if (_freqDisplay) _freqDisplay.addEventListener('wheel', function (e) {
      e.preventDefault();
      var step = _lastData && _lastData.mode === 'wbfm' ? 0.1 : 0.025;
      var cur = _lastData ? _lastData.frequency_mhz : 95.5;
      var next = cur + (e.deltaY < 0 ? step : -step);
      next = Math.max(_freqMin, Math.min(_freqMax, Math.round(next * 1000) / 1000));
      if (_freqDisplay) _freqDisplay.textContent = next.toFixed(3);
      if (_bandLabel) _bandLabel.textContent = _bandFor(next);
      _setPending('tune');
      _sendWs({ action: 'radio_tune', frequency_mhz: next });
    });

    if (_lockCb) _lockCb.addEventListener('change', function () {
      var action = _lockCb.checked ? 'radio_lock' : 'radio_unlock';
      _sendWs({ action: action });
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
    if (_audioCtx) _stopAudio();
    _playing = true;

    try {
      _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    } catch (e) {
      _playing = false;
      return;
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
        }).catch(function () {});
      }
      pump();
    }).catch(function () {});

    _animFrame = requestAnimationFrame(_renderLoop);
    _updatePlayBtn(true);
  }

  function _stopAudio() {
    _playing = false;
    if (_abortCtrl) { _abortCtrl.abort(); _abortCtrl = null; }
    if (_audioCtx) {
      try { _audioCtx.close(); } catch (e) {}
      _audioCtx = null;
      _analyser = null;
      _gainNode = null;
    }
    if (_animFrame) { cancelAnimationFrame(_animFrame); _animFrame = null; }
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

  // -- Visualizations -------------------------------------------------------
  function _renderLoop() {
    if (!_playing) return;
    _renderVu();
    _renderFft();
    _animFrame = requestAnimationFrame(_renderLoop);
  }

  function _renderVu() {
    if (!_vuCtx || !_analyser) return;
    var w = _vuCanvas.width, h = _vuCanvas.height;
    var data = new Uint8Array(_analyser.fftSize);
    _analyser.getByteTimeDomainData(data);
    var sum = 0;
    for (var i = 0; i < data.length; i++) {
      var v = (data[i] - 128) / 128;
      sum += v * v;
    }
    var rms = Math.sqrt(sum / data.length);
    var level = Math.min(1, rms * 3);

    _vuCtx.fillStyle = '#0a0e1a';
    _vuCtx.fillRect(0, 0, w, h);
    var barW = Math.round(level * w);
    if (barW > 0) {
      var grad = _vuCtx.createLinearGradient(0, 0, w, 0);
      grad.addColorStop(0, '#00e5ff');
      grad.addColorStop(0.6, '#00ff88');
      grad.addColorStop(0.85, '#ffb627');
      grad.addColorStop(1, '#ff1744');
      _vuCtx.fillStyle = grad;
      _vuCtx.fillRect(0, 2, barW, h - 4);
    }
  }

  function _renderFft() {
    if (!_fftCtx || !_analyser) return;
    var w = _fftCanvas.width, h = _fftCanvas.height;
    var bins = _analyser.frequencyBinCount;
    var data = new Uint8Array(bins);
    _analyser.getByteFrequencyData(data);

    _fftCtx.fillStyle = '#0a0e1a';
    _fftCtx.fillRect(0, 0, w, h);

    var barW = Math.max(1, Math.floor(w / bins));
    var grad = _fftCtx.createLinearGradient(0, h, 0, 0);
    grad.addColorStop(0, '#00e5ff');
    grad.addColorStop(0.5, '#00ff88');
    grad.addColorStop(0.8, '#ffb627');
    grad.addColorStop(1, '#ff1744');
    _fftCtx.fillStyle = grad;

    for (var i = 0; i < bins; i++) {
      var barH = Math.round((data[i] / 255) * h);
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
          _deadZoneEl.textContent = data.dead_zone_warning;
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
    if (data.output_rate_hz) _outputRate = data.output_rate_hz;

    if (_pendingAction !== 'play' && _pendingAction !== 'stop') {
      if (data.playing && !_playing) {
        _startAudio();
      } else if (!data.playing && _playing) {
        _stopAudio();
      }
      _updatePlayBtn(data.playing);
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

    if (!_presetsBuilt) {
      R.api('/api/radio/presets').then(function (r) {
        if (r && r.ok) _buildPresets(r.data);
      });
    }
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
  }

  R.onRadioResponse = _onRadioResponse;
  R.updateRadio = update;
})();
