import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/product-browser",
  testMatch: "**/*.pw.ts",
  outputDir: "./test-results/product-browser",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["line"], ["html", { open: "never" }]] : "line",
  use: {
    baseURL: "http://127.0.0.1:4174",
    colorScheme: "light",
    locale: "zh-CN",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  expect: {
    toHaveScreenshot: {
      animations: "disabled",
      maxDiffPixelRatio: 0.015,
    },
  },
  projects: [
    { name: "desktop-1440", use: { viewport: { width: 1440, height: 900 } } },
    { name: "desktop-1024", use: { viewport: { width: 1024, height: 768 } } },
    { name: "minimum-760", use: { viewport: { width: 760, height: 800 } } },
  ],
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 4174 --strictPort",
    url: "http://127.0.0.1:4174/product-preview.html?scenario=new-user",
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
