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
  type Page,
  type Request,
  type Response,
  type Route,
} from "@playwright/test";
import { z } from "zod";
import { desktopBootstrapContextV1Schema } from "../../src/api/v1/schemas";
import {
  drainPendingSnapshot,
  InFlightCaptureWindow,
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
type CapturedArtifact = {
  id: string;
  artifact_type: Target;
  target_id: Target;
  content_sha256: string;
  lineage: { source_artifact_ids: string[] };
};
type CapturedContent = {
  artifact_id: string;
  artifact_type: Target;
  documents: Array<{
    content: string;
    content_sha256: string;
    byte_size: number;
    truncated: boolean;
    relative_path: string;
  }>;
  total_documents: number;
  total_utf8_bytes: number;
  returned_utf8_bytes: number;
  truncated: boolean;
};
type CapturedTimeline = { phase: Phase; sequence: number };

type CaptureState = {
  sourceCommit: string | null;
  timelines: Map<string, Map<string, CapturedTimeline>>;
  logs: Map<string, Map<string, unknown>>;
  artifacts: Map<string, CapturedArtifact>;
  contents: Map<string, CapturedContent>;
  diffs: Map<string, { previous_artifact_id: string; document_changes: unknown[] }>;
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
  await installPackagedWebRoute(page, packaged);
  await installWebSocketGate(page, capture);
  await installNetworkGate(
    page,
    liveOrigin,
    handoff.bootstrap.session_token,
    handoff.expected.project_id,
    capture,
  );
  observeNetwork(
    page,
    liveOrigin,
    handoff.bootstrap.session_token,
    handoff.expected.project_id,
    capture,
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
    const expectedRenderedTimeline = [...(capture.timelines.get(session.run_id)?.entries() ?? [])]
      .sort(([leftId, left], [rightId, right]) => left.sequence - right.sequence || leftId.localeCompare(rightId))
      .map(([, entry]) => entry);
    const renderedTimeline = await page.locator(".run-timeline li").evaluateAll((entries) => entries.map((entry) => ({
      sequence: Number((entry as HTMLElement).dataset.sequence),
      phase: (entry as HTMLElement).dataset.phase,
    })));
    assertClosed(
      renderedTimeline.length === expectedRenderedTimeline.length
      && renderedTimeline.every((entry, index) => (
        entry.sequence === expectedRenderedTimeline[index]?.sequence
        && entry.phase === expectedRenderedTimeline[index]?.phase
      )),
      `renderer timeline for session ${session.ordinal} does not follow the authoritative remote sequence`,
    );
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
  assertTimelineCapture(capture, handoff);
  const latestLogs = capture.logs.get(latestSession.run_id);
  assertClosed(
    latestLogs !== undefined && latestLogs.size >= latestSession.minimum_log_count,
    "live session output did not satisfy the expected lower bound",
  );

  await page.getByRole("button", { name: "Evolution", exact: true }).click();
  await expect(page.getByTestId("evolution-workspace")).toBeVisible();
  await expect(page.locator(".revision-node").filter({
    hasText: `Project Head ${handoff.expected.project_head_generation}`,
  })).toBeVisible();
  await expect(page.locator(".artifact-list-heading")).toContainText("3 selected");
  await expect(page.locator(".artifact-list-item")).toHaveCount(3);
  await drainCapture(capture);
  assertExpectedArtifactCollection(capture, handoff);

  const artifactEvidence: Array<z.infer<typeof resultArtifactSchema>> = [];
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

    const content = capture.contents.get(expectedArtifact.artifact_id);
    const artifact = capture.artifacts.get(expectedArtifact.artifact_id);
    assertClosed(content !== undefined && artifact !== undefined, "artifact observation is incomplete");
    assertClosed(
      content.artifact_id === expectedArtifact.artifact_id
      && content.artifact_type === expectedArtifact.artifact_type
      && artifact.content_sha256 === expectedArtifact.artifact_content_sha256,
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
    const runtimeDocuments = expectedArtifact.target_id === "skill_bundle"
      ? content.documents.filter((document) => document.relative_path === "SKILL.md")
      : content.documents;
    assertClosed(
      runtimeDocuments.length === 1
      && runtimeDocuments[0]?.content_sha256 === expectedArtifact.runtime_document_sha256,
      "artifact runtime document changed after workflow verification",
    );
    const rendered = await page.locator(".artifact-document").textContent();
    assertClosed(
      rendered === content.documents[0]!.content && rendered.trim().length > 0,
      "artifact content was not rendered",
    );
    artifactEvidence.push({
      artifact_id_sha256: sha256(Buffer.from(expectedArtifact.artifact_id, "utf8")),
      artifact_type: expectedArtifact.artifact_type,
      target_id: expectedArtifact.target_id,
      document_count: content.documents.length,
      total_utf8_bytes: content.total_utf8_bytes,
      content_sha256: artifact.content_sha256,
      runtime_document_sha256: runtimeDocuments[0]!.content_sha256,
    });
  }

  const diffArtifact = handoff.expected.artifacts.find((candidate) => {
    const artifact = capture.artifacts.get(candidate.artifact_id);
    return artifact?.lineage.source_artifact_ids.some((id) => capture.artifacts.has(id));
  });
  if (diffArtifact !== undefined) {
    await page.locator(".artifact-list").getByRole("button", {
      name: new RegExp(`^${escapeRegex(artifactLabel(diffArtifact.target_id))}`),
    }).click();
    await page.getByRole("tab", { name: "Changes", exact: true }).click();
    await expect(page.locator(".diff-view")).toBeVisible();
    await expect.poll(async () => {
      await drainCapture(capture);
      return capture.diffs.has(diffArtifact.artifact_id);
    }, { message: "live artifact diff was not observed by the renderer" }).toBe(true);
    const diff = capture.diffs.get(diffArtifact.artifact_id);
    const diffSourceIds = capture.artifacts.get(diffArtifact.artifact_id)?.lineage.source_artifact_ids ?? [];
    assertClosed(
      diff !== undefined
      && diffSourceIds.includes(diff.previous_artifact_id)
      && diff.document_changes.length > 0,
      "artifact changes do not prove predecessor lineage",
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

  const native = await readNativeObservation(page);
  await stopObservingResponses();
  await drainCapture(capture);
  assertClosed(native.rendererReady, "renderer readiness was not acknowledged");
  assertClosed(native.commands.includes("start_sidecar"), "native bootstrap was not invoked");
  assertClosed(native.stages.includes("product_committed"), "product commit was not reported");
  assertClosed(native.unexpected.length === 0, "renderer invoked an unsupported native command");
  assertClosed(capture.errors.length === 0, "a live response could not be captured");
  assertClosed(
    capture.networkErrors.length === 0,
    `renderer crossed the allowed network boundary: ${capture.networkErrors.join(",")}`,
  );

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
    logs: { count: latestLogs!.size },
    project_head_generation: handoff.expected.project_head_generation,
    independent_target_controls_verified: true,
    remote_method_selection_verified: true,
    artifacts: artifactEvidence,
    screenshot_sha256: sha256(screenshot),
  });

  await page.close();
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
): void {
  page.on("request", (request) => {
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
  });
  page.on("requestfailed", (request) => {
    const url = new URL(request.url());
    if (url.origin === liveOrigin) {
      capture.networkErrors.push(`${request.method()} ${url.pathname} ${request.failure()?.errorText ?? "failed"}`);
    }
  });
  page.on("console", (message) => {
    if (message.type() !== "error" && message.type() !== "warning") return;
    const value = message.text();
    if (value.includes("more-private address space") || value.includes("Private Network Access")) {
      capture.browserErrors.push("private-network access blocked");
    } else if (value.includes("CORS policy")) {
      capture.browserErrors.push("CORS policy blocked request");
    } else if (value.includes("Failed to load resource")) {
      capture.browserErrors.push("resource load failed");
    }
  });
}

function observeResponses(
  page: Page,
  liveOrigin: string,
  capture: CaptureState,
): () => Promise<void> {
  const window = new InFlightCaptureWindow<Request>();
  const observes = (request: Request): boolean => {
    const url = new URL(request.url());
    return url.origin === liveOrigin
      && url.pathname !== "/desktop/v1/events"
      && request.method() !== "OPTIONS";
  };
  const requestListener = (request: Request) => {
    if (observes(request)) window.begin(request);
  };
  const requestSettledListener = (request: Request) => {
    if (observes(request)) window.finish(request);
  };
  const listener = (response: Response) => {
    const url = new URL(response.url());
    if (!window.accepts(response.request())) return;
    if (capture.responses.length >= MAX_CAPTURE_RESPONSES) {
      capture.errors.push("live Local API response inventory exceeded the capture budget");
      return;
    }
    capture.responses.push(`${response.request().method()} ${url.pathname} ${response.status()}`);
    let promise: Promise<void>;
    promise = captureResponse(response, capture)
      .catch((error: unknown) => {
        capture.errors.push(`${url.pathname}:${error instanceof Error ? error.message : "response capture"}`);
      })
      .finally(() => capture.pending.delete(promise));
    capture.pending.add(promise);
  };
  page.on("request", requestListener);
  page.on("requestfinished", requestSettledListener);
  page.on("requestfailed", requestSettledListener);
  page.on("response", listener);
  return async () => {
    await window.close();
    page.off("request", requestListener);
    page.off("requestfinished", requestSettledListener);
    page.off("requestfailed", requestSettledListener);
    page.off("response", listener);
  };
}

async function captureResponse(response: Response, capture: CaptureState): Promise<void> {
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
      runEntries.set(textField(item, "id"), {
        phase: phaseSchema.parse(item.phase),
        sequence: integerField(item, "sequence"),
      });
    }
    assertClosed(runEntries.size <= MAX_CAPTURE_ENTRIES, "live timeline exceeded the capture budget");
    capture.timelines.set(timeline, runEntries);
    return;
  }
  const logs = resourceMatch(url.pathname, "logs");
  if (logs !== null) {
    const runLogs = capture.logs.get(logs) ?? new Map<string, unknown>();
    for (const item of pageItems(payload)) runLogs.set(textField(item, "id"), item);
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
    const object = record(payload);
    const artifactId = textField(object, "artifact_id");
    assertClosed(artifactId === artifactContent, "artifact content route identity mismatch");
    capture.contents.set(artifactId, {
      artifact_id: artifactId,
      artifact_type: targetSchema.parse(object.artifact_type),
      documents: arrayField(object, "documents").map((item) => {
        const document = record(item);
        return {
          content: stringField(document, "content"),
          content_sha256: sha256Schema.parse(document.content_sha256),
          byte_size: integerField(document, "byte_size"),
          truncated: booleanField(document, "truncated"),
          relative_path: stringField(document, "relative_path"),
        };
      }),
      total_documents: integerField(object, "total_documents"),
      total_utf8_bytes: integerField(object, "total_utf8_bytes"),
      returned_utf8_bytes: integerField(object, "returned_utf8_bytes"),
      truncated: booleanField(object, "truncated"),
    });
    assertClosed(capture.contents.size <= MAX_CAPTURE_ENTRIES, "live artifact content exceeded the capture budget");
    return;
  }
  const artifactDiff = artifactResourceMatch(url.pathname, "diff");
  if (artifactDiff !== null) {
    const object = record(payload);
    assertClosed(textField(object, "artifact_id") === artifactDiff, "artifact diff route identity mismatch");
    capture.diffs.set(artifactDiff, {
      previous_artifact_id: textField(object, "previous_artifact_id"),
      document_changes: arrayField(object, "document_changes"),
    });
    assertClosed(capture.diffs.size <= MAX_CAPTURE_ENTRIES, "live artifact diffs exceeded the capture budget");
  }
}

function captureArtifact(value: unknown, capture: CaptureState): void {
  const artifact = record(value);
  const lineage = record(artifact.lineage);
  const captured: CapturedArtifact = {
    id: textField(artifact, "id"),
    artifact_type: targetSchema.parse(artifact.artifact_type),
    target_id: targetSchema.parse(artifact.target_id),
    content_sha256: sha256Schema.parse(artifact.content_sha256),
    lineage: {
      source_artifact_ids: arrayField(lineage, "source_artifact_ids").map((item) => {
        assertClosed(typeof item === "string" && safeText(item), "artifact predecessor identity is invalid");
        return item;
      }),
    },
  };
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

function stringField(value: Record<string, unknown>, key: string): string {
  const field = value[key];
  assertClosed(typeof field === "string", "captured payload has an invalid string");
  return field;
}

function integerField(value: Record<string, unknown>, key: string): number {
  const field = value[key];
  assertClosed(typeof field === "number" && Number.isSafeInteger(field) && field >= 0, "captured payload has an invalid count");
  return field;
}

function booleanField(value: Record<string, unknown>, key: string): boolean {
  const field = value[key];
  assertClosed(typeof field === "boolean", "captured payload has an invalid flag");
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

function artifactLabel(target: Target): string {
  if (target === "text_memory") return "Text memory";
  if (target === "skill_bundle") return "Skills";
  return "Agent guidance";
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
