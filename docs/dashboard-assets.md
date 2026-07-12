# Dashboard asset pipeline

ReticulumPi ships prebuilt browser assets. Production installations and
containers do not require Node.js, npm, or a source checkout.

## Rebuilding assets

The development toolchain pins esbuild in `package.json` and `package-lock.json`.
After changing dashboard JavaScript or CSS, run:

```sh
npm ci
npm run build:dashboard
npm run check:dashboard
```

Commit the resulting files below
`src/reticulumpi/builtin_plugins/web_dashboard/static/assets/` and the generated
`static/asset-manifest.json`. The check command builds into a temporary
directory and fails if the committed output differs byte-for-byte.

The manifest records each logical asset's content-addressed path, byte length,
SHA-256 digest, and Subresource Integrity value. At startup the Python resource
loader resolves the manifest with `importlib.resources` and verifies every
entry before rendering HTML. Missing, stale, traversing, or digest-mismatched
entries fail closed. Wheel verification repeats these checks against the archive
itself.

The wheel gate then installs `reticulumpi[dashboard]` into a disposable virtual
environment, changes to a source-unavailable directory, and runs Python with
isolated import paths. It discovers the packaged built-in plugins through the
production `PluginLoader` before a bounded loopback-only aiohttp smoke logs in,
fetches the rendered login/dashboard, both manifests, a hashed asset, and
`/api/status`, then shuts the runner down. This catches plugin modules and resource
paths that work only from a checkout.

Content-addressed assets are served with a one-year immutable cache policy.
HTML and the service worker are revalidated. The service worker receives the
verified manifest paths at request time and never intercepts or stores `/api/`,
authentication, configuration, or restart responses.

`static/manifest.webmanifest` is the separate installable-web-app manifest. It
is linked from every shell page, served as `application/manifest+json`, included
in the offline shell, and verified as required wheel content.

## Bundle boundaries

The authenticated dashboard has a small coordinator bundle plus independently
hashed ESM chunks for ACARS, ADS-B, AIS, GPS, Hotspot, Link Tester, LoRa, Map,
Mesh, Mesh Bridge, MeshCore, Messages, Meshtastic, NOAA, NTP, Radio,
Radiosonde, Routing, Space, and Weather Alerts. Each chunk exports
`init(context)` and `dispose(context)`. The coordinator imports a chunk only
after the backing plugin is ready (or produces live data) and its panel is
opened or approaches the viewport. Data arriving before a chunk loads is
bounded and replayed after initialization; a plugin tombstone deactivates its
feature.

The HTTP fallback follows the same boundary. Initial boot requests only core
metrics, interfaces, and plugin readiness. Feature-specific requests are
issued from the corresponding feature bootstrap only after its availability
and panel gates pass. LoRa is additionally gated by the interface/LoRa summary
response, and the spectrum navigation remains hidden until a scanner,
LoRa-diagnostics, or link-tester provider becomes ready.

The build manifest is the only extra request needed to resolve a feature chunk.
It is part of the offline shell, but feature chunks are deliberately excluded
from service-worker installation so an unavailable or unopened plugin cannot
consume bandwidth. A chunk is cached on first successful use by the normal
static-asset fetch path. If a never-used chunk is requested while offline, the
panel reports the failure and the next activation retries; the core dashboard
continues operating.

Login and spectrum retain separate navigation bundles. Spectrum code is not
referenced by the dashboard page and is fetched only after navigation to the
spectrum page. Leaflet CSS and JavaScript are also absent from the initial page
and service-worker install set. The coordinator injects them once, immediately
before the first Map, ADS-B, or Space import, and all three features share the
same promise. GPS tolerates the dependency being absent and replays its latest
fix after Leaflet initializes.

The current generated coordinator is approximately 68.4 KiB uncompressed.
The locally measured critical shell uses four static requests and approximately
52 KiB compressed, below the six-request and 180 KiB gates. These figures are a
development-worktree measurement. The Chromium performance lane additionally
passes the 1 Mbps/150 ms RTT, LCP, INP, CLS, long-task, and normal-UI frame-rate
budgets and attaches its measurements as JSON. Neither result substitutes for
the signed release artifact, Lighthouse, or reference-device gates.

`tests/test_dashboard_feature_loading.py` verifies the availability/open gates,
first-payload replay, tombstones, and absence of optional feature requests at
boot. The Playwright suite additionally verifies that GPS and Leaflet are not
loaded before activation and that the activated feature runs under the strict
CSP without console errors.
