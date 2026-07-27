import AxeBuilder from "@axe-core/playwright";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Route } from "@playwright/test";

const DESKTOP_ENDPOINT = "http://127.0.0.1:43117";
const DESKTOP_SESSION_TOKEN = "release-readonly-session-token-000000000001";
const OPENAPI_SHA256 = "4cd120dab0797e223ba892b0382fd61f8e4156318df9ab6676236c201191a98a";
const EVENT_SCHEMA_SHA256 = "515b6d90e9ebdf3f5b4f7c4a57a1924dc85011536d9396b1ab3a5dc73fc48b6b";
const FEATURE_SET_SHA256 = "67b6ad24f67de611f32c365079fcf8384c800d0855effaa64e1ff24251a7acda";
const FEATURE_FLAGS = [
  "core_control_v2",
  "daemon_bundle_v2",
  "event_replay_v2",
  "host_key_review",
  "lifecycle_operations_v2",
  "lifecycle_process_logs_v2",
  "mutation_idempotency_v2",
  "native_askpass",
  "system_openssh_profiles",
  "task_admission_v2",
] as const;
const DESKTOP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const EXPECTED_PROJECTS = new Set([
  "release-packaged-1440",
  "release-packaged-1024",
  "release-packaged-760",
]);
const LIFECYCLE_OPERATION_ID = "release-pending-connect-1";
const LIFECYCLE_REQUEST_SHA256 = "d".repeat(64);
const LIFECYCLE_ETAG = `"${"e".repeat(64)}"`;

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
  await expect(page.locator(".product-shell")).toHaveAttribute("data-api-version", "2");
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
  const remoteDialog = page.getByRole("dialog", { name: "Configured SSH host" });
  await expect(remoteDialog).toBeVisible();
  await expect(remoteDialog.locator("select")).toHaveValue("gpu-lab");
  await expect(remoteDialog).not.toContainText(/server address|user name|private key|password/i);
  await page.getByRole("button", { name: "Close remote workspace setup", exact: true }).click();
  await expect(remoteDialog).toBeHidden();
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
    { method: "GET", path: "/desktop/v2/state", authenticated: true },
    { method: "GET", path: "/desktop/v2/ssh-hosts", authenticated: true },
    { method: "GET", path: "/desktop/v2/profiles?limit=100", authenticated: true },
    { method: "GET", path: "/desktop/v2/events", authenticated: true },
  ]));
  expect(sidecarObservation.httpCalls.every(({ method }) => method === "GET")).toBe(true);
  expect(
    connectedObservation.nativeCalls.some(({ command }) => command === "begin_sidecar_start"),
  ).toBe(true);
  expect(
    connectedObservation.nativeCalls.some(({ command }) => command === "sidecar_bootstrap_context"),
  ).toBe(true);
  expect(connectedObservation.nativeCalls.some(({ command }) => command === "start_sidecar")).toBe(
    false,
  );
  expect(connectedObservation.nativeCalls.some(({ command }) => command === "renderer_ready")).toBe(true);
  expect(connectedObservation.nativeCalls.every(({ command }) => [
    "begin_sidecar_start",
    "sidecar_bootstrap_context",
    "sidecar_startup_status",
    "stop_sidecar",
    "renderer_bootstrap_stage",
    "renderer_ready",
    "read_mutation_intent_journal_v2",
    "compare_and_swap_mutation_intent_journal_v2",
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
    "Repair",
    "Restart OpenEvo runtime",
    "Clean diagnostic history",
  ]) {
    await expect(page.getByRole("button", { name, exact: true })).toHaveCount(0);
  }

  const startupDiagnostics = page.getByRole("button", { name: "Diagnostics", exact: true });
  await expect(startupDiagnostics).toBeVisible();
  await expect(startupDiagnostics).toHaveAttribute("aria-expanded", "false");
  await startupDiagnostics.click();
  await expect(startupDiagnostics).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByRole("button", { name: "View logs", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Reveal in Finder", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Export diagnostics", exact: true })).toBeVisible();

  const failedObservation = await readHarnessObservation(page);
  expect(sidecarObservation.httpCalls).toEqual([]);
  expect(sidecarObservation.unexpectedCalls).toEqual([]);
  expect(failedObservation.unexpectedCalls).toEqual([]);
  expect(externalNetworkCalls).toEqual([]);
  expect(
    failedObservation.nativeCalls.some(({ command }) => command === "begin_sidecar_start"),
  ).toBe(true);
  expect(
    failedObservation.nativeCalls.some(({ command }) => command === "sidecar_bootstrap_context"),
  ).toBe(true);
  expect(failedObservation.nativeCalls.some(({ command }) => command === "start_sidecar")).toBe(
    false,
  );
  expect(failedObservation.nativeCalls.some(({ command }) => command === "renderer_ready")).toBe(false);
  expect(failedObservation.nativeCalls.every(({ command }) => [
    "begin_sidecar_start",
    "sidecar_bootstrap_context",
    "sidecar_startup_status",
    "stop_sidecar",
    "renderer_bootstrap_stage",
    "read_mutation_intent_journal_v2",
    "compare_and_swap_mutation_intent_journal_v2",
  ].includes(command))).toBe(true);

  await page.evaluate(() => window.scrollTo(0, 0));
  await assertViewportSafety(page, testInfo.project.name);
  await assertAccessibility(page);
});

test("the packaged release recovers pending lifecycle progress and process logs", async ({ page }) => {
  const observation: Pick<HarnessObservation, "httpCalls" | "unexpectedCalls"> = {
    httpCalls: [],
    unexpectedCalls: [],
  };
  await installReleaseNativeContract(page);
  await installReleaseSidecarContract(page, observation, { lifecycleOperation: true });

  await page.goto("/");
  const panel = page.locator(".lifecycle-operation-panel").filter({
    has: page.getByRole("heading", { name: "Connect remote workspace", exact: true }),
  });
  await expect(panel).toBeVisible();
  await expect(panel.getByText("Checking remote server requirements", { exact: true }).last()).toBeVisible();
  await expect(panel.getByText("Checkpoint 6 of 17", { exact: true })).toBeVisible();
  await expect(panel.getByText("3 of 8 items", { exact: true })).toBeVisible();
  await expect(panel.getByText("SSH output", { exact: true })).toBeVisible();
  await expect(panel.getByText("Daemon output", { exact: true })).toBeVisible();
  await expect(panel.getByText("remote preflight accepted", { exact: true })).toBeVisible();
  await expect(panel.getByText("daemon bundle inventory verified", { exact: true })).toBeVisible();
  await assertViewportSafety(page, "release-lifecycle");
  await assertAccessibility(page);

  const nativeObservation = await readHarnessObservation(page);
  expect(nativeObservation.unexpectedCalls).toEqual([]);
  expect(observation.unexpectedCalls).toEqual([]);
  expect(observation.httpCalls).toEqual(expect.arrayContaining([
    { method: "GET", path: `/desktop/v2/operations/${LIFECYCLE_OPERATION_ID}`, authenticated: true },
    { method: "GET", path: `/desktop/v2/operations/${LIFECYCLE_OPERATION_ID}/logs?limit=100`, authenticated: true },
  ]));
  expect(observation.httpCalls.every(({ method }) => method === "GET")).toBe(true);
  await page.goto("about:blank");
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
          if (command === "sidecar_startup_status") {
            return {
              schema_version: "2",
              startup_epoch: 1,
              status: "succeeded",
              phase: "ready",
              phase_index: 5,
              phase_total: 6,
              elapsed_milliseconds: 750,
              cancellable: false,
              failure: null,
            };
          }
          if (command === "read_mutation_intent_journal_v2") return null;
          if (command === "compare_and_swap_mutation_intent_journal_v2") return null;
          if (command === "renderer_bootstrap_stage") {
            const allowedStages = new Set([
              "bootstrap_context_validated",
              "bootstrap_context_failed",
              "local_api_version_verified",
              "local_api_version_failed",
              "provider_adapter_ready",
              "provider_adapter_failed",
              "provider_created",
              "provider_create_failed",
              "initial_snapshot_failed",
              "product_committed",
            ]);
            if (!allowedStages.has(String(args.stage))) {
              observation.unexpectedCalls.push(`native stage ${String(args.stage)}`);
              throw new Error("Unexpected release bootstrap stage");
            }
            return null;
          }
          if (command === "renderer_ready") {
            if (
              args.openapiSha256 !== contract.openapiSha256
              || args.eventSchemaSha256 !== contract.eventSchemaSha256
              || args.releaseVersion !== "0.1.10"
            ) {
              observation.unexpectedCalls.push("renderer_ready digest");
              throw new Error("Renderer readiness digest mismatch");
            }
            return null;
          }
          if (command === "begin_sidecar_start") {
            if (new URL(window.location.href).searchParams.get("native") === "readiness-failure") {
              throw new Error("Native sidecar readiness failed");
            }
            return null;
          }
          if (command === "sidecar_bootstrap_context") {
            return {
              schema_version: "2",
              endpoint: contract.endpoint,
              session_token: contract.sessionToken,
              negotiated_contract: {
                major: 2,
                mutation_major: 2,
                openapi_sha256: contract.openapiSha256,
                event_schema_sha256: contract.eventSchemaSha256,
                release_version: "0.1.10",
                build_id: "a".repeat(64),
                source_commit: "abcdef1",
                provider_kind: "desktop_sidecar",
                build_channel: "release",
                feature_flags: contract.featureFlags,
                feature_set_sha256: contract.featureSetSha256,
                required_core_api_major: 2,
                mutation_compatible: true,
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
    eventSchemaSha256: EVENT_SCHEMA_SHA256,
    featureSetSha256: FEATURE_SET_SHA256,
    featureFlags: [...FEATURE_FLAGS],
  });
}

async function installReleaseSidecarContract(
  page: Page,
  observation: Pick<HarnessObservation, "httpCalls" | "unexpectedCalls">,
  options: { lifecycleOperation?: boolean } = {},
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
        schema_version: "2",
        api_name: "openevo-desktop-local-api",
        preferred_major: 2,
        supported_majors: [2],
        mutation_major: 2,
        openapi_sha256: OPENAPI_SHA256,
        event_schema_sha256: EVENT_SCHEMA_SHA256,
        release_version: "0.1.10",
        build_id: "a".repeat(64),
        source_commit: "abcdef1",
        build_channel: "release",
        provider_kind: "desktop_sidecar",
        feature_flags: [...FEATURE_FLAGS],
        feature_set_sha256: FEATURE_SET_SHA256,
        required_core_api_major: 2,
        mutation_compatible: true,
      });
    }
    if (!authenticated) {
      return rejectUnexpectedRoute(route, observation, `unauthenticated ${path}`);
    }
    if (path === "/desktop/v2/state") {
      return json(route, disconnectedDesktopState(options.lifecycleOperation === true));
    }
    if (path === "/desktop/v2/ssh-hosts") {
      return json(route, configuredSshHosts());
    }
    if (path === "/desktop/v2/profiles?limit=100") {
      return json(route, {
        schema_version: "2",
        items: [],
        next_cursor: null,
        has_more: false,
      });
    }
    if (path === "/desktop/v2/events") {
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: ": release-readonly heartbeat\n\n",
      });
    }
    if (options.lifecycleOperation === true
      && path === `/desktop/v2/operations/${LIFECYCLE_OPERATION_ID}`) {
      return json(route, pendingLifecycleOperation());
    }
    if (options.lifecycleOperation === true
      && path === `/desktop/v2/operations/${LIFECYCLE_OPERATION_ID}/logs?limit=100`) {
      return json(route, pendingLifecycleLogs());
    }
    return rejectUnexpectedRoute(route, observation, `${method} ${path}`);
  });
}

function disconnectedDesktopState(withLifecycleOperation = false) {
  return {
    schema_version: "2",
    profiles: [],
    active_profile_id: null,
    active_project_id: null,
    pending_operations: withLifecycleOperation ? [pendingLifecycleOperationReference()] : [],
    last_event_id: null,
    updated_at: "2026-07-19T12:00:00Z",
  };
}

function pendingLifecycleOperationReference() {
  return {
    schema_version: "2",
    operation_id: LIFECYCLE_OPERATION_ID,
    kind: "profile_connect",
    resource: { resource_kind: "profile", resource_id: "profile-release-lab" },
    request_sha256: LIFECYCLE_REQUEST_SHA256,
    status: "running",
    phase: "remote_preflight",
    phase_index: 5,
    phase_total: 17,
    log_sequence_high_watermark: 3,
    updated_at: "2026-07-27T08:00:03Z",
    etag: LIFECYCLE_ETAG,
  };
}

function pendingLifecycleOperation() {
  return {
    ...pendingLifecycleOperationReference(),
    progress: { kind: "items", completed: 3, total: 8 },
    cancellable: true,
    result: null,
    failure: null,
    created_at: "2026-07-27T08:00:00Z",
    started_at: "2026-07-27T08:00:01Z",
    finished_at: null,
  };
}

function pendingLifecycleLogs() {
  return {
    schema_version: "2",
    operation_id: LIFECYCLE_OPERATION_ID,
    dropped_before_sequence: 0,
    items: [
      {
        schema_version: "2",
        operation_id: LIFECYCLE_OPERATION_ID,
        sequence: 1,
        occurred_at: "2026-07-27T08:00:01Z",
        source: "ssh_stdout",
        text: "remote preflight accepted",
        truncated: false,
      },
      {
        schema_version: "2",
        operation_id: LIFECYCLE_OPERATION_ID,
        sequence: 2,
        occurred_at: "2026-07-27T08:00:02Z",
        source: "daemon_stdout",
        text: "daemon bundle inventory verified",
        truncated: false,
      },
      {
        schema_version: "2",
        operation_id: LIFECYCLE_OPERATION_ID,
        sequence: 3,
        occurred_at: "2026-07-27T08:00:03Z",
        source: "daemon_stderr",
        text: "verified registry is still warming",
        truncated: false,
      },
    ],
    next_cursor: null,
    has_more: false,
  };
}

function configuredSshHosts() {
  return {
    schema_version: "2",
    catalog_generation: 1,
    hosts: [{
      schema_version: "2",
      ssh_host_alias: "gpu-lab",
      availability: "selectable",
      source_kind: "literal_host",
    }],
    warnings: [],
    scanned_at: "2026-07-19T12:00:00Z",
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
