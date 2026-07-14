import type { DesktopApiClientV1, FetchLike, ListRequestOptions } from "../api/v1/client";
import { DesktopApiError, DesktopContractError } from "../api/v1/client";
import {
  projectSourceV1Schema,
  remoteProfileV1Schema,
  type ApiErrorV1,
  type ArtifactV1,
  type LocalOperationV1,
  type OperationV1,
  type PageV1,
  type ProjectCapabilitiesV1,
  type ProjectSourceV1,
  type ProjectV1,
  type RemoteProfileV1,
  type RunV1,
  type TimelineEntryV1,
} from "../api/v1/schemas";
import { parseEventStreamFailure, parseSseFrame } from "../api/v1/sse";
import {
  DesktopProductUserError,
  ProductRefreshOrder,
  type DesktopProductProvider,
  type DesktopProductSnapshot,
  type ProductMutationIntent,
  type ProductRefreshResult,
  type ProductResourceMutationIntent,
  type ProductRunIntent,
  type ProductStreamState,
  type ProductSubscriptionSignal,
  type ProjectCapabilityState,
  type ProjectSourceSelectionIntent,
  type ProjectValidationState,
} from "./provider";

const PAGE_LIMIT = 100;
const MAX_REFRESH_PAGES = 512;
const MAX_REFRESH_RESOURCES = 20_000;
const MAX_COLLECTION_PAGES = 100;
const MAX_CONCURRENCY = 6;
const MAX_SSE_BUFFER_BYTES = 1_048_576 + 4;
const DEFAULT_RECONNECT_DELAYS_MS = [250, 500, 1_000, 2_000, 4_000] as const;

type CredentialSlotKind = RemoteProfileV1["credential_slots"][number]["kind"];

export interface LocalApiNativeBridge {
  selectProjectSource(intent: ProjectSourceSelectionIntent): Promise<unknown>;
  configureCredential(profileId: string, slotKind: CredentialSlotKind, etag: string, actionId: string): Promise<unknown>;
}

export interface LocalApiDesktopProductProviderOptions {
  readonly client: DesktopApiClientV1;
  readonly native: LocalApiNativeBridge;
  readonly fetch?: FetchLike;
  readonly reconnectDelaysMs?: readonly number[];
}

class RefreshBudget {
  private pages = 0;
  private resources = 0;

  consumePage(itemCount: number): void {
    this.pages += 1;
    this.resources += itemCount;
    if (this.pages > MAX_REFRESH_PAGES) {
      throw new DesktopContractError("Desktop snapshot exceeded the page budget");
    }
    if (this.resources > MAX_REFRESH_RESOURCES) {
      throw new DesktopContractError("Desktop snapshot exceeded the resource budget");
    }
  }

  consumeResources(count: number): void {
    this.resources += count;
    if (this.resources > MAX_REFRESH_RESOURCES) {
      throw new DesktopContractError("Desktop snapshot exceeded the resource budget");
    }
  }
}

export class LocalApiDesktopProductProvider implements DesktopProductProvider {
  readonly providerKind = "desktop_sidecar" as const;

  private readonly client: DesktopApiClientV1;
  private readonly native: LocalApiNativeBridge;
  private readonly fetch: FetchLike;
  private readonly reconnectDelaysMs: readonly number[];
  private readonly refreshOrder = new ProductRefreshOrder();
  private readonly listeners = new Set<(signal: ProductSubscriptionSignal) => void>();
  private snapshot: DesktopProductSnapshot | null = null;
  private epoch = 0;
  private lastEventId: string | null = null;
  private lastEventSequence: number | null = null;
  private streamAbort: AbortController | null = null;
  private streamPromise: Promise<void> | null = null;
  private waitingForRefresh = false;

  constructor(options: LocalApiDesktopProductProviderOptions) {
    this.client = options.client;
    this.native = options.native;
    this.fetch = options.fetch ?? globalThis.fetch.bind(globalThis);
    this.reconnectDelaysMs = options.reconnectDelaysMs ?? DEFAULT_RECONNECT_DELAYS_MS;
  }

  async refresh(): Promise<ProductRefreshResult> {
    const refreshSequence = this.refreshOrder.begin();
    try {
      const snapshot = await this.loadSnapshot();
      if (!this.refreshOrder.isCurrent(refreshSequence)) {
        return { status: "stale", stream: { status: "stale", epoch: this.epoch, reason: "refresh_pending" } };
      }
      this.epoch += 1;
      const freshSnapshot: DesktopProductSnapshot = {
        ...snapshot,
        stream: { status: "fresh", epoch: this.epoch, lastEventId: this.lastEventId },
      };
      this.snapshot = freshSnapshot;
      this.waitingForRefresh = false;
      this.ensureEventStream();
      return { status: "fresh", snapshot: freshSnapshot };
    } catch (error) {
      if (!this.refreshOrder.isCurrent(refreshSequence)) {
        return { status: "stale", stream: { status: "stale", epoch: this.epoch, reason: "refresh_pending" } };
      }
      const stream: Extract<ProductStreamState, { status: "error" }> = {
        status: "error",
        epoch: this.epoch,
        error: apiErrorOf(error),
      };
      this.setSnapshotStream(stream);
      return { status: "error", stream };
    }
  }

  subscribe(listener: (signal: ProductSubscriptionSignal) => void): () => void {
    this.listeners.add(listener);
    this.ensureEventStream();
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0) {
        this.streamAbort?.abort();
        this.streamAbort = null;
      }
    };
  }

  async createProfile(input: Parameters<DesktopApiClientV1["createProfile"]>[0], intent: ProductMutationIntent): Promise<RemoteProfileV1> {
    this.assertIntent(intent);
    return this.invalidateAfter(this.client.createProfile(input, { idempotencyKey: intent.actionId }));
  }

  async updateProfile(
    profileId: string,
    input: Parameters<DesktopApiClientV1["updateProfile"]>[1],
    intent: ProductResourceMutationIntent,
  ): Promise<RemoteProfileV1> {
    this.assertIntent(intent);
    return this.invalidateAfter(this.client.updateProfile(profileId, input, { ifMatch: intent.etag }));
  }

  async configureCredential(
    profileId: string,
    slotKind: CredentialSlotKind,
    intent: ProductResourceMutationIntent,
  ): Promise<RemoteProfileV1> {
    this.assertIntent(intent);
    const profile = remoteProfileV1Schema.parse(
      await this.native.configureCredential(profileId, slotKind, intent.etag, intent.actionId),
    );
    if (profile.profile_id !== profileId) {
      throw new DesktopContractError("Native credential response returned the wrong profile");
    }
    this.invalidate();
    return profile;
  }

  async connectProfile(profileId: string, intent: ProductResourceMutationIntent): Promise<LocalOperationV1> {
    this.assertIntent(intent);
    return this.invalidateAfter(this.client.connectProfile(profileId, actionOptions(intent)));
  }

  async acceptHostKey(
    profileId: string,
    input: Parameters<DesktopApiClientV1["acceptProfileHostKey"]>[1],
    intent: ProductResourceMutationIntent,
  ): Promise<LocalOperationV1> {
    this.assertIntent(intent);
    return this.invalidateAfter(this.client.acceptProfileHostKey(profileId, input, actionOptions(intent)));
  }

  async createProject(input: Parameters<DesktopApiClientV1["createProject"]>[0], intent: ProductMutationIntent): Promise<ProjectV1> {
    this.assertIntent(intent);
    return this.invalidateAfter(this.client.createProject(input, { idempotencyKey: intent.actionId }));
  }

  async updateProject(
    projectId: string,
    input: Parameters<DesktopApiClientV1["updateProject"]>[1],
    intent: ProductResourceMutationIntent,
  ): Promise<ProjectV1> {
    this.assertIntent(intent);
    return this.invalidateAfter(this.client.updateProject(projectId, input, { ifMatch: intent.etag }));
  }

  async activateProject(projectId: string, intent: ProductResourceMutationIntent): Promise<LocalOperationV1> {
    this.assertIntent(intent);
    return this.invalidateAfter(this.client.activateProject(projectId, actionOptions(intent)));
  }

  async syncProjectWorkspace(projectId: string, intent: ProductResourceMutationIntent): Promise<LocalOperationV1> {
    this.assertIntent(intent);
    return this.invalidateAfter(this.client.syncProjectWorkspace(projectId, actionOptions(intent)));
  }

  async selectProjectSource(intent: ProjectSourceSelectionIntent): Promise<ProjectSourceV1> {
    this.assertIntent(intent);
    return projectSourceV1Schema.parse(await this.native.selectProjectSource(intent));
  }

  async startRun(intent: ProductRunIntent): Promise<RunV1> {
    this.assertIntent(intent);
    return this.invalidateAfter(
      this.client.createRun({ project_id: intent.projectId }, actionOptions(intent)),
    );
  }

  async cancelRun(runId: string, intent: ProductResourceMutationIntent): Promise<RunV1> {
    this.assertIntent(intent);
    return this.invalidateAfter(this.client.cancelRun(runId, actionOptions(intent)));
  }

  getArtifactContent(artifactId: string) {
    return this.client.artifactContent(artifactId);
  }

  getArtifactDiff(artifactId: string) {
    return this.client.artifactDiff(artifactId);
  }

  async repairProject(projectId: string, intent: ProductResourceMutationIntent): Promise<LocalOperationV1> {
    this.assertIntent(intent);
    return this.invalidateAfter(this.client.repairProject(projectId, actionOptions(intent)));
  }

  async restartService(serviceId: string, intent: ProductResourceMutationIntent): Promise<OperationV1> {
    this.assertIntent(intent);
    return this.invalidateAfter(this.client.restartService(serviceId, actionOptions(intent)));
  }

  private async loadSnapshot(): Promise<Omit<DesktopProductSnapshot, "stream">> {
    const budget = new RefreshBudget();
    const [state, profiles, projects, runSummaries, services] = await Promise.all([
      this.client.state(),
      collectPages((options) => this.client.listProfiles(options), budget),
      collectPages((options) => this.client.listProjects(options), budget),
      collectPages((options) => this.client.listRuns(options), budget),
      collectPages((options) => this.client.listServices(options), budget),
    ]);

    const runs = await mapLimited(runSummaries, MAX_CONCURRENCY, async (summary) => {
      const run = await this.client.getRun(summary.id);
      if (run.id !== summary.id) throw new DesktopContractError("Run detail identity does not match its summary");
      budget.consumeResources(1);
      return run;
    });

    const timelines: Record<string, readonly TimelineEntryV1[]> = {};
    const artifactGroups = await mapLimited(runs, MAX_CONCURRENCY, async (run) => {
      const timeline = await collectPages((options) => this.client.runTimeline(run.id, options), budget);
      if (timeline.some((entry) => entry.run_id !== run.id)) {
        throw new DesktopContractError("Run timeline contains an entry for another run");
      }
      timelines[run.id] = timeline;
      try {
        const artifacts = await collectPages((options) => this.client.runArtifacts(run.id, options), budget);
        if (artifacts.some((artifact) => artifact.run_id !== null && artifact.run_id !== run.id)) {
          throw new DesktopContractError("Run artifact collection contains an artifact for another run");
        }
        return { complete: true as const, artifacts };
      } catch {
        return { complete: false as const, artifacts: [] as ArtifactV1[] };
      }
    });

    const artifactsComplete = artifactGroups.every((group) => group.complete);
    const artifacts = artifactsComplete
      ? deduplicateArtifacts(artifactGroups.flatMap((group) => group.artifacts))
      : [];
    const activeOperation = await this.loadActiveOperation(state.pending_operation_ids, budget);
    const { capability, validation } = await this.loadProjectAuthority(state, projects, profiles);

    return {
      state,
      profiles,
      projects,
      runs,
      timelines,
      artifacts,
      artifactCollection: artifactsComplete
        ? { status: "complete" }
        : { status: "incomplete", reason: "refresh_failed" },
      services,
      capability,
      validation,
      diagnostic: null,
      activeOperation,
    };
  }

  private async loadActiveOperation(operationIds: readonly string[], budget: RefreshBudget): Promise<LocalOperationV1 | null> {
    const ids = [...new Set(operationIds)].sort();
    if (ids.length === 0) return null;
    const operations = await mapLimited(ids, MAX_CONCURRENCY, async (operationId) => {
      const operation = await this.client.getOperation(operationId);
      if (operation.operation_id !== operationId) {
        throw new DesktopContractError("Pending operation identity does not match Desktop state");
      }
      return operation;
    });
    budget.consumeResources(operations.length);
    return operations.find((operation) => !["succeeded", "failed", "cancelled"].includes(operation.state)) ?? null;
  }

  private async loadProjectAuthority(
    state: Awaited<ReturnType<DesktopApiClientV1["state"]>>,
    projects: readonly ProjectV1[],
    profiles: readonly RemoteProfileV1[],
  ): Promise<{ capability: ProjectCapabilityState | null; validation: ProjectValidationState | null }> {
    const active = state.active_project;
    if (active === null) return { capability: null, validation: null };
    const project = projects.find((item) => item.project_id === active.project_id);
    if (
      !project
      || project.state !== "active"
      || project.etag !== active.project_etag
      || project.profile_id !== active.profile_id
      || !profiles.some((profile) => profile.profile_id === active.profile_id)
    ) {
      throw new DesktopContractError("Desktop active project does not match the authoritative project snapshot");
    }
    const unavailable = (error: ApiErrorV1 | null) => ({
      capability: {
        status: "unavailable" as const,
        projectId: project.project_id,
        executionMode: project.execution.mode,
        error,
      },
      validation: {
        status: "unavailable" as const,
        projectId: project.project_id,
        executionMode: project.execution.mode,
        projectEtag: project.etag,
        error,
      },
    });
    const tunnelReady = active.connection_state === "ready"
      && state.core.active_tunnel
      && state.core.core !== null
      && state.core.profile_id === active.profile_id
      && ["online", "degraded"].includes(state.core.state);
    if (!tunnelReady) return unavailable(null);

    let capabilities: ProjectCapabilitiesV1;
    try {
      capabilities = await this.client.projectCapabilities(project.project_id);
    } catch (error) {
      if (isUnavailable(error)) return unavailable(apiErrorOf(error));
      throw error;
    }
    if (
      capabilities.project_id !== project.project_id
      || capabilities.project_etag !== project.etag
      || capabilities.capabilities.evaluated_profile.execution_mode !== capabilityExecutionMode(project)
    ) {
      throw new DesktopContractError("Project capabilities do not match the active project");
    }
    const capability: ProjectCapabilityState = {
      status: "ready",
      projectId: project.project_id,
      executionMode: project.execution.mode,
      value: capabilities,
    };
    try {
      const value = await this.client.validateProject(project.project_id, {
        ifMatch: project.etag,
        idempotencyKey: validationIdempotencyKey(project),
      });
      if (
        value.project_id !== project.project_id
        || value.project_etag !== project.etag
        || value.registry_digest !== capabilities.capabilities.registry_digest
      ) {
        throw new DesktopContractError("Project validation does not match the active project snapshot");
      }
      return {
        capability,
        validation: {
          status: "ready",
          projectId: project.project_id,
          executionMode: project.execution.mode,
          projectEtag: project.etag,
          value,
        },
      };
    } catch (error) {
      if (isUnavailable(error)) {
        return {
          capability,
          validation: {
            status: "unavailable",
            projectId: project.project_id,
            executionMode: project.execution.mode,
            projectEtag: project.etag,
            error: apiErrorOf(error),
          },
        };
      }
      throw error;
    }
  }

  private assertIntent(intent: ProductMutationIntent): void {
    if (!this.snapshot || this.snapshot.stream.status !== "fresh" || intent.streamEpoch !== this.epoch) {
      throw new DesktopProductUserError("Refresh this view before trying again.");
    }
  }

  private async invalidateAfter<T>(request: Promise<T>): Promise<T> {
    const value = await request;
    this.invalidate();
    return value;
  }

  private invalidate(): void {
    this.setSnapshotStream({ status: "stale", epoch: this.epoch, reason: "refresh_pending" });
    this.emit({ kind: "snapshot_changed" });
  }

  private setSnapshotStream(stream: ProductStreamState): void {
    if (this.snapshot) this.snapshot = { ...this.snapshot, stream };
  }

  private emit(signal: ProductSubscriptionSignal): void {
    for (const listener of this.listeners) listener(signal);
  }

  private ensureEventStream(): void {
    if (this.listeners.size === 0 || this.waitingForRefresh || this.streamPromise !== null) return;
    this.streamPromise = this.runEventStream()
      .catch((error) => {
        this.waitingForRefresh = true;
        const apiError = apiErrorOf(error);
        this.setSnapshotStream({ status: "error", epoch: this.epoch, error: apiError });
        this.emit({ kind: "stream_error", error: apiError });
      })
      .finally(() => {
        this.streamPromise = null;
        this.streamAbort = null;
        if (this.listeners.size > 0 && !this.waitingForRefresh) queueMicrotask(() => this.ensureEventStream());
      });
  }

  private async runEventStream(): Promise<void> {
    let attempt = 0;
    while (this.listeners.size > 0 && attempt <= this.reconnectDelaysMs.length) {
      const controller = new AbortController();
      this.streamAbort = controller;
      try {
        const request = await this.client.eventStreamRequest(this.lastEventId ?? undefined);
        const response = await this.fetch(request.url, {
          method: "GET",
          headers: request.headers,
          credentials: "omit",
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) {
          const payload = await readStreamError(response);
          const recovery = parseEventStreamFailure(response.status, payload);
          this.lastEventId = recovery.resumeFromEventId;
          this.lastEventSequence = null;
          this.waitingForRefresh = true;
          this.setSnapshotStream({ status: "cursor_reset", epoch: this.epoch, resumeFromEventId: null });
          this.emit({ kind: "cursor_reset", resumeFromEventId: null });
          return;
        }
        const contentType = response.headers.get("Content-Type")?.toLowerCase() ?? "";
        if (!contentType.includes("text/event-stream")) {
          throw new DesktopContractError("Desktop event stream response is not an event stream", { status: response.status });
        }
        if (!response.body) throw new DesktopContractError("Desktop event stream response has no body");
        const sawEvent = await this.consumeEventStream(response.body, controller);
        if (sawEvent) attempt = 0;
      } catch (error) {
        if (controller.signal.aborted) return;
        if (error instanceof DesktopApiError || error instanceof DesktopContractError) throw error;
      }
      if (attempt >= this.reconnectDelaysMs.length) {
        throw new DesktopContractError("Desktop event stream exhausted its reconnect budget");
      }
      await delay(this.reconnectDelaysMs[attempt]!, controller.signal);
      attempt += 1;
    }
  }

  private async consumeEventStream(stream: ReadableStream<Uint8Array>, controller: AbortController): Promise<boolean> {
    const reader = stream.getReader();
    const decoder = new TextDecoder("utf-8", { fatal: true });
    let buffer = "";
    let sawEvent = false;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decodeUtf8(decoder, value, true);
        if (utf8Bytes(buffer) > MAX_SSE_BUFFER_BYTES) {
          throw new DesktopContractError("Desktop event stream exceeded the buffer limit");
        }
        let boundary = findFrameBoundary(buffer);
        while (boundary !== null) {
          const frame = buffer.slice(0, boundary.index);
          buffer = buffer.slice(boundary.index + boundary.length);
          if (frame !== "") {
            const parsed = parseSseFrame(frame);
            if (parsed.kind === "event") {
              sawEvent = true;
              const disposition = this.acceptEvent(parsed.id, parsed.envelope.sequence);
              if (disposition === "gap") {
                this.waitingForRefresh = true;
                this.setSnapshotStream({ status: "stale", epoch: this.epoch, reason: "event_gap" });
                this.emit({ kind: "stream_stale", reason: "event_gap" });
                this.emit({ kind: "snapshot_changed" });
                controller.abort();
                return sawEvent;
              }
              if (disposition === "accepted" && parsed.envelope.data.kind !== "heartbeat") {
                this.invalidate();
              }
            }
          }
          boundary = findFrameBoundary(buffer);
        }
      }
      buffer += decodeUtf8(decoder, undefined, false);
      if (buffer.trim() !== "") throw new DesktopContractError("Desktop event stream ended with an incomplete frame");
      return sawEvent;
    } finally {
      reader.releaseLock();
    }
  }

  private acceptEvent(eventId: string, sequence: number): "accepted" | "duplicate" | "gap" {
    if (this.lastEventSequence === null) {
      this.lastEventSequence = sequence;
      this.lastEventId = eventId;
      return "accepted";
    }
    if (sequence === this.lastEventSequence && eventId === this.lastEventId) return "duplicate";
    if (sequence !== this.lastEventSequence + 1) {
      if (sequence > this.lastEventSequence) {
        this.lastEventSequence = sequence;
        this.lastEventId = eventId;
      }
      return "gap";
    }
    this.lastEventSequence = sequence;
    this.lastEventId = eventId;
    return "accepted";
  }
}

export function createLocalApiDesktopProductProvider(
  options: LocalApiDesktopProductProviderOptions,
): DesktopProductProvider {
  return new LocalApiDesktopProductProvider(options);
}

async function collectPages<T>(
  load: (options: ListRequestOptions) => Promise<PageV1<T>>,
  budget: RefreshBudget,
): Promise<T[]> {
  const items: T[] = [];
  const cursors = new Set<string>();
  let after: string | undefined;
  for (let pageNumber = 0; pageNumber < MAX_COLLECTION_PAGES; pageNumber += 1) {
    const page = await load({ limit: PAGE_LIMIT, ...(after === undefined ? {} : { after }) });
    budget.consumePage(page.items.length);
    items.push(...page.items);
    if (page.has_more !== (page.next_cursor !== null)) {
      throw new DesktopContractError("Desktop pagination cursor does not agree with has_more");
    }
    if (!page.has_more) return items;
    const cursor = page.next_cursor;
    if (cursor === null || cursors.has(cursor)) {
      throw new DesktopContractError("Desktop pagination cursor cycle detected");
    }
    cursors.add(cursor);
    after = cursor;
  }
  throw new DesktopContractError("Desktop collection exceeded the page budget");
}

async function mapLimited<T, R>(
  values: readonly T[],
  concurrency: number,
  mapper: (value: T, index: number) => Promise<R>,
): Promise<R[]> {
  const results = new Array<R>(values.length);
  let nextIndex = 0;
  const workers = Array.from({ length: Math.min(concurrency, values.length) }, async () => {
    while (nextIndex < values.length) {
      const index = nextIndex;
      nextIndex += 1;
      results[index] = await mapper(values[index]!, index);
    }
  });
  await Promise.all(workers);
  return results;
}

function actionOptions(intent: ProductResourceMutationIntent) {
  return { idempotencyKey: intent.actionId, ifMatch: intent.etag };
}

function validationIdempotencyKey(project: ProjectV1): string {
  return `desktop-validation-${project.etag.slice(1, -1)}`;
}

function capabilityExecutionMode(project: ProjectV1): "subscription" | "self_deployed" {
  return project.execution.mode === "codex_subscription_transcript" ? "subscription" : "self_deployed";
}

function isUnavailable(error: unknown): boolean {
  return (error instanceof DesktopApiError && error.status === 503)
    || (error instanceof TypeError && !(error instanceof DesktopContractError));
}

function apiErrorOf(error: unknown): ApiErrorV1 | null {
  return error instanceof DesktopApiError ? error.apiError : null;
}

function deduplicateArtifacts(artifacts: readonly ArtifactV1[]): ArtifactV1[] {
  const values = new Map<string, ArtifactV1>();
  for (const artifact of artifacts) {
    const existing = values.get(artifact.id);
    if (existing && JSON.stringify(existing) !== JSON.stringify(artifact)) {
      throw new DesktopContractError("Artifact identity was reused for conflicting snapshots");
    }
    values.set(artifact.id, artifact);
  }
  return [...values.values()];
}

function findFrameBoundary(buffer: string): { index: number; length: number } | null {
  const match = /\r\n\r\n|\n\n|\r\r/.exec(buffer);
  return match && match.index !== undefined ? { index: match.index, length: match[0].length } : null;
}

function utf8Bytes(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

async function readStreamError(response: Response): Promise<unknown> {
  const contentType = response.headers.get("Content-Type")?.toLowerCase() ?? "";
  if (!contentType.includes("application/json")) {
    throw new DesktopContractError("Desktop event stream error response is not JSON", { status: response.status });
  }
  if (!response.body) {
    throw new DesktopContractError("Desktop event stream error response has no body", { status: response.status });
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let bytes = 0;
  let body = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > MAX_SSE_BUFFER_BYTES) {
        throw new DesktopContractError("Desktop event stream error exceeded the buffer limit", { status: response.status });
      }
      body += decodeUtf8(decoder, value, true);
    }
    body += decodeUtf8(decoder, undefined, false);
  } finally {
    reader.releaseLock();
  }
  try {
    return JSON.parse(body);
  } catch (error) {
    throw new DesktopContractError("Desktop event stream returned malformed JSON", { cause: error, status: response.status });
  }
}

function decodeUtf8(decoder: TextDecoder, value: Uint8Array | undefined, stream: boolean): string {
  try {
    return value === undefined ? decoder.decode() : decoder.decode(value, { stream });
  } catch (error) {
    throw new DesktopContractError("Desktop event stream contains invalid UTF-8", { cause: error });
  }
}

function delay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted || milliseconds <= 0) {
      resolve();
      return;
    }
    const timer = setTimeout(resolve, milliseconds);
    signal.addEventListener("abort", () => {
      clearTimeout(timer);
      resolve();
    }, { once: true });
  });
}
