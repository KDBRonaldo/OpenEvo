import { z } from "zod";
import type {
  DesktopApiClientV2,
  FetchLikeV2,
  ListRequestOptionsV2,
} from "../api/v2/client";
import {
  DesktopApiErrorV2,
  DesktopContractErrorV2,
} from "../api/v2/client";
import {
  compareUtcTimestampsV2,
  lifecycleOperationV2Schema,
  opaqueIdV2Schema,
  scienceProjectConfigV2Schema,
  type CoreEventEnvelopeV2,
  type DesktopErrorV2,
  type DesktopEventEnvelopeV2,
  type DesktopStateV2,
  type LifecycleOperationV2,
  type LocalOperationV2,
  type OperationV2,
  type HostKeyReviewRequestV2,
  type ProjectV2,
  type RemoteProfileV2,
  type RemoteWorkspaceProfileV2,
  type ScienceProjectConfigV2,
  type ServiceV2,
  type SuccessorTransitionV2,
  type TaskV2,
} from "../api/v2/schemas";
import {
  DesktopEventReplayAuthorityV2,
  parseEventStreamFailureV2,
  parseSseFrameV2,
} from "../api/v2/sse";
import type {
  DesktopProductProviderV2,
  DesktopProductSnapshotV2,
  NativeWorkspaceSelectionIntentV2,
  NativeWorkspaceSourceV2,
  ProductMutationIntentV2,
  ProductOperationV2,
  ProductRefreshResultV2,
  ProductStreamStateV2,
  ProductSubscriptionSignalV2,
  ProjectDraftV2,
} from "./providerV2";

const PAGE_LIMIT = 100;
const MAX_COLLECTION_PAGES = 100;
const MAX_REFRESH_RESOURCES = 20_000;
const MAX_SSE_BUFFER_BYTES = 1_048_580;
const DEFAULT_RECONNECT_DELAYS_MS = [250, 500, 1_000, 2_000, 4_000] as const;

export interface LocalApiNativeBridgeV2 {
  selectProjectSource(intent: NativeWorkspaceSelectionIntentV2): Promise<unknown>;
  cancelProjectSource(actionId: string): Promise<unknown>;
  settleProjectSource(actionId: string, outcome: "adopt" | "discard"): Promise<unknown>;
}

export interface LocalApiDesktopProductProviderOptionsV2 {
  readonly client: DesktopApiClientV2;
  readonly native: LocalApiNativeBridgeV2;
  readonly featureFlags: readonly string[];
  readonly fetch?: FetchLikeV2;
  readonly reconnectDelaysMs?: readonly number[];
}

export class LocalApiDesktopProductProviderV2 implements DesktopProductProviderV2 {
  readonly apiVersion = 2 as const;
  readonly providerKind = "desktop_sidecar" as const;
  readonly featureFlags: readonly string[];

  private readonly client: DesktopApiClientV2;
  private readonly native: LocalApiNativeBridgeV2;
  private readonly fetch: FetchLikeV2;
  private readonly reconnectDelaysMs: readonly number[];
  private readonly listeners = new Set<(signal: ProductSubscriptionSignalV2) => void>();
  private readonly replay = new DesktopEventReplayAuthorityV2();
  private refreshSequence = 0;
  private epoch = 0;
  private snapshot: DesktopProductSnapshotV2 | null = null;
  private validation: DesktopProductSnapshotV2["validation"] = null;
  private activeOperation: ProductOperationV2 | null = null;
  private streamAbort: AbortController | null = null;
  private streamPromise: Promise<void> | null = null;
  private waitingForRefresh = false;

  constructor(options: LocalApiDesktopProductProviderOptionsV2) {
    this.client = options.client;
    this.native = options.native;
    this.featureFlags = Object.freeze([...options.featureFlags]);
    this.fetch = options.fetch ?? globalThis.fetch.bind(globalThis);
    this.reconnectDelaysMs = options.reconnectDelaysMs ?? DEFAULT_RECONNECT_DELAYS_MS;
  }

  async refresh(): Promise<ProductRefreshResultV2> {
    const sequence = ++this.refreshSequence;
    try {
      const loaded = await this.loadSnapshot();
      if (sequence !== this.refreshSequence) {
        return { status: "stale", stream: { status: "stale", epoch: this.epoch, reason: "refresh_pending" } };
      }
      this.epoch += 1;
      const snapshot: DesktopProductSnapshotV2 = {
        ...loaded,
        activeOperation: this.activeOperation,
        stream: { status: "fresh", epoch: this.epoch, lastEventId: this.replay.lastEventId },
      };
      this.snapshot = snapshot;
      this.waitingForRefresh = false;
      this.ensureEventStream();
      return { status: "fresh", snapshot };
    } catch (error) {
      if (sequence !== this.refreshSequence) {
        return { status: "stale", stream: { status: "stale", epoch: this.epoch, reason: "refresh_pending" } };
      }
      const stream: Extract<ProductStreamStateV2, { status: "error" }> = {
        status: "error",
        epoch: this.epoch,
        error: apiErrorOfV2(error),
      };
      this.setSnapshotStream(stream);
      return { status: "error", stream };
    }
  }

  subscribe(listener: (signal: ProductSubscriptionSignalV2) => void): () => void {
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

  async rescanSshHosts(intent: ProductMutationIntentV2) {
    const snapshot = this.requireIntent(intent);
    const catalog = await this.client.rescanSshHosts({ schema_version: "2" }, {
      resourceGeneration: snapshot.catalog.catalog_generation,
      idempotencyKey: intent.actionId,
    });
    this.invalidate();
    return catalog;
  }

  async createProfile(displayName: string, sshHostAlias: string, intent: ProductMutationIntentV2) {
    const snapshot = this.requireIntent(intent);
    const profile = await this.client.createProfile({
      schema_version: "2",
      display_name: displayName,
      connection_authority: "system_openssh",
      ssh_host_alias: sshHostAlias,
    }, {
      resourceGeneration: snapshot.catalog.catalog_generation,
      idempotencyKey: intent.actionId,
    });
    this.invalidate();
    return profile;
  }

  async renameProfile(profileId: string, input: { schema_version?: "2"; display_name: string }, intent: ProductMutationIntentV2) {
    const profile = this.requireProfile(profileId, intent);
    const result = await this.client.updateProfile(profileId, input, {
      resourceGeneration: profile.profile_kind === "system_openssh" ? profile.connection_generation : 0,
      ifMatch: profile.etag,
      idempotencyKey: intent.actionId,
    });
    this.invalidate();
    return result;
  }

  async deleteProfile(profileId: string, intent: ProductMutationIntentV2): Promise<void> {
    const profile = this.requireProfile(profileId, intent);
    await this.client.deleteProfile(profileId, {
      resourceGeneration: profile.profile_kind === "system_openssh" ? profile.connection_generation : 0,
      ifMatch: profile.etag,
      idempotencyKey: intent.actionId,
    });
    this.invalidate();
  }

  async rebindProfile(profileId: string, sshHostAlias: string, intent: ProductMutationIntentV2) {
    const snapshot = this.requireIntent(intent);
    const profile = snapshot.profiles.find((candidate) => candidate.profile_id === profileId);
    if (profile?.profile_kind !== "legacy_explicit") throw new DesktopContractErrorV2("Only a retained Preview profile can be rebound");
    const result = await this.client.rebindProfile(profileId, {
      schema_version: "2",
      connection_authority: "system_openssh",
      ssh_host_alias: sshHostAlias,
      catalog_generation: snapshot.catalog.catalog_generation,
    }, {
      resourceGeneration: 0,
      ifMatch: profile.etag,
      idempotencyKey: intent.actionId,
    });
    this.invalidate();
    return result;
  }

  async connectProfile(profileId: string, intent: ProductMutationIntentV2) {
    const profile = this.requireSystemProfile(profileId, intent);
    return this.observeOperation(await this.client.connectProfile(profileId, {
      schema_version: "2",
      expected_connection_generation: profile.connection_generation,
    }, profileMutation(profile, intent)));
  }

  async disconnectProfile(profileId: string, intent: ProductMutationIntentV2) {
    const profile = this.requireSystemProfile(profileId, intent);
    return this.observeOperation(await this.client.disconnectProfile(profileId, {
      schema_version: "2",
      expected_connection_generation: profile.connection_generation,
    }, profileMutation(profile, intent)));
  }

  async reviewHostKey(profileId: string, action: HostKeyReviewRequestV2["action"], intent: ProductMutationIntentV2) {
    const profile = this.requireSystemProfile(profileId, intent);
    const reviewId = profile.trust.review_id;
    const reviewSha256 = profile.trust.review_sha256;
    if (reviewId === null || reviewSha256 === null) throw new DesktopContractErrorV2("Profile has no current host-key review authority");
    return this.observeOperation(await this.client.reviewProfileHostKey(profileId, {
      schema_version: "2",
      expected_connection_generation: profile.connection_generation,
      review_id: reviewId,
      review_sha256: reviewSha256,
      action,
    }, profileMutation(profile, intent)));
  }

  async selectNativeWorkspace(intent: NativeWorkspaceSelectionIntentV2): Promise<NativeWorkspaceSourceV2> {
    this.requireIntent(intent);
    const operation = lifecycleOperationV2Schema.parse(await this.native.selectProjectSource(intent));
    if (operation.kind !== "native_workspace_prepare"
      || operation.resource.resource_kind !== "native_workspace") {
      throw new DesktopContractErrorV2("Native workspace selection returned another lifecycle operation");
    }
    const terminal = await this.waitForLifecycleTerminal(this.observeOperation(operation));
    if (terminal.status !== "succeeded" || terminal.result?.result_kind !== "native_workspace") {
      throw lifecycleTerminalError(terminal, "Native workspace preparation did not succeed");
    }
    return { kind: "native_folder_snapshot", display_name: terminal.result.display_name };
  }

  async cancelNativeWorkspace(actionId: string): Promise<void> {
    await this.native.cancelProjectSource(actionIdV2(actionId));
  }

  async settleNativeWorkspace(actionId: string, outcome: "adopt" | "discard"): Promise<void> {
    await this.native.settleProjectSource(actionIdV2(actionId), outcome);
  }

  async createProject(draft: ProjectDraftV2, intent: ProductMutationIntentV2) {
    const profile = this.requireSystemProfile(draft.profileId, intent);
    if (profile.connection_state !== "connected") throw new DesktopContractErrorV2("Project creation requires a connected system-OpenSSH profile");
    const config = scienceProjectConfigV2Schema.parse(draft.config);
    const operation = this.observeOperation(await this.client.createProject({
      schema_version: "2",
      profile_id: profile.profile_id,
      profile_connection_generation: profile.connection_generation,
      display_name: draft.displayName,
      config,
    }, {
      resourceGeneration: profile.connection_generation,
      idempotencyKey: intent.actionId,
    }));
    const terminal = await this.waitForLifecycleTerminal(operation);
    if (terminal.status !== "succeeded" || terminal.result?.result_kind !== "project") {
      throw lifecycleTerminalError(terminal, "Remote project creation did not succeed");
    }
    const projectId = terminal.result.project_id;
    const projects = await collectPages((options) => this.client.listProjects(options));
    const project = projects.find((candidate) => candidate.project_id === projectId);
    if (project === undefined) {
      throw new DesktopContractErrorV2("Project creation result is absent from remote authority");
    }
    this.invalidate();
    return project;
  }

  async updateProject(projectId: string, displayName: string, configInput: ScienceProjectConfigV2, intent: ProductMutationIntentV2) {
    const project = this.requireProject(projectId, intent);
    const config = scienceProjectConfigV2Schema.parse(configInput);
    const head = project.active_project_head;
    const result = await this.client.updateProject(projectId, {
      schema_version: "2",
      expected_project_head_id: head?.project_head_id ?? null,
      expected_project_head_manifest_sha256: head?.manifest_sha256 ?? null,
      expected_project_config_sha256: project.project_config_sha256,
      display_name: displayName,
      config,
    }, projectMutation(project, intent));
    this.validation = null;
    this.invalidate();
    return result;
  }

  async activateProject(projectId: string, intent: ProductMutationIntentV2) {
    const project = this.requireProject(projectId, intent);
    const head = requireProjectHead(project);
    return this.observeOperation(await this.client.activateProject(projectId, {
      schema_version: "2",
      expected_project_head_id: head.project_head_id,
      expected_project_head_manifest_sha256: head.manifest_sha256,
    }, projectMutation(project, intent)));
  }

  async loadProjectCapabilities(projectId: string) {
    const snapshot = this.requireSnapshot();
    const project = snapshot.projects.find((candidate) => candidate.project_id === projectId);
    if (!project) throw new DesktopContractErrorV2("Project capabilities reference an unknown project");
    const capability = await this.client.projectCapabilities(projectId);
    this.snapshot = this.snapshot === null ? null : { ...this.snapshot, capability };
    return capability;
  }

  async validateProject(projectId: string, intent: ProductMutationIntentV2) {
    const project = this.requireProject(projectId, intent);
    const head = requireProjectHead(project);
    const capability = this.requireSnapshot().capability;
    if (capability === null || capability.project_id !== projectId || capability.execution_mode !== project.config.execution.mode) {
      throw new DesktopContractErrorV2("Project validation requires current remote capabilities");
    }
    const validation = await this.client.validateProject(projectId, {
      schema_version: "2",
      expected_project_head_id: head.project_head_id,
      expected_project_head_manifest_sha256: head.manifest_sha256,
      expected_project_config_sha256: project.project_config_sha256,
      capability_registry_sha256: capability.registry_sha256,
    }, projectMutation(project, intent));
    this.validation = validation;
    if (this.snapshot !== null) this.snapshot = { ...this.snapshot, validation };
    return validation;
  }

  async submitTask(projectId: string, intent: ProductMutationIntentV2) {
    const project = this.requireProject(projectId, intent);
    const head = requireProjectHead(project);
    if (project.state !== "ready" || project.admission_etag === null) {
      throw new DesktopContractErrorV2("Project successor is not ready for task admission");
    }
    const capability = this.requireSnapshot().capability;
    const validation = this.validation;
    if (capability === null || validation === null || validation.project_id !== projectId
      || validation.registry_sha256 !== capability.registry_sha256 || !validation.valid) {
      throw new DesktopContractErrorV2("Project must pass current remote validation before task admission");
    }
    const task = await this.client.submitTask({
      schema_version: "2",
      project_id: projectId,
      expected_project_admission_etag: project.admission_etag,
      expected_project_head_id: head.project_head_id,
      expected_project_head_manifest_sha256: head.manifest_sha256,
      expected_project_config_sha256: project.project_config_sha256,
    }, {
      resourceGeneration: head.generation,
      idempotencyKey: intent.actionId,
    });
    this.invalidate();
    return task;
  }

  async cancelTask(taskId: string, intent: ProductMutationIntentV2) {
    const task = this.requireTask(taskId, intent);
    return this.observeOperation(await this.client.cancelTask(taskId, taskAction(task), taskMutation(task, intent)));
  }

  async retryTask(taskId: string, intent: ProductMutationIntentV2) {
    const task = this.requireTask(taskId, intent);
    return this.observeOperation(await this.client.retryTask(taskId, taskAction(task), taskMutation(task, intent)));
  }

  async getProjectHead(projectHeadId: string) {
    return this.client.getProjectHead(projectHeadId);
  }

  async getEvolutionRevision(evolutionRevisionId: string) {
    return this.client.getEvolutionRevision(evolutionRevisionId);
  }

  async getRuntimeContext(runtimeContextSnapshotId: string) {
    return this.client.getRuntimeContext(runtimeContextSnapshotId);
  }

  async retryTransition(transitionId: string, intent: ProductMutationIntentV2) {
    const { transition, project } = this.requireTransition(transitionId, intent);
    return this.observeOperation(await this.client.retryTransition(transitionId, transitionAction(transition), transitionMutation(transition, project, intent)));
  }

  async replaceTransition(transitionId: string, intent: ProductMutationIntentV2) {
    const { transition, project } = this.requireTransition(transitionId, intent);
    return this.observeOperation(await this.client.replaceTransition(transitionId, {
      ...transitionAction(transition),
      replacement_plan_sha256: transition.transition.plan_sha256,
    }, transitionMutation(transition, project, intent)));
  }

  async abandonTransition(transitionId: string, intent: ProductMutationIntentV2) {
    const { transition, project } = this.requireTransition(transitionId, intent);
    return this.observeOperation(await this.client.abandonTransition(transitionId, transitionAction(transition), transitionMutation(transition, project, intent)));
  }

  async getArtifactContent(artifactId: string) {
    this.requireSnapshot().artifacts.find((artifact) => artifact.artifact_id === artifactId) ?? fail("Artifact content references an unknown artifact");
    return this.client.artifactContent(artifactId);
  }

  async getArtifactDiff(artifactId: string, previousArtifactId?: string) {
    this.requireSnapshot().artifacts.find((artifact) => artifact.artifact_id === artifactId) ?? fail("Artifact diff references an unknown artifact");
    return this.client.artifactDiff(artifactId, previousArtifactId === undefined ? undefined : { previousArtifactId });
  }

  async restartService(serviceId: string, intent: ProductMutationIntentV2) {
    const snapshot = this.requireIntent(intent);
    const service = snapshot.services.find((candidate) => candidate.service_id === serviceId);
    if (!service) throw new DesktopContractErrorV2("Service restart references an unknown service");
    const profile = activeConnectedProfile(snapshot);
    return this.observeOperation(await this.client.restartService(serviceId, {
      schema_version: "2",
      expected_service_id: serviceId,
    }, {
      resourceGeneration: profile.connection_generation,
      ifMatch: service.etag,
      idempotencyKey: intent.actionId,
    }));
  }

  async createDiagnostic(
    input: { scope: "system" | "project" | "task" | "transition" | "service"; resource_id: string | null },
    intent: ProductMutationIntentV2,
  ) {
    const snapshot = this.requireIntent(intent);
    const profile = activeConnectedProfile(snapshot);
    const diagnostic = await this.client.createDiagnostic({
      schema_version: "2",
      profile_id: profile.profile_id,
      profile_connection_generation: profile.connection_generation,
      scope: input.scope,
      resource_id: input.resource_id,
    }, {
      resourceGeneration: profile.connection_generation,
      idempotencyKey: intent.actionId,
    });
    this.invalidate();
    return diagnostic;
  }

  async getDiagnostic(diagnosticId: string) {
    return this.client.getDiagnostic(diagnosticId);
  }

  private async loadSnapshot(): Promise<Omit<DesktopProductSnapshotV2, "activeOperation" | "stream">> {
    const [state, catalog, profiles] = await Promise.all([
      this.client.state(),
      this.client.listSshHosts(),
      collectPages((options) => this.client.listProfiles(options)),
    ]);
    assertProfileAuthority(state, profiles);
    if (state.active_project_id === null) {
      this.validation = null;
      return {
        state,
        catalog,
        profiles,
        projects: [],
        tasks: [],
        transitions: {},
        timelines: {},
        artifacts: [],
        services: [],
        capability: null,
        validation: null,
      };
    }
    activeConnectedProfile({ state, profiles });
    const [projects, tasks, services] = await Promise.all([
      collectPages((options) => this.client.listProjects(options)),
      collectPages((options) => this.client.listTasks({ ...options, projectId: state.active_project_id! })),
      collectPages((options) => this.client.listServices(options)),
    ]);
    const activeProject = projects.find((project) => project.project_id === state.active_project_id);
    if (!activeProject) throw new DesktopContractErrorV2("Active project is absent from the remote project collection");
    if (projects.some((project) => project.project_id !== state.active_project_id)) {
      throw new DesktopContractErrorV2("Active project tunnel returned another project");
    }
    const [capability, taskDetails] = await Promise.all([
      this.client.projectCapabilities(activeProject.project_id),
      Promise.all(tasks.map(async (task) => {
        const [timeline, transition] = await Promise.all([
          collectPages((options) => this.client.taskTimeline(task.task_id, options)),
          task.successor_transition === null
            ? Promise.resolve(null)
            : this.client.getTransition(task.successor_transition.successor_transition_id),
        ]);
        return { task, timeline, transition };
      })),
    ]);
    const timelines: Record<string, readonly CoreEventEnvelopeV2[]> = {};
    const transitions: Record<string, SuccessorTransitionV2> = {};
    for (const detail of taskDetails) {
      timelines[detail.task.task_id] = detail.timeline;
      if (detail.transition !== null) transitions[detail.transition.transition.successor_transition_id] = detail.transition;
    }
    if (this.validation !== null && (
      this.validation.project_id !== activeProject.project_id
      || this.validation.registry_sha256 !== capability.registry_sha256
    )) this.validation = null;
    return {
      state,
      catalog,
      profiles,
      projects,
      tasks,
      transitions,
      timelines,
      // v0.1.9 intentionally exposes successor Evolution Revision counts while
      // Core v2 artifact inspection remains unavailable. Never probe a missing
      // route or fall back to SSH/v1 during an ordinary snapshot refresh.
      artifacts: [],
      services,
      capability,
      validation: this.validation,
    };
  }

  private requireSnapshot(): DesktopProductSnapshotV2 {
    if (this.snapshot === null || this.snapshot.stream.status !== "fresh") throw new DesktopContractErrorV2("Desktop v2 snapshot is not current");
    return this.snapshot;
  }

  private requireIntent(intent: ProductMutationIntentV2): DesktopProductSnapshotV2 {
    actionIdV2(intent.actionId);
    const snapshot = this.requireSnapshot();
    if (intent.streamEpoch !== snapshot.stream.epoch) throw new DesktopContractErrorV2("Desktop state changed before this action");
    return snapshot;
  }

  private requireProfile(profileId: string, intent: ProductMutationIntentV2): RemoteProfileV2 {
    const profile = this.requireIntent(intent).profiles.find((candidate) => candidate.profile_id === profileId);
    if (!profile) throw new DesktopContractErrorV2("Profile action references an unknown profile");
    return profile;
  }

  private requireSystemProfile(profileId: string, intent: ProductMutationIntentV2): RemoteWorkspaceProfileV2 {
    const profile = this.requireProfile(profileId, intent);
    if (profile.profile_kind !== "system_openssh") throw new DesktopContractErrorV2("Preview profile must be rebound before connection");
    return profile;
  }

  private requireProject(projectId: string, intent: ProductMutationIntentV2): ProjectV2 {
    const project = this.requireIntent(intent).projects.find((candidate) => candidate.project_id === projectId);
    if (!project) throw new DesktopContractErrorV2("Project action references an unknown active project");
    return project;
  }

  private requireTask(taskId: string, intent: ProductMutationIntentV2): TaskV2 {
    const task = this.requireIntent(intent).tasks.find((candidate) => candidate.task_id === taskId);
    if (!task) throw new DesktopContractErrorV2("Task action references an unknown task");
    return task;
  }

  private requireTransition(transitionId: string, intent: ProductMutationIntentV2) {
    const snapshot = this.requireIntent(intent);
    const transition = snapshot.transitions[transitionId];
    const project = snapshot.projects.find((candidate) => candidate.project_id === transition?.transition.project_id);
    if (!transition || !project) throw new DesktopContractErrorV2("Transition action references unknown authority");
    return { transition, project };
  }

  private observeOperation<T extends LocalOperationV2 | LifecycleOperationV2 | OperationV2>(operation: T): T {
    this.activeOperation = operation;
    this.invalidate();
    return operation;
  }

  private async waitForLifecycleTerminal(initial: LifecycleOperationV2): Promise<LifecycleOperationV2> {
    let current = initial;
    while (!isLifecycleTerminal(current)) {
      await delayV2(250);
      const next = await this.client.getLifecycleOperation(current.operation_id);
      assertLifecycleObservationDoesNotRegress(current, next);
      current = this.observeOperation(next);
    }
    return current;
  }

  private invalidate(): void {
    this.waitingForRefresh = true;
    if (this.snapshot !== null) {
      const stream: ProductStreamStateV2 = { status: "stale", epoch: this.epoch, reason: "refresh_pending" };
      this.snapshot = { ...this.snapshot, stream };
    }
    this.emit({ kind: "snapshot_changed" });
  }

  private setSnapshotStream(stream: ProductStreamStateV2): void {
    if (this.snapshot !== null) this.snapshot = { ...this.snapshot, stream };
  }

  private emit(signal: ProductSubscriptionSignalV2): void {
    for (const listener of this.listeners) listener(signal);
  }

  private ensureEventStream(): void {
    if (this.listeners.size === 0 || this.waitingForRefresh || this.streamPromise !== null) return;
    this.streamPromise = this.runEventStream()
      .catch((error) => {
        this.waitingForRefresh = true;
        const apiError = apiErrorOfV2(error);
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
        const streamRequest = await this.client.eventStreamRequest(this.replay.lastEventId ?? undefined);
        const response = await this.fetch(streamRequest.url, {
          method: "GET",
          headers: streamRequest.headers,
          credentials: "omit",
          cache: "no-store",
          redirect: "error",
          referrerPolicy: "no-referrer",
          signal: controller.signal,
        });
        if (!response.ok) {
          const payload = await readStreamErrorV2(response);
          const recovery = parseEventStreamFailureV2(response.status, payload);
          this.replay.reset();
          this.waitingForRefresh = true;
          this.setSnapshotStream({ status: "cursor_reset", epoch: this.epoch, resumeFromEventId: recovery.resumeFromEventId });
          this.emit({ kind: "cursor_reset", resumeFromEventId: null });
          return;
        }
        if (!(response.headers.get("Content-Type") ?? "").toLowerCase().includes("text/event-stream")) {
          throw new DesktopContractErrorV2("Desktop v2 event response is not an event stream", { status: response.status });
        }
        if (response.body === null) throw new DesktopContractErrorV2("Desktop v2 event response has no body");
        const sawEvent = await this.consumeEventStream(response.body, controller);
        if (sawEvent) attempt = 0;
      } catch (error) {
        if (controller.signal.aborted) return;
        if (error instanceof DesktopApiErrorV2 || error instanceof DesktopContractErrorV2) throw error;
      }
      if (attempt >= this.reconnectDelaysMs.length) throw new DesktopContractErrorV2("Desktop v2 event stream exhausted its reconnect budget");
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
        buffer += decoder.decode(value, { stream: true });
        if (new TextEncoder().encode(buffer).byteLength > MAX_SSE_BUFFER_BYTES) throw new DesktopContractErrorV2("Desktop v2 event stream exceeded the buffer limit");
        let boundary = findFrameBoundary(buffer);
        while (boundary !== null) {
          const frame = buffer.slice(0, boundary.index);
          buffer = buffer.slice(boundary.index + boundary.length);
          if (frame !== "") {
            const parsed = parseSseFrameV2(frame);
            if (parsed.kind === "event") {
              sawEvent = true;
              try {
                const observation = this.replay.observe(parsed.envelope);
                if (observation.kind === "accepted") {
                  this.waitingForRefresh = true;
                  this.emit({ kind: "snapshot_changed" });
                  controller.abort();
                  return true;
                }
              } catch (error) {
                this.waitingForRefresh = true;
                this.setSnapshotStream({ status: "stale", epoch: this.epoch, reason: "event_gap" });
                this.emit({ kind: "stream_stale", reason: "event_gap" });
                this.emit({ kind: "snapshot_changed" });
                controller.abort();
                return true;
              }
            }
          }
          boundary = findFrameBoundary(buffer);
        }
      }
      buffer += decoder.decode();
      if (buffer.trim() !== "") throw new DesktopContractErrorV2("Desktop v2 event stream ended with an incomplete frame");
      return sawEvent;
    } finally {
      reader.releaseLock();
    }
  }
}

export function createLocalApiDesktopProductProviderV2(options: LocalApiDesktopProductProviderOptionsV2): DesktopProductProviderV2 {
  return new LocalApiDesktopProductProviderV2(options);
}

async function collectPages<T>(load: (options: ListRequestOptionsV2) => Promise<{ items: T[]; has_more: boolean; next_cursor: string | null }>): Promise<T[]> {
  const items: T[] = [];
  const cursors = new Set<string>();
  let after: string | undefined;
  for (let page = 0; page < MAX_COLLECTION_PAGES; page += 1) {
    const result = await load({ limit: PAGE_LIMIT, ...(after === undefined ? {} : { after }) });
    items.push(...result.items);
    if (items.length > MAX_REFRESH_RESOURCES) throw new DesktopContractErrorV2("Desktop v2 snapshot exceeded the resource budget");
    if (!result.has_more) return items;
    if (result.next_cursor === null || cursors.has(result.next_cursor)) throw new DesktopContractErrorV2("Desktop v2 pagination cursor cycle detected");
    cursors.add(result.next_cursor);
    after = result.next_cursor;
  }
  throw new DesktopContractErrorV2("Desktop v2 collection exceeded the page budget");
}

function assertProfileAuthority(state: DesktopStateV2, profiles: readonly RemoteProfileV2[]): void {
  const stateById = new Map(state.profiles.map((profile) => [profile.profile_id, JSON.stringify(profile)]));
  if (stateById.size !== state.profiles.length || profiles.length !== state.profiles.length) {
    throw new DesktopContractErrorV2("Desktop state and profile collection disagree");
  }
  for (const profile of profiles) {
    if (stateById.get(profile.profile_id) !== JSON.stringify(profile)) throw new DesktopContractErrorV2("Desktop profile authority drifted across one refresh");
  }
}

function activeConnectedProfile(snapshot: Pick<DesktopProductSnapshotV2, "state" | "profiles">): RemoteWorkspaceProfileV2 {
  const profile = snapshot.profiles.find((candidate) => candidate.profile_id === snapshot.state.active_profile_id);
  if (profile?.profile_kind !== "system_openssh" || profile.connection_state !== "connected") {
    throw new DesktopContractErrorV2("Active Core authority has no connected system-OpenSSH profile");
  }
  return profile;
}

function profileMutation(profile: RemoteWorkspaceProfileV2, intent: ProductMutationIntentV2) {
  return { resourceGeneration: profile.connection_generation, ifMatch: profile.etag, idempotencyKey: intent.actionId };
}

function projectMutation(project: ProjectV2, intent: ProductMutationIntentV2) {
  return {
    resourceGeneration: project.active_project_head?.generation ?? 0,
    ifMatch: project.etag,
    idempotencyKey: intent.actionId,
  };
}

function taskAction(task: TaskV2) {
  return {
    schema_version: "2" as const,
    task_admission_id: task.admission.task_admission_id,
    admission_sha256: task.admission.admission_sha256,
    predecessor_project_head_id: task.admission.predecessor_project_head.project_head_id,
  };
}

function taskMutation(task: TaskV2, intent: ProductMutationIntentV2) {
  const attempt = task.attempts.at(-1);
  if (!attempt) throw new DesktopContractErrorV2("Task has no infrastructure attempt authority");
  return { resourceGeneration: attempt.ordinal, ifMatch: task.etag, idempotencyKey: intent.actionId };
}

function transitionAction(transition: SuccessorTransitionV2) {
  return {
    schema_version: "2" as const,
    expected_predecessor_project_head_id: transition.transition.predecessor_project_head.project_head_id,
    plan_sha256: transition.transition.plan_sha256,
  };
}

function transitionMutation(transition: SuccessorTransitionV2, project: ProjectV2, intent: ProductMutationIntentV2) {
  return {
    resourceGeneration: transition.transition.expected_successor_generation,
    ifMatch: project.etag,
    idempotencyKey: intent.actionId,
  };
}

function requireProjectHead(project: ProjectV2) {
  if (project.active_project_head === null) throw new DesktopContractErrorV2("Project has no active Project Head");
  return project.active_project_head;
}

function isLifecycleTerminal(operation: LifecycleOperationV2): boolean {
  return operation.status === "succeeded"
    || operation.status === "failed"
    || operation.status === "cancelled";
}

function lifecycleTerminalError(operation: LifecycleOperationV2, fallback: string): DesktopContractErrorV2 {
  return new DesktopContractErrorV2(operation.failure?.summary ?? fallback);
}

function assertLifecycleObservationDoesNotRegress(
  previous: LifecycleOperationV2,
  next: LifecycleOperationV2,
): void {
  if (next.operation_id !== previous.operation_id
    || next.kind !== previous.kind
    || JSON.stringify(next.resource) !== JSON.stringify(previous.resource)
    || next.request_sha256 !== previous.request_sha256
    || next.created_at !== previous.created_at) {
    throw new DesktopContractErrorV2("Lifecycle operation identity changed while being observed");
  }
  if (isLifecycleTerminal(previous) && JSON.stringify(next) !== JSON.stringify(previous)) {
    throw new DesktopContractErrorV2("Terminal lifecycle operation changed");
  }
  if (next.phase_index < previous.phase_index
    || next.log_sequence_high_watermark < previous.log_sequence_high_watermark
    || compareUtcTimestampsV2(next.updated_at, previous.updated_at) < 0) {
    throw new DesktopContractErrorV2("Lifecycle operation progress regressed");
  }
  if (previous.status === "running" && next.status === "queued") {
    throw new DesktopContractErrorV2("Lifecycle operation status regressed");
  }
  if (previous.progress?.kind === next.progress?.kind
    && previous.progress !== null
    && next.progress !== null
    && previous.progress.kind !== "indeterminate"
    && next.progress.kind !== "indeterminate"
    && (next.progress.total !== previous.progress.total
      || next.progress.completed < previous.progress.completed)) {
    throw new DesktopContractErrorV2("Lifecycle operation measurable progress regressed");
  }
}

function delayV2(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function actionIdV2(value: string): string {
  return z.string().min(16).max(256).refine((item) => item === item.trim() && !/[\u0000-\u001f\u007f]/.test(item)).parse(value);
}

function apiErrorOfV2(error: unknown): DesktopErrorV2 | null {
  return error instanceof DesktopApiErrorV2 ? error.apiError : null;
}

async function readStreamErrorV2(response: Response): Promise<unknown> {
  const text = await response.text();
  if (new TextEncoder().encode(text).byteLength > 1_048_576) throw new DesktopContractErrorV2("Desktop v2 event error exceeds the byte limit");
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new DesktopContractErrorV2("Desktop v2 event error contains malformed JSON", { cause: error, status: response.status });
  }
}

function findFrameBoundary(value: string): { index: number; length: number } | null {
  const candidates = ["\n\n", "\r\n\r\n", "\r\r"]
    .map((separator) => ({ index: value.indexOf(separator), length: separator.length }))
    .filter((candidate) => candidate.index >= 0)
    .sort((left, right) => left.index - right.index);
  return candidates[0] ?? null;
}

function delay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) return reject(new DOMException("Aborted", "AbortError"));
    const timeout = setTimeout(resolve, milliseconds);
    signal.addEventListener("abort", () => {
      clearTimeout(timeout);
      reject(new DOMException("Aborted", "AbortError"));
    }, { once: true });
  });
}

function fail(message: string): never {
  throw new DesktopContractErrorV2(message);
}
