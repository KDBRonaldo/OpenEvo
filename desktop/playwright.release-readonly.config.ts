import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/product-browser",
  testMatch: "release-readonly.pw.ts",
  outputDir: "./test-results/release-readonly",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["line"], ["html", { open: "never" }]] : "line",
  use: {
    baseURL: "http://127.0.0.1:4176",
    colorScheme: "light",
    locale: "zh-CN",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "release-packaged-1440",
      use: { viewport: { width: 1440, height: 900 } },
    },
    {
      name: "release-packaged-1024",
      use: { viewport: { width: 1024, height: 768 } },
    },
    {
      name: "release-packaged-760",
      use: { viewport: { width: 760, height: 600 } },
    },
  ],
  webServer: {
    command: "npm run build:openevo && npm run preview -- --host 127.0.0.1 --port 4176 --strictPort --outDir packaging/web",
    url: "http://127.0.0.1:4176/",
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
