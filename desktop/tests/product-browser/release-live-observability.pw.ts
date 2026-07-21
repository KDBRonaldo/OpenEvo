import { createHash } from "node:crypto";
import {
  lstat,
  open,
  readFile,
  readdir,
  realpath,
} from "node:fs/promises";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import {
  expect,
  test,
  type ConsoleMessage,
  type Locator,
  type Page,
  type Request,
  type Response,
  type Route,
} from "@playwright/test";
import { z } from "zod";
import {
  artifactContentV1Schema,
  artifactDiffV1Schema,
  artifactV1Schema,
  desktopBootstrapContextV1Schema,
  logEntryV1Schema,
  timelineEntryV1Schema,
} from "../../src/api/v1/schemas";
import {
  drainPendingSnapshot,
  InFlightCaptureCutoff,
  selectLatestArtifactPredecessor,
} from "./release-live-capture";

const HANDOFF_ENV = "OPENEVO_DESKTOP_LIVE_RENDERER_HANDOFF";
const HANDOFF_PATH = process.env[HANDOFF_ENV];
const STATIC_ORIGIN = "http://tauri.localhost";
const MAX_HANDOFF_BYTES = 64 * 1024;
const MAX_CAPTURE_BYTES = 2 * 1024 * 1024;
const MAX_CAPTURE_TOTAL_BYTES = 16 * 1024 * 1024;
const MAX_CAPTURE_RESPONSES = 1_024;
const MAX_CAPTURE_ENTRIES = 4_096;
const MAX_RESULT_BYTES = 64 * 1024;
const RESPONSE_BODY_TIMEOUT_MS = 15_000;
const CAPTURE_CUTOFF_TIMEOUT_MS = 20_000;
const NETWORK_CUTOFF_TIMEOUT_MS = 25_000;
const DESKTOP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const REPOSITORY_PACKAGED_WEB_ROOT = resolve(DESKTOP_ROOT, "packaging/web");
const REQUIRED_PHASES = [
  "admission",
  "preparation",
  "execution",
  "evolution",
  "revision",
  "terminal",
] as const;
const PHASE_ORDER = [
  "admission",
  "preparation",
  "execution",
  "capture",
  "dataset",
  "evolution",
  "materialization",
  "revision",
  "terminal",
] as const;
const TARGETS = ["agent_system", "skill_bundle", "text_memory"] as const;
const SAMPLE_NAMES = ["酶动力学模型复核", "蛋白质稳定性证据整合"] as const;

const sha256Schema = z.string().regex(/^[0-9a-f]{64}$/);
const phaseSchema = z.enum(PHASE_ORDER);
const targetSchema = z.enum(TARGETS);
const privatePathSchema = z.string().min(1).max(4096).refine(isAbsolute);
const handoffSchema = z
  .object({
    schema_version: z.literal("1"),
    kind: z.literal("openevo_desktop_live_renderer_handoff"),
    bootstrap: desktopBootstrapContextV1Schema,
    expected: z
      .object({
        source_commit: z.string().min(1).max(128).refine(safeText),
        project_id: z.string().min(1).max(256).refine(safeText),
        project_name: z.string().min(1).max(256).refine(safeText),
        codex_model: z.string().min(1).max(256).refine(safeText),
        reasoning_effort: z.enum(["low", "medium", "high", "xhigh"]),
        method_ids: z.record(targetSchema, z.string().min(1).max(256).refine(safeText)),
        sessions: z
          .array(
            z
              .object({
                ordinal: z.number().int().min(1).max(2),
                run_id: z.string().min(1).max(256).refine(safeText),
                timeline_phase_values: z.array(phaseSchema).min(REQUIRED_PHASES.length).max(PHASE_ORDER.length),
                minimum_log_count: z.number().int().min(1).max(1_000_000),
              })
              .strict(),
          )
          .min(1)
          .max(2),
        project_head_generation: z.number().int().min(1).max(Number.MAX_SAFE_INTEGER),
        artifacts: z
          .array(
            z
              .object({
                artifact_id: z.string().min(1).max(256).refine(safeText),
                artifact_type: targetSchema,
                target_id: targetSchema,
                artifact_content_sha256: sha256Schema,
                runtime_document_sha256: sha256Schema,
              })
              .strict(),
          )
          .length(3),
      })
      .strict(),
    packaged_web_root: privatePathSchema,
    result_path: privatePathSchema,
    screenshot_path: privatePathSchema,
  })
  .strict()
  .superRefine((value, context) => {
    const sessions = value.expected.sessions;
    const expectedOrdinals = Array.from({ length: sessions.length }, (_, index) => index + 1);
    if (sessions.map(({ ordinal }) => ordinal).sort().join(",") !== expectedOrdinals.join(",")) {
      context.addIssue({ code: z.ZodIssueCode.custom, path: ["expected", "sessions"], message: "session ordinals" });
    }
    if (new Set(sessions.map(({ run_id }) => run_id)).size !== sessions.length) {
      context.addIssue({ code: z.ZodIssueCode.custom, path: ["expected", "sessions"], message: "session identities" });
    }
    if (new Set(Object.keys(value.expected.method_ids)).size !== TARGETS.length
      || TARGETS.some((target) => !(target in value.expected.method_ids))) {
      context.addIssue({ code: z.ZodIssueCode.custom, path: ["expected", "method_ids"], message: "method identities" });
    }
    for (const [index, session] of sessions.entries()) {
      if (new Set(session.timeline_phase_values).size !== session.timeline_phase_values.length) {
        context.addIssue({ code: z.ZodIssueCode.custom, path: ["expected", "sessions", index, "timeline_phase_values"], message: "duplicate phases" });
      }
      if (REQUIRED_PHASES.some((phase) => !session.timeline_phase_values.includes(phase))) {
        context.addIssue({ code: z.ZodIssueCode.custom, path: ["expected", "sessions", index, "timeline_phase_values"], message: "required phases" });
      }
    }
    const artifacts = value.expected.artifacts;
    if (
      new Set(artifacts.map(({ artifact_id }) => artifact_id)).size !== artifacts.length
      || new Set(artifacts.map(({ target_id }) => target_id)).size !== TARGETS.length
      || artifacts.some(({ artifact_type, target_id }) => artifact_type !== target_id)
    ) {
      context.addIssue({ code: z.ZodIssueCode.custom, path: ["expected", "artifacts"], message: "artifact identities" });
    }
    if (value.result_path === value.screenshot_path) {
      context.addIssue({ code: z.ZodIssueCode.custom, path: ["result_path"], message: "output paths" });
    }
  });

type LiveHandoff = z.infer<typeof handoffSchema>;
type Target = typeof TARGETS[number];
type Phase = typeof PHASE_ORDER[number];
type CapturedArtifact = z.infer<typeof artifactV1Schema>;
type CapturedContent = z.infer<typeof artifactContentV1Schema>;
type CapturedDiff = z.infer<typeof artifactDiffV1Schema>;
type CapturedLog = z.infer<typeof logEntryV1Schema>;
type CapturedTimeline = z.infer<typeof timelineEntryV1Schema>;

type CaptureState = {
  sourceCommit: string | null;
  timelines: Map<string, Map<string, CapturedTimeline>>;
  logs: Map<string, Map<string, CapturedLog>>;
  artifacts: Map<string, CapturedArtifact>;
  contents: Map<string, CapturedContent>;
  diffs: Map<string, CapturedDiff>;
  pending: Set<Promise<void>>;
  responses: string[];
  errors: string[];
  browserErrors: string[];
  networkErrors: string[];
  capturedBytes: number;
  responseCount: number;
};

type NativeObservation = {
  commands: string[];
  stages: string[];
  rendererReady: boolean;
  unexpected: string[];
};

const resultArtifactSchema = z
  .object({
    artifact_id_sha256: sha256Schema,
    artifact_type: targetSchema,
    target_id: targetSchema,
    document_count: z.number().int().min(1),
    total_utf8_bytes: z.number().int().min(1),
    content_sha256: sha256Schema,
    runtime_document_sha256: sha256Schema,
  })
  .strict();
const resultSchema = z
  .object({
    schema_version: z.literal("1"),
    kind: z.literal("openevo_desktop_live_renderer_observability"),
    outcome: z.literal("passed"),
    provider_kind: z.literal("desktop_sidecar"),
    source_commit: z.string().min(1).max(128).refine(safeText),
    packaged_web_build_digest: sha256Schema,
    renderer_ready: z.literal(true),
    builtin_sample_count: z.literal(2),
    project_id_sha256: sha256Schema,
    session_count: z.number().int().min(1).max(2),
    timeline: z
      .object({
        count: z.number().int().min(REQUIRED_PHASES.length),
        phase_values: z.array(phaseSchema).min(REQUIRED_PHASES.length).max(PHASE_ORDER.length),
      })
      .strict(),
    logs: z.object({ count: z.number().int().min(1) }).strict(),
    project_head_generation: z.number().int().min(1).max(Number.MAX_SAFE_INTEGER),
    independent_target_controls_verified: z.literal(true),
    remote_method_selection_verified: z.literal(true),
    artifacts: z.array(resultArtifactSchema).length(3),
    screenshot_sha256: sha256Schema,
  })
  .strict();

test.skip(!HANDOFF_PATH, `requires ${HANDOFF_ENV}`);

test("packaged renderer observes the live Desktop Local API state", async ({ page }) => {
  const handoff = await readPrivateHandoff(HANDOFF_PATH!);
  await assertOutputDoesNotExist(handoff.result_path);
  await assertOutputDoesNotExist(handoff.screenshot_path);
  const packaged = await loadPackagedWeb(handoff.packaged_web_root);
  const capture = createCaptureState();
  const liveOrigin = new URL(handoff.bootstrap.endpoint).origin;

  await installNativeBridge(page, handoff.bootstrap);
  const networkFreeze = await installReleaseNetworkFreeze(page);
  await installPackagedWebRoute(page, packaged);
  await installWebSocketGate(page, capture);
  await installNetworkGate(
    page,
    liveOrigin,
    handoff.bootstrap.session_token,
    handoff.expected.project_id,
    capture,
  );
  const networkObservation = observeNetwork(
    page,
    liveOrigin,
    handoff.bootstrap.session_token,
    handoff.expected.project_id,
    capture,
    networkFreeze.isFrozen,
  );
  const stopObservingResponses = observeResponses(page, liveOrigin, capture);

  await page.goto(`${STATIC_ORIGIN}/`);
  const startupTerminalStages = new Set([
    "bootstrap_context_failed",
    "local_api_version_failed",
    "retry_recovery_failed",
    "provider_adapter_failed",
    "provider_create_failed",
    "initial_snapshot_failed",
    "product_committed",
  ]);
  await expect.poll(async () => (
    (await readNativeObservation(page)).stages.some((stage) => startupTerminalStages.has(stage))
  ), { timeout: 90_000 }).toBe(true);
  const startupNative = await readNativeObservation(page);
  if (!startupNative.stages.includes("product_committed")) {
    await drainCapture(capture);
    throw new Error(
      `release provider startup failed; stages=${startupNative.stages.join(",")}; responses=${capture.responses.join(",")}; browser=${capture.browserErrors.join(",")}; network=${capture.networkErrors.join(",")}`,
    );
  }
  await expect(page.locator(".product-shell")).toHaveAttribute(
    "data-provider-kind",
    "desktop_sidecar",
  );
  await drainCapture(capture);
  assertClosed(capture.sourceCommit === handoff.expected.source_commit, "release source identity mismatch");

  const projectSelector = page.getByLabel("Project", { exact: true });
  await expect(projectSelector).toBeVisible();
  for (const sampleName of SAMPLE_NAMES) {
    await projectSelector.selectOption({ label: `[只读] ${sampleName}` });
    await expect(page.getByRole("heading", { name: sampleName })).toBeVisible();
  }
  const selectedRealProject = await page.evaluate(({ projectId, projectName }) => {
    const selector = document.querySelector<HTMLSelectElement>("#project-switcher");
    const option = Array.from(selector?.options ?? []).find(
      (candidate) => candidate.dataset.projectId === projectId,
    );
    if (!selector || !option || option.textContent?.trim() !== projectName) return false;
    selector.value = option.value;
    selector.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }, {
    projectId: handoff.expected.project_id,
    projectName: handoff.expected.project_name,
  });
  assertClosed(selectedRealProject, "expected live project is unavailable");

  await expect(page.getByTestId("research-workspace")).toBeVisible();
  await page.getByRole("button", { name: "Edit project", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Research configuration" })).toBeVisible();
  await expect(page.getByLabel("Codex model", { exact: true })).toHaveValue(handoff.expected.codex_model);
  await expect(page.getByRole("combobox", { name: "Reasoning effort", exact: true })).toHaveValue(
    handoff.expected.reasoning_effort,
  );
  for (const target of TARGETS) {
    const targetRow = page.locator(`.target-toggle[data-target-id="${target}"]`);
    await expect(targetRow.getByRole("switch")).toBeChecked();
    await expect(targetRow.getByRole("combobox")).toBeEnabled();
    await expect(targetRow.getByRole("combobox")).toHaveValue(handoff.expected.method_ids[target]);
    const optionValues = await targetRow.getByRole("option").evaluateAll(
      (options) => options.map((option) => (option as HTMLOptionElement).value),
    );
    assertClosed(optionValues.includes(handoff.expected.method_ids[target]), "selected remote method is absent from the rendered capability options");
  }
  const memoryRow = page.locator('.target-toggle[data-target-id="text_memory"]');
  await memoryRow.getByRole("switch").click();
  await expect(memoryRow.getByRole("switch")).not.toBeChecked();
  await expect(page.locator('.target-toggle[data-target-id="skill_bundle"]').getByRole("switch")).toBeChecked();
  await expect(page.locator('.target-toggle[data-target-id="agent_system"]').getByRole("switch")).toBeChecked();
  await memoryRow.getByRole("switch").click();
  await expect(memoryRow.getByRole("switch")).toBeChecked();
  await expect(memoryRow.getByRole("combobox")).toHaveValue(handoff.expected.method_ids.text_memory);
  await page.getByRole("button", { name: "Undo", exact: true }).click();
  await expect(memoryRow.getByRole("switch")).toBeChecked();
  await page.getByRole("button", { name: "Close settings", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Research configuration" })).toBeHidden();
  await expect(page.getByText("Latest session complete", { exact: true })).toBeVisible();
  const sessionRows = page.getByRole("table", { name: "Session history" }).getByRole("row");
  await expect(sessionRows).toHaveCount(handoff.expected.sessions.length + 1);
  for (const session of handoff.expected.sessions) {
    const sessionButton = page.getByRole("button", { name: `Session ${session.ordinal}`, exact: true });
    await expect(sessionButton).toBeVisible();
    await sessionButton.click();
    await expect(sessionButton).toHaveAttribute("aria-pressed", "true");
    await expect(page.locator(".active-run-panel h2")).toHaveText(`Session ${session.ordinal}`);
    await expect(page.locator(".run-timeline")).toBeVisible();
    await drainCapture(capture);
    await expect.poll(async () => {
      await drainCapture(capture);
      return capture.logs.get(session.run_id)?.size ?? 0;
    }, { timeout: 30_000, message: `session ${session.ordinal} output did not satisfy the expected lower bound` })
      .toBeGreaterThanOrEqual(session.minimum_log_count);
  }
  const latestSession = handoff.expected.sessions.at(-1)!;
  await expect(page.locator(".session-output-entry").first()).toBeVisible();
  await expect(page.locator(".session-output-message").first()).not.toHaveText("");
  await drainCapture(capture);

  await page.getByRole("button", { name: "Evolution", exact: true }).click();
  await expect(page.getByTestId("evolution-workspace")).toBeVisible();
  await expect(page.locator(".revision-node").filter({
    hasText: `Project Head ${handoff.expected.project_head_generation}`,
  })).toBeVisible();
  await expect(page.locator(".artifact-list-heading")).toContainText("3 selected");
  await expect(page.locator(".artifact-list-item")).toHaveCount(3);
  await drainCapture(capture);
  assertExpectedArtifactCollection(capture, handoff);

  for (const expectedArtifact of handoff.expected.artifacts) {
    const label = artifactLabel(expectedArtifact.target_id);
    await page.locator(".artifact-list")
      .getByRole("button", { name: new RegExp(`^${escapeRegex(label)}`) })
      .click();
    await expect(page.locator(".artifact-document")).toBeVisible();
    await expect(page.locator(".artifact-document")).not.toHaveText("");
    await expect.poll(async () => {
      await drainCapture(capture);
      return capture.contents.has(expectedArtifact.artifact_id);
    }, { message: "live artifact content was not observed by the renderer" }).toBe(true);
  }

  const prefetchedDiffArtifactIds = new Set<string>();
  const diffArtifacts = handoff.expected.artifacts.filter((candidate) => {
    const artifact = capture.artifacts.get(candidate.artifact_id);
    return artifact?.lineage.source_artifact_ids.some((id) => capture.artifacts.has(id));
  });
  assertClosed(
    diffArtifacts.length === handoff.expected.artifacts.length,
    "not every evolved artifact has a reachable predecessor",
  );
  for (const diffArtifact of diffArtifacts) {
    await page.locator(".artifact-list").getByRole("button", {
      name: new RegExp(`^${escapeRegex(artifactLabel(diffArtifact.target_id))}`),
    }).click();
    await page.getByRole("tab", { name: "Changes", exact: true }).click();
    await expect(page.locator(".diff-view")).toBeVisible();
    await expect.poll(async () => {
      await drainCapture(capture);
      return capture.diffs.has(diffArtifact.artifact_id);
    }, { message: "live artifact diff was not observed by the renderer" }).toBe(true);
    assertArtifactDiffCapture(capture, diffArtifact.artifact_id);
    prefetchedDiffArtifactIds.add(diffArtifact.artifact_id);
  }

  await drainCapture(capture);
  assertTimelineCapture(capture, handoff);
  assertExpectedArtifactCollection(capture, handoff);
  const latestLogs = capture.logs.get(latestSession.run_id);
  assertClosed(
    latestLogs !== undefined && latestLogs.size >= latestSession.minimum_log_count,
    "live session output did not satisfy the expected lower bound",
  );
  const renderedTimelines = new Map<string, Awaited<ReturnType<typeof readRenderedTimeline>>>();
  const renderedLogsByRun = new Map<string, Awaited<ReturnType<typeof readRenderedLogs>>>();
  const renderedContents = new Map<string, { captureJson: string; documents: readonly string[] }>();
  const renderedDiffs = new Map<string, Awaited<ReturnType<typeof readRenderedDiff>>>();

  await page.getByRole("button", { name: "Research", exact: true }).click();
  await expect(page.getByTestId("research-workspace")).toBeVisible();
  for (const session of handoff.expected.sessions) {
    const sessionButton = page.getByRole("button", { name: `Session ${session.ordinal}`, exact: true });
    await sessionButton.click();
    await expect(sessionButton).toHaveAttribute("aria-pressed", "true");
    await expect(page.locator(".active-run-panel h2")).toHaveText(`Session ${session.ordinal}`);
    await expect(page.locator(".run-timeline")).toBeVisible();
    const renderedTimeline = await readRenderedTimeline(page);
    renderedTimelines.set(session.run_id, renderedTimeline);
    assertRenderedTimelineCapture(capture, session, renderedTimeline);
    const stableLogs = capture.logs.get(session.run_id);
    assertClosed(
      stableLogs !== undefined && stableLogs.size >= session.minimum_log_count,
      `stable logs for session ${session.ordinal} are incomplete`,
    );
    await expect(page.locator(".session-output-state")).toContainText(`${stableLogs.size} records`);
    const expectedRenderedLogs = [...stableLogs.values()]
      .sort((left, right) => left.sequence - right.sequence || left.id.localeCompare(right.id))
      .slice(-200)
      .map((entry) => ({
        id: entry.id,
        sequence: entry.sequence,
        stream: entry.stream,
        level: entry.level,
        contentSha256: entry.content_sha256,
        runId: entry.run_id ?? "",
        occurredAt: entry.occurred_at,
        attemptId: entry.attempt_id ?? "",
        serviceId: entry.service_id,
        streamLabel: sessionStreamLabel(entry.stream),
        dateTime: entry.occurred_at,
        displayedTime: formatSessionTime(entry.occurred_at),
        message: entry.message,
      }));
    await expect(page.locator(".session-output-entry")).toHaveCount(expectedRenderedLogs.length);
    const renderedLogs = await readRenderedLogs(page);
    renderedLogsByRun.set(session.run_id, renderedLogs);
    assertClosed(
      JSON.stringify(renderedLogs) === JSON.stringify(expectedRenderedLogs),
      `renderer logs for session ${session.ordinal} differ from the stable response cutoff`,
    );
  }

  await page.getByRole("button", { name: "Evolution", exact: true }).click();
  await expect(page.getByTestId("evolution-workspace")).toBeVisible();
  await expect(page.locator(".revision-node").filter({
    hasText: `Project Head ${handoff.expected.project_head_generation}`,
  })).toBeVisible();
  await expect(page.locator(".artifact-list-heading")).toContainText("3 selected");
  await expect(page.locator(".artifact-list-item")).toHaveCount(3);
  for (const expectedArtifact of handoff.expected.artifacts) {
    await page.locator(".artifact-list").getByRole("button", {
      name: new RegExp(`^${escapeRegex(artifactLabel(expectedArtifact.target_id))}`),
    }).click();
    await page.getByRole("tab", { name: "Content", exact: true }).click();
    const stableContent = capture.contents.get(expectedArtifact.artifact_id);
    assertClosed(
      stableContent !== undefined && stableContent.documents.length > 0,
      "stable artifact content is missing",
    );
    const contentRoot = page.locator(".artifact-content-view");
    await expect(contentRoot).toHaveAttribute("data-artifact-id", stableContent.artifact_id);
    await expect(contentRoot).toHaveAttribute("data-artifact-type", stableContent.artifact_type);
    await expect(contentRoot).toHaveAttribute("data-artifact-content-sha256", stableContent.artifact_content_sha256);
    await expect(contentRoot).toHaveAttribute("data-total-documents", String(stableContent.total_documents));
    await expect(contentRoot).toHaveAttribute("data-total-utf8-bytes", String(stableContent.total_utf8_bytes));
    await expect(contentRoot).toHaveAttribute("data-returned-utf8-bytes", String(stableContent.returned_utf8_bytes));
    await expect(contentRoot).toHaveAttribute("data-truncated", String(stableContent.truncated));
    await expect(page.locator(".document-tabs [role=tab]")).toHaveCount(
      stableContent.documents.length > 1 ? stableContent.documents.length : 0,
    );
    const renderedDocuments: string[] = [];
    for (const [index, document] of stableContent.documents.entries()) {
      if (stableContent.documents.length > 1) {
        const tab = page.locator(`#artifact-document-tab-${index}`);
        await expect(tab).toHaveText(document.display_name);
        await expectArtifactDocumentIdentity(tab, document);
        await tab.click();
        await expect(tab).toHaveAttribute("aria-selected", "true");
      }
      await expect.poll(
        () => page.locator(".artifact-document").textContent(),
        { message: "renderer artifact content differs from the stable response cutoff" },
      ).toBe(document.content);
      const panel = page.locator(".artifact-document");
      await expectArtifactDocumentIdentity(panel, document);
      const rendered = await panel.textContent();
      assertClosed(rendered !== null, "renderer artifact content disappeared after verification");
      renderedDocuments.push(rendered);
    }
    renderedContents.set(expectedArtifact.artifact_id, {
      captureJson: JSON.stringify(stableContent),
      documents: renderedDocuments,
    });
  }
  for (const stableDiffArtifact of diffArtifacts) {
    await page.locator(".artifact-list").getByRole("button", {
      name: new RegExp(`^${escapeRegex(artifactLabel(stableDiffArtifact.target_id))}`),
    }).click();
    await page.getByRole("tab", { name: "Changes", exact: true }).click();
    const stableDiff = capture.diffs.get(stableDiffArtifact.artifact_id);
    assertClosed(stableDiff !== undefined, "stable artifact diff is missing");
    await expect(page.locator(".diff-view")).toBeVisible();
    const renderedDiff = await readRenderedDiff(page);
    renderedDiffs.set(stableDiffArtifact.artifact_id, renderedDiff);
    assertClosed(
      JSON.stringify(renderedDiff) === JSON.stringify(expectedRenderedDiff(stableDiff)),
      `renderer ${stableDiffArtifact.target_id} diff differs from the stable response cutoff`,
    );
  }

  await page.getByRole("button", { name: "Research", exact: true }).click();
  await expect(page.getByTestId("research-workspace")).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, 0));
  const screenshot = await page.screenshot({
    animations: "disabled",
    caret: "hide",
    fullPage: false,
    mask: [
      page.locator(".brief-body"),
      page.locator(".session-output-panel"),
      page.locator(".run-timeline"),
      page.locator(".source-chip"),
      page.locator(".workspace-heading h1"),
      page.locator(".workspace-heading p:not(.eyebrow)"),
    ],
    maskColor: "#d7dce2",
  });

  await networkFreeze.freeze();
  await stopObservingResponses();
  await networkObservation.settleFiniteRequests();
  await drainCapture(capture);
  assertTimelineCapture(capture, handoff);
  assertExpectedArtifactCollection(capture, handoff);
  for (const session of handoff.expected.sessions) {
    const renderedTimeline = renderedTimelines.get(session.run_id);
    const renderedLogs = renderedLogsByRun.get(session.run_id);
    assertClosed(renderedTimeline !== undefined && renderedLogs !== undefined, "renderer session evidence is incomplete");
    assertRenderedTimelineCapture(capture, session, renderedTimeline);
    assertRenderedLogCapture(capture, session, renderedLogs);
  }
  const finalLatestLogs = capture.logs.get(latestSession.run_id);
  assertClosed(
    finalLatestLogs !== undefined && finalLatestLogs.size >= latestSession.minimum_log_count,
    "stable live session output did not satisfy the expected lower bound",
  );
  const stableDiffArtifacts = handoff.expected.artifacts.filter((candidate) => {
    const artifact = capture.artifacts.get(candidate.artifact_id);
    return artifact?.lineage.source_artifact_ids.some((id) => capture.artifacts.has(id));
  });
  assertClosed(
    stableDiffArtifacts.length === handoff.expected.artifacts.length
    && stableDiffArtifacts.every((artifact) => prefetchedDiffArtifactIds.has(artifact.artifact_id)),
    "stable predecessor lineage was not observed before the response cutoff",
  );
  const artifactEvidence: Array<z.infer<typeof resultArtifactSchema>> = [];
  for (const expectedArtifact of handoff.expected.artifacts) {
    const rendered = renderedContents.get(expectedArtifact.artifact_id);
    const stableContent = capture.contents.get(expectedArtifact.artifact_id);
    assertClosed(rendered !== undefined && stableContent !== undefined, "stable artifact renderer evidence is incomplete");
    assertClosed(
      rendered.captureJson === JSON.stringify(stableContent),
      "artifact content changed between renderer observation and the response cutoff",
    );
    artifactEvidence.push(artifactEvidenceFromCapture(
      capture,
      expectedArtifact,
      rendered.documents,
    ));
  }
  for (const stableDiffArtifact of stableDiffArtifacts) {
    assertArtifactDiffCapture(capture, stableDiffArtifact.artifact_id);
    const stableDiff = capture.diffs.get(stableDiffArtifact.artifact_id);
    const renderedDiff = renderedDiffs.get(stableDiffArtifact.artifact_id);
    assertClosed(stableDiff !== undefined && renderedDiff !== undefined, "stable artifact diff evidence is incomplete");
    assertClosed(
      JSON.stringify(renderedDiff) === JSON.stringify(expectedRenderedDiff(stableDiff)),
      `renderer ${stableDiffArtifact.target_id} diff changed before the response cutoff`,
    );
  }

  const native = await readNativeObservation(page);
  assertClosed(native.rendererReady, "renderer readiness was not acknowledged");
  assertClosed(native.commands.includes("start_sidecar"), "native bootstrap was not invoked");
  assertClosed(native.stages.includes("product_committed"), "product commit was not reported");
  assertClosed(native.unexpected.length === 0, "renderer invoked an unsupported native command");
  const timelineEntries = handoff.expected.sessions.flatMap(
    (session) => [...(capture.timelines.get(session.run_id)?.values() ?? [])],
  );
  const timelinePhases = orderedPhases(timelineEntries.map((entry) => entry.phase));
  const result = resultSchema.parse({
    schema_version: "1",
    kind: "openevo_desktop_live_renderer_observability",
    outcome: "passed",
    provider_kind: "desktop_sidecar",
    source_commit: handoff.expected.source_commit,
    packaged_web_build_digest: packaged.buildDigest,
    renderer_ready: true,
    builtin_sample_count: SAMPLE_NAMES.length,
    project_id_sha256: sha256(Buffer.from(handoff.expected.project_id, "utf8")),
    session_count: handoff.expected.sessions.length,
    timeline: {
      count: timelineEntries.length,
      phase_values: timelinePhases,
    },
    logs: { count: finalLatestLogs.size },
    project_head_generation: handoff.expected.project_head_generation,
    independent_target_controls_verified: true,
    remote_method_selection_verified: true,
    artifacts: artifactEvidence,
    screenshot_sha256: sha256(screenshot),
  });

  await page.close();
  networkObservation.stop();
  assertClosed(capture.errors.length === 0, "a live response could not be captured");
  assertClosed(
    capture.browserErrors.length === 0,
    `renderer reported browser security/resource errors: ${capture.browserErrors.join(",")}`,
  );
  assertClosed(
    capture.networkErrors.length === 0,
    `renderer crossed the allowed network boundary: ${capture.networkErrors.join(",")}`,
  );
  await writeExclusive(handoff.screenshot_path, screenshot);
  const resultBytes = Buffer.from(`${JSON.stringify(result, null, 2)}\n`, "utf8");
  assertClosed(resultBytes.byteLength <= MAX_RESULT_BYTES, "renderer result exceeds its byte budget");
  await writeExclusive(handoff.result_path, resultBytes);
});

function safeText(value: string): boolean {
  return value.trim() === value && !/[\u0000-\u001f\u007f]/.test(value);
}

function assertClosed(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function sha256(bytes: Buffer | Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}

async function readPrivateHandoff(path: string): Promise<LiveHandoff> {
  assertClosed(isAbsolute(path), "live renderer handoff path must be absolute");
  const metadata = await lstat(path);
  assertClosed(metadata.isFile() && !metadata.isSymbolicLink(), "live renderer handoff is not a regular file");
  assertClosed(metadata.nlink === 1, "live renderer handoff has an unsafe link count");
  assertClosed((metadata.mode & 0o777) === 0o600, "live renderer handoff mode is not private");
  if (typeof process.getuid === "function") {
    assertClosed(metadata.uid === process.getuid(), "live renderer handoff owner mismatch");
  }
  assertClosed(metadata.size > 0 && metadata.size <= MAX_HANDOFF_BYTES, "live renderer handoff size is invalid");
  let payload: unknown;
  try {
    payload = JSON.parse(await readFile(path, "utf8"));
  } catch {
    throw new Error("live renderer handoff is not valid JSON");
  }
  const parsed = handoffSchema.safeParse(payload);
  assertClosed(
    parsed.success,
    `live renderer handoff does not match the closed contract: ${
      parsed.success
        ? "unknown"
        : parsed.error.issues.map((issue) => `${issue.path.join(".")}:${issue.message}`).join(";")
    }`,
  );
  return parsed.data;
}

async function assertOutputDoesNotExist(path: string): Promise<void> {
  try {
    await lstat(path);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return;
    throw new Error("live renderer output path could not be inspected");
  }
  throw new Error("live renderer output already exists");
}

async function writeExclusive(path: string, bytes: Buffer): Promise<void> {
  let handle;
  try {
    handle = await open(path, "wx", 0o600);
    await handle.chmod(0o600);
    await handle.writeFile(bytes);
    await handle.sync();
  } catch {
    throw new Error("live renderer output could not be written exclusively");
  } finally {
    await handle?.close();
  }
}

type PackagedWeb = {
  root: string;
  buildDigest: string;
  files: Map<string, { bytes: Buffer; contentType: string }>;
};

const manifestSchema = z
  .object({
    schema_version: z.literal("1"),
    build_digest: sha256Schema,
    files: z
      .array(
        z
          .object({
            path: z.string().min(1).max(512).refine(safeAssetPath),
            sha256: sha256Schema,
            byte_size: z.number().int().min(1).max(8 * 1024 * 1024),
          })
          .strict(),
      )
      .min(2)
      .max(128),
  })
  .strict();

async function loadPackagedWeb(requestedRoot: string): Promise<PackagedWeb> {
  const [requested, expected] = await Promise.all([
    realpath(requestedRoot),
    realpath(REPOSITORY_PACKAGED_WEB_ROOT),
  ]);
  assertClosed(requested === expected, "packaged renderer root is not the release asset root");
  const rootMetadata = await lstat(requested);
  assertClosed(rootMetadata.isDirectory() && !rootMetadata.isSymbolicLink(), "packaged renderer root is invalid");
  const manifestPath = resolve(requested, ".openevo-product-web.json");
  const manifestMetadata = await lstat(manifestPath);
  assertClosed(manifestMetadata.isFile() && !manifestMetadata.isSymbolicLink(), "packaged renderer manifest is invalid");
  let unknownManifest: unknown;
  try {
    unknownManifest = JSON.parse(await readFile(manifestPath, "utf8"));
  } catch {
    throw new Error("packaged renderer manifest is not valid JSON");
  }
  const parsed = manifestSchema.safeParse(unknownManifest);
  assertClosed(parsed.success, "packaged renderer manifest does not match the closed contract");
  const manifest = parsed.data;
  assertClosed(
    sha256(Buffer.from(JSON.stringify(manifest.files), "utf8")) === manifest.build_digest,
    "packaged renderer manifest digest mismatch",
  );
  const inventory = await assetInventory(requested);
  const expectedPaths = new Set([".openevo-product-web.json", ...manifest.files.map(({ path }) => path)]);
  assertClosed(
    inventory.length === expectedPaths.size && inventory.every((path) => expectedPaths.has(path)),
    "packaged renderer inventory differs from its manifest",
  );
  const files = new Map<string, { bytes: Buffer; contentType: string }>();
  for (const entry of manifest.files) {
    const path = resolve(requested, ...entry.path.split("/"));
    assertClosed(path.startsWith(`${requested}${sep}`), "packaged renderer asset escaped its root");
    const metadata = await lstat(path);
    assertClosed(metadata.isFile() && !metadata.isSymbolicLink(), "packaged renderer asset is invalid");
    const bytes = await readFile(path);
    assertClosed(
      bytes.byteLength === entry.byte_size && sha256(bytes) === entry.sha256,
      "packaged renderer asset identity mismatch",
    );
    files.set(entry.path, { bytes, contentType: contentType(entry.path) });
  }
  assertClosed(files.has("index.html"), "packaged renderer index is missing");
  return { root: requested, buildDigest: manifest.build_digest, files };
}

async function assetInventory(root: string): Promise<string[]> {
  const output: string[] = [];
  async function visit(directory: string): Promise<void> {
    for (const name of (await readdir(directory)).sort()) {
      const absolute = resolve(directory, name);
      const metadata = await lstat(absolute);
      assertClosed(!metadata.isSymbolicLink(), "packaged renderer contains a symbolic link");
      if (metadata.isDirectory()) {
        await visit(absolute);
      } else {
        assertClosed(metadata.isFile(), "packaged renderer contains a non-file asset");
        output.push(relative(root, absolute).split(sep).join("/"));
      }
    }
  }
  await visit(root);
  return output;
}

function safeAssetPath(path: string): boolean {
  return !path.startsWith("/")
    && !path.includes("\\")
    && !/[\u0000-\u001f\u007f]/.test(path)
    && path.split("/").every((segment) => segment !== "" && segment !== "." && segment !== "..");
}

function contentType(path: string): string {
  if (path.endsWith(".html")) return "text/html; charset=utf-8";
  if (path.endsWith(".css")) return "text/css; charset=utf-8";
  if (path.endsWith(".js")) return "text/javascript; charset=utf-8";
  if (path.endsWith(".json")) return "application/json; charset=utf-8";
  if (path.endsWith(".map")) return "application/json; charset=utf-8";
  return "text/plain; charset=utf-8";
}

async function installPackagedWebRoute(page: Page, packaged: PackagedWeb): Promise<void> {
  await page.route(`${STATIC_ORIGIN}/**`, async (route: Route) => {
    const request = route.request();
    if (request.method() !== "GET") {
      await route.abort("blockedbyclient");
      return;
    }
    const url = new URL(request.url());
    if (url.origin !== STATIC_ORIGIN) {
      await route.abort("blockedbyclient");
      return;
    }
    let path: string;
    try {
      path = decodeURIComponent(url.pathname.replace(/^\/+/, "")) || "index.html";
    } catch {
      await route.abort("blockedbyclient");
      return;
    }
    if (!safeAssetPath(path)) {
      await route.abort("blockedbyclient");
      return;
    }
    const asset = packaged.files.get(path);
    if (!asset) {
      await route.fulfill({ status: 404, contentType: "text/plain", body: "Not found" });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: asset.contentType,
      body: asset.bytes,
      headers: {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
      },
    });
  });
}

async function installNativeBridge(
  page: Page,
  bootstrap: LiveHandoff["bootstrap"],
): Promise<void> {
  await page.addInitScript((context) => {
    const observation: NativeObservation = {
      commands: [],
      stages: [],
      rendererReady: false,
      unexpected: [],
    };
    let recovery: string | null = null;
    Object.defineProperty(window, "__OPENEVO_LIVE_NATIVE_OBSERVATION__", {
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
          observation.commands.push(command);
          if (command === "start_sidecar") return context;
          // The packaged shell owns lifecycle calls. They terminate at this
          // no-op bridge and can never stop the live sidecar owned by Python.
          if (command === "stop_sidecar") return null;
          if (command === "read_run_retry_recovery") return recovery;
          if (command === "write_run_retry_recovery") {
            const value = args.value;
            const expectedValue = args.expectedValue;
            if (
              (value !== null && typeof value !== "string")
              || (expectedValue !== null && typeof expectedValue !== "string")
              || expectedValue !== recovery
            ) {
              observation.unexpected.push("retry recovery CAS");
              throw new Error("Native retry recovery CAS rejected");
            }
            recovery = value;
            return null;
          }
          if (command === "renderer_bootstrap_stage") {
            const stage = typeof args.stage === "string" ? args.stage : "";
            const allowed = new Set([
              "bootstrap_context_validated",
              "bootstrap_context_failed",
              "local_api_version_verified",
              "local_api_version_failed",
              "retry_recovery_ready",
              "retry_recovery_failed",
              "provider_adapter_ready",
              "provider_adapter_failed",
              "provider_created",
              "provider_create_failed",
              "initial_snapshot_failed",
              "product_committed",
            ]);
            if (!allowed.has(stage)) {
              observation.unexpected.push("bootstrap stage");
              throw new Error("Unexpected renderer bootstrap stage");
            }
            observation.stages.push(stage);
            return null;
          }
          if (command === "renderer_ready") {
            if (args.openapiSha256 !== context.negotiated_contract.openapi_sha256) {
              observation.unexpected.push("renderer digest");
              throw new Error("Renderer readiness digest mismatch");
            }
            observation.rendererReady = true;
            return null;
          }
          observation.unexpected.push("native command");
          throw new Error("Unexpected native command");
        },
      },
    });
  }, bootstrap);
}

function createCaptureState(): CaptureState {
  return {
    sourceCommit: null,
    timelines: new Map(),
    logs: new Map(),
    artifacts: new Map(),
    contents: new Map(),
    diffs: new Map(),
    pending: new Set(),
    responses: [],
    errors: [],
    browserErrors: [],
    networkErrors: [],
    capturedBytes: 0,
    responseCount: 0,
  };
}

async function installWebSocketGate(page: Page, capture: CaptureState): Promise<void> {
  await page.routeWebSocket(/.*/, async (socket) => {
    capture.networkErrors.push("blocked WebSocket connection");
    await socket.close({ code: 1008, reason: "Network access is disabled for release observation" });
  });
}

async function installReleaseNetworkFreeze(page: Page): Promise<{
  isFrozen: () => boolean;
  freeze: () => Promise<void>;
}> {
  let frozen = false;
  await page.addInitScript(() => {
    type ReleaseNetworkWindow = Window & {
      __openevoReleasePrepareNetworkFreeze?: () => void;
      __openevoReleaseAbortEventStreams?: () => void;
      __openevoReleaseNetworkState?: () => { frozen: boolean; activeEventStreams: number };
    };
    const releaseWindow = window as ReleaseNetworkWindow;
    const originalFetch = globalThis.fetch.bind(globalThis);
    const eventStreamControllers = new Set<AbortController>();
    let networkFrozen = false;
    globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      if (networkFrozen) return new Promise<Response>(() => undefined);
      const requestUrl = new URL(input instanceof Request ? input.url : String(input), globalThis.location.href);
      if (requestUrl.pathname !== "/desktop/v1/events") return originalFetch(input, init);
      const controller = new AbortController();
      const callerSignal = init?.signal ?? (input instanceof Request ? input.signal : null);
      const forwardAbort = () => controller.abort(callerSignal?.reason);
      eventStreamControllers.add(controller);
      const releaseController = () => {
        callerSignal?.removeEventListener("abort", forwardAbort);
        eventStreamControllers.delete(controller);
      };
      controller.signal.addEventListener("abort", releaseController, { once: true });
      if (callerSignal?.aborted) forwardAbort();
      else callerSignal?.addEventListener("abort", forwardAbort, { once: true });
      try {
        return await originalFetch(input, { ...init, signal: controller.signal });
      } catch (error) {
        releaseController();
        throw error;
      }
    };
    releaseWindow.__openevoReleasePrepareNetworkFreeze = () => {
      networkFrozen = true;
    };
    releaseWindow.__openevoReleaseAbortEventStreams = () => {
      for (const controller of eventStreamControllers) controller.abort("release evidence frozen");
    };
    releaseWindow.__openevoReleaseNetworkState = () => ({
      frozen: networkFrozen,
      activeEventStreams: eventStreamControllers.size,
    });
  });
  return {
    isFrozen: () => frozen,
    freeze: async () => {
      if (frozen) return;
      await page.evaluate(() => {
        const releaseWindow = window as Window & { __openevoReleasePrepareNetworkFreeze?: () => void };
        if (!releaseWindow.__openevoReleasePrepareNetworkFreeze) {
          throw new Error("release network freeze controller is unavailable");
        }
        releaseWindow.__openevoReleasePrepareNetworkFreeze();
      });
      frozen = true;
      await page.evaluate(() => {
        const releaseWindow = window as Window & { __openevoReleaseAbortEventStreams?: () => void };
        if (!releaseWindow.__openevoReleaseAbortEventStreams) {
          throw new Error("release event-stream abort controller is unavailable");
        }
        releaseWindow.__openevoReleaseAbortEventStreams();
      });
      await expect.poll(async () => page.evaluate(() => {
        const releaseWindow = window as Window & {
          __openevoReleaseNetworkState?: () => { frozen: boolean; activeEventStreams: number };
        };
        return releaseWindow.__openevoReleaseNetworkState?.() ?? null;
      }), { timeout: 5_000, message: "renderer event stream did not quiesce at the evidence cutoff" })
        .toEqual({ frozen: true, activeEventStreams: 0 });
    },
  };
}

async function installNetworkGate(
  page: Page,
  liveOrigin: string,
  sessionToken: string,
  projectId: string,
  capture: CaptureState,
): Promise<void> {
  const validationPath = `/desktop/v1/projects/${encodeURIComponent(projectId)}/validate`;
  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const headers = request.headers();
    if (url.origin === STATIC_ORIGIN) {
      if (headers["x-openevo-desktop-session"]) {
        capture.networkErrors.push("credential on packaged asset request");
        await route.abort("blockedbyclient");
        return;
      }
      await route.fallback();
      return;
    }
    if (url.origin !== liveOrigin) {
      capture.networkErrors.push("blocked external origin");
      await route.abort("blockedbyclient");
      return;
    }
    if (request.method() === "OPTIONS") {
      const requestedMethod = headers["access-control-request-method"];
      const requestedHeaders = new Set((headers["access-control-request-headers"] ?? "")
        .toLowerCase().split(",").map((value) => value.trim()).filter(Boolean));
      const allowedGet = requestedMethod === "GET"
        && url.pathname.startsWith("/desktop/v1/")
        && requestedHeaders.has("x-openevo-desktop-session");
      const allowedValidation = requestedMethod === "POST"
        && url.pathname === validationPath
        && ["x-openevo-desktop-session", "idempotency-key", "if-match"]
          .every((header) => requestedHeaders.has(header));
      if (!allowedGet && !allowedValidation) {
        capture.networkErrors.push("blocked Local API preflight");
        await route.abort("blockedbyclient");
        return;
      }
      await route.continue();
      return;
    }
    if (request.method() === "GET" && url.pathname === "/version" && !headers["x-openevo-desktop-session"]) {
      await route.continue();
      return;
    }
    const authenticated = headers["x-openevo-desktop-session"] === sessionToken;
    const allowedGet = request.method() === "GET"
      && url.pathname.startsWith("/desktop/v1/")
      && authenticated;
    const allowedValidation = request.method() === "POST"
      && url.pathname === validationPath
      && authenticated
      && Boolean(headers["idempotency-key"])
      && Boolean(headers["if-match"]);
    if (!allowedGet && !allowedValidation) {
      capture.networkErrors.push("blocked Local API request");
      await route.abort("blockedbyclient");
      return;
    }
    await route.continue();
  });
}

function observeNetwork(
  page: Page,
  liveOrigin: string,
  sessionToken: string,
  projectId: string,
  capture: CaptureState,
  isFrozen: () => boolean,
): { settleFiniteRequests: () => Promise<void>; stop: () => void } {
  const cutoff = new InFlightCaptureCutoff<Request>();
  const isFiniteObservedRequest = (request: Request) => {
    const url = new URL(request.url());
    return [STATIC_ORIGIN, liveOrigin].includes(url.origin)
      && !(url.origin === liveOrigin && url.pathname === "/desktop/v1/events");
  };
  const requestListener = (request: Request) => {
    if (isFiniteObservedRequest(request)) cutoff.begin(request);
    const url = new URL(request.url());
    if (url.origin === STATIC_ORIGIN) return;
    if (url.origin !== liveOrigin) {
      capture.networkErrors.push("external origin");
      return;
    }
    if (request.method() === "OPTIONS") {
      if (request.headers()["access-control-request-private-network"] === "true") {
        capture.networkErrors.push("private-network preflight");
      }
      const requestedMethod = request.headers()["access-control-request-method"];
      const requestedHeaders = new Set((request.headers()["access-control-request-headers"] ?? "")
        .toLowerCase().split(",").map((value) => value.trim()).filter(Boolean));
      const expectedValidationPath = `/desktop/v1/projects/${encodeURIComponent(projectId)}/validate`;
      const allowedGet = requestedMethod === "GET"
        && url.pathname.startsWith("/desktop/v1/")
        && requestedHeaders.has("x-openevo-desktop-session");
      const allowedValidation = requestedMethod === "POST"
        && url.pathname === expectedValidationPath
        && ["x-openevo-desktop-session", "idempotency-key", "if-match"]
          .every((header) => requestedHeaders.has(header));
      if (!allowedGet && !allowedValidation) {
        capture.networkErrors.push("invalid Local API preflight");
      }
      return;
    }
    const authenticated = request.headers()["x-openevo-desktop-session"] === sessionToken;
    if (request.method() === "POST") {
      const expectedValidationPath = `/desktop/v1/projects/${encodeURIComponent(projectId)}/validate`;
      if (
        url.pathname !== expectedValidationPath
        || !authenticated
        || !request.headers()["idempotency-key"]
        || !request.headers()["if-match"]
      ) {
        capture.networkErrors.push("unexpected Local API mutation");
      }
      return;
    }
    if (request.method() !== "GET") {
      capture.networkErrors.push("unexpected Local API method");
      return;
    }
    if (url.pathname === "/version") {
      if (authenticated) capture.networkErrors.push("authenticated version request");
      return;
    }
    if (!url.pathname.startsWith("/desktop/v1/") || !authenticated) {
      capture.networkErrors.push("invalid Local API request");
    }
  };
  const requestFailedListener = (request: Request) => {
    const url = new URL(request.url());
    const expectedEventStreamAbort = isFrozen()
      && url.origin === liveOrigin
      && url.pathname === "/desktop/v1/events";
    if ([STATIC_ORIGIN, liveOrigin].includes(url.origin) && !expectedEventStreamAbort) {
      capture.networkErrors.push(`${request.method()} ${url.pathname} ${request.failure()?.errorText ?? "failed"}`);
    }
    cutoff.finish(request);
  };
  const responseListener = (response: Response) => {
    const url = new URL(response.url());
    if (
      [STATIC_ORIGIN, liveOrigin].includes(url.origin)
      && response.status() >= 400
    ) {
      capture.networkErrors.push(`${response.request().method()} ${url.pathname} ${response.status()}`);
    }
  };
  const requestFinishedListener = (request: Request) => cutoff.finish(request);
  const consoleListener = (message: ConsoleMessage) => {
    if (message.type() !== "error" && message.type() !== "warning") return;
    const value = message.text();
    if (value.includes("more-private address space") || value.includes("Private Network Access")) {
      capture.browserErrors.push("private-network access blocked");
    } else if (value.includes("CORS policy")) {
      capture.browserErrors.push("CORS policy blocked request");
    } else if (value.includes("Failed to load resource")) {
      capture.browserErrors.push("resource load failed");
    } else if (message.type() === "error") {
      capture.browserErrors.push("console error");
    }
  };
  const pageErrorListener = () => capture.browserErrors.push("unhandled page error");
  const crashListener = () => capture.browserErrors.push("renderer page crashed");
  page.on("request", requestListener);
  page.on("requestfailed", requestFailedListener);
  page.on("requestfinished", requestFinishedListener);
  page.on("response", responseListener);
  page.on("console", consoleListener);
  page.on("pageerror", pageErrorListener);
  page.on("crash", crashListener);
  return {
    settleFiniteRequests: async () => {
      const closing = cutoff.close(NETWORK_CUTOFF_TIMEOUT_MS);
      page.off("request", requestListener);
      const unresolved = await closing;
      if (unresolved.length > 0) {
        capture.networkErrors.push(`network cutoff timed out: ${unresolved.map(requestLabel).join(",")}`);
      }
    },
    stop: () => {
      page.off("request", requestListener);
      page.off("requestfailed", requestFailedListener);
      page.off("requestfinished", requestFinishedListener);
      page.off("response", responseListener);
      page.off("console", consoleListener);
      page.off("pageerror", pageErrorListener);
      page.off("crash", crashListener);
    },
  };
}

function requestLabel(request: Request): string {
  const url = new URL(request.url());
  return `${request.method()} ${url.pathname}`;
}

async function withDeadline<T>(promise: Promise<T>, timeoutMs: number, message: string): Promise<T> {
  let timeout: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      promise,
      new Promise<T>((_, reject) => {
        timeout = setTimeout(() => reject(new Error(message)), timeoutMs);
      }),
    ]);
  } finally {
    if (timeout !== undefined) clearTimeout(timeout);
  }
}

function observeResponses(
  page: Page,
  liveOrigin: string,
  capture: CaptureState,
): () => Promise<void> {
  const cutoff = new InFlightCaptureCutoff<Request>();
  const responded = new Set<Request>();
  const observes = (request: Request) => {
    const url = new URL(request.url());
    return url.origin === liveOrigin
      && url.pathname !== "/desktop/v1/events"
      && request.method() !== "OPTIONS";
  };
  const requestListener = (request: Request) => {
    if (observes(request)) cutoff.begin(request);
  };
  const responseListener = (response: Response) => {
    const request = response.request();
    if (!cutoff.accepts(request)) return;
    responded.add(request);
    const url = new URL(response.url());
    let pending: Promise<void>;
    pending = withDeadline(
      (async () => {
        if (capture.responses.length >= MAX_CAPTURE_RESPONSES) {
          throw new Error("live Local API response inventory exceeded the capture budget");
        }
        capture.responses.push(`${request.method()} ${url.pathname} ${response.status()}`);
        await captureResponse(response, capture, () => cutoff.accepts(request));
      })(),
      RESPONSE_BODY_TIMEOUT_MS,
      `response body capture timed out for ${requestLabel(request)}`,
    )
      .catch((error: unknown) => {
        capture.errors.push(`${url.pathname}:${error instanceof Error ? error.message : "response capture"}`);
      })
      .finally(() => {
        capture.pending.delete(pending);
        cutoff.finish(request);
      });
    capture.pending.add(pending);
  };
  const requestFinishedListener = (request: Request) => {
    if (!cutoff.accepts(request) || responded.has(request)) return;
    capture.errors.push(`captured request finished without a response: ${requestLabel(request)}`);
    cutoff.finish(request);
  };
  const requestFailedListener = (request: Request) => {
    if (!cutoff.accepts(request)) return;
    capture.errors.push(`captured request failed: ${requestLabel(request)}`);
    cutoff.finish(request);
  };
  page.on("request", requestListener);
  page.on("response", responseListener);
  page.on("requestfinished", requestFinishedListener);
  page.on("requestfailed", requestFailedListener);
  return async () => {
    const closing = cutoff.close(CAPTURE_CUTOFF_TIMEOUT_MS);
    page.off("request", requestListener);
    const unresolved = await closing;
    if (unresolved.length > 0) {
      capture.errors.push(`response cutoff timed out: ${unresolved.map(requestLabel).join(",")}`);
    }
    page.off("response", responseListener);
    page.off("requestfinished", requestFinishedListener);
    page.off("requestfailed", requestFailedListener);
  };
}

async function captureResponse(
  response: Response,
  capture: CaptureState,
  isAccepted: () => boolean,
): Promise<void> {
  assertClosed(response.ok(), "live Local API returned a non-success response");
  const contentLength = response.headers()["content-length"];
  assertClosed(contentLength !== undefined && /^\d+$/.test(contentLength), "live Local API omitted a bounded response length");
  const declaredBytes = Number(contentLength);
  assertClosed(Number.isSafeInteger(declaredBytes) && declaredBytes <= MAX_CAPTURE_BYTES, "live Local API response exceeded the capture budget");
  assertClosed(capture.responseCount < MAX_CAPTURE_RESPONSES, "live Local API response count exceeded the capture budget");
  assertClosed(capture.capturedBytes + declaredBytes <= MAX_CAPTURE_TOTAL_BYTES, "live Local API aggregate response bytes exceeded the capture budget");
  capture.responseCount += 1;
  capture.capturedBytes += declaredBytes;
  const bytes = await response.body();
  assertClosed(isAccepted(), "live Local API response completed after its capture deadline");
  assertClosed(bytes.byteLength === declaredBytes, "live Local API response length changed during capture");
  const payload = JSON.parse(bytes.toString("utf8")) as unknown;
  const url = new URL(response.url());
  if (url.pathname === "/version") {
    const object = record(payload);
    capture.sourceCommit = textField(object, "source_commit");
    return;
  }
  const timeline = resourceMatch(url.pathname, "timeline");
  if (timeline !== null) {
    const runEntries = capture.timelines.get(timeline) ?? new Map<string, CapturedTimeline>();
    for (const item of pageItems(payload)) {
      const entry = timelineEntryV1Schema.parse(item);
      assertClosed(entry.run_id === timeline, "timeline entry route identity mismatch");
      runEntries.set(entry.id, entry);
    }
    assertClosed(runEntries.size <= MAX_CAPTURE_ENTRIES, "live timeline exceeded the capture budget");
    capture.timelines.set(timeline, runEntries);
    return;
  }
  const logs = resourceMatch(url.pathname, "logs");
  if (logs !== null) {
    const runLogs = capture.logs.get(logs) ?? new Map<string, CapturedLog>();
    for (const item of pageItems(payload)) {
      const entry = logEntryV1Schema.parse(item);
      assertClosed(entry.run_id === logs, "log entry route identity mismatch");
      runLogs.set(entry.id, entry);
    }
    assertClosed(runLogs.size <= MAX_CAPTURE_ENTRIES, "live logs exceeded the capture budget");
    capture.logs.set(logs, runLogs);
    return;
  }
  const runArtifacts = resourceMatch(url.pathname, "artifacts");
  if (runArtifacts !== null) {
    for (const item of pageItems(payload)) captureArtifact(item, capture);
    assertClosed(capture.artifacts.size <= MAX_CAPTURE_ENTRIES, "live artifacts exceeded the capture budget");
    return;
  }
  const artifactContent = artifactResourceMatch(url.pathname, "content");
  if (artifactContent !== null) {
    const content = artifactContentV1Schema.parse(payload);
    assertClosed(content.artifact_id === artifactContent, "artifact content route identity mismatch");
    capture.contents.set(content.artifact_id, content);
    assertClosed(capture.contents.size <= MAX_CAPTURE_ENTRIES, "live artifact content exceeded the capture budget");
    return;
  }
  const artifactDiff = artifactResourceMatch(url.pathname, "diff");
  if (artifactDiff !== null) {
    const diff = artifactDiffV1Schema.parse(payload);
    assertClosed(diff.artifact_id === artifactDiff, "artifact diff route identity mismatch");
    capture.diffs.set(artifactDiff, diff);
    assertClosed(capture.diffs.size <= MAX_CAPTURE_ENTRIES, "live artifact diffs exceeded the capture budget");
  }
}

function captureArtifact(value: unknown, capture: CaptureState): void {
  const captured = artifactV1Schema.parse(value);
  capture.artifacts.set(captured.id, captured);
}

async function drainCapture(capture: CaptureState): Promise<void> {
  await drainPendingSnapshot(capture.pending);
  assertClosed(capture.errors.length === 0, "live Local API response capture failed");
}

function resourceMatch(path: string, collection: "timeline" | "logs" | "artifacts"): string | null {
  const match = new RegExp(`^/desktop/v1/runs/([^/]+)/${collection}$`).exec(path);
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]!);
  } catch {
    throw new Error("live Local API route identity is invalid");
  }
}

function artifactResourceMatch(path: string, collection: "content" | "diff"): string | null {
  const match = new RegExp(`^/desktop/v1/artifacts/([^/]+)/${collection}$`).exec(path);
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]!);
  } catch {
    throw new Error("live artifact route identity is invalid");
  }
}

function record(value: unknown): Record<string, unknown> {
  assertClosed(value !== null && typeof value === "object" && !Array.isArray(value), "captured payload is not an object");
  return value as Record<string, unknown>;
}

function pageItems(value: unknown): Record<string, unknown>[] {
  return arrayField(record(value), "items").map(record);
}

function arrayField(value: Record<string, unknown>, key: string): unknown[] {
  const field = value[key];
  assertClosed(Array.isArray(field), "captured payload has an invalid collection");
  return field;
}

function textField(value: Record<string, unknown>, key: string): string {
  const field = value[key];
  assertClosed(typeof field === "string" && safeText(field), "captured payload has invalid text");
  return field;
}

function assertTimelineCapture(capture: CaptureState, handoff: LiveHandoff): void {
  for (const session of handoff.expected.sessions) {
    const phases = capture.timelines.get(session.run_id);
    assertClosed(phases !== undefined && phases.size > 0, "expected live timeline was not observed");
    const actual = orderedPhases([...phases.values()].map((entry) => entry.phase));
    const expected = orderedPhases(session.timeline_phase_values);
    assertClosed(
      actual.length === expected.length && actual.every((phase, index) => phase === expected[index]),
      "live timeline phases differ from the expected workflow",
    );
    assertClosed(
      REQUIRED_PHASES.every((phase) => phasesHas(phases, phase)),
      "live timeline is missing a required phase",
    );
  }
}

async function readRenderedTimeline(page: Page) {
  return page.locator(".run-timeline li").evaluateAll((entries) => entries.map((entry) => ({
    id: (entry as HTMLElement).dataset.timelineId,
    sequence: Number((entry as HTMLElement).dataset.sequence),
    phase: (entry as HTMLElement).dataset.phase,
    status: (entry as HTMLElement).dataset.status,
    contentSha256: (entry as HTMLElement).dataset.contentSha256,
    attemptId: (entry as HTMLElement).dataset.attemptId,
    serviceId: (entry as HTMLElement).dataset.serviceId,
    occurredAt: (entry as HTMLElement).dataset.occurredAt,
    artifactIds: (entry as HTMLElement).dataset.artifactIds,
    error: (entry as HTMLElement).dataset.error,
    title: entry.querySelector("strong")?.textContent ?? "",
    message: entry.querySelector("div > span")?.textContent ?? "",
  })));
}

async function readRenderedLogs(page: Page) {
  return page.locator(".session-output-entry").evaluateAll((entries) => entries.map((entry) => ({
    id: (entry as HTMLElement).dataset.logId,
    sequence: Number((entry as HTMLElement).dataset.sequence),
    stream: (entry as HTMLElement).dataset.stream,
    level: (entry as HTMLElement).dataset.level,
    contentSha256: (entry as HTMLElement).dataset.contentSha256,
    runId: (entry as HTMLElement).dataset.runId,
    occurredAt: (entry as HTMLElement).dataset.occurredAt,
    attemptId: (entry as HTMLElement).dataset.attemptId,
    serviceId: (entry as HTMLElement).dataset.serviceId,
    streamLabel: entry.querySelector(".session-stream")?.textContent ?? "",
    dateTime: entry.querySelector("time")?.getAttribute("datetime") ?? "",
    displayedTime: entry.querySelector("time")?.textContent ?? "",
    message: entry.querySelector(".session-output-message")?.textContent ?? "",
  })));
}

function assertRenderedTimelineCapture(
  capture: CaptureState,
  session: LiveHandoff["expected"]["sessions"][number],
  rendered: ReadonlyArray<{
    id: string | undefined;
    sequence: number;
    phase: string | undefined;
    status: string | undefined;
    contentSha256: string | undefined;
    attemptId: string | undefined;
    serviceId: string | undefined;
    occurredAt: string | undefined;
    artifactIds: string | undefined;
    error: string | undefined;
    title: string;
    message: string;
  }>,
): void {
  const expected = [...(capture.timelines.get(session.run_id)?.entries() ?? [])]
    .sort(([leftId, left], [rightId, right]) => (
      left.sequence - right.sequence || leftId.localeCompare(rightId)
    ))
    .map(([, entry]) => ({
      id: entry.id,
      sequence: entry.sequence,
      phase: entry.phase,
      status: entry.status,
      contentSha256: entry.content_sha256,
      attemptId: entry.attempt_id ?? "",
      serviceId: entry.service_id,
      occurredAt: entry.occurred_at,
      artifactIds: JSON.stringify(entry.artifact_ids),
      error: JSON.stringify(entry.error),
      title: entry.title,
      message: entry.message,
    }));
  assertClosed(
    JSON.stringify(rendered) === JSON.stringify(expected),
    `renderer timeline for session ${session.ordinal} does not follow the authoritative remote sequence`,
  );
}

function assertRenderedLogCapture(
  capture: CaptureState,
  session: LiveHandoff["expected"]["sessions"][number],
  rendered: Awaited<ReturnType<typeof readRenderedLogs>>,
): void {
  const expected = [...(capture.logs.get(session.run_id)?.values() ?? [])]
    .sort((left, right) => left.sequence - right.sequence || left.id.localeCompare(right.id))
    .slice(-200)
    .map((entry) => ({
      id: entry.id,
      sequence: entry.sequence,
      stream: entry.stream,
      level: entry.level,
      contentSha256: entry.content_sha256,
      runId: entry.run_id ?? "",
      occurredAt: entry.occurred_at,
      attemptId: entry.attempt_id ?? "",
      serviceId: entry.service_id,
      streamLabel: sessionStreamLabel(entry.stream),
      dateTime: entry.occurred_at,
      displayedTime: formatSessionTime(entry.occurred_at),
      message: entry.message,
    }));
  assertClosed(
    JSON.stringify(rendered) === JSON.stringify(expected),
    `renderer logs for session ${session.ordinal} differ from the stable response cutoff`,
  );
}

function phasesHas(phases: Map<string, CapturedTimeline>, phase: typeof REQUIRED_PHASES[number]): boolean {
  return [...phases.values()].some((entry) => entry.phase === phase);
}

function orderedPhases(phases: readonly Phase[]): Phase[] {
  const observed = new Set(phases);
  return PHASE_ORDER.filter((phase) => observed.has(phase));
}

function assertExpectedArtifactCollection(capture: CaptureState, handoff: LiveHandoff): void {
  for (const expected of handoff.expected.artifacts) {
    const artifact = capture.artifacts.get(expected.artifact_id);
    assertClosed(
      artifact !== undefined
      && artifact.artifact_type === expected.artifact_type
      && artifact.target_id === expected.target_id,
      "expected selected artifact was not observed",
    );
  }
}

async function expectArtifactDocumentIdentity(
  locator: Locator,
  document: CapturedContent["documents"][number],
): Promise<void> {
  await expect(locator).toHaveAttribute("data-document-id", document.document_id);
  await expect(locator).toHaveAttribute("data-display-name", document.display_name);
  await expect(locator).toHaveAttribute("data-relative-path", document.relative_path ?? "");
  await expect(locator).toHaveAttribute("data-mime-type", document.mime_type);
  await expect(locator).toHaveAttribute("data-content-sha256", document.content_sha256);
  await expect(locator).toHaveAttribute("data-byte-size", String(document.byte_size));
  await expect(locator).toHaveAttribute("data-truncated", String(document.truncated));
}

function artifactEvidenceFromCapture(
  capture: CaptureState,
  expected: LiveHandoff["expected"]["artifacts"][number],
  renderedDocuments: readonly string[],
): z.infer<typeof resultArtifactSchema> {
  const content = capture.contents.get(expected.artifact_id);
  const artifact = capture.artifacts.get(expected.artifact_id);
  assertClosed(content !== undefined && artifact !== undefined, "artifact observation is incomplete");
  assertClosed(
    content.artifact_id === expected.artifact_id
    && content.artifact_type === expected.artifact_type
    && content.artifact_content_sha256 === artifact.content_sha256
    && artifact.content_sha256 === expected.artifact_content_sha256,
    "artifact content identity mismatch",
  );
  assertClosed(
    !content.truncated
    && content.documents.length > 0
    && content.documents.length === content.total_documents
    && content.returned_utf8_bytes === content.total_utf8_bytes
    && content.returned_utf8_bytes > 0,
    "artifact content is empty or incomplete",
  );
  for (const document of content.documents) {
    const bytes = Buffer.from(document.content, "utf8");
    assertClosed(
      !document.truncated
      && bytes.byteLength === document.byte_size
      && sha256(bytes) === document.content_sha256,
      "artifact document content identity mismatch",
    );
  }
  const runtimeDocuments = expected.target_id === "skill_bundle"
    ? content.documents.filter((document) => document.relative_path === "SKILL.md")
    : content.documents;
  assertClosed(
    runtimeDocuments.length === 1
    && runtimeDocuments[0]?.content_sha256 === expected.runtime_document_sha256,
    "artifact runtime document changed after workflow verification",
  );
  assertClosed(
    renderedDocuments.length === content.documents.length
    && renderedDocuments.every((rendered, index) => (
      rendered === content.documents[index]?.content && rendered.trim().length > 0
    )),
    "artifact documents were not rendered from the stable response cutoff",
  );
  return {
    artifact_id_sha256: sha256(Buffer.from(expected.artifact_id, "utf8")),
    artifact_type: expected.artifact_type,
    target_id: expected.target_id,
    document_count: content.documents.length,
    total_utf8_bytes: content.total_utf8_bytes,
    content_sha256: artifact.content_sha256,
    runtime_document_sha256: runtimeDocuments[0]!.content_sha256,
  };
}

async function readRenderedDiff(page: Page) {
  return page.locator(".diff-view").evaluate((root) => {
    const attribute = (element: Element, name: string) => element.getAttribute(name) ?? "";
    const integer = (element: Element, name: string) => {
      const value = attribute(element, name);
      if (!/^\d+$/.test(value)) throw new Error(`Rendered diff omitted ${name}`);
      return Number(value);
    };
    const nullableInteger = (element: Element, name: string) => {
      const value = attribute(element, name);
      if (value === "") return null;
      if (!/^\d+$/.test(value)) throw new Error(`Rendered diff contains invalid ${name}`);
      return Number(value);
    };
    return {
      artifactId: attribute(root, "data-artifact-id"),
      artifactContentSha256: attribute(root, "data-artifact-content-sha256"),
      previousArtifactId: attribute(root, "data-previous-artifact-id"),
      previousArtifactContentSha256: attribute(root, "data-previous-artifact-content-sha256"),
      truncated: attribute(root, "data-truncated") === "true",
      changes: [...root.querySelectorAll(":scope > .diff-hunk")].map((change) => ({
        kind: attribute(change, "data-kind"),
        oldDocumentId: attribute(change, "data-old-document-id"),
        oldPath: attribute(change, "data-old-path"),
        oldContentSha256: attribute(change, "data-old-content-sha256"),
        newDocumentId: attribute(change, "data-new-document-id"),
        newPath: attribute(change, "data-new-path"),
        newContentSha256: attribute(change, "data-new-content-sha256"),
        heading: change.querySelector(".diff-document-heading h3")?.textContent ?? "",
        emptyMessage: change.querySelector(".diff-document-empty")?.textContent ?? null,
        hunks: [...change.querySelectorAll(":scope > .diff-hunk-block")].map((hunk) => ({
          oldStart: integer(hunk, "data-old-start"),
          oldCount: integer(hunk, "data-old-count"),
          newStart: integer(hunk, "data-new-start"),
          newCount: integer(hunk, "data-new-count"),
          lines: [...hunk.querySelectorAll(":scope > .diff-line")].map((line) => ({
            kind: attribute(line, "data-kind"),
            oldLineNumber: nullableInteger(line, "data-old-line-number"),
            newLineNumber: nullableInteger(line, "data-new-line-number"),
            text: line.querySelector("code")?.textContent ?? "",
          })),
        })),
      })),
    };
  });
}

function expectedRenderedDiff(diff: CapturedDiff) {
  return {
    artifactId: diff.artifact_id,
    artifactContentSha256: diff.artifact_content_sha256,
    previousArtifactId: diff.previous_artifact_id,
    previousArtifactContentSha256: diff.previous_artifact_content_sha256,
    truncated: diff.truncated,
    changes: diff.document_changes.map((change) => {
      const oldDocument = "old_document" in change ? change.old_document : null;
      const newDocument = "new_document" in change ? change.new_document : null;
      const oldPath = oldDocument?.relative_path ?? null;
      const newPath = newDocument?.relative_path ?? null;
      const heading = change.kind === "renamed" ? `${oldPath} to ${newPath}` : (newPath ?? oldPath ?? "");
      const emptyMessage = change.hunks.length > 0
        ? null
        : change.kind === "renamed"
          ? "Renamed without content changes."
          : change.kind === "added"
            ? "Empty document added."
            : change.kind === "removed"
              ? "Empty document removed."
              : "Content identity changed without line changes.";
      return {
        kind: change.kind,
        oldDocumentId: oldDocument?.document_id ?? "",
        oldPath: oldDocument?.relative_path ?? "",
        oldContentSha256: oldDocument?.content_sha256 ?? "",
        newDocumentId: newDocument?.document_id ?? "",
        newPath: newDocument?.relative_path ?? "",
        newContentSha256: newDocument?.content_sha256 ?? "",
        heading,
        emptyMessage,
        hunks: change.hunks.map((hunk) => ({
          oldStart: hunk.old_start,
          oldCount: hunk.old_count,
          newStart: hunk.new_start,
          newCount: hunk.new_count,
          lines: hunk.lines.map((line) => ({
            kind: line.kind,
            oldLineNumber: line.old_line_number,
            newLineNumber: line.new_line_number,
            text: line.text,
          })),
        })),
      };
    }),
  };
}

function assertArtifactDiffCapture(capture: CaptureState, artifactId: string): void {
  const diff = capture.diffs.get(artifactId);
  const artifact = capture.artifacts.get(artifactId);
  assertClosed(diff !== undefined && artifact !== undefined, "artifact changes are incomplete");
  const previous = expectedDiffPredecessor(capture, artifact);
  const hunks = diff.document_changes.flatMap((change) => change.hunks);
  const lines = hunks.flatMap((hunk) => hunk.lines);
  assertClosed(
    diff.artifact_id === artifact.id
    && diff.artifact_content_sha256 === artifact.content_sha256
    && diff.previous_artifact_id === previous.id
    && diff.previous_artifact_content_sha256 === previous.content_sha256
    && diff.document_changes.length > 0,
    "artifact changes do not prove predecessor lineage",
  );
  assertClosed(
    !diff.truncated
    && diff.document_changes.length === diff.total_document_changes
    && hunks.length === diff.total_hunks
    && lines.length === diff.total_lines,
    "artifact changes are truncated or incomplete",
  );
}

function expectedDiffPredecessor(
  capture: CaptureState,
  artifact: CapturedArtifact,
): CapturedArtifact {
  const sources = artifact.lineage.source_artifact_ids.map((sourceId) => {
    const source = capture.artifacts.get(sourceId);
    assertClosed(source !== undefined, "artifact predecessor is absent from the stable capture");
    return source;
  });
  const previous = selectLatestArtifactPredecessor(artifact, sources);
  assertClosed(previous !== undefined, "artifact has no compatible stable predecessor");
  return previous;
}

function artifactLabel(target: Target): string {
  if (target === "text_memory") return "Text memory";
  if (target === "skill_bundle") return "Skills";
  return "Agent guidance";
}

function sessionStreamLabel(stream: CapturedLog["stream"]): string {
  if (stream === "agent") return "Agent";
  if (stream === "evolution") return "Evolution";
  if (stream === "service") return "Service";
  return "Core";
}

function formatSessionTime(timestamp: string): string {
  return new Intl.DateTimeFormat("en", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZone: "UTC",
  }).format(new Date(timestamp));
}

function escapeRegex(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

async function readNativeObservation(page: Page): Promise<NativeObservation> {
  return page.evaluate(() => (
    window as typeof window & {
      __OPENEVO_LIVE_NATIVE_OBSERVATION__: NativeObservation;
    }
  ).__OPENEVO_LIVE_NATIVE_OBSERVATION__);
}
