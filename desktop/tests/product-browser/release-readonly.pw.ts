import AxeBuilder from "@axe-core/playwright";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Route } from "@playwright/test";

const DESKTOP_ENDPOINT = "http://127.0.0.1:43117";
const DESKTOP_SESSION_TOKEN = "release-readonly-session-token-000000000001";
const OPENAPI_SHA256 = "26ee1e2b6b25f3297c5c09544a9a10ce95baae233ac4b3de2dc0f72cc32ad3cb";
const FEATURE_FLAGS = [
  "remote_profiles",
  "project_validation",
  "operation_events",
  "run_observability",
  "artifact_inspection",
  "service_control",
  "diagnostics",
  "maintenance",
] as const;
const DESKTOP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const EXPECTED_PROJECTS = new Set([
  "release-packaged-1440",
  "release-packaged-1024",
  "release-packaged-760",
]);

type HarnessObservation = {
  nativeCalls: Array<{ command: string; args: Record<string, unknown> }>;
  httpCalls: Array<{ method: string; path: string; authenticated: boolean }>;
  unexpectedCalls: string[];
};

test("first launch uses the release sidecar composition and keeps demo navigation non-mutating", async ({
  page,
}, testInfo) => {
  const sidecarObservation: Pick<HarnessObservation, "httpCalls" | "unexpectedCalls"> = {
    httpCalls: [],
    unexpectedCalls: [],
  };
  const externalNetworkCalls: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (
      (url.protocol === "http:" || url.protocol === "https:")
      && url.origin !== "http://127.0.0.1:4176"
      && url.origin !== DESKTOP_ENDPOINT
    ) {
      externalNetworkCalls.push(request.url());
    }
  });
  expect(EXPECTED_PROJECTS.has(testInfo.project.name)).toBe(true);
  await installReleaseNativeContract(page);
  await installReleaseSidecarContract(page, sidecarObservation);

  await page.goto("/");
  await expect(page.getByTestId("sample-research-workspace")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Enzyme Kinetics Model Review" })).toBeVisible();
  await expect(page.locator(".product-shell")).toHaveAttribute(
    "data-provider-kind",
    "desktop_sidecar",
  );
  await expect(page.locator(".product-shell")).toHaveAttribute(
    "data-system-maintenance-available",
    "true",
  );
  await expect(page.getByText("Demo", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("Demo data · 12 observations", { exact: true })).toBeVisible();
  await expect(page.locator("body")).not.toContainText("contract_simulator");
  const addRemoteWorkspace = page.getByRole("button", {
    name: "Add remote workspace",
    exact: true,
  });
  await expect(addRemoteWorkspace).toBeVisible();
  await expect(addRemoteWorkspace).toHaveClass(/primary-button/);
  await addRemoteWorkspace.click();
  await expect(page.getByRole("dialog", { name: "Server connection" })).toBeVisible();
  await page.getByRole("button", { name: "Close connection settings", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Server connection" })).toBeHidden();
  const projectSelector = page.getByLabel("Project", { exact: true });
  await expect(projectSelector.locator("option")).toHaveCount(2);
  await projectSelector.selectOption({ label: "[Demo] Protein Stability Evidence Review" });
  await expect(page.getByRole("heading", { name: "Protein Stability Evidence Review" })).toBeVisible();
  await expect(page.getByText("48 DSF curves + 12 SEC summaries", { exact: false })).toBeVisible();
  await projectSelector.selectOption({ label: "[Demo] Enzyme Kinetics Model Review" });
  await expect(page.getByRole("heading", { name: "Enzyme Kinetics Model Review" })).toBeVisible();
  await assertViewportSafety(page, testInfo.project.name);
  await expect(page).toHaveScreenshot("release-packaged-research.png");
  let researchCoveredThrough = page.viewportSize()?.height ?? 0;
  if (testInfo.project.name === "release-packaged-760") {
    await page.evaluate(() => window.scrollTo(0, 600));
    await expect(page).toHaveScreenshot("release-packaged-research-middle-1.png");
    await page.evaluate(() => window.scrollTo(0, 1_200));
    await expect(page).toHaveScreenshot("release-packaged-research-middle-2.png");
    researchCoveredThrough = 1_800;
  }
  await scrollToPageEndWithoutCoverageGap(page, researchCoveredThrough);
  await expect(page.locator(".sample-trace-entry").last()).toBeVisible();
  await expect(page).toHaveScreenshot("release-packaged-research-detail.png");

  const selectedSession = page.locator('.sample-session-card[aria-selected="true"]');
  await selectedSession.focus();
  await selectedSession.press("ArrowLeft");
  await expect(page.getByRole("tab", { name: /Task 2/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );

  const evolution = page.getByRole("button", { name: "Evolution", exact: true });
  await evolution.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("sample-evolution-workspace")).toBeVisible();
  await expect(page.getByRole("heading", { name: "How OpenEvo improved this project" })).toBeVisible();
  await expect(page.getByText("Update components", { exact: true })).toBeVisible();
  const processTab = page.getByRole("tab", { name: "Evolution", exact: true });
  await processTab.focus();
  await processTab.press("ArrowRight");
  await expect(page.getByRole("tab", { name: "Output", exact: true })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.getByText("memory.md", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: /Trajectory to Skill/ }).click();
  await expect(page.getByText("Failed fit trajectory", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "Output", exact: true }).click();
  await expect(page.getByText("SKILL.md", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: /Agent System/ }).click();
  await expect(page.getByText("Rejected conclusion", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "Output", exact: true }).click();
  await expect(page.getByText("AGENTS.md", { exact: true })).toBeVisible();
  const agentSystemArtifact = page.getByText("AGENTS.md", { exact: true });
  await agentSystemArtifact.scrollIntoViewIfNeeded();
  await assertViewportSafety(page, testInfo.project.name);
  await page.evaluate(() => window.scrollTo(0, 0));
  await expect(page.getByRole("heading", { name: "How OpenEvo improved this project" })).toBeInViewport();
  await expect(page).toHaveScreenshot("release-packaged-evolution.png");
  let evolutionCoveredThrough = page.viewportSize()?.height ?? 0;
  if (testInfo.project.name === "release-packaged-760") {
    await page.evaluate(() => window.scrollTo(0, 600));
    await expect(page).toHaveScreenshot("release-packaged-evolution-middle.png");
    evolutionCoveredThrough = 1_200;
  }
  await scrollToPageEndWithoutCoverageGap(page, evolutionCoveredThrough);
  await expect(page.locator(".sample-artifact-document pre")).toBeVisible();
  await expect(page).toHaveScreenshot("release-packaged-evolution-detail.png");

  const system = page.getByRole("button", { name: "System", exact: true });
  await system.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("sample-about-workspace")).toBeVisible();
  await expect(page.getByRole("heading", { name: "No remote workspace" })).toBeVisible();
  await expect(
    page.getByTestId("sample-about-workspace").getByRole("button", {
      name: "Add remote workspace",
      exact: true,
    }),
  ).toBeVisible();

  for (const name of [
    "Check",
    "Diagnostics",
    "Repair",
    "Restart OpenEvo runtime",
    "Clean diagnostic history",
  ]) {
    await expect(page.getByRole("button", { name, exact: true })).toHaveCount(0);
  }

  const connectedObservation = await readHarnessObservation(page);
  expect(connectedObservation.unexpectedCalls).toEqual([]);
  expect(sidecarObservation.unexpectedCalls).toEqual([]);
  expect(externalNetworkCalls).toEqual([]);
  expect(sidecarObservation.httpCalls).toEqual(expect.arrayContaining([
    { method: "GET", path: "/version", authenticated: false },
    { method: "GET", path: "/desktop/v1/state", authenticated: true },
    { method: "GET", path: "/desktop/v1/profiles?limit=100", authenticated: true },
    { method: "GET", path: "/desktop/v1/projects?limit=100", authenticated: true },
  ]));
  expect(sidecarObservation.httpCalls.every(({ method }) => method === "GET")).toBe(true);
  expect(connectedObservation.nativeCalls.some(({ command }) => command === "start_sidecar")).toBe(true);
  expect(connectedObservation.nativeCalls.some(({ command }) => command === "renderer_ready")).toBe(true);
  expect(connectedObservation.nativeCalls.every(({ command }) => [
    "start_sidecar",
    "stop_sidecar",
    "read_run_retry_recovery",
    "renderer_bootstrap_stage",
    "renderer_ready",
  ].includes(command))).toBe(true);

  await page.evaluate(() => window.scrollTo(0, 0));
  await assertViewportSafety(page, testInfo.project.name);
  await assertAccessibility(page);
  await expectPackagedReleaseAssets(page);

  // Tear down the connected renderer before establishing the failure-case
  // request boundary so an in-flight event poll cannot cross scenarios.
  await page.goto("about:blank");
  sidecarObservation.httpCalls.length = 0;
  sidecarObservation.unexpectedCalls.length = 0;
  await page.goto("/?native=readiness-failure");
  await expect(page.getByTestId("release-startup-sample")).toBeVisible();
  await expect(page.getByTestId("sample-research-workspace")).toBeVisible();
  await expect(page.getByText("OpenEvo Desktop could not start", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Add remote workspace", exact: true })).toBeVisible();
  const offlineProjectSelector = page.getByLabel("Project", { exact: true });
  await expect(offlineProjectSelector.locator("option")).toHaveCount(2);
  await offlineProjectSelector.selectOption({
    label: "[Demo] Protein Stability Evidence Review",
  });
  await expect(page.getByRole("heading", { name: "Protein Stability Evidence Review" })).toBeVisible();
  await offlineProjectSelector.selectOption({ label: "[Demo] Enzyme Kinetics Model Review" });
  await expect(page.locator("body")).not.toContainText("Renderer sample");
  await expect(page.locator("body")).not.toContainText("provider");
  await expect(page.locator(".product-shell")).not.toHaveAttribute(
    "data-provider-kind",
    "contract_simulator",
  );
  await assertViewportSafety(page, testInfo.project.name);

  await page.keyboard.press("Tab");
  const demoNavigation = page.getByLabel("Demo views");
  const researchTab = demoNavigation.getByRole("tab", { name: "Research", exact: true });
  await expect(researchTab).toBeFocused();
  await researchTab.press("ArrowDown");
  const evolutionTab = demoNavigation.getByRole("tab", { name: "Evolution", exact: true });
  await expect(evolutionTab).toBeFocused();
  await expect(evolutionTab).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("sample-evolution-workspace")).toBeVisible();

  const offlineProcessTab = page
    .getByTestId("sample-evolution-workspace")
    .getByRole("tab", { name: "Evolution", exact: true });
  await offlineProcessTab.focus();
  await offlineProcessTab.press("ArrowRight");
  await expect(page.getByRole("tab", { name: "Output", exact: true })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await assertViewportSafety(page, testInfo.project.name);

  await evolutionTab.focus();
  await evolutionTab.press("ArrowDown");
  const systemTab = demoNavigation.getByRole("tab", { name: "System", exact: true });
  await expect(systemTab).toBeFocused();
  await expect(systemTab).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("sample-about-workspace")).toBeVisible();
  await assertViewportSafety(page, testInfo.project.name);
  await systemTab.press("Home");
  await expect(researchTab).toBeFocused();
  await expect(researchTab).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("sample-research-workspace")).toBeVisible();

  for (const name of [
    "Add workspace",
    "Create project",
    "Check",
    "Diagnostics",
    "Repair",
    "Restart OpenEvo runtime",
    "Clean diagnostic history",
  ]) {
    await expect(page.getByRole("button", { name, exact: true })).toHaveCount(0);
  }

  const failedObservation = await readHarnessObservation(page);
  expect(sidecarObservation.httpCalls).toEqual([]);
  expect(sidecarObservation.unexpectedCalls).toEqual([]);
  expect(failedObservation.unexpectedCalls).toEqual([]);
  expect(externalNetworkCalls).toEqual([]);
  expect(failedObservation.nativeCalls.some(({ command }) => command === "start_sidecar")).toBe(true);
  expect(failedObservation.nativeCalls.some(({ command }) => command === "renderer_ready")).toBe(false);
  expect(failedObservation.nativeCalls.every(({ command }) => [
    "start_sidecar",
    "stop_sidecar",
    "renderer_bootstrap_stage",
  ].includes(command))).toBe(true);

  await page.evaluate(() => window.scrollTo(0, 0));
  await assertViewportSafety(page, testInfo.project.name);
  await assertAccessibility(page);
});

async function installReleaseNativeContract(page: Page): Promise<void> {
  await page.addInitScript((contract) => {
    const observation: HarnessObservation = {
      nativeCalls: [],
      httpCalls: [],
      unexpectedCalls: [],
    };
    Object.defineProperty(window, "__OPENEVO_RELEASE_READONLY__", {
      configurable: false,
      enumerable: false,
      writable: false,
      value: observation,
    });
    Object.defineProperty(window, "__TAURI_INTERNALS__", {
      configurable: false,
      enumerable: false,
      writable: false,
      value: {
        invoke: async (command: string, args: Record<string, unknown> = {}) => {
          observation.nativeCalls.push({ command, args });
          if (command === "stop_sidecar") return null;
          if (command === "read_run_retry_recovery") return null;
          if (command === "renderer_bootstrap_stage") {
            const allowedStages = new Set([
              "bootstrap_context_validated",
              "bootstrap_context_failed",
              "local_api_version_verified",
              "retry_recovery_ready",
              "provider_adapter_ready",
              "provider_created",
              "provider_create_failed",
              "product_committed",
            ]);
            if (!allowedStages.has(String(args.stage))) {
              observation.unexpectedCalls.push(`native stage ${String(args.stage)}`);
              throw new Error("Unexpected release bootstrap stage");
            }
            return null;
          }
          if (command === "renderer_ready") {
            if (args.openapiSha256 !== contract.openapiSha256) {
              observation.unexpectedCalls.push("renderer_ready digest");
              throw new Error("Renderer readiness digest mismatch");
            }
            return null;
          }
          if (command === "start_sidecar") {
            if (new URL(window.location.href).searchParams.get("native") === "readiness-failure") {
              throw new Error("Native sidecar readiness failed");
            }
            return {
              schema_version: "1",
              endpoint: contract.endpoint,
              session_token: contract.sessionToken,
              negotiated_contract: {
                major: 1,
                openapi_sha256: contract.openapiSha256,
                provider_kind: "desktop_sidecar",
                feature_flags: contract.featureFlags,
              },
            };
          }
          observation.unexpectedCalls.push(`native command ${command}`);
          throw new Error(`Unexpected Tauri command: ${command}`);
        },
      },
    });
  }, {
    endpoint: DESKTOP_ENDPOINT,
    sessionToken: DESKTOP_SESSION_TOKEN,
    openapiSha256: OPENAPI_SHA256,
    featureFlags: [...FEATURE_FLAGS],
  });
}

async function installReleaseSidecarContract(
  page: Page,
  observation: Pick<HarnessObservation, "httpCalls" | "unexpectedCalls">,
): Promise<void> {
  await page.route(`${DESKTOP_ENDPOINT}/**`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = `${url.pathname}${url.search}`;
    const method = request.method();
    const authenticated =
      request.headers()["x-openevo-desktop-session"] === DESKTOP_SESSION_TOKEN;
    observation.httpCalls.push({ method, path, authenticated });

    if (method !== "GET") return rejectUnexpectedRoute(route, observation, `${method} ${path}`);
    if (path === "/version") {
      if (authenticated) {
        return rejectUnexpectedRoute(route, observation, "authenticated /version");
      }
      return json(route, {
        schema_version: "1",
        api_name: "openevo-desktop-local-api",
        preferred_major: 1,
        supported_majors: [1],
        openapi_sha256: OPENAPI_SHA256,
        build_version: "0.1.2",
        source_commit: "abcdef12",
        build_channel: "release",
        provider_kind: "desktop_sidecar",
        feature_flags: [...FEATURE_FLAGS],
      });
    }
    if (!authenticated) {
      return rejectUnexpectedRoute(route, observation, `unauthenticated ${path}`);
    }
    if (path === "/desktop/v1/state") {
      return json(route, disconnectedDesktopState());
    }
    if (
      path === "/desktop/v1/profiles?limit=100"
      || path === "/desktop/v1/projects?limit=100"
    ) {
      return json(route, {
        schema_version: "1",
        items: [],
        next_cursor: null,
        has_more: false,
      });
    }
    if (path === "/desktop/v1/events") {
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: ": release-readonly heartbeat\n\n",
      });
    }
    return rejectUnexpectedRoute(route, observation, `${method} ${path}`);
  });
}

function disconnectedDesktopState() {
  return {
    schema_version: "1",
    observed_at: "2026-07-19T12:00:00Z",
    contract: {
      selected_major: 1,
      desktop_openapi_sha256: OPENAPI_SHA256,
      core_openapi_sha256: null,
      compatible: true,
    },
    execution_mode_capabilities: {
      schema_version: "1",
      modes: [
        {
          mode: "codex_subscription_transcript",
          display_name: "Subscription",
          support_state: "supported",
          reason_code: null,
          message: "Available in this OpenEvo Desktop release.",
        },
        {
          mode: "self-deployed",
          display_name: "Self-deployed",
          support_state: "unavailable",
          reason_code: "self_deployed_release_unavailable",
          message: "Self-deployed execution is unavailable in this release.",
        },
      ],
    },
    core: {
      state: "disconnected",
      profile_id: null,
      active_tunnel: false,
      operation_id: null,
      host_key_review: null,
      core: null,
      failure: null,
    },
    active_project: null,
    pending_operation_ids: [],
  };
}

async function expectPackagedReleaseAssets(page: Page): Promise<void> {
  const manifest = JSON.parse(
    await readFile(resolve(DESKTOP_ROOT, "packaging/web/.openevo-product-web.json"), "utf8"),
  ) as {
    files: Array<{ path: string; sha256: string; byte_size: number }>;
  };
  const loadedPaths = await page.evaluate(() =>
    performance.getEntriesByType("resource")
      .map((entry) => new URL(entry.name).pathname.replace(/^\//, "")),
  );
  const scriptEntries = manifest.files.filter(({ path }) => path.endsWith(".js"));
  expect(scriptEntries.length).toBeGreaterThan(0);
  expect(scriptEntries.some(({ path }) => loadedPaths.includes(path))).toBe(true);

  for (const entry of manifest.files) {
    const bytes = await readFile(resolve(DESKTOP_ROOT, "packaging/web", entry.path));
    expect(bytes.byteLength, entry.path).toBe(entry.byte_size);
    expect(createHash("sha256").update(bytes).digest("hex"), entry.path).toBe(entry.sha256);
    if (entry.path.endsWith(".js")) {
      const source = bytes.toString("utf8");
      expect(source, entry.path).not.toContain("contract_simulator");
      expect(source, entry.path).not.toContain("FixtureDesktopProductProvider");
    }
  }
}

async function assertAccessibility(page: Page): Promise<void> {
  const accessibility = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  const blockingViolations = accessibility.violations.filter(
    (violation) => violation.impact === "serious" || violation.impact === "critical",
  );
  expect(blockingViolations, JSON.stringify(blockingViolations, null, 2)).toEqual([]);
}

async function scrollToPageEndWithoutCoverageGap(
  page: Page,
  coveredThrough: number,
): Promise<void> {
  const position = await page.evaluate(() => {
    window.scrollTo(0, document.documentElement.scrollHeight);
    return {
      scrollTop: window.scrollY,
      viewportHeight: window.innerHeight,
      scrollHeight: document.documentElement.scrollHeight,
    };
  });
  expect(position.scrollTop, "the final visual snapshot must overlap prior coverage")
    .toBeLessThanOrEqual(coveredThrough);
  expect(position.scrollTop + position.viewportHeight)
    .toBeGreaterThanOrEqual(position.scrollHeight - 1);
}

async function assertViewportSafety(page: Page, projectName: string): Promise<void> {
  const result = await page.evaluate((checkTextOcclusion) => {
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
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
    const obscuredText: Array<{ text: string; rect: DOMRect }> = [];
    if (checkTextOcclusion) {
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) {
        const node = walker.currentNode as Text;
        const text = node.data.trim();
        const owner = node.parentElement;
        if (!text || !owner) continue;
        const style = window.getComputedStyle(owner);
        if (style.display === "none" || style.visibility === "hidden") continue;
        const range = document.createRange();
        range.selectNodeContents(node);
        for (const rect of range.getClientRects()) {
          if (
            rect.width === 0
            || rect.height === 0
            || rect.top < 0
            || rect.bottom > viewportHeight
          ) {
            continue;
          }
          const x = Math.min(viewportWidth - 1, Math.max(0, rect.left + rect.width / 2));
          const y = Math.min(viewportHeight - 1, Math.max(0, rect.top + rect.height / 2));
          const hit = document.elementFromPoint(x, y);
          if (
            rect.left < -1
            || rect.right > viewportWidth + 1
            || !hit
            || (!owner.contains(hit) && !hit.contains(owner))
          ) {
            obscuredText.push({ text: text.slice(0, 80), rect: rect.toJSON() });
          }
        }
      }
    }
    return { documentOverflow, clippedControls, obscuredText };
  }, projectName === "release-packaged-760");

  expect(result.documentOverflow, `document overflows by ${result.documentOverflow}px`).toBeLessThanOrEqual(1);
  expect(result.clippedControls, JSON.stringify(result.clippedControls, null, 2)).toEqual([]);
  expect(result.obscuredText, JSON.stringify(result.obscuredText, null, 2)).toEqual([]);
}

async function readHarnessObservation(page: Page): Promise<HarnessObservation> {
  return page.evaluate(() => {
    const observation = (window as typeof window & {
      __OPENEVO_RELEASE_READONLY__: HarnessObservation;
    }).__OPENEVO_RELEASE_READONLY__;
    return observation;
  });
}

async function rejectUnexpectedRoute(
  route: Route,
  observation: Pick<HarnessObservation, "unexpectedCalls">,
  label: string,
): Promise<void> {
  observation.unexpectedCalls.push(`sidecar route ${label}`);
  await route.abort("blockedbyclient");
}

async function json(route: Route, payload: unknown): Promise<void> {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(payload),
  });
}
