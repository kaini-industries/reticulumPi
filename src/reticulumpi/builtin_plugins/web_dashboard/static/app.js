/* ReticulumPi Dashboard — vanilla JS */
(function() {
  'use strict';

  // Keep initially unavailable panels hidden before JavaScript runs, then move
  // their visibility state to CSSOM properties.  CSP blocks style attributes,
  // while property assignment remains available for live dashboard state.
  var initiallyHidden = document.querySelectorAll('.csp-initial-hidden');
  for (var hiddenIndex = 0; hiddenIndex < initiallyHidden.length; hiddenIndex++) {
    initiallyHidden[hiddenIndex].style.display = 'none';
    initiallyHidden[hiddenIndex].classList.remove('csp-initial-hidden');
  }

  // The server knows which plugins are READY while rendering the shell.  Seed
  // feature visibility from that state so optional panels never flash and then
  // disappear after the first status payload (a large layout shift on Pi-class
  // hardware and slow LANs).
  var _serverReadyFeatures = Object.create(null);
  var readyFeatureTokens = ((document.body && document.body.dataset.readyFeatures) || '')
    .split(/\s+/);
  for (var readyFeatureIndex = 0; readyFeatureIndex < readyFeatureTokens.length; readyFeatureIndex++) {
    if (readyFeatureTokens[readyFeatureIndex]) {
      _serverReadyFeatures[readyFeatureTokens[readyFeatureIndex]] = true;
    }
  }

  // Horizontal and bounded vertical regions must remain operable without a
  // pointer.  Tables can be empty during first paint, so their wrappers cannot
  // rely on a focusable descendant to satisfy this requirement.
  var scrollableRegions = document.querySelectorAll(
    '.table-wrap, .table-scroll, .scroll-max-500, .scroll-max-400, ' +
    '.config-content, .mqtt-feed-list, .msg-conversations, .msg-chat, ' +
    '.dialog-body, .lt-log-wrap, .nt-results, .radio-band-selector'
  );
  for (var scrollRegionIndex = 0; scrollRegionIndex < scrollableRegions.length; scrollRegionIndex++) {
    var scrollRegion = scrollableRegions[scrollRegionIndex];
    if (!scrollRegion.hasAttribute('tabindex')) scrollRegion.tabIndex = 0;
    if (!scrollRegion.hasAttribute('aria-label') && !scrollRegion.hasAttribute('aria-labelledby')) {
      var labelledSection = scrollRegion.closest('section[aria-label]');
      var scrollLabel = labelledSection && labelledSection.getAttribute('aria-label');
      if (!scrollRegion.hasAttribute('role')) scrollRegion.setAttribute('role', 'region');
      scrollRegion.setAttribute('aria-label', (scrollLabel || 'Dashboard') + ' scrollable content');
    }
  }

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js', { scope: '/' })
      .catch(function () {});
    navigator.serviceWorker.addEventListener('message', function (ev) {
      if (ev.data && ev.data.type === 'sw-updated') {
        var banner = document.getElementById('sw-update-banner');
        if (!banner) {
          banner = document.createElement('div');
          banner.id = 'sw-update-banner';
          banner.className = 'sw-update-banner';
          banner.textContent = 'A new version is available. ';
          var reloadLink = document.createElement('a');
          reloadLink.href = '#';
          reloadLink.textContent = 'Reload';
          reloadLink.addEventListener('click', function(e) { e.preventDefault(); location.reload(); });
          banner.appendChild(reloadLink);
          document.body.appendChild(banner);
        }
        banner.style.display = '';
      }
    });
  }

  /* ── Shared namespace ─────────────────────────────────────────────── */
  var RPI = window.RPI = window.RPI || {};

  var ws = null;
  var reconnectDelay = 1000;
  var maxReconnect = 30000;
  var reconnectAttempts = 0;
  var maxReconnectAttempts = 50;
  var pollTimer = null;
  var uptimeStart = 0;
  var uptimeTimer = null;
  var prevIfaces = {};      // {name: {rxb, txb, time}} for rate calculation
  var configIfaces = {};    // {name: {enabled, type, properties}} from config file
  var pendingRestart = false;
  var lastLiveIfaces = [];  // last live interfaces from RNS
  var _wsFirstTick = false;
  var _wsReadyCallbacks = [];
  var passwordChangeRequired = new URLSearchParams(window.location.search)
    .get('password_change') === 'required';

  // Wrap a block of wiring/boot work so an exception in one block cannot kill
  // the rest of the script. Reports through errlog.js if present (it may not be
  // loaded yet / at all), otherwise falls back to console.
  function safeWire(label, fn) {
    try {
      fn();
    } catch (e) {
      if (window.__rpiReportError) window.__rpiReportError(e, 'wire:' + label);
      else if (window.console) console.error('wire ' + label + ' failed', e);
    }
  }

  // Arm the data pipeline (WS + HTTP fallback + staleness) as early as possible.
  // boot() is a hoisted declaration; every function it calls is hoisted and all
  // DOM lookups are valid (app.js is a defer script). Wrapping in safeWire keeps
  // a boot-time exception from killing the rest of the script.
  function boot() {
    if (passwordChangeRequired) {
      showPasswordChangeDialog();
      return;
    }
    // If we reached this page, the cookie is valid.
    initOffgridToggle();
    fetchNode();
    connectWS();

    // WS delivers a full initial snapshot covering metrics, interfaces,
    // transport, connectivity, routing, meshtastic, meshcore, gps, adsb, etc.
    // Only fall back to HTTP if WS hasn't delivered data within 2s.
    var _criticalFallbackFired = false;
    var _criticalFallback = setTimeout(function() {
      _criticalFallbackFired = true;
      fetchCritical();
      setTimeout(fetchSecondary, 500);
    }, 2000);

    // Once WS is ready, cancel HTTP fallback and fetch only WS-uncovered data.
    var _wsUncoveredTimer = setTimeout(fetchWsUncovered, 3000);
    onWsReady(function() {
      clearTimeout(_criticalFallback);
      clearTimeout(_wsUncoveredTimer);
      if (!_criticalFallbackFired) {
        // Full plugin detail and LoRa config are not in the WS snapshot
        apiRetry('/api/plugins').then(function(r) {
          if (r && r.ok) updatePlugins(r.data.plugins, r.data.failed_plugins);
        });
        apiRetry('/api/lora').then(function(r) {
          if (r && r.ok && RPI.updateLoraRadio) RPI.updateLoraRadio(null, r.data);
        });
      }
      fetchWsUncovered();
    });

    // Periodic refresh: only poll WS-uncovered data when WS is live
    setInterval(function() {
      if (!_wsFirstTick) {
        fetchCritical();
        fetchSecondary();
      }
      fetchWsUncovered();
    }, 30000);
  }
  safeWire('boot', boot);

  // --- Helpers ---

  function api(path, opts) {
    opts = opts || {};
    var headers = Object.assign({}, opts.headers || {});
    headers['Accept'] = 'application/json';
    headers['X-Requested-With'] = 'XMLHttpRequest';
    var hasJson = opts.json !== undefined && opts.json !== null;
    if (hasJson) headers['Content-Type'] = 'application/json';
    if (hasJson && typeof opts.json !== 'object') {
      return Promise.resolve({ok: false, error: 'API json must be an object'});
    }
    var timeoutMs = opts.timeout || 10000;
    var ctrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var timer = ctrl ? setTimeout(function() { try { ctrl.abort(); } catch (e) {} }, timeoutMs) : null;
    return RPI.jsonFetch(path, {
      method: opts.method || 'GET',
      headers: headers,
      credentials: 'same-origin',
      json: hasJson ? opts.json : undefined,
      signal: ctrl ? ctrl.signal : undefined
    }).then(function(r) {
      if (timer) clearTimeout(timer);
      if (r.status === 401) { window.location.href = '/login.html'; return null; }
      if (r.status === 428) {
        passwordChangeRequired = true;
        showPasswordChangeDialog();
      }
      return r.json().catch(function() { return {ok: false, error: 'Invalid response'}; });
    }).catch(function() { if (timer) clearTimeout(timer); return null; });
  }

  function apiRetry(path, opts, maxRetries) {
    maxRetries = maxRetries || 2;
    return api(path, opts).then(function(r) {
      if (r !== null || maxRetries <= 0) return r;
      return new Promise(function(resolve) {
        setTimeout(function() {
          resolve(apiRetry(path, opts, maxRetries - 1));
        }, 500);
      });
    });
  }

  function $(id) { return document.getElementById(id); }

  function applyCspDynamicStyles(root) {
    if (!root || !root.querySelectorAll) return;
    var widthNodes = root.querySelectorAll('[data-rpi-width]');
    for (var i = 0; i < widthNodes.length; i++) {
      var width = Number(widthNodes[i].getAttribute('data-rpi-width'));
      if (Number.isFinite(width)) {
        widthNodes[i].style.width = Math.max(0, Math.min(100, width)) + '%';
      }
      widthNodes[i].removeAttribute('data-rpi-width');
    }
  }
  RPI.applyCspDynamicStyles = applyCspDynamicStyles;

  function confirmDestructive(title, message, confirmLabel) {
    var dialog = $('destructive-dialog');
    if (!dialog || dialog.open) return Promise.resolve(false);
    $('destructive-dialog-title').textContent = title || 'Confirm action';
    $('destructive-dialog-message').textContent = message || 'This action cannot be undone.';
    $('destructive-dialog-confirm').textContent = confirmLabel || 'Continue';
    dialog.returnValue = '';
    return new Promise(function(resolve) {
      dialog.addEventListener('close', function() {
        resolve(dialog.returnValue === 'confirm');
      }, {once: true});
      dialog.showModal();
      $('destructive-dialog-cancel').focus();
    });
  }
  RPI.confirmDestructive = confirmDestructive;

  function esc(s) {
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  function formatUptime(seconds) {
    if (!seconds || seconds < 0) return '--';
    var d = Math.floor(seconds / 86400);
    var h = Math.floor((seconds % 86400) / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    var s = Math.floor(seconds % 60);
    if (d > 0) return d + 'd ' + h + 'h ' + m + 'm';
    if (h > 0) return h + 'h ' + m + 'm ' + s + 's';
    return m + 'm ' + s + 's';
  }

  function formatBytes(b) {
    if (b == null) return '--';
    if (b < 1024) return b + ' B';
    if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
    if (b < 1073741824) return (b / 1048576).toFixed(1) + ' MB';
    return (b / 1073741824).toFixed(2) + ' GB';
  }

  function formatRate(bytesPerSec) {
    if (bytesPerSec == null || bytesPerSec < 0) return '--';
    if (bytesPerSec < 1) return '0 B/s';
    if (bytesPerSec < 1024) return bytesPerSec.toFixed(0) + ' B/s';
    if (bytesPerSec < 1048576) return (bytesPerSec / 1024).toFixed(1) + ' KB/s';
    return (bytesPerSec / 1048576).toFixed(2) + ' MB/s';
  }

  function metricClass(value, warn, crit) {
    if (value == null) return '';
    if (value >= crit) return 'metric-crit';
    if (value >= warn) return 'metric-warn';
    return 'metric-ok';
  }

  // --- Panel visibility for WS update gating ---

  function isPanelVisible(bodyId) {
    var body = document.getElementById(bodyId);
    if (!body) return true;
    if (!body.classList.contains('hidden')) return true;
    var secId = bodyId.replace(/-body$/, '-section');
    var sec = document.getElementById(secId);
    return sec ? sec.style.display !== 'none' : false;
  }
  RPI.isPanelVisible = isPanelVisible;

  var _stash = {};
  var _sectionOnExpand = {};

  // --- Optional feature loading -----------------------------------------
  // Keep the coordinator in the core shell, but load heavyweight panels only
  // after their backing plugin has produced an availability signal and the
  // user opens the panel.  Feature bundles are ESM modules with an explicit
  // init(context) / dispose(context) contract.

  var _featureManifestPromise = null;
  var _leafletPromise = null;
  var _featureMessageEvents = [];
  var _FEATURE_MESSAGE_EVENT_LIMIT = 100;
  var _featureByToggle = Object.create(null);
  var _features = Object.create(null);
  var _readyPlugins = Object.create(null);

  function registerFeature(name, spec) {
    if (!name || !spec || !spec.asset) throw new Error('Invalid dashboard feature');
    var feature = {
      name: name,
      asset: spec.asset,
      sections: spec.sections || [],
      toggles: spec.toggles || [],
      bodies: spec.bodies || [],
      dependencies: spec.dependencies || [],
      hideUntilAvailable: spec.hideUntilAvailable !== false,
      loadWhenVisible: spec.loadWhenVisible === true,
      available: !!_serverReadyFeatures[name],
      desired: false,
      active: false,
      replayingClick: false,
      module: null,
      promise: null,
      dataPromise: null,
      lastDataFetch: 0,
      error: null
    };
    _features[name] = feature;
    for (var i = 0; i < feature.toggles.length; i++) {
      _featureByToggle[feature.toggles[i]] = name;
    }
    if (feature.hideUntilAvailable) {
      _setFeatureSectionsVisible(feature, feature.available);
    }
    if (feature.loadWhenVisible) _observeFeatureVisibility(feature);
    return feature;
  }
  RPI.registerFeature = registerFeature;

  function _featureIsVisible(feature) {
    var viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;
    for (var i = 0; i < feature.sections.length; i++) {
      var section = $(feature.sections[i]);
      if (!section) continue;
      var rect = section.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < viewportHeight) {
        return true;
      }
    }
    return false;
  }

  function _observeFeatureVisibility(feature) {
    if (!('IntersectionObserver' in window)) return;
    feature.observer = new IntersectionObserver(function(entries) {
      for (var i = 0; i < entries.length; i++) {
        if (!entries[i].isIntersecting) continue;
        if (!feature.available || feature.active || feature.promise) return;
        feature.desired = true;
        loadFeature(feature.name).catch(function() {});
        return;
      }
    }, {rootMargin: '160px 0px'});
    for (var i = 0; i < feature.sections.length; i++) {
      var section = $(feature.sections[i]);
      if (section) feature.observer.observe(section);
    }
  }

  function _setFeatureSectionsVisible(feature, visible) {
    for (var i = 0; i < feature.sections.length; i++) {
      var section = $(feature.sections[i]);
      if (section) section.style.display = visible ? '' : 'none';
    }
  }

  function _featureHasOpenBody(feature) {
    for (var i = 0; i < feature.bodies.length; i++) {
      var body = $(feature.bodies[i]);
      if (body && !body.classList.contains('hidden')) return true;
    }
    return false;
  }

  function _setFeatureBusy(feature, busy) {
    for (var i = 0; i < feature.sections.length; i++) {
      var section = $(feature.sections[i]);
      if (!section) continue;
      if (busy) section.setAttribute('aria-busy', 'true');
      else section.removeAttribute('aria-busy');
    }
  }

  function _featureContext() {
    return {rpi: RPI, replay: _replayFeature};
  }

  function _loadStylesheetOnce(id, href) {
    var existing = $(id);
    if (existing && existing.sheet) return Promise.resolve(existing);
    return new Promise(function(resolve, reject) {
      var link = existing || document.createElement('link');
      link.id = id;
      link.rel = 'stylesheet';
      link.href = href;
      link.addEventListener('load', function() { resolve(link); }, {once: true});
      link.addEventListener('error', function() { reject(new Error('Could not load ' + href)); }, {once: true});
      if (!existing) document.head.appendChild(link);
    });
  }

  function _loadScriptOnce(id, src) {
    if (window.L) return Promise.resolve();
    var existing = $(id);
    return new Promise(function(resolve, reject) {
      var script = existing || document.createElement('script');
      script.id = id;
      script.src = src;
      script.defer = true;
      script.addEventListener('load', function() { resolve(); }, {once: true});
      script.addEventListener('error', function() { reject(new Error('Could not load ' + src)); }, {once: true});
      if (!existing) document.head.appendChild(script);
    });
  }

  function _loadLeaflet() {
    if (window.L) return Promise.resolve();
    if (_leafletPromise) return _leafletPromise;
    _leafletPromise = _loadStylesheetOnce('rpi-leaflet-css', '/static/vendor/leaflet.css')
      .then(function() {
        return _loadScriptOnce('rpi-leaflet-script', '/static/vendor/leaflet.js');
      }).then(function() {
        if (!window.L) throw new Error('Leaflet did not initialize');
        if (_stash.gps && RPI.updateGps) RPI.updateGps(_stash.gps);
      }).catch(function(error) {
        var css = $('rpi-leaflet-css');
        var script = $('rpi-leaflet-script');
        if (css) css.remove();
        if (script) script.remove();
        _leafletPromise = null;
        throw error;
      });
    return _leafletPromise;
  }

  function _loadFeatureDependencies(feature) {
    var pending = [];
    for (var i = 0; i < feature.dependencies.length; i++) {
      if (feature.dependencies[i] === 'leaflet') pending.push(_loadLeaflet());
    }
    return Promise.all(pending);
  }

  function _loadFeatureManifest() {
    if (_featureManifestPromise) return _featureManifestPromise;
    _featureManifestPromise = fetch('/static/asset-manifest.json', {
      credentials: 'same-origin',
      cache: 'no-cache',
      headers: {'Accept': 'application/json'}
    }).then(function(response) {
      if (!response.ok) throw new Error('feature manifest request failed');
      return response.json();
    }).then(function(manifest) {
      if (!manifest || manifest.schema !== 1 || !manifest.assets) {
        throw new Error('feature manifest is invalid');
      }
      return manifest;
    }).catch(function(error) {
      _featureManifestPromise = null;
      throw error;
    });
    return _featureManifestPromise;
  }

  function loadFeature(name) {
    var feature = _features[name];
    if (!feature) return Promise.reject(new Error('Unknown feature: ' + name));
    if (!feature.available) return Promise.reject(new Error('Feature is unavailable: ' + name));
    if (feature.active) {
      _fetchFeatureData(name);
      return Promise.resolve(feature.module);
    }
    if (feature.promise) return feature.promise;

    _setFeatureBusy(feature, true);
    feature.error = null;
    feature.promise = _loadFeatureDependencies(feature).then(function() {
      if (feature.module) return feature.module;
      return _loadFeatureManifest().then(function(manifest) {
          var record = manifest.assets[feature.asset];
          if (!record || typeof record.path !== 'string' ||
              !/^assets\/[a-z0-9-]+-[A-Z0-9]+\.js$/.test(record.path)) {
            throw new Error('Feature asset is missing: ' + feature.asset);
          }
          return import('/static/' + record.path);
        });
    }).then(function(module) {
      if (!module || typeof module.init !== 'function' || typeof module.dispose !== 'function') {
        throw new Error('Feature module contract is invalid: ' + name);
      }
      feature.module = module;
      return Promise.resolve(module.init(_featureContext())).then(function() {
        feature.active = true;
        feature.error = null;
        return _fetchFeatureData(name).then(function() { return module; });
      });
    }).catch(function(error) {
      feature.error = error;
      feature.active = false;
      showToast('Could not load ' + name + '. Open the panel to retry.', 'error');
      throw error;
    }).finally(function() {
      feature.promise = null;
      _setFeatureBusy(feature, false);
    });
    return feature.promise;
  }
  RPI.loadFeature = loadFeature;

  function _fetchFeatureData(name) {
    var feature = _features[name];
    if (!feature || !feature.active) return Promise.resolve([]);
    if (feature.dataPromise) return feature.dataPromise;
    if (Date.now() - feature.lastDataFetch < 10000) return Promise.resolve([]);

    var requests = [];
    if (name === 'map') {
      if (_readyPlugins.meshtastic_gateway) {
        var mshStatus = null, mshNodes = null;
        function mergeMeshtasticFeatureData() {
          if (mshStatus === null || mshNodes === null) return;
          _stash.meshtastic_status = mshStatus;
          _stash.meshtastic_nodes = mshNodes;
          if (RPI.updateMeshtastic) RPI.updateMeshtastic(mshStatus, mshNodes);
          if (RPI.updateMap) RPI.updateMap(mshNodes);
        }
        requests.push(apiRetry('/api/meshtastic/status').then(function(r) {
          mshStatus = (r && r.ok) ? r.data : {};
          mergeMeshtasticFeatureData();
        }));
        requests.push(apiRetry('/api/meshtastic/nodes').then(function(r) {
          mshNodes = (r && r.ok) ? r.data.nodes : [];
          mergeMeshtasticFeatureData();
        }));
        requests.push(apiRetry('/api/meshtastic/device').then(function(r) {
          if (r && r.ok && RPI.updateMeshtasticDevice) RPI.updateMeshtasticDevice(r.data);
        }));
        requests.push(apiRetry('/api/meshtastic/lora_neighbors').then(function(r) {
          if (!r || !r.ok) return;
          _stash.meshtastic_lora_neighbors = r.data.neighbors;
          if (RPI.updateLoraNeighbors) RPI.updateLoraNeighbors(r.data.neighbors);
          if (RPI.updateMapLoraNeighbors) RPI.updateMapLoraNeighbors(r.data.neighbors);
        }));
      }

      if (_readyPlugins.meshcore_gateway || _readyPlugins.meshcore_observer) {
        var mcStatus = null, mcContacts = null;
        function mergeMeshCoreFeatureData() {
          if (mcStatus === null || mcContacts === null) return;
          _stash.meshcore_status = mcStatus;
          _stash.meshcore_contacts = mcContacts;
          if (RPI.updateMeshCore) RPI.updateMeshCore(mcStatus, mcContacts);
          if (RPI.updateMapMeshCore) RPI.updateMapMeshCore(mcContacts);
        }
        requests.push(apiRetry('/api/meshcore/status').then(function(r) {
          mcStatus = (r && r.ok) ? r.data : {};
          mergeMeshCoreFeatureData();
        }));
        requests.push(apiRetry('/api/meshcore/contacts').then(function(r) {
          mcContacts = (r && r.ok) ? r.data.contacts : [];
          mergeMeshCoreFeatureData();
        }));
        requests.push(api('/api/meshcore/device').then(function(r) {
          if (r && r.ok && RPI.updateMeshCoreDevice) RPI.updateMeshCoreDevice(r.data);
        }));
        requests.push(api('/api/meshcore_observer/status').then(function(r) {
          if (r && r.ok && RPI.updateMeshCoreObserver) RPI.updateMeshCoreObserver(r.data);
        }));
      }

      if (_readyPlugins.gps_telemetry) {
        requests.push(apiRetry('/api/gps').then(function(r) {
          if (!r || !r.ok) return;
          _stash.gps = r.data;
          if (RPI.updateGps) RPI.updateGps(r.data);
          if (r.data.last_fix && RPI.updateMapGps) RPI.updateMapGps(r.data.last_fix);
        }));
      }
      if (_readyPlugins.mesh_telemetry) {
        requests.push(api('/api/mesh/telemetry').then(function(r) {
          if (!r || !r.ok) return;
          _stash.mesh_peers = r.data.peers;
          if (RPI.cacheMeshPeers) RPI.cacheMeshPeers(r.data.peers);
          if (RPI.updatePeerTelemetry) RPI.updatePeerTelemetry(r.data.peers);
          if (RPI.updateMapReticulum) RPI.updateMapReticulum(r.data.peers);
        }));
      }
    } else if (name === 'lora') {
      if (RPI.updateLoraRadio) RPI.updateLoraRadio(_stash.interfaces || null, _stash.lora || null);
      if (_stash.interfaces && RPI.updateLoraSignal) RPI.updateLoraSignal(_stash.interfaces);
      if (RPI.fetchLoraReachability) RPI.fetchLoraReachability();
      requests.push(Promise.resolve());
    } else if (name === 'mesh') {
      if (RPI.fetchMeshNodes) RPI.fetchMeshNodes();
      if (RPI.fetchMeshSummary) RPI.fetchMeshSummary();
      if (_stash.mesh && RPI.updateMeshFromWS) RPI.updateMeshFromWS(_stash.mesh);
      requests.push(Promise.resolve());
    } else if (name === 'routing' && _readyPlugins.connectivity_monitor) {
      requests.push(apiRetry('/api/routing?per_page=0').then(function(r) {
        if (!r || !r.ok) return;
        _stash.routing = r.data.summary || {};
        if (RPI.updateRoutingSummary) RPI.updateRoutingSummary(_stash.routing);
      }));
    } else if (name === 'mesh-bridge' && _readyPlugins.mesh_bridge) {
      requests.push(apiRetry('/api/mesh_bridge/status').then(function(r) {
        if (!r || !r.ok) return;
        _stash.mesh_bridge = r.data;
        if (RPI.updateMeshBridge) RPI.updateMeshBridge(r.data);
      }));
    } else if (name === 'meshtastic' && _readyPlugins.meshtastic_gateway) {
      var featureMshStatus = null, featureMshNodes = null;
      function mergeMeshtasticPanelData() {
        if (featureMshStatus === null || featureMshNodes === null) return;
        _stash.meshtastic_status = featureMshStatus;
        _stash.meshtastic_nodes = featureMshNodes;
        if (RPI.updateMeshtastic) RPI.updateMeshtastic(featureMshStatus, featureMshNodes);
      }
      requests.push(apiRetry('/api/meshtastic/status').then(function(r) {
        featureMshStatus = (r && r.ok) ? r.data : {};
        mergeMeshtasticPanelData();
      }));
      requests.push(apiRetry('/api/meshtastic/nodes').then(function(r) {
        featureMshNodes = (r && r.ok) ? r.data.nodes : [];
        mergeMeshtasticPanelData();
      }));
      requests.push(apiRetry('/api/meshtastic/device').then(function(r) {
        if (!r || !r.ok) return;
        _stash.meshtastic_device = r.data;
        if (RPI.updateMeshtasticDevice) RPI.updateMeshtasticDevice(r.data);
      }));
      requests.push(apiRetry('/api/meshtastic/lora_neighbors').then(function(r) {
        if (!r || !r.ok) return;
        _stash.meshtastic_lora_neighbors = r.data.neighbors;
        if (RPI.updateLoraNeighbors) RPI.updateLoraNeighbors(r.data.neighbors);
      }));
    } else if (name === 'meshcore' &&
               (_readyPlugins.meshcore_gateway || _readyPlugins.meshcore_observer)) {
      var featureMcStatus = null, featureMcContacts = null;
      function mergeMeshCorePanelData() {
        if (featureMcStatus === null || featureMcContacts === null) return;
        _stash.meshcore_status = featureMcStatus;
        _stash.meshcore_contacts = featureMcContacts;
        if (RPI.updateMeshCore) RPI.updateMeshCore(featureMcStatus, featureMcContacts);
      }
      requests.push(apiRetry('/api/meshcore/status').then(function(r) {
        featureMcStatus = (r && r.ok) ? r.data : {};
        mergeMeshCorePanelData();
      }));
      requests.push(apiRetry('/api/meshcore/contacts').then(function(r) {
        featureMcContacts = (r && r.ok) ? r.data.contacts : [];
        mergeMeshCorePanelData();
      }));
      requests.push(apiRetry('/api/meshcore/device').then(function(r) {
        if (!r || !r.ok) return;
        _stash.meshcore_device = r.data;
        if (RPI.updateMeshCoreDevice) RPI.updateMeshCoreDevice(r.data);
      }));
      requests.push(apiRetry('/api/meshcore_observer/status').then(function(r) {
        if (!r || !r.ok) return;
        _stash.meshcore_observer = r.data;
        if (RPI.updateMeshCoreObserver) RPI.updateMeshCoreObserver(r.data);
      }));
    } else if (name === 'gps' && _readyPlugins.gps_telemetry) {
      requests.push(apiRetry('/api/gps').then(function(r) {
        if (!r || !r.ok) return;
        _stash.gps = r.data;
        if (RPI.updateGps) RPI.updateGps(r.data);
      }));
    } else if (name === 'ntp' && _readyPlugins.ntp_server) {
      requests.push(apiRetry('/api/ntp').then(function(r) {
        if (!r || !r.ok) return;
        _stash.ntp = r.data;
        if (RPI.updateNtp) RPI.updateNtp(r.data);
      }));
    } else if (name === 'link-tester' && _readyPlugins.lora_link_tester) {
      requests.push(apiRetry('/api/link_tester').then(function(r) {
        if (!r || !r.ok) return;
        _stash.link_tester = r.data;
        if (RPI.updateLinkTester) RPI.updateLinkTester(r.data);
      }));
    } else if (name === 'hotspot') {
      requests.push(apiRetry('/api/captive_portal').then(function(r) {
        if (r && r.ok) _stash.captive_portal = r.data;
        if (_stash.hotspot && RPI.updateHotspot) {
          RPI.updateHotspot(_stash.hotspot, _stash.captive_portal || null);
        }
      }));
    } else if (name === 'weather-alert' && _readyPlugins.weather_alert) {
      requests.push(apiRetry('/api/weather_alert').then(function(r) {
        if (!r || !r.ok) return;
        _stash.weather_alert = r.data;
        if (RPI.updateWeatherAlert) RPI.updateWeatherAlert(r.data);
      }));
    } else if (name === 'ais' && _readyPlugins.ais_receiver) {
      requests.push(apiRetry('/api/ais').then(function(r) {
        if (!r || !r.ok) return;
        _stash.ais = r.data;
        if (RPI.updateAis) RPI.updateAis(r.data);
      }));
    } else if (name === 'acars' && _readyPlugins.acars_decoder) {
      requests.push(apiRetry('/api/acars').then(function(r) {
        if (!r || !r.ok) return;
        _stash.acars = r.data;
        if (RPI.updateAcars) RPI.updateAcars(r.data);
      }));
    } else if (name === 'radiosonde' && _readyPlugins.radiosonde_tracker) {
      requests.push(apiRetry('/api/radiosonde').then(function(r) {
        if (!r || !r.ok) return;
        _stash.radiosonde = r.data;
        if (RPI.updateRadiosonde) RPI.updateRadiosonde(r.data);
      }));
    } else if (name === 'noaa' && _readyPlugins.noaa_apt_decoder) {
      requests.push(apiRetry('/api/noaa').then(function(r) {
        if (!r || !r.ok) return;
        _stash.noaa_apt = r.data;
        if (RPI.updateNoaa) RPI.updateNoaa(r.data);
      }));
    } else if (name === 'adsb' && _readyPlugins.adsb_radar) {
      requests.push(apiRetry('/api/adsb').then(function(r) {
        if (!r || !r.ok) return;
        _stash.adsb = r.data;
        if (RPI.adsb && RPI.adsb.update) RPI.adsb.update(r.data);
      }));
    }

    if (!requests.length) return Promise.resolve([]);
    feature.dataPromise = Promise.all(requests).then(function(results) {
      feature.lastDataFetch = Date.now();
      return results;
    }).finally(function() {
      feature.dataPromise = null;
    });
    return feature.dataPromise;
  }
  RPI.fetchFeatureData = _fetchFeatureData;

  function _disposeFeature(feature) {
    if (!feature || !feature.module || !feature.active) return Promise.resolve();
    feature.active = false;
    return Promise.resolve(feature.module.dispose(_featureContext())).catch(function(error) {
      if (window.__rpiReportError) window.__rpiReportError(error, 'feature-dispose:' + feature.name);
    });
  }

  function _setFeatureAvailable(name, available) {
    var feature = _features[name];
    if (!feature) return;
    feature.available = !!available;
    if (feature.hideUntilAvailable) _setFeatureSectionsVisible(feature, feature.available);
    if (!feature.available) {
      feature.desired = false;
      _disposeFeature(feature);
      return;
    }
    if (feature.loadWhenVisible &&
        (!('IntersectionObserver' in window) || _featureIsVisible(feature))) {
      feature.desired = true;
      loadFeature(name).catch(function() {});
      return;
    }
    if (_featureHasOpenBody(feature)) {
      feature.desired = true;
      loadFeature(name).catch(function() {});
    }
  }
  RPI.setFeatureAvailable = _setFeatureAvailable;

  function _pluginIsReady(plugins, name) {
    var plugin = plugins && plugins[name];
    if (!plugin) return false;
    var status = plugin.status || {};
    var lifecycle = status._lifecycle || {};
    if (lifecycle.state) return lifecycle.state === 'ready';
    return status.active !== false;
  }

  function _markFeaturesFromPlugins(plugins) {
    _readyPlugins = Object.create(null);
    var pluginNames = Object.keys(plugins || {});
    for (var pi = 0; pi < pluginNames.length; pi++) {
      if (_pluginIsReady(plugins, pluginNames[pi])) _readyPlugins[pluginNames[pi]] = true;
    }
    var spectrumLink = $('spectrum-nav-link');
    if (spectrumLink) {
      var spectrumAvailable = _pluginIsReady(plugins, 'spectrum_scanner') ||
        _pluginIsReady(plugins, 'lora_scanner') ||
        _pluginIsReady(plugins, 'lora_link_tester');
      spectrumLink.hidden = !spectrumAvailable;
      spectrumLink.setAttribute('aria-disabled', spectrumAvailable ? 'false' : 'true');
    }
    _setFeatureAvailable('messages', _pluginIsReady(plugins, 'messaging_hub'));
    _setFeatureAvailable('adsb', _pluginIsReady(plugins, 'adsb_radar'));
    _setFeatureAvailable('space', _pluginIsReady(plugins, 'space_tracker'));
    _setFeatureAvailable('radio', _pluginIsReady(plugins, 'fm_receiver'));
    _setFeatureAvailable('mesh',
      _pluginIsReady(plugins, 'network_map') || _pluginIsReady(plugins, 'mesh_telemetry'));
    _setFeatureAvailable('routing', _pluginIsReady(plugins, 'connectivity_monitor'));
    _setFeatureAvailable('mesh-bridge', _pluginIsReady(plugins, 'mesh_bridge'));
    _setFeatureAvailable('meshtastic', _pluginIsReady(plugins, 'meshtastic_gateway'));
    _setFeatureAvailable('meshcore',
      _pluginIsReady(plugins, 'meshcore_gateway') || _pluginIsReady(plugins, 'meshcore_observer'));
    _setFeatureAvailable('gps', _pluginIsReady(plugins, 'gps_telemetry'));
    _setFeatureAvailable('ntp', _pluginIsReady(plugins, 'ntp_server'));
    _setFeatureAvailable('link-tester', _pluginIsReady(plugins, 'lora_link_tester'));
    _setFeatureAvailable('hotspot',
      _pluginIsReady(plugins, 'hotspot_monitor') || _pluginIsReady(plugins, 'captive_portal'));
    _setFeatureAvailable('weather-alert', _pluginIsReady(plugins, 'weather_alert'));
    _setFeatureAvailable('ais', _pluginIsReady(plugins, 'ais_receiver'));
    _setFeatureAvailable('acars', _pluginIsReady(plugins, 'acars_decoder'));
    _setFeatureAvailable('radiosonde', _pluginIsReady(plugins, 'radiosonde_tracker'));
    _setFeatureAvailable('noaa', _pluginIsReady(plugins, 'noaa_apt_decoder'));
    var mapPlugins = [
      'meshtastic_gateway', 'meshcore_gateway', 'meshcore_observer',
      'node_location_tracker', 'gps_telemetry', 'mesh_telemetry'
    ];
    var mapAvailable = false;
    for (var i = 0; i < mapPlugins.length; i++) {
      if (_pluginIsReady(plugins, mapPlugins[i])) {
        mapAvailable = true;
        break;
      }
    }
    _setFeatureAvailable('map', mapAvailable);
  }

  function _queueMessageFeatureEvent(handler, data) {
    _setFeatureAvailable('messages', true);
    if (typeof RPI[handler] === 'function') {
      RPI[handler](data);
      return;
    }
    if (_featureMessageEvents.length >= _FEATURE_MESSAGE_EVENT_LIMIT) {
      _featureMessageEvents.shift();
    }
    _featureMessageEvents.push({handler: handler, data: data});
  }

  function _replayFeature(name) {
    if (name === 'messages') {
      if (_stash.messaging) {
        if (RPI.updateMessagingLxmf) RPI.updateMessagingLxmf(_stash.messaging);
        if (RPI.updateMqttFeed) RPI.updateMqttFeed(_stash.messaging);
        if (RPI.updateMessagingLora) RPI.updateMessagingLora(_stash.messaging);
        if (RPI.updateMessagingMeshcore) RPI.updateMessagingMeshcore(_stash.messaging);
      }
      var events = _featureMessageEvents.splice(0, _featureMessageEvents.length);
      for (var mi = 0; mi < events.length; mi++) {
        if (typeof RPI[events[mi].handler] === 'function') {
          RPI[events[mi].handler](events[mi].data);
        }
      }
      return;
    }
    if (name === 'map') {
      if (_stash.meshtastic_nodes !== undefined && RPI.updateMap) RPI.updateMap(_stash.meshtastic_nodes);
      if (_stash.meshtastic_lora_neighbors !== undefined && RPI.updateMapLoraNeighbors) {
        RPI.updateMapLoraNeighbors(_stash.meshtastic_lora_neighbors);
      }
      if (_stash.meshcore_contacts !== undefined && RPI.updateMapMeshCore) RPI.updateMapMeshCore(_stash.meshcore_contacts);
      if (_stash.mesh_peers !== undefined && RPI.updateMapReticulum) RPI.updateMapReticulum(_stash.mesh_peers);
      if (_stash.gps && _stash.gps.last_fix && RPI.updateMapGps) RPI.updateMapGps(_stash.gps.last_fix);
      if (RPI.updateNodeTracker) {
        RPI.updateNodeTracker(
          _stash.meshtastic_nodes || null,
          _stash.meshtastic_lora_neighbors || null,
          _stash.meshcore_contacts || null
        );
      }
      return;
    }
    if (name === 'adsb' && _stash.adsb && RPI.adsb && RPI.adsb.update) RPI.adsb.update(_stash.adsb);
    if (name === 'space' && _stash.space && RPI.space && RPI.space.update) RPI.space.update(_stash.space);
    if (name === 'radio' && _stash.fm_receiver && RPI.updateRadio) RPI.updateRadio(_stash.fm_receiver);
    if (name === 'lora') {
      if (RPI.updateLoraRadio) RPI.updateLoraRadio(_stash.interfaces || null, _stash.lora || null);
      if (_stash.interfaces && RPI.updateLoraSignal) RPI.updateLoraSignal(_stash.interfaces);
    }
    if (name === 'mesh') {
      if (_stash.mesh && RPI.updateMeshFromWS) RPI.updateMeshFromWS(_stash.mesh);
      if (_stash.mesh_peers && RPI.cacheMeshPeers) RPI.cacheMeshPeers(_stash.mesh_peers);
      if (_stash.mesh_peers && RPI.updatePeerTelemetry) RPI.updatePeerTelemetry(_stash.mesh_peers);
    }
    if (name === 'routing' && _stash.routing && RPI.updateRoutingSummary) {
      RPI.updateRoutingSummary(_stash.routing);
    }
    if (name === 'mesh-bridge' && _stash.mesh_bridge && RPI.updateMeshBridge) {
      RPI.updateMeshBridge(_stash.mesh_bridge);
    }
    if (name === 'meshtastic') {
      if ((_stash.meshtastic_status || _stash.meshtastic_nodes) && RPI.updateMeshtastic) {
        RPI.updateMeshtastic(_stash.meshtastic_status || {}, _stash.meshtastic_nodes || []);
      }
      if (_stash.meshtastic_device && RPI.updateMeshtasticDevice) {
        RPI.updateMeshtasticDevice(_stash.meshtastic_device);
      }
      if (_stash.meshtastic_lora_neighbors && RPI.updateLoraNeighbors) {
        RPI.updateLoraNeighbors(_stash.meshtastic_lora_neighbors);
      }
    }
    if (name === 'meshcore') {
      if ((_stash.meshcore_status || _stash.meshcore_contacts) && RPI.updateMeshCore) {
        RPI.updateMeshCore(_stash.meshcore_status || {}, _stash.meshcore_contacts || []);
      }
      if (_stash.meshcore_device && RPI.updateMeshCoreDevice) {
        RPI.updateMeshCoreDevice(_stash.meshcore_device);
      }
      if (_stash.meshcore_observer && RPI.updateMeshCoreObserver) {
        RPI.updateMeshCoreObserver(_stash.meshcore_observer);
      }
    }
    if (name === 'gps' && _stash.gps && RPI.updateGps) RPI.updateGps(_stash.gps);
    if (name === 'ntp' && _stash.ntp && RPI.updateNtp) RPI.updateNtp(_stash.ntp);
    if (name === 'hotspot' && _stash.hotspot && RPI.updateHotspot) {
      RPI.updateHotspot(_stash.hotspot, _stash.captive_portal || null);
    }
    if (name === 'link-tester' && _stash.link_tester && RPI.updateLinkTester) {
      RPI.updateLinkTester(_stash.link_tester);
    }
    if (name === 'weather-alert' && _stash.weather_alert && RPI.updateWeatherAlert) {
      RPI.updateWeatherAlert(_stash.weather_alert);
    }
    if (name === 'ais' && _stash.ais && RPI.updateAis) RPI.updateAis(_stash.ais);
    if (name === 'acars' && _stash.acars && RPI.updateAcars) RPI.updateAcars(_stash.acars);
    if (name === 'radiosonde' && _stash.radiosonde && RPI.updateRadiosonde) {
      RPI.updateRadiosonde(_stash.radiosonde);
    }
    if (name === 'noaa' && _stash.noaa_apt && RPI.updateNoaa) RPI.updateNoaa(_stash.noaa_apt);
  }
  RPI.replayFeature = _replayFeature;

  registerFeature('messages', {
    asset: 'feature-messages.js',
    sections: ['msg-lxmf-section', 'mqtt-feed-section', 'msg-lora-section', 'msg-meshcore-section'],
    toggles: ['msg-lxmf-toggle', 'mqtt-feed-toggle', 'msg-lora-toggle', 'msg-meshcore-toggle'],
    bodies: ['msg-lxmf-body', 'mqtt-feed-body', 'msg-lora-body', 'msg-meshcore-body']
  });
  registerFeature('map', {
    asset: 'feature-map.js',
    sections: ['map-section', 'node-tracker-section'],
    bodies: ['map-body', 'node-tracker-body'],
    dependencies: ['leaflet'],
    hideUntilAvailable: false
  });
  registerFeature('adsb', {
    asset: 'feature-adsb.js', sections: ['adsb-section'], toggles: ['adsb-toggle'],
    bodies: ['adsb-body'], dependencies: ['leaflet']
  });
  registerFeature('space', {
    asset: 'feature-space.js', sections: ['space-section'], toggles: ['space-toggle'],
    bodies: ['space-body'], dependencies: ['leaflet']
  });
  registerFeature('radio', {
    asset: 'feature-radio.js', sections: ['radio-section'], toggles: ['radio-toggle'], bodies: ['radio-body']
  });
  registerFeature('lora', {
    asset: 'feature-lora.js', sections: ['lora-section'],
    hideUntilAvailable: false, loadWhenVisible: true
  });
  registerFeature('mesh', {
    asset: 'feature-mesh.js', sections: ['mesh-section', 'telemetry-section'],
    toggles: ['telemetry-toggle'], bodies: ['telemetry-body'], loadWhenVisible: true
  });
  registerFeature('routing', {
    asset: 'feature-routing.js', sections: ['routing-section'], loadWhenVisible: true
  });
  registerFeature('mesh-bridge', {
    asset: 'feature-mesh-bridge.js', sections: ['mesh-bridge-section'],
    toggles: ['mesh-bridge-section-toggle'], bodies: ['mesh-bridge-section-body']
  });
  registerFeature('meshtastic', {
    asset: 'feature-meshtastic.js', sections: ['meshtastic-section'], loadWhenVisible: true
  });
  registerFeature('meshcore', {
    asset: 'feature-meshcore.js', sections: ['meshcore-section'], loadWhenVisible: true
  });
  registerFeature('gps', {
    asset: 'feature-gps.js', sections: ['gps-section'], dependencies: ['leaflet'], loadWhenVisible: true
  });
  registerFeature('ntp', {
    asset: 'feature-ntp.js', sections: ['ntp-section'], loadWhenVisible: true
  });
  registerFeature('link-tester', {
    asset: 'feature-link-tester.js', sections: ['link-tester-section'],
    toggles: ['link-tester-toggle'], bodies: ['link-tester-body']
  });
  registerFeature('hotspot', {
    asset: 'feature-hotspot.js', sections: ['hotspot-section'],
    toggles: ['hotspot-toggle'], bodies: ['hotspot-body']
  });
  registerFeature('weather-alert', {
    asset: 'feature-weather-alert.js', sections: ['weather-alert-section'],
    toggles: ['weather-alert-toggle'], bodies: ['weather-alert-body']
  });
  registerFeature('ais', {
    asset: 'feature-ais.js', sections: ['ais-section'], toggles: ['ais-toggle'], bodies: ['ais-body']
  });
  registerFeature('acars', {
    asset: 'feature-acars.js', sections: ['acars-section'], toggles: ['acars-toggle'], bodies: ['acars-body']
  });
  registerFeature('radiosonde', {
    asset: 'feature-radiosonde.js', sections: ['radiosonde-section'],
    toggles: ['radiosonde-toggle'], bodies: ['radiosonde-body']
  });
  registerFeature('noaa', {
    asset: 'feature-noaa.js', sections: ['noaa-section'], toggles: ['noaa-toggle'], bodies: ['noaa-body']
  });

  // Capture the first activation before a legacy feature can toggle its body.
  // After import/init, replay that one click so existing keyboard and pointer
  // behavior remains unchanged.
  document.addEventListener('click', function(event) {
    var toggle = event.target.closest && event.target.closest('.section-header.collapsible');
    var name = toggle && _featureByToggle[toggle.id];
    var feature = name && _features[name];
    if (!feature || feature.replayingClick) return;
    if (feature.active) {
      _fetchFeatureData(name);
      return;
    }
    event.preventDefault();
    event.stopImmediatePropagation();
    feature.desired = true;
    if (!feature.available) {
      showToast('This panel is not available until its plugin is ready.', 'warning');
      return;
    }
    loadFeature(name).then(function() {
      feature.replayingClick = true;
      try { toggle.click(); } finally { feature.replayingClick = false; }
    }).catch(function() {});
  }, true);

  // --- Section freshness tracking ---

  var _sectionUpdated = {};  // sectionId -> timestamp (seconds)
  var _STALE_THRESHOLD = 30; // seconds before marking stale
  var _GLOBAL_STALE_THRESHOLD = 60;
  var _lastWsUpdate = 0;
  var _lastHttpUpdate = 0;
  var _lastSnapshotRequest = 0;
  var _tabHiddenSince = 0;
  var _TAB_STALE_SECONDS = 30;
  var _bootTs = Date.now() / 1000;
  var _NEVER_DATA_GRACE = 20;  // seconds to wait before warning that no data ever arrived

  function markUpdated(sectionId) {
    _sectionUpdated[sectionId] = Date.now() / 1000;
    var sec = document.getElementById(sectionId);
    if (sec) sec.classList.remove('awaiting-data');
  }

  function _refreshFreshness() {
    var now = Date.now() / 1000;
    for (var id in _sectionUpdated) {
      var el = document.querySelector('#' + id + ' .freshness');
      if (!el) continue;
      var age = Math.floor(now - _sectionUpdated[id]);
      if (age < 2) { el.textContent = 'just now'; el.className = 'freshness'; }
      else if (age < 60) { el.textContent = age + 's ago'; el.className = 'freshness' + (age >= _STALE_THRESHOLD ? ' stale' : ''); }
      else { el.textContent = Math.floor(age / 60) + 'm ago'; el.className = 'freshness stale'; }
    }
  }
  setInterval(_refreshFreshness, 2000);

  function _showStaleBanner() {
    var el = document.getElementById('stale-banner');
    if (el) el.style.display = 'flex';
  }

  function _hideStaleBanner() {
    var el = document.getElementById('stale-banner');
    if (el && el.style.display !== 'none') el.style.display = 'none';
  }

  function _requestSnapshot() {
    var now = Date.now() / 1000;
    if (now - _lastSnapshotRequest < 10) return;
    _lastSnapshotRequest = now;
    if (RPI.ws && RPI.ws.readyState === WebSocket.OPEN) {
      RPI.ws.send(JSON.stringify({action: 'request_snapshot'}));
    }
  }

  function _manualRefresh() {
    var now = Date.now() / 1000;
    if (now - _lastSnapshotRequest < 10) return;
    _lastSnapshotRequest = now;
    var btn = $('stale-refresh-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'Refreshing…'; }
    if (RPI.ws && RPI.ws.readyState === WebSocket.OPEN) {
      RPI.ws.send(JSON.stringify({action: 'request_snapshot'}));
    } else {
      fetchCritical();
      fetchSecondary();
      fetchWsUncovered();
      connectWS();
    }
    setTimeout(function() {
      if (btn) { btn.disabled = false; btn.textContent = 'Refresh'; }
    }, 3000);
  }

  function _checkStaleness() {
    var latest = Math.max(_lastWsUpdate, _lastHttpUpdate);
    if (!latest) {
      // No data has ever arrived. If we're past the boot grace window, the
      // pipeline never delivered -- surface the stale banner so it's visible.
      if ((Date.now() / 1000) - _bootTs > _NEVER_DATA_GRACE) _showStaleBanner();
      return;
    }
    var age = (Date.now() / 1000) - latest;
    if (age > _GLOBAL_STALE_THRESHOLD) _showStaleBanner();
    else if (age < _STALE_THRESHOLD) _hideStaleBanner();
  }
  setInterval(_checkStaleness, 10000);

  document.addEventListener('visibilitychange', function() {
    if (document.hidden) {
      _tabHiddenSince = Date.now() / 1000;
    } else if (_tabHiddenSince > 0) {
      var away = (Date.now() / 1000) - _tabHiddenSince;
      _tabHiddenSince = 0;
      if (away >= _TAB_STALE_SECONDS) {
        _checkStaleness();
        _requestSnapshot();
      }
    }
  });

  // --- Rendering ---

  var _prevMetricValues = {};

  function flashCard(card) {
    if (!card) return;
    card.classList.remove('flash');
    void card.offsetWidth;
    card.classList.add('flash');
  }

  function setMetric(id, value, unit, warnAt, critAt) {
    var el = $(id);
    if (!el) return;
    var card = el.closest('.metric-card');
    var ringId = id.replace('m-', 'ring-');
    var ring = $(ringId);

    if (value == null || value === undefined) {
      el.innerHTML = '--<span class="unit">' + unit + '</span>';
      el.className = 'value';
      if (card) card.className = 'metric-card';
      if (ring) ring.style.setProperty('--pct', '0');
      return;
    }
    var display = (typeof value === 'number') ? value.toFixed(1) : value;
    el.innerHTML = esc(String(display)) + '<span class="unit">' + unit + '</span>';
    var cls = metricClass(value, warnAt, critAt);
    el.className = 'value ' + cls;
    if (card) card.className = 'metric-card ' + cls;

    if (ring) {
      var pct = Math.min(100, Math.max(0, value));
      ring.style.setProperty('--pct', pct);
    }

    var prevVal = _prevMetricValues[id];
    _prevMetricValues[id] = display;
    if (prevVal !== undefined && prevVal !== display && card) {
      flashCard(card);
    }
  }

  function formatTimeAgo(timestamp) {
    if (!timestamp) return '--';
    var seconds = Math.floor(Date.now() / 1000 - timestamp);
    if (seconds < 0) seconds = 0;
    if (seconds < 60) return seconds + 's ago';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm ago';
    if (seconds < 86400) return Math.floor(seconds / 3600) + 'h ago';
    return Math.floor(seconds / 86400) + 'd ago';
  }

  // Shared detail-item builder used by mesh.js, lora.js, and meshtastic.js
  // _di escapes value for safe display; use _diRaw for pre-escaped HTML values
  function _di(label, value, cls) {
    return '<div class="node-detail-item">'
      + '<span class="node-detail-label">' + label + '</span>'
      + '<span class="node-detail-value' + (cls ? ' ' + cls : '') + '">' + esc(value) + '</span>'
      + '</div>';
  }

  function _diRaw(label, value, cls) {
    return '<div class="node-detail-item">'
      + '<span class="node-detail-label">' + label + '</span>'
      + '<span class="node-detail-value' + (cls ? ' ' + cls : '') + '">' + value + '</span>'
      + '</div>';
  }

  function onWsReady(fn) {
    if (_wsFirstTick) { fn(); return; }
    _wsReadyCallbacks.push(fn);
  }

  /* ── Expose shared utilities for sub-modules ─────────────────────── */
  RPI.api = api;
  RPI.apiRetry = apiRetry;
  RPI.$ = $;
  RPI.esc = esc;
  RPI.formatUptime = formatUptime;
  RPI.formatBytes = formatBytes;
  RPI.formatRate = formatRate;
  RPI.metricClass = metricClass;
  RPI.markUpdated = markUpdated;
  RPI.setMetric = setMetric;
  RPI.formatTimeAgo = formatTimeAgo;
  RPI._di = _di;
  RPI._diRaw = _diRaw;
  RPI.onWsReady = onWsReady;

  // Shared mutable object used by both mesh.js and lora.js
  RPI._reachScores = {};

  // --- Metrics ---

  var _metricHistory = { cpu: [], temp: [], mem: [], disk: [], ws_latency: [], ws_msgrate: [], ws_clients: [] };
  var _METRIC_HISTORY_MAX = 30;
  var _wsLatency = null;
  var _wsMsgRateWindow = [];
  var _wsPingTimer = null;
  var _wsMaxClients = 10;

  function pushMetricHistory(key, value) {
    if (value == null) return;
    var arr = _metricHistory[key];
    arr.push(value);
    if (arr.length > _METRIC_HISTORY_MAX) arr.shift();
  }

  function renderMetricSparkline(containerId, values) {
    var el = $(containerId);
    if (!el || !values || values.length < 2) { if (el) el.innerHTML = ''; return; }
    var sig = values.length + ':' + values[values.length - 1].toFixed(1);
    if (el._lastSig === sig) return;
    el._lastSig = sig;
    var min = Infinity, max = -Infinity;
    for (var i = 0; i < values.length; i++) {
      if (values[i] < min) min = values[i];
      if (values[i] > max) max = values[i];
    }
    var range = max - min || 1;
    var w = 160, h = 24, pad = 2;
    var points = [];
    for (var j = 0; j < values.length; j++) {
      var x = (j / (values.length - 1)) * w;
      var y = h - pad - ((values[j] - min) / range) * (h - pad * 2);
      points.push(x.toFixed(1) + ',' + y.toFixed(1));
    }
    var polyline = points.join(' ');
    var area = polyline + ' ' + w + ',' + h + ' 0,' + h;
    el.innerHTML = '<svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none">'
      + '<polygon class="spark-area" points="' + area + '"/>'
      + '<polyline class="spark-line" points="' + polyline + '"/>'
      + '</svg>';
  }

  function updateHeaderHealth(metrics) {
    var header = document.querySelector('.header');
    if (!header) return;
    var vals = [
      metricClass(metrics.cpu_percent, 70, 90),
      metricClass(metrics.cpu_temp, 65, 80),
      metricClass(metrics.memory_percent, 70, 90),
      metricClass(metrics.disk_percent, 80, 95)
    ];
    var color;
    if (vals.indexOf('metric-crit') >= 0) color = 'var(--red)';
    else if (vals.indexOf('metric-warn') >= 0) color = 'var(--yellow)';
    else color = 'var(--green)';
    header.style.setProperty('--health-color', color);
  }

  function updateMetrics(metrics) {
    if (!metrics) return;
    markUpdated('metrics-grid');
    setMetric('m-cpu', metrics.cpu_percent, '%', 70, 90);
    setMetric('m-temp', metrics.cpu_temp, '\u00B0C', 65, 80);
    setMetric('m-mem', metrics.memory_percent, '%', 70, 90);
    setMetric('m-disk', metrics.disk_percent, '%', 80, 95);

    pushMetricHistory('cpu', metrics.cpu_percent);
    pushMetricHistory('temp', metrics.cpu_temp);
    pushMetricHistory('mem', metrics.memory_percent);
    pushMetricHistory('disk', metrics.disk_percent);
    renderMetricSparkline('spark-cpu', _metricHistory.cpu);
    renderMetricSparkline('spark-temp', _metricHistory.temp);
    renderMetricSparkline('spark-mem', _metricHistory.mem);
    renderMetricSparkline('spark-disk', _metricHistory.disk);

    updateHeaderHealth(metrics);
  }

  function updateWsStats(wsStats) {
    if (wsStats) {
      _wsMaxClients = wsStats.max_clients || 10;
      var clients = wsStats.clients;
      var clientWarn = Math.round(_wsMaxClients * 0.7);
      var clientCrit = Math.round(_wsMaxClients * 0.9);
      setMetric('m-ws-clients', clients, '', clientWarn, clientCrit);
      var clientRing = $('ring-ws-clients');
      if (clientRing) clientRing.style.setProperty('--pct', Math.min(100, (clients / _wsMaxClients) * 100));
      pushMetricHistory('ws_clients', clients);
      renderMetricSparkline('spark-ws-clients', _metricHistory.ws_clients);
    }

    if (_wsLatency !== null) {
      setMetric('m-ws-latency', _wsLatency, 'ms', 200, 400);
      var latRing = $('ring-ws-latency');
      if (latRing) latRing.style.setProperty('--pct', Math.min(100, (_wsLatency / 500) * 100));
      pushMetricHistory('ws_latency', _wsLatency);
      renderMetricSparkline('spark-ws-latency', _metricHistory.ws_latency);
    }

    var now = Date.now();
    while (_wsMsgRateWindow.length && _wsMsgRateWindow[0] < now - 10000) _wsMsgRateWindow.shift();
    var rate = _wsMsgRateWindow.length / 10;
    var rateDisplay = rate.toFixed(1);
    var rateEl = $('m-ws-msgrate');
    if (rateEl) rateEl.innerHTML = esc(rateDisplay) + '<span class="unit">/s</span>';
    var rateCard = document.getElementById('card-ws-msgrate');
    if (rateCard) rateCard.className = 'metric-card ' + (rate < 0.05 ? 'metric-warn' : 'metric-ok');
    var rateRing = $('ring-ws-msgrate');
    if (rateRing) rateRing.style.setProperty('--pct', Math.min(100, (rate / 2) * 100));
    pushMetricHistory('ws_msgrate', rate);
    renderMetricSparkline('spark-ws-msgrate', _metricHistory.ws_msgrate);
  }

  // --- Plugins ---

  function updatePlugins(plugins, failedPlugins) {
    _markFeaturesFromPlugins(plugins);
    var tbody = $('plugins-table');
    if (!tbody) return;
    var html = '';
    var count = 0;

    if (plugins) {
      var names = Object.keys(plugins).sort();
      count = names.length;
      for (var i = 0; i < names.length; i++) {
        var name = names[i];
        var p = plugins[name];
        var st = p.status || {};
        var active = st.active;
        var dotClass = active ? 'status-active' : 'status-inactive';
        var statusText = active ? 'Active' : 'Stopped';

        // Build details from status keys
        var details = [];
        if (st.web_url) details.push('URL: ' + st.web_url);
        if (st.pid) details.push('PID: ' + st.pid);
        if (st.restart_count > 0) details.push('Restarts: ' + st.restart_count);

        var addr = p.address || '';

        html += '<tr>'
          + '<td>' + esc(name) + '</td>'
          + '<td>' + esc(p.version || '--') + '</td>'
          + '<td><span class="status-dot ' + dotClass + '" title="' + statusText + '"></span></td>'
          + '<td class="addr">' + esc(addr || '--') + '</td>'
          + '<td>' + esc(details.join(', ') || p.description || '') + '</td>'
          + '</tr>';
      }
    }

    // Failed plugins
    if (failedPlugins && failedPlugins.length > 0) {
      for (var j = 0; j < failedPlugins.length; j++) {
        var fp = failedPlugins[j];
        html += '<tr>'
          + '<td>' + esc(fp.name) + '</td>'
          + '<td>--</td>'
          + '<td><span class="status-dot status-failed" title="Failed"></span></td>'
          + '<td>--</td>'
          + '<td>' + esc(fp.error) + '</td>'
          + '</tr>';
        count++;
      }
    }

    tbody.innerHTML = html;
    $('plugin-count').textContent = count + ' total';

    // Failed alert
    var alertEl = $('failed-alert');
    if (failedPlugins && failedPlugins.length > 0) {
      $('failed-list').textContent = failedPlugins.map(function(f) { return f.name + ': ' + f.error; }).join('; ');
      alertEl.classList.remove('hidden');
    } else {
      alertEl.classList.add('hidden');
    }
  }

  // --- Interfaces ---

  function updateInterfaces(interfaces) {
    var tbody = $('interfaces-table');
    if (!tbody) return;
    markUpdated('interfaces-section');
    lastLiveIfaces = interfaces || [];

    // Build merged list: union of config interfaces and live interfaces
    var merged = [];
    var liveByName = {};
    for (var i = 0; i < lastLiveIfaces.length; i++) {
      // Live interface names from rnsd look like "TCPInterface[TCP Client beleth/host:port]"
      // Extract the label portion for matching against config names
      var raw = lastLiveIfaces[i].name || '';
      var label = _extractIfaceLabel(raw);
      liveByName[label] = lastLiveIfaces[i];
    }

    // Start with config interfaces (preserves config order, shows disabled ones)
    var seen = {};
    var configNames = Object.keys(configIfaces);
    for (var c = 0; c < configNames.length; c++) {
      var cname = configNames[c];
      var cfg = configIfaces[cname];
      var live = liveByName[cname] || null;
      merged.push({name: cname, cfg: cfg, live: live});
      seen[cname] = true;
    }
    // Add any live interfaces not in config (shouldn't happen, but be safe)
    for (var lname in liveByName) {
      if (!seen[lname]) {
        merged.push({name: lname, cfg: null, live: liveByName[lname]});
      }
    }

    if (merged.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5">No interfaces detected</td></tr>';
      $('iface-count').textContent = '0';
      return;
    }

    var now = Date.now() / 1000;
    var html = '';
    var activeCount = 0;
    for (var m = 0; m < merged.length; m++) {
      var entry = merged[m];
      var isEnabled = entry.cfg ? entry.cfg.enabled : true;
      var isLive = entry.live != null;
      var online = isLive && entry.live.online !== false;
      var rowClass = isEnabled ? '' : ' class="row-disabled"';

      // Toggle switch (no inline handler -- CSP blocks inline scripts)
      var toggleHtml = '';
      if (entry.cfg) {
        var checked = isEnabled ? ' checked' : '';
        toggleHtml = '<label class="toggle-switch">'
          + '<input type="checkbox"' + checked + ' data-iface="' + esc(entry.name) + '">'
          + '<span class="toggle-slider"></span>'
          + '</label>';
      }

      // Status
      var statusHtml;
      if (!isEnabled) {
        statusHtml = '<span class="text-muted">Disabled</span>';
      } else if (isLive) {
        var dotClass = online ? 'status-active' : 'status-inactive';
        statusHtml = '<span class="status-dot ' + dotClass + '"></span>' + (online ? 'Online' : 'Offline');
        activeCount++;
      } else {
        statusHtml = '<span class="text-muted">Not active</span>';
      }

      // Traffic
      var traffic = '';
      if (isLive && (entry.live.rxb != null || entry.live.txb != null)) {
        var rxRate = null, txRate = null;
        var prev = prevIfaces[entry.name];
        if (prev) {
          var dt = now - prev.time;
          if (dt > 0.5) {
            rxRate = (entry.live.rxb - prev.rxb) / dt;
            txRate = (entry.live.txb - prev.txb) / dt;
          }
        }
        traffic = 'RX: ' + formatBytes(entry.live.rxb);
        if (rxRate != null) traffic += ' (' + formatRate(rxRate) + ')';
        traffic += ' / TX: ' + formatBytes(entry.live.txb);
        if (txRate != null) traffic += ' (' + formatRate(txRate) + ')';
        prevIfaces[entry.name] = {rxb: entry.live.rxb, txb: entry.live.txb, time: now};
      } else if (!isEnabled) {
        traffic = '<span class="text-muted">\u2014</span>';
      }

      // Type from config or live
      var ifaceType = (entry.cfg ? entry.cfg.type : '') || (entry.live ? entry.live.type : '');

      html += '<tr' + rowClass + '>'
        + '<td>' + toggleHtml + '</td>'
        + '<td>' + esc(entry.name) + '</td>'
        + '<td>' + esc(ifaceType) + '</td>'
        + '<td>' + statusHtml + '</td>'
        + '<td>' + traffic + '</td>'
        + '</tr>';
    }

    tbody.innerHTML = html;
    var total = configNames.length || lastLiveIfaces.length;
    $('iface-count').textContent = activeCount + '/' + total;
  }

  function _extractIfaceLabel(rnsName) {
    // "TCPInterface[TCP Client beleth/host:port]" -> "TCP Client beleth"
    // "AutoInterface[Auto Discovery Interface]" -> "Auto Discovery Interface"
    var m = rnsName.match(/\[([^\]\/]+)/);
    if (m) return m[1].trim();
    return rnsName;
  }

  function fetchInterfacesConfig() {
    api('/api/interfaces/config').then(function(r) {
      if (!r || !r.ok) return;
      configIfaces = {};
      var ifaces = r.data.interfaces || [];
      for (var i = 0; i < ifaces.length; i++) {
        configIfaces[ifaces[i].name] = ifaces[i];
      }
      // Re-render with merged data
      updateInterfaces(lastLiveIfaces);
    });
  }

  window._toggleIface = function(name) {
    // Optimistic update so WebSocket re-renders don't revert the toggle
    var prev = configIfaces[name] ? configIfaces[name].enabled : true;
    if (configIfaces[name]) configIfaces[name].enabled = !prev;
    pendingRestart = true;
    $('restart-banner').classList.remove('hidden');
    updateInterfaces(lastLiveIfaces);

    api('/api/interfaces/' + encodeURIComponent(name) + '/toggle', {method: 'POST'})
      .then(function(r) {
        if (!r || !r.ok) {
          // Revert on failure
          if (configIfaces[name]) configIfaces[name].enabled = prev;
          updateInterfaces(lastLiveIfaces);
          alert('Toggle failed: ' + (r ? r.error : 'no response'));
          return;
        }
        // Confirm with server state
        if (configIfaces[name]) configIfaces[name].enabled = r.data.enabled;
      });
  };

  window._setLoraAnnounceMode = function(mode) {
    var sel = $('lora-announce-mode');
    if (sel) sel.disabled = true;
    api('/api/lora/announce_mode', {
      method: 'POST',
      json: {mode: mode}
    }).then(function(r) {
      if (!r || !r.ok) {
        alert('Failed to set announce mode: ' + (r ? (r.error || 'unknown error') : 'no response'));
        // Revert select
        if (sel) { sel.value = RPI._currentLoraAnnounceMode(); sel.disabled = false; }
        return;
      }
      RPI._setCurrentLoraAnnounceMode(mode);
      if (sel) sel.disabled = false;
      // rnsd was restarted -- data will refresh on next poll cycle
    });
  };

  function showPasswordChangeDialog() {
    var dialog = $('password-change-dialog');
    var current = $('current-dashboard-password');
    if (!dialog) return;
    if (!dialog.open) dialog.showModal();
    if (current) current.focus();
  }

  function submitPasswordChange() {
    var current = $('current-dashboard-password');
    var replacement = $('new-dashboard-password');
    var confirmation = $('confirm-dashboard-password');
    var feedback = $('password-change-feedback');
    var submit = $('password-change-submit');
    if (!current || !replacement || !confirmation) return;
    if (replacement.value.length < 12) {
      feedback.textContent = 'Use at least 12 characters for the new password.';
      replacement.focus();
      return;
    }
    if (replacement.value !== confirmation.value) {
      feedback.textContent = 'The new passwords do not match.';
      confirmation.focus();
      return;
    }
    if (submit) submit.disabled = true;
    feedback.textContent = '';
    api('/api/auth/password', {
      method: 'POST',
      json: {
        current_password: current.value,
        new_password: replacement.value
      }
    }).then(function(result) {
      current.value = '';
      replacement.value = '';
      confirmation.value = '';
      if (!result || !result.ok) {
        feedback.textContent = (result && result.error) || 'Password change failed.';
        if (submit) submit.disabled = false;
        current.focus();
        return;
      }
      window.location.href = '/login.html?password_changed=1';
    });
  }

  function doRestart() {
    var dialog = $('restart-dialog');
    var password = $('restart-password');
    var feedback = $('restart-feedback');
    if (!dialog || !password) return;
    password.value = '';
    if (feedback) feedback.textContent = '';
    dialog.showModal();
    password.focus();
  }

  function submitRestart() {
    var dialog = $('restart-dialog');
    var password = $('restart-password');
    var feedback = $('restart-feedback');
    if (!password || !password.value) {
      if (feedback) feedback.textContent = 'Enter your dashboard password.';
      if (password) password.focus();
      return;
    }
    var btn = $('restart-btn');
    var confirmBtn = $('restart-confirm-btn');
    if (confirmBtn) confirmBtn.disabled = true;
    btn.disabled = true;
    btn.textContent = 'Restarting\u2026';
    api('/api/services/restart', {
      method: 'POST',
      headers: {'X-Confirm-Password': password.value}
    }).then(function(r) {
      password.value = '';
      if (!r || !r.ok) {
        btn.disabled = false;
        btn.textContent = 'Restart Services';
        if (confirmBtn) confirmBtn.disabled = false;
        if (feedback) feedback.textContent = (r && r.error) || 'Restart request failed.';
        return;
      }
      if (dialog) dialog.close();
      startRestartWatcher(r.data && r.data.operation_id);
    });
  }

  function startRestartWatcher(operationId) {
    var attempts = 0;
    var maxAttempts = 30;
    var sawUnavailable = false;
    var check = setInterval(function() {
      attempts++;
      var statusPath = operationId
        ? '/api/services/restart/' + encodeURIComponent(operationId)
        : '/api/status';
      fetch(statusPath, {credentials: 'same-origin'})
        .then(function(r) {
          if (r.ok && !operationId) {
            clearInterval(check);
            pendingRestart = false;
            $('restart-banner').classList.add('hidden');
            var btn = $('restart-btn');
            btn.disabled = false;
            btn.textContent = 'Restart Services';
            window.location.reload();
          }
          if (operationId && sawUnavailable && r.status === 404) {
            return fetch('/api/status', {credentials: 'same-origin'}).then(function(statusResp) {
              if (statusResp.ok) window.location.reload();
              return null;
            });
          }
          return r.json().catch(function() { return null; });
        }).then(function(payload) {
          if (!operationId || !payload || !payload.ok) return;
          var state = payload.data && payload.data.state;
          if (state === 'failed' || state === 'cancelled') {
            clearInterval(check);
            var restartBtn = $('restart-btn');
            restartBtn.textContent = 'Restart failed \u2014 try again';
            restartBtn.disabled = false;
          } else if (state === 'complete') {
            clearInterval(check);
            window.location.reload();
          }
        })
        .catch(function() { sawUnavailable = true; });
      if (attempts >= maxAttempts) {
        clearInterval(check);
        var btn = $('restart-btn');
        btn.textContent = 'Restart timed out \u2014 refresh page';
        btn.disabled = false;
      }
    }, 3000);
  }

  // --- Alerts ---

  function updateAlerts(alertData) {
    var el = $('alerts-info');
    if (!el) return;
    if (!alertData || alertData.message === 'alert_system plugin not available') {
      el.textContent = 'Alert system not enabled';
      $('alerts-count').textContent = '';
      return;
    }
    var html = 'Alerts sent: ' + (alertData.alerts_sent || 0);
    if (alertData.last_alert) {
      html += ' | Last: ' + esc(alertData.last_alert.message || '')
        + ' (' + formatTimeAgo(alertData.last_alert.time) + ')';
    }
    html += ' | Recipients: ' + (alertData.recipients || 0);
    el.innerHTML = html;
    $('alerts-count').textContent = (alertData.alerts_sent || 0) + ' sent';
  }

  // --- Shared Files ---

  function updateSharedFiles(files) {
    var tbody = $('files-table');
    if (!tbody) return;
    if (!files || files.length === 0) {
      tbody.innerHTML = '<tr><td colspan="3" class="text-muted">No shared files</td></tr>';
      $('files-count').textContent = '0';
      return;
    }
    var html = '';
    for (var i = 0; i < files.length; i++) {
      var f = files[i];
      html += '<tr>'
        + '<td>' + esc(f.name) + '</td>'
        + '<td>' + formatBytes(f.size) + '</td>'
        + '<td>' + (f.modified ? formatTimeAgo(f.modified) : '--') + '</td>'
        + '</tr>';
    }
    tbody.innerHTML = html;
    $('files-count').textContent = files.length + ' files';
  }

  // -- Sensor rendering ---------------------------------------------------

  var _sensorHistory = {};   // { "sensorName:field": [value, value, ...] }
  var _sensorHistoryMax = 60;

  // Unit & threshold metadata per reading field name
  var SENSOR_FIELDS = {
    temperature: { unit: '\u00b0C', precision: 1,
      thresh: function(v) { return v < 10 ? 'sv-cold' : v < 35 ? 'sv-ok' : v < 45 ? 'sv-warm' : 'sv-hot'; }
    },
    humidity: { unit: '%', precision: 1,
      thresh: function(v) { return v < 25 ? 'sv-dry' : v < 65 ? 'sv-ok' : v < 80 ? 'sv-wet' : 'sv-damp'; }
    },
    pressure: { unit: ' hPa', precision: 1, thresh: function() { return ''; } },
    voltage: { unit: ' V', precision: 2, thresh: function() { return ''; } },
    current: { unit: ' A', precision: 3, thresh: function() { return ''; } },
    power: { unit: ' W', precision: 1, thresh: function() { return ''; } },
    quality: { unit: '', precision: 0, thresh: function() { return ''; } }
  };

  function sensorFieldMeta(key) {
    if (SENSOR_FIELDS[key]) return SENSOR_FIELDS[key];
    // Auto-detect from key name
    if (/temp/i.test(key)) return SENSOR_FIELDS.temperature;
    if (/humid/i.test(key)) return SENSOR_FIELDS.humidity;
    if (/press/i.test(key)) return SENSOR_FIELDS.pressure;
    if (/volt/i.test(key)) return SENSOR_FIELDS.voltage;
    return { unit: '', precision: 2, thresh: function() { return ''; } };
  }

  function buildSparkline(values) {
    if (!values || values.length < 2) return '';
    var min = Infinity, max = -Infinity;
    for (var i = 0; i < values.length; i++) {
      if (values[i] < min) min = values[i];
      if (values[i] > max) max = values[i];
    }
    var range = max - min || 1;
    var w = 200, h = 36, pad = 2;
    var points = [];
    for (var j = 0; j < values.length; j++) {
      var x = (j / (values.length - 1)) * w;
      var y = h - pad - ((values[j] - min) / range) * (h - pad * 2);
      points.push(x.toFixed(1) + ',' + y.toFixed(1));
    }
    var polyline = points.join(' ');
    // Area: close the path to the bottom
    var area = polyline + ' ' + w + ',' + h + ' 0,' + h;
    var last = values[values.length - 1];
    var lastX = w;
    var lastY = h - pad - ((last - min) / range) * (h - pad * 2);
    return '<div class="sensor-sparkline">'
      + '<svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none">'
      + '<defs><linearGradient id="spark-grad" x1="0" y1="0" x2="0" y2="1">'
      + '<stop offset="0%" stop-color="var(--accent)"/>'
      + '<stop offset="100%" stop-color="transparent"/>'
      + '</linearGradient></defs>'
      + '<polygon class="spark-area" points="' + area + '"/>'
      + '<polyline class="spark-line" points="' + polyline + '"/>'
      + '<circle class="spark-dot" cx="' + lastX.toFixed(1) + '" cy="' + lastY.toFixed(1) + '" r="2.5"/>'
      + '</svg></div>';
  }

  function fetchSensorHistory(sensorNames) {
    for (var i = 0; i < sensorNames.length; i++) {
      (function(name) {
        api('/api/sensors/history?sensor=' + encodeURIComponent(name) + '&limit=' + _sensorHistoryMax)
          .then(function(r) {
            if (!r || !r.ok || !r.data.history) return;
            // History comes newest-first; group by reading field and reverse
            var byField = {};
            var hist = r.data.history;
            for (var j = hist.length - 1; j >= 0; j--) {
              var key = name + ':' + hist[j].reading;
              if (!byField[key]) byField[key] = [];
              byField[key].push(hist[j].value);
            }
            for (var k in byField) {
              _sensorHistory[k] = byField[k];
            }
            // Re-render with sparklines now available
            if (_lastSensorData) renderSensorCards(_lastSensorData);
          });
      })(sensorNames[i]);
    }
  }

  var _lastSensorData = null;

  function updateSensors(sensors) {
    var grid = $('sensors-grid');
    if (!grid) return;
    markUpdated('sensors-section');
    if (!sensors || Object.keys(sensors).length === 0) {
      grid.innerHTML = '<div class="sensor-card"><div class="sensor-name text-muted">No sensor plugins active</div></div>';
      $('sensors-count').textContent = '';
      return;
    }
    // Track history from live updates
    var names = Object.keys(sensors);
    for (var i = 0; i < names.length; i++) {
      var reading = sensors[names[i]];
      if (reading.error) continue;
      var fields = Object.keys(reading);
      for (var j = 0; j < fields.length; j++) {
        if (fields[j] === 'timestamp') continue;
        var hk = names[i] + ':' + fields[j];
        if (!_sensorHistory[hk]) _sensorHistory[hk] = [];
        _sensorHistory[hk].push(reading[fields[j]]);
        if (_sensorHistory[hk].length > _sensorHistoryMax) {
          _sensorHistory[hk] = _sensorHistory[hk].slice(-_sensorHistoryMax);
        }
      }
    }
    _lastSensorData = sensors;
    renderSensorCards(sensors);
  }

  function renderSensorCards(sensors) {
    var grid = $('sensors-grid');
    if (!grid) return;
    var names = Object.keys(sensors);
    var html = '';
    for (var i = 0; i < names.length; i++) {
      var name = names[i];
      var reading = sensors[name];
      var hasError = !!reading.error;

      html += '<div class="sensor-card' + (hasError ? ' sensor-error-card' : '') + '">';

      // Header: name + driver badge
      html += '<div class="sensor-card-header">'
        + '<span class="sensor-name">' + esc(name.replace(/_/g, ' ')) + '</span>'
        + '</div>';

      if (hasError) {
        html += '<div class="sensor-readings"><div class="sensor-reading">'
          + '<div class="sensor-reading-value">' + esc(reading.error) + '</div>'
          + '</div></div>';
      } else {
        // Reading values
        html += '<div class="sensor-readings">';
        var fields = Object.keys(reading);
        var primaryField = null;
        for (var j = 0; j < fields.length; j++) {
          var k = fields[j];
          if (k === 'timestamp') continue;
          var v = reading[k];
          if (typeof v !== 'number') continue;
          var meta = sensorFieldMeta(k);
          var cls = meta.thresh(v);
          if (!primaryField) primaryField = k;
          html += '<div class="sensor-reading">'
            + '<div class="sensor-reading-value ' + cls + '">'
            + v.toFixed(meta.precision)
            + '<span class="sensor-unit">' + esc(meta.unit) + '</span>'
            + '</div>'
            + '<div class="sensor-reading-label">' + esc(k) + '</div>'
            + '</div>';
        }
        html += '</div>';

        // Sparkline for primary field
        var hk = name + ':' + primaryField;
        if (_sensorHistory[hk] && _sensorHistory[hk].length >= 2) {
          html += buildSparkline(_sensorHistory[hk]);
        }
      }

      // Freshness
      if (reading.timestamp) {
        var age = (Date.now() / 1000) - reading.timestamp;
        var stale = age > 300;
        html += '<div class="sensor-meta">'
          + '<span class="' + (stale ? 'sensor-stale' : '') + '">'
          + (stale ? '\u26a0 ' : '') + formatTimeAgo(reading.timestamp)
          + '</span></div>';
      }

      html += '</div>';
    }
    grid.innerHTML = html;
    $('sensors-count').textContent = names.length + (names.length === 1 ? ' sensor' : ' sensors');
  }

  // --- Emergency Broadcasts ---

  var PRIORITY_NAMES = {0: 'INFO', 1: 'WARNING', 2: 'CRITICAL', 3: 'EMERGENCY'};
  var PRIORITY_CLASSES = {0: '', 1: 'warn', 2: 'crit', 3: 'crit'};

  function updateEmergency(data) {
    var tbody = $('emergency-table');
    if (!tbody) return;
    markUpdated('emergency-section');
    var messages = data.messages || [];
    if (messages.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" class="text-muted">No emergency broadcasts</td></tr>';
      $('emergency-count').textContent = '0';
      return;
    }
    var html = '';
    for (var i = 0; i < messages.length; i++) {
      var m = messages[i];
      var pName = PRIORITY_NAMES[m.priority] || 'UNKNOWN';
      var pClass = PRIORITY_CLASSES[m.priority] || '';
      html += '<tr>'
        + '<td><span class="' + pClass + '">' + esc(pName) + '</span></td>'
        + '<td>' + esc(m.message || '') + '</td>'
        + '<td>' + esc(m.origin_name || m.origin || 'Unknown') + '</td>'
        + '<td>' + formatTimeAgo(m.timestamp) + '</td>'
        + '<td>' + (m.ttl || 0) + '</td>'
        + '</tr>';
    }
    tbody.innerHTML = html;
    $('emergency-count').textContent = messages.length + ' messages';
  }

  // --- Connectivity Health ---

  function updateConnectivity(data) {
    if (!data || !$('connectivity-indicators')) return;
    markUpdated('connectivity-section');

    // rnsd indicator
    var ciRnsd = $('ci-rnsd');
    if (ciRnsd) {
      var rnsdOk = data.rnsd_reachable;
      ciRnsd.textContent = 'rnsd: ' + (rnsdOk ? 'UP' : 'DOWN');
      ciRnsd.className = 'conn-indicator ' + (rnsdOk ? 'ci-ok' : 'ci-err');
    }

    // I2P indicator
    var ciI2p = $('ci-i2p');
    if (ciI2p) {
      var i2pSt = data.i2p_status || 'unknown';
      var i2pPeers = data.i2p_peers || 0;
      var i2pCls = 'ci-info';
      var i2pTxt = 'I2P: ' + i2pSt;
      if (i2pSt === 'ok') { i2pCls = 'ci-ok'; i2pTxt = 'I2P: OK' + (i2pPeers > 0 ? ' (' + i2pPeers + ' RNS peers)' : ''); }
      else if (i2pSt === 'connected') { i2pCls = 'ci-ok'; i2pTxt = 'I2P: ' + i2pPeers + ' peers'; }
      else if (i2pSt === 'firewalled') { i2pCls = 'ci-info'; i2pTxt = 'I2P: firewalled (NAT)'; }
      else if (i2pSt === 'bootstrapping') { i2pCls = 'ci-info'; i2pTxt = 'I2P: bootstrapping'; }
      else if (i2pSt === 'testing') { i2pCls = 'ci-info'; i2pTxt = 'I2P: testing'; }
      else if (i2pSt === 'sam_unreachable') { i2pCls = 'ci-err'; i2pTxt = 'I2P: SAM down'; }
      ciI2p.textContent = i2pTxt;
      ciI2p.className = 'conn-indicator ' + i2pCls;
    }

    // SAM indicator
    var ciSam = $('ci-sam');
    if (ciSam) {
      var samOk = data.sam_reachable;
      ciSam.textContent = 'SAM: ' + (samOk ? 'OK' : 'DOWN');
      ciSam.className = 'conn-indicator ' + (samOk ? 'ci-ok' : 'ci-err');
    }

    // Interfaces indicator
    var ciIfaces = $('ci-ifaces');
    if (ciIfaces) {
      var on = data.interfaces_online || 0;
      var total = data.interfaces_total || 0;
      var ifCls = on === total && total > 0 ? 'ci-ok' : (on > 0 ? 'ci-warn' : 'ci-err');
      ciIfaces.textContent = 'Interfaces: ' + on + '/' + total;
      ciIfaces.className = 'conn-indicator ' + ifCls;
    }

    // Paths indicator
    var ciPaths = $('ci-paths');
    if (ciPaths) {
      var pc = data.path_count || 0;
      var pathCls = pc > 100 ? 'ci-ok' : (pc > 0 ? 'ci-info' : 'ci-warn');
      ciPaths.textContent = 'Paths: ~' + pc;
      ciPaths.className = 'conn-indicator ' + pathCls;
    }

    // Issues list
    var issuesEl = $('connectivity-issues');
    if (issuesEl) {
      var issues = data.issues || [];
      if (issues.length === 0) {
        issuesEl.innerHTML = '';
      } else {
        var html = '';
        for (var i = 0; i < issues.length; i++) {
          var isCritical = issues[i].toLowerCase().indexOf('unreachable') >= 0
            || issues[i].toLowerCase().indexOf('all') >= 0;
          html += '<div class="issue' + (isCritical ? ' critical' : '') + '">\u26A0 ' + esc(issues[i]) + '</div>';
        }
        issuesEl.innerHTML = html;
      }
    }

    // Overall status
    var statusEl = $('connectivity-status');
    if (statusEl) {
      var issues = data.issues || [];
      if (issues.length === 0) {
        statusEl.textContent = 'healthy';
        statusEl.style.color = 'var(--green)';
      } else {
        statusEl.textContent = issues.length + ' issue(s)';
        statusEl.style.color = 'var(--yellow)';
      }
    }
  }

  // --- Messaging Hub --- (code in messages.js module)

  // --- Transport Hubs ---

  // Track previous traffic values for rate calculation (keyed by hub address)
  // Each entry stores {rxb, txb, time} so rates are always computed from
  // the *same* update source's last sample, avoiding WS/polling race conditions.
  var _prevTraffic = {};

  function updateTransport(data) {
    var tbody = $('transport-table');
    if (!tbody) return;
    markUpdated('transport-section');
    var primaries = data.primaries || [];
    var fallbacks = data.active_fallbacks || [];
    var ad = data.auto_discovery || {};
    var poolHubs = ad.connected || [];
    var tcpDisabled = data.tcp_disabled || false;

    var all = primaries
      .concat(fallbacks.map(function(f) { f._tag = 'fallback'; return f; }))
      .concat(poolHubs.map(function(p) { p._tag = 'pool'; p.online = true; return p; }));

    if (all.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" class="text-muted">No TCP transport hubs configured</td></tr>';
      $('transport-count').textContent = '0';
      _updatePoolStatus(ad);
      return;
    }

    var now = Date.now() / 1000;

    var html = '';
    for (var i = 0; i < all.length; i++) {
      var h = all[i];
      var key = (h.target_host || '') + ':' + (h.target_port || '');
      var label = esc(h.name || '--');
      if (h._tag === 'fallback') label += ' <small>(fallback)</small>';
      if (h._tag === 'pool') label += ' <small>(pool)</small>';
      var statusCls = h.online ? 'status-ok' : (h.reconnecting ? 'status-warn' : 'status-err');
      var statusTxt = h.online ? 'Online' : (h.reconnecting ? 'Reconnecting' : 'Offline');
      if (tcpDisabled) {
        statusCls = 'text-muted';
        statusTxt = h.online ? 'Reachable' : 'Unreachable';
      }
      var probeTxt = ' <small class="text-muted">(TCP probe only)</small>';

      var txRate = '--', rxRate = '--';
      if (h.rxb != null && h.txb != null) {
        var prev = _prevTraffic[key];
        if (prev && prev.time) {
          var dt = now - prev.time;
          if (dt > 0.5) {
            var txPerSec = Math.max(0, (h.txb - prev.txb) / dt);
            var rxPerSec = Math.max(0, (h.rxb - prev.rxb) / dt);
            txRate = formatRate(txPerSec);
            rxRate = formatRate(rxPerSec);
          } else {
            txRate = prev.lastTx || '...';
            rxRate = prev.lastRx || '...';
          }
        } else {
          txRate = '...';
          rxRate = '...';
        }
        _prevTraffic[key] = { rxb: h.rxb, txb: h.txb, time: now, lastTx: txRate, lastRx: rxRate };
      }

      html += '<tr>'
        + '<td>' + label + '</td>'
        + '<td class="addr">' + esc(h.target_host || '--') + ':' + (h.target_port || '') + '</td>'
        + '<td><span class="' + statusCls + '">' + statusTxt + '</span>' + probeTxt + '</td>'
        + '<td>\u2191' + txRate + ' \u2193' + rxRate + '</td>'
        + '</tr>';
    }
    tbody.innerHTML = html;
    var online = primaries.filter(function(p) { return p.online; }).length;
    var countText = online + '/' + primaries.length + ' primary';
    if (poolHubs.length > 0) countText += ' + ' + poolHubs.length + ' pool';
    if (tcpDisabled) countText += ' (not connected \u2014 no TCP interfaces enabled)';
    $('transport-count').textContent = countText;
    _updatePoolStatus(ad);
  }

  function _updatePoolStatus(ad) {
    var el = $('pool-status');
    if (!el) return;
    if (!ad.enabled) { el.style.display = 'none'; return; }
    var connected = (ad.connected || []).length;
    var target = ad.target_connections || 0;
    var pool = ad.pool_size || 0;
    var cooldowns = ad.cooldowns ? Object.keys(ad.cooldowns).length : 0;
    var parts = ['Auto-discovery: ' + connected + '/' + target + ' target'];
    parts.push(pool + ' in pool');
    if (cooldowns > 0) parts.push(cooldowns + ' in cooldown');
    el.textContent = parts.join(' \u00b7 ');
    el.style.display = 'block';
  }

  // --- Connection status ---

  function setConnStatus(state) {
    var el = $('conn-status');
    if (!el) return;
    var label = el.querySelector('.conn-label');
    el.className = 'conn-status';
    if (state === 'live') {
      el.classList.add('conn-live');
      if (label) label.textContent = 'live';
      el.title = 'WebSocket connected \u2014 updates every 5s';
    } else if (state === 'polling') {
      el.classList.add('conn-poll');
      if (label) label.textContent = 'polling (10s)';
      el.title = 'WebSocket down \u2014 polling every 10s';
    } else {
      el.classList.add('conn-off');
      if (label) label.textContent = 'disconnected';
      el.title = 'No connection to dashboard server';
    }
  }

  var _offgridActive = false;
  var _lastOnlineState = null;
  var _offgridReenableTimer = null;

  function updateOffgridState(enabled) {
    _offgridActive = !!enabled;
    var toggle = document.getElementById('offgrid-toggle');
    var sw = document.getElementById('offgrid-switch');
    if (toggle) {
      if (_offgridActive) toggle.classList.add('active');
      else toggle.classList.remove('active');
    }
    if (sw) sw.checked = _offgridActive;
    var banner = document.getElementById('internet-status-banner');
    if (banner) {
      if (_offgridActive) {
        banner.style.display = 'block';
        banner.textContent = 'Off Grid Mode Active — internet disabled';
        banner.classList.add('offgrid-active');
      } else {
        banner.classList.remove('offgrid-active');
        banner.textContent = 'Internet Unavailable — some features are limited';
        if (_lastOnlineState === true) {
          banner.style.display = 'none';
        }
      }
    }
  }

  function initOffgridToggle() {
    var sw = document.getElementById('offgrid-switch');
    if (!sw) return;
    sw.addEventListener('change', function() {
      var enabled = sw.checked;
      sw.disabled = true;
      if (_offgridReenableTimer) clearTimeout(_offgridReenableTimer);
      _offgridReenableTimer = setTimeout(function() {
        sw.disabled = false;
        sw.checked = _offgridActive;
        _offgridReenableTimer = null;
      }, 5000);
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({action: 'set_offgrid_mode', enabled: enabled}));
      } else {
        api('/api/offgrid', {method: 'POST', json: {enabled: enabled}}).then(function(r) {
          if (_offgridReenableTimer) clearTimeout(_offgridReenableTimer);
          _offgridReenableTimer = null;
          sw.disabled = false;
          if (!r || !r.ok) sw.checked = _offgridActive;
        });
      }
    });
  }

  function updateInternetStatus(info) {
    var online, wanIp, lanIp, forceOffline;
    if (typeof info === 'boolean') {
      online = info;
      wanIp = null;
      lanIp = null;
      forceOffline = false;
    } else if (info && typeof info === 'object') {
      online = info.online;
      wanIp = info.wan_ip || null;
      lanIp = info.lan_ip || null;
      forceOffline = !!info.force_offline;
    } else {
      return;
    }

    _lastOnlineState = online;
    updateOffgridState(forceOffline);

    var banner = document.getElementById('internet-status-banner');
    if (banner && !forceOffline) {
      banner.style.display = online ? 'none' : 'block';
    }

    var badge = $('inet-status');
    if (badge) {
      badge.className = 'badge badge-inet ' + (online ? 'inet-online' : 'inet-offline');
      var lbl = badge.querySelector('.inet-label');
      if (lbl) lbl.textContent = online ? 'online' : 'offline';
      badge.title = 'Internet: ' + (online ? 'online' : 'offline');
    }

    var wanEl = $('wan-ip');
    if (wanEl) wanEl.textContent = wanIp || '';
    var lanEl = $('lan-ip');
    if (lanEl) lanEl.textContent = lanIp || '';
  }

  // --- Toast notifications ---

  var _toastContainer = null;

  function _ensureToastContainer() {
    if (_toastContainer) return _toastContainer;
    _toastContainer = document.createElement('div');
    _toastContainer.id = 'toast-container';
    document.body.appendChild(_toastContainer);
    return _toastContainer;
  }

  function showToast(message, level, durationMs) {
    if (durationMs === undefined) durationMs = 6000;
    var container = _ensureToastContainer();
    var el = document.createElement('div');
    el.className = 'toast toast-' + (level || 'info');
    el.textContent = message;
    container.appendChild(el);
    requestAnimationFrame(function() {
      requestAnimationFrame(function() { el.classList.add('show'); });
    });
    function dismiss() {
      el.classList.remove('show');
      el.classList.add('hide');
      el.addEventListener('transitionend', function() { el.remove(); }, {once: true});
      setTimeout(function() { el.remove(); }, 400);
    }
    el.addEventListener('click', dismiss);
    if (durationMs > 0) setTimeout(dismiss, durationMs);
  }
  RPI.showToast = showToast;

  // --- Firmware hang banner & events ---

  var _FW_REASONS = {
    usb_disappeared: 'USB device disappeared',
    probe_timeout: 'device not responding to probe',
    serial_open_timeout: 'serial port open failure'
  };

  function updateFirmwareHangBanner(hang, reason) {
    var banner = document.getElementById('firmware-hang-banner');
    if (!banner) return;
    if (hang) {
      var reasonText = _FW_REASONS[reason] || 'device unresponsive';
      banner.textContent = 'Meshtastic Firmware Hang — ' + reasonText;
      banner.style.display = 'block';
    } else {
      banner.style.display = 'none';
    }
  }

  var _lastFwHang = false;

  function handleFirmwareEvent(data) {
    if (data.hang) {
      var reasonText = _FW_REASONS[data.reason] || 'device unresponsive';
      showToast('Meshtastic firmware hang: ' + reasonText, 'error', 0);
      updateFirmwareHangBanner(true, data.reason);
    } else {
      showToast('Meshtastic firmware recovered', 'success', 8000);
      updateFirmwareHangBanner(false);
    }
    _lastFwHang = data.hang;
    if (RPI.onFirmwareEvent) RPI.onFirmwareEvent(data);
  }

  // --- Config ---

  function fetchConfig() {
    api('/api/config').then(function(r) {
      if (!r || !r.ok) return;
      $('config-content').textContent = JSON.stringify(r.data, null, 2);
    });
  }

  // --- Uptime counter ---

  function startUptimeCounter() {
    if (uptimeTimer) clearInterval(uptimeTimer);
    uptimeTimer = setInterval(function() {
      var elapsed = Date.now() / 1000 - uptimeStart;
      $('uptime').textContent = 'uptime: ' + formatUptime(elapsed);
    }, 1000);
    // Immediate update
    var elapsed = Date.now() / 1000 - uptimeStart;
    $('uptime').textContent = 'uptime: ' + formatUptime(elapsed);
  }

  // --- Server clock ---

  var _clockTimer = null;
  var _serverOffset = 0;
  var _browserTzAbbr = '';
  var _browserIsUtc = true;
  try {
    _browserIsUtc = new Date().getTimezoneOffset() === 0;
    if (!_browserIsUtc) {
      var _tzParts = new Intl.DateTimeFormat('en-US', {timeZoneName: 'short'}).formatToParts(new Date());
      _browserTzAbbr = (_tzParts.find(function(p) { return p.type === 'timeZoneName'; }) || {}).value || '';
    }
  } catch(e) {}

  function startClockTicker() {
    if (_clockTimer) clearInterval(_clockTimer);
    function tick() {
      var el = $('header-clock');
      if (!el) return;
      var now = new Date((Date.now() / 1000 + _serverOffset) * 1000);
      var hh = String(now.getUTCHours()).padStart(2, '0');
      var mm = String(now.getUTCMinutes()).padStart(2, '0');
      var ss = String(now.getUTCSeconds()).padStart(2, '0');
      el.textContent = hh + ':' + mm + ':' + ss + ' UTC';
      var localEl = $('header-clock-local');
      if (localEl && !_browserIsUtc) {
        var lh = String(now.getHours()).padStart(2, '0');
        var lm = String(now.getMinutes()).padStart(2, '0');
        var ls = String(now.getSeconds()).padStart(2, '0');
        localEl.textContent = lh + ':' + lm + ':' + ls + ' ' + _browserTzAbbr;
      }
    }
    tick();
    _clockTimer = setInterval(tick, 1000);
  }

  function updateHeaderNtpSync(d) {
    if (!d) return;
    var el = $('header-ntp-sync');
    if (!el) return;
    var state = d.sync_state || 'unknown';

    var cls = 'badge badge-ntp-sync ';
    if (state === 'synced') cls += 'ntp-synced';
    else if (state === 'gps_disciplined') cls += 'ntp-gps';
    else if (state === 'unsynced') cls += 'ntp-unsynced';
    else cls += 'ntp-unknown';
    el.className = cls;

    var text;
    if (state === 'gps_disciplined') text = 'GPS';
    else if (state === 'synced') text = 'NTP';
    else if (state === 'unsynced') text = 'UNSYNC';
    else text = '--';
    el.textContent = text;

    var parts = [state];
    if (d.stratum != null) parts.push('stratum ' + d.stratum);
    if (d.offset_ms != null) parts.push('offset ' + Number(d.offset_ms).toFixed(3) + 'ms');
    if (d.ref_id) parts.push('ref: ' + d.ref_id);
    el.title = parts.join(' | ');
  }

  // --- Data fetching ---

  var _nodeLoaded = false;
  function fetchNode() {
    if (!$('node-name')) return;
    apiRetry('/api/node').then(function(r) {
      if (!r || !r.ok) return;
      _nodeLoaded = true;
      var d = r.data;
      $('node-name').textContent = d.node_name || 'ReticulumPi';
      $('version').textContent = 'v' + (d.version || '?');
      $('identity-hash').textContent = d.identity_hash || '';
      uptimeStart = Date.now() / 1000 - (d.uptime || 0);
      startUptimeCounter();
      if (d.server_time) {
        _serverOffset = d.server_time - Date.now() / 1000;
      }
      startClockTicker();
    });
  }

  // ── Tiered fetch: Critical (above-fold), Secondary (WS-covered
  //    fallback), WsUncovered (always needed), Deferred (on expand) ──

  function fetchCritical() {
    apiRetry('/api/metrics').then(function(r) {
      if (r && r.ok) { _lastHttpUpdate = Date.now() / 1000; updateMetrics(r.data); }
    });

    var _ifaceResult = null, _loraResult = null;
    function mergeIfaceLora() {
      if (_ifaceResult === null || _loraResult === null) return;
      _stash.interfaces = _ifaceResult;
      _stash.lora = _loraResult;
      _setFeatureAvailable('lora', true);
      if (RPI.updateLoraRadio) RPI.updateLoraRadio(_ifaceResult, _loraResult);
    }
    apiRetry('/api/interfaces').then(function(r) {
      if (!r || !r.ok) return;
      updateInterfaces(r.data.interfaces);
      _stash.interfaces = r.data.interfaces;
      if (RPI.updateLoraSignal) RPI.updateLoraSignal(r.data.interfaces);
      _ifaceResult = r.data.interfaces;
      mergeIfaceLora();
    });
    apiRetry('/api/lora').then(function(r) {
      _loraResult = (r && r.ok) ? r.data : {};
      _stash.lora = _loraResult;
      mergeIfaceLora();
    });

    apiRetry('/api/plugins').then(function(r) {
      if (r && r.ok) updatePlugins(r.data.plugins, r.data.failed_plugins);
    });
  }

  function fetchSecondary() {
    api('/api/transport').then(function(r) {
      if (r && r.ok) updateTransport(r.data);
    });
    api('/api/connectivity').then(function(r) {
      if (r && r.ok) updateConnectivity(r.data);
    });
  }

  function fetchWsUncovered() {
    if (RPI.fetchMeshNodes) RPI.fetchMeshNodes();
    if (RPI.fetchMeshSummary) RPI.fetchMeshSummary();
    if (RPI.fetchLoraReachability) RPI.fetchLoraReachability();
  }

  // --- WebSocket ---

  function _applyUpdate(d) {
    if (Array.isArray(d._removed)) {
      var sectionByKey = {
        hotspot: 'hotspot-section', captive_portal: 'hotspot-section',
        meshtastic_status: 'meshtastic-section', meshcore_status: 'meshcore-section',
        mesh_bridge: 'mesh-bridge-section', gps: 'gps-section', ntp: 'ntp-section',
        adsb: 'adsb-section', ais: 'ais-section', acars: 'acars-section',
        radiosonde: 'radiosonde-section', noaa_apt: 'noaa-section',
        fm_receiver: 'radio-section', link_tester: 'link-tester-section',
        weather_alert: 'weather-alert-section', space: 'space-section',
        mesh: 'mesh-section', routing: 'routing-section'
      };
      var featureByKey = {
        mesh: 'mesh', routing: 'routing', mesh_bridge: 'mesh-bridge',
        meshtastic_status: 'meshtastic', meshtastic_nodes: 'meshtastic',
        meshtastic_device: 'meshtastic', meshtastic_lora_neighbors: 'meshtastic',
        meshcore_status: 'meshcore', meshcore_contacts: 'meshcore',
        meshcore_device: 'meshcore', meshcore_observer: 'meshcore',
        gps: 'gps', ntp: 'ntp', hotspot: 'hotspot', link_tester: 'link-tester',
        weather_alert: 'weather-alert', ais: 'ais', acars: 'acars',
        radiosonde: 'radiosonde', noaa_apt: 'noaa', messaging: 'messages',
        adsb: 'adsb', space: 'space', fm_receiver: 'radio'
      };
      d._removed.forEach(function(key) {
        delete _stash[key];
        if (featureByKey[key]) _setFeatureAvailable(featureByKey[key], false);
        var section = $(sectionByKey[key]);
        if (section) section.style.display = 'none';
      });
    }
    if (d.internet !== undefined) {
      updateInternetStatus(d.internet);
    }
    if (d.metrics) updateMetrics(d.metrics);
    updateWsStats(d.ws_stats || null);
    if (d.interfaces) {
      _stash.interfaces = d.interfaces;
      _setFeatureAvailable('lora', true);
      updateInterfaces(d.interfaces);
      if (RPI.updateLoraRadio) RPI.updateLoraRadio(d.interfaces, null);
    }
    if (d.mesh) {
      _stash.mesh = d.mesh;
      _setFeatureAvailable('mesh', true);
      if (d.mesh.peers) {
        _stash.mesh_peers = d.mesh.peers;
        _setFeatureAvailable('map', true);
      }
      if (d.mesh.peers && RPI.cacheMeshPeers) RPI.cacheMeshPeers(d.mesh.peers);
      if (d.mesh.peers && RPI.updateMapReticulum) RPI.updateMapReticulum(d.mesh.peers);
      if (RPI.updateMeshFromWS) RPI.updateMeshFromWS(d.mesh);
    }
    if (d.sensors) {
      _stash.sensors = d.sensors;
      if (isPanelVisible('sensors-body')) updateSensors(d.sensors);
    }
    if (d.emergency) {
      _stash.emergency = d.emergency;
      if (isPanelVisible('emergency-body')) updateEmergency(d.emergency);
    }
    if (d.transport) updateTransport(d.transport);
    if (d.connectivity) updateConnectivity(d.connectivity);
    if (d.routing) {
      _stash.routing = d.routing;
      _setFeatureAvailable('routing', true);
      if (RPI.updateRoutingSummary) RPI.updateRoutingSummary(d.routing);
    }
    if (d.meshtastic_device) {
      _stash.meshtastic_device = d.meshtastic_device;
      _setFeatureAvailable('meshtastic', true);
      if (RPI.updateMeshtasticDevice) RPI.updateMeshtasticDevice(d.meshtastic_device);
    }
    if (d.meshtastic_status) {
      _stash.meshtastic_status = d.meshtastic_status;
      _setFeatureAvailable('meshtastic', true);
      var _fw = d.meshtastic_status.firmware_watchdog;
      if (_fw && _fw.enabled) {
        var _fwHang = !!_fw.hang_detected;
        updateFirmwareHangBanner(_fwHang, _fw.hang_reason || null);
        _lastFwHang = _fwHang;
      }
    }
    if (d.meshtastic_nodes) {
      _stash.meshtastic_status = d.meshtastic_status || {};
      _stash.meshtastic_nodes = d.meshtastic_nodes;
      _setFeatureAvailable('meshtastic', true);
      _setFeatureAvailable('map', true);
      if (RPI.updateMeshtastic) RPI.updateMeshtastic(d.meshtastic_status || {}, d.meshtastic_nodes);
      if (RPI.updateMap) RPI.updateMap(d.meshtastic_nodes);
    }
    if (d.meshtastic_lora_neighbors) {
      _stash.meshtastic_lora_neighbors = d.meshtastic_lora_neighbors;
      _setFeatureAvailable('meshtastic', true);
      _setFeatureAvailable('map', true);
      if (RPI.updateLoraNeighbors) RPI.updateLoraNeighbors(d.meshtastic_lora_neighbors);
      if (RPI.updateMapLoraNeighbors) RPI.updateMapLoraNeighbors(d.meshtastic_lora_neighbors);
    }
    if (d.meshtastic_nodes || d.meshtastic_lora_neighbors || d.meshcore_contacts) {
      if (RPI.updateNodeTracker) RPI.updateNodeTracker(d.meshtastic_nodes || null, d.meshtastic_lora_neighbors || null, d.meshcore_contacts || null);
    }
    if (d.meshcore_status) {
      _stash.meshcore_status = d.meshcore_status;
      _setFeatureAvailable('meshcore', true);
    }
    if (d.meshcore_contacts) {
      _stash.meshcore_contacts = d.meshcore_contacts;
      _setFeatureAvailable('meshcore', true);
      _setFeatureAvailable('map', true);
    }
    if (d.meshcore_status && RPI.updateMeshCore) RPI.updateMeshCore(d.meshcore_status, d.meshcore_contacts);
    if (d.meshcore_contacts && RPI.updateMapMeshCore) RPI.updateMapMeshCore(d.meshcore_contacts);
    if (d.meshcore_device) {
      _stash.meshcore_device = d.meshcore_device;
      _setFeatureAvailable('meshcore', true);
      if (RPI.updateMeshCoreDevice) RPI.updateMeshCoreDevice(d.meshcore_device);
    }
    if (d.meshcore_observer) {
      _stash.meshcore_observer = d.meshcore_observer;
      _setFeatureAvailable('meshcore', true);
      if (RPI.updateMeshCoreObserver) RPI.updateMeshCoreObserver(d.meshcore_observer);
    }
    if (d.mesh_bridge) {
      _stash.mesh_bridge = d.mesh_bridge;
      _setFeatureAvailable('mesh-bridge', true);
      if (RPI.updateMeshBridge) RPI.updateMeshBridge(d.mesh_bridge);
    }
    if (d.messaging) {
      _stash.messaging = d.messaging;
      _setFeatureAvailable('messages', true);
      if (RPI.updateMessagingLxmf) RPI.updateMessagingLxmf(d.messaging);
      if (RPI.updateMqttFeed) RPI.updateMqttFeed(d.messaging);
      if (RPI.updateMessagingLora) RPI.updateMessagingLora(d.messaging);
      if (RPI.updateMessagingMeshcore) RPI.updateMessagingMeshcore(d.messaging);
    }
    if (d.space) {
      _stash.space = d.space;
      _setFeatureAvailable('space', true);
      if (RPI.space && RPI.space.update) RPI.space.update(d.space);
    }
    if (d.gps) {
      _stash.gps = d.gps;
      _setFeatureAvailable('gps', true);
      _setFeatureAvailable('map', true);
      if (RPI.updateGps) RPI.updateGps(d.gps);
    }
    if (d.gps && d.gps.last_fix && RPI.updateMapGps) RPI.updateMapGps(d.gps.last_fix);
    if (d.adsb) {
      _stash.adsb = d.adsb;
      _setFeatureAvailable('adsb', true);
      if (RPI.adsb && RPI.adsb.update) RPI.adsb.update(d.adsb);
    }
    if (d.ntp) {
      _stash.ntp = d.ntp;
      _setFeatureAvailable('ntp', true);
      if (RPI.updateNtp) RPI.updateNtp(d.ntp);
    }
    if (d.ntp) updateHeaderNtpSync(d.ntp);
    if (d.hotspot) {
      _stash.hotspot = d.hotspot;
      _setFeatureAvailable('hotspot', true);
      if (RPI.updateHotspot) RPI.updateHotspot(d.hotspot, _stash.captive_portal || null);
    }
    if (d.captive_portal) {
      _stash.captive_portal = d.captive_portal;
      _setFeatureAvailable('hotspot', true);
      if (_stash.hotspot && isPanelVisible('hotspot-body') && RPI.updateHotspot) {
        RPI.updateHotspot(_stash.hotspot, d.captive_portal);
      }
    }
    if (d.fm_receiver) {
      _stash.fm_receiver = d.fm_receiver;
      _setFeatureAvailable('radio', true);
      if (RPI.updateRadio) RPI.updateRadio(d.fm_receiver);
    }
    if (d.link_tester) {
      _stash.link_tester = d.link_tester;
      _setFeatureAvailable('link-tester', true);
      if (RPI.updateLinkTester) RPI.updateLinkTester(d.link_tester);
    }
    if (d.weather_alert) {
      _stash.weather_alert = d.weather_alert;
      _setFeatureAvailable('weather-alert', true);
      if (RPI.updateWeatherAlert) RPI.updateWeatherAlert(d.weather_alert);
    }
    if (d.ais) {
      _stash.ais = d.ais;
      _setFeatureAvailable('ais', true);
      if (RPI.updateAis) RPI.updateAis(d.ais);
    }
    if (d.acars) {
      _stash.acars = d.acars;
      _setFeatureAvailable('acars', true);
      if (RPI.updateAcars) RPI.updateAcars(d.acars);
    }
    if (d.radiosonde) {
      _stash.radiosonde = d.radiosonde;
      _setFeatureAvailable('radiosonde', true);
      if (RPI.updateRadiosonde) RPI.updateRadiosonde(d.radiosonde);
    }
    if (d.noaa_apt) {
      _stash.noaa_apt = d.noaa_apt;
      _setFeatureAvailable('noaa', true);
      if (RPI.updateNoaa) RPI.updateNoaa(d.noaa_apt);
    }
  }

  function connectWS() {
    if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;

    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var url = proto + '//' + location.host + '/ws/metrics';
    try { ws = new WebSocket(url); } catch(e) { startPolling(); return; }
    RPI.ws = ws;

    ws.onopen = function() {
      reconnectDelay = 1000;
      reconnectAttempts = 0;
      setConnStatus('live');
      _hideStaleBanner();
      _tabHiddenSince = 0;
      stopPolling();
      // Reset traffic rate tracking so we don't compute stale deltas
      _prevTraffic = {};
      prevIfaces = {};
      if (!_nodeLoaded) fetchNode();
      if (_wsPingTimer) clearInterval(_wsPingTimer);
      _wsPingTimer = setInterval(function() {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({action: 'ping', ts: Date.now()}));
        }
      }, 5000);
    };

    ws.onmessage = function(ev) {
      var _now = Date.now();
      _wsMsgRateWindow.push(_now);
      while (_wsMsgRateWindow.length && _wsMsgRateWindow[0] < _now - 10000) _wsMsgRateWindow.shift();
      try {
        var msg = JSON.parse(ev.data);
        if (msg.type === 'pong' && msg.ts) {
          _wsLatency = Date.now() - msg.ts;
          return;
        }
        if (msg.type && msg.type.indexOf('radio_') === 0) {
          if (RPI.onRadioResponse) RPI.onRadioResponse(msg);
          return;
        }
        if (msg.type === 'message' && msg.data) {
          _lastWsUpdate = Date.now() / 1000;
          _queueMessageFeatureEvent('onMessagingEvent', msg.data);
          _queueMessageFeatureEvent('onMqttFeedMessage', msg.data);
          return;
        }
        if (msg.type === 'message_status' && msg.data) {
          _lastWsUpdate = Date.now() / 1000;
          _queueMessageFeatureEvent('onMessagingStatus', msg.data);
          return;
        }
        if (msg.type === 'reaction' && msg.data) {
          _lastWsUpdate = Date.now() / 1000;
          _queueMessageFeatureEvent('onMessagingReaction', msg.data);
          return;
        }
        if (msg.type === 'conversation_deleted' && msg.data) {
          _queueMessageFeatureEvent('onConversationDeleted', msg.data);
          return;
        }
        if (msg.type === 'internet_status' && msg.data) {
          updateInternetStatus(msg.data);
          return;
        }
        if (msg.type === 'firmware_status' && msg.data) {
          handleFirmwareEvent(msg.data);
          return;
        }
        if (msg.type === 'offgrid_mode_changed' && msg.data) {
          updateOffgridState(msg.data.enabled);
          return;
        }
        if (msg.type === 'offgrid_mode_set') {
          updateOffgridState(msg.enabled);
          if (msg.persisted === false) {
            showToast('Off-grid mode will not persist after reboot (read-only storage)', 'warning');
          }
          var _sw = document.getElementById('offgrid-switch');
          if (_sw) _sw.disabled = false;
          if (_offgridReenableTimer) { clearTimeout(_offgridReenableTimer); _offgridReenableTimer = null; }
          return;
        }
        if (msg.type === 'offgrid_error') {
          var _sw2 = document.getElementById('offgrid-switch');
          if (_sw2) { _sw2.checked = _offgridActive; _sw2.disabled = false; }
          if (_offgridReenableTimer) { clearTimeout(_offgridReenableTimer); _offgridReenableTimer = null; }
          return;
        }
        if (msg.type === 'trail_update') {
          if (RPI.onTrailUpdate) RPI.onTrailUpdate(msg.data);
          return;
        }
        if (msg.type === 'update' && msg.data) {
          _lastWsUpdate = Date.now() / 1000;
          if (!_wsFirstTick) {
            _wsFirstTick = true;
            for (var _wi = 0; _wi < _wsReadyCallbacks.length; _wi++) _wsReadyCallbacks[_wi]();
            _wsReadyCallbacks = [];
          }
          // Batch DOM updates into the next animation frame.
          var pending = RPI._pendingUpdate || {};
          Object.keys(msg.data).forEach(function(key) {
            if (key === '_removed') {
              var prior = Array.isArray(pending._removed) ? pending._removed : [];
              pending._removed = Array.from(new Set(prior.concat(msg.data._removed || [])));
            } else {
              pending[key] = msg.data[key];
            }
          });
          RPI._pendingUpdate = pending;
          if (!RPI._rafPending) {
            RPI._rafPending = true;
            requestAnimationFrame(function() {
              RPI._rafPending = false;
              var d = RPI._pendingUpdate;
              RPI._pendingUpdate = null;
              if (!d) return;
              _applyUpdate(d);
              _checkStaleness();
            });
          }
        }
      } catch(e) { console.warn('WS message parse error:', e); }
    };

    ws.onclose = function() {
      _wsFirstTick = false;
      if (_wsPingTimer) { clearInterval(_wsPingTimer); _wsPingTimer = null; }
      _wsLatency = null;
      _wsMsgRateWindow = [];
      setMetric('m-ws-latency', null, 'ms');
      setMetric('m-ws-msgrate', null, '/s');
      setMetric('m-ws-clients', null, '');
      if (RPI.onMessagingConnectionLost) RPI.onMessagingConnectionLost();
      scheduleReconnect();
    };

    ws.onerror = function() {
      // onerror is always followed by onclose -- no action needed here
    };
  }

  function scheduleReconnect() {
    startPolling();
    reconnectAttempts++;
    if (reconnectAttempts > maxReconnectAttempts) {
      console.warn('WS max reconnect attempts (' + maxReconnectAttempts + ') reached, using polling only');
      setConnStatus('polling');
      return;
    }
    setTimeout(function() {
      reconnectDelay = Math.min(reconnectDelay * 2, maxReconnect);
      connectWS();
    }, reconnectDelay);
  }

  // --- Polling fallback ---

  function startPolling() {
    // Always update status so it shows "polling" even if timer already exists
    // (prevents stuck "disconnected" when WS reconnect keeps failing)
    setConnStatus('polling');
    if (pollTimer) return;
    pollTimer = setInterval(function() {
      fetchCritical();
      fetchSecondary();
      fetchWsUncovered();
    }, 10000);
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  // --- Events ---
  // Each logical wiring group below is wrapped in safeWire so an exception in
  // one group (e.g. a querySelectorAll loop hitting an unexpected DOM shape)
  // cannot abort the rest of the wiring or the boot tail.
  // NOTE: declarations referenced across groups (_el scratch, _sectionFirstExpand,
  // registerDeferredSection) stay at module scope -- only wiring statements are wrapped.

  var _el;
  // _sectionFirstExpand / registerDeferredSection must stay visible to the
  // deferred-sections block at the bottom of the file.
  var _sectionFirstExpand = {};
  function registerDeferredSection(name, fn) { _sectionFirstExpand[name] = fn; }
  RPI.registerDeferredSection = registerDeferredSection;

  safeWire('stale-logout', function () {
    if (_el = $('stale-refresh-btn')) _el.addEventListener('click', function() {
      _manualRefresh();
    });
    if (_el = $('logout-btn')) _el.addEventListener('click', function() {
      api('/api/auth/logout', {method: 'POST'}).finally(function() {
        window.location.href = '/login.html';
      });
    });
    if (_el = $('password-change-form')) _el.addEventListener('submit', function(ev) {
      ev.preventDefault();
      submitPasswordChange();
    });
    if (_el = $('password-change-logout')) _el.addEventListener('click', function() {
      api('/api/auth/logout', {method: 'POST'}).finally(function() {
        window.location.href = '/login.html';
      });
    });
  });

  // Collapsible section toggles with deferred-fetch on first expand
  safeWire('section-toggles', function () {
    ['plugins', 'telemetry', 'files', 'alerts', 'sensors', 'emergency', 'mesh-bridge-section', 'hotspot', 'node-tracker', 'map'].forEach(function(name) {
      var toggle = $(name + '-toggle');
      var body = $(name + '-body');
      if (toggle && body) {
        toggle.addEventListener('click', function() {
          if (body.classList.contains('hidden')) {
            body.classList.remove('hidden');
            body.hidden = false;
            toggle.classList.add('open');
            toggle.setAttribute('aria-expanded', 'true');
            if (_sectionOnExpand[name]) _sectionOnExpand[name]();
            if (_sectionFirstExpand[name]) {
              _sectionFirstExpand[name]();
              delete _sectionFirstExpand[name];
            }
          } else {
            body.classList.add('hidden');
            body.hidden = true;
            toggle.classList.remove('open');
            toggle.setAttribute('aria-expanded', 'false');
          }
        });
      }
    });

    // Other feature modules also own collapsible headers. Add one semantic,
    // keyboard-operable contract without duplicating their click handlers.
    var collapsibles = document.querySelectorAll('.section-header.collapsible');
    for (var ci = 0; ci < collapsibles.length; ci++) {
      var control = collapsibles[ci];
      var targetId = control.getAttribute('aria-controls') ||
        (control.id ? control.id.replace(/-toggle$/, '-body') : '');
      var controlledBody = targetId && $(targetId);
      var expanded = !!(controlledBody && !controlledBody.classList.contains('hidden'));
      if (controlledBody) controlledBody.hidden = !expanded;
      control.classList.toggle('open', expanded);
      control.setAttribute('aria-expanded', expanded ? 'true' : 'false');
      if (controlledBody) control.setAttribute('aria-controls', targetId);
      if (control.tagName !== 'BUTTON') {
        control.setAttribute('role', 'button');
        control.setAttribute('tabindex', '0');
        control.addEventListener('keydown', function(ev) {
          if (ev.key === 'Enter' || ev.key === ' ') {
            ev.preventDefault();
            ev.currentTarget.click();
          }
        });
      }
    }

    if (_el = $('config-toggle')) _el.addEventListener('click', function() {
      var content = $('config-content');
      var btn = $('config-toggle');
      if (content.classList.contains('hidden')) {
        content.classList.remove('hidden');
        btn.textContent = 'Hide';
        fetchConfig();
      } else {
        content.classList.add('hidden');
        btn.textContent = 'Show';
      }
    });
  });

  // --- Init ---
  // Auth is handled by the server middleware (cookie-based).
  // Wire up sortable mesh table headers
  safeWire('mesh-sort-headers', function () {
    var sortHeaders = document.querySelectorAll('#mesh-section th[data-sort]');
    for (var i = 0; i < sortHeaders.length; i++) {
      (function(th) {
        th.addEventListener('click', function() {
          RPI.onMeshSort(th.getAttribute('data-sort'));
        });
      })(sortHeaders[i]);
    }
  });

  safeWire('accessibility-contract', function () {
    // Preserve compact visual labels while ensuring controls have names.
    var unnamedFields = document.querySelectorAll('input:not([aria-label]), textarea:not([aria-label]), select:not([aria-label])');
    for (var ai = 0; ai < unnamedFields.length; ai++) {
      var field = unnamedFields[ai];
      if (field.labels && field.labels.length) continue;
      var name = field.getAttribute('placeholder') || field.getAttribute('title');
      if (name) field.setAttribute('aria-label', name.replace(/\u2026/g, ''));
    }
    var iconButtons = document.querySelectorAll(
      'button[title]:not([aria-label]):not(.section-header.collapsible)'
    );
    for (var bi = 0; bi < iconButtons.length; bi++) {
      iconButtons[bi].setAttribute('aria-label', iconButtons[bi].getAttribute('title'));
    }
    var canvases = document.querySelectorAll('canvas:not([aria-label])');
    for (var cai = 0; cai < canvases.length; cai++) {
      canvases[cai].setAttribute('role', 'img');
      canvases[cai].setAttribute('aria-label', canvases[cai].getAttribute('title') || 'Live telemetry chart');
    }
    var sortable = document.querySelectorAll('th[data-sort]');
    for (var si = 0; si < sortable.length; si++) {
      sortable[si].setAttribute('tabindex', '0');
      sortable[si].setAttribute('aria-sort', 'none');
    }
    document.addEventListener('keydown', function(ev) {
      var th = ev.target.closest && ev.target.closest('th[data-sort]');
      if (th && (ev.key === 'Enter' || ev.key === ' ')) {
        ev.preventDefault();
        th.click();
      }
    });
    document.addEventListener('click', function(ev) {
      var toggle = ev.target.closest && ev.target.closest('.section-header.collapsible');
      if (toggle) {
        var isExpanded = toggle.classList.contains('open');
        toggle.setAttribute('aria-expanded', isExpanded ? 'true' : 'false');
        var controlled = $(toggle.getAttribute('aria-controls'));
        if (controlled) controlled.hidden = !isExpanded;
      }
      var th = ev.target.closest && ev.target.closest('th[data-sort]');
      if (th) {
        var peers = th.parentElement ? th.parentElement.querySelectorAll('th[data-sort]') : [];
        for (var pi = 0; pi < peers.length; pi++) peers[pi].setAttribute('aria-sort', 'none');
        th.setAttribute('aria-sort', th.classList.contains('sort-asc') ? 'ascending' : 'descending');
      }
    });
    var toastRegion = $('toast-container');
    if (toastRegion) {
      toastRegion.setAttribute('role', 'status');
      toastRegion.setAttribute('aria-live', 'polite');
    }
  });

  // Routing table -- event delegation (replaces per-render listener binding)
  safeWire('routing-table', function () {
    // Hash cell click-to-copy
    if (_el = $('routing-table-body')) _el.addEventListener('click', function(ev) {
      var cell = ev.target.closest('.hash-cell');
      if (!cell) return;
      var full = cell.getAttribute('title');
      if (full && navigator.clipboard) {
        navigator.clipboard.writeText(full);
        cell.style.color = 'var(--green)';
        setTimeout(function() { cell.style.color = ''; }, 500);
      }
    });
    // Pagination button clicks
    if (_el = $('routing-pagination')) _el.addEventListener('click', function(ev) {
      var btn = ev.target.closest('button[data-rt-page]');
      if (!btn || btn.disabled) return;
      var pg = parseInt(btn.getAttribute('data-rt-page'));
      if (pg && pg !== RPI._rtPage()) {
        RPI._setRtPage(pg);
        RPI.fetchRoutingTable();
      }
    });

    // Routing table toggle
    if (_el = $('routing-table-toggle')) _el.addEventListener('click', function() {
      var wrapper = $('routing-table-wrapper');
      var btn = $('routing-table-toggle');
      if (RPI._rtTableOpen()) {
        wrapper.classList.add('hidden');
        btn.textContent = 'Show Path Table';
        RPI._setRtTableOpen(false);
        var autoRef = RPI._rtAutoRefresh();
        if (autoRef) { clearInterval(autoRef); RPI._setRtAutoRefresh(null); }
      } else {
        wrapper.classList.remove('hidden');
        btn.textContent = 'Hide Path Table';
        RPI._setRtTableOpen(true);
        RPI._setRtPage(1);
        RPI.fetchRoutingTable();
        RPI._setRtAutoRefresh(setInterval(function() { RPI.fetchRoutingTable(); }, 15000));
      }
    });
  });

  // Routing table sort headers
  safeWire('routing-sort-headers', function () {
    var rtSortHeaders = document.querySelectorAll('#routing-section th[data-rt-sort]');
    for (var si = 0; si < rtSortHeaders.length; si++) {
      (function(th) {
        th.addEventListener('click', function() {
          var key = th.getAttribute('data-rt-sort');
          if (RPI._rtSort() === key) {
            RPI._setRtOrder(RPI._rtOrder() === 'asc' ? 'desc' : 'asc');
          } else {
            RPI._setRtSort(key);
            RPI._setRtOrder((key === 'hops') ? 'asc' : 'desc');
          }
          RPI._setRtPage(1);
          RPI.fetchRoutingTable();
        });
      })(rtSortHeaders[si]);
    }
  });

  // Routing table filters (debounced)
  safeWire('routing-filters', function () {
    if (_el = $('rt-search')) _el.addEventListener('input', function() {
      var timer = RPI._rtDebounceTimer();
      if (timer) clearTimeout(timer);
      var val = this.value;
      RPI._setRtDebounceTimer(setTimeout(function() {
        RPI._setRtSearch(val);
        RPI._setRtPage(1);
        RPI.fetchRoutingTable();
      }, 300));
    });

    if (_el = $('rt-iface-filter')) _el.addEventListener('change', function() {
      RPI._setRtIfaceFilter(this.value);
      RPI._setRtPage(1);
      RPI.fetchRoutingTable();
    });

    if (_el = $('rt-hops-filter')) _el.addEventListener('change', function() {
      RPI._setRtHopsFilter(this.value);
      RPI._setRtPage(1);
      RPI.fetchRoutingTable();
    });
  });

  // Mesh filter tabs / search / pagination / row clicks -- event delegation
  safeWire('mesh-controls', function () {
    if (_el = $('mesh-filter-bar')) _el.addEventListener('click', function(ev) {
      var tab = ev.target.closest('[data-mesh-view]');
      if (!tab) return;
      var view = tab.getAttribute('data-mesh-view');
      // Update active state
      var tabs = document.querySelectorAll('.mesh-tab');
      for (var i = 0; i < tabs.length; i++) tabs[i].classList.remove('active');
      tab.classList.add('active');
      // Cancel pending search if switching views
      var timer = RPI._meshSearchTimer();
      if (timer) { clearTimeout(timer); RPI._setMeshSearchTimer(null); }
      RPI._setMeshView(view);
      RPI._setMeshPage(1);
      RPI.fetchMeshNodes();
    });

    // Mesh search -- debounced input
    if (_el = $('mesh-search')) _el.addEventListener('input', function() {
      var input = this;
      var timer = RPI._meshSearchTimer();
      if (timer) clearTimeout(timer);
      RPI._setMeshSearchTimer(setTimeout(function() {
        RPI._setMeshSearch(input.value.trim());
        RPI._setMeshPage(1);
        RPI.fetchMeshNodes();
      }, 300));
    });

    // Mesh pagination -- event delegation for page buttons
    if (_el = $('mesh-show-more')) _el.addEventListener('click', function(ev) {
      var btn = ev.target.closest('[data-mesh-page]');
      if (!btn) return;
      var pg = parseInt(btn.getAttribute('data-mesh-page'));
      if (pg && pg !== RPI._meshPage()) {
        RPI._setMeshPage(pg);
        RPI.fetchMeshNodes();
      }
    });
    // Mesh table row clicks -- event delegation
    if (_el = $('mesh-table')) _el.addEventListener('click', function(ev) {
      var row = ev.target.closest('tr[data-hash]');
      if (!row) return;
      var hash = row.getAttribute('data-hash');
      if (!hash) return;
      var nodes = RPI._meshNodes();
      var node = null;
      for (var i = 0; i < nodes.length; i++) {
        if (nodes[i].destination_hash === hash) { node = nodes[i]; break; }
      }
      if (node) RPI.toggleNodeDetail(node, hash);
    });
    // LoRa table row clicks -- event delegation
    if (_el = $('lora-table')) _el.addEventListener('click', function(ev) {
      var row = ev.target.closest('tr[data-lora-hash]');
      if (!row) return;
      var hash = row.getAttribute('data-lora-hash');
      if (!hash) return;
      var curHash = RPI._loraExpandedHash();
      RPI._setLoraExpandedHash(curHash === hash ? null : hash);
      RPI.updateLoraNodes(RPI._loraNodes());
    });
    if (_el = $('peer-show-more')) _el.addEventListener('click', function() {
      var peers = Object.values(RPI._meshPeers());
      if (RPI._peerVisible() >= peers.length) {
        RPI._setPeerVisible(RPI._peerPageSize());
      } else {
        RPI._setPeerVisible(RPI._peerVisible() + RPI._peerPageSize());
      }
      RPI.updatePeerTelemetry(peers);
    });
  });

  // Messaging hub controls wired in messages.js module

  // Wire up sortable Meshtastic MQTT table headers (exclude lora- prefixed)
  safeWire('meshtastic-sort-headers', function () {
    var mshSortHeaders = document.querySelectorAll('#meshtastic-section th[data-sort]:not([data-sort^="lora-"])');
    for (var mi = 0; mi < mshSortHeaders.length; mi++) {
      (function(th) {
        th.addEventListener('click', function() {
          RPI.onMeshtasticSort(th.getAttribute('data-sort'));
        });
      })(mshSortHeaders[mi]);
    }
    if (_el = $('meshtastic-show-more')) _el.addEventListener('click', function() {
      if (RPI.meshtasticShowMore) RPI.meshtasticShowMore();
    });
  });

  // Wire up sortable LoRa neighbors table headers
  safeWire('lora-neighbors-sort-headers', function () {
    var loraSortHeaders = document.querySelectorAll('#meshtastic-lora-neighbors th[data-sort]');
    for (var li = 0; li < loraSortHeaders.length; li++) {
      (function(th) {
        th.addEventListener('click', function() {
          var key = th.getAttribute('data-sort').replace('lora-', '');
          RPI.onLoraSort(key);
        });
      })(loraSortHeaders[li]);
    }
    if (_el = $('lora-neighbors-show-more')) _el.addEventListener('click', function() {
      if (RPI.loraNeighborsShowMore) RPI.loraNeighborsShowMore();
    });
  });

  // Wire up sortable MeshCore contacts table headers
  safeWire('meshcore-sort-headers', function () {
    var mcSortHeaders = document.querySelectorAll('#meshcore-section th[data-sort]');
    for (var mci = 0; mci < mcSortHeaders.length; mci++) {
      (function(th) {
        th.addEventListener('click', function() {
          RPI.onMeshCoreSort(th.getAttribute('data-sort'));
        });
      })(mcSortHeaders[mci]);
    }
    if (_el = $('meshcore-show-more')) _el.addEventListener('click', function() {
      if (RPI.meshcoreShowMore) RPI.meshcoreShowMore();
    });
  });

  // Wire up sortable GPS satellites table headers
  safeWire('gps-sort-headers', function () {
    var gpsSortHeaders = document.querySelectorAll('#gps-section th[data-sort]');
    for (var gi = 0; gi < gpsSortHeaders.length; gi++) {
      (function(th) {
        th.addEventListener('click', function() {
          var key = th.getAttribute('data-sort').replace('gps-', '');
          if (RPI.onGpsSort) RPI.onGpsSort(key);
        });
      })(gpsSortHeaders[gi]);
    }
  });

  // Interface management -- event delegation (CSP blocks inline handlers)
  safeWire('iface-lora-controls', function () {
    if (_el = $('restart-btn')) _el.addEventListener('click', doRestart);
    if (_el = $('restart-dialog-form')) _el.addEventListener('submit', function(ev) {
      ev.preventDefault();
      submitRestart();
    });
    if (_el = $('restart-cancel-btn')) _el.addEventListener('click', function() {
      var dialog = $('restart-dialog');
      if (dialog) dialog.close();
    });
    if (_el = $('interfaces-table')) _el.addEventListener('change', function(ev) {
      var cb = ev.target;
      if (cb.tagName === 'INPUT' && cb.dataset.iface) {
        window._toggleIface(cb.dataset.iface);
      }
    });

    // LoRa announce mode -- event delegation (select is dynamically rendered)
    if (_el = $('lora-section')) _el.addEventListener('change', function(ev) {
      if (ev.target.id === 'lora-announce-mode') {
        window._setLoraAnnounceMode(ev.target.value);
      }
    });
  });

  // Deferred / config wiring. Left at the bottom because it depends on
  // declarations inside the wiring region above and is not pipeline-critical
  // (the data pipeline is already armed via safeWire('boot', boot) near the top).
  safeWire('deferred-sections', function () {
    fetchInterfacesConfig();

    // Render from WS stash when a collapsed section is re-expanded
    _sectionOnExpand.sensors = function() { if (_stash.sensors) updateSensors(_stash.sensors); };
    _sectionOnExpand.emergency = function() { if (_stash.emergency) updateEmergency(_stash.emergency); };
    _sectionOnExpand.hotspot = function() {
      if (_stash.hotspot && RPI.updateHotspot) RPI.updateHotspot(_stash.hotspot, _stash.captive_portal || null);
    };
    _sectionOnExpand.map = function() {
      var feature = _features.map;
      if (feature) feature.desired = true;
      if (feature && feature.available) {
        loadFeature('map').then(function() {
          if (RPI._mapInvalidate) RPI._mapInvalidate();
        }).catch(function() {});
      }
    };

    // Register deferred fetches for collapsed sections
    registerDeferredSection('alerts', function() {
      api('/api/alerts').then(function(r) { if (r && r.ok) updateAlerts(r.data); });
    });
    registerDeferredSection('sensors', function() {
      if (_lastSensorData) return;
      api('/api/sensors').then(function(r) {
        if (!r || !r.ok) return;
        updateSensors(r.data.sensors);
        var names = Object.keys(r.data.sensors || {});
        if (names.length > 0) fetchSensorHistory(names);
      });
    });
    registerDeferredSection('emergency', function() {
      api('/api/emergency').then(function(r) { if (r && r.ok) updateEmergency(r.data); });
    });
    registerDeferredSection('files', function() {
      api('/api/files').then(function(r) { if (r && r.ok) updateSharedFiles(r.data.files); });
    });
  });

})();
