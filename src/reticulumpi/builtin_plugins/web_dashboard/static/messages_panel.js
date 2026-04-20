/* ReticulumPi Dashboard — shared single-transport Messages panel factory.
 *
 * Each transport (LXMF, Meshtastic MQTT, Meshtastic LoRa, MeshCore) gets its
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
  var api = R.api, $ = R.$, esc = R.esc;
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
    _chInflight = api('/api/meshtastic/channels').then(function (r) {
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

  function createMessagesPanel(cfg) {
    // ── Per-panel state ────────────────────────────────────────────
    var _conversations = [];
    var _activeContactId = null;
    var _activeMsgType = null;
    var _threadMessages = [];
    var _unreadCounts = {};
    var _contacts = [];
    var _channels = [];
    var _newComposeOpen = false;
    var _expanded = false;
    var _dom = null;       // resolved DOM refs, cached on first data arrival
    var _available = false;
    var _destOptions = [];
    var _destSelection = null;

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

    function _fetchConversations() {
      return api('/api/messages/conversations' + _qs()).then(function (r) {
        if (!r || !r.ok) return;
        _conversations = r.data.conversations || [];
        _renderConversations();
      });
    }

    function _fetchThread(contactId, append) {
      if (!contactId) return;
      var extra = ['limit=50'];
      if (append && _threadMessages.length > 0) {
        var oldest = _threadMessages[_threadMessages.length - 1];
        if (oldest && oldest.timestamp) extra.push('before=' + oldest.timestamp);
      }
      var url = '/api/messages/conversation/' + encodeURIComponent(contactId) +
                '?' + extra.join('&');
      return api(url).then(function (r) {
        if (!r || !r.ok) return;
        var msgs = r.data.messages || [];
        _threadMessages = append ? _threadMessages.concat(msgs) : msgs;
        _renderThread();
      });
    }

    function _fetchContacts() {
      return api('/api/messages/contacts' + _qs()).then(function (r) {
        if (!r || !r.ok) return;
        _contacts = r.data.contacts || [];
        _renderDestSelect();
      });
    }

    function _fetchUnread() {
      return api('/api/messages/unread' + _qs()).then(function (r) {
        if (!r || !r.ok) return;
        _unreadCounts = r.data.unread || {};
        _updateUnreadUI();
      });
    }

    function _fetchChannels() {
      if (!cfg.supportsChannels) return;
      return _fetchChannelsShared().then(function (channels) {
        _channels = channels.filter(function (ch) { return ch.active; });
        _renderChannelSelect();
        // Channels drive the pinned per-channel broadcast rows.
        _renderConversations();
        _renderDestSelect();
      });
    }

    function _refresh() {
      _fetchConversations();
      _fetchUnread();
      _fetchContacts();
      _fetchChannels();
      if (_activeContactId) _fetchThread(_activeContactId);
    }

    // ── Rendering ──────────────────────────────────────────────────
    function _renderConversations() {
      if (!_dom.convs) return;
      // Pin one broadcast row per active Meshtastic channel (Primary,
      // private channels, etc.).  Panels without channel support fall
      // back to a single broadcast row using the legacy id shape.
      var pinnedRows = [];
      var seen = {};
      var canon = _canonicalize(_channels);
      if (cfg.supportsChannels && canon.canonical.length > 0) {
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

      var html = '';
      for (var j = 0; j < rows.length; j++) {
        var r = rows[j];
        var active = r.contact_id === _activeContactId;
        var preview = (r.last_text || '').slice(0, 60);
        var timeStr = r.last_ts ? formatTimeAgo(r.last_ts) : '';
        var unread = _unreadCounts[r.contact_id] || r.unread_count || 0;
        var name = _displayName(r);
        html += '<div class="msg-conv-item' + (active ? ' active' : '') + '"'
             + ' data-contact="' + esc(r.contact_id) + '"'
             + ' data-msgtype="' + esc(r.msg_type || 'direct') + '">'
             + '<div class="msg-conv-body">'
             + '<div class="msg-conv-top">'
             + '<span class="msg-conv-name">' + esc(name) + '</span>'
             + '<span class="msg-conv-time">' + esc(timeStr) + '</span>'
             + '</div>'
             + '<div class="msg-conv-preview">' + esc(preview) + '</div>'
             + '</div>'
             + (unread > 0 ? '<span class="msg-conv-unread">' + unread + '</span>' : '')
             + '</div>';
      }
      _dom.convs.innerHTML = html;
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
        var m = sorted[i];
        var isSent = m.direction === 'sent';
        var sender = isSent
          ? 'You'
          : (m.from_name || m.from_id || '?');
        var time = m.timestamp ? formatTimeAgo(m.timestamp) : '';
        var statusHtml = '';
        if (isSent && m.status) {
          if (m.status === 'sent')
            statusHtml = ' <span class="msg-status-sent">&#10003;</span>';
          else if (m.status === 'delivered')
            statusHtml = ' <span class="msg-status-sent">&#10003;&#10003;</span>';
          else if (m.status === 'pending')
            statusHtml = ' <span class="msg-status-pending">…</span>';
          else if (m.status === 'failed' || m.status === 'delivery_failed')
            statusHtml = ' <span class="msg-status-failed">&#10007;</span>';
        }
        html += '<div class="msg-bubble ' + (isSent ? 'sent' : 'received') + '">'
             + '<div class="msg-meta"><span>' + esc(sender) + '</span>'
             + '<span>' + esc(time) + '</span>' + statusHtml + '</div>'
             + '<div class="msg-text">' + esc(m.text || '') + '</div>'
             + '</div>';
      }
      var atBottom = (_dom.chat.scrollTop + _dom.chat.clientHeight
                      >= _dom.chat.scrollHeight - 40);
      _dom.chat.innerHTML = html;
      if (atBottom) _dom.chat.scrollTop = _dom.chat.scrollHeight;
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
      if (cfg.supportsChannels && canonical.length > 0) {
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
      // Re-render badges inside sidebar rows
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
          badge.style.display = '';
        } else if (badge) {
          badge.style.display = 'none';
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

      _fetchThread(contactId);
      if (_unreadCounts[contactId]) {
        delete _unreadCounts[contactId];
        _updateUnreadUI();
        api('/api/messages/read', {
          method: 'POST',
          body: {contact_id: contactId},
        });
      }
    }

    function _goBack() {
      var layout = _dom.body && _dom.body.querySelector('.msg-layout');
      if (layout) layout.classList.remove('thread-active');
      if (_dom.deleteBtn) _dom.deleteBtn.classList.add('hidden');
    }

    function _onDeleteConversation() {
      if (!_activeContactId) return;
      var name = _dom.threadName ? _dom.threadName.textContent : 'this conversation';
      if (!window.confirm('Delete all messages in "' + name + '"? This cannot be undone.')) {
        return;
      }
      var cid = _activeContactId;
      api('/api/messages/conversation/' + encodeURIComponent(cid), {method: 'DELETE'})
        .then(function (r) {
          if (!r || !r.ok) {
            _setFeedback((r && r.error) || 'Delete failed', 'error');
            return;
          }
          var deleted = (r.data && r.data.deleted) || 0;
          // Clear local thread state and drop the conversation from the list
          _threadMessages = [];
          if (_dom.chat) {
            // Visible success banner in the chat pane — the feedback span
            // lives inside compose (hidden next line), so use the chat area
            // so the user sees confirmation that the delete succeeded.
            _dom.chat.innerHTML = '<div class="msg-deleted-notice">Deleted ' +
              deleted + ' message' + (deleted === 1 ? '' : 's') +
              ' from &ldquo;' + esc(name) + '&rdquo;</div>';
          }
          if (_unreadCounts[cid]) {
            delete _unreadCounts[cid];
            _updateUnreadUI();
          }
          _activeContactId = null;
          _activeMsgType = null;
          if (_dom.threadName) _dom.threadName.textContent = 'Select a conversation';
          if (_dom.compose) _dom.compose.classList.add('hidden');
          if (_dom.deleteBtn) _dom.deleteBtn.classList.add('hidden');
          _goBack();
          _fetchConversations();
          _fetchUnread();
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

    function _onSend() {
      var target = _resolveSendTarget();
      if (!_dom.text) return;
      var text = _dom.text.value.trim();
      if (!text || !target) return;
      if (_dom.sendBtn) _dom.sendBtn.disabled = true;
      _setFeedback('Sending…', '');

      var body = {
        transport: cfg.transport,
        text: text,
        destination: target.destination,
      };
      if (target.msgType === 'broadcast') body.msg_type = 'broadcast';
      if (cfg.subTransport) body.sub_transport = cfg.subTransport;
      if (cfg.supportsChannels) {
        // Broadcast threads carry channel in their contact_id; that
        // wins over the standalone selector (which is disabled while
        // such a thread is active).  DMs still use the selector.
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
        } else if (_dom.chSelect
                   && _canonicalize(_channels).canonical.length > 1) {
          body.channel = parseInt(_dom.chSelect.value, 10);
        }
      }

      api('/api/messages/send', {method: 'POST', body: body})
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
          _fetchConversations();
          if (_newComposeOpen) {
            _newComposeOpen = false;
            if (_dom.newCompose) _dom.newCompose.classList.add('hidden');
            _renderChannelSelect();
          } else if (_activeContactId) {
            _fetchThread(_activeContactId);
          }
        })
        .finally(function () {
          if (_dom.sendBtn) _dom.sendBtn.disabled = false;
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
      if (cfg.transport === 'meshtastic') {
        _dom.byteCount.textContent = bytes + '/237';
        _dom.byteCount.className = 'msg-byte-count'
          + (bytes > 237 ? ' over' : bytes > 200 ? ' near' : '');
      } else {
        _dom.byteCount.textContent = '';
      }
      if (_dom.sendBtn) _dom.sendBtn.disabled = !text.trim();
    }

    function _autoGrow(el) {
      if (!el) return;
      el.style.height = 'auto';
      el.style.height = Math.min(el.scrollHeight, 96) + 'px';
    }

    // ── Search ─────────────────────────────────────────────────────
    function _onSearch() {
      if (!_dom.search) return;
      var q = _dom.search.value.trim();
      if (!q) { _fetchConversations(); return; }
      var extra = ['q=' + encodeURIComponent(q), 'limit=30'];
      api('/api/messages/search' + _qs(extra)).then(function (r) {
        if (!r || !r.ok) return;
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
    function update(wsPayload) {
      if (!wsPayload) return;
      if (!_resolveDom()) return;

      // Only react to messages relevant to this panel
      var relevant = false;
      if (wsPayload.messages && wsPayload.messages.length) {
        for (var i = 0; i < wsPayload.messages.length; i++) {
          var m = wsPayload.messages[i];
          if (m.transport !== cfg.transport) continue;
          if (cfg.subTransport !== null && cfg.subTransport !== undefined) {
            if ((m.sub_transport || '') !== cfg.subTransport) continue;
          }
          relevant = true;
          break;
        }
      }

      // Transport availability hint for section visibility
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

      if (relevant && _expanded) {
        _fetchConversations();
        if (_activeContactId) _fetchThread(_activeContactId);
      }
      // Unread counts filtered server-side — refresh on any broadcast tick
      if (wsPayload.unread) {
        _fetchUnread();
      }
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
      api('/api/messages/transports').then(function (r) {
        if (!r || !r.ok) return;
        var list = r.data.transports || [];
        var entry = null;
        for (var i = 0; i < list.length; i++) {
          if (list[i].name === cfg.transport) { entry = list[i]; break; }
        }
        _available = !!entry;
        if (_dom.section) _dom.section.style.display = _available ? '' : 'none';
        if (entry && entry.address) _renderTransportAddress(entry.address);
      });
      // First pass so counts appear before the user expands.
      // Pre-fetch channels too: without them, the conversation render
      // can't merge per-channel broadcast rows onto canonical pinned
      // ones, so the first paint after expand flickers as channels
      // arrive late.  The shared fetcher coalesces the LoRa+MQTT calls.
      _fetchUnread();
      _fetchConversations();
      if (cfg.supportsChannels) _fetchChannels();
    }

    // Register for cross-panel channel refresh so join/delete from the
    // shared dialog updates this panel's selector without a hard reload.
    if (cfg.supportsChannels) {
      R._channelRefreshHooks = R._channelRefreshHooks || [];
      R._channelRefreshHooks.push(_fetchChannels);
    }

    _init();

    // Public surface
    return {update: update};
  }

  R.createMessagesPanel = createMessagesPanel;

  // ── Shared channel-management dialog ───────────────────────────
  // Only one dialog instance is needed on the page; both Meshtastic
  // panels trigger it via R.openChannelDialog().  Logic was trimmed
  // from the old messages.js.
  R.openChannelDialog = function () {
    var overlay = $('channel-dialog-overlay');
    if (!overlay) return;
    overlay.style.display = 'flex';
    // Always show fresh state when the dialog opens — a stale cache
    // here would mislead the user about what's currently configured.
    _invalidateChannels();
    _refreshChannelList();
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
    if (o) o.style.display = 'none';
    ['channel-join-url', 'channel-join-name', 'channel-join-psk'].forEach(
      function (x) { var el = $(x); if (el) el.value = ''; },
    );
    var fb = $('channel-join-feedback');
    if (fb) { fb.textContent = ''; fb.className = 'msg-feedback'; }
  }

  function _joinChannel() {
    var url = ($('channel-join-url') || {}).value || '';
    var name = ($('channel-join-name') || {}).value || '';
    var psk = ($('channel-join-psk') || {}).value || '';
    var fb = $('channel-join-feedback');
    url = url.trim(); name = name.trim(); psk = psk.trim();
    if (!url && !name) {
      if (fb) { fb.textContent = 'Enter URL or name + PSK'; fb.className = 'msg-feedback error'; }
      return;
    }
    var body = url ? {url: url} : {name: name, psk: psk || 'default'};
    if (fb) { fb.textContent = 'Joining…'; fb.className = 'msg-feedback'; }
    api('/api/meshtastic/channels/join', {method: 'POST', body: body}).then(
      function (r) {
        if (!r || !r.ok) {
          if (fb) {
            fb.textContent = (r && r.error) || 'Join failed';
            fb.className = 'msg-feedback error';
          }
          return;
        }
        if (fb) { fb.textContent = 'Joined!'; fb.className = 'msg-feedback ok'; }
        _notifyChannelChange();   // invalidates cache before refresh
        _refreshChannelList();
      },
    );
  }

  function _deleteChannel(idx) {
    api('/api/meshtastic/channels/' + idx, {method: 'DELETE'}).then(
      function (r) {
        if (!r || !r.ok) return;
        _notifyChannelChange();   // invalidates cache before refresh
        _refreshChannelList();
      },
    );
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

  // Wire dialog close/join buttons once the DOM is ready
  document.addEventListener('DOMContentLoaded', function () {
    var close = $('channel-dialog-close');
    var join = $('channel-join-btn');
    var overlay = $('channel-dialog-overlay');
    var list = $('channel-list');
    if (close) close.addEventListener('click', _closeChannelDialog);
    if (join) join.addEventListener('click', _joinChannel);
    if (overlay) overlay.addEventListener('click', function (e) {
      if (e.target === overlay) _closeChannelDialog();
    });
    if (list) list.addEventListener('click', function (e) {
      var btn = e.target.closest('.channel-delete-btn');
      if (btn) _deleteChannel(parseInt(btn.getAttribute('data-index'), 10));
    });
  });
})();
