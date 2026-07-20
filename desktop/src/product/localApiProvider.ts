import type { DesktopApiClientV1, FetchLike, ListRequestOptions } from "../api/v1/client";
import { DesktopApiError, DesktopContractError } from "../api/v1/client";
import {
  profileCreateV1Schema,
  profilePatchV1Schema,
  projectCreateV1Schema,
  projectPatchV1Schema,
  projectSourceV1Schema,
  type CacheCleanupRequestV1,
  type ApiErrorV1,
  type ArtifactContentV1,
  type ArtifactDiffV1,
  type ArtifactV1,
  type DiagnosticCreateV1,
  type DiagnosticReportV1,
  type LocalOperationV1,
  type LogEntryV1,
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
  DesktopProductAmbiguousMutationError,
  DesktopProductUserError,
  OperationContinuationAuthority,
  ProductRefreshOrder,
  type DesktopProductSnapshot,
  type ProductMutationIntent,
  type ProductRefreshResult,
  type ProductResourceMutationIntent,
  type ProductRunRetryRecovery,
  type ProductRunIntent,
  type ProductStreamState,
  type ProductSubscriptionSignal,
  type ProjectCapabilityState,
  type ProjectSourceSelectionIntent,
  type ProjectValidationState,
  type ReleaseDesktopProductProvider,
} from "./provider";
import {
  createRunRetryRecovery,
  overlayAcceptedRetryRun,
  parseRunRetryRecovery,
  retryRunProvesApplied,
  sameRunRetryIntent,
  serializeRunRetryRecovery,
  withAcceptedRetryRun,
  type ProductRunRetryRecoveryStore,
} from "./runRetryRecovery";

const PAGE_LIMIT = 100;
const MAX_REFRESH_PAGES = 512;
const MAX_REFRESH_RESOURCES = 20_000;
const MAX_COLLECTION_PAGES = 100;
const MAX_CONCURRENCY = 6;
const MAX_SSE_BUFFER_BYTES = 1_048_576 + 4;
const DEFAULT_RECONNECT_DELAYS_MS = [250, 500, 1_000, 2_000, 4_000] as const;
const RETRY_RECOVERY_BUSY_MESSAGE = "OpenEvo is updating local retry recovery state. Wait for it to finish.";
const RETRY_RECOVERY_RESTART_MESSAGE = "OpenEvo could not save local retry recovery state. Restart Desktop and try again.";

export interface LocalApiNativeBridge {
  selectProjectSource(intent: ProjectSourceSelectionIntent): Promise<unknown>;
  cancelProjectSource(actionId: string): Promise<unknown>;
  settleProjectSource(actionId: string, outcome: "adopt" | "discard"): Promise<unknown>;
}

export interface LocalApiDesktopProductProviderOptions {
  readonly client: DesktopApiClientV1;
  readonly native: LocalApiNativeBridge;
  readonly fetch?: FetchLike;
  readonly reconnectDelaysMs?: readonly number[];
  readonly retryRecoveryStore?: ProductRunRetryRecoveryStore | null;
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

export class LocalApiDesktopProductProvider implements ReleaseDesktopProductProvider {
  readonly providerKind = "desktop_sidecar" as const;
  readonly systemMaintenanceAvailable = false;

  private readonly client: DesktopApiClientV1;
  private readonly native: LocalApiNativeBridge;
  private readonly fetch: FetchLike;
  private readonly reconnectDelaysMs: readonly number[];
  private readonly continuationAuthority = new OperationContinuationAuthority();
  private readonly refreshOrder = new ProductRefreshOrder();
  private readonly listeners = new Set<(signal: ProductSubscriptionSignal) => void>();
  private snapshot: DesktopProductSnapshot | null = null;
  private epoch = 0;
  private lastEventId: string | null = null;
  private lastEventSequence: number | null = null;
  private streamAbort: AbortController | null = null;
  private streamPromise: Promise<void> | null = null;
  private waitingForRefresh = false;
  private readonly retryRecoveryStore: ProductRunRetryRecoveryStore | null;
  private retryReplay: ProductRunRetryRecovery | null;
  private retryRequestInFlight: ProductRunRetryRecovery | null = null;
  private retryRecoveryWriteInFlight = false;
  private retryRecoveryUncertain = false;

  constructor(options: LocalApiDesktopProductProviderOptions) {
    this.client = options.client;
    this.native = options.native;
    this.fetch = options.fetch ?? globalThis.fetch.bind(globalThis);
    this.reconnectDelaysMs = options.reconnectDelaysMs ?? DEFAULT_RECONNECT_DELAYS_MS;
    const retryRecoveryStore = options.retryRecoveryStore;
    if (!retryRecoveryStore) {
      throw new DesktopContractError("Persistent Desktop run retry recovery is unavailable");
    }
    this.retryRecoveryStore = retryRecoveryStore;
    this.retryReplay = this.restoreRunRetryRecovery();
  }

  async refresh(): Promise<ProductRefreshResult> {
    const refreshSequence = this.refreshOrder.begin();
    try {
      let snapshot = await this.loadSnapshot();
      if (!this.refreshOrder.isCurrent(refreshSequence)) {
        return { status: "stale", stream: { status: "stale", epoch: this.epoch, reason: "refresh_pending" } };
      }
      this.epoch += 1;
      const recovery = this.retryReplay;
      if (recovery) {
        const run = snapshot.runs.find((item) => item.id === recovery.runId);
        if (!this.retryRecoveryWriteInFlight
          && this.retryRequestInFlight !== recovery
          && run
          && retryRunProvesApplied(run, recovery)) {
          await this.setRunRetryRecovery(null);
        } else if (recovery.acceptedRun && snapshot.projects.some(
          (project) => project.remote?.core_project_id === recovery.projectId,
        )) {
          snapshot = { ...snapshot, runs: overlayAcceptedRetryRun(snapshot.runs, recovery) };
        }
      }
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
    const expected = profileCreateV1Schema.parse(input);
    const profile = await this.client.createProfile(input, { idempotencyKey: intent.actionId });
    assertProfileFields(profile, expected, "Created profile does not match the request");
    this.invalidate();
    return profile;
  }

  async updateProfile(
    profileId: string,
    input: Parameters<DesktopApiClientV1["updateProfile"]>[1],
    intent: ProductResourceMutationIntent,
  ): Promise<RemoteProfileV1> {
    this.assertIntent(intent);
    const expected = profilePatchV1Schema.parse(input);
    const profile = await this.client.updateProfile(profileId, input, { ifMatch: intent.etag });
    if (profile.profile_id !== profileId) {
      throw new DesktopContractError("Profile mutation returned the wrong profile");
    }
    assertProfileFields(profile, expected, "Updated profile does not match the request");
    this.invalidate();
    return profile;
  }

  async connectProfile(profileId: string, intent: ProductResourceMutationIntent): Promise<LocalOperationV1> {
    this.assertIntent(intent);
    const operation = await this.client.connectProfile(profileId, actionOptions(intent));
    assertLocalOperation(operation, "profile_connect", "profile", profileId);
    this.invalidate();
    return this.continuationAuthority.observeLocal(operation);
  }

  async acceptHostKey(
    profileId: string,
    input: Parameters<DesktopApiClientV1["acceptProfileHostKey"]>[1],
    intent: ProductResourceMutationIntent,
  ): Promise<LocalOperationV1> {
    this.assertIntent(intent);
    const operation = await this.client.acceptProfileHostKey(profileId, input, actionOptions(intent));
    assertLocalOperation(operation, "host_key_accept", "profile", profileId);
    this.invalidate();
    return this.continuationAuthority.observeLocal(operation);
  }

  async createProject(input: Parameters<DesktopApiClientV1["createProject"]>[0], intent: ProductMutationIntent): Promise<ProjectV1> {
    this.assertIntent(intent);
    const expected = projectCreateV1Schema.parse(input);
    const project = await this.client.createProject(input, { idempotencyKey: intent.actionId });
    assertProjectFields(project, expected, "Created project does not match the request");
    this.invalidate();
    return project;
  }

  async updateProject(
    projectId: string,
    input: Parameters<DesktopApiClientV1["updateProject"]>[1],
    intent: ProductResourceMutationIntent,
  ): Promise<ProjectV1> {
    this.assertIntent(intent);
    const expected = projectPatchV1Schema.parse(input);
    const project = await this.client.updateProject(projectId, input, { ifMatch: intent.etag });
    if (project.project_id !== projectId) {
      throw new DesktopContractError("Project mutation returned the wrong project");
    }
    assertProjectFields(project, expected, "Updated project does not match the request");
    this.invalidate();
    return project;
  }

  async activateProject(projectId: string, intent: ProductResourceMutationIntent): Promise<LocalOperationV1> {
    this.assertIntent(intent);
    const operation = await this.client.activateProject(projectId, actionOptions(intent));
    assertLocalOperation(operation, "project_activate", "project", projectId);
    this.invalidate();
    return this.continuationAuthority.observeLocal(operation);
  }

  async selectProjectSource(intent: ProjectSourceSelectionIntent): Promise<ProjectSourceV1> {
    this.assertIntent(intent);
    const source = projectSourceV1Schema.parse(await this.native.selectProjectSource(intent));
    assertProjectSource(source, intent.kind);
    return source;
  }

  async settleProjectSource(actionId: string, outcome: "adopt" | "discard"): Promise<void> {
    if (actionId.length < 16 || actionId.length > 256 || actionId.trim() !== actionId) {
      throw new DesktopContractError("Native project source action identity is invalid");
    }
    await this.native.settleProjectSource(actionId, outcome);
  }

  async cancelProjectSource(actionId: string): Promise<void> {
    if (actionId.length < 16 || actionId.length > 256 || actionId.trim() !== actionId) {
      throw new DesktopContractError("Native project source action identity is invalid");
    }
    await this.native.cancelProjectSource(actionId);
  }

  async startRun(intent: ProductRunIntent): Promise<RunV1> {
    this.assertIntent(intent);
    const expectedProjectId = this.snapshot?.projects.find(
      (project) => project.project_id === intent.projectId,
    )?.remote?.core_project_id;
    if (expectedProjectId === undefined) {
      throw new DesktopContractError("Run creation references a project without a remote identity");
    }
    const run = await this.client.createRun({ project_id: intent.projectId }, actionOptions(intent));
    if (run.project_id !== expectedProjectId) {
      throw new DesktopContractError("Run creation returned a run for another project");
    }
    this.invalidate();
    return run;
  }

  async cancelRun(runId: string, intent: ProductResourceMutationIntent): Promise<RunV1> {
    this.assertIntent(intent);
    const run = await this.client.cancelRun(runId, actionOptions(intent));
    this.assertKnownRunResponse(run, runId, "Run cancellation returned the wrong run");
    this.invalidate();
    return run;
  }

  async getRunLogs(runId: string): Promise<readonly LogEntryV1[]> {
    const snapshot = this.snapshot;
    const run = snapshot?.runs.find((item) => item.id === runId);
    if (!snapshot || !run) {
      throw new DesktopContractError("Run logs reference a run outside the current snapshot");
    }
    const logs = await collectPages(
      (options) => this.client.runLogs(runId, options),
      new RefreshBudget(),
      { sort: "sequence", direction: "asc" },
    );
    if (logs.some((entry) => entry.run_id !== runId)) {
      throw new DesktopContractError("Run log collection contains an entry for another run");
    }
    const attemptIds = new Set(run.attempts.map((attempt) => attempt.id));
    if (logs.some((entry) => entry.attempt_id !== null && !attemptIds.has(entry.attempt_id))) {
      throw new DesktopContractError("Run log collection references an attempt outside its run");
    }
    const serviceIds = new Set(snapshot.services.map((service) => service.id));
    if (logs.some((entry) => !serviceIds.has(entry.service_id))) {
      throw new DesktopContractError("Run log collection references an unknown service");
    }
    assertUniqueIdentity(logs, (entry) => entry.id, "Run log collection contains a duplicate identity");
    for (let index = 1; index < logs.length; index += 1) {
      if (logs[index]!.sequence <= logs[index - 1]!.sequence) {
        throw new DesktopContractError("Run log collection is not strictly ordered by sequence");
      }
    }
    return logs;
  }

  async retryRun(runId: string, intent: ProductResourceMutationIntent): Promise<RunV1> {
    this.assertRunRetryRecoveryHealthy();
    const exactReplay = this.retryReplay?.runId === runId
      && sameRunRetryIntent(this.retryReplay.intent, intent);
    if (this.retryReplay && !exactReplay) {
      throw new DesktopProductUserError(
        "OpenEvo is still reconciling an earlier session retry. Wait for it to finish before retrying another session.",
      );
    }
    if (this.retryRequestInFlight) {
      throw new DesktopProductUserError("OpenEvo is already sending this session retry.");
    }
    this.assertRunRetryRecoveryDispatchAvailable();
    let requestAuthority: ProductRunRetryRecovery;
    if (!exactReplay) {
      this.assertIntent(intent);
      const original = this.snapshot?.runs.find((run) => run.id === runId);
      if (!original || original.etag !== intent.etag) {
        throw new DesktopProductUserError("This session changed remotely. Refresh before retrying it.");
      }
      requestAuthority = createRunRetryRecovery(original, intent);
    } else {
      requestAuthority = this.retryReplay!;
    }
    const terminalAttemptId = requestAuthority.originalRun.current_attempt_id;
    if (terminalAttemptId === null) {
      throw new DesktopContractError("Run retry recovery has no terminal attempt identity");
    }
    // Claim synchronously so two callers cannot both cross the first durable
    // write boundary before either recovery record becomes visible.
    this.retryRequestInFlight = requestAuthority;
    if (!exactReplay) {
      try {
        await this.setRunRetryRecovery(requestAuthority);
      } catch (error) {
        if (this.retryRequestInFlight === requestAuthority) this.retryRequestInFlight = null;
        throw error;
      }
    }
    try {
      this.assertRunRetryRecoveryDispatchAvailable();
      const run = await this.client.retryRun(
        runId,
        { terminal_attempt_id: terminalAttemptId },
        actionOptions(intent),
      );
      this.assertKnownRunResponse(run, runId, "Run retry returned the wrong run");
      const recovery = this.retryReplay;
      if (!recovery || recovery.runId !== runId || !sameRunRetryIntent(recovery.intent, intent)) {
        throw new DesktopContractError("Run retry response lost its exact recovery authority");
      }
      const accepted = withAcceptedRetryRun(recovery, run);
      await this.setRunRetryRecovery(accepted);
      if (this.snapshot) {
        this.snapshot = { ...this.snapshot, runs: overlayAcceptedRetryRun(this.snapshot.runs, accepted) };
      }
      this.invalidate();
      return run;
    } catch (error) {
      if (this.retryRecoveryUncertain) {
        throw error instanceof DesktopProductUserError
          ? error
          : new DesktopProductUserError(RETRY_RECOVERY_RESTART_MESSAGE);
      }
      if (error instanceof DesktopApiError) {
        if (error.apiError.code === "core_mutation_outcome_unknown") {
          throw new DesktopProductAmbiguousMutationError(undefined, error);
        }
        if (this.retryReplay?.runId === runId && sameRunRetryIntent(this.retryReplay.intent, intent)) {
          await this.setRunRetryRecovery(null);
        }
        throw error;
      }
      throw error instanceof DesktopProductAmbiguousMutationError
        ? error
        : new DesktopProductAmbiguousMutationError(undefined, error);
    } finally {
      if (this.retryRequestInFlight === requestAuthority) this.retryRequestInFlight = null;
    }
  }

  getRunRetryRecovery(): ProductRunRetryRecovery | null {
    this.assertRunRetryRecoveryHealthy();
    return this.retryReplay === null ? null : structuredClone(this.retryReplay);
  }

  async getArtifactContent(artifactId: string): Promise<ArtifactContentV1> {
    const content = await this.client.artifactContent(artifactId);
    if (content.artifact_id !== artifactId) {
      throw new DesktopContractError("Artifact content returned the wrong artifact");
    }
    return content;
  }

  async getArtifactDiff(artifactId: string): Promise<ArtifactDiffV1> {
    const diff = await this.client.artifactDiff(artifactId);
    if (diff.artifact_id !== artifactId) {
      throw new DesktopContractError("Artifact diff returned the wrong artifact");
    }
    return diff;
  }

  async cancelOperation(operationId: string, intent: ProductResourceMutationIntent): Promise<LocalOperationV1> {
    this.assertIntent(intent);
    const operation = await this.client.cancelOperation(operationId, actionOptions(intent));
    if (operation.operation_id !== operationId) {
      throw new DesktopContractError("Operation cancellation returned the wrong operation");
    }
    this.invalidate();
    return this.continuationAuthority.observeLocal(operation);
  }

  async getLocalOperation(operationId: string): Promise<LocalOperationV1> {
    const operation = await this.client.getOperation(operationId);
    if (operation.operation_id !== operationId) {
      throw new DesktopContractError("Local operation lookup returned the wrong operation");
    }
    return this.continuationAuthority.observeLocal(operation);
  }

  async doctorProject(
    projectId: string,
    intent: ProductResourceMutationIntent,
  ): Promise<LocalOperationV1> {
    this.assertIntent(intent);
    const operation = await this.client.doctorProject(projectId, actionOptions(intent));
    assertLocalOperation(operation, "project_doctor", "project", projectId);
    this.invalidate();
    return this.continuationAuthority.observeLocal(operation);
  }

  async repairProject(
    projectId: string,
    intent: ProductResourceMutationIntent,
  ): Promise<LocalOperationV1> {
    this.assertIntent(intent);
    const operation = await this.client.repairProject(projectId, actionOptions(intent));
    assertLocalOperation(operation, "project_repair", "project", projectId);
    this.invalidate();
    return this.continuationAuthority.observeLocal(operation);
  }

  async restartService(
    serviceId: string,
    intent: ProductResourceMutationIntent,
  ): Promise<OperationV1> {
    this.assertIntent(intent);
    const operation = await this.client.restartService(serviceId, actionOptions(intent));
    assertCoreOperation(operation, "service_restart");
    if (operation.request.kind !== "service_restart" || operation.request.service_id !== serviceId) {
      throw new DesktopContractError("Service restart returned an operation for another service");
    }
    this.invalidate();
    return this.continuationAuthority.observeCore(operation);
  }

  async getCoreOperation(operationId: string): Promise<OperationV1> {
    const operation = await this.client.getCoreOperation(operationId);
    if (operation.id !== operationId) {
      throw new DesktopContractError("Core operation lookup returned the wrong operation");
    }
    return this.continuationAuthority.observeCore(operation);
  }

  async createDiagnostic(
    input: DiagnosticCreateV1,
    intent: ProductMutationIntent,
  ): Promise<DiagnosticReportV1> {
    this.assertIntent(intent);
    const diagnostic = await this.client.createDiagnostic(input, {
      idempotencyKey: intent.actionId,
    });
    if (JSON.stringify(diagnostic.scopes) !== JSON.stringify(input.scopes)
      || JSON.stringify(diagnostic.target) !== JSON.stringify(input.target)) {
      throw new DesktopContractError("Diagnostic report does not match the requested scope");
    }
    this.invalidate();
    return this.continuationAuthority.observeDiagnostic(diagnostic);
  }

  async getDiagnostic(diagnosticId: string): Promise<DiagnosticReportV1> {
    const diagnostic = await this.client.getDiagnostic(diagnosticId);
    if (diagnostic.id !== diagnosticId) {
      throw new DesktopContractError("Diagnostic lookup returned the wrong report");
    }
    return this.continuationAuthority.observeDiagnostic(diagnostic);
  }

  async cleanupCaches(
    input: CacheCleanupRequestV1,
    intent: ProductMutationIntent,
  ): Promise<OperationV1> {
    this.assertIntent(intent);
    const operation = await this.client.cleanupMaintenanceCache(input, {
      idempotencyKey: intent.actionId,
    });
    assertCoreOperation(operation, "cache_cleanup");
    if (operation.request.kind !== "cache_cleanup"
      || JSON.stringify(operation.request.request) !== JSON.stringify(input)) {
      throw new DesktopContractError("Cache cleanup operation does not match the request");
    }
    this.invalidate();
    return this.continuationAuthority.observeCore(operation);
  }

  private async loadSnapshot(): Promise<Omit<DesktopProductSnapshot, "stream">> {
    const budget = new RefreshBudget();
    const [state, profiles, projects] = await Promise.all([
      this.client.state(),
      collectPages((options) => this.client.listProfiles(options), budget),
      collectPages((options) => this.client.listProjects(options), budget),
    ]);
    const [runSummaries, services] = hasReadableCoreCollections(state)
      ? await Promise.all([
        collectPages((options) => this.client.listRuns(options), budget),
        collectPages((options) => this.client.listServices(options), budget),
      ])
      : [[], []] as const;

    assertUniqueIdentity(profiles, (profile) => profile.profile_id, "Desktop profile collection contains a duplicate identity");
    assertUniqueIdentity(projects, (project) => project.project_id, "Desktop project collection contains a duplicate identity");
    assertUniqueIdentity(runSummaries, (run) => run.id, "Desktop run collection contains a duplicate identity");
    assertUniqueIdentity(services, (service) => service.id, "Desktop service collection contains a duplicate identity");
    const profileIds = new Set(profiles.map((profile) => profile.profile_id));
    if (projects.some((project) => !profileIds.has(project.profile_id))) {
      throw new DesktopContractError("Desktop project collection references an unknown profile");
    }
    if (state.core.profile_id !== null && !profileIds.has(state.core.profile_id)) {
      throw new DesktopContractError("Desktop state references an unknown profile");
    }
    const coreProjectIds = new Set<string>();
    for (const project of projects) {
      const coreProjectId = project.remote?.core_project_id;
      if (coreProjectId !== undefined && coreProjectIds.has(coreProjectId)) {
        throw new DesktopContractError("Desktop projects reuse a remote project identity");
      }
      if (coreProjectId !== undefined) coreProjectIds.add(coreProjectId);
    }

    budget.consumeResources(runSummaries.length);
    const runs = await mapLimited(runSummaries, MAX_CONCURRENCY, async (summary) => {
      const run = await this.client.getRun(summary.id);
      assertRunIdentity(run, summary.id, "Run detail identity does not match its summary");
      const { attempts: _attempts, ...detailSummary } = run;
      if (!sameJson(detailSummary, summary)) {
        throw new DesktopContractError("Run detail does not match its summary");
      }
      if (!projects.some((project) => project.remote?.core_project_id === run.project_id)) {
        throw new DesktopContractError("Desktop run collection references an unknown project");
      }
      return run;
    });

    const serviceIds = new Set(services.map((service) => service.id));
    const timelines: Record<string, readonly TimelineEntryV1[]> = {};
    const artifactGroups = await mapLimited(runs, MAX_CONCURRENCY, async (run) => {
      const timeline = await collectPages((options) => this.client.runTimeline(run.id, options), budget);
      if (timeline.some((entry) => entry.run_id !== run.id)) {
        throw new DesktopContractError("Run timeline contains an entry for another run");
      }
      assertUniqueIdentity(timeline, (entry) => entry.id, "Run timeline contains a duplicate identity");
      const attemptIds = new Set(run.attempts.map((attempt) => attempt.id));
      if (timeline.some((entry) => entry.attempt_id !== null && !attemptIds.has(entry.attempt_id))) {
        throw new DesktopContractError("Run timeline references an attempt outside its run");
      }
      if (timeline.some((entry) => !serviceIds.has(entry.service_id))) {
        throw new DesktopContractError("Run timeline references an unknown service");
      }
      timelines[run.id] = timeline;
      try {
        const artifacts = await collectPages((options) => this.client.runArtifacts(run.id, options), budget);
        if (artifacts.some((artifact) => artifact.run_id !== run.id)) {
          throw new DesktopContractError("Run artifact collection contains an artifact for another run");
        }
        if (artifacts.some((artifact) => artifact.project_id !== run.project_id)) {
          throw new DesktopContractError("Run artifact collection contains an artifact for another project");
        }
        assertUniqueIdentity(artifacts, (artifact) => artifact.id, "Run artifact collection contains a duplicate identity");
        return { complete: true as const, artifacts };
      } catch (error) {
        if (!(error instanceof TypeError)) throw error;
        return { complete: false as const, artifacts: [] as ArtifactV1[] };
      }
    });

    const artifactsComplete = artifactGroups.every((group) => group.complete);
    const artifacts = artifactsComplete
      ? deduplicateArtifacts(artifactGroups.flatMap((group) => group.artifacts))
      : [];
    if (artifactsComplete) {
      const artifactsById = new Map(artifacts.map((artifact) => [artifact.id, artifact]));
      for (const [runId, timeline] of Object.entries(timelines)) {
        if (timeline.some((entry) => entry.artifact_ids.some(
          (artifactId) => artifactsById.get(artifactId)?.run_id !== runId,
        ))) {
          throw new DesktopContractError("Run timeline references an artifact outside the refreshed run collection");
        }
      }
    }
    const activeOperation = await this.loadActiveOperation(state.pending_operation_ids, budget);
    const { capability, validation } = await this.loadProjectAuthority(state, projects, profiles);
    return {
      state,
      executionModeCapabilities: state.execution_mode_capabilities,
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
      activeOperation,
    };
  }

  private async loadActiveOperation(operationIds: readonly string[], budget: RefreshBudget): Promise<LocalOperationV1 | null> {
    const ids = [...new Set(operationIds)].sort();
    if (ids.length === 0) return null;
    budget.consumeResources(ids.length);
    const operations = await mapLimited(ids, MAX_CONCURRENCY, async (operationId) => {
      const operation = await this.client.getOperation(operationId);
      if (operation.operation_id !== operationId) {
        throw new DesktopContractError("Pending operation identity does not match Desktop state");
      }
      return operation;
    });
    const active = operations.find(
      (operation) => !["succeeded", "failed", "cancelled"].includes(operation.state),
    );
    return active === undefined ? null : this.continuationAuthority.observeLocal(active);
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

  private assertKnownRunResponse(run: RunV1, runId: string, message: string): void {
    assertRunIdentity(run, runId, message);
    const existing = this.snapshot?.runs.find((item) => item.id === runId);
    if (!existing || existing.project_id !== run.project_id) {
      throw new DesktopContractError(message);
    }
  }

  private restoreRunRetryRecovery(): ProductRunRetryRecovery | null {
    let value: string | null;
    try {
      value = this.retryRecoveryStore?.read() ?? null;
    } catch (error) {
      throw new DesktopContractError("Saved run retry recovery state could not be read", { cause: error });
    }
    if (value === null) return null;
    try {
      return parseRunRetryRecovery(value);
    } catch (error) {
      throw new DesktopContractError("Saved run retry recovery state is invalid", { cause: error });
    }
  }

  private async setRunRetryRecovery(recovery: ProductRunRetryRecovery | null): Promise<void> {
    if (this.retryRecoveryWriteInFlight) {
      throw new DesktopProductUserError(RETRY_RECOVERY_BUSY_MESSAGE);
    }
    this.retryRecoveryWriteInFlight = true;
    try {
      await this.retryRecoveryStore?.write(recovery === null ? null : serializeRunRetryRecovery(recovery));
      this.retryReplay = recovery;
    } catch {
      this.retryRecoveryUncertain = true;
      throw new DesktopProductUserError(RETRY_RECOVERY_RESTART_MESSAGE);
    } finally {
      this.retryRecoveryWriteInFlight = false;
    }
  }

  private assertRunRetryRecoveryHealthy(): void {
    if (this.retryRecoveryUncertain) {
      throw new DesktopProductUserError(RETRY_RECOVERY_RESTART_MESSAGE);
    }
  }

  private assertRunRetryRecoveryDispatchAvailable(): void {
    this.assertRunRetryRecoveryHealthy();
    if (this.retryRecoveryWriteInFlight) {
      throw new DesktopProductUserError(RETRY_RECOVERY_BUSY_MESSAGE);
    }
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

function hasReadableCoreCollections(
  state: Awaited<ReturnType<DesktopApiClientV1["state"]>>,
): boolean {
  const active = state.active_project;
  return active !== null
    && active.connection_state === "ready"
    && state.core.active_tunnel
    && state.core.core !== null
    && state.core.profile_id === active.profile_id
    && ["online", "degraded"].includes(state.core.state);
}

export function createLocalApiDesktopProductProvider(
  options: LocalApiDesktopProductProviderOptions,
): ReleaseDesktopProductProvider {
  return new LocalApiDesktopProductProvider(options);
}

async function collectPages<T>(
  load: (options: ListRequestOptions) => Promise<PageV1<T>>,
  budget: RefreshBudget,
  baseOptions: Omit<ListRequestOptions, "limit" | "after"> = {},
): Promise<T[]> {
  const items: T[] = [];
  const cursors = new Set<string>();
  let after: string | undefined;
  for (let pageNumber = 0; pageNumber < MAX_COLLECTION_PAGES; pageNumber += 1) {
    const page = await load({
      limit: PAGE_LIMIT,
      ...baseOptions,
      ...(after === undefined ? {} : { after }),
    });
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

type ProfileFields = Pick<RemoteProfileV1, "name" | "host" | "port" | "user" | "authentication_kind" | "proxy">;

function assertProfileFields(
  profile: RemoteProfileV1,
  expected: Partial<ProfileFields>,
  message: string,
): void {
  if ((expected.name !== undefined && profile.name !== expected.name)
    || (expected.host !== undefined && profile.host !== expected.host)
    || (expected.port !== undefined && profile.port !== expected.port)
    || (expected.user !== undefined && profile.user !== expected.user)
    || (expected.authentication_kind !== undefined && profile.authentication_kind !== expected.authentication_kind)
    || (expected.proxy !== undefined && !sameJson(profile.proxy, expected.proxy))) {
    throw new DesktopContractError(message);
  }
}

type ProjectFields = Pick<
  ProjectV1,
  "name" | "profile_id" | "task" | "source" | "execution" | "evolution" | "evolution_configuration_state"
>;

function assertProjectFields(
  project: ProjectV1,
  expected: Partial<ProjectFields>,
  message: string,
): void {
  if ((expected.name !== undefined && project.name !== expected.name)
    || (expected.profile_id !== undefined && project.profile_id !== expected.profile_id)
    || (expected.task !== undefined && !sameJson(project.task, expected.task))
    || (expected.source !== undefined && !sameJson(project.source, expected.source))
    || (expected.execution !== undefined && !sameJson(project.execution, expected.execution))
    || (expected.evolution !== undefined && !sameJson(project.evolution, expected.evolution))
    || (expected.evolution_configuration_state !== undefined
      && project.evolution_configuration_state !== expected.evolution_configuration_state)) {
    throw new DesktopContractError(message);
  }
}

function assertLocalOperation(
  operation: LocalOperationV1,
  operationKind: LocalOperationV1["operation_kind"],
  resourceType: LocalOperationV1["resource"]["resource_type"],
  resourceId: string,
): void {
  if (operation.operation_kind !== operationKind
    || operation.resource.resource_type !== resourceType
    || operation.resource.resource_id !== resourceId) {
    throw new DesktopContractError("Desktop action returned an operation for another resource");
  }
  if (operation.result !== null) {
    const matchesResult = resourceType === "profile"
      ? operation.result.kind === "connection" && operation.result.profile_id === resourceId
      : resourceType === "project"
        ? operation.result.kind === "project" && operation.result.project_id === resourceId
        : false;
    if (!matchesResult) {
      throw new DesktopContractError("Desktop action result does not match its resource");
    }
  }
}

function assertCoreOperation(
  operation: OperationV1,
  kind: OperationV1["kind"],
): void {
  if (
    operation.kind !== kind
    || operation.descriptor.kind !== kind
    || operation.request.kind !== kind
  ) {
    throw new DesktopContractError("Core action returned an operation of another kind");
  }
}

function assertProjectSource(source: ProjectSourceV1, expectedKind: ProjectSourceSelectionIntent["kind"]): void {
  if (source.kind !== expectedKind || source.import_ref === null) {
    throw new DesktopContractError("Native project source does not match the requested kind");
  }
}

function assertRunIdentity(run: RunV1, runId: string, message: string): void {
  if (run.id !== runId) throw new DesktopContractError(message);
}

function assertUniqueIdentity<T>(values: readonly T[], identity: (value: T) => string, message: string): void {
  const ids = new Set<string>();
  for (const value of values) {
    const id = identity(value);
    if (ids.has(id)) throw new DesktopContractError(message);
    ids.add(id);
  }
}

function sameJson(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
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
