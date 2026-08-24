import { defineConfig } from "@playwright/test";

const productURL = process.env.OPENEVO_FORMAL_BROWSER_URL?.trim();
const sshHostAlias = process.env.OPENEVO_FORMAL_SSH_ALIAS?.trim();

if (!productURL) {
  throw new Error(
    "OPENEVO_FORMAL_BROWSER_URL is required. Start `npm run dev:formal:browser`, "
      + "keep it running, and copy the complete /openevo#browser-bootstrap=... URL.",
  );
}

const parsedURL = new URL(productURL);
if (parsedURL.hostname !== "127.0.0.1" || parsedURL.pathname !== "/openevo") {
  throw new Error(
    "OPENEVO_FORMAL_BROWSER_URL must be the complete loopback /openevo URL printed by the formal browser launcher.",
  );
}
if (!parsedURL.hash.startsWith("#browser-bootstrap=")) {
  throw new Error(
    "OPENEVO_FORMAL_BROWSER_URL must include the browser-bootstrap fragment printed by the formal browser launcher.",
  );
}
if (!sshHostAlias) {
  throw new Error(
    "OPENEVO_FORMAL_SSH_ALIAS is required and must name a literal Host entry in ~/.ssh/config.",
  );
}

export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: "development-agent-web-layer.spec.ts",
  fullyParallel: false,
  workers: 1,
  timeout: 15 * 60 * 1000,
  expect: { timeout: 30_000 },
  metadata: {
    openevoChain: "formal",
    productURL,
    sshHostAlias,
  },
  reporter: [["list"], ["html", {
    outputFolder: "test-results/formal-desktop-report",
    open: "never",
  }]],
  outputDir: "test-results/formal-desktop",
  use: {
    channel: process.env.OPENEVO_E2E_BROWSER_CHANNEL,
    headless: process.env.OPENEVO_E2E_HEADED !== "1",
    viewport: { width: 1600, height: 1000 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },
});
