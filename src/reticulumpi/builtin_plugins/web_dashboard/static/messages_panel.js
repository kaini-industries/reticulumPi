/* ReticulumPi Dashboard — shared single-transport Messages panel factory.
 *
 * Each transport (LXMF, Meshtastic MQTT, Meshtastic, MeshCore) gets its
 * own <section> on the dashboard.  A thin wrapper calls
 * `RPI.createMessagesPanel(config)` with transport-specific settings; this
 * factory owns all DOM rendering, state, API calls, and WebSocket handling
 * for that one panel.
 *
 * Config shape:
 *   {
 *     rootId:        'msg-mqtt',          // DOM id prefix
 *     sectionTitle:  'Meshtastic MQTT',
 *     transport:     'meshtastic',        // API transport filter
 *     subTransport:  'mqtt' | 'lora' | null,
 *     supportsChannels: true|false,       // show channel selector + Channels btn
 *     broadcastLabel: 'Broadcast (all)',
 *   }
 */
(function () {
  'use strict';
  var R = window.RPI;
  if (!R) return;
  var api = R.api, apiRetry = R.apiRetry, $ = R.$, esc = R.esc;
  var formatTimeAgo = R.formatTimeAgo;

  // Shared /api/meshtastic/channels fetcher.  Both Meshtastic panels
  // (MQTT, LoRa) and the channel-management dialog hit the same
  // endpoint, which talks to the radio over serial — so we coalesce
  // in-flight requests and reuse a brief cache to make one call
  // instead of three on page load.  Invalidate after join/delete.
  var _chCache = null;        // {data: [...], ts: <ms>}
  var _chInflight = null;     // shared Promise
  var CH_CACHE_MS = 5000;
  function _fetchChannelsShared() {
    if (_chCache && Date.now() - _chCache.ts < CH_CACHE_MS) {
      return Promise.resolve(_chCache.data);
    }
    if (_chInflight) return _chInflight;
    _chInflight = apiRetry('/api/meshtastic/channels').then(function (r) {
      _chInflight = null;
      if (!r || !r.ok) return [];
      var data = r.data.channels || [];
      _chCache = {data: data, ts: Date.now()};
      return data;
    }, function () {
      _chInflight = null;
      return [];
    });
    return _chInflight;
  }
  function _invalidateChannels() { _chCache = null; }

  var _trCache = null;
  var _trInflight = null;
  var TR_CACHE_MS = 5000;
  function _fetchTransportsShared() {
    if (_trCache && Date.now() - _trCache.ts < TR_CACHE_MS) {
      return Promise.resolve(_trCache.data);
    }
    if (_trInflight) return _trInflight;
    _trInflight = apiRetry('/api/messages/transports').then(function (r) {
      _trInflight = null;
      if (!r || !r.ok) return [];
      var data = r.data.transports || [];
      _trCache = {data: data, ts: Date.now()};
      return data;
    }, function () {
      _trInflight = null;
      return [];
    });
    return _trInflight;
  }

  // ── Module-level panel registry ──────────────────────────────────
  // Populated on each createMessagesPanel() call so the top-level WS
  // dispatch (R.onMessagingEvent / R.onMessagingStatus) can fan a
  // single server event out to all 4 panels; each panel filters by
  // its own transport/sub_transport.
  var _allPanels = [];

  function createMessagesPanel(cfg) {
    // ── Per-panel state ────────────────────────────────────────────
    var _conversations = [];
    var _activeContactId = null;
    var _activeMsgType = null;
    var _threadMessages = [];
    var _unreadCounts = {};
    var _readGrace = {};
    var _deletePending = null;
    var _sending = false;
    var _contacts = [];
    var _channels = [];
    var _newComposeOpen = false;
    var _expanded = false;
    var _dom = null;       // resolved DOM refs, cached on first data arrival
    var _available = false;
    var _maxBytes = null;
    var _destOptions = [];
    var _destSelection = null;

    // Dedupe per-message event arrivals.  An incoming row may reach
    // this panel both via a per-message WS push and (during rollout
    // or reconnect) via a tick-replay; keep the latest 500 seen ids.
    var _seenMsgIds = Object.create(null);
    var _seenOrder = [];
    function _markSeen(id) {
      if (id === null || id === undefined) return false;
      var key = String(id);
      if (_seenMsgIds[key]) return true;
      _seenMsgIds[key] = true;
      _seenOrder.push(key);
      if (_seenOrder.length > 500) {
        delete _seenMsgIds[_seenOrder.shift()];
      }
      return false;
    }

    // Gate for `_refresh()` — when true, tab-switch refresh trusts
    // in-memory state instead of round-tripping the server.  Flipped
    // on after the first successful conversations fetch or WS event,
    // cleared on WS disconnect.
    var _hasFreshData = false;

    // Load-older pagination.  When the user scrolls near the top of
    // an active thread, we fetch the next page with `before=<oldest>`
    // and prepend to the DOM without re-rendering the whole thread.
    // `_olderLoading` debounces the fetch; `_olderExhausted` pins
    // after a short-page response (<limit rows) so further scrolls
    // don't keep asking the server.
    var OLDER_PAGE = 50;
    var _olderLoading = false;
    var _olderExhausted = false;

    var id = function (suffix) { return cfg.rootId + '-' + suffix; };

    // ── Broadcast contact-id helpers ───────────────────────────────
    // Per-channel broadcast ids look like:
    //   __broadcast_meshtastic_lora_ch0__
    //   __broadcast_meshtastic_mqtt_ch2__
    // Legacy (pre-channel-split) ids were
    //   __broadcast_meshtastic_lora__
    // and have been purged from the DB on first startup after upgrade.
    function _broadcastCid(chIndex) {
      var base = '__broadcast_' + cfg.transport +
        (cfg.subTransport ? '_' + cfg.subTransport : '');
      if (chIndex !== undefined && chIndex !== null) base += '_ch' + chIndex;
      return base + '__';
    }
    function _parseBroadcastChannel(cid) {
      // Channel key may be a numeric radio-slot index (LoRa, or an MQTT
      // channel we have locally) or a sanitized channel name (MQTT peer
      // on a channel we don't have locally, e.g. "LongFast").
      var m = /_ch([A-Za-z0-9_-]+)__$/.exec(cid || '');
      if (!m) return null;
      var raw = m[1];
      if (/^\d+$/.test(raw)) return parseInt(raw, 10);
      return raw;
    }
    function _channelName(key) {
      if (typeof key === 'number') {
        for (var i = 0; i < _channels.length; i++) {
          if (_channels[i].index === key) {
            return _channels[i].name ||
              (key === 0 ? 'Primary' : 'Ch ' + key);
          }
        }
        return (key === 0 ? 'Primary' : 'Ch ' + key);
      }
      // String key — channel not configured locally, show the name as-is.
      return key;
    }

    // Channels with identical (name, psk_label) are functionally the
    // same conversation on the radio — same encryption key, same
    // display name — so the UI treats the lowest-indexed slot as
    // canonical and merges the others into it.  Returns the canonical
    // list and an alias map (aliasIndex -> canonicalIndex) for any
    // duplicates.
    function _canonicalize(channels) {
      var canonical = [];
      var aliasToCanonical = {};
      var seen = {};
      for (var i = 0; i < channels.length; i++) {
        var ch = channels[i];
        var key = (ch.name || '') + '|' + (ch.psk_label || '');
        if (seen.hasOwnProperty(key)) {
          aliasToCanonical[ch.index] = seen[key];
        } else {
          seen[key] = ch.index;
          canonical.push(ch);
        }
      }
      return { canonical: canonical, aliasToCanonical: aliasToCanonical };
    }

    // ── DOM ────────────────────────────────────────────────────────
    function _resolveDom() {
      if (_dom) return true;
      var section = $(cfg.rootId + '-section');
      if (!section) return false;
      _dom = {
        section: section,
        toggle: $(id('toggle')),
        body: $(id('body')),
        count: $(id('count')),
        unread: $(id('unread-total')),
        convs: $(id('conversations')),
        search: $(id('search')),
        newBtn: $(id('new-btn')),
        backBtn: $(id('back-btn')),
        deleteBtn: $(id('delete-btn')),
        chManage: $(id('ch-manage-btn')),
        chat: $(id('chat')),
        threadName: $(id('thread-name')),
        compose: $(id('compose')),
        newCompose: $(id('new-compose')),
        text: $(id('text')),
        sendBtn: $(id('send-btn')),
        byteCount: $(id('byte-count')),
        feedback: $(id('feedback')),
        chSelectWrap: $(id('channel-wrap')),
        chSelect: $(id('channel-select')),
        gpsBtn: $(id('gps-btn')),
        destCombo: $(id('dest-combo')),
        destInput: $(id('dest-input')),
        destList: $(id('dest-list')),
        lxmfRaw: $(id('lxmf-raw')),
      };
      _wire();
      return true;
    }

    function _wire() {
      if (_dom.toggle) {
        _dom.toggle.addEventListener('click', _onToggleClick);
      }
      if (_dom.text) {
        _dom.text.addEventListener('input', function () {
          _autoGrow(_dom.text);
          _updateByteCount();
        });
        _dom.text.addEventListener('keydown', function (e) {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            _onSend();
          }
        });
      }
      if (_dom.sendBtn) _dom.sendBtn.addEventListener('click', _onSend);
      if (_dom.gpsBtn) _dom.gpsBtn.addEventListener('click', _onInsertGps);
      if (_dom.newBtn) _dom.newBtn.addEventListener('click', _toggleNewCompose);
      if (_dom.backBtn) _dom.backBtn.addEventListener('click', _goBack);
      if (_dom.deleteBtn) _dom.deleteBtn.addEventListener('click', _onDeleteConversation);
      if (_dom.search) {
        var searchTimer = null;
        _dom.search.addEventListener('input', function () {
          if (searchTimer) clearTimeout(searchTimer);
          searchTimer = setTimeout(_onSearch, 300);
        });
      }
      if (_dom.convs) {
        _dom.convs.addEventListener('click', function (e) {
          var el = e.target.closest('.msg-conv-item');
          if (el) _selectConversation(el.getAttribute('data-contact'),
                                      el.getAttribute('data-msgtype'));
        });
      }
      if (_dom.chat) {
        _dom.chat.addEventListener('scroll', _onChatScroll, {passive: true});
      }
      if (_dom.destInput) {
        _dom.destInput.addEventListener('focus', function () {
          if (_dom.destInput) _dom.destInput.select();
          _openDestList();
        });
        _dom.destInput.addEventListener('input', function () {
          if (_dom.destCombo) _dom.destCombo.classList.add('open');
          _renderDestList();
        });
        _dom.destInput.addEventListener('keydown', _onDestKeydown);
      }
      if (_dom.destList) {
        _dom.destList.addEventListener('mousedown', function (e) {
          var el = e.target.closest('.msg-dest-item');
          if (!el) return;
          e.preventDefault();
          _pickDest(el.getAttribute('data-id'));
        });
      }
      document.addEventListener('mousedown', function (e) {
        if (!_dom.destCombo) return;
        if (!_dom.destCombo.classList.contains('open')) return;
        if (!_dom.destCombo.contains(e.target)) _closeDestList();
      });
      window.addEventListener('resize', _positionDestList);
      window.addEventListener('scroll', _positionDestList, true);
      if (_dom.chManage) {
        _dom.chManage.addEventListener('click', function () {
          if (R.openChannelDialog) R.openChannelDialog();
        });
      }
    }

    function _onToggleClick() {
      _expanded = !_expanded;
      _dom.body.classList.toggle('hidden', !_expanded);
      var chev = _dom.toggle.querySelector('.chevron');
      if (chev) chev.innerHTML = _expanded ? '&#9662;' : '&#9656;';
      if (_expanded) _refresh();
    }

    // ── Fetch helpers (all filtered to this panel's transport) ─────
    function _qs(extra) {
      var parts = ['transport=' + encodeURIComponent(cfg.transport)];
      if (cfg.subTransport !== null && cfg.subTransport !== undefined) {
        parts.push('sub_transport=' + encodeURIComponent(cfg.subTransport));
      }
      if (extra) parts = parts.concat(extra);
      return '?' + parts.join('&');
    }

    function _showConvsError(msg) {
      if (!_dom.convs) return;
      var banner = _dom.convs.querySelector('.msg-convs-error');
      if (!banner) {
        banner = document.createElement('div');
        banner.className = 'msg-convs-error';
        banner.addEventListener('click', function () { _fetchConversations(); });
        _dom.convs.insertBefore(banner, _dom.convs.firstChild);
      }
      banner.textContent = 'Couldn\u2019t refresh conversations — ' + msg + ' · click to retry';
    }

    function _clearConvsError() {
      if (!_dom.convs) return;
      var banner = _dom.convs.querySelector('.msg-convs-error');
      if (banner) banner.remove();
    }

    function _fetchConversations() {
      return apiRetry('/api/messages/conversations' + _qs()).then(function (r) {
        if (!r) { _showConvsError('network error'); return; }
        if (!r.ok) { _showConvsError(r.error || 'request failed'); return; }
        _clearConvsError();
        _conversations = (r.data && r.data.conversations) || [];
        _hasFreshData = true;
        _renderConversations();
      }).catch(function (err) {
        console.error('[' + cfg.rootId + '] _fetchConversations:', err);
        _showConvsError('unexpected error');
      });
    }

    function _fetchThread(contactId, append) {
      if (!contactId) return;
      var extra = ['limit=' + OLDER_PAGE];
      if (append && _threadMessages.length > 0) {
        var oldest = _threadMessages[_threadMessages.length - 1];
        if (oldest && oldest.timestamp) extra.push('before=' + oldest.timestamp);
      }
      var url = '/api/messages/conversation/' + encodeURIComponent(contactId) +
                '?' + extra.join('&');
      if (append) _olderLoading = true;
      return apiRetry(url).then(function (r) {
        if (append) _olderLoading = false;
        if (contactId !== _activeContactId) return;
        if (!r || !r.ok) return;
        var msgs = r.data.messages || [];
        // Seed dedupe set so a racing WS push (arriving after the HTTP
        // response for the same row — e.g. on reconnect backfill) is
        // skipped by _onMessage instead of producing a duplicate bubble.
        for (var i = 0; i < msgs.length; i++) _markSeen(msgs[i].id);
        if (append) {
          _threadMessages = _threadMessages.concat(msgs);
          if (msgs.length < OLDER_PAGE) _olderExhausted = true;
          _prependOlderBubbles(msgs);
        } else {
          _threadMessages = msgs;
          // A fresh fetch could also be short, but the initial limit
          // is the single source of "did we get all of them" — keep
          // the logic symmetrical.
          _olderExhausted = (msgs.length < OLDER_PAGE);
          _renderThread();
        }
      }, function () {
        if (append) _olderLoading = false;
      });
    }

    function _fetchContacts() {
      return apiRetry('/api/messages/contacts' + _qs()).then(function (r) {
        if (!r || !r.ok) return;
        _contacts = (r.data && r.data.contacts) || [];
        _renderDestSelect();
      }).catch(function (err) {
        console.error('[' + cfg.rootId + '] _fetchContacts:', err);
      });
    }

    function _fetchUnread() {
      return apiRetry('/api/messages/unread' + _qs()).then(function (r) {
        if (!r || !r.ok) return;
        _unreadCounts = (r.data && r.data.unread) || {};
        _updateUnreadUI();
      }).catch(function (err) {
        console.error('[' + cfg.rootId + '] _fetchUnread:', err);
      });
    }

    // Debounced server-truth reconciliation for unread counts.  Called
    // after an optimistic local increment on an inbound message — coalesces
    // bursts so a 20-message flurry triggers one fetch, not twenty.
    var _unreadReconcileTimer = null;
    function _scheduleUnreadReconcile() {
      if (_unreadReconcileTimer) clearTimeout(_unreadReconcileTimer);
      _unreadReconcileTimer = setTimeout(function () {
        _unreadReconcileTimer = null;
        _fetchUnread();
      }, 400);
    }

    var READ_GRACE_MS = 10000;
    function _addReadGrace(cid) {
      _readGrace[cid] = Date.now() + READ_GRACE_MS;
    }
    function _applyReadGrace(obj) {
      if (!obj) return;
      var now = Date.now();
      for (var k in _readGrace) {
        if (_readGrace[k] < now) {
          delete _readGrace[k];
        } else if (obj[k] !== undefined) {
          delete obj[k];
        }
      }
    }

    function _fetchChannels() {
      if (!cfg.supportsChannels) return;
      return _fetchChannelsShared().then(function (channels) {
        _channels = channels.filter(function (ch) { return ch.active; });
        _renderChannelSelect();
        // Channels drive the pinned per-channel broadcast rows.
        _renderConversations();
        _renderDestSelect();
      }).catch(function (err) {
        console.error('[' + cfg.rootId + '] _fetchChannels:', err);
      });
    }

    function _refresh() {
      // After the first WS-delivered event (or the bootstrap fetch in
      // _init), the panel's in-memory state is authoritative; skipping
      // the round-trip makes expanding a tab instant.  A WS disconnect
      // clears the flag via _resetFreshness so the next expand refetches.
      if (_hasFreshData && _expanded) {
        if (cfg.supportsChannels && _channels.length === 0) _fetchChannels();
        _renderConversations();
        _updateUnreadUI();
        if (_activeContactId) _renderThread();
        return;
      }
      if (_dom.convs && _conversations.length === 0) {
        _dom.convs.innerHTML = '<div class="msg-convs-loading">Loading…</div>';
      }
      _fetchConversations();
      _fetchUnread();
      _fetchContacts();
      _fetchChannels();
      if (_activeContactId) _fetchThread(_activeContactId);
    }

    function _resetFreshness() {
      _hasFreshData = false;
    }

    // ── Rendering ──────────────────────────────────────────────────
    function _renderConversations() {
      if (!_dom.convs) return;
      // When a search is active, the convs pane shows search results.
      // Skip re-rendering the conversation list on WS events / metrics
      // ticks so the user's results aren't wiped from under them, which
      // would also let the debounce cache no-op a retyped identical query.
      if (_dom.search && _dom.search.value.trim()) return;
      // Don't run the keyed-diff render from WS ticks when the panel is
      // collapsed and no conversations have been fetched yet — the empty
      // rows array would keep the container :empty, showing the CSS
      // "No conversations" placeholder and preventing the first real
      // fetch from populating it.
      if (!_expanded && _conversations.length === 0) return;
      var loading = _dom.convs.querySelector('.msg-convs-loading');
      if (loading) loading.remove();
      // Pin one broadcast row per active Meshtastic channel (Primary,
      // private channels, etc.).  Panels without channel support fall
      // back to a single broadcast row using the legacy id shape.
      var pinnedRows = [];
      var seen = {};
      var canon = _canonicalize(_channels);
      if (!cfg.broadcastLabel) {
        // No broadcast support — skip pinned broadcast rows entirely.
      } else if (cfg.supportsChannels && canon.canonical.length > 0) {
        for (var k = 0; k < canon.canonical.length; k++) {
          var ch = canon.canonical[k];
          var cid = _broadcastCid(ch.index);
          pinnedRows.push({
            contact_id: cid,
            msg_type: 'broadcast',
            contact_name: cfg.broadcastLabel || 'Broadcast',
            _channelIndex: ch.index,
            _channelName: _channelName(ch.index),
            last_text: '',
            last_ts: null,
            unread_count: 0,
            _pinned: true,
          });
          seen[cid] = pinnedRows.length - 1;
        }
      } else {
        var legacy = _broadcastCid(null);
        pinnedRows.push({
          contact_id: legacy,
          msg_type: 'broadcast',
          contact_name: cfg.broadcastLabel || 'Broadcast',
          last_text: '',
          last_ts: null,
          unread_count: _unreadCounts[legacy] || 0,
          _pinned: true,
        });
        seen[legacy] = 0;
      }

      var rows = pinnedRows.slice();
      for (var i = 0; i < _conversations.length; i++) {
        var c = _conversations[i];
        // Re-key aliased broadcast conversations onto their canonical
        // pinned row so duplicate same-PSK/same-name slots don't show
        // up as distinct conversations.
        var effectiveCid = c.contact_id;
        var parsedCh = _parseBroadcastChannel(c.contact_id);
        if (typeof parsedCh === 'number'
            && canon.aliasToCanonical.hasOwnProperty(parsedCh)) {
          effectiveCid = _broadcastCid(canon.aliasToCanonical[parsedCh]);
        }
        if (seen[effectiveCid] !== undefined) {
          var idx = seen[effectiveCid];
          // Use the newest preview across merged aliases.
          if (!rows[idx].last_ts
              || (c.last_ts && c.last_ts > rows[idx].last_ts)) {
            rows[idx].last_text = c.last_text;
            rows[idx].last_ts = c.last_ts;
          }
          rows[idx].unread_count = (rows[idx].unread_count || 0)
            + (c.unread_count || 0);
          continue;
        }
        // Includes DMs and any broadcast rows on channels no longer
        // active on this node — still worth surfacing so history isn't
        // orphaned.
        rows.push(c);
        seen[effectiveCid] = rows.length - 1;
      }

      // Layer canonical cid unread counts (from /api/messages/unread)
      // on top — handles the case where a conversation isn't yet in
      // _conversations but has unread tracked.
      for (var ci = 0; ci < pinnedRows.length; ci++) {
        var pcid = pinnedRows[ci].contact_id;
        if (!rows[ci].last_ts && _unreadCounts[pcid]) {
          rows[ci].unread_count = _unreadCounts[pcid];
        }
      }

      // Keyed-diff render: reuse existing nodes by data-contact so the
      // list doesn't flash on each update, CSS hover state is preserved,
      // and focus/selection are stable when a new message bumps a row.
      var existing = Object.create(null);
      for (var ei = 0; ei < _dom.convs.children.length; ei++) {
        var node = _dom.convs.children[ei];
        var key = node.getAttribute('data-contact');
        if (key) existing[key] = node;
      }
      for (var j = 0; j < rows.length; j++) {
        var r = rows[j];
        var isActive = r.contact_id === _activeContactId;
        var preview = (r.last_text || '').slice(0, 60);
        var timeStr = r.last_ts ? formatTimeAgo(r.last_ts) : '';
        var unread = _unreadCounts[r.contact_id] || r.unread_count || 0;
        var name = _displayName(r);
        var el = existing[r.contact_id];
        if (el) {
          delete existing[r.contact_id];
          el.classList.toggle('active', isActive);
          el.setAttribute('data-msgtype', r.msg_type || 'direct');
          var nameEl = el.querySelector('.msg-conv-name');
          if (nameEl && nameEl.textContent !== name) nameEl.textContent = name;
          var timeEl = el.querySelector('.msg-conv-time');
          if (timeEl && timeEl.textContent !== timeStr) timeEl.textContent = timeStr;
          var prevEl = el.querySelector('.msg-conv-preview');
          if (prevEl && prevEl.textContent !== preview) prevEl.textContent = preview;
          var badge = el.querySelector('.msg-conv-unread');
          if (unread > 0) {
            if (!badge) {
              badge = document.createElement('span');
              badge.className = 'msg-conv-unread';
              el.appendChild(badge);
            }
            var badgeText = String(unread);
            if (badge.textContent !== badgeText) badge.textContent = badgeText;
          } else if (badge) {
            badge.remove();
          }
        } else {
          el = document.createElement('div');
          el.className = 'msg-conv-item' + (isActive ? ' active' : '');
          el.setAttribute('data-contact', r.contact_id);
          el.setAttribute('data-msgtype', r.msg_type || 'direct');
          el.innerHTML =
            '<div class="msg-conv-body">'
            + '<div class="msg-conv-top">'
            + '<span class="msg-conv-name">' + esc(name) + '</span>'
            + '<span class="msg-conv-time">' + esc(timeStr) + '</span>'
            + '</div>'
            + '<div class="msg-conv-preview">' + esc(preview) + '</div>'
            + '</div>'
            + (unread > 0
                ? '<span class="msg-conv-unread">' + unread + '</span>' : '');
        }
        // appendChild on an in-tree node moves it, so rows land in order.
        _dom.convs.appendChild(el);
      }
      // Drop rows that no longer match any conversation.
      Object.keys(existing).forEach(function (k) { existing[k].remove(); });
    }

    function _displayName(conv) {
      if (conv.msg_type === 'broadcast') {
        var label = cfg.broadcastLabel || 'Broadcast';
        if (conv._channelName) return label + ': ' + conv._channelName;
        var chIdx = _parseBroadcastChannel(conv.contact_id);
        if (chIdx !== null) return label + ': ' + _channelName(chIdx);
        return label;
      }
      if (conv.contact_name && conv.contact_name !== conv.contact_id) {
        // Strip sub_transport suffix from display id tail, show clean name
        return conv.contact_name;
      }
      var id = conv.contact_id || '?';
      // Strip the __mqtt / __lora tail for display
      id = id.replace(/__(mqtt|lora)$/, '');
      if (id.length > 16) return id.substring(0, 8) + '…' + id.substring(id.length - 4);
      return id;
    }

    function _statusGlyph(status) {
      if (status === 'sent')
        return ' <span class="msg-status-sent">&#10003;</span>';
      if (status === 'broadcast_sent')
        return ' <span class="msg-status-broadcast-sent">&#10003;&#8226;</span>';
      if (status === 'delivered')
        return ' <span class="msg-status-delivered">&#10003;&#10003;</span>';
      if (status === 'read')
        return ' <span class="msg-status-read">&#10003;&#10003;</span>';
      if (status === 'queued')
        return ' <span class="msg-status-queued">&#9203;</span>';
      if (status === 'propagated')
        return ' <span class="msg-status-propagated">&#10003;</span>';
      if (status === 'timeout')
        return ' <span class="msg-status-timeout">&#10003;?</span>';
      if (status === 'expired')
        return ' <span class="msg-status-expired">&#10007;</span>';
      if (status === 'failed' || status === 'delivery_failed')
        return ' <span class="msg-status-failed">&#10007;</span>';
      return '';
    }

    function _hopsLabel(m) {
      var pl = m.metadata && m.metadata.path_len;
      if (pl === null || pl === undefined) return '';
      if (pl === 0) return 'direct';
      if (pl === 255) return 'flood';
      return pl + (pl === 1 ? ' hop' : ' hops');
    }

    function _snrLabel(m) {
      var snr = m.metadata && m.metadata.snr;
      if (snr === null || snr === undefined) return '';
      return Number(snr).toFixed(1) + ' dB';
    }

    function _reactionsHtml(m) {
      var reactions = m.metadata && m.metadata.reactions;
      if (!reactions || !reactions.length) return '';
      var counts = {};
      var order = [];
      for (var i = 0; i < reactions.length; i++) {
        var e = reactions[i].emoji;
        if (!counts[e]) { counts[e] = 0; order.push(e); }
        counts[e]++;
      }
      var html = '<div class="msg-reactions">';
      for (var j = 0; j < order.length; j++) {
        var em = order[j];
        html += '<span class="msg-reaction-badge">'
             + esc(em) + (counts[em] > 1 ? ' ' + counts[em] : '')
             + '</span>';
      }
      return html + '</div>';
    }

    function _bubbleHtml(m) {
      var isSent = m.direction === 'sent';
      var sender = isSent ? 'You' : (m.from_name || m.from_id || '?');
      var time = m.timestamp ? formatTimeAgo(m.timestamp) : '';
      var statusHtml = isSent && m.status ? _statusGlyph(m.status) : '';
      var hops = !isSent ? _hopsLabel(m) : '';
      var hopsHtml = hops
        ? '<span class="msg-hops">' + esc(hops) + '</span>' : '';
      var snr = !isSent ? _snrLabel(m) : '';
      var snrHtml = snr
        ? '<span class="msg-snr">' + esc(snr) + '</span>' : '';
      var chBadge = '';
      if (cfg.supportsChannels && m.msg_type !== 'broadcast'
          && m.metadata && m.metadata.channel !== undefined
          && m.metadata.channel !== null) {
        chBadge = '<span class="msg-channel-tag">'
               + esc(_channelName(m.metadata.channel)) + '</span>';
      }
      var idAttr = (m.id !== null && m.id !== undefined)
        ? ' data-msg-id="' + esc(String(m.id)) + '"' : '';
      var tsAttr = m.timestamp ? ' data-ts="' + m.timestamp + '"' : '';
      return '<div class="msg-bubble ' + (isSent ? 'sent' : 'received') + '"'
           + idAttr + tsAttr + '>'
           + '<div class="msg-meta"><span>' + esc(sender) + '</span>'
           + '<span>' + esc(time) + '</span>'
           + hopsHtml + snrHtml + chBadge
           + '<span class="msg-status">' + statusHtml + '</span>'
           + '</div>'
           + '<div class="msg-text">' + esc(m.text || '') + '</div>'
           + _reactionsHtml(m)
           + '</div>';
    }

    function _renderThread() {
      if (!_dom.chat) return;
      if (_threadMessages.length === 0) {
        if (_activeContactId) {
          _dom.chat.innerHTML =
            '<div class="msg-empty-notice">' +
            'No messages yet. Send one below to start the conversation.' +
            '</div>';
        } else {
          _dom.chat.innerHTML = '';
        }
        return;
      }
      var sorted = _threadMessages.slice().reverse();  // chronological
      var html = '';
      for (var i = 0; i < sorted.length; i++) {
        html += _bubbleHtml(sorted[i]);
      }
      var atBottom = (_dom.chat.scrollTop + _dom.chat.clientHeight
                      >= _dom.chat.scrollHeight - 40);
      _dom.chat.innerHTML = html;
      if (atBottom) _dom.chat.scrollTop = _dom.chat.scrollHeight;
    }

    function _appendBubbleToChat(row) {
      if (!_dom.chat) return;
      var placeholder = _dom.chat.querySelector('.msg-empty-notice');
      if (placeholder) placeholder.remove();
      var atBottom = (_dom.chat.scrollTop + _dom.chat.clientHeight
                      >= _dom.chat.scrollHeight - 40);
      var tmp = document.createElement('div');
      tmp.innerHTML = _bubbleHtml(row);
      var bubble = tmp.firstChild;
      if (!bubble) return;
      // Insert at the correct chronological position so out-of-order
      // arrivals (common with multi-hop mesh) display correctly.
      var ts = row.timestamp || 0;
      var inserted = false;
      var children = _dom.chat.children;
      for (var i = children.length - 1; i >= 0; i--) {
        var childTs = parseFloat(children[i].getAttribute('data-ts')) || 0;
        if (childTs <= ts) {
          var next = children[i + 1] || null;
          _dom.chat.insertBefore(bubble, next);
          inserted = true;
          break;
        }
      }
      if (!inserted) _dom.chat.insertBefore(bubble, children[0] || null);
      if (atBottom) _dom.chat.scrollTop = _dom.chat.scrollHeight;
    }

    function _prependOlderBubbles(msgs) {
      // Called with a page of older messages (server returns them
      // newest-first within the page).  Reverse so we can insert in
      // chronological order at the top, and anchor the scroll position
      // on the previously-topmost bubble so the viewport doesn't jump.
      if (!_dom.chat || !msgs || msgs.length === 0) return;
      var anchor = _dom.chat.firstChild;
      var anchorOffset = anchor ? anchor.offsetTop : 0;
      var frag = document.createDocumentFragment();
      for (var i = msgs.length - 1; i >= 0; i--) {
        var tmp = document.createElement('div');
        tmp.innerHTML = _bubbleHtml(msgs[i]);
        var bubble = tmp.firstChild;
        if (bubble) frag.appendChild(bubble);
      }
      _dom.chat.insertBefore(frag, _dom.chat.firstChild);
      // After prepend, the anchor's new offsetTop = (sum of inserted
      // heights) + old offset.  Keep the same visible offset under it.
      if (anchor) {
        var newAnchorOffset = anchor.offsetTop;
        _dom.chat.scrollTop += (newAnchorOffset - anchorOffset);
      }
    }

    function _onChatScroll() {
      if (!_dom.chat || !_activeContactId) return;
      if (_olderLoading || _olderExhausted) return;
      // Don't race the initial thread fetch — without this guard, a
      // stray scroll event while the prior thread's DOM is still on
      // screen fires _fetchThread(append=true) with an empty
      // _threadMessages, which issues a bogus no-`before` refetch in
      // parallel with the non-append initial fetch.
      if (_threadMessages.length === 0) return;
      // Trigger once the user is within ~1 viewport of the top — far
      // enough to feel smooth, close enough not to thrash on long
      // threads where scrollHeight >> clientHeight.
      if (_dom.chat.scrollTop > 60) return;
      _fetchThread(_activeContactId, true);
    }

    function _updateBubbleStatus(msgId, status) {
      if (!_dom.chat || msgId === null || msgId === undefined) return;
      var sel = '[data-msg-id="' + String(msgId).replace(/"/g, '\\"') + '"]';
      var bubble = _dom.chat.querySelector(sel);
      if (!bubble) return;
      var statusEl = bubble.querySelector('.msg-status');
      if (!statusEl) return;
      // _statusGlyph returns a safe static-HTML glyph (entities only, no
      // user data), so innerHTML here is not an XSS vector.
      statusEl.innerHTML = _statusGlyph(status);
    }

    function _renderChannelSelect() {
      if (!cfg.supportsChannels || !_dom.chSelect) return;
      var cur = _dom.chSelect.value;
      var html = '';
      var canonical = _canonicalize(_channels).canonical;
      for (var i = 0; i < canonical.length; i++) {
        var ch = canonical[i];
        var label = _channelName(ch.index);
        if (ch.psk_label === 'unencrypted') label += ' (open)';
        html += '<option value="' + ch.index + '">' + esc(label) + '</option>';
      }
      _dom.chSelect.innerHTML = html;
      if (cur) _dom.chSelect.value = cur;
      // Hide the selector when the node only has one active channel, or
      // while composing a new message — the destination picker's broadcast
      // options already encode the channel and mirror it onto this select.
      if (_dom.chSelectWrap) {
        var show = (canonical.length > 1) && !_newComposeOpen;
        _dom.chSelectWrap.style.display = show ? '' : 'none';
      }
    }

    function _renderDestSelect() {
      if (!_dom.destCombo) return;
      var bcLabel = cfg.broadcastLabel || 'Broadcast';
      var canonical = _canonicalize(_channels).canonical;
      _destOptions = [];
      if (!cfg.broadcastLabel) {
        // No broadcast support — omit broadcast destinations.
      } else if (cfg.supportsChannels && canonical.length > 0) {
        for (var k = 0; k < canonical.length; k++) {
          var ch = canonical[k];
          _destOptions.push({
            id: _broadcastCid(ch.index),
            type: 'broadcast',
            label: bcLabel + ': ' + _channelName(ch.index),
            channel: ch.index,
            group: 'broadcasts',
          });
        }
      } else {
        _destOptions.push({
          id: _broadcastCid(null),
          type: 'broadcast',
          label: bcLabel,
          group: 'broadcasts',
        });
      }
      if (cfg.transport === 'lxmf') {
        _destOptions.push({
          id: '__raw__', type: 'raw', label: 'Enter address…', group: 'other',
        });
      }
      for (var i = 0; i < _contacts.length; i++) {
        var c = _contacts[i];
        var nm = c.name || c.id;
        var hint = (c.name && c.id && c.name !== c.id)
          ? (c.id.length > 14 ? c.id.substring(0, 12) + '…' : c.id)
          : '';
        _destOptions.push({
          id: c.id, type: 'direct', label: nm, hint: hint, group: 'nodes',
        });
      }
      // Preserve a prior selection if the same id still exists; otherwise
      // fall back to the first available option (usually the first broadcast).
      var prev = _destSelection;
      _destSelection = null;
      if (prev) {
        for (var j = 0; j < _destOptions.length; j++) {
          if (_destOptions[j].id === prev.id) {
            _destSelection = _destOptions[j];
            break;
          }
        }
      }
      if (!_destSelection && _destOptions.length) {
        _destSelection = _destOptions[0];
      }
      if (_dom.destInput && _destSelection) {
        _dom.destInput.value = _destSelection.label;
      }
      _onDestChange();
      if (_dom.destCombo.classList.contains('open')) _renderDestList();
    }

    function _renderDestList() {
      if (!_dom.destList || !_dom.destInput) return;
      var raw = _dom.destInput.value || '';
      // When the input still matches the selected label, treat as
      // unfiltered — the list just opened.
      var q = (_destSelection && raw === _destSelection.label)
        ? '' : raw.trim().toLowerCase();
      var groups = ['broadcasts', 'other', 'nodes'];
      var groupLabels = {broadcasts: 'Broadcasts', other: '', nodes: 'Nodes'};
      var html = '';
      for (var gi = 0; gi < groups.length; gi++) {
        var g = groups[gi];
        var items = [];
        for (var i = 0; i < _destOptions.length; i++) {
          var o = _destOptions[i];
          if (o.group !== g) continue;
          if (q && g === 'nodes') {
            var hay = (o.label + ' ' + (o.hint || '') + ' ' + o.id).toLowerCase();
            if (hay.indexOf(q) < 0) continue;
          }
          items.push(o);
        }
        if (!items.length) continue;
        if (groupLabels[g]) {
          html += '<div class="msg-dest-group-label">'
               + esc(groupLabels[g]) + '</div>';
        }
        for (var k = 0; k < items.length; k++) {
          var it = items[k];
          var active = _destSelection && it.id === _destSelection.id;
          html += '<div class="msg-dest-item' + (active ? ' active' : '') + '"'
               + ' data-id="' + esc(it.id) + '">'
               + '<span class="dest-name">' + esc(it.label) + '</span>'
               + (it.hint
                    ? '<span class="dest-hint">' + esc(it.hint) + '</span>'
                    : '')
               + '</div>';
        }
      }
      if (!html) html = '<div class="msg-dest-empty">No matches</div>';
      _dom.destList.innerHTML = html;
      _positionDestList();
    }

    function _positionDestList() {
      if (!_dom.destCombo || !_dom.destInput || !_dom.destList) return;
      if (!_dom.destCombo.classList.contains('open')) return;
      var r = _dom.destInput.getBoundingClientRect();
      _dom.destList.style.top = r.bottom + 'px';
      _dom.destList.style.left = r.left + 'px';
      _dom.destList.style.width = r.width + 'px';
    }

    function _openDestList() {
      if (!_dom.destCombo) return;
      _dom.destCombo.classList.add('open');
      _renderDestList();
    }

    function _closeDestList() {
      if (!_dom.destCombo) return;
      _dom.destCombo.classList.remove('open');
      if (_destSelection && _dom.destInput) {
        _dom.destInput.value = _destSelection.label;
      }
    }

    function _pickDest(optId) {
      var picked = null;
      for (var i = 0; i < _destOptions.length; i++) {
        if (_destOptions[i].id === optId) { picked = _destOptions[i]; break; }
      }
      if (!picked) return;
      _destSelection = picked;
      if (_dom.destInput) _dom.destInput.value = picked.label;
      _closeDestList();
      _onDestChange();
      if (picked.type === 'raw' && _dom.lxmfRaw) _dom.lxmfRaw.focus();
    }

    function _onDestKeydown(e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        var first = _dom.destList && _dom.destList.querySelector('.msg-dest-item');
        if (first) _pickDest(first.getAttribute('data-id'));
      } else if (e.key === 'Escape') {
        e.preventDefault();
        _closeDestList();
      }
    }

    function _onDestChange() {
      var sel = _destSelection;
      if (_dom.lxmfRaw) {
        _dom.lxmfRaw.style.display = (sel && sel.type === 'raw') ? '' : 'none';
      }
      if (cfg.supportsChannels && _dom.chSelect
          && sel && sel.type === 'broadcast'
          && sel.channel !== undefined && sel.channel !== null) {
        _dom.chSelect.value = String(sel.channel);
      }
    }

    // ── Unread UI ──────────────────────────────────────────────────
    function _updateUnreadUI() {
      if (!_dom.unread) return;
      var total = 0;
      for (var k in _unreadCounts) {
        if (_unreadCounts.hasOwnProperty(k)) total += _unreadCounts[k];
      }
      _dom.unread.textContent = total > 0 ? total : '';
      // Re-render badges inside sidebar rows. Use .remove() (not display:none)
      // so the DOM state matches _renderConversations — otherwise the two
      // code paths leave badges in different states depending on which ran
      // last, which has produced subtle "ghost badge" bugs before.
      var items = _dom.convs ? _dom.convs.querySelectorAll('.msg-conv-item') : [];
      for (var i = 0; i < items.length; i++) {
        var cid = items[i].getAttribute('data-contact');
        var n = _unreadCounts[cid] || 0;
        var badge = items[i].querySelector('.msg-conv-unread');
        if (n > 0) {
          if (!badge) {
            badge = document.createElement('span');
            badge.className = 'msg-conv-unread';
            items[i].appendChild(badge);
          }
          badge.textContent = n;
        } else if (badge) {
          badge.remove();
        }
      }
    }

    // ── Conversation selection ─────────────────────────────────────
    function _selectConversation(contactId, msgType) {
      if (!contactId) return;
      _activeContactId = contactId;
      _activeMsgType = msgType || 'direct';
      _newComposeOpen = false;
      _threadMessages = [];
      // Fresh thread — reset pagination state so load-older can re-arm
      // once this fetch completes.
      _olderLoading = false;
      _olderExhausted = false;
      if (_dom.chat) {
        _dom.chat.innerHTML = '<div class="msg-loading">Loading\u2026</div>';
      }

      var items = _dom.convs
        ? _dom.convs.querySelectorAll('.msg-conv-item') : [];
      for (var i = 0; i < items.length; i++) {
        items[i].classList.toggle(
          'active',
          items[i].getAttribute('data-contact') === contactId,
        );
      }
      if (_dom.threadName) {
        var conv = null;
        for (var j = 0; j < _conversations.length; j++) {
          if (_conversations[j].contact_id === contactId) {
            conv = _conversations[j];
            break;
          }
        }
        _dom.threadName.textContent = conv
          ? _displayName(conv)
          : (_activeMsgType === 'broadcast'
              ? (cfg.broadcastLabel || 'Broadcast')
              : _displayName({contact_id: contactId}));
      }
      if (_dom.compose) _dom.compose.classList.remove('hidden');
      if (_dom.newCompose) _dom.newCompose.classList.add('hidden');
      if (_dom.deleteBtn) _dom.deleteBtn.classList.remove('hidden');

      // Broadcast threads imply a channel — lock the selector to it
      // and disable editing (it's informational only for these threads).
      // String-keyed threads come from MQTT peers on channels we don't
      // have locally; we can't transmit on them, so disable the selector
      // and leave the value alone.
      if (cfg.supportsChannels && _dom.chSelect) {
        var chKey = (_activeMsgType === 'broadcast')
          ? _parseBroadcastChannel(_activeContactId) : null;
        if (typeof chKey === 'number') {
          _dom.chSelect.value = String(chKey);
          _dom.chSelect.disabled = true;
        } else if (typeof chKey === 'string') {
          _dom.chSelect.disabled = true;
        } else {
          _dom.chSelect.disabled = false;
        }
        _renderChannelSelect();
      }

      var layout = _dom.body && _dom.body.querySelector('.msg-layout');
      if (layout) layout.classList.add('thread-active');

      _fetchThread(contactId).then(function () {
        if (cfg.supportsChannels && _dom.chSelect
            && !_dom.chSelect.disabled && _threadMessages.length > 0) {
          var lastCh = null;
          for (var ti = 0; ti < _threadMessages.length; ti++) {
            var md = _threadMessages[ti].metadata;
            if (md && md.channel !== undefined && md.channel !== null) {
              lastCh = md.channel;
              break;
            }
          }
          if (lastCh !== null) _dom.chSelect.value = String(lastCh);
        }
      });

      // Collect every cid whose unread feeds this row. Broadcast rows
      // merge aliased channel slots (same name + PSK), so clicking the
      // canonical row must also clear the aliases or the badge re-sums
      // from _conversations on the next render.
      var contributors = [contactId];
      var parsedCh = _parseBroadcastChannel(contactId);
      if (typeof parsedCh === 'number' && _channels.length > 0) {
        var canon = _canonicalize(_channels);
        for (var aliasIdx in canon.aliasToCanonical) {
          if (canon.aliasToCanonical.hasOwnProperty(aliasIdx)
              && canon.aliasToCanonical[aliasIdx] === parsedCh) {
            contributors.push(_broadcastCid(parseInt(aliasIdx, 10)));
          }
        }
      }

      var anyCleared = false;
      for (var ci = 0; ci < contributors.length; ci++) {
        var cid = contributors[ci];
        var convUnread = 0;
        var convIdx = -1;
        for (var k = 0; k < _conversations.length; k++) {
          if (_conversations[k].contact_id === cid) {
            convIdx = k;
            convUnread = _conversations[k].unread_count || 0;
            break;
          }
        }
        var wasUnread = (_unreadCounts[cid] || 0) > 0 || convUnread > 0;
        if (_unreadCounts[cid]) delete _unreadCounts[cid];
        // Zero the server-cached count too. _renderConversations falls
        // back to `r.unread_count` when _unreadCounts is missing the key,
        // which otherwise re-shows a stale badge on the next tick.
        if (convIdx >= 0) _conversations[convIdx].unread_count = 0;
        if (wasUnread) {
          anyCleared = true;
          _addReadGrace(cid);
          api('/api/messages/read', {
            method: 'POST',
            json: {contact_id: cid},
          });
        }
      }
      if (anyCleared) _updateUnreadUI();
    }

    function _goBack() {
      var layout = _dom.body && _dom.body.querySelector('.msg-layout');
      if (layout) layout.classList.remove('thread-active');
      if (_dom.deleteBtn) _dom.deleteBtn.classList.add('hidden');
      // Clear the active contact so _onMessage doesn't keep treating the
      // (now hidden) thread as "open" — without this, inbound messages
      // for the previously-viewed peer get auto-marked-read and their
      // bubbles are appended into an invisible chat pane.
      _activeContactId = null;
      _activeMsgType = null;
    }

    function _onDeleteConversation() {
      if (!_activeContactId) return;
      var name = _dom.threadName ? _dom.threadName.textContent : 'this conversation';
      R.confirmDestructive(
        'Delete conversation',
        'Delete all messages in "' + name + '"? This cannot be undone.',
        'Delete messages'
      ).then(function(confirmed) {
        if (!confirmed || !_activeContactId) return;
        var cid = _activeContactId;
        _deletePending = cid;
        _addReadGrace(cid);
        api('/api/messages/conversation/' + encodeURIComponent(cid), {method: 'DELETE'})
          .then(function (r) {
            if (!r || !r.ok) {
              _deletePending = null;
              _setFeedback((r && r.error) || 'Delete failed', 'error');
              return;
            }
            var deleted = (r.data && r.data.deleted) || 0;
            _threadMessages = [];
            if (_dom.chat) {
              _dom.chat.innerHTML = '<div class="msg-deleted-notice">Deleted ' +
                deleted + ' message' + (deleted === 1 ? '' : 's') +
                ' from &ldquo;' + esc(name) + '&rdquo;</div>';
            }
            if (_unreadCounts[cid]) {
              delete _unreadCounts[cid];
              _updateUnreadUI();
            }
            _conversations = _conversations.filter(function (c) {
              return c.contact_id !== cid;
            });
            _renderConversations();
            _activeContactId = null;
            _activeMsgType = null;
            if (_dom.threadName) _dom.threadName.textContent = 'Select a conversation';
            if (_dom.compose) _dom.compose.classList.add('hidden');
            if (_dom.deleteBtn) _dom.deleteBtn.classList.add('hidden');
            _goBack();
            Promise.all([_fetchConversations(), _fetchUnread()])
              .then(function () { _deletePending = null; })
              .catch(function () { _deletePending = null; });
          }).catch(function () { _deletePending = null; });
      });
    }

    function _toggleNewCompose() {
      _newComposeOpen = !_newComposeOpen;
      if (_newComposeOpen) {
        _activeContactId = null;
        _activeMsgType = null;
        _threadMessages = [];
        _destSelection = null;
        if (_dom.destCombo) _dom.destCombo.classList.remove('open');
        if (_dom.chat) _dom.chat.innerHTML = '';
        if (_dom.threadName) _dom.threadName.textContent = 'New Message';
        // Show both: destination picker (newCompose) and the shared textarea (compose)
        if (_dom.compose) _dom.compose.classList.remove('hidden');
        if (_dom.newCompose) _dom.newCompose.classList.remove('hidden');
        if (_dom.deleteBtn) _dom.deleteBtn.classList.add('hidden');
        // No active broadcast thread while composing — selector unlocks.
        if (cfg.supportsChannels && _dom.chSelect) _dom.chSelect.disabled = false;
        _renderChannelSelect();
        var items = _dom.convs
          ? _dom.convs.querySelectorAll('.msg-conv-item') : [];
        for (var i = 0; i < items.length; i++) items[i].classList.remove('active');
        var layout = _dom.body && _dom.body.querySelector('.msg-layout');
        if (layout) layout.classList.add('thread-active');
        _fetchContacts();
      } else {
        if (_dom.newCompose) _dom.newCompose.classList.add('hidden');
        if (_dom.threadName) _dom.threadName.textContent = 'Select a conversation';
        _renderChannelSelect();
      }
    }

    // ── Send ───────────────────────────────────────────────────────
    function _resolveSendTarget() {
      if (_newComposeOpen) {
        var sel = _destSelection;
        if (!sel) return null;
        if (sel.type === 'raw') {
          var raw = _dom.lxmfRaw ? _dom.lxmfRaw.value.trim() : '';
          return raw ? {destination: raw, msgType: 'direct'} : null;
        }
        if (sel.type === 'broadcast') {
          return {destination: 'broadcast', msgType: 'broadcast'};
        }
        return {destination: sel.id, msgType: 'direct'};
      }
      if (!_activeContactId) return null;
      if (_activeMsgType === 'broadcast') {
        return {destination: 'broadcast', msgType: 'broadcast'};
      }
      // Strip sub_transport suffix to recover raw peer id for send
      var peer = _activeContactId.replace(/__(mqtt|lora)$/, '');
      return {destination: peer, msgType: 'direct'};
    }

    function _sendResolved(target, text) {
      var body = {
        transport: cfg.transport,
        text: text,
        destination: target.destination,
      };
      if (target.msgType === 'broadcast') body.msg_type = 'broadcast';
      if (cfg.subTransport) body.sub_transport = cfg.subTransport;
      if (cfg.supportsChannels) {
        var chFromThread = null;
        if (_newComposeOpen && _destSelection
            && _destSelection.channel !== undefined
            && _destSelection.channel !== null) {
          chFromThread = _destSelection.channel;
        } else if (_activeContactId && _activeMsgType === 'broadcast') {
          chFromThread = _parseBroadcastChannel(_activeContactId);
        }
        if (chFromThread !== null) {
          body.channel = chFromThread;
        } else if (_dom.chSelect) {
          body.channel = parseInt(_dom.chSelect.value, 10);
        }
      }

      api('/api/messages/send', {method: 'POST', json: body})
        .then(function (r) {
          if (!r) { _setFeedback('Network error', 'error'); return; }
          if (!r.ok) { _setFeedback(r.error || 'Send failed', 'error'); return; }
          if (!r.data.sent) {
            _setFeedback('Not sent: ' + (r.data.reason || 'unknown'), 'error');
            return;
          }
          _dom.text.value = '';
          _autoGrow(_dom.text);
          _updateByteCount();
          _setFeedback(r.data.truncated ? 'Sent (truncated)' : 'Sent', 'ok');
          if (_newComposeOpen) {
            _newComposeOpen = false;
            if (_dom.newCompose) _dom.newCompose.classList.add('hidden');
            _renderChannelSelect();
          }
        })
        .finally(function () {
          _sending = false;
          if (_dom.sendBtn) _dom.sendBtn.disabled = false;
        });
    }

    function _onSend() {
      if (_sending) return;
      var target = _resolveSendTarget();
      if (!_dom.text) return;
      var text = _dom.text.value.trim();
      if (!text || !target) return;
      _sending = true;
      if (_dom.sendBtn) _dom.sendBtn.disabled = true;
      _setFeedback('Sending…', '');

      var confirmation = Promise.resolve(true);
      if (_maxBytes && new TextEncoder().encode(text).length > _maxBytes) {
        confirmation = R.confirmDestructive(
          'Send truncated message',
          'This message exceeds ' + _maxBytes + ' bytes and will be truncated.',
          'Send anyway'
        );
      }
      confirmation.then(function(confirmed) {
        if (!confirmed) {
          _sending = false;
          if (_dom.sendBtn) _dom.sendBtn.disabled = false;
          _setFeedback('', '');
          return;
        }
        _sendResolved(target, text);
      }).catch(function() {
        _sending = false;
        if (_dom.sendBtn) _dom.sendBtn.disabled = false;
        _setFeedback('Confirmation failed', 'error');
      });
    }

    function _onInsertGps() {
      if (!_dom.text) return;
      var fix = (R.getLastGpsFix && R.getLastGpsFix()) || null;
      if (!fix || fix.lat == null || fix.lon == null) {
        _setFeedback('No GPS fix available', 'error');
        return;
      }
      var lat = Number(fix.lat).toFixed(5);
      var lon = Number(fix.lon).toFixed(5);
      var parts = ['GPS ' + lat + ',' + lon];
      if (fix.alt_m != null) parts.push('alt ' + Math.round(fix.alt_m) + 'm');
      parts.push('https://www.openstreetmap.org/?mlat=' + lat
                 + '&mlon=' + lon + '&zoom=15');
      var snippet = parts.join(' ');
      var cur = _dom.text.value;
      _dom.text.value = cur && !cur.endsWith(' ') && !cur.endsWith('\n')
        ? cur + ' ' + snippet
        : cur + snippet;
      _autoGrow(_dom.text);
      _updateByteCount();
      _dom.text.focus();
      _setFeedback('GPS inserted', 'ok');
    }

    function _setFeedback(text, cls) {
      if (!_dom.feedback) return;
      _dom.feedback.textContent = text;
      _dom.feedback.className = 'msg-feedback' + (cls ? ' ' + cls : '');
      if (cls === 'ok') {
        setTimeout(function () {
          if (_dom.feedback.textContent === text) _dom.feedback.textContent = '';
        }, 3000);
      }
    }

    function _updateByteCount() {
      if (!_dom.byteCount || !_dom.text) return;
      var text = _dom.text.value;
      var bytes = new TextEncoder().encode(text).length;
      if (_maxBytes) {
        var warn = Math.floor(_maxBytes * 0.85);
        _dom.byteCount.textContent = bytes + '/' + _maxBytes;
        _dom.byteCount.className = 'msg-byte-count'
          + (bytes > _maxBytes ? ' over' : bytes > warn ? ' near' : '');
      } else {
        _dom.byteCount.textContent = '';
      }
      if (_dom.sendBtn && !_sending) _dom.sendBtn.disabled = !text.trim();
    }

    function _autoGrow(el) {
      if (!el) return;
      el.style.height = 'auto';
      el.style.height = Math.min(el.scrollHeight, 96) + 'px';
    }

    // ── Search ─────────────────────────────────────────────────────
    var _lastSearchQuery = null;   // tracks last query that hit the server
    function _onSearch() {
      if (!_dom.search) return;
      var q = _dom.search.value.trim();
      if (!q) {
        if (_lastSearchQuery !== null) {
          _lastSearchQuery = null;
          // Cleared — re-render from in-memory conversations instead of
          // re-fetching; they're kept fresh by the per-message WS events.
          if (_hasFreshData) _renderConversations();
          else _fetchConversations();
        }
        return;
      }
      if (q === _lastSearchQuery) return;  // debounced input sent the same query
      _lastSearchQuery = q;
      var extra = ['q=' + encodeURIComponent(q), 'limit=30'];
      apiRetry('/api/messages/search' + _qs(extra)).then(function (r) {
        if (!r || !r.ok) return;
        // Stale result check: user typed further and _lastSearchQuery
        // has already moved on — drop this render to avoid flicker.
        if (_lastSearchQuery !== q) return;
        _renderSearchResults(r.data.messages || [], q);
      });
    }

    function _renderSearchResults(messages, q) {
      if (!_dom.convs) return;
      if (messages.length === 0) {
        _dom.convs.innerHTML =
          '<div class="msg-dest-empty">No results for "' + esc(q) + '"</div>';
        return;
      }
      var re = new RegExp(
        '(' + q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi',
      );
      var html = '';
      for (var i = 0; i < messages.length; i++) {
        var m = messages[i];
        var sender = m.direction === 'sent'
          ? 'You' : (m.from_name || m.from_id || '?');
        var preview = (m.text || '').substring(0, 80);
        var marked = esc(preview).replace(re, '<mark>$1</mark>');
        var time = m.timestamp ? formatTimeAgo(m.timestamp) : '';
        html += '<div class="msg-conv-item"'
             + ' data-contact="' + esc(m.contact_id || '') + '"'
             + ' data-msgtype="' + esc(m.msg_type || 'direct') + '">'
             + '<div class="msg-conv-body">'
             + '<div class="msg-conv-top">'
             + '<span class="msg-conv-name">' + esc(sender) + '</span>'
             + '<span class="msg-conv-time">' + esc(time) + '</span>'
             + '</div>'
             + '<div class="msg-conv-preview">' + marked + '</div>'
             + '</div></div>';
      }
      _dom.convs.innerHTML = html;
    }

    // ── WebSocket update entry ─────────────────────────────────────
    // Metrics tick (type: "update") now carries only the slow-moving
    // messaging state (transports + unread totals).  Per-message deltas
    // arrive via the dedicated `message` / `message_status` envelopes
    // dispatched to onMessage / onStatus below, which append in place
    // instead of triggering a full thread refetch.
    function update(wsPayload) {
      if (!wsPayload) return;
      if (!_resolveDom()) return;

      if (wsPayload.transports) {
        var avail = wsPayload.transports.some(function (t) {
          return t.name === cfg.transport;
        });
        if (avail !== _available) {
          _available = avail;
          if (_dom.section) {
            _dom.section.style.display = avail ? '' : 'none';
          }
        }
      }
      // Unread dict is the authoritative across-session badge source;
      // re-render counts without round-tripping the per-panel endpoint.
      // The server sends a nested map keyed by transport[:sub_transport];
      // pick the bucket for this panel so sibling panels don't contaminate
      // our badge totals.
      if (wsPayload.unread) {
        if (_unreadReconcileTimer) {
          clearTimeout(_unreadReconcileTimer);
          _unreadReconcileTimer = null;
        }
        var bucketKey = cfg.subTransport
          ? cfg.transport + ':' + cfg.subTransport
          : cfg.transport;
        var bucket = wsPayload.unread[bucketKey];
        if (bucket && typeof bucket === 'object') {
          _applyReadGrace(bucket);
          if (_deletePending && bucket[_deletePending] !== undefined) {
            delete bucket[_deletePending];
          }
          _unreadCounts = bucket;
        } else if (
          !wsPayload.unread.hasOwnProperty(bucketKey)
          && _isFlatUnreadMap(wsPayload.unread)
        ) {
          // Backwards compat: older server sends a flat {cid: count}.
          _applyReadGrace(wsPayload.unread);
          _unreadCounts = wsPayload.unread;
        } else {
          _unreadCounts = {};
        }
        _updateUnreadUI();
        try { _renderConversations(); } catch (err) {
          console.error('[' + cfg.rootId + '] render in update():', err);
        }
      }
      if (wsPayload.conversations) {
        var mine = wsPayload.conversations.filter(function (c) {
          if (c.transport !== cfg.transport) return false;
          if (cfg.subTransport !== null && cfg.subTransport !== undefined) {
            if ((c.sub_transport || '') !== cfg.subTransport) return false;
          }
          return true;
        });
        if (_deletePending) {
          mine = mine.filter(function (c) {
            return c.contact_id !== _deletePending;
          });
        }
        if (mine.length > 0 || _conversations.length === 0) {
          var graceNow = Date.now();
          for (var mi = 0; mi < mine.length; mi++) {
            var gc = mine[mi].contact_id;
            if (_readGrace[gc] && _readGrace[gc] >= graceNow) {
              mine[mi].unread_count = 0;
            }
          }
          _conversations = mine;
          _hasFreshData = true;
          _renderConversations();
        }
      }
    }

    function _isFlatUnreadMap(obj) {
      for (var k in obj) {
        if (obj.hasOwnProperty(k)) return typeof obj[k] === 'number';
      }
      return false;
    }

    // ── Per-message WebSocket events ───────────────────────────────
    function _matchesPanel(row) {
      if (!row || row.transport !== cfg.transport) return false;
      if (cfg.subTransport !== null && cfg.subTransport !== undefined) {
        if ((row.sub_transport || '') !== cfg.subTransport) return false;
      }
      return true;
    }

    function _upsertConversation(row) {
      if (!row || !row.contact_id) return;
      if (_deletePending && row.contact_id === _deletePending) return;
      var cid = row.contact_id;
      var idx = -1;
      for (var i = 0; i < _conversations.length; i++) {
        if (_conversations[i].contact_id === cid) { idx = i; break; }
      }
      if (idx < 0) {
        _conversations.unshift({
          contact_id: cid,
          contact_name: (row.direction === 'sent'
            ? (row.to_name || row.to_id || '')
            : (row.from_name || row.from_id || '')),
          transport: row.transport,
          sub_transport: row.sub_transport || '',
          msg_type: row.msg_type || 'direct',
          last_text: row.text || '',
          last_ts: row.timestamp,
          unread_count: 0,
        });
        return;
      }
      var entry = _conversations[idx];
      var bumped = !entry.last_ts || row.timestamp >= entry.last_ts;
      if (bumped) {
        entry.last_text = row.text || '';
        entry.last_ts = row.timestamp;
      }
      if (!entry.contact_name) {
        entry.contact_name = (row.direction === 'sent'
          ? (row.to_name || row.to_id || '')
          : (row.from_name || row.from_id || ''));
      }
      // Float to top so the list order matches the server's last_ts-desc
      // sort without a refetch.  Skip when the incoming row is older
      // than what we already had (happens on out-of-order delivery).
      if (bumped && idx > 0) {
        _conversations.splice(idx, 1);
        _conversations.unshift(entry);
      }
    }

    function _onMessage(row) {
      if (!_matchesPanel(row)) return;
      if (_markSeen(row.id)) return;
      if (_deletePending && row.contact_id === _deletePending) return;
      if (!_resolveDom()) return;

      var isReceived = row.direction === 'received';
      var activeMatch = _activeContactId && row.contact_id === _activeContactId;

      if (activeMatch && _expanded) {
        // Insert into newest-first array at the sorted position so
        // out-of-order arrivals stay chronologically correct.
        var ts = row.timestamp || 0;
        var insertIdx = 0;
        for (var j = 0; j < _threadMessages.length; j++) {
          if ((_threadMessages[j].timestamp || 0) >= ts) { insertIdx = j + 1; }
          else break;
        }
        _threadMessages.splice(insertIdx, 0, row);
        _appendBubbleToChat(row);
        if (isReceived) {
          // Always POST /read, not just when the local unread map
          // already has this cid.  The backend increments stored
          // unread on receive, but our local dict won't reflect it
          // until the next metrics tick — without this POST the
          // conversation flashes a stale badge on the next tick.
          if (_unreadCounts[row.contact_id]) {
            delete _unreadCounts[row.contact_id];
            for (var ci = 0; ci < _conversations.length; ci++) {
              if (_conversations[ci].contact_id === row.contact_id) {
                _conversations[ci].unread_count = 0;
                break;
              }
            }
            _updateUnreadUI();
          }
          _addReadGrace(row.contact_id);
          api('/api/messages/read', {
            method: 'POST',
            json: {contact_id: row.contact_id},
          });
        }
      } else if (isReceived) {
        // Optimistic local bump for instant badge feedback, then reconcile
        // against the server's authoritative count shortly after.  Without
        // the reconcile we can race the next metrics tick and inflate the
        // badge when the tick arrives pre-push and our increment applies
        // on top of an already-current count.
        _unreadCounts[row.contact_id] =
          (_unreadCounts[row.contact_id] || 0) + 1;
        _updateUnreadUI();
        _scheduleUnreadReconcile();
      }

      _upsertConversation(row);
      _renderConversations();
    }

    function _onStatus(row) {
      if (!_matchesPanel(row)) return;
      if (_deletePending && row.contact_id === _deletePending) return;
      if (!_resolveDom()) return;
      if (row.id === null || row.id === undefined) return;
      var changed = false;
      for (var i = 0; i < _threadMessages.length; i++) {
        if (String(_threadMessages[i].id) === String(row.id)) {
          _threadMessages[i].status = row.status;
          changed = true;
          break;
        }
      }
      if (changed) _updateBubbleStatus(row.id, row.status);
    }

    function _onReaction(data) {
      if (!_matchesPanel(data)) return;
      if (_deletePending && data.contact_id === _deletePending) return;
      if (!_resolveDom()) return;
      if (data.id === null || data.id === undefined) return;
      for (var i = 0; i < _threadMessages.length; i++) {
        if (String(_threadMessages[i].id) === String(data.id)) {
          if (!_threadMessages[i].metadata) _threadMessages[i].metadata = {};
          _threadMessages[i].metadata.reactions = data.reactions || [];
          break;
        }
      }
      if (!_dom.chat) return;
      var sel = '[data-msg-id="' + String(data.id).replace(/"/g, '\\"') + '"]';
      var bubble = _dom.chat.querySelector(sel);
      if (!bubble) return;
      var existing = bubble.querySelector('.msg-reactions');
      if (existing) existing.remove();
      var tmp = document.createElement('div');
      tmp.innerHTML = _reactionsHtml({metadata: {reactions: data.reactions || []}});
      var newEl = tmp.firstChild;
      if (newEl) bubble.appendChild(newEl);
    }

    function _renderTransportAddress(addr) {
      if (!_dom || !_dom.toggle || !addr) return;
      var el = _dom.toggle.querySelector('.msg-transport-address');
      if (!el) {
        el = document.createElement('span');
        el.className = 'msg-transport-address';
        el.title = 'Click to copy address';
        el.addEventListener('click', function (e) {
          e.stopPropagation();
          if (navigator.clipboard) {
            navigator.clipboard.writeText(addr).then(function () {
              var prev = el.textContent;
              el.textContent = 'Copied';
              setTimeout(function () { el.textContent = prev; }, 900);
            });
          }
        });
        _dom.toggle.appendChild(el);
      }
      el.textContent = '<' + addr + '>';
    }

    // ── Section-availability bootstrap ─────────────────────────────
    // Even before the first WS tick, hide the section until we know the
    // transport is registered — avoids showing empty panels on load.
    function _init() {
      if (!_resolveDom()) {
        // DOM isn't there yet (script loaded before HTML injection).
        // Retry once on next frame.
        setTimeout(_init, 100);
        return;
      }
      _fetchTransportsShared().then(function (list) {
        var entry = null;
        for (var i = 0; i < list.length; i++) {
          if (list[i].name === cfg.transport) { entry = list[i]; break; }
        }
        _available = !!entry;
        _maxBytes = (entry && entry.max_message_bytes) || null;
        if (_dom.section) _dom.section.style.display = _available ? '' : 'none';
        if (entry && entry.address) _renderTransportAddress(entry.address);
        if (_available && !_hasFreshData) _fetchConversations();
      });
      if (!_hasFreshData) _fetchUnread();
    }

    // Register for cross-panel channel refresh so join/delete from the
    // shared dialog updates this panel's selector without a hard reload.
    if (cfg.supportsChannels) {
      R._channelRefreshHooks = R._channelRefreshHooks || [];
      R._channelRefreshHooks.push(_fetchChannels);
    }

    _init();

    function _onConversationDeleted(data) {
      if (!data || !data.contact_id) return;
      var cid = data.contact_id;
      var had = false;
      _conversations = _conversations.filter(function (c) {
        if (c.contact_id === cid) { had = true; return false; }
        return true;
      });
      if (!had) return;
      if (_unreadCounts[cid]) {
        delete _unreadCounts[cid];
        _updateUnreadUI();
      }
      if (_activeContactId === cid) {
        _activeContactId = null;
        _activeMsgType = null;
        _threadMessages = [];
        if (_dom.chat) {
          _dom.chat.innerHTML = '<div class="msg-deleted-notice">'
            + 'Conversation deleted</div>';
        }
        if (_dom.compose) _dom.compose.classList.add('hidden');
        if (_dom.deleteBtn) _dom.deleteBtn.classList.add('hidden');
        _goBack();
      }
      _renderConversations();
    }

    // Public surface
    var panelApi = {
      update: update,
      onMessage: _onMessage,
      onStatus: _onStatus,
      onReaction: _onReaction,
      onConversationDeleted: _onConversationDeleted,
      resetFreshness: _resetFreshness,
    };
    _allPanels.push(panelApi);
    return panelApi;
  }

  R.createMessagesPanel = createMessagesPanel;

  // Fan out a single per-message WS envelope to every registered
  // panel; each panel filters by its own transport/sub_transport and
  // no-ops if the row isn't relevant.
  R.onMessagingEvent = function (row) {
    for (var i = 0; i < _allPanels.length; i++) {
      try { _allPanels[i].onMessage(row); } catch (e) { /* keep other panels alive */ }
    }
  };
  R.onMessagingStatus = function (row) {
    for (var i = 0; i < _allPanels.length; i++) {
      try { _allPanels[i].onStatus(row); } catch (e) { /* keep other panels alive */ }
    }
  };
  R.onMessagingReaction = function (data) {
    for (var i = 0; i < _allPanels.length; i++) {
      try { _allPanels[i].onReaction(data); } catch (e) { /* keep other panels alive */ }
    }
  };
  R.onConversationDeleted = function (data) {
    for (var i = 0; i < _allPanels.length; i++) {
      try { _allPanels[i].onConversationDeleted(data); } catch (e) { /* keep other panels alive */ }
    }
  };
  // Called from app.js on ws.onclose so that the next _refresh() on
  // each panel falls back to a real fetch instead of trusting stale
  // in-memory state.
  R.onMessagingConnectionLost = function () {
    for (var i = 0; i < _allPanels.length; i++) {
      try { _allPanels[i].resetFreshness(); } catch (e) { /* ignore */ }
    }
  };

  // ── Shared channel-management dialog ───────────────────────────
  // Only one dialog instance is needed on the page; both Meshtastic
  // panels trigger it via R.openChannelDialog().  Logic was trimmed
  // from the old messages.js.
  R.openChannelDialog = function () {
    var overlay = $('channel-dialog-overlay');
    if (!overlay) return;
    if (typeof overlay.showModal === 'function') overlay.showModal();
    else overlay.style.display = 'flex';
    // Always show fresh state when the dialog opens — a stale cache
    // here would mislead the user about what's currently configured.
    _invalidateChannels();
    _refreshChannelList();
    var firstField = $('channel-join-url');
    if (firstField) firstField.focus();
  };

  function _refreshChannelList() {
    _fetchChannelsShared().then(function (all) {
      var listEl = $('channel-list');
      if (!listEl) return;
      var html = '';
      for (var i = 0; i < all.length; i++) {
        var ch = all[i];
        if (ch.role === 'DISABLED') continue;
        var label = ch.name || (ch.index === 0 ? 'Primary' : 'Ch ' + ch.index);
        html += '<div class="channel-row">'
             + '<span class="channel-name">' + esc(label) + '</span>'
             + '<span class="channel-meta">' + esc(ch.role)
             + ' · ' + esc(ch.psk_label || '') + '</span>'
             + (ch.role === 'SECONDARY'
                  ? '<button class="channel-delete-btn" data-index="'
                    + ch.index + '" title="Leave channel">&times;</button>'
                  : '')
             + '</div>';
      }
      if (!html) {
        html = '<div class="channel-row">'
             + '<span class="channel-meta">No channels configured</span></div>';
      }
      listEl.innerHTML = html;
    });
  }

  function _closeChannelDialog() {
    var o = $('channel-dialog-overlay');
    if (o) {
      if (typeof o.close === 'function' && o.open) o.close();
      else o.style.display = 'none';
    }
    ['channel-join-url', 'channel-join-name', 'channel-join-psk'].forEach(
      function (x) { var el = $(x); if (el) el.value = ''; },
    );
    var fb = $('channel-join-feedback');
    if (fb) { fb.textContent = ''; fb.className = 'msg-feedback'; }
  }

  var _channelOpPending = false;

  function _joinChannel() {
    if (_channelOpPending) return;
    var url = ($('channel-join-url') || {}).value || '';
    var name = ($('channel-join-name') || {}).value || '';
    var psk = ($('channel-join-psk') || {}).value || '';
    var fb = $('channel-join-feedback');
    url = url.trim(); name = name.trim(); psk = psk.trim();
    if (!url && !name) {
      if (fb) { fb.textContent = 'Enter URL or name + PSK'; fb.className = 'msg-feedback error'; }
      return;
    }
    _channelOpPending = true;
    var body = url ? {url: url} : {name: name, psk: psk || 'default'};
    if (fb) { fb.textContent = 'Joining…'; fb.className = 'msg-feedback'; }
    api('/api/meshtastic/channels/join', {method: 'POST', json: body}).then(
      function (r) {
        _channelOpPending = false;
        if (!r || !r.ok) {
          if (fb) {
            fb.textContent = (r && r.error) || 'Join failed';
            fb.className = 'msg-feedback error';
          }
          return;
        }
        if (fb) { fb.textContent = 'Joined!'; fb.className = 'msg-feedback ok'; }
        _notifyChannelChange();
        _refreshChannelList();
      },
    ).catch(function () { _channelOpPending = false; });
  }

  function _deleteChannel(idx) {
    if (_channelOpPending) return;
    R.confirmDestructive(
      'Leave Meshtastic channel',
      'Leave this channel? Its pre-shared key may not be recoverable.',
      'Leave channel'
    ).then(function(confirmed) {
      if (!confirmed) return;
      _channelOpPending = true;
      var fb = $('channel-join-feedback');
      api('/api/meshtastic/channels/' + idx, {method: 'DELETE'}).then(
        function (r) {
          _channelOpPending = false;
          if (!r || !r.ok) {
            if (fb) {
              fb.textContent = (r && r.error) || 'Delete failed';
              fb.className = 'msg-feedback error';
            }
            return;
          }
          _notifyChannelChange();
          _refreshChannelList();
        },
      ).catch(function () { _channelOpPending = false; });
    });
  }

  function _notifyChannelChange() {
    // Invalidate first so each panel's hook re-fetches live data
    // instead of replaying the cached list from before the change.
    _invalidateChannels();
    var hooks = R._channelRefreshHooks || [];
    for (var i = 0; i < hooks.length; i++) {
      try { hooks[i](); } catch (e) { /* ignore */ }
    }
  }

  var _messagesFeatureInitialized = false;
  function _onChannelOverlayClick(e) {
    if (e.target === e.currentTarget) _closeChannelDialog();
  }
  function _onChannelDialogCancel(e) {
    e.preventDefault();
    _closeChannelDialog();
  }
  function _onChannelListClick(e) {
    var btn = e.target.closest('.channel-delete-btn');
    if (btn) _deleteChannel(parseInt(btn.getAttribute('data-index'), 10));
  }

  // Explicit ESM lifecycle hook.  The feature bundle is imported after DOM
  // readiness, so relying on DOMContentLoaded would permanently skip wiring.
  R.initMessagesFeature = function () {
    if (_messagesFeatureInitialized) return;
    var close = $('channel-dialog-close');
    var join = $('channel-join-btn');
    var overlay = $('channel-dialog-overlay');
    var list = $('channel-list');
    if (close) close.addEventListener('click', _closeChannelDialog);
    if (join) join.addEventListener('click', _joinChannel);
    if (overlay) overlay.addEventListener('click', _onChannelOverlayClick);
    if (overlay) overlay.addEventListener('cancel', _onChannelDialogCancel);
    if (list) list.addEventListener('click', _onChannelListClick);
    _messagesFeatureInitialized = true;
  };

  R.disposeMessagesFeature = function () {
    if (!_messagesFeatureInitialized) return;
    var close = $('channel-dialog-close');
    var join = $('channel-join-btn');
    var overlay = $('channel-dialog-overlay');
    var list = $('channel-list');
    if (close) close.removeEventListener('click', _closeChannelDialog);
    if (join) join.removeEventListener('click', _joinChannel);
    if (overlay) overlay.removeEventListener('click', _onChannelOverlayClick);
    if (overlay) overlay.removeEventListener('cancel', _onChannelDialogCancel);
    if (list) list.removeEventListener('click', _onChannelListClick);
    _closeChannelDialog();
    _messagesFeatureInitialized = false;
  };
})();
