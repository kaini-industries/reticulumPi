/* Service worker — cache-first for map tiles */
var CACHE = 'rpi-tiles-v1';
var MAX_ENTRIES = 5000;
var TILE_RE = /\.tile\.openstreetmap\.org\//;

self.addEventListener('install', function () { self.skipWaiting(); });
self.addEventListener('activate', function (e) {
  e.waitUntil(
    caches.keys().then(function (names) {
      return Promise.all(
        names.filter(function (n) { return n !== CACHE; })
             .map(function (n) { return caches.delete(n); })
      );
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener('fetch', function (e) {
  if (!TILE_RE.test(e.request.url)) return;
  e.respondWith(
    caches.open(CACHE).then(function (cache) {
      return cache.match(e.request).then(function (cached) {
        if (cached) return cached;
        return fetch(e.request).then(function (resp) {
          if (resp.ok) {
            cache.put(e.request, resp.clone());
            _trimCache(cache);
          }
          return resp;
        });
      });
    }).catch(function () { return _placeholder(); })
  );
});

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
