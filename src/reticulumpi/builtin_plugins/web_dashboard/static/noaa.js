/* ReticulumPi Dashboard -- NOAA APT weather satellite panel */
(function () {
  'use strict';
  var R = window.RPI;
  if (!R) return;
  var $ = R.$, esc = R.esc;
  var formatTimeAgo = R.formatTimeAgo, formatBytes = R.formatBytes;
  var markUpdated = R.markUpdated;

  var MAX_THUMBS = 12;
  var MAX_PASSES = 4;

  var _section, _toggle, _body, _statusEl;
  var _progressEl, _galleryEl, _passesEl, _statsEl;
  var _expanded = false;
  var _wired = false;

  function _resolveDom() {
    if (_section) return true;
    _section    = $('noaa-section');
    if (!_section) return false;
    _toggle     = $('noaa-toggle');
    _body       = $('noaa-body');
    _statusEl   = $('noaa-status');
    _progressEl = $('noaa-progress');
    _galleryEl  = $('noaa-gallery');
    _passesEl   = $('noaa-passes');
    _statsEl    = $('noaa-stats');
    return true;
  }

  function _wire() {
    if (_wired || !_toggle || !_body) return;
    _wired = true;
    _toggle.addEventListener('click', function () {
      _expanded = !_expanded;
      _body.classList.toggle('hidden', !_expanded);
      var chev = _toggle.querySelector('.chevron');
      if (chev) chev.textContent = _expanded ? '▾' : '▶';
    });
  }

  // -- Quality badge color class --
  function _qualityCls(q) {
    if (q === 'good') return 'status-ok';
    if (q === 'fair') return 'status-warn';
    return 'status-err';
  }

  // -- Format countdown from seconds --
  function _fmtCountdown(s) {
    if (s == null || s < 0) return '--';
    var h = Math.floor(s / 3600);
    var m = Math.floor((s % 3600) / 60);
    if (h > 0) return h + 'h ' + m + 'm';
    return m + 'm';
  }

  // -- Render current pass progress --
  function _renderProgress(pass) {
    if (!_progressEl) return;
    if (!pass) {
      _progressEl.style.display = 'none';
      return;
    }
    _progressEl.style.display = '';
    var pct = Math.max(0, Math.min(100, pass.progress_pct || 0));
    _progressEl.innerHTML =
      '<div class="noaa-progress-label">'
      + 'Recording ' + esc(pass.satellite || '?')
      + ' (' + (pass.max_el != null ? pass.max_el : '?') + '°)'
      + ' — ' + pct.toFixed(1) + '%'
      + '</div>'
      + '<div class="noaa-progress-track">'
      + '<div class="noaa-progress-fill" data-rpi-width="' + pct + '"></div>'
      + '</div>';
    R.applyCspDynamicStyles(_progressEl);
  }

  // -- Render image gallery --
  function _renderGallery(images) {
    if (!_galleryEl) return;
    if (!images || !images.length) {
      _galleryEl.innerHTML = '<div class="noaa-empty">No satellite images captured yet</div>';
      return;
    }
    var html = '';
    var count = Math.min(images.length, MAX_THUMBS);
    for (var i = 0; i < count; i++) {
      var img = images[i];
      var url = '/api/noaa/image/' + encodeURIComponent(img.filename);
      var date = img.captured_at ? formatTimeAgo(img.captured_at) : '--';
      var size = img.file_size_bytes != null ? formatBytes(img.file_size_bytes) : '';
      html += '<a class="noaa-thumb" href="' + esc(url) + '" target="_blank" rel="noopener">'
        + '<img src="' + esc(url) + '" alt="' + esc(img.satellite || 'NOAA') + '" loading="lazy" />'
        + '<div class="noaa-thumb-info">'
        + '<span class="noaa-thumb-sat">' + esc(img.satellite || '?') + '</span>'
        + '<span class="noaa-thumb-date">' + esc(date) + '</span>'
        + '</div>'
        + '<span class="noaa-quality count ' + _qualityCls(img.quality) + '">'
        + esc(img.quality || '?') + '</span>'
        + (size ? '<span class="noaa-thumb-size">' + esc(size) + '</span>' : '')
        + '</a>';
    }
    _galleryEl.innerHTML = html;
  }

  // -- Render upcoming passes --
  function _renderPasses(passes) {
    if (!_passesEl) return;
    if (!passes || !passes.length) {
      _passesEl.innerHTML = '<div class="noaa-empty">No upcoming passes</div>';
      return;
    }
    var html = '';
    var count = Math.min(passes.length, MAX_PASSES);
    for (var i = 0; i < count; i++) {
      var p = passes[i];
      var dur = p.duration_s != null ? Math.round(p.duration_s / 60) : '?';
      html += '<div class="noaa-pass-entry">'
        + esc(p.satellite || '?')
        + ' in <strong>' + _fmtCountdown(p.countdown_s) + '</strong>'
        + ' (' + (p.max_el != null ? p.max_el : '?') + '° el, ' + dur + 'min)'
        + '</div>';
    }
    _passesEl.innerHTML = html;
  }

  // -- Render stats --
  function _renderStats(stats) {
    if (!_statsEl) return;
    if (!stats) { _statsEl.textContent = ''; return; }
    var total = stats.total_captures != null ? stats.total_captures : 0;
    var decoded = stats.successful_decodes != null ? stats.successful_decodes : 0;
    _statsEl.textContent = total + ' capture' + (total !== 1 ? 's' : '')
      + ', ' + decoded + ' decoded';
  }

  // -- Public API --
  R.updateNoaa = function (d) {
    if (!d) return;
    if (!_resolveDom()) return;
    _wire();

    _section.style.display = '';
    markUpdated('noaa-section');

    // Status badge
    if (_statusEl) {
      _statusEl.textContent = d.status || 'idle';
      var s = d.status || 'idle';
      _statusEl.className = 'count '
        + (s === 'recording' ? 'status-warn'
          : s === 'idle' ? 'status-ok' : 'status-info');
    }

    _renderProgress(d.current_pass || null);
    _renderGallery(d.recent_images || []);
    _renderPasses(d.next_passes || []);
    _renderStats(d.stats || null);
  };
})();
