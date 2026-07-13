/* Service worker — cache-first tiles + stale-while-revalidate app shell */
importScripts('/static/version.js');
var TILE_CACHE = 'rpi-tiles-v1';
var SHELL_CACHE = 'rpi-shell-' + APP_VERSION;
var MAX_ENTRIES = 5000;
var TILE_RE = /\.tile\.openstreetmap\.org\/|\/tiles\//;

var SHELL_ASSETS = [
  '/index.html',
  '/login.html',
  '/spectrum.html',
  '/static/asset-manifest.json',
  '/static/manifest.webmanifest',
  '/static/version.js',
  /*__RPI_BUILT_ASSETS__*/
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
  var path = new URL(url).pathname;

  // API/auth responses always bypass the service worker, including unusual
  // navigation-mode requests made by diagnostics or browser address bars.
  if (_isPrivateEndpoint(path)) return;

  // 1. Map tiles — cache-first (existing behavior)
  if (TILE_RE.test(url)) {
    e.respondWith(_tileFetch(e.request));
    return;
  }

  // 2. App shell — stale-while-revalidate
  if (e.request.mode === 'navigate') {
    e.respondWith(_navigationFetch(e.request));
    return;
  }
  if (_isShellAsset(path)) {
    e.respondWith(_shellFetch(e.request, e));
    return;
  }

  // 3. Everything else (API, WS) — passthrough
});

function _isPrivateEndpoint(path) {
  return path === '/api' || path.indexOf('/api/') === 0 ||
    path === '/auth' || path.indexOf('/auth/') === 0;
}

function _isShellAsset(path) {
  if (path === '/' || path === '/index.html' || path === '/spectrum.html') return true;
  if (path === '/login.html') return true;
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
          return cache.put(request, resp.clone()).then(function () {
            return _trimCache(cache);
          }).then(function () { return resp; });
        }
        return resp;
      });
    });
  }).catch(function () { return _placeholder(); });
}

function _trimCache(cache) {
  return cache.keys().then(function (keys) {
    var excess = keys.length - MAX_ENTRIES;
    if (excess <= 0) return;
    // Delete oldest entries in a single pass (no recursion)
    var toDelete = keys.slice(0, excess);
    return Promise.all(toDelete.map(function (k) { return cache.delete(k); }));
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

function _shellFetch(request, event) {
  var isVersion = request.url.indexOf('/version.js') !== -1;
  return caches.open(SHELL_CACHE).then(function (cache) {
    return cache.match(request).then(function (cached) {
      var snapshot = (cached && isVersion) ? cached.clone() : null;
      var networkFetch = fetch(request).then(function (resp) {
        if (resp.ok) {
          return cache.put(request, resp.clone()).then(function () {
            if (!snapshot) return resp;
            return Promise.all([snapshot.text(), resp.clone().text()]).then(function (pair) {
              if (pair[0] !== pair[1]) {
                return self.clients.matchAll().then(function (clients) {
                  clients.forEach(function (c) {
                    c.postMessage({ type: 'sw-updated' });
                  });
                });
              }
            }).then(function () { return resp; });
          });
        }
        return resp;
      }).catch(function () {
        if (!cached) {
          return new Response('Offline — cached version not available', {
            status: 503,
            headers: { 'Content-Type': 'text/plain' }
          });
        }
      });
      if (cached && event) event.waitUntil(networkFetch.then(function () {}));
      return cached || networkFetch;
    });
  });
}

// -- Navigation: network-first, cached shell fallback ----------------------

function _navigationFetch(request) {
  var path = new URL(request.url).pathname;
  var fallbackPath = _navigationFallbackPath(path);
  return fetch(request).then(function (resp) {
    // Never cache redirects or authentication failures as the application
    // shell; doing so could strand the user on the wrong side of login.
    if (!resp.ok || resp.redirected) return resp;
    return caches.open(SHELL_CACHE).then(function (cache) {
      return cache.put(fallbackPath, resp.clone()).then(function () { return resp; });
    });
  }).catch(function () {
    return caches.open(SHELL_CACHE).then(function (cache) {
      return cache.match(fallbackPath);
    }).then(function (cached) {
      return cached || new Response('Offline — dashboard shell is not cached yet', {
        status: 503,
        headers: {'Content-Type': 'text/plain', 'Cache-Control': 'no-store'}
      });
    });
  });
}

function _navigationFallbackPath(path) {
  // Keep every standalone document under its own key in the versioned shell
  // cache.  In particular, caching a successful spectrum navigation as
  // /index.html would replace the dashboard's offline fallback with the
  // spectrum shell until the next complete service-worker installation.
  if (path === '/login.html') return '/login.html';
  if (path === '/spectrum.html') return '/spectrum.html';
  return '/index.html';
}
