/* ReticulumPi Dashboard — Space Tracker module
 *
 * Three-band layout:
 *   Band 1 — Sky Dome (polar plot), Kp arc gauge, Launch hero (live T-minus)
 *   Band 2 — 12h pass swimlane timeline
 *   Band 3 — Leaflet map (+ day/night terminator, footprint circles)
 *            + collapsible launches/passes tables
 *
 * All SVG is hand-built via createElementNS; no chart libraries.
 * Heavy work (map, footprints, terminator) is gated on section expansion.
 */
(function () {
  'use strict';
  var R = window.RPI;
  if (!R) return;
  var $ = R.$, esc = R.esc;

  var SVGNS = 'http://www.w3.org/2000/svg';
  var DEG = Math.PI / 180;
  var R_EARTH_KM = 6371.0;
  var TIMELINE_S = 12 * 3600;
  var TIMELINE_LANES = 8;
  var COMPASS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];

  // -- DOM handles (resolved on first data arrival) ------------------------
  var _section = null, _body = null, _toggle = null, _countEl = null;
  var _satCountEl, _overheadEl, _overheadNamesEl, _groupsSubEl;
  var _kpEl, _kpBandEl, _kpTimeEl, _kpGaugeEl;
  var _nextLaunchEl, _nextLaunchWhenEl, _heroCountdownEl;
  var _nextPassEl, _nextPassWhenEl;
  var _launchTableEl, _passTableEl;
  var _skydomeEl, _timelineEl;

  // -- Map state -----------------------------------------------------------
  var _map = null;
  var _satLayer = null;
  var _footprintLayer = null;
  var _terminatorLayer = null;
  var _observerMarker = null;
  var _satMarkers = {};
  var _footprints = {};

  // -- Runtime state -------------------------------------------------------
  var _expanded = false;
  var _lastSpace = null;
  var _lastLaunch = null;
  var _heroTickerId = null;

  // -- SVG helpers ---------------------------------------------------------
  function _svg(tag, attrs, text) {
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
  function _clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
  }

  // -- DOM setup -----------------------------------------------------------
  function _resolveDom() {
    if (_section) return true;
    _section = $('space-section');
    if (!_section) return false;
    _body = $('space-body');
    _toggle = $('space-toggle');
    _countEl = $('space-count');
    _satCountEl = $('space-sat-count');
    _overheadEl = $('space-overhead');
    _overheadNamesEl = $('space-overhead-names');
    _groupsSubEl = $('space-groups-sub');
    _kpEl = $('space-kp');
    _kpBandEl = $('space-kp-band');
    _kpTimeEl = $('space-kp-time');
    _kpGaugeEl = $('space-kp-gauge');
    _nextLaunchEl = $('space-next-launch');
    _nextLaunchWhenEl = $('space-next-launch-when');
    _heroCountdownEl = $('space-hero-countdown');
    _nextPassEl = $('space-next-pass');
    _nextPassWhenEl = $('space-next-pass-when');
    _launchTableEl = $('space-launch-table');
    _passTableEl = $('space-pass-table');
    _skydomeEl = $('space-skydome');
    _timelineEl = $('space-timeline');
    if (_toggle) _toggle.addEventListener('click', _onToggleClick);
    return true;
  }

  function _onToggleClick() {
    _expanded = !_expanded;
    _body.classList.toggle('hidden', !_expanded);
    var chev = _toggle.querySelector('.chevron');
    if (chev) chev.innerHTML = _expanded ? '&#9662;' : '&#9656;';
    if (_expanded) {
      _initMapIfNeeded();
      setTimeout(function () {
        if (_map) _map.invalidateSize();
        if (_lastSpace) update(_lastSpace);
      }, 50);
    }
  }

  function _initMapIfNeeded() {
    if (_map) return;
    if (typeof L === 'undefined') return;
    var container = $('space-map-container');
    if (!container) return;
    _map = L.map(container, {
      zoomControl: true,
      attributionControl: true,
      worldCopyJump: true
    }).setView([20, 0], 2);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; OpenStreetMap',
      maxZoom: 6
    }).addTo(_map);
    _footprintLayer = L.layerGroup().addTo(_map);
    _terminatorLayer = L.layerGroup().addTo(_map);
    _satLayer = L.layerGroup().addTo(_map);
  }

  // -- Time formatting -----------------------------------------------------
  function _formatCountdown(iso) {
    if (!iso) return '';
    var target = Date.parse(iso);
    if (isNaN(target)) return iso;
    return _countdownFromEpoch(target / 1000);
  }

  function _countdownFromEpoch(epoch_s) {
    if (!epoch_s) return '';
    var delta = Math.floor(epoch_s - Date.now() / 1000);
    var sign = delta < 0 ? '+' : '-';
    delta = Math.abs(delta);
    var d = Math.floor(delta / 86400); delta -= d * 86400;
    var h = Math.floor(delta / 3600);  delta -= h * 3600;
    var m = Math.floor(delta / 60);
    if (d > 0) return 'T' + sign + d + 'd ' + h + 'h';
    if (h > 0) return 'T' + sign + h + 'h ' + m + 'm';
    return 'T' + sign + m + 'm';
  }

  // Hero variant: HH:MM:SS when close, Nd HHh MMm when distant
  function _countdownHero(iso) {
    if (!iso) return 'T--';
    var target = Date.parse(iso);
    if (isNaN(target)) return 'T--';
    var delta = Math.floor(target / 1000 - Date.now() / 1000);
    var sign = delta < 0 ? '+' : '-';
    delta = Math.abs(delta);
    var d = Math.floor(delta / 86400);
    var h = Math.floor((delta % 86400) / 3600);
    var m = Math.floor((delta % 3600) / 60);
    var s = delta % 60;
    function p2(n) { return n < 10 ? '0' + n : String(n); }
    if (d > 0) return 'T' + sign + d + 'd ' + h + 'h ' + p2(m) + 'm';
    return 'T' + sign + p2(h) + ':' + p2(m) + ':' + p2(s);
  }

  function _bearingLabel(az) {
    if (az == null || isNaN(az)) return '—';
    var idx = Math.round((((az % 360) + 360) % 360) / 45) % 8;
    return COMPASS[idx];
  }

  // -- Main entry point ----------------------------------------------------
  function update(space) {
    if (!space) return;
    if (!_resolveDom()) return;
    _lastSpace = space;
    _section.style.display = '';

    // Totals & groups
    var groups = space.tle_groups || {};
    var totalSats = 0, groupNames = [];
    for (var g in groups) {
      if (Object.prototype.hasOwnProperty.call(groups, g)) {
        totalSats += groups[g];
        groupNames.push(g + ' (' + groups[g] + ')');
      }
    }
    if (_satCountEl) _satCountEl.textContent = totalSats + ' tracked';
    if (_groupsSubEl) _groupsSubEl.textContent = groupNames.join(', ');
    if (_countEl) _countEl.textContent = totalSats;

    // Positions / overhead counts
    var positions = space.positions;
    var objects = (positions && positions.objects) || [];
    var overhead = 0, overheadNames = [];
    for (var i = 0; i < objects.length; i++) {
      if (typeof objects[i].el === 'number' && objects[i].el >= 10) {
        overhead++;
        if (overheadNames.length < 3) overheadNames.push(objects[i].name);
      }
    }
    if (_overheadEl) {
      _overheadEl.textContent = space.observer ? String(overhead) : '—';
    }
    if (_overheadNamesEl) {
      _overheadNamesEl.textContent = space.observer
        ? (overheadNames.length ? 'Overhead: ' + overheadNames.join(', ') : 'Nothing overhead')
        : 'No observer location';
    }

    // Space weather
    var weather = space.weather;
    var kp = (weather && weather.kp != null) ? weather.kp : null;
    _renderKpGauge(kp);
    if (_kpEl) {
      _kpEl.textContent = kp != null ? kp.toFixed(1) : '—';
    }
    if (_kpTimeEl) {
      _kpTimeEl.textContent = weather && weather.time_tag
        ? 'NOAA ' + weather.time_tag.replace('T', ' ').slice(11, 16) + ' UTC'
        : '';
    }

    // Passes: summary, timeline, table
    var passes = space.passes || [];
    _renderPassSummary(passes, !!space.observer);
    _renderPassTimeline(passes, !!space.observer);
    _renderPassTable(passes);

    // Sky dome (uses next pass for arc, objects for sats)
    var nowEpoch = Date.now() / 1000;
    var nextPass = null;
    for (var p = 0; p < passes.length; p++) {
      if (passes[p].los_ts > nowEpoch) { nextPass = passes[p]; break; }
    }
    _renderSkyDome(objects, space.observer, nextPass);

    // Launches
    var launches = space.launches || [];
    _renderLaunchHero(launches[0]);
    _renderLaunchTable(launches);

    // Map (+ terminator + footprints) only when the section is expanded
    if (_expanded && _map) {
      _renderMap(objects, space.observer);
      _renderFootprints(objects);
      _renderTerminator();
    }
  }

  // -- Sky Dome ------------------------------------------------------------
  function _renderSkyDome(objects, observer, nextPass) {
    if (!_skydomeEl) return;
    _clear(_skydomeEl);

    var cx = 120, cy = 120, Rr = 100;

    if (!observer) {
      _skydomeEl.appendChild(_svg('circle', { cx: cx, cy: cy, r: Rr, 'class': 'sd-ring' }));
      _skydomeEl.appendChild(_svg('text', { x: cx, y: cy - 4, 'class': 'sd-empty' },
        'No observer location'));
      _skydomeEl.appendChild(_svg('text', { x: cx, y: cy + 12, 'class': 'sd-empty' },
        'configured'));
      return;
    }

    // Rings
    _skydomeEl.appendChild(_svg('circle', { cx: cx, cy: cy, r: Rr, 'class': 'sd-ring' }));
    _skydomeEl.appendChild(_svg('circle', { cx: cx, cy: cy, r: Rr * 2 / 3, 'class': 'sd-ring sd-ring-mid' }));
    _skydomeEl.appendChild(_svg('circle', { cx: cx, cy: cy, r: Rr * 1 / 3, 'class': 'sd-ring sd-ring-mid' }));
    // Cardinal crosshairs
    _skydomeEl.appendChild(_svg('line', { x1: cx, y1: cy - Rr, x2: cx, y2: cy + Rr, 'class': 'sd-axis' }));
    _skydomeEl.appendChild(_svg('line', { x1: cx - Rr, y1: cy, x2: cx + Rr, y2: cy, 'class': 'sd-axis' }));
    // Compass labels
    _skydomeEl.appendChild(_svg('text', { x: cx, y: cy - Rr - 8, 'class': 'sd-compass' }, 'N'));
    _skydomeEl.appendChild(_svg('text', { x: cx, y: cy + Rr + 10, 'class': 'sd-compass' }, 'S'));
    _skydomeEl.appendChild(_svg('text', { x: cx + Rr + 10, y: cy, 'class': 'sd-compass' }, 'E'));
    _skydomeEl.appendChild(_svg('text', { x: cx - Rr - 10, y: cy, 'class': 'sd-compass' }, 'W'));

    function proj(az, el) {
      var rn = (1 - Math.max(0, Math.min(90, el)) / 90) * Rr;
      return {
        x: cx + rn * Math.sin(az * DEG),
        y: cy - rn * Math.cos(az * DEG)
      };
    }

    // Projected arc of the next pass
    if (nextPass && nextPass.aos_az != null && nextPass.los_az != null) {
      var aos = proj(nextPass.aos_az, 0);
      var los = proj(nextPass.los_az, 0);
      var tcaAz = nextPass.max_el_az != null
        ? nextPass.max_el_az
        : (nextPass.aos_az + nextPass.los_az) / 2;
      var tcaEl = nextPass.max_el != null ? nextPass.max_el : 30;
      var tca = proj(tcaAz, tcaEl);
      // Quadratic Bezier that passes through TCA at t=0.5
      var ctrlX = 2 * tca.x - 0.5 * (aos.x + los.x);
      var ctrlY = 2 * tca.y - 0.5 * (aos.y + los.y);
      _skydomeEl.appendChild(_svg('path', {
        d: 'M ' + aos.x + ' ' + aos.y + ' Q ' + ctrlX + ' ' + ctrlY + ' ' + los.x + ' ' + los.y,
        'class': 'sd-arc'
      }));
      _skydomeEl.appendChild(_svg('circle', { cx: aos.x, cy: aos.y, r: 2.5, 'class': 'sd-arc-pt' }));
      _skydomeEl.appendChild(_svg('circle', { cx: tca.x, cy: tca.y, r: 3.5, 'class': 'sd-arc-pt' }));
      _skydomeEl.appendChild(_svg('circle', { cx: los.x, cy: los.y, r: 2.5, 'class': 'sd-arc-pt' }));
    }

    // Satellites currently above horizon
    for (var i = 0; i < objects.length; i++) {
      var o = objects[i];
      if (o.el == null || o.az == null || o.el < 0) continue;
      var pt = proj(o.az, o.el);
      var cls = 'sd-sat';
      if (o.el >= 30) cls += ' sd-sat-overhead';
      else if (o.el < 10) cls += ' sd-sat-low';
      var radius = 2 + (o.el / 90) * 3;
      var gg = _svg('g');
      gg.appendChild(_svg('circle', { cx: pt.x, cy: pt.y, r: radius, 'class': cls }));
      gg.appendChild(_svg('title', null,
        o.name + '  ·  el ' + o.el.toFixed(1) + '°  ·  az ' + o.az.toFixed(1) + '° (' + _bearingLabel(o.az) + ')'));
      _skydomeEl.appendChild(gg);
    }

    // Observer at zenith
    _skydomeEl.appendChild(_svg('circle', { cx: cx, cy: cy, r: 2, 'class': 'sd-observer' }));
  }

  // -- Kp Gauge ------------------------------------------------------------
  function _kpBandClass(kp) {
    if (kp == null) return '';
    if (kp < 4) return 'kp-quiet';
    if (kp < 5) return 'kp-unsettled';
    if (kp < 7) return 'kp-active';
    return 'kp-storm';
  }
  function _kpBandLabel(kp) {
    if (kp == null) return '';
    if (kp < 4) return 'Quiet';
    if (kp < 5) return 'Unsettled';
    if (kp < 6) return 'Active';
    if (kp < 7) return 'Minor Storm';
    return 'Storm';
  }

  function _renderKpGauge(kp) {
    if (!_kpGaugeEl) return;
    _clear(_kpGaugeEl);

    var cx = 110, cy = 100, r = 80;

    // Background track (full half-circle)
    _kpGaugeEl.appendChild(_svg('path', {
      d: 'M ' + (cx - r) + ' ' + cy + ' A ' + r + ' ' + r + ' 0 0 1 ' + (cx + r) + ' ' + cy,
      'class': 'kp-track'
    }));

    // Tick marks + numeric labels (0..9)
    for (var k = 0; k <= 9; k++) {
      var a = (180 - (k / 9) * 180) * DEG;
      var x1 = cx + r * Math.cos(a);
      var y1 = cy - r * Math.sin(a);
      var x2 = cx + (r - 9) * Math.cos(a);
      var y2 = cy - (r - 9) * Math.sin(a);
      _kpGaugeEl.appendChild(_svg('line', { x1: x1, y1: y1, x2: x2, y2: y2, 'class': 'kp-tick' }));
      var lx = cx + (r + 10) * Math.cos(a);
      var ly = cy - (r + 10) * Math.sin(a);
      _kpGaugeEl.appendChild(_svg('text', { x: lx, y: ly + 3, 'class': 'kp-tick-label' }, String(k)));
    }

    if (kp != null) {
      var clamped = Math.max(0, Math.min(9, kp));
      var endAngle = (180 - (clamped / 9) * 180) * DEG;
      var ex = cx + r * Math.cos(endAngle);
      var ey = cy - r * Math.sin(endAngle);
      // Fill arc — sweep is never >180°, so large-arc-flag=0
      _kpGaugeEl.appendChild(_svg('path', {
        d: 'M ' + (cx - r) + ' ' + cy + ' A ' + r + ' ' + r + ' 0 0 1 ' + ex + ' ' + ey,
        'class': 'kp-fill ' + _kpBandClass(kp)
      }));
      // Needle
      var nLen = r - 14;
      var nx = cx + nLen * Math.cos(endAngle);
      var ny = cy - nLen * Math.sin(endAngle);
      _kpGaugeEl.appendChild(_svg('line', { x1: cx, y1: cy, x2: nx, y2: ny, 'class': 'kp-needle' }));
      _kpGaugeEl.appendChild(_svg('circle', { cx: cx, cy: cy, r: 4, 'class': 'kp-needle-hub' }));
    } else {
      _kpGaugeEl.appendChild(_svg('text', { x: cx, y: cy - 8, 'class': 'sd-empty' }, 'No data'));
    }

    if (_kpBandEl) {
      _kpBandEl.textContent = _kpBandLabel(kp);
      _kpBandEl.className = 'space-kp-label ' + _kpBandClass(kp);
    }
  }

  // -- Launch Hero ---------------------------------------------------------
  function _renderLaunchHero(launch) {
    _lastLaunch = launch || null;
    if (_nextLaunchEl) {
      _nextLaunchEl.textContent = launch ? (launch.name || '—') : 'No upcoming launch';
    }
    if (_nextLaunchWhenEl) {
      var bits = [];
      if (launch && launch.provider) bits.push(launch.provider);
      if (launch && launch.pad) bits.push(launch.pad);
      _nextLaunchWhenEl.textContent = bits.join('  ·  ');
    }
    _updateHeroCountdown();
    if (_lastLaunch && !_heroTickerId) {
      _heroTickerId = setInterval(_updateHeroCountdown, 1000);
    } else if (!_lastLaunch && _heroTickerId) {
      clearInterval(_heroTickerId);
      _heroTickerId = null;
    }
  }

  function _updateHeroCountdown() {
    if (!_heroCountdownEl) return;
    if (!_lastLaunch || !_lastLaunch.net) {
      _heroCountdownEl.textContent = 'T--';
      _heroCountdownEl.classList.remove('imminent');
      return;
    }
    _heroCountdownEl.textContent = _countdownHero(_lastLaunch.net);
    var target = Date.parse(_lastLaunch.net);
    var delta = !isNaN(target) ? (target / 1000 - Date.now() / 1000) : 1e9;
    _heroCountdownEl.classList.toggle('imminent', delta > 0 && delta < 3600);
  }

  // -- Pass summary + timeline --------------------------------------------
  function _renderPassSummary(passes, haveObserver) {
    var nowEpoch = Date.now() / 1000;
    var next = null;
    for (var i = 0; i < passes.length; i++) {
      if (passes[i].los_ts > nowEpoch) { next = passes[i]; break; }
    }
    if (_nextPassEl) {
      _nextPassEl.textContent = next ? (next.name || '—') : '—';
    }
    if (_nextPassWhenEl) {
      if (next) {
        var inProgress = next.aos_ts <= nowEpoch;
        var when = inProgress ? 'in progress' : _countdownFromEpoch(next.aos_ts);
        var maxEl = (next.max_el != null) ? ('max ' + next.max_el.toFixed(0) + '°') : '';
        _nextPassWhenEl.textContent = maxEl ? (when + '  ·  ' + maxEl) : when;
      } else {
        _nextPassWhenEl.textContent = haveObserver ? 'none upcoming' : '(no observer)';
      }
    }
  }

  function _renderPassTimeline(passes, haveObserver) {
    if (!_timelineEl) return;
    var rect = _timelineEl.getBoundingClientRect();
    if (rect.width < 20) return;  // hidden or not yet laid out

    _clear(_timelineEl);

    var W = Math.floor(rect.width);
    var H = 200;
    _timelineEl.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    _timelineEl.setAttribute('height', H);

    var leftGutter = 96;
    var topMargin = 24;
    var bottomMargin = 6;
    var plotW = W - leftGutter - 8;
    var plotH = H - topMargin - bottomMargin;

    if (!haveObserver) {
      _timelineEl.appendChild(_svg('text', { x: W / 2, y: H / 2, 'class': 'tl-empty' },
        'Configure observer location to see upcoming passes'));
      return;
    }

    var nowEpoch = Date.now() / 1000;
    var tEnd = nowEpoch + TIMELINE_S;

    // Hour grid + labels every 2h
    for (var t = 0; t <= TIMELINE_S; t += 2 * 3600) {
      var tx = leftGutter + (t / TIMELINE_S) * plotW;
      _timelineEl.appendChild(_svg('line', {
        x1: tx, y1: topMargin, x2: tx, y2: topMargin + plotH, 'class': 'tl-grid'
      }));
      var lbl = t === 0 ? 'now' : '+' + (t / 3600) + 'h';
      _timelineEl.appendChild(_svg('text', {
        x: tx, y: topMargin - 8, 'class': 'tl-hour-label'
      }, lbl));
    }

    // Filter passes into window
    var upcoming = [];
    for (var i = 0; i < passes.length; i++) {
      var pp = passes[i];
      if (pp.los_ts <= nowEpoch) continue;
      if (pp.aos_ts >= tEnd) continue;
      upcoming.push(pp);
    }

    if (upcoming.length === 0) {
      _timelineEl.appendChild(_svg('text', { x: W / 2, y: H / 2, 'class': 'tl-empty' },
        'No passes in the next 12 hours'));
      _drawNowLine(leftGutter, topMargin, topMargin + plotH);
      return;
    }

    // Assign lanes in order of first appearance
    var laneOrder = [];
    var laneIndex = {};
    for (var j = 0; j < upcoming.length; j++) {
      var nm = upcoming[j].name || '—';
      if (!(nm in laneIndex)) {
        laneIndex[nm] = laneOrder.length;
        laneOrder.push(nm);
      }
    }
    var visibleLanes = Math.min(laneOrder.length, TIMELINE_LANES);
    var hiddenLaneCount = laneOrder.length - visibleLanes;
    var laneH = plotH / Math.max(visibleLanes, 1);

    // Lane labels
    for (var k = 0; k < visibleLanes; k++) {
      var laneY = topMargin + (k + 0.5) * laneH;
      _timelineEl.appendChild(_svg('text', {
        x: leftGutter - 6, y: laneY, 'class': 'tl-lane-label'
      }, laneOrder[k]));
    }
    if (hiddenLaneCount > 0) {
      _timelineEl.appendChild(_svg('text', {
        x: leftGutter - 6, y: topMargin + plotH - 4, 'class': 'tl-lane-more'
      }, '+' + hiddenLaneCount + ' more'));
    }

    // Pass bars
    for (var m = 0; m < upcoming.length; m++) {
      var p = upcoming[m];
      var li = laneIndex[p.name || '—'];
      if (li >= visibleLanes) continue;
      var x1 = leftGutter + Math.max(0, (p.aos_ts - nowEpoch) / TIMELINE_S) * plotW;
      var x2 = leftGutter + Math.min(1, (p.los_ts - nowEpoch) / TIMELINE_S) * plotW;
      var laneTop = topMargin + li * laneH;
      var maxEl = p.max_el != null ? p.max_el : 0;
      var barH = Math.min(laneH - 4, 6 + (maxEl / 90) * Math.max(0, laneH - 10));
      var y = laneTop + (laneH - barH) / 2;
      var cls = 'tl-pass';
      if (maxEl >= 60) cls += ' high';
      else if (maxEl >= 30) cls += ' med';
      var gg = _svg('g');
      gg.appendChild(_svg('rect', {
        x: x1, y: y, width: Math.max(2, x2 - x1), height: barH, rx: 2, ry: 2, 'class': cls
      }));
      if (p.max_el_ts && p.max_el_ts > nowEpoch && p.max_el_ts < tEnd) {
        var tcaX = leftGutter + ((p.max_el_ts - nowEpoch) / TIMELINE_S) * plotW;
        gg.appendChild(_svg('polygon', {
          points: (tcaX - 3) + ',' + (y - 2) + ' ' +
                  (tcaX + 3) + ',' + (y - 2) + ' ' +
                  tcaX + ',' + (y + 3),
          'class': 'tl-tca'
        }));
      }
      var dur = Math.max(0, Math.round((p.los_ts - p.aos_ts) / 60));
      gg.appendChild(_svg('title', null,
        p.name + '  ·  AOS ' + _countdownFromEpoch(p.aos_ts) +
        '  ·  ' + _bearingLabel(p.aos_az) + '→' + _bearingLabel(p.los_az) +
        '  ·  max el ' + (p.max_el != null ? p.max_el.toFixed(0) + '°' : '—') +
        '  ·  ' + dur + ' min'));
      _timelineEl.appendChild(gg);
    }

    _drawNowLine(leftGutter, topMargin, topMargin + plotH);
  }

  function _drawNowLine(x, y1, y2) {
    _timelineEl.appendChild(_svg('line', { x1: x, y1: y1, x2: x, y2: y2, 'class': 'tl-now' }));
  }

  // -- Tables --------------------------------------------------------------
  function _renderLaunchTable(launches) {
    if (!_launchTableEl) return;
    var rows = '';
    for (var j = 0; j < launches.length; j++) {
      var L_ = launches[j];
      rows += '<tr>' +
        '<td>' + esc(_formatCountdown(L_.net)) + '</td>' +
        '<td>' + esc(L_.name || '—') + '</td>' +
        '<td>' + esc(L_.provider || '') + '</td>' +
        '<td>' + esc(L_.pad || '') + '</td>' +
        '</tr>';
    }
    _launchTableEl.innerHTML = rows || '<tr><td colspan="4">No upcoming launches loaded</td></tr>';
  }

  function _renderPassTable(passes) {
    if (!_passTableEl) return;
    var nowEpoch = Date.now() / 1000;
    var rows = '';
    var shown = 0;
    for (var k = 0; k < passes.length && shown < 20; k++) {
      var P = passes[k];
      if (P.los_ts <= nowEpoch) continue;
      var when = (P.aos_ts <= nowEpoch)
        ? 'in progress'
        : _countdownFromEpoch(P.aos_ts);
      var dir = _bearingLabel(P.aos_az) + '→' + _bearingLabel(P.los_az);
      var dur = Math.max(0, Math.round((P.duration_s || (P.los_ts - P.aos_ts)) / 60));
      var maxElStr = (P.max_el != null) ? (P.max_el.toFixed(0) + '°') : '—';
      var nameCell;
      if (P.catnr) {
        nameCell = '<a href="https://www.n2yo.com/satellite/?s=' + esc(P.catnr) +
          '" target="_blank" rel="noopener noreferrer" class="sat-link" title="Satellite info (N2YO)">' +
          esc(P.name || '—') + '</a>';
      } else {
        nameCell = esc(P.name || '—');
      }
      rows += '<tr>' +
        '<td>' + esc(when) + '</td>' +
        '<td>' + nameCell + '</td>' +
        '<td>' + esc(maxElStr) + '</td>' +
        '<td>' + esc(dir) + '</td>' +
        '<td>' + esc(dur + 'm') + '</td>' +
        '</tr>';
      shown++;
    }
    _passTableEl.innerHTML = rows || '<tr><td colspan="5">No upcoming passes</td></tr>';
  }

  // -- World Map -----------------------------------------------------------
  function _renderMap(objects, observer) {
    if (!_map || !_satLayer) return;

    if (observer && observer.lat != null && observer.lon != null) {
      if (!_observerMarker) {
        _observerMarker = L.circleMarker([observer.lat, observer.lon], {
          radius: 7,
          color: '#ff5a1f',
          fillColor: '#ff8c42',
          fillOpacity: 0.9,
          weight: 2
        }).addTo(_map).bindPopup('Observer');
      } else {
        _observerMarker.setLatLng([observer.lat, observer.lon]);
      }
    }

    var seen = {};
    for (var i = 0; i < objects.length; i++) {
      var o = objects[i];
      if (o.lat == null || o.lon == null) continue;
      seen[o.name] = true;
      var overhead = (o.el != null && o.el >= 10);
      var color = overhead ? '#7ee787' : '#6aa9ff';
      var radius = overhead ? 5 : 3;
      var existing = _satMarkers[o.name];
      if (existing) {
        existing.setLatLng([o.lat, o.lon]);
        existing.setStyle({ color: color, fillColor: color });
        existing.setRadius(radius);
      } else {
        var m = L.circleMarker([o.lat, o.lon], {
          radius: radius,
          color: color,
          fillColor: color,
          fillOpacity: 0.8,
          weight: 1
        });
        m.bindPopup(_satPopup(o));
        m.addTo(_satLayer);
        _satMarkers[o.name] = m;
      }
    }
    for (var name in _satMarkers) {
      if (!seen[name]) {
        _satLayer.removeLayer(_satMarkers[name]);
        delete _satMarkers[name];
      }
    }
  }

  function _satPopup(o) {
    var h = '<div class="map-popup">';
    h += '<div class="map-popup-name">' + esc(o.name) + '</div>';
    h += '<div class="map-popup-grid">';
    h += '<div><b>Lat/Lon:</b> ' + o.lat.toFixed(2) + ', ' + o.lon.toFixed(2) + '</div>';
    if (o.alt_km != null) h += '<div><b>Alt:</b> ' + o.alt_km.toFixed(0) + ' km</div>';
    if (o.el != null) h += '<div><b>Elev:</b> ' + o.el.toFixed(1) + '&deg;</div>';
    if (o.az != null) h += '<div><b>Az:</b> ' + o.az.toFixed(1) + '&deg;</div>';
    h += '</div></div>';
    return h;
  }

  // -- Satellite footprints ------------------------------------------------
  function _footprintRadiusKm(alt_km) {
    if (alt_km == null || alt_km <= 0) return 0;
    var theta = Math.acos(R_EARTH_KM / (R_EARTH_KM + alt_km));
    return R_EARTH_KM * theta;
  }

  function _renderFootprints(objects) {
    if (!_footprintLayer) return;
    var seen = {};
    for (var i = 0; i < objects.length; i++) {
      var o = objects[i];
      if (o.lat == null || o.lon == null) continue;
      if (o.el == null || o.el < 10) continue;  // only overhead sats get a footprint
      seen[o.name] = true;
      var radiusM = _footprintRadiusKm(o.alt_km) * 1000;
      if (radiusM <= 0) continue;
      var existing = _footprints[o.name];
      if (existing) {
        existing.setLatLng([o.lat, o.lon]);
        existing.setRadius(radiusM);
      } else {
        var c = L.circle([o.lat, o.lon], {
          radius: radiusM,
          color: '#58a6ff',
          weight: 1,
          opacity: 0.35,
          fillColor: '#58a6ff',
          fillOpacity: 0.07,
          interactive: false
        });
        c.addTo(_footprintLayer);
        _footprints[o.name] = c;
      }
    }
    for (var name in _footprints) {
      if (!seen[name]) {
        _footprintLayer.removeLayer(_footprints[name]);
        delete _footprints[name];
      }
    }
  }

  // -- Day/Night Terminator ------------------------------------------------
  function _subsolarPoint() {
    var now = new Date();
    var startOfYear = Date.UTC(now.getUTCFullYear(), 0, 0);
    var dayOfYear = (now.getTime() - startOfYear) / 86400000;
    var gamma = 2 * Math.PI * (dayOfYear - 1) / 365.0;
    // Spencer declination approximation (radians)
    var decl = 0.006918
      - 0.399912 * Math.cos(gamma)     + 0.070257 * Math.sin(gamma)
      - 0.006758 * Math.cos(2 * gamma) + 0.000907 * Math.sin(2 * gamma)
      - 0.002697 * Math.cos(3 * gamma) + 0.001480 * Math.sin(3 * gamma);
    var utcHours = now.getUTCHours()
      + now.getUTCMinutes() / 60
      + now.getUTCSeconds() / 3600;
    var slon = -15 * (utcHours - 12);
    if (slon > 180) slon -= 360;
    if (slon < -180) slon += 360;
    return { lat: decl * 180 / Math.PI, lon: slon };
  }

  function _renderTerminator() {
    if (!_terminatorLayer) return;
    _terminatorLayer.clearLayers();

    var sub = _subsolarPoint();
    var aslat = -sub.lat * DEG;            // antisolar lat (rad)
    var aslon = (sub.lon + 180) * DEG;     // antisolar lon (rad)

    // Terminator = great circle 90° from the antisolar point.
    // cos(pi/2)=0, sin(pi/2)=1 — the formulas simplify.
    var seg = [];
    var lastLon = null;
    function flushSegment() {
      if (seg.length >= 2) {
        L.polyline(seg, {
          color: '#8b949e',
          weight: 1,
          opacity: 0.55,
          dashArray: '4 4',
          interactive: false
        }).addTo(_terminatorLayer);
      }
      seg = [];
    }
    for (var b = 0; b <= 360; b += 2) {
      var bRad = b * DEG;
      var lat = Math.asin(Math.cos(aslat) * Math.cos(bRad));
      var lon = aslon + Math.atan2(
        Math.sin(bRad) * Math.cos(aslat),
        -Math.sin(aslat) * Math.sin(lat)
      );
      var latD = lat / DEG;
      var lonD = ((lon / DEG + 540) % 360) - 180;
      if (lastLon != null && Math.abs(lonD - lastLon) > 180) flushSegment();
      seg.push([latD, lonD]);
      lastLon = lonD;
    }
    flushSegment();
  }

  // Public surface
  R.space = {
    update: update
  };
})();
