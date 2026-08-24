import { defineConfig } from "@playwright/test";

const baseURL = process.env.OPENEVO_E2E_BASE_URL ?? "http://127.0.0.1:5173/product-preview.html";

export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: "development-agent-web-layer.spec.ts",
  fullyParallel: false,
  workers: 1,
  timeout: 15 * 60 * 1000,
  expect: { timeout: 30_000 },
  metadata: {
    openevoChain: "development",
    productURL: baseURL,
  },
  reporter: [["list"], ["html", { outputFolder: "test-results/development-agent-web-report", open: "never" }]],
  outputDir: "test-results/development-agent-web",
  use: {
    baseURL,
    channel: process.env.OPENEVO_E2E_BROWSER_CHANNEL,
    headless: process.env.OPENEVO_E2E_HEADED !== "1",
    viewport: { width: 1600, height: 1000 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },
});
