/* Service worker — cache-first tiles + stale-while-revalidate app shell */
var TILE_CACHE = 'rpi-tiles-v1';
var SHELL_CACHE = 'rpi-shell-v1';
var MAX_ENTRIES = 5000;
var TILE_RE = /\.tile\.openstreetmap\.org\/|\/tiles\//;

var SHELL_ASSETS = [
  '/login.html',
  '/static/style.css',
  '/static/vendor/leaflet.css',
  '/static/vendor/leaflet.js',
  '/static/vendor/images/marker-icon.png',
  '/static/vendor/images/marker-icon-2x.png',
  '/static/vendor/images/marker-shadow.png',
  '/static/app.js',
  '/static/map.js',
  '/static/gps.js',
  '/static/space.js',
  '/static/adsb.js',
  '/static/mesh.js',
  '/static/lora.js',
  '/static/routing.js',
  '/static/meshtastic.js',
  '/static/meshcore.js',
  '/static/messages_panel.js',
  '/static/messages_lxmf.js',
  '/static/messages_meshtastic_lora.js',
  '/static/messages_meshcore.js',
  '/static/mesh_bridge_panel.js',
  '/static/node_tracker.js',
  '/static/link_tester.js',
  '/static/radio.js',
  '/static/spectrum.js',
  '/static/spectrum_common.js',
  '/static/spectrum_page.js',
  '/static/lora_spectrum.js',
  '/static/hotspot.js',
  '/static/ntp.js',
  '/static/weather_alert.js',
  '/static/ais.js',
  '/static/acars.js',
  '/static/radiosonde.js',
  '/static/noaa.js',
  '/static/mqtt_feed.js',
  '/static/login.js'
];

var KEEP_CACHES = [TILE_CACHE, SHELL_CACHE];

self.addEventListener('install', function (e) {
  e.waitUntil(
    caches.open(SHELL_CACHE).then(function (cache) {
      return cache.addAll(SHELL_ASSETS);
    }).then(function () {
      return self.skipWaiting();
    })
  );
});

self.addEventListener('activate', function (e) {
  e.waitUntil(
    caches.keys().then(function (names) {
      return Promise.all(
        names.filter(function (n) { return KEEP_CACHES.indexOf(n) === -1; })
             .map(function (n) { return caches.delete(n); })
      );
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener('fetch', function (e) {
  var url = e.request.url;

  // 1. Map tiles — cache-first (existing behavior)
  if (TILE_RE.test(url)) {
    e.respondWith(_tileFetch(e.request));
    return;
  }

  // 2. App shell — stale-while-revalidate
  var path = new URL(url).pathname;
  if (_isShellAsset(path)) {
    e.respondWith(_shellFetch(e.request));
    return;
  }

  // 3. Everything else (API, WS) — passthrough
});

function _isShellAsset(path) {
  if (path === '/' || path === '/index.html' || path === '/login.html'
      || path === '/spectrum.html') return true;
  if (path.indexOf('/static/') === 0) return true;
  return false;
}

// -- Tile: cache-first with offline placeholder ----------------------------

function _tileFetch(request) {
  return caches.open(TILE_CACHE).then(function (cache) {
    return cache.match(request).then(function (cached) {
      if (cached) return cached;
      return fetch(request).then(function (resp) {
        if (resp.ok) {
          cache.put(request, resp.clone());
          _trimCache(cache);
        }
        return resp;
      });
    });
  }).catch(function () { return _placeholder(); });
}

function _trimCache(cache) {
  cache.keys().then(function (keys) {
    if (keys.length > MAX_ENTRIES) {
      cache.delete(keys[0]).then(function () {
        if (keys.length - 1 > MAX_ENTRIES) _trimCache(cache);
      });
    }
  });
}

function _placeholder() {
  var sz = 256;
  var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' + sz + '" height="' + sz + '">' +
    '<rect width="' + sz + '" height="' + sz + '" fill="#2d2d2d"/>' +
    '<path d="M0 0h256M0 64h256M0 128h256M0 192h256M64 0v256M128 0v256M192 0v256" ' +
    'stroke="#3a3a3a" stroke-width="0.5" fill="none"/>' +
    '<text x="128" y="134" text-anchor="middle" fill="#555" font-size="11" ' +
    'font-family="sans-serif">offline</text></svg>';
  return new Response(svg, {
    headers: { 'Content-Type': 'image/svg+xml', 'Cache-Control': 'no-store' }
  });
}

// -- Shell: stale-while-revalidate -----------------------------------------

function _shellFetch(request) {
  return caches.open(SHELL_CACHE).then(function (cache) {
    return cache.match(request).then(function (cached) {
      var networkFetch = fetch(request).then(function (resp) {
        if (resp.ok) cache.put(request, resp.clone());
        return resp;
      }).catch(function () {});
      return cached || networkFetch;
    });
  });
}
