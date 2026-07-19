import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.goto("/product-preview.html?scenario=new-user");
  await expect(page.getByTestId("sample-research-workspace")).toBeVisible();
});

test("first-run sample is accessible, keyboard-operable, and viewport-safe", async ({
  page,
}) => {
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(page.getByTestId("sample-research-workspace")).toHaveAttribute("lang", "zh-CN");
  await expect(page.getByRole("heading", { name: "酶动力学模型复核" })).toBeVisible();
  await expect(page.getByText("Project Head", { exact: true })).toBeVisible();
  await expect(page.getByText("Evolution Revision", { exact: true })).toBeVisible();
  await assertViewportSafety(page);
  await assertAccessibility(page);
  await expect(page).toHaveScreenshot("scientific-project-research.png", {
    fullPage: true,
  });

  const selectedSession = page.locator('.sample-session-card[aria-selected="true"]');
  await selectedSession.focus();
  await selectedSession.press("ArrowLeft");
  await expect(page.getByRole("tab", { name: /Task 2/ })).toHaveAttribute("aria-selected", "true");

  await page.getByRole("button", { name: "Evolution" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("sample-evolution-workspace")).toBeVisible();
  await expect(page.getByText("Evolution Revision ER-3", { exact: false }).first()).toBeVisible();

  const processTab = page.getByRole("tab", { name: /演化过程/ });
  await processTab.focus();
  await processTab.press("ArrowRight");
  await expect(page.getByRole("tab", { name: /可读产物/ })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByText("memory.md", { exact: true })).toBeVisible();

  await assertViewportSafety(page);
  await assertAccessibility(page);
  await page.evaluate(() => window.scrollTo(0, 0));
  await expect(page).toHaveScreenshot("scientific-project-evolution.png", {
    fullPage: true,
  });
});

async function assertAccessibility(page: import("@playwright/test").Page): Promise<void> {
  const accessibility = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  const blockingViolations = accessibility.violations.filter(
    (violation) => violation.impact === "serious" || violation.impact === "critical",
  );
  expect(blockingViolations, JSON.stringify(blockingViolations, null, 2)).toEqual([]);
}

async function assertViewportSafety(page: import("@playwright/test").Page): Promise<void> {
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
}
