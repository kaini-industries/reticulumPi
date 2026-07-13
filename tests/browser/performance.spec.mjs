import { expect, test } from "@playwright/test";

function success(data) {
  return JSON.stringify({ ok: true, data, timestamp: Date.now() / 1000 });
}

async function installApiFixtures(page) {
  const responses = {
    "/api/node": {
      node_name: "Performance Fixture",
      version: "test",
      identity_hash: "performance-test",
      uptime: 1,
      server_time: Date.now() / 1000,
    },
    "/api/metrics": {
      cpu_percent: 1,
      memory_percent: 2,
      disk_percent: 3,
      temperature_c: 40,
      uptime: 1,
    },
    "/api/interfaces": { interfaces: [] },
    "/api/plugins": { plugins: {}, failed_plugins: [] },
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

async function establishSession(context, page) {
  await page.goto("/login.html", { waitUntil: "domcontentloaded" });
  const response = await context.request.post("/api/auth/login", {
    data: { password: "browser-fixture" },
    headers: { "X-Requested-With": "XMLHttpRequest" },
  });
  expect(response.ok()).toBe(true);
  // The associated request context shares its cookie jar with the browser, so
  // authentication is established without first warming the dashboard shell.
  await page.evaluate(async () => {
    const registrations = await navigator.serviceWorker?.getRegistrations?.();
    await Promise.all((registrations || []).map((registration) => registration.unregister()));
    await Promise.all((await caches.keys()).map((name) => caches.delete(name)));
  });
}

test("@performance slow-LAN shell meets Web Vitals and main-thread budgets", async ({
  browserName,
  context,
  page,
}) => {
  test.skip(browserName !== "chromium", "Chromium is the authoritative performance lane");
  await installApiFixtures(page);
  await establishSession(context, page);

  const cdp = await context.newCDPSession(page);
  await cdp.send("Network.enable");
  await cdp.send("Network.clearBrowserCache");
  await page.addInitScript(() => {
    window.__reticulumpiPerformance = {
      cls: 0,
      lcp: 0,
      longestTask: 0,
      longestInteraction: 0,
      firstInputDuration: 0,
      firstInputObserved: false,
      frames: 0,
      frameStarted: 0,
      frameEnded: 0,
      shifts: [],
    };
    const state = window.__reticulumpiPerformance;
    try {
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          if (!entry.hadRecentInput) {
            state.cls += entry.value;
            if (state.shifts.length < 20) {
              state.shifts.push({
                value: entry.value,
                startTime: entry.startTime,
                nodes: entry.sources.map((source) => {
                  const node = source.node;
                  if (!node) return "unknown";
                  return node.id ? `#${node.id}` : node.className || node.tagName;
                }),
              });
            }
          }
        }
      }).observe({ type: "layout-shift", buffered: true });
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) state.lcp = Math.max(state.lcp, entry.startTime);
      }).observe({ type: "largest-contentful-paint", buffered: true });
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          state.longestTask = Math.max(state.longestTask, entry.duration);
        }
      }).observe({ type: "longtask", buffered: true });
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          if (entry.interactionId) {
            state.longestInteraction = Math.max(state.longestInteraction, entry.duration);
          }
        }
      }).observe({ type: "event", buffered: true, durationThreshold: 16 });
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          state.firstInputObserved = true;
          state.firstInputDuration = Math.max(state.firstInputDuration, entry.duration);
        }
      }).observe({ type: "first-input", buffered: true });
    } catch {
      // Unsupported entry types stay at zero and are reported below.
    }
    const countFrame = (timestamp) => {
      if (!state.frameStarted) state.frameStarted = timestamp;
      state.frameEnded = timestamp;
      state.frames += 1;
      if (timestamp - state.frameStarted < 2000) requestAnimationFrame(countFrame);
    };
    requestAnimationFrame(countFrame);
  });

  // 1 Mbps downstream with 150 ms RTT, matching the release gate.
  await cdp.send("Network.emulateNetworkConditions", {
    offline: false,
    latency: 150,
    downloadThroughput: 125_000,
    uploadThroughput: 125_000,
    connectionType: "cellular3g",
  });

  const criticalRequests = new Set();
  page.on("request", (request) => {
    if (!["document", "script", "stylesheet"].includes(request.resourceType())) return;
    criticalRequests.add(new URL(request.url()).pathname);
  });

  const started = Date.now();
  const navigation = await page.goto("/index.html?performance=1", {
    waitUntil: "domcontentloaded",
  });
  expect(navigation).not.toBeNull();
  expect(navigation.fromServiceWorker()).toBe(false);
  await expect(page.locator("#node-name")).toHaveText("Performance Fixture");
  const usableMilliseconds = Date.now() - started;

  const plugins = page.locator("#plugins-toggle");
  await plugins.focus();
  await plugins.press("Enter");
  await expect(plugins).toHaveAttribute("aria-expanded", "true");
  await page.waitForTimeout(2200);

  const metrics = await page.evaluate(() => window.__reticulumpiPerformance);
  const frameDuration = metrics.frameEnded - metrics.frameStarted;
  const frameRate = frameDuration > 0 ? ((metrics.frames - 1) * 1000) / frameDuration : 0;
  const evidence = {
    usableMilliseconds,
    criticalRequests: criticalRequests.size,
    lcpMilliseconds: metrics.lcp,
    cls: metrics.cls,
    longestInteractionMilliseconds: Math.max(
      metrics.longestInteraction,
      metrics.firstInputDuration,
    ),
    firstInputObserved: metrics.firstInputObserved,
    longestTaskMilliseconds: metrics.longestTask,
    frameRate,
  };
  await test.info().attach("dashboard-performance.json", {
    body: Buffer.from(`${JSON.stringify(evidence, null, 2)}\n`),
    contentType: "application/json",
  });
  console.log(`ReticulumPi performance evidence: ${JSON.stringify(evidence)}`);

  expect(usableMilliseconds).toBeLessThanOrEqual(4000);
  expect(criticalRequests.size).toBeLessThanOrEqual(6);
  expect(metrics.lcp).toBeGreaterThan(0);
  expect(metrics.lcp).toBeLessThanOrEqual(2500);
  expect(metrics.cls, JSON.stringify(metrics.shifts)).toBeLessThanOrEqual(0.1);
  expect(metrics.firstInputObserved).toBe(true);
  expect(Math.max(metrics.longestInteraction, metrics.firstInputDuration)).toBeGreaterThan(0);
  expect(Math.max(metrics.longestInteraction, metrics.firstInputDuration)).toBeLessThanOrEqual(200);
  expect(metrics.longestTask).toBeLessThanOrEqual(50);
  expect(frameRate).toBeGreaterThanOrEqual(55);
});
