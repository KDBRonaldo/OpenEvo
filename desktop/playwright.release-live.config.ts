import { defineConfig } from "@playwright/test";

const configuredTimeout = process.env.OPENEVO_DESKTOP_LIVE_RENDERER_TIMEOUT_MS;
const timeout = configuredTimeout && /^\d+$/.test(configuredTimeout)
  ? Number(configuredTimeout)
  : 300_000;
if (!Number.isSafeInteger(timeout) || timeout < 30_000 || timeout > 600_000) {
  throw new Error("OPENEVO_DESKTOP_LIVE_RENDERER_TIMEOUT_MS is outside the closed test budget");
}

export default defineConfig({
  testDir: "./tests/product-browser",
  testMatch: "release-live-observability.pw.ts",
  outputDir: "./test-results/release-live-observability",
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  reporter: "line",
  timeout,
  expect: { timeout: 30_000 },
  use: {
    baseURL: "http://tauri.localhost",
    colorScheme: "light",
    locale: "zh-CN",
    permissions: ["local-network-access"],
    viewport: { width: 1440, height: 900 },
    acceptDownloads: false,
    serviceWorkers: "block",
    screenshot: "off",
    trace: "off",
    video: "off",
  },
});
