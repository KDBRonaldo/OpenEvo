import { createHash } from "node:crypto";
import { lstat, open, readFile, readdir, realpath } from "node:fs/promises";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Route } from "@playwright/test";
import { z } from "zod";
import {
  desktopBootstrapContextV2Schema,
  projectHeadRefV2Schema,
} from "../../src/api/v2/schemas";
import { liveDesktopRequestAllowed } from "./release-live-network-boundary";

const HANDOFF_ENV = "OPENEVO_DESKTOP_LIVE_RENDERER_HANDOFF";
const HANDOFF_PATH = process.env[HANDOFF_ENV];
const SECRET_CANARY_ENV = "OPENEVO_E2E_SECRET_CANARY";
const STATIC_ORIGIN = "http://tauri.localhost";
const MAX_HANDOFF_BYTES = 64 * 1024;
const MAX_RESULT_BYTES = 64 * 1024;
const DESKTOP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const REPOSITORY_PACKAGED_WEB_ROOT = resolve(DESKTOP_ROOT, "packaging/web");
const TARGETS = ["agent_system", "skill_bundle", "text_memory"] as const;

const sha256Schema = z.string().regex(/^[0-9a-f]{64}$/);
const targetSchema = z.enum(TARGETS);
const privatePathSchema = z.string().min(1).max(4096).refine(isAbsolute);
const handoffSchema = z.object({
  schema_version: z.literal("2"),
  kind: z.literal("openevo_desktop_live_renderer_handoff"),
  bootstrap: desktopBootstrapContextV2Schema,
  expected: z.object({
    source_commit: z.string().min(7).max(64).regex(/^[0-9a-f]+$/),
    project_id: z.string().min(1).max(128).refine(safeText),
    project_name: z.string().min(1).max(256).refine(safeText),
    ssh_host_alias: z.string().min(1).max(128).refine(safeText),
    method_ids: z.record(targetSchema, z.string().min(1).max(128).refine(safeText)),
    task_ids: z.array(z.string().min(1).max(128).refine(safeText)).length(2),
    active_project_head: projectHeadRefV2Schema,
  }).strict(),
  packaged_web_root: privatePathSchema,
  result_path: privatePathSchema,
  screenshot_path: privatePathSchema,
}).strict().superRefine((value, context) => {
  if (new Set(value.expected.task_ids).size !== 2) {
    context.addIssue({ code: z.ZodIssueCode.custom, path: ["expected", "task_ids"], message: "Task identities must be distinct" });
  }
  if (Object.keys(value.expected.method_ids).sort().join(",") !== [...TARGETS].sort().join(",")) {
    context.addIssue({ code: z.ZodIssueCode.custom, path: ["expected", "method_ids"], message: "Target method identities are incomplete" });
  }
  const head = value.expected.active_project_head;
  if (head.project_id !== value.expected.project_id || head.generation !== 2 || head.evolution_revision.artifact_count !== 3) {
    context.addIssue({ code: z.ZodIssueCode.custom, path: ["expected", "active_project_head"], message: "Expected active Project Head is not generation two with three outputs" });
  }
  if (value.result_path === value.screenshot_path) {
    context.addIssue({ code: z.ZodIssueCode.custom, path: ["result_path"], message: "Output paths must be distinct" });
  }
});

type LiveHandoff = z.infer<typeof handoffSchema>;
type NativeObservation = {
  commands: string[];
  stages: string[];
  rendererReady: boolean;
  unexpected: string[];
};
type NetworkObservation = {
  routeKinds: Set<"desktop_v2" | "packaged_web">;
  violations: string[];
};

const resultSchema = z.object({
  schema_version: z.literal("2"),
  kind: z.literal("openevo_desktop_live_renderer_observability"),
  outcome: z.literal("passed"),
  provider_kind: z.literal("desktop_sidecar"),
  source_commit: z.string().min(7).max(64).regex(/^[0-9a-f]+$/),
  packaged_web_build_digest: sha256Schema,
  desktop_api_major: z.literal(2),
  renderer_ready: z.literal(true),
  builtin_sample_count: z.literal(2),
  project_id_sha256: sha256Schema,
  task_count: z.literal(2),
  task_id_sha256: z.array(sha256Schema).length(2),
  active_project_head_generation: z.literal(2),
  evolution_artifact_count: z.literal(3),
  system_openssh_workspace_verified: z.literal(true),
  remote_target_controls_verified: z.literal(true),
  secret_canary_absent: z.literal(true),
  selected_methods: z.record(targetSchema, z.string().min(1).max(128).refine(safeText)),
  observed_route_kinds: z.tuple([z.literal("desktop_v2"), z.literal("packaged_web")]),
  screenshot_sha256: sha256Schema,
}).strict();

test.skip(!HANDOFF_PATH, `requires ${HANDOFF_ENV}`);

test("packaged renderer observes the live Desktop v2 authority", async ({ page }) => {
  const secretCanary = process.env[SECRET_CANARY_ENV];
  assertClosed(
    typeof secretCanary === "string"
      && Buffer.byteLength(secretCanary, "utf8") >= 16
      && Buffer.byteLength(secretCanary, "utf8") <= 256
      && !/[\u0000\r\n]/.test(secretCanary),
    "release secret canary is unavailable",
  );
  const handoff = await readPrivateHandoff(HANDOFF_PATH!);
  await assertOutputDoesNotExist(handoff.result_path);
  await assertOutputDoesNotExist(handoff.screenshot_path);
  const packaged = await loadPackagedWeb(handoff.packaged_web_root);
  const liveOrigin = new URL(handoff.bootstrap.endpoint).origin;
  const browserErrors: string[] = [];
  page.on("pageerror", () => browserErrors.push("pageerror"));
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push("console_error");
  });
  page.on("websocket", () => browserErrors.push("websocket"));

  await installNativeBridge(page, handoff.bootstrap);
  const network = await installNetworkBoundary(page, packaged, liveOrigin, handoff.bootstrap.session_token);
  await page.goto(`${STATIC_ORIGIN}/`);

  const terminalStages = new Set([
    "bootstrap_context_failed",
    "local_api_version_failed",
    "provider_adapter_failed",
    "provider_create_failed",
    "initial_snapshot_failed",
    "product_committed",
  ]);
  await expect.poll(async () => (
    (await readNativeObservation(page)).stages.some((stage) => terminalStages.has(stage))
  ), { timeout: 90_000 }).toBe(true);
  const startup = await readNativeObservation(page);
  assertClosed(
    startup.stages.includes("product_committed"),
    `release provider startup failed at ${startup.stages.join(",")}`,
  );

  const shell = page.locator(".product-shell");
  await expect(shell).toHaveAttribute("data-provider-kind", "desktop_sidecar");
  await expect(shell).toHaveAttribute("data-api-version", "2");
  const projectSelector = page.locator("#v2-project-switcher");
  await expect(projectSelector).toHaveValue(`project:${handoff.expected.project_id}`);
  await expect(projectSelector.locator('option[value^="sample:"]')).toHaveCount(2);
  await expect(projectSelector.locator(`option[value="project:${handoff.expected.project_id}"]`)).toHaveText(
    handoff.expected.project_name,
  );

  const activeHead = handoff.expected.active_project_head;
  const headCard = page.locator(".v2-identity-card").filter({ hasText: "Project Head" }).first();
  await expect(headCard).toContainText(activeHead.project_head_id);
  await expect(headCard).toContainText("Generation 2");
  const revisionCard = page.locator(".v2-identity-card").filter({ hasText: "Evolution Revision" }).first();
  await expect(revisionCard).toContainText(activeHead.evolution_revision.evolution_revision_id);
  await expect(revisionCard).toContainText("3 artifacts");
  await expect(page.locator(".v2-identity-card").filter({ hasText: "Runtime Context Snapshot" }).first())
    .toContainText(activeHead.runtime_context_snapshot.runtime_context_snapshot_id);
  await expect(page.locator(".v2-identity-card").filter({ hasText: "Effective Execution Snapshot" }).first())
    .toContainText(activeHead.effective_execution_snapshot.effective_execution_snapshot_id);

  const taskCards = page.locator(".v2-task-card");
  await expect(taskCards).toHaveCount(2);
  for (const taskId of handoff.expected.task_ids) {
    await expect(taskCards.filter({ hasText: `Task ${taskId}` })).toHaveCount(1);
  }

  await page.getByRole("button", { name: "Evolution", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Evolution targets", exact: true })).toBeVisible();
  const targetRows = page.locator(".v2-target-list article");
  await expect(targetRows).toHaveCount(3);
  const targetToggles = targetRows.locator('input[type="checkbox"]');
  await expect(targetToggles).toHaveCount(3);
  for (let index = 0; index < 3; index += 1) {
    await expect(targetToggles.nth(index)).toBeChecked();
  }
  const observedMethods = await targetRows.locator("select").evaluateAll((selects) => (
    selects.map((select) => (select as HTMLSelectElement).value).sort()
  ));
  const expectedMethods = Object.values(handoff.expected.method_ids).sort();
  assertClosed(JSON.stringify(observedMethods) === JSON.stringify(expectedMethods), "remote method selections differ from the live project");
  for (const [targetId, methodId] of Object.entries(handoff.expected.method_ids)) {
    const select = targetRows.locator("select").filter({ has: page.locator(`option[value="${methodId}"]`) });
    await expect(select, `${targetId} method control`).toHaveCount(1);
    await expect(select).toHaveValue(methodId);
    await expect(select).toBeEnabled();
  }
  await expect(page.getByText("blocks Task admission", { exact: false })).toHaveCount(0);

  await page.getByRole("button", { name: "System", exact: true }).click();
  await expect(page.getByRole("heading", { name: "System OpenSSH workspace", exact: true })).toBeVisible();
  const systemSummary = page.locator(".v2-system-summary");
  await expect(systemSummary.locator("code")).toHaveText(handoff.expected.ssh_host_alias);
  await expect(systemSummary).toContainText("v2 verified");

  await page.getByRole("button", { name: "Research", exact: true }).click();
  await expect(taskCards).toHaveCount(2);
  const renderedSurface = await page.locator("body").evaluate((body) => {
    const controls = [...body.querySelectorAll("input, textarea, select")]
      .map((control) => (control as HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement).value);
    return [body.textContent ?? "", (body as HTMLElement).innerText, ...controls].join("\n");
  });
  assertClosed(!renderedSurface.includes(secretCanary), "secret canary reached rendered UI text");
  assertClosed(!(await page.content()).includes(secretCanary), "secret canary reached serialized renderer DOM");
  const screenshot = await page.screenshot({
    animations: "disabled",
    caret: "hide",
    fullPage: false,
    mask: [
      page.locator(".brief-body"),
      page.locator(".v2-task-card code"),
      page.locator(".v2-task-card strong"),
      page.locator(".v2-identity-card strong"),
      page.locator(".v2-identity-card code"),
    ],
    maskColor: "#d7dce2",
  });
  assertClosed(!screenshot.includes(Buffer.from(secretCanary, "utf8")), "secret canary reached screenshot bytes");

  const native = await readNativeObservation(page);
  assertClosed(native.rendererReady, "renderer readiness was not acknowledged");
  assertClosed(
    native.commands.includes("begin_sidecar_start")
      && native.commands.includes("sidecar_bootstrap_context")
      && !native.commands.includes("start_sidecar"),
    "native bootstrap did not use the release protocol",
  );
  assertClosed(native.stages.includes("product_committed"), "product commit was not reported");
  assertClosed(native.unexpected.length === 0, "renderer invoked an unsupported native command");
  assertClosed(network.violations.length === 0, `renderer crossed the network boundary: ${network.violations.join(",")}`);
  assertClosed(browserErrors.length === 0, `renderer reported browser errors: ${browserErrors.join(",")}`);

  const selectedMethods = Object.fromEntries(
    [...TARGETS].sort().map((target) => [target, handoff.expected.method_ids[target]]),
  );
  const result = resultSchema.parse({
    schema_version: "2",
    kind: "openevo_desktop_live_renderer_observability",
    outcome: "passed",
    provider_kind: "desktop_sidecar",
    source_commit: handoff.expected.source_commit,
    packaged_web_build_digest: packaged.buildDigest,
    desktop_api_major: 2,
    renderer_ready: true,
    builtin_sample_count: 2,
    project_id_sha256: sha256(Buffer.from(handoff.expected.project_id, "utf8")),
    task_count: 2,
    task_id_sha256: handoff.expected.task_ids.map((taskId) => sha256(Buffer.from(taskId, "utf8"))),
    active_project_head_generation: 2,
    evolution_artifact_count: 3,
    system_openssh_workspace_verified: true,
    remote_target_controls_verified: true,
    secret_canary_absent: true,
    selected_methods: selectedMethods,
    observed_route_kinds: [...network.routeKinds].sort(),
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
  assertClosed(metadata.nlink === 1 && (metadata.mode & 0o777) === 0o600, "live renderer handoff is not private");
  if (typeof process.getuid === "function") assertClosed(metadata.uid === process.getuid(), "live renderer handoff owner mismatch");
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
    `live renderer handoff does not match the closed v2 contract${parsed.success ? "" : `: ${parsed.error.issues.map((issue) => issue.path.join(".")).join(",")}`}`,
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

const manifestSchema = z.object({
  schema_version: z.literal("1"),
  build_digest: sha256Schema,
  files: z.array(z.object({
    path: z.string().min(1).max(512).refine(safeAssetPath),
    sha256: sha256Schema,
    byte_size: z.number().int().min(1).max(8 * 1024 * 1024),
  }).strict()).min(2).max(128),
}).strict();

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
  const parsed = manifestSchema.safeParse(JSON.parse(await readFile(manifestPath, "utf8")));
  assertClosed(parsed.success, "packaged renderer manifest does not match the closed contract");
  const manifest = parsed.data;
  assertClosed(sha256(Buffer.from(JSON.stringify(manifest.files), "utf8")) === manifest.build_digest, "packaged renderer manifest digest mismatch");
  const inventory = await assetInventory(requested);
  const expectedPaths = new Set([".openevo-product-web.json", ...manifest.files.map(({ path }) => path)]);
  assertClosed(inventory.length === expectedPaths.size && inventory.every((path) => expectedPaths.has(path)), "packaged renderer inventory differs from its manifest");
  const files = new Map<string, { bytes: Buffer; contentType: string }>();
  for (const entry of manifest.files) {
    const path = resolve(requested, ...entry.path.split("/"));
    assertClosed(path.startsWith(`${requested}${sep}`), "packaged renderer asset escaped its root");
    const metadata = await lstat(path);
    assertClosed(metadata.isFile() && !metadata.isSymbolicLink(), "packaged renderer asset is invalid");
    const bytes = await readFile(path);
    assertClosed(bytes.byteLength === entry.byte_size && sha256(bytes) === entry.sha256, "packaged renderer asset identity mismatch");
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
      if (metadata.isDirectory()) await visit(absolute);
      else {
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
  if (path.endsWith(".json") || path.endsWith(".map")) return "application/json; charset=utf-8";
  if (path.endsWith(".svg")) return "image/svg+xml";
  if (path.endsWith(".png")) return "image/png";
  if (path.endsWith(".woff2")) return "font/woff2";
  return "application/octet-stream";
}

async function installNetworkBoundary(
  page: Page,
  packaged: PackagedWeb,
  liveOrigin: string,
  sessionToken: string,
): Promise<NetworkObservation> {
  const observation: NetworkObservation = { routeKinds: new Set(), violations: [] };
  await page.route("**/*", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin === STATIC_ORIGIN) {
      observation.routeKinds.add("packaged_web");
      if (request.method() !== "GET") {
        observation.violations.push("packaged_method");
        await route.abort("blockedbyclient");
        return;
      }
      let path: string;
      try {
        path = decodeURIComponent(url.pathname.replace(/^\/+/, "")) || "index.html";
      } catch {
        observation.violations.push("packaged_path");
        await route.abort("blockedbyclient");
        return;
      }
      if (!safeAssetPath(path)) {
        observation.violations.push("packaged_path");
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
        headers: { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" },
      });
      return;
    }
    if (url.origin === liveOrigin) {
      observation.routeKinds.add("desktop_v2");
      if (!liveDesktopRequestAllowed({
        staticOrigin: STATIC_ORIGIN,
        liveOrigin,
        requestOrigin: url.origin,
        method: request.method(),
        pathname: url.pathname,
        headers: request.headers(),
        sessionToken,
      })) {
        observation.violations.push("desktop_contract");
        await route.abort("blockedbyclient");
        return;
      }
      await route.continue();
      return;
    }
    observation.violations.push("external_origin");
    await route.abort("blockedbyclient");
  });
  return observation;
}

async function installNativeBridge(page: Page, bootstrap: LiveHandoff["bootstrap"]): Promise<void> {
  await page.addInitScript((context) => {
    const observation: NativeObservation = { commands: [], stages: [], rendererReady: false, unexpected: [] };
    Object.defineProperty(window, "__OPENEVO_LIVE_NATIVE_OBSERVATION__", { configurable: false, enumerable: false, writable: false, value: observation });
    Object.defineProperty(window, "__TAURI_INTERNALS__", {
      configurable: false,
      enumerable: false,
      writable: false,
      value: {
        invoke: async (command: string, args: Record<string, unknown> = {}) => {
          observation.commands.push(command);
          if (command === "begin_sidecar_start") return null;
          if (command === "sidecar_bootstrap_context") return context;
          if (command === "stop_sidecar") return null;
          if (command === "renderer_bootstrap_stage") {
            const stage = typeof args.stage === "string" ? args.stage : "";
            const allowed = new Set([
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
            if (!allowed.has(stage)) {
              observation.unexpected.push("bootstrap_stage");
              throw new Error("Unexpected renderer bootstrap stage");
            }
            observation.stages.push(stage);
            return null;
          }
          if (command === "renderer_ready") {
            if (
              args.openapiSha256 !== context.negotiated_contract.openapi_sha256
              || args.eventSchemaSha256 !== context.negotiated_contract.event_schema_sha256
              || args.releaseVersion !== context.negotiated_contract.release_version
            ) {
              observation.unexpected.push("renderer_identity");
              throw new Error("Renderer readiness identity mismatch");
            }
            observation.rendererReady = true;
            return null;
          }
          observation.unexpected.push("native_command");
          throw new Error("Unexpected native command");
        },
      },
    });
  }, bootstrap);
}

async function readNativeObservation(page: Page): Promise<NativeObservation> {
  return page.evaluate(() => {
    const value = (window as unknown as { __OPENEVO_LIVE_NATIVE_OBSERVATION__?: NativeObservation })
      .__OPENEVO_LIVE_NATIVE_OBSERVATION__;
    if (!value) throw new Error("native observation is unavailable");
    return JSON.parse(JSON.stringify(value)) as NativeObservation;
  });
}
