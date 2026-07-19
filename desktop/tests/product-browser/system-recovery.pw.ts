import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.goto("/product-preview.html?scenario=degraded");
  await expect(page.getByText("Remote services need attention", { exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: "System", exact: true }).click();
  await expect(page.getByTestId("system-workspace")).toBeVisible();
});

test("System recovery is keyboard-operable, accessible, and viewport-safe", async ({
  page,
}) => {
  await expect(page.getByText("Remote services need attention", { exact: true })).toBeVisible();

  const check = page.getByRole("button", { name: "Check", exact: true });
  await check.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("Check remote environment", { exact: true })).toBeVisible();

  const diagnostics = page.getByRole("button", { name: "Diagnostics", exact: true });
  await diagnostics.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("Remote diagnostics", { exact: true })).toBeVisible();
  await expect(page.getByText("Evolution registry", { exact: true })).toBeVisible();

  const restart = page.getByRole("button", { name: "Restart OpenEvo runtime" });
  await restart.focus();
  await page.keyboard.press("Enter");
  const restartDialog = page.getByRole("alertdialog");
  await expect(restartDialog).toContainText("Restart OpenEvo runtime?");
  const restartCancel = restartDialog.getByRole("button", { name: "Cancel", exact: true });
  const restartConfirm = restartDialog.getByRole("button", { name: "Confirm", exact: true });
  await expect(restartCancel).toBeFocused();
  await restartConfirm.focus();
  await page.keyboard.press("Tab");
  await expect(restartCancel).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(restartDialog).toHaveCount(0);
  await expect(restart).toBeFocused();

  await page.keyboard.press("Enter");
  await expect(page.getByRole("alertdialog")).toBeVisible();
  await page.getByRole("button", { name: "Confirm", exact: true }).press("Enter");
  await expect(page.getByText("Restart OpenEvo runtime", { exact: true }).first()).toBeVisible();
  await expect(page.locator(".system-activity .state-pill")).toHaveText("Complete");

  const cleanup = page.getByRole("button", { name: "Clean diagnostic history", exact: true });
  await cleanup.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("alertdialog")).toContainText("Project inputs");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
  await expect(cleanup).toBeFocused();

  await assertViewportSafety(page);
  const accessibility = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  const blockingViolations = accessibility.violations.filter(
    (violation) => violation.impact === "serious" || violation.impact === "critical",
  );
  expect(blockingViolations, JSON.stringify(blockingViolations, null, 2)).toEqual([]);
});

test("System remains reachable at the minimum width and a constrained window height", async ({
  page,
}) => {
  await page.setViewportSize({ width: 760, height: 180 });
  await page.getByRole("button", { name: "Research", exact: true }).click();

  const system = page.getByRole("button", { name: "System", exact: true });
  await system.scrollIntoViewIfNeeded();
  await expect(system).toBeVisible();
  await system.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("system-workspace")).toBeVisible();

  await assertViewportSafety(page);
});

async function assertViewportSafety(page: import("@playwright/test").Page): Promise<void> {
  const interactiveSelector =
    "button, a[href], input, select, textarea, [role='tab'], [tabindex]";
  const result = await page.evaluate(() => {
    const viewportWidth = window.innerWidth;
    const documentOverflow =
      Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) - viewportWidth;
    const clippedControls = Array.from(document.querySelectorAll<HTMLElement>(
      "button, a[href], input, select, textarea, [role='tab'], [tabindex]",
    ))
      .filter((element) => {
        const style = window.getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.visibility !== "hidden"
          && style.display !== "none"
          && rect.width > 0
          && rect.height > 0
          && (rect.left < -1 || rect.right > viewportWidth + 1);
      })
      .map((element) => ({
        label: element.getAttribute("aria-label") ?? element.textContent?.trim().slice(0, 80) ?? "",
        rect: element.getBoundingClientRect().toJSON(),
      }));
    return { documentOverflow, clippedControls };
  });

  expect(result.documentOverflow, `document overflows by ${result.documentOverflow}px`).toBeLessThanOrEqual(1);
  expect(result.clippedControls, JSON.stringify(result.clippedControls, null, 2)).toEqual([]);

  const viewport = page.viewportSize();
  if (!viewport) throw new Error("The Playwright viewport is unavailable.");
  const controls = page.locator(interactiveSelector);
  for (let index = 0; index < await controls.count(); index += 1) {
    const control = controls.nth(index);
    if (!await control.isVisible()) continue;
    await control.scrollIntoViewIfNeeded();
    const box = await control.boundingBox();
    if (!box) continue;
    const label = await control.getAttribute("aria-label")
      ?? (await control.textContent())?.trim().slice(0, 80)
      ?? `control ${index}`;
    expect(box.y, `${label} is clipped above the viewport`).toBeGreaterThanOrEqual(-1);
    expect(
      box.y + box.height,
      `${label} is clipped below the viewport`,
    ).toBeLessThanOrEqual(viewport.height + 1);
    const exposed = await control.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const hit = document.elementFromPoint(
        Math.min(window.innerWidth - 1, Math.max(0, rect.left + rect.width / 2)),
        Math.min(window.innerHeight - 1, Math.max(0, rect.top + rect.height / 2)),
      );
      return hit === element || (hit !== null && element.contains(hit));
    });
    expect(exposed, `${label} is vertically obscured by another element`).toBe(true);
  }
}
