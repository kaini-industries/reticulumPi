import { defineConfig, devices } from "@playwright/test";

const port = Number(process.env.RETICULUMPI_BROWSER_TEST_PORT || 18765);
const baseURL = `http://127.0.0.1:${port}`;
const python = process.env.RETICULUMPI_BROWSER_TEST_PYTHON || "python3";

export default defineConfig({
  testDir: "./tests/browser",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 3 : undefined,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  outputDir: process.env.RETICULUMPI_PLAYWRIGHT_OUTPUT || "/tmp/reticulumpi-playwright-results",
  reporter: [["line"]],
  use: {
    baseURL,
    serviceWorkers: "block",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "firefox", use: { ...devices["Desktop Firefox"] } },
    { name: "webkit", use: { ...devices["Desktop Safari"] } },
  ],
  webServer: {
    command: `"${python}" tools/dashboard_browser_server.py`,
    url: `${baseURL}/login.html`,
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
    env: {
      ...process.env,
      RETICULUMPI_BROWSER_TEST_PORT: String(port),
      RETICULUMPI_BROWSER_TEST_PASSWORD: "browser-fixture",
    },
  },
});
