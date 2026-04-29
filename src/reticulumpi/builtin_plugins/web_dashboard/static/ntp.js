/* ReticulumPi Dashboard -- NTP / Time sync panel */
(function () {
  'use strict';
  var R = window.RPI;
  var $ = R.$, esc = R.esc;

  var _SYNC_LABELS = {
    'synced': '\u2705 Synced',
    'gps_disciplined': '\u{1f6f0}\ufe0f GPS',
    'unsynced': '\u274c Unsynced',
    'unknown': '\u2754 Unknown'
  };

  var _STATE_LABELS = {
    '*': '\u2705', '+': '\u2795', '-': '\u2796',
    '?': '\u2753', 'x': '\u274c', '~': '\u223c'
  };

  function _formatVal(v, decimals) {
    if (v == null) return '--';
    return Number(v).toFixed(decimals != null ? decimals : 1);
  }

  R.updateNtp = function (d) {
    if (!d) return;
    var el = $('ntp-section');
    if (!el) return;
    el.style.display = '';
    el.classList.remove('awaiting-data');

    // Status badge
    var badge = $('ntp-status');
    if (badge) {
      badge.textContent = d.sync_state === 'gps_disciplined' ? 'GPS'
        : d.sync_state === 'synced' ? 'synced'
        : d.sync_state || 'unknown';
    }

    // Sync state
    var ms = $('m-ntp-sync');
    if (ms) {
      var label = _SYNC_LABELS[d.sync_state] || d.sync_state || '--';
      ms.innerHTML = label;
    }

    // Stratum
    var mst = $('m-ntp-stratum');
    if (mst) mst.textContent = d.stratum != null ? d.stratum : '--';

    // Offset
    var mo = $('m-ntp-offset');
    if (mo) mo.innerHTML = _formatVal(d.offset_ms, 3) + '<span class="unit"> ms</span>';

    // Ref ID
    var mref = $('m-ntp-refid');
    if (mref) mref.textContent = d.ref_id || '--';

    // Sources table
    var tbody = $('ntp-sources-table');
    if (tbody && d.sources) {
      var html = '';
      for (var i = 0; i < d.sources.length; i++) {
        var s = d.sources[i];
        var stateIcon = _STATE_LABELS[s.state] || s.state || '?';
        html += '<tr>'
          + '<td title="' + esc(s.state_label || '') + '">' + stateIcon + '</td>'
          + '<td>' + esc(s.name || '--') + '</td>'
          + '<td>' + (s.stratum != null ? s.stratum : '--') + '</td>'
          + '<td>' + (s.poll != null ? s.poll : '--') + '</td>'
          + '<td>' + (s.reach != null ? s.reach : '--') + '</td>'
          + '<td>' + _formatVal(s.last_rx, 0) + 's</td>'
          + '<td>' + _formatVal(s.offset_ms, 3) + ' ms</td>'
          + '</tr>';
      }
      tbody.innerHTML = html;
    }
  };
})();
