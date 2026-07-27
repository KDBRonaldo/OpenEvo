import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.goto("/lifecycle-preview.html");
  await expect(page.getByRole("heading", { name: "Connect remote workspace" })).toBeVisible();
});

test("long lifecycle work is observable, pageable, cancellable, and viewport-safe", async ({
  page,
}) => {
  const connect = operationPanel(page, "Connect remote workspace");

  await expect(connect.locator(".lifecycle-progress-label").getByText("Transferring", { exact: true })).toBeVisible();
  await expect(connect.getByText("Checkpoint 7 of 17", { exact: true })).toBeVisible();
  await expect(connect.getByText("8 MB of 32 MB", { exact: true })).toBeVisible();
  await expect(connect.getByText("SSH output", { exact: true }).first()).toBeVisible();
  await expect(connect.getByText("Daemon error", { exact: true }).first()).toBeVisible();
  await expect(connect.getByText(/SSH transfer block 61 accepted/)).toBeVisible();
  await expect(connect.getByText("line truncated", { exact: true })).toBeVisible();

  await connect.getByRole("button", { name: "Show all logs" }).click();
  await expect(connect.getByText(/Daemon readiness probe 51/)).toBeVisible();
  await assertViewportSafety(page);

  await connect.getByRole("button", { name: "Load older logs" }).click();
  await expect(connect.getByText(/SSH transfer block 1 accepted/)).toBeVisible();
  await expect(connect.getByRole("button", { name: "Show latest log tail" })).toBeVisible();

  await page.keyboard.press("Escape");
  await expect(connect.getByRole("button", { name: "Show all logs" })).toBeVisible();
  await connect.getByRole("button", { name: "Show latest log tail" }).click();
  await expect(connect.getByText(/registry-chunk-/)).toBeVisible();

  await page.emulateMedia({ reducedMotion: "reduce" });
  const core = operationPanel(page, "Clean safe remote caches");
  await expect(core.getByText("Working — progress is not measurable for this phase", { exact: true })).toBeVisible();
  await expect(core.locator('progress[aria-label="Current phase progress"]')).toHaveCSS("animation-name", "none");

  await assertAccessibility(page);
  await assertViewportSafety(page);
  await expect(page.locator(".v2-global-operations")).toHaveScreenshot("lifecycle-operations.png", {
    mask: [page.locator(".lifecycle-meta > span")],
  });

  await connect.getByRole("button", { name: "Cancel operation" }).click();
  await expect(connect.getByText("Cancelled", { exact: true }).first()).toBeVisible();
  await expect(connect.getByRole("button", { name: "Cancel operation" })).toHaveCount(0);
  await expect(connect.getByRole("button", { name: "Resume / reconcile" })).toBeVisible();
});

async function assertAccessibility(page: Page): Promise<void> {
  const accessibility = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  const blockingViolations = accessibility.violations.filter(
    (violation) => violation.impact === "serious" || violation.impact === "critical",
  );
  expect(blockingViolations, JSON.stringify(blockingViolations, null, 2)).toEqual([]);
}

async function assertViewportSafety(page: Page): Promise<void> {
  const result = await page.evaluate(() => {
    const viewportWidth = window.innerWidth;
    const documentOverflow =
      Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) - viewportWidth;
    const operationOverflow = Array.from(document.querySelectorAll<HTMLElement>(
      ".v2-global-operations, .lifecycle-operation-panel, .lifecycle-log-section, .lifecycle-log-viewport",
    )).map((element) => ({
      className: element.className,
      overflow: element.scrollWidth - element.clientWidth,
    }));
    const clippedControls = Array.from(document.querySelectorAll<HTMLElement>(
      "button, a[href], input, select, textarea, [role='tab'], [tabindex]",
    )).filter((element) => {
      const style = window.getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.visibility !== "hidden"
        && style.display !== "none"
        && rect.width > 0
        && rect.height > 0
        && (rect.left < -1 || rect.right > viewportWidth + 1);
    }).map((element) => ({
      label: element.getAttribute("aria-label") ?? element.textContent?.trim().slice(0, 80) ?? "",
      rect: element.getBoundingClientRect().toJSON(),
    }));
    return { documentOverflow, operationOverflow, clippedControls };
  });

  expect(result.documentOverflow, `document overflows by ${result.documentOverflow}px`).toBeLessThanOrEqual(1);
  expect(
    result.operationOverflow.filter((entry) => entry.overflow > 1),
    JSON.stringify(result.operationOverflow, null, 2),
  ).toEqual([]);
  expect(result.clippedControls, JSON.stringify(result.clippedControls, null, 2)).toEqual([]);
}

function operationPanel(page: Page, heading: string) {
  return page.locator(".lifecycle-operation-panel").filter({
    has: page.getByRole("heading", { name: heading, exact: true }),
  });
}
