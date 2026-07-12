import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const READY_PLUGINS = [
  "acars_decoder",
  "adsb_radar",
  "ais_receiver",
  "captive_portal",
  "connectivity_monitor",
  "fm_receiver",
  "gps_telemetry",
  "hotspot_monitor",
  "lora_link_tester",
  "lora_scanner",
  "mesh_bridge",
  "mesh_telemetry",
  "meshcore_gateway",
  "meshtastic_gateway",
  "messaging_hub",
  "network_map",
  "noaa_apt_decoder",
  "ntp_server",
  "radiosonde_tracker",
  "space_tracker",
  "spectrum_scanner",
  "weather_alert",
];

function success(data) {
  return JSON.stringify({ ok: true, data, timestamp: Date.now() / 1000 });
}

async function installApiFixtures(page) {
  const plugins = Object.fromEntries(
    READY_PLUGINS.map((name) => [
      name,
      {
        name,
        version: "test",
        description: "Playwright fixture",
        status: { active: true, _lifecycle: { state: "ready", health: "healthy" } },
        address: null,
      },
    ]),
  );
  const responses = {
    "/api/node": {
      node_name: "Playwright Node",
      version: "0.2.5",
      identity_hash: "abcd",
      uptime: 5,
      server_time: Date.now() / 1000,
    },
    "/api/metrics": {
      cpu_percent: 1,
      memory_percent: 2,
      disk_percent: 3,
      temperature_c: 40,
      uptime: 5,
    },
    "/api/interfaces": { interfaces: [] },
    "/api/lora": {},
    "/api/link_tester": {
      available: true,
      connected: true,
      status: "ready",
      test_running: false,
      results: [],
      stats: {},
    },
    "/api/plugins": { plugins, failed_plugins: [] },
    "/api/transport": { hubs: [] },
    "/api/connectivity": { issues: [] },
    "/api/gps": {
      available: true,
      connected: true,
      have_fix: true,
      serial_port: "/dev/ttyTEST",
      baudrate: 9600,
      msgs_received: 10,
      satellites_in_view_count: 0,
      satellites_in_view: [],
      last_fix: {
        lat: 41.88,
        lon: -87.63,
        alt_m: 180,
        fix_type: 3,
        satellites_used: 7,
        timestamp: Date.now() / 1000,
      },
    },
    "/api/captive_portal": { available: true, portal_active: false, mode: "off" },
  };

  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/auth/login") return route.continue();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: success(responses[path] || {}),
    });
  });
}

async function login(page) {
  await page.goto("/login.html", { waitUntil: "domcontentloaded" });
  await page.getByLabel("Dashboard password").fill("browser-fixture");
  await Promise.all([page.waitForURL("/"), page.getByRole("button", { name: "Login" }).click()]);
  await expect(page.locator("#node-name")).toHaveText("Playwright Node");
}

async function revealAvailablePanelsForAccessibilityAudit(page) {
  await page.evaluate(() => {
    for (const section of document.querySelectorAll("main section")) {
      if (getComputedStyle(section).display === "none") continue;
      const toggle = section.querySelector(":scope > .section-header.collapsible");
      const target = toggle && document.getElementById(toggle.getAttribute("aria-controls"));
      if (!toggle || !target) continue;
      target.classList.remove("hidden");
      target.hidden = false;
      toggle.classList.add("open");
      toggle.setAttribute("aria-expanded", "true");
    }

    // Conditional controls are normally exposed only after their data exists.
    // Make them visible in this deterministic fixture so axe evaluates their
    // source-level names instead of silently excluding dormant markup.
    for (const id of ["msg-lora-compose", "routing-table-wrapper"]) {
      const element = document.getElementById(id);
      if (element) {
        element.classList.remove("hidden");
        element.hidden = false;
      }
    }
    const channel = document.getElementById("msg-lora-channel-wrap");
    if (channel) channel.style.display = "";
  });
}

test.beforeEach(async ({ page }) => {
  await installApiFixtures(page);
});

test("strict CSP and lazy GPS/Leaflet feature work without browser errors", async ({ page }) => {
  const violations = [];
  const errors = [];
  const featureRequests = [];
  let clientErrorPayload = null;
  await page.addInitScript(() => {
    window.__cspViolations = [];
    document.addEventListener("securitypolicyviolation", (event) => {
      window.__cspViolations.push({
        directive: event.effectiveDirective,
        blocked: event.blockedURI,
      });
    });
  });
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (/\/static\/assets\/feature-/.test(path)) featureRequests.push(path);
    if (path === "/api/client_error") clientErrorPayload = JSON.parse(request.postData());
  });

  const loginResponse = await page.goto("/login.html", { waitUntil: "domcontentloaded" });
  const csp = loginResponse.headers()["content-security-policy"];
  expect(csp).toContain("script-src 'self'");
  expect(csp).toContain("style-src-attr 'none'");
  expect(csp).not.toContain("unsafe-inline");
  await page.getByLabel("Dashboard password").fill("browser-fixture");
  await Promise.all([page.waitForURL("/"), page.getByRole("button", { name: "Login" }).click()]);
  await expect(page.locator("#node-name")).toHaveText("Playwright Node");
  expect(featureRequests.some((path) => path.includes("feature-gps-"))).toBe(false);

  await page.locator("#gps-section").scrollIntoViewIfNeeded();
  await expect.poll(() => featureRequests.some((path) => path.includes("feature-gps-"))).toBe(true);
  await page.waitForFunction(() => typeof window.RPI?.updateGps === "function");
  await page.waitForFunction(() => typeof window.L?.map === "function");
  await expect(page.locator("#gps-map .leaflet-map-pane")).toBeAttached();
  await page.evaluate(() => window.__rpiReportError(new Error("serialization fixture"), "e2e"));
  await expect.poll(() => clientErrorPayload?.message).toBe("serialization fixture");
  expect(clientErrorPayload.source).toBe("e2e");
  violations.push(...(await page.evaluate(() => window.__cspViolations)));

  expect(violations).toEqual([]);
  expect(errors).toEqual([]);
});

test("login and dashboard have no critical or serious WCAG 2.2 AA axe findings", async ({ page }) => {
  await page.goto("/login.html", { waitUntil: "domcontentloaded" });
  const loginResults = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
    .analyze();
  expect(loginResults.violations.filter((item) => ["critical", "serious"].includes(item.impact))).toEqual([]);

  await page.getByLabel("Dashboard password").fill("browser-fixture");
  await Promise.all([page.waitForURL("/"), page.getByRole("button", { name: "Login" }).click()]);
  await expect(page.locator("#node-name")).toHaveText("Playwright Node");
  await expect(page.locator("#radio-section")).toBeVisible();
  await revealAvailablePanelsForAccessibilityAudit(page);
  const dashboardResults = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
    .analyze();
  expect(
    dashboardResults.violations.filter((item) => ["critical", "serious"].includes(item.impact)),
  ).toEqual([]);
});

test("lazy radio and link-test disclosures expose named controls and visualizations", async ({
  page,
}) => {
  const featureRequests = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (/\/static\/assets\/feature-(radio|link-tester)-/.test(path)) featureRequests.push(path);
  });

  await login(page);
  const radioToggle = page.locator("#radio-toggle");
  const radioBody = page.locator("#radio-body");
  await expect(radioToggle).toBeVisible();
  await expect(radioToggle).toHaveAttribute("aria-expanded", "false");
  await radioToggle.click();
  await expect(radioToggle).toHaveAttribute("aria-expanded", "true");
  await expect(radioBody).toBeVisible();
  await expect.poll(() => featureRequests.some((path) => path.includes("feature-radio-"))).toBe(
    true,
  );
  await expect(page.locator("#radio-gain")).toHaveAccessibleName("Gain");
  await expect(page.locator("#radio-squelch")).toHaveAccessibleName("Squelch");
  await expect(page.locator("#radio-volume")).toHaveAccessibleName("Volume");
  await expect(page.locator("#radio-dial-svg")).toHaveAccessibleName("Radio frequency tuning dial");
  await expect(page.locator("#radio-vu-canvas")).toHaveAccessibleName("Live radio audio level");
  await expect(page.locator("#radio-fft-canvas")).toHaveAccessibleName(
    "Live radio audio spectrum",
  );

  const linkToggle = page.locator("#link-tester-toggle");
  const linkBody = page.locator("#link-tester-body");
  await expect(linkToggle).toBeVisible();
  await linkToggle.click();
  await expect(linkToggle).toHaveAttribute("aria-expanded", "true");
  await expect(linkBody).toBeVisible();
  await expect
    .poll(() => featureRequests.some((path) => path.includes("feature-link-tester-")))
    .toBe(true);
  await expect(page.locator("#link-tester-rtt-chart")).toHaveAccessibleName(
    "LoRa link round-trip time chart",
  );
  await expect(page.locator("#link-tester-signal-chart")).toHaveAccessibleName(
    "LoRa link signal quality chart",
  );

  const results = await new AxeBuilder({ page })
    .include("#radio-section")
    .include("#link-tester-section")
    .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
    .analyze();
  expect(
    results.violations.filter((item) => ["critical", "serious"].includes(item.impact)),
  ).toEqual([]);
});

test("restart confirmation traps initial focus and restores it on Escape", async ({ page }) => {
  let restartRequests = 0;
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/services/restart") restartRequests += 1;
  });
  await login(page);

  // The restart action is intentionally available only after a persisted
  // configuration change. Expose that post-change state so this test can
  // exercise the dialog in isolation without mutating fixture configuration.
  await page.locator("#restart-banner").evaluate((banner) => banner.classList.remove("hidden"));
  const trigger = page.getByRole("button", { name: "Restart Services" });
  await expect(trigger).toBeVisible();
  await trigger.focus();
  await trigger.press("Enter");
  const dialog = page.getByRole("dialog", { name: "Restart node services" });
  await expect(dialog).toBeVisible();
  await expect(page.getByLabel("Dashboard password", { exact: false }).last()).toBeFocused();
  expect(restartRequests).toBe(0);

  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await expect(trigger).toBeFocused();
  expect(restartRequests).toBe(0);
});

test("native collapsible buttons toggle exactly once from the keyboard", async ({ page }) => {
  await login(page);
  const toggle = page.locator("#plugins-toggle");
  const body = page.locator("#plugins-body");

  await expect(toggle).toHaveAccessibleName(/Plugins/i);
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(body).toBeHidden();
  await toggle.focus();
  await toggle.press("Enter");
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(body).toBeVisible();
  await toggle.press("Space");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(body).toBeHidden();
});

test("WebSocket updates coalesce into one animation-frame render without data loss", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const sockets = [];

    class ControlledWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      static CLOSING = 2;
      static CLOSED = 3;

      constructor(url) {
        this.url = url;
        this.readyState = ControlledWebSocket.CONNECTING;
        sockets.push(this);
        setTimeout(() => {
          this.readyState = ControlledWebSocket.OPEN;
          this.onopen?.({ target: this });
        }, 0);
      }

      send() {}

      close() {
        this.readyState = ControlledWebSocket.CLOSED;
        this.onclose?.({ target: this });
      }

      emit(message) {
        this.onmessage?.({ data: JSON.stringify(message), target: this });
      }
    }

    window.__controlledWebSockets = sockets;
    window.WebSocket = ControlledWebSocket;
  });

  await login(page);
  await page.waitForFunction(() =>
    window.__controlledWebSockets?.some((socket) => socket.url.includes("/ws/metrics")),
  );

  const result = await page.evaluate(() => {
    const socket = window.__controlledWebSockets.find((candidate) =>
      candidate.url.includes("/ws/metrics"),
    );
    const originalRequestAnimationFrame = window.requestAnimationFrame;
    const originalCancelAnimationFrame = window.cancelAnimationFrame;
    const frames = [];
    const renderCalls = { linkTester: [], weatherAlert: [] };

    window.RPI._pendingUpdate = null;
    window.RPI._rafPending = false;
    window.RPI.updateLinkTester = (value) => renderCalls.linkTester.push(value);
    window.RPI.updateWeatherAlert = (value) => renderCalls.weatherAlert.push(value);
    window.requestAnimationFrame = (callback) => {
      frames.push(callback);
      return frames.length;
    };
    window.cancelAnimationFrame = () => {};

    socket.emit({
      type: "update",
      data: {
        link_tester: { sequence: 1 },
        _removed: ["ais", "ais"],
      },
    });
    socket.emit({
      type: "update",
      data: {
        link_tester: { sequence: 3 },
        weather_alert: { sequence: 2 },
        _removed: ["ais", "acars", "ais"],
      },
    });

    const pendingBeforeRender = structuredClone(window.RPI._pendingUpdate);
    const queuedBeforeRender = frames.length;
    const frame = frames.shift();
    frame(performance.now());
    const pendingAfterRender = window.RPI._pendingUpdate;
    const framePendingAfterRender = window.RPI._rafPending;

    window.requestAnimationFrame = originalRequestAnimationFrame;
    window.cancelAnimationFrame = originalCancelAnimationFrame;

    return {
      queuedBeforeRender,
      pendingBeforeRender,
      pendingAfterRender,
      framePendingAfterRender,
      framesRemaining: frames.length,
      renderCalls,
    };
  });

  expect(result.queuedBeforeRender).toBe(1);
  expect(result.pendingBeforeRender).toEqual({
    link_tester: { sequence: 3 },
    _removed: ["ais", "acars"],
    weather_alert: { sequence: 2 },
  });
  expect(result.renderCalls).toEqual({
    linkTester: [{ sequence: 3 }],
    weatherAlert: [{ sequence: 2 }],
  });
  expect(result.pendingAfterRender).toBeNull();
  expect(result.framePendingAfterRender).toBe(false);
  expect(result.framesRemaining).toBe(0);
});

test("spectrum page supports pointer and keyboard zoom on a HiDPI-aware canvas", async ({ page }) => {
  await login(page);
  await page.goto("/spectrum.html", { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => typeof window.RPI?.spectrum?.update === "function");

  await page.evaluate(() => {
    const bins = Array.from({ length: 96 }, (_unused, index) => 902_000_000 + index * 250_000);
    const powers = bins.map((_unused, index) => -88 + 28 * Math.sin(index / 8));
    const snapshot = {
      status: "active",
      bins_hz: bins,
      latest_powers_db: powers,
      waterfall_tail: [powers],
      sweep_count: 1,
      start_hz: bins[0],
      stop_hz: bins[bins.length - 1],
      bin_size_hz: 250_000,
      gain: "auto",
    };
    window.RPI.spectrumCommon.historyStore.ingestTick(snapshot);
    window.RPI.spectrum.update(snapshot);
    window.RPI.loraSpectrum.update({ spectrum: snapshot });
  });

  const toggle = page.locator("#spectrum-toggle");
  const body = page.locator("#spectrum-body");
  const line = page.locator("#spectrum-line");
  const canvas = page.locator("#spectrum-waterfall");
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(body).toBeVisible();
  await expect(line.locator("polyline")).toBeAttached();

  const dimensions = await canvas.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return {
      backingWidth: element.width,
      backingHeight: element.height,
      cssWidth: rect.width,
      cssHeight: rect.height,
      dpr: window.devicePixelRatio,
      recordedDpr: Number(element.dataset.pixelRatio),
    };
  });
  expect(dimensions.recordedDpr).toBe(dimensions.dpr);
  expect(dimensions.backingWidth).toBeGreaterThanOrEqual(
    Math.floor(dimensions.cssWidth * dimensions.dpr) - 2,
  );
  expect(dimensions.backingHeight).toBeGreaterThanOrEqual(
    Math.floor(dimensions.cssHeight * dimensions.dpr) - 2,
  );

  await line.focus();
  await line.press("Enter");
  await expect(line).toHaveAttribute("aria-label", /zoomed from/i);
  await expect(page.locator("#spectrum-zoom-reset")).toBeVisible();
  const centeredLabel = await line.getAttribute("aria-label");
  await line.press("ArrowRight");
  await expect(line).not.toHaveAttribute("aria-label", centeredLabel);
  await line.press("Escape");
  await expect(line).toHaveAttribute("aria-label", /full band/i);

  const box = await line.boundingBox();
  expect(box).not.toBeNull();
  await page.mouse.move(box.x + box.width * 0.2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.7, box.y + box.height / 2);
  await page.mouse.up();
  await expect(line).toHaveAttribute("aria-label", /zoomed from/i);

  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
    .analyze();
  expect(
    results.violations.filter((item) => ["critical", "serious"].includes(item.impact)),
  ).toEqual([]);
});

test("responsive layout has no page overflow and keeps phone targets at 44px", async ({ page }) => {
  await login(page);
  const viewports = [
    { name: "phone-320", width: 320, height: 700 },
    { name: "phone-375", width: 375, height: 812 },
    { name: "phone-landscape", width: 599, height: 320 },
    { name: "tablet", width: 768, height: 1024 },
    { name: "desktop", width: 1440, height: 900 },
    { name: "4k", width: 3840, height: 2160 },
  ];

  for (const viewport of viewports) {
    await page.setViewportSize(viewport);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await expect(page.locator("#node-name")).toHaveText("Playwright Node");
    const layout = await page.evaluate(({ width }) => {
      const tooSmall = [];
      if (width < 600) {
        for (const control of document.querySelectorAll(
          "button, input, select, textarea, a.nav-link, .section-header.collapsible",
        )) {
          const rect = control.getBoundingClientRect();
          const style = getComputedStyle(control);
          if (
            style.display === "none" ||
            style.visibility === "hidden" ||
            rect.width === 0 ||
            rect.height === 0
          ) {
            continue;
          }
          if (rect.width < 44 || rect.height < 44) {
            tooSmall.push({ id: control.id || control.className, width: rect.width, height: rect.height });
          }
        }
      }
      const main = document.querySelector("main");
      const metrics = document.querySelector(".metrics-grid");
      const overflowing = Array.from(document.querySelectorAll("body *"))
        .map((element) => {
          const rect = element.getBoundingClientRect();
          return {
            element: element.id || element.className || element.tagName,
            left: Math.round(rect.left),
            right: Math.round(rect.right),
          };
        })
        .filter((item) => item.left < -1 || item.right > width + 1)
        .sort((left, right) => left.right - right.right)
        .slice(0, 40);
      return {
        viewport: document.documentElement.clientWidth,
        scrollWidth: document.documentElement.scrollWidth,
        mainWidth: main?.getBoundingClientRect().width || 0,
        metricsColumns: getComputedStyle(metrics).gridTemplateColumns.split(" ").length,
        tooSmall,
        overflowing,
      };
    }, viewport);

    expect(layout.scrollWidth, JSON.stringify(layout.overflowing)).toBeLessThanOrEqual(
      layout.viewport + 1,
    );
    expect(layout.tooSmall).toEqual([]);
    if (viewport.width < 600) expect(layout.metricsColumns).toBe(1);
    else if (viewport.width < 1024) expect(layout.metricsColumns).toBe(2);
    if (viewport.width >= 1440) expect(layout.mainWidth).toBeLessThanOrEqual(1440);

    const results = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
      .analyze();
    expect(
      results.violations.filter((item) => ["critical", "serious"].includes(item.impact)),
    ).toEqual([]);
  }
});
