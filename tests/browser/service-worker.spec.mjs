import { expect, test } from "@playwright/test";

test.use({ serviceWorkers: "allow" });

async function installApiFixtures(page) {
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/auth/login" || path === "/api/auth/logout") {
      return route.continue();
    }
    const values = {
      "/api/node": {
        node_name: "Offline Fixture",
        version: "test",
        identity_hash: "offline-test",
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
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        data: values[path] || {},
        timestamp: Date.now() / 1000,
      }),
    });
  });
}

async function login(page) {
  await page.goto("/login.html", { waitUntil: "domcontentloaded" });
  await page.getByLabel("Dashboard password").fill("browser-fixture");
  await Promise.all([page.waitForURL("/"), page.getByRole("button", { name: "Login" }).click()]);
  await expect(page.locator("#node-name")).toHaveText("Offline Fixture");
}

async function waitForServiceWorkerControl(page) {
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
    if (!navigator.serviceWorker.controller) {
      await new Promise((resolve) => {
        navigator.serviceWorker.addEventListener("controllerchange", resolve, { once: true });
      });
    }
  });
}

async function configureServiceWorkerFixture(page, fixture) {
  await page.evaluate(async (value) => {
    const response = await fetch("/__test/service-worker", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
      },
      body: JSON.stringify(value),
    });
    if (!response.ok) throw new Error(`service-worker fixture failed: ${response.status}`);
  }, fixture);
}

test("complete shell installs atomically and reloads offline without caching private data", async ({
  browserName,
  context,
  page,
}) => {
  test.skip(browserName !== "chromium", "Chromium is the authoritative service-worker lane");
  await installApiFixtures(page);
  await login(page);
  await waitForServiceWorkerControl(page);

  const installed = await page.evaluate(async () => {
    const names = await caches.keys();
    const shell = names.find((name) => name.startsWith("rpi-shell-"));
    const requests = shell ? await (await caches.open(shell)).keys() : [];
    return {
      names,
      paths: requests.map((request) => new URL(request.url).pathname),
    };
  });

  expect(installed.names.filter((name) => name.startsWith("rpi-shell-"))).toHaveLength(1);
  expect(installed.paths).toEqual(
    expect.arrayContaining(["/index.html", "/login.html", "/spectrum.html"]),
  );
  expect(installed.paths.some((path) => path === "/api" || path.startsWith("/api/"))).toBe(false);
  expect(installed.paths.some((path) => path === "/auth" || path.startsWith("/auth/"))).toBe(false);
  expect(installed.paths.filter((path) => /\/assets\/feature-/.test(path))).toEqual([]);

  await context.setOffline(true);
  const offlineStarted = Date.now();
  const offlineResponse = await page.reload({ waitUntil: "domcontentloaded" });
  const offlineMilliseconds = Date.now() - offlineStarted;
  expect(offlineResponse).not.toBeNull();
  expect(offlineResponse.fromServiceWorker()).toBe(true);
  expect(offlineResponse.status()).toBe(200);
  expect(offlineMilliseconds).toBeLessThanOrEqual(1000);
  await expect(page.locator("main")).toBeVisible();
  await context.setOffline(false);

  // A successful spectrum navigation must refresh only the spectrum fallback.
  // The historical bug stored this response under /index.html, so both routes
  // appeared to work until an offline dashboard navigation returned the wrong
  // document shell.
  await page.goto("/spectrum.html", { waitUntil: "domcontentloaded" });
  await expect(page).toHaveTitle("Spectrum — ReticulumPi");
  await expect(page.locator("#spectrum-main")).toBeAttached();
  await context.setOffline(true);
  const offlineSpectrum = await page.reload({ waitUntil: "domcontentloaded" });
  expect(offlineSpectrum).not.toBeNull();
  expect(offlineSpectrum.fromServiceWorker()).toBe(true);
  expect(offlineSpectrum.status()).toBe(200);
  await expect(page).toHaveTitle("Spectrum — ReticulumPi");
  await expect(page.locator("#spectrum-main")).toBeAttached();
  await expect(page.locator("#main-content")).toHaveCount(0);

  const offlineDashboard = await page.goto("/index.html", { waitUntil: "domcontentloaded" });
  expect(offlineDashboard).not.toBeNull();
  expect(offlineDashboard.fromServiceWorker()).toBe(true);
  expect(offlineDashboard.status()).toBe(200);
  await expect(page.locator("#main-content")).toBeVisible();
  await expect(page.locator("#spectrum-main")).toHaveCount(0);
  await context.setOffline(false);

  await page.evaluate(async () => {
    await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
  });
  const privateEntries = await page.evaluate(async () => {
    const requests = [];
    for (const name of await caches.keys()) {
      requests.push(...(await (await caches.open(name)).keys()));
    }
    return requests
      .map((request) => new URL(request.url).pathname)
      .filter(
        (path) =>
          path === "/api" ||
          path.startsWith("/api/") ||
          path === "/auth" ||
          path.startsWith("/auth/"),
      );
  });
  expect(privateEntries).toEqual([]);
});

test("interrupted shell update preserves the complete prior version and rolls back", async ({
  browserName,
  context,
  page,
}) => {
  test.skip(browserName !== "chromium", "Chromium is the authoritative service-worker lane");
  await installApiFixtures(page);
  await login(page);
  await waitForServiceWorkerControl(page);

  const prior = await page.evaluate(async () => {
    const names = await caches.keys();
    const name = names.find((candidate) => candidate.startsWith("rpi-shell-"));
    const cache = await caches.open(name);
    const requests = await cache.keys();
    return {
      name,
      paths: requests.map((request) => new URL(request.url).pathname).sort(),
      hasDashboard: Boolean(await cache.match("/index.html")),
    };
  });
  expect(prior.name).toBeTruthy();
  expect(prior.hasDashboard).toBe(true);

  await configureServiceWorkerFixture(page, {
    version: "interrupted-update",
    failed_asset: "/static/asset-manifest.json",
  });
  const interrupted = await page.evaluate(async () => {
    const registration = await navigator.serviceWorker.getRegistration();
    const transition = new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error("update worker did not settle")), 10_000);
      registration.addEventListener(
        "updatefound",
        () => {
          const worker = registration.installing;
          const record = () => {
            if (worker.state !== "redundant" && worker.state !== "activated") return;
            clearTimeout(timeout);
            resolve(worker.state);
          };
          worker.addEventListener("statechange", record);
          record();
        },
        { once: true },
      );
    });
    await registration.update();
    const state = await transition;
    return {
      state,
      active: registration.active?.state,
      waiting: registration.waiting?.state || null,
    };
  });
  expect(interrupted).toEqual({ state: "redundant", active: "activated", waiting: null });

  const afterFailure = await page.evaluate(async (priorName) => {
    const names = await caches.keys();
    const cache = await caches.open(priorName);
    const requests = await cache.keys();
    return {
      names,
      paths: requests.map((request) => new URL(request.url).pathname).sort(),
      hasDashboard: Boolean(await cache.match("/index.html")),
    };
  }, prior.name);
  expect(afterFailure.names).toContain(prior.name);
  expect(afterFailure.paths).toEqual(prior.paths);
  expect(afterFailure.hasDashboard).toBe(true);

  await configureServiceWorkerFixture(page, {
    version: prior.name.replace(/^rpi-shell-/, ""),
    failed_asset: null,
  });
  const rolledBack = await page.evaluate(async (priorName) => {
    const registration = await navigator.serviceWorker.getRegistration();
    const source = await fetch("/sw.js", { cache: "no-store", credentials: "same-origin" }).then(
      (response) => response.text(),
    );
    await registration.update();
    return {
      servedPriorVersion: source.includes(`var SHELL_CACHE = '${priorName}';`),
      active: registration.active?.state,
      installing: registration.installing?.state || null,
      waiting: registration.waiting?.state || null,
    };
  }, prior.name);
  expect(rolledBack).toEqual({
    servedPriorVersion: true,
    active: "activated",
    installing: null,
    waiting: null,
  });

  await context.setOffline(true);
  const response = await page.reload({ waitUntil: "domcontentloaded" });
  expect(response).not.toBeNull();
  expect(response.fromServiceWorker()).toBe(true);
  expect(response.status()).toBe(200);
  await expect(page.locator("#main-content")).toBeVisible();
  await context.setOffline(false);
});
