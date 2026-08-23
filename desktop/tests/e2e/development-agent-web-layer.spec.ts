import { expect, test, type Page, type TestInfo } from "@playwright/test";

const RUN_ID = new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14);
const PROJECT_NAME = `Web Layer browser E2E ${RUN_ID}`;
const TASK_TITLE = `Browser acceptance ${RUN_ID}`;
const RESULT_MARKER = `OPENEVO_BROWSER_E2E_OK_${RUN_ID}`;
const PRODUCT_URL = process.env.OPENEVO_E2E_BASE_URL ?? "";

async function assertNoProductErrors(page: Page): Promise<void> {
  await expect(page.getByText("Refresh failed", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Action could not be completed", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Connection action failed", { exact: true })).toHaveCount(0);
}

test("real browser can create a project, run an agent session, and produce evolution output", async ({ page }, testInfo: TestInfo) => {
  const browserErrors: string[] = [];
  const observedRequests: string[] = [];
  let eventConnectionAttempts = 0;
  await page.route("**/desktop/v2/events", async (route) => {
    eventConnectionAttempts += 1;
    if (eventConnectionAttempts === 1) {
      await route.abort("connectionfailed");
      return;
    }
    await route.continue();
  });
  page.on("request", (request) => {
    const url = new URL(request.url());
    observedRequests.push(`${request.method()} ${url.pathname}`);
  });
  page.on("console", (message) => {
    if (message.type() === "error" && !message.text().includes("Failed to load resource")) {
      browserErrors.push(`console: ${message.text()}`);
    }
  });
  page.on("pageerror", (error) => browserErrors.push(`pageerror: ${error.message}`));
  page.on("response", (response) => {
    const url = new URL(response.url());
    if (response.status() >= 400 && url.pathname !== "/favicon.ico") {
      browserErrors.push(`http ${response.status()}: ${url.pathname}`);
    }
  });

  await test.step("open the real development product through Vite and Web Layer", async () => {
    await page.goto(PRODUCT_URL, { waitUntil: "domcontentloaded", timeout: 60_000 });
    await expect(page.locator(".product-shell")).toBeVisible({ timeout: 60_000 });
    await expect(page.getByText("Real-agent development mode", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Add remote workspace" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Remote workspace settings" }).first()).toBeVisible();
    expect(observedRequests).toContain("GET /desktop/v2/state");
    await expect.poll(() => eventConnectionAttempts, { timeout: 30_000 }).toBeGreaterThanOrEqual(2);
    expect(observedRequests).toContain("GET /desktop/v2/events");
    await assertNoProductErrors(page);
  });

  await test.step("create a project by using the visible project form", async () => {
    await page.getByRole("button", { name: "Create project" }).click();
    const dialog = page.getByRole("dialog", { name: "Create science project" });
    await expect(dialog).toBeVisible();
    await dialog.getByLabel("Project name").fill(PROJECT_NAME);
    await dialog.getByLabel("Task title").fill(TASK_TITLE);
    await dialog.getByLabel("Task objective").fill(
      `Reply with exactly ${RESULT_MARKER}. Do not modify files and do not perform network access.`,
    );
    await dialog.getByRole("button", { name: "Create project", exact: true }).click();
    await expect(dialog).toBeHidden({ timeout: 120_000 });
    await expect(page.getByRole("combobox", { name: "Select project" })).toHaveValue(/project:/);
    await expect(page.getByRole("heading", { name: PROJECT_NAME, exact: true })).toBeVisible({ timeout: 120_000 });
    await expect(page.getByRole("button", { name: "Start session" })).toBeEnabled({ timeout: 120_000 });
    expect(observedRequests).toContain("POST /desktop/v2/projects");
    await assertNoProductErrors(page);
  });

  await test.step("upload and reload a real workspace file through daemon v2", async () => {
    await page.getByLabel("Choose files to upload").setInputFiles({
      name: "browser-v2-evidence.txt",
      mimeType: "text/plain",
      buffer: Buffer.from(`workspace-v2-${RUN_ID}\n`, "utf8"),
    });
    await expect(
      page.getByRole("treeitem").filter({ hasText: "browser-v2-evidence.txt" }),
    ).toBeVisible({ timeout: 120_000 });
    expect(observedRequests.some((request) => (
      request.startsWith("PUT /desktop/v2/development/projects/")
    ))).toBe(true);
    await page.reload({ waitUntil: "domcontentloaded", timeout: 60_000 });
    await expect(
      page.getByRole("treeitem").filter({ hasText: "browser-v2-evidence.txt" }),
    ).toBeVisible({ timeout: 120_000 });
    await assertNoProductErrors(page);
  });

  await test.step("start the real remote agent session", async () => {
    await expect(page.getByText("Run separately", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Start session" }).click();
    await expect(page.getByTestId("session-detail-workspace")).toBeVisible({ timeout: 120_000 });
    expect(observedRequests).toContain("POST /desktop/v2/tasks");
    await assertNoProductErrors(page);
  });

  await test.step("wait for the remote agent response and sealed transcript", async () => {
    const detail = page.getByTestId("session-detail-workspace");
    await expect(detail.locator(".session-inspector-heading .state-pill")).toHaveText("closed", {
      timeout: 10 * 60 * 1000,
    });
    await expect(detail.getByLabel("Agent")).toContainText(RESULT_MARKER, { timeout: 60_000 });
    await assertNoProductErrors(page);
  });

  await test.step("reload the browser and hydrate the same authoritative Session", async () => {
    await page.reload({ waitUntil: "domcontentloaded", timeout: 60_000 });
    const detail = page.getByTestId("session-detail-workspace");
    await expect(detail).toBeVisible({ timeout: 120_000 });
    await expect(detail.locator(".session-inspector-heading .state-pill")).toHaveText("closed");
    await expect(detail.getByLabel("Agent")).toContainText(RESULT_MARKER);
    await assertNoProductErrors(page);
  });

  await test.step("prove logs remain reachable from the rendered product", async () => {
    const technical = page.locator("details.session-troubleshooting-disclosure");
    await technical.locator(":scope > summary").click();
    await technical.getByRole("button", { name: "Refresh task logs" }).click();
    await expect(technical).toContainText(/Task|Attempt|closed/i, { timeout: 60_000 });
    await assertNoProductErrors(page);
  });

  await test.step("run text-memory evolution from the completed Session and apply it", async () => {
    await page.getByRole("button", { name: "Evolution", exact: true }).click();
    const workspace = page.getByTestId("evolution-workspace");
    await expect(workspace).toBeVisible({ timeout: 60_000 });
    await expect(workspace.getByText(TASK_TITLE, { exact: true })).toBeVisible({ timeout: 60_000 });

    const target = workspace.locator(".v2-target-list article").filter({ hasText: /text memory/i });
    await expect(target).toHaveCount(1);
    const targetCheckbox = target.getByRole("checkbox");
    if (!(await targetCheckbox.isChecked())) await targetCheckbox.check();
    const runEvolution = workspace.getByRole("button", { name: "Run Evolution" });
    await expect(runEvolution).toBeEnabled();
    await runEvolution.click();

    const currentRun = workspace.locator(".v2-current-evolution-run");
    await expect(currentRun).toBeVisible({ timeout: 120_000 });
    await expect(currentRun.locator(".state-pill")).toHaveText("candidate ready", {
      timeout: 5 * 60 * 1000,
    });
    await expect(currentRun.locator(".v2-current-evolution-result")).toBeVisible();
    await currentRun.getByRole("button", { name: "Apply to future Sessions" }).click();
    await expect(currentRun.locator(".state-pill")).toHaveText("applied", { timeout: 120_000 });
    await expect(currentRun.getByText("Active context", { exact: true })).toBeVisible();
    expect(observedRequests).toContain("POST /openevo-dev-agent/v1/evolution-runs");
    await assertNoProductErrors(page);
  });

  await page.screenshot({ path: testInfo.outputPath("full-chain-passed.png"), fullPage: true });
  expect(browserErrors, `Browser errors:\n${browserErrors.join("\n")}`).toEqual([]);
});
