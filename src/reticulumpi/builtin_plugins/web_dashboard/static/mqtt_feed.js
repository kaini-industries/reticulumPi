/* ReticulumPi Dashboard — MQTT Feed module
 *
 * Read-only scrolling feed of Meshtastic MQTT messages.  Replaces the
 * former full messaging panel with a compact activity-log view, since
 * MQTT traffic is a global broadcast firehose with no delivery
 * confirmation.
 */
(function () {
  'use strict';
  var R = window.RPI;
  if (!R) return;
  var api = R.api, $ = R.$, esc = R.esc;
  var formatTimeAgo = R.formatTimeAgo;

  var MAX_ITEMS = 200;
  var BOOTSTRAP_LIMIT = 100;
  var SCROLL_LOAD_LIMIT = 50;

  // -- DOM handles --
  var _section, _toggle, _body, _countEl;
  var _statsEl, _searchEl, _listEl;

  // -- State --
  var _expanded = false;
  var _items = [];
  var _peers = {};
  var _totalCount = 0;
  var _bootstrapped = false;
  var _bootstrapping = false;
  var _loadingOlder = false;
  var _allLoaded = false;
  var _filterText = '';
  var _available = false;

  function _resolveDom() {
    if (_section) return true;
    _section  = $('mqtt-feed-section');
    _toggle   = $('mqtt-feed-toggle');
    _body     = $('mqtt-feed-body');
    _countEl  = $('mqtt-feed-count');
    _statsEl  = $('mqtt-feed-stats');
    _searchEl = $('mqtt-feed-search');
    _listEl   = $('mqtt-feed-list');
    if (!_section || !_listEl) return false;
    _wire();
    return true;
  }

  function _wire() {
    if (_toggle) {
      _toggle.addEventListener('click', function () {
        _expanded = !_expanded;
        _body.classList.toggle('hidden', !_expanded);
        var chev = _toggle.querySelector('.chevron');
        if (chev) chev.innerHTML = _expanded ? '&#9662;' : '&#9656;';
        if (_expanded && !_bootstrapped) _bootstrap();
      });
    }
    if (_searchEl) {
      var timer = null;
      _searchEl.addEventListener('input', function () {
        if (timer) clearTimeout(timer);
        timer = setTimeout(function () {
          _filterText = (_searchEl.value || '').trim().toLowerCase();
          _renderFeed();
        }, 250);
      });
    }
    if (_listEl) {
      _listEl.addEventListener('scroll', function () {
        if (_loadingOlder || _allLoaded) return;
        var el = _listEl;
        if (el.scrollTop + el.clientHeight >= el.scrollHeight - 40) {
          _loadOlder();
        }
      });
    }
  }

  // -- Bootstrap --
  function _bootstrap() {
    if (_bootstrapping) return;
    _bootstrapping = true;
    api('/api/messages?transport=meshtastic&sub_transport=mqtt&limit=' + BOOTSTRAP_LIMIT)
      .then(function (res) {
        _bootstrapping = false;
        var msgs = res && res.data && res.data.messages || [];
        if (!res) return;
        _bootstrapped = true;
        for (var i = 0; i < msgs.length; i++) {
          _ingestItem(msgs[i]);
        }
        if (msgs.length < BOOTSTRAP_LIMIT) _allLoaded = true;
        _renderFeed();
        _renderStats();
        _markAllRead();
      })
      .catch(function () { _bootstrapping = false; });
  }

  function _loadOlder() {
    _loadingOlder = true;
    var offset = _items.length;
    api('/api/messages?transport=meshtastic&sub_transport=mqtt&limit=' + SCROLL_LOAD_LIMIT + '&offset=' + offset)
      .then(function (res) {
        _loadingOlder = false;
        var msgs = res && res.data && res.data.messages || [];
        if (!res) return;
        if (!msgs.length) { _allLoaded = true; return; }
        if (msgs.length < SCROLL_LOAD_LIMIT) _allLoaded = true;
        for (var i = 0; i < msgs.length; i++) {
          _ingestItem(msgs[i]);
        }
        _renderFeed();
        _renderStats();
      })
      .catch(function () { _loadingOlder = false; });
  }

  // -- Data --
  function _ingestItem(msg) {
    if (msg.direction !== 'received') return;
    for (var i = 0; i < _items.length; i++) {
      if (_items[i].id === msg.id) return;
    }
    _items.push(msg);
    _totalCount++;
    if (msg.from_id) _peers[msg.from_id] = msg.from_name || msg.from_id;
  }

  function _matchesFilter(msg) {
    if (_filterText) {
      var haystack = ((msg.from_name || '') + ' ' + (msg.text || '')).toLowerCase();
      if (haystack.indexOf(_filterText) === -1) return false;
    }
    return true;
  }

  // -- Rendering --
  function _cardHtml(msg) {
    var sender = msg.from_name || msg.from_id || '?';
    var text = msg.text || '';
    var ts = msg.timestamp ? formatTimeAgo(msg.timestamp) : '';
    var meta = msg.metadata;
    if (typeof meta === 'string') {
      try { meta = JSON.parse(meta); } catch (e) { meta = null; }
    }
    var hops = '';
    if (meta && meta.hop_start !== undefined && meta.hops_away !== undefined) {
      hops = meta.hops_away + (meta.hops_away === 1 ? ' hop' : ' hops');
    } else if (meta && meta.path_len !== undefined) {
      hops = meta.path_len === 0 ? 'direct' : meta.path_len + (meta.path_len === 1 ? ' hop' : ' hops');
    }
    return '<div class="mqtt-feed-card" data-msg-id="' + esc(String(msg.id)) + '">'
      + '<div class="mqtt-feed-card-top">'
      + '<span class="mqtt-feed-sender">' + esc(sender) + '</span>'
      + (hops ? '<span class="mqtt-feed-hops">' + esc(hops) + '</span>' : '')
      + '<span class="mqtt-feed-time">' + esc(ts) + '</span>'
      + '</div>'
      + '<div class="mqtt-feed-text">' + esc(text) + '</div>'
      + '</div>';
  }

  function _renderFeed() {
    if (!_listEl) return;
    var html = '';
    var shown = 0;
    for (var i = 0; i < _items.length; i++) {
      if (!_matchesFilter(_items[i])) continue;
      html += _cardHtml(_items[i]);
      shown++;
    }
    if (!shown && _bootstrapped) {
      html = '<div class="mqtt-feed-empty">No messages' + (_filterText ? ' matching filter' : '') + '</div>';
    }
    _listEl.innerHTML = html;
  }

  function _prependCard(msg) {
    if (!_listEl || !_expanded) return;
    if (!_matchesFilter(msg)) return;
    var tmp = document.createElement('div');
    tmp.innerHTML = _cardHtml(msg);
    var card = tmp.firstChild;
    if (card) {
      card.classList.add('mqtt-feed-card-new');
      _listEl.insertBefore(card, _listEl.firstChild);
    }
    while (_listEl.children.length > MAX_ITEMS) {
      _listEl.removeChild(_listEl.lastChild);
    }
  }

  function _renderStats() {
    if (!_statsEl) return;
    var peerCount = Object.keys(_peers).length;
    var last = _items.length > 0 && _items[0].timestamp
      ? formatTimeAgo(_items[0].timestamp) : '--';
    _statsEl.innerHTML =
      '<span>' + peerCount + ' peer' + (peerCount !== 1 ? 's' : '') + '</span>'
      + '<span class="mqtt-feed-stats-sep">&middot;</span>'
      + '<span>' + _totalCount + ' message' + (_totalCount !== 1 ? 's' : '') + '</span>'
      + '<span class="mqtt-feed-stats-sep">&middot;</span>'
      + '<span>last ' + esc(last) + '</span>';
  }


  function _markAllRead() {
    var seen = {};
    for (var i = 0; i < _items.length; i++) {
      var cid = _items[i].contact_id;
      if (cid && !seen[cid]) {
        seen[cid] = true;
        api('/api/messages/read', { method: 'POST', json: { contact_id: cid } });
      }
    }
  }

  // -- Public API --
  function update(payload) {
    if (!_resolveDom()) return;
    var transports = payload && payload.transports;
    var nowAvail = false;
    if (transports) {
      for (var i = 0; i < transports.length; i++) {
        if (transports[i].name === 'meshtastic' && transports[i].available) {
          nowAvail = true;
          break;
        }
      }
    }
    _available = nowAvail;
    _section.style.display = nowAvail ? '' : 'none';
    if (_countEl) {
      _countEl.textContent = nowAvail ? '' : 'offline';
    }
    if (_expanded && _statsEl) _renderStats();
    if (_expanded && !_bootstrapped && nowAvail) _bootstrap();
  }

  function onMessage(row) {
    if (!row || row.transport !== 'meshtastic') return;
    if ((row.sub_transport || '') !== 'mqtt') return;
    if (row.direction !== 'received') return;
    if (!_resolveDom()) return;
    _ingestItem(row);
    _items.sort(function (a, b) { return (b.timestamp || 0) - (a.timestamp || 0); });
    while (_items.length > MAX_ITEMS) _items.pop();
    _prependCard(row);
    _renderStats();
    if (_expanded && row.contact_id) {
      api('/api/messages/read', { method: 'POST', json: { contact_id: row.contact_id } });
    }
  }

  R.updateMqttFeed = update;
  R.onMqttFeedMessage = onMessage;
})();
