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

    var id = function (suffix) { return cfg.rootId + '-' + suffix; };

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
        destSelect: $(id('dest-select')),
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
      if (_dom.destSelect) {
        _dom.destSelect.addEventListener('change', _onDestChange);
      }
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
      return api('/api/meshtastic/channels').then(function (r) {
        if (!r || !r.ok) return;
        _channels = (r.data.channels || []).filter(function (ch) {
          return ch.active;
        });
        _renderChannelSelect();
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
      // Prepend the broadcast conversation as a pinned entry so users can
      // always jump straight to a broadcast without creating one first.
      var broadcastCid = '__broadcast_' + cfg.transport +
        (cfg.subTransport ? '_' + cfg.subTransport : '') + '__';
      var rows = [{
        contact_id: broadcastCid,
        msg_type: 'broadcast',
        contact_name: cfg.broadcastLabel || 'Broadcast',
        last_text: '',
        last_ts: null,
        unread_count: _unreadCounts[broadcastCid] || 0,
        _pinned: true,
      }];
      var seen = {};
      seen[broadcastCid] = true;
      for (var i = 0; i < _conversations.length; i++) {
        var c = _conversations[i];
        if (seen[c.contact_id]) {
          // merge with pinned broadcast (it already existed in DB)
          rows[0].last_text = c.last_text;
          rows[0].last_ts = c.last_ts;
          rows[0].unread_count = c.unread_count || 0;
          continue;
        }
        rows.push(c);
        seen[c.contact_id] = true;
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
        return cfg.broadcastLabel || 'Broadcast';
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
        _dom.chat.innerHTML = '';
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
      for (var i = 0; i < _channels.length; i++) {
        var ch = _channels[i];
        var label = ch.name || (ch.index === 0 ? 'Primary' : 'Ch ' + ch.index);
        if (ch.psk_label === 'unencrypted') label += ' (open)';
        html += '<option value="' + ch.index + '">' + esc(label) + '</option>';
      }
      _dom.chSelect.innerHTML = html;
      if (cur) _dom.chSelect.value = cur;
      // Hide the selector when the node only has one active channel
      if (_dom.chSelectWrap) {
        _dom.chSelectWrap.style.display = (_channels.length > 1) ? '' : 'none';
      }
    }

    function _renderDestSelect() {
      if (!_dom.destSelect) return;
      var html = '';
      var bcid = '__broadcast_' + cfg.transport +
        (cfg.subTransport ? '_' + cfg.subTransport : '') + '__';
      html += '<option value="' + esc(bcid) + '" data-type="broadcast">'
           + esc(cfg.broadcastLabel || 'Broadcast') + '</option>';
      if (cfg.transport === 'lxmf') {
        html += '<option value="__raw__" data-type="raw">Enter address…</option>';
      }
      for (var i = 0; i < _contacts.length; i++) {
        var c = _contacts[i];
        var label = c.name || c.id;
        if (c.name && c.id && c.name !== c.id) {
          label = c.name + ' (' + c.id.substring(0, 10) + '…)';
        }
        html += '<option value="' + esc(c.id) + '" data-type="direct">'
             + esc(label) + '</option>';
      }
      _dom.destSelect.innerHTML = html;
    }

    function _onDestChange() {
      if (!_dom.destSelect || !_dom.lxmfRaw) return;
      var opt = _dom.destSelect.options[_dom.destSelect.selectedIndex];
      var t = opt && opt.getAttribute('data-type');
      _dom.lxmfRaw.style.display = (t === 'raw') ? '' : 'none';
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
          // Clear local thread state and drop the conversation from the list
          _threadMessages = [];
          if (_dom.chat) _dom.chat.innerHTML = '';
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
        if (_dom.chat) _dom.chat.innerHTML = '';
        if (_dom.threadName) _dom.threadName.textContent = 'New Message';
        // Show both: destination picker (newCompose) and the shared textarea (compose)
        if (_dom.compose) _dom.compose.classList.remove('hidden');
        if (_dom.newCompose) _dom.newCompose.classList.remove('hidden');
        if (_dom.deleteBtn) _dom.deleteBtn.classList.add('hidden');
        var items = _dom.convs
          ? _dom.convs.querySelectorAll('.msg-conv-item') : [];
        for (var i = 0; i < items.length; i++) items[i].classList.remove('active');
        var layout = _dom.body && _dom.body.querySelector('.msg-layout');
        if (layout) layout.classList.add('thread-active');
        _fetchContacts();
      } else {
        if (_dom.newCompose) _dom.newCompose.classList.add('hidden');
        if (_dom.threadName) _dom.threadName.textContent = 'Select a conversation';
      }
    }

    // ── Send ───────────────────────────────────────────────────────
    function _resolveSendTarget() {
      if (_newComposeOpen) {
        if (!_dom.destSelect) return null;
        var val = _dom.destSelect.value;
        if (val === '__raw__') {
          var raw = _dom.lxmfRaw ? _dom.lxmfRaw.value.trim() : '';
          return raw ? {destination: raw, msgType: 'direct'} : null;
        }
        if (val.indexOf('__broadcast_') === 0) {
          return {destination: 'broadcast', msgType: 'broadcast'};
        }
        return {destination: val, msgType: 'direct'};
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
      if (cfg.supportsChannels && _dom.chSelect && _channels.length > 1) {
        body.channel = parseInt(_dom.chSelect.value, 10);
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
          } else if (_activeContactId) {
            _fetchThread(_activeContactId);
          }
        })
        .finally(function () {
          if (_dom.sendBtn) _dom.sendBtn.disabled = false;
        });
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
        _available = list.some(function (t) { return t.name === cfg.transport; });
        if (_dom.section) _dom.section.style.display = _available ? '' : 'none';
      });
      // First pass so counts appear before the user expands
      _fetchUnread();
      _fetchConversations();
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
    _refreshChannelList();
  };

  function _refreshChannelList() {
    api('/api/meshtastic/channels').then(function (r) {
      if (!r || !r.ok) return;
      var all = r.data.channels || [];
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
        _refreshChannelList();
      },
    );
  }

  function _deleteChannel(idx) {
    api('/api/meshtastic/channels/' + idx, {method: 'DELETE'}).then(
      function (r) { if (r && r.ok) _refreshChannelList(); },
    );
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
