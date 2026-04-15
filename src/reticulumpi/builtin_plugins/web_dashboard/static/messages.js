/* ReticulumPi Dashboard — Messages module (Phase 3: conversation-centric) */
(function() {
  'use strict';
  var R = window.RPI;
  var api = R.api, $ = R.$, esc = R.esc;
  var formatTimeAgo = R.formatTimeAgo;

  // ── State ──────────────────────────────────────────────────────────
  var _conversations = [];
  var _activeContactId = null;
  var _activeTransport = null;   // transport of the active conversation
  var _activeMsgType = null;     // 'direct' or 'broadcast'
  var _threadMessages = [];
  var _unreadCounts = {};        // {contact_id: count}
  var _msgTransports = [];
  var _msgContacts = [];
  var _msgSectionVisible = false;
  var _searchTimeout = null;
  var _isSearching = false;
  var _newComposeOpen = false;
  var _transportLastAvail = {};     // {name: timestamp} — debounce brief disconnects
  var _destActiveIdx = -1;          // keyboard-highlighted item in recipient dropdown
  var _destFetchTimer = null;       // debounce timer for recipient search API calls

  // ── Data fetching ──────────────────────────────────────────────────

  function fetchConversations() {
    var filterEl = $('msg-transport-filter');
    var transport = filterEl ? filterEl.value : '';
    var params = transport ? '?transport=' + encodeURIComponent(transport) : '';
    api('/api/messages/conversations' + params).then(function(r) {
      if (!r || !r.ok) return;
      _conversations = r.data.conversations || [];
      renderConversations();
    });
  }

  function fetchThreadMessages(contactId, append) {
    if (!contactId) return;
    var params = '?limit=50';
    if (append && _threadMessages.length > 0) {
      var oldest = _threadMessages[_threadMessages.length - 1];
      if (oldest && oldest.timestamp) params += '&before=' + oldest.timestamp;
    }
    api('/api/messages/conversation/' + encodeURIComponent(contactId) + params).then(function(r) {
      if (!r || !r.ok) return;
      var msgs = r.data.messages || [];
      if (append) {
        _threadMessages = _threadMessages.concat(msgs);
      } else {
        _threadMessages = msgs;
      }
      renderThread();
    });
  }

  function fetchTransports() {
    api('/api/messages/transports').then(function(r) {
      var section = $('messaging-section');
      if (!r || !r.ok) {
        if (section) {
          $('msg-count').textContent = 'unavailable';
          $('msg-count').style.color = 'var(--text-muted)';
        }
        return;
      }
      _msgTransports = r.data.transports || [];
      updateTransportDropdowns();
      if (section && _msgTransports.length > 0) {
        _msgSectionVisible = true;
        $('msg-count').textContent = '';
        $('msg-count').style.color = '';
      } else if (section) {
        $('msg-count').textContent = 'no transports';
        $('msg-count').style.color = 'var(--text-muted)';
      }
    });
  }

  function fetchContacts(transport, query) {
    var params = [];
    if (transport) params.push('transport=' + encodeURIComponent(transport));
    if (query) params.push('q=' + encodeURIComponent(query));
    var qs = params.length ? '?' + params.join('&') : '';
    api('/api/messages/contacts' + qs).then(function(r) {
      if (!r || !r.ok) return;
      _msgContacts = r.data.contacts || [];
      renderDestList();
    });
  }

  function fetchUnread() {
    api('/api/messages/unread').then(function(r) {
      if (!r || !r.ok) return;
      _unreadCounts = r.data.unread || {};
      updateUnreadUI();
    });
  }

  // ── Dropdown management ────────────────────────────────────────────

  function updateTransportDropdowns() {
    var now = Date.now();
    var GRACE_MS = 15000; // 15s grace before showing "(off)"

    // Track last-seen-available timestamp per transport
    for (var k = 0; k < _msgTransports.length; k++) {
      var tr = _msgTransports[k];
      if (tr.available) _transportLastAvail[tr.name] = now;
    }

    // Sidebar filter
    var filter = $('msg-transport-filter');
    if (filter) {
      var curFilter = filter.value;
      var opts = '<option value="">All</option>';
      for (var i = 0; i < _msgTransports.length; i++) {
        var t = _msgTransports[i];
        var stale = !t.available && (!_transportLastAvail[t.name]
                    || now - _transportLastAvail[t.name] > GRACE_MS);
        opts += '<option value="' + esc(t.name) + '">' + esc(t.display);
        if (stale) opts += ' (off)';
        opts += '</option>';
      }
      filter.innerHTML = opts;
      filter.value = curFilter;
    }
    // New-compose transport dropdown
    var send = $('msg-send-transport');
    if (send) {
      var curSend = send.value;
      var sendOpts = '';
      for (var j = 0; j < _msgTransports.length; j++) {
        var s = _msgTransports[j];
        var sendStale = !s.available && (!_transportLastAvail[s.name]
                        || now - _transportLastAvail[s.name] > GRACE_MS);
        sendOpts += '<option value="' + esc(s.name) + '"';
        if (sendStale) sendOpts += ' disabled';
        sendOpts += '>' + esc(s.display);
        if (s.address) sendOpts += ' (' + esc(s.address.substring(0, 12)) + '...)';
        sendOpts += '</option>';
      }
      send.innerHTML = sendOpts;
      if (curSend) send.value = curSend;
    }
  }

  // ── Searchable recipient combobox ────────────────────────────────

  /**
   * Build the grouped/sorted item list and render into #msg-dest-list.
   * Items come from _msgContacts (already sorted by last_heard from API)
   * and _conversations (recent conversation partners shown first).
   */
  function renderDestList() {
    var listEl = $('msg-dest-list');
    if (!listEl) return;
    var transport = $('msg-send-transport') ? $('msg-send-transport').value : '';
    var html = '';
    _destActiveIdx = -1;

    // --- Collect items by group ---

    // Group 1: built-in options (broadcast, enter address)
    var builtins = [];
    if (!transport || transport === 'meshtastic') {
      builtins.push({ id: 'broadcast', name: 'Broadcast (all)', transport: 'meshtastic', group: 'meshtastic' });
    }
    if (!transport || transport === 'meshcore') {
      builtins.push({ id: 'broadcast', name: 'Public Channel', transport: 'meshcore', group: 'meshcore' });
    }
    if (!transport || transport === 'lxmf') {
      builtins.push({ id: '__lxmf_raw__', name: 'Enter address\u2026', transport: 'lxmf', group: 'lxmf' });
    }

    // Group 2: recent conversation partners (from _conversations)
    var recentIds = {};
    var recentItems = [];
    for (var r = 0; r < _conversations.length && recentItems.length < 10; r++) {
      var conv = _conversations[r];
      if (conv.msg_type === 'broadcast') continue;
      if (transport && conv.transport !== transport) continue;
      var rid = conv.contact_id;
      if (recentIds[rid]) continue;
      recentIds[rid] = true;
      // Find matching contact for latest name
      var cname = conv.contact_name || rid;
      for (var ci = 0; ci < _msgContacts.length; ci++) {
        if (_msgContacts[ci].id === rid) { cname = _msgContacts[ci].name; break; }
      }
      recentItems.push({ id: rid, name: cname, transport: conv.transport, group: 'recent' });
    }

    // Group 3: all contacts (already sorted by last_heard from API)
    var contactItems = [];
    for (var i = 0; i < _msgContacts.length; i++) {
      var c = _msgContacts[i];
      if (transport && c.transport !== transport) continue;
      if (recentIds[c.id]) continue;  // skip dupes already in recent
      contactItems.push({ id: c.id, name: c.name, transport: c.transport, group: 'contacts' });
    }

    // --- Render ---
    var idx = 0;

    // Builtins first (no group label)
    for (var b = 0; b < builtins.length; b++) {
      html += destItemHtml(builtins[b], idx++);
    }

    // Recent conversations
    if (recentItems.length > 0) {
      html += '<div class="msg-dest-group-label">Recent</div>';
      for (var ri = 0; ri < recentItems.length; ri++) {
        html += destItemHtml(recentItems[ri], idx++);
      }
    }

    // All contacts
    if (contactItems.length > 0) {
      html += '<div class="msg-dest-group-label">All nodes</div>';
      for (var ci2 = 0; ci2 < contactItems.length; ci2++) {
        html += destItemHtml(contactItems[ci2], idx++);
      }
    }

    if (idx === 0) {
      html = '<div class="msg-dest-empty">No matching contacts</div>';
    }

    listEl.innerHTML = html;
  }

  function destItemHtml(item, idx) {
    return '<div class="msg-dest-item" data-idx="' + idx
      + '" data-id="' + esc(item.id) + '" data-transport="' + esc(item.transport) + '">'
      + '<span class="dest-name">' + esc(item.name) + '</span>'
      + (item.hint ? '<span class="dest-hint">' + esc(item.hint) + '</span>' : '')
      + '</div>';
  }

  function selectDestItem(id, name) {
    var hidden = $('msg-send-dest');
    var input = $('msg-dest-input');
    if (hidden) hidden.value = id;
    if (input) input.value = (id === 'broadcast' || id === '__lxmf_raw__') ? '' : name;
    closeDestList();
    toggleLxmfRawInput();
    updateByteCount('msg-new-text', 'msg-new-byte-count', 'msg-new-send-btn', 'msg-send-dest');
  }

  function openDestList() {
    var combo = $('msg-dest-combo');
    var listEl = $('msg-dest-list');
    if (!combo || !listEl) return;
    combo.classList.add('open');
    // Position the fixed dropdown relative to the input
    var rect = combo.getBoundingClientRect();
    listEl.style.top = rect.bottom + 'px';
    listEl.style.left = rect.left + 'px';
    listEl.style.width = rect.width + 'px';
  }

  function closeDestList() {
    var combo = $('msg-dest-combo');
    if (combo) combo.classList.remove('open');
    _destActiveIdx = -1;
  }

  function toggleLxmfRawInput() {
    var hidden = $('msg-send-dest');
    var rawEl = $('msg-lxmf-raw-input');
    if (!hidden) return;
    if (hidden.value === '__lxmf_raw__') {
      if (!rawEl) {
        // Create inline input after the combo
        rawEl = document.createElement('input');
        rawEl.type = 'text';
        rawEl.id = 'msg-lxmf-raw-input';
        rawEl.className = 'rt-input';
        rawEl.placeholder = '32-char hex hash...';
        rawEl.maxLength = 32;
        rawEl.style.flex = '1';
        rawEl.style.fontSize = '0.8rem';
        rawEl.style.fontFamily = 'var(--mono)';
        // Insert after the combo container in the compose row
        var combo = $('msg-dest-combo');
        (combo ? combo.parentNode : hidden.parentNode).appendChild(rawEl);
        rawEl.addEventListener('input', function() {
          updateByteCount('msg-new-text', 'msg-new-byte-count', 'msg-new-send-btn', null);
          // Enable send if hex hash looks valid (32 chars)
          var btn = $('msg-new-send-btn');
          var textEl = $('msg-new-text');
          if (btn && textEl) {
            btn.disabled = rawEl.value.length < 8 || !textEl.value.trim();
          }
        });
      }
      rawEl.style.display = '';
      rawEl.focus();
    } else if (rawEl) {
      rawEl.style.display = 'none';
    }
  }

  // ── Conversation rendering ─────────────────────────────────────────

  function renderConversations() {
    var container = $('msg-conversations');
    if (!container) return;

    if (_conversations.length === 0) {
      container.innerHTML = '';
      return;
    }

    var html = '';
    for (var i = 0; i < _conversations.length; i++) {
      var c = _conversations[i];
      var isActive = c.contact_id === _activeContactId;
      var name = contactDisplayName(c);
      var preview = c.last_text || '';
      if (preview.length > 60) preview = preview.substring(0, 60) + '...';
      var timeStr = c.last_ts ? formatTimeAgo(c.last_ts) : '';
      var unread = _unreadCounts[c.contact_id] || c.unread_count || 0;

      html += '<div class="msg-conv-item' + (isActive ? ' active' : '') + '"'
            + ' data-contact="' + esc(c.contact_id) + '"'
            + ' data-transport="' + esc(c.transport) + '"'
            + ' data-msgtype="' + esc(c.msg_type || 'direct') + '">';

      // Transport dot
      html += '<span class="msg-conv-transport">'
            + '<span class="msg-transport-badge ' + esc(c.transport) + '">'
            + esc(transportLabel(c.transport))
            + '</span></span>';

      html += '<div class="msg-conv-body">'
            + '<div class="msg-conv-top">'
            + '<span class="msg-conv-name">' + esc(name) + '</span>'
            + '<span class="msg-conv-time">' + timeStr + '</span>'
            + '</div>'
            + '<div class="msg-conv-preview">' + esc(preview) + '</div>'
            + '</div>';

      if (unread > 0) {
        html += '<span class="msg-conv-unread">' + unread + '</span>';
      }

      html += '</div>';
    }
    container.innerHTML = html;
  }

  function contactDisplayName(conv) {
    // Broadcast conversations — check sub-transport variants first
    if (conv.contact_id === '__broadcast_meshtastic_lora__') return 'Meshtastic LoRa';
    if (conv.contact_id === '__broadcast_meshtastic_mqtt__') return 'Meshtastic MQTT';
    if (conv.contact_id === '__broadcast_meshcore__') return 'MeshCore Public';
    if (conv.contact_id && conv.contact_id.indexOf('__broadcast_') === 0) {
      var transport = conv.transport || '';
      return transport.charAt(0).toUpperCase() + transport.slice(1) + ' Broadcast';
    }
    // Meshtastic DM — show long name with node ID hint
    if (conv.transport === 'meshtastic' && conv.msg_type !== 'broadcast'
        && conv.contact_name && conv.contact_id && conv.contact_id.charAt(0) === '!') {
      return conv.contact_name + ' (' + conv.contact_id + ')';
    }
    if (conv.contact_name && conv.contact_name !== conv.contact_id) {
      return conv.contact_name;
    }
    // Truncate long hex IDs
    var id = conv.contact_id || '?';
    if (id.length > 16) return id.substring(0, 8) + '...' + id.substring(id.length - 4);
    return id;
  }

  // ── Thread rendering ───────────────────────────────────────────────

  function renderThread() {
    var chat = $('msg-chat');
    if (!chat) return;

    if (_threadMessages.length === 0) {
      chat.innerHTML = '';
      return;
    }

    // Messages come newest-first from API; reverse for chronological display
    var sorted = _threadMessages.slice().reverse();
    var html = '';
    for (var i = 0; i < sorted.length; i++) {
      var m = sorted[i];
      var isSent = m.direction === 'sent';
      var cls = 'msg-bubble ' + (isSent ? 'sent' : 'received');

      var senderLabel = '';
      if (isSent) {
        senderLabel = 'You';
      } else if (_activeMsgType === 'broadcast' || (m.msg_type === 'broadcast')) {
        // In broadcast conversations, show sender
        senderLabel = m.from_name ? esc(m.from_name) : (m.from_id ? esc(m.from_id) : '?');
      } else {
        senderLabel = m.from_name ? esc(m.from_name) : (m.from_id ? esc(m.from_id) : '?');
      }

      var timeStr = m.timestamp ? formatTimeAgo(m.timestamp) : '';

      // Status indicator for sent messages
      var statusHtml = '';
      if (isSent && m.status) {
        if (m.status === 'sent') statusHtml = ' <span class="msg-status-sent">&#10003;</span>';
        else if (m.status === 'pending') statusHtml = ' <span class="msg-status-pending">&middot;&middot;&middot;</span>';
        else if (m.status === 'failed') statusHtml = ' <span class="msg-status-failed">&#10007;</span>';
      }

      html += '<div class="' + cls + '">'
            + '<div class="msg-meta"><span>' + senderLabel + '</span>'
            + '<span>' + timeStr + '</span>' + statusHtml + '</div>'
            + '<div class="msg-text">' + esc(m.text) + '</div>'
            + '</div>';
    }

    var wasAtBottom = (chat.scrollTop + chat.clientHeight >= chat.scrollHeight - 40);
    chat.innerHTML = html;
    if (wasAtBottom) chat.scrollTop = chat.scrollHeight;
  }

  // ── Conversation selection ─────────────────────────────────────────

  function selectConversation(el) {
    var contactId = el.getAttribute('data-contact');
    var transport = el.getAttribute('data-transport');
    var msgType = el.getAttribute('data-msgtype');
    if (!contactId) return;

    _activeContactId = contactId;
    _activeTransport = transport;
    _activeMsgType = msgType;
    _newComposeOpen = false;
    _threadMessages = [];

    // Update sidebar active state
    var items = document.querySelectorAll('.msg-conv-item');
    for (var i = 0; i < items.length; i++) {
      items[i].classList.toggle('active', items[i].getAttribute('data-contact') === contactId);
    }

    // Update thread header
    var nameEl = $('msg-thread-name');
    var badgeEl = $('msg-thread-transport');
    if (nameEl) {
      // Find the conversation object for display name
      var conv = null;
      for (var j = 0; j < _conversations.length; j++) {
        if (_conversations[j].contact_id === contactId) { conv = _conversations[j]; break; }
      }
      nameEl.textContent = conv ? contactDisplayName(conv) : contactId;
    }
    if (badgeEl) {
      badgeEl.className = 'msg-transport-badge ' + (transport || '');
      badgeEl.textContent = transportLabel(transport);
    }

    // Show compose, hide new-compose
    var compose = $('msg-compose');
    var newCompose = $('msg-new-compose');
    if (compose) compose.style.display = '';
    if (newCompose) newCompose.style.display = 'none';

    // Mobile: show thread pane
    var layout = document.querySelector('.msg-layout');
    if (layout) layout.classList.add('thread-active');

    // Fetch thread messages
    fetchThreadMessages(contactId);

    // Mark as read (optimistic)
    if (_unreadCounts[contactId]) {
      delete _unreadCounts[contactId];
      updateUnreadUI();
      api('/api/messages/read', {
        method: 'POST',
        body: { contact_id: contactId }
      });
    }
  }

  function goBack() {
    var layout = document.querySelector('.msg-layout');
    if (layout) layout.classList.remove('thread-active');
  }

  // ── Unread UI ──────────────────────────────────────────────────────

  function updateUnreadUI() {
    // Total unread for header
    var total = 0;
    for (var k in _unreadCounts) {
      if (_unreadCounts.hasOwnProperty(k)) total += _unreadCounts[k];
    }
    var totalEl = $('msg-unread-total');
    if (totalEl) totalEl.textContent = total > 0 ? total : '';

    // Re-render conversation badges
    var items = document.querySelectorAll('.msg-conv-item');
    for (var i = 0; i < items.length; i++) {
      var cid = items[i].getAttribute('data-contact');
      var unread = _unreadCounts[cid] || 0;
      var badge = items[i].querySelector('.msg-conv-unread');
      if (unread > 0 && !badge) {
        badge = document.createElement('span');
        badge.className = 'msg-conv-unread';
        items[i].appendChild(badge);
      }
      if (badge) {
        badge.textContent = unread > 0 ? unread : '';
        badge.style.display = unread > 0 ? '' : 'none';
      }
    }
  }

  // ── Send message (thread compose) ─────────────────────────────────

  function sendMessage() {
    if (!_activeContactId || !_activeTransport) return;

    var textEl = $('msg-send-text');
    var btn = $('msg-send-btn');
    if (!textEl) return;
    var text = textEl.value.trim();
    if (!text) return;

    var dest = _activeContactId;
    var isBroadcast = dest.indexOf('__broadcast_') === 0;
    // Broadcast: send to 'broadcast'
    if (isBroadcast) dest = 'broadcast';

    btn.disabled = true;
    showMsgFeedback('msg-feedback', 'Sending...', '');

    var body = { transport: _activeTransport, text: text, destination: dest };
    if (isBroadcast) {
      body.msg_type = 'broadcast';
      if (_activeContactId === '__broadcast_meshtastic_lora__') body.sub_transport = 'lora';
      else if (_activeContactId === '__broadcast_meshtastic_mqtt__') body.sub_transport = 'mqtt';
    }
    api('/api/messages/send', {
      method: 'POST',
      body: body
    }).then(function(r) {
      if (!r) { showMsgFeedback('msg-feedback', 'Network error', 'error'); return; }
      if (!r.ok) { showMsgFeedback('msg-feedback', r.error || 'Send failed', 'error'); return; }
      var d = r.data;
      if (!d.sent) {
        showMsgFeedback('msg-feedback', 'Not sent: ' + (d.reason || 'unknown'), 'error');
        return;
      }
      textEl.value = '';
      autoGrow(textEl);
      updateByteCount('msg-send-text', 'msg-byte-count', 'msg-send-btn', null);
      var note = d.truncated ? 'Sent (truncated)' : 'Sent';
      showMsgFeedback('msg-feedback', note, 'ok');
      // Refresh thread
      fetchThreadMessages(_activeContactId);
      fetchConversations();
    }).finally(function() {
      btn.disabled = false;
    });
  }

  // ── Send message (new compose) ─────────────────────────────────────

  function sendNewMessage() {
    var transportEl = $('msg-send-transport');
    var destEl = $('msg-send-dest');
    var textEl = $('msg-new-text');
    var btn = $('msg-new-send-btn');
    if (!transportEl || !destEl || !textEl) return;

    var transport = transportEl.value;
    var dest = destEl.value;
    var text = textEl.value.trim();
    // Handle raw LXMF address
    if (dest === '__lxmf_raw__') {
      var rawEl = $('msg-lxmf-raw-input');
      dest = rawEl ? rawEl.value.trim() : '';
    }
    if (!transport || !dest || !text) return;

    btn.disabled = true;
    showMsgFeedback('msg-new-feedback', 'Sending...', '');

    api('/api/messages/send', {
      method: 'POST',
      body: { transport: transport, text: text, destination: dest }
    }).then(function(r) {
      if (!r) { showMsgFeedback('msg-new-feedback', 'Network error', 'error'); return; }
      if (!r.ok) { showMsgFeedback('msg-new-feedback', r.error || 'Send failed', 'error'); return; }
      var d = r.data;
      if (!d.sent) {
        showMsgFeedback('msg-new-feedback', 'Not sent: ' + (d.reason || 'unknown'), 'error');
        return;
      }
      textEl.value = '';
      autoGrow(textEl);
      showMsgFeedback('msg-new-feedback', d.truncated ? 'Sent (truncated)' : 'Sent', 'ok');
      // Close new compose, refresh conversations
      _newComposeOpen = false;
      var newCompose = $('msg-new-compose');
      if (newCompose) newCompose.style.display = 'none';
      fetchConversations();
    }).finally(function() {
      btn.disabled = false;
    });
  }

  // ── Helpers ────────────────────────────────────────────────────────

  function transportLabel(name) {
    if (name === 'meshtastic') return 'MSH';
    if (name === 'meshcore') return 'MC';
    if (name === 'lxmf') return 'LXMF';
    return name || '';
  }

  function showMsgFeedback(elId, text, cls) {
    var el = $(elId);
    if (!el) return;
    el.textContent = text;
    el.className = 'msg-feedback' + (cls ? ' ' + cls : '');
    if (cls === 'ok') {
      setTimeout(function() {
        if (el.textContent === text) el.textContent = '';
      }, 3000);
    }
  }

  function updateByteCount(textElId, byteElId, btnId, destElId) {
    var textEl = $(textElId);
    var byteEl = $(byteElId);
    var btn = $(btnId);
    if (!textEl || !byteEl) return;
    var text = textEl.value;
    var bytes = new TextEncoder().encode(text).length;

    // Determine transport context
    var transport = _activeTransport;
    if (textElId === 'msg-new-text') {
      var tEl = $('msg-send-transport');
      transport = tEl ? tEl.value : '';
    }

    if (transport === 'meshtastic') {
      byteEl.textContent = bytes + '/237';
      byteEl.className = 'msg-byte-count' + (bytes > 237 ? ' over' : bytes > 200 ? ' near' : '');
    } else {
      byteEl.textContent = '';
      byteEl.className = 'msg-byte-count';
    }

    // Enable send
    var hasText = text.trim().length > 0;
    if (btn) {
      if (destElId) {
        var destEl = $(destElId);
        btn.disabled = !hasText || !(destEl && destEl.value);
      } else {
        btn.disabled = !hasText;
      }
    }
  }

  function updateMsgByteCount() {
    // Backward-compat: called from app.js fetchAll
    if (_activeContactId) {
      updateByteCount('msg-send-text', 'msg-byte-count', 'msg-send-btn', null);
    }
  }

  function autoGrow(el) {
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 96) + 'px';
  }

  // ── Search ─────────────────────────────────────────────────────────

  function onSearchInput() {
    var input = $('msg-search-input');
    if (!input) return;
    var query = input.value.trim();

    if (_searchTimeout) clearTimeout(_searchTimeout);

    if (!query) {
      _isSearching = false;
      fetchConversations();
      return;
    }

    _searchTimeout = setTimeout(function() {
      _isSearching = true;
      var filterEl = $('msg-transport-filter');
      var transport = filterEl ? filterEl.value : '';
      var params = '?q=' + encodeURIComponent(query) + '&limit=30';
      if (transport) params += '&transport=' + encodeURIComponent(transport);
      api('/api/messages/search' + params).then(function(r) {
        if (!r || !r.ok) return;
        renderSearchResults(r.data.messages || [], query);
      });
    }, 300);
  }

  function renderSearchResults(messages, query) {
    var container = $('msg-conversations');
    if (!container) return;

    if (messages.length === 0) {
      container.innerHTML = '<div style="text-align:center;padding:2rem 0.5rem;color:var(--text-muted);font-size:0.85rem;">No results for "' + esc(query) + '"</div>';
      return;
    }

    var html = '';
    for (var i = 0; i < messages.length; i++) {
      var m = messages[i];
      var sender = m.direction === 'sent' ? 'You' : (m.from_name || m.from_id || '?');
      var preview = m.text || '';
      if (preview.length > 80) preview = preview.substring(0, 80) + '...';
      // Highlight match
      var re = new RegExp('(' + query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
      var highlightedPreview = esc(preview).replace(re, '<mark>$1</mark>');
      var timeStr = m.timestamp ? formatTimeAgo(m.timestamp) : '';

      html += '<div class="msg-conv-item"'
            + ' data-contact="' + esc(m.contact_id || '') + '"'
            + ' data-transport="' + esc(m.transport) + '"'
            + ' data-msgtype="' + esc(m.msg_type || 'direct') + '">';
      html += '<span class="msg-conv-transport">'
            + '<span class="msg-transport-badge ' + esc(m.transport) + '">'
            + esc(transportLabel(m.transport))
            + '</span></span>';
      html += '<div class="msg-conv-body">'
            + '<div class="msg-conv-top">'
            + '<span class="msg-conv-name">' + esc(sender) + '</span>'
            + '<span class="msg-conv-time">' + timeStr + '</span>'
            + '</div>'
            + '<div class="msg-conv-preview">' + highlightedPreview + '</div>'
            + '</div>';
      html += '</div>';
    }
    container.innerHTML = html;
  }

  // ── New Message toggle ─────────────────────────────────────────────

  function toggleNewCompose() {
    _newComposeOpen = !_newComposeOpen;
    var newCompose = $('msg-new-compose');
    var compose = $('msg-compose');
    var headerName = $('msg-thread-name');

    if (_newComposeOpen) {
      _activeContactId = null;
      _activeTransport = null;
      _activeMsgType = null;
      _threadMessages = [];

      // Clear active state from conversation list
      var items = document.querySelectorAll('.msg-conv-item');
      for (var i = 0; i < items.length; i++) items[i].classList.remove('active');

      // Update thread header
      if (headerName) headerName.textContent = 'New Message';
      var badgeEl = $('msg-thread-transport');
      if (badgeEl) { badgeEl.className = 'msg-transport-badge'; badgeEl.textContent = ''; }

      // Show new compose, hide thread compose, clear chat
      if (newCompose) newCompose.style.display = '';
      if (compose) compose.style.display = 'none';
      var chat = $('msg-chat');
      if (chat) chat.innerHTML = '';

      // Mobile: show thread pane
      var layout = document.querySelector('.msg-layout');
      if (layout) layout.classList.add('thread-active');

      // Fetch contacts for the current transport
      var tEl = $('msg-send-transport');
      if (tEl) fetchContacts(tEl.value);
    } else {
      if (newCompose) newCompose.style.display = 'none';
      if (headerName) headerName.textContent = 'Select a conversation';
    }
  }

  // ── WebSocket updates ──────────────────────────────────────────────

  function updateMessaging(data) {
    if (!data) return;

    // Update unread counts from WS
    if (data.unread) {
      _unreadCounts = data.unread;
      updateUnreadUI();
    }

    // If new messages arrived, refresh conversations list
    if (data.messages && data.messages.length > 0) {
      fetchConversations();

      // If viewing a thread, check if new messages belong to active conversation
      if (_activeContactId) {
        var hasNew = false;
        for (var i = 0; i < data.messages.length; i++) {
          if (data.messages[i].contact_id === _activeContactId) {
            hasNew = true;
            break;
          }
        }
        if (hasNew) {
          fetchThreadMessages(_activeContactId);
          // Auto-mark read since user is viewing this conversation
          if (_unreadCounts[_activeContactId]) {
            delete _unreadCounts[_activeContactId];
            updateUnreadUI();
            api('/api/messages/read', {
              method: 'POST',
              body: { contact_id: _activeContactId }
            });
          }
        }
      }
    }

    // Update transport availability
    if (data.transports) {
      _msgTransports = data.transports;
      updateTransportDropdowns();
    }
  }

  // ── Backwards-compat stubs ─────────────────────────────────────────
  // fetchMessages is still called by app.js fetchAll(); redirect to conversations
  function fetchMessages() {
    fetchConversations();
    fetchUnread();
  }

  // ── Event wiring ───────────────────────────────────────────────────

  // Thread compose
  var sendBtn = $('msg-send-btn');
  var sendText = $('msg-send-text');
  if (sendBtn) sendBtn.addEventListener('click', sendMessage);
  if (sendText) {
    sendText.addEventListener('keydown', function(e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
    });
    sendText.addEventListener('input', function() {
      autoGrow(sendText);
      updateByteCount('msg-send-text', 'msg-byte-count', 'msg-send-btn', null);
    });
  }

  // New-compose
  var newSendBtn = $('msg-new-send-btn');
  var newText = $('msg-new-text');
  var newTransport = $('msg-send-transport');
  var destInput = $('msg-dest-input');
  var destList = $('msg-dest-list');
  if (newSendBtn) newSendBtn.addEventListener('click', sendNewMessage);
  if (newText) {
    newText.addEventListener('keydown', function(e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendNewMessage(); }
    });
    newText.addEventListener('input', function() {
      autoGrow(newText);
      updateByteCount('msg-new-text', 'msg-new-byte-count', 'msg-new-send-btn', 'msg-send-dest');
    });
  }
  if (newTransport) newTransport.addEventListener('change', function() {
    // Clear selection when transport changes
    if (destInput) destInput.value = '';
    var hidden = $('msg-send-dest');
    if (hidden) hidden.value = '';
    fetchContacts(newTransport.value);
    updateByteCount('msg-new-text', 'msg-new-byte-count', 'msg-new-send-btn', 'msg-send-dest');
  });

  // Searchable recipient input
  if (destInput) {
    destInput.addEventListener('focus', function() {
      openDestList();
      // Load full list on first focus if empty
      if (_msgContacts.length === 0) {
        var t = newTransport ? newTransport.value : '';
        fetchContacts(t);
      }
    });
    destInput.addEventListener('input', function() {
      // Clear previous selection when typing
      var hidden = $('msg-send-dest');
      if (hidden) hidden.value = '';
      openDestList();
      // Debounce API search (250ms)
      if (_destFetchTimer) clearTimeout(_destFetchTimer);
      var q = destInput.value.trim();
      var t = newTransport ? newTransport.value : '';
      _destFetchTimer = setTimeout(function() {
        fetchContacts(t, q || undefined);
      }, 250);
    });
    destInput.addEventListener('keydown', function(e) {
      var items = destList ? destList.querySelectorAll('.msg-dest-item') : [];
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        _destActiveIdx = Math.min(_destActiveIdx + 1, items.length - 1);
        highlightDestItem(items);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        _destActiveIdx = Math.max(_destActiveIdx - 1, 0);
        highlightDestItem(items);
      } else if (e.key === 'Enter') {
        e.preventDefault();
        if (_destActiveIdx >= 0 && _destActiveIdx < items.length) {
          var it = items[_destActiveIdx];
          selectDestItem(it.getAttribute('data-id'), it.querySelector('.dest-name').textContent);
        }
      } else if (e.key === 'Escape') {
        closeDestList();
        destInput.blur();
      }
    });
  }

  // Click on item in dropdown list
  if (destList) {
    destList.addEventListener('click', function(e) {
      var item = e.target.closest('.msg-dest-item');
      if (item) {
        selectDestItem(item.getAttribute('data-id'), item.querySelector('.dest-name').textContent);
      }
    });
  }

  // Close dropdown when clicking outside
  document.addEventListener('mousedown', function(e) {
    var combo = $('msg-dest-combo');
    if (combo && !combo.contains(e.target)) {
      closeDestList();
    }
  });

  function highlightDestItem(items) {
    for (var i = 0; i < items.length; i++) {
      items[i].classList.toggle('active', i === _destActiveIdx);
    }
    if (_destActiveIdx >= 0 && items[_destActiveIdx]) {
      items[_destActiveIdx].scrollIntoView({ block: 'nearest' });
    }
  }

  // Conversation click — event delegation (CSP blocks inline onclick)
  var convContainer = $('msg-conversations');
  if (convContainer) convContainer.addEventListener('click', function(e) {
    var el = e.target.closest('.msg-conv-item');
    if (el) selectConversation(el);
  });

  // Sidebar controls
  var searchInput = $('msg-search-input');
  var filterTransport = $('msg-transport-filter');
  var newBtn = $('msg-new-btn');
  var backBtn = $('msg-back-btn');
  if (searchInput) searchInput.addEventListener('input', onSearchInput);
  if (filterTransport) filterTransport.addEventListener('change', function() {
    if (_isSearching) onSearchInput();
    else fetchConversations();
  });
  if (newBtn) newBtn.addEventListener('click', toggleNewCompose);
  if (backBtn) backBtn.addEventListener('click', goBack);

  // ── Expose to RPI namespace ────────────────────────────────────────
  R.fetchMessages = fetchMessages;
  R.fetchTransports = fetchTransports;
  R.fetchContacts = fetchContacts;
  R.updateMessaging = updateMessaging;
  R.sendMessage = sendMessage;
  R.updateMsgByteCount = updateMsgByteCount;
  R.selectConversation = selectConversation;
  R.goBack = goBack;

  // ── Self-initialize ────────────────────────────────────────────────
  // app.js calls fetchAll() before messages.js loads, so RPI.fetchMessages
  // is undefined at that point and messaging is silently skipped.  Fetch
  // initial data here so conversations appear immediately on page load.
  fetchTransports();
  fetchConversations();
  fetchUnread();

})();
