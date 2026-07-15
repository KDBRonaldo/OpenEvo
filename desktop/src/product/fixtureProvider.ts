import {
  CONTRACT_FIXTURE_V1,
  RELEASE_EXECUTION_MODE_CAPABILITIES_FIXTURE_V1,
} from "../api/v1/fixtures";
import { DesktopApiError } from "../api/v1/client";
import {
  apiErrorV1Schema,
  artifactContentV1Schema,
  artifactDiffV1Schema,
  artifactV1Schema,
  desktopStateV1Schema,
  executionModeCapabilitiesV1Schema,
  localOperationV1Schema,
  logEntryV1Schema,
  projectCapabilitiesV1Schema,
  projectSourceV1Schema,
  projectValidationV1Schema,
  projectV1Schema,
  remoteProfileV1Schema,
  revisionTransitionV1Schema,
  runV1Schema,
  serviceV1Schema,
  timelineEntryV1Schema,
  type ArtifactContentV1,
  type ArtifactDiffV1,
  type ArtifactV1,
  type DesktopStateV1,
  type HostKeyAcceptV1,
  type LocalOperationV1,
  type LogEntryV1,
  type ProfileCreateV1,
  type ProfilePatchV1,
  type ProjectCapabilitiesV1,
  type ProjectCreateV1,
  type ProjectPatchV1,
  type ProjectSourceV1,
  type ProjectValidationV1,
  type ProjectV1,
  type RemoteProfileV1,
  type RunV1,
  type ServiceV1,
  type TimelineEntryV1,
} from "../api/v1/schemas";
import {
  DesktopProductUserError,
  type DesktopProductProvider,
  type DesktopProductSnapshot,
  type ProductMutationIntent,
  type ProductRefreshResult,
  type ProductResourceMutationIntent,
  type ProductRunIntent,
  type ProductSubscriptionSignal,
  type ProjectSourceSelectionIntent,
} from "./provider";

const A = "a".repeat(64);
const B = "b".repeat(64);
const C = "c".repeat(64);
const D = "d".repeat(64);
const ETAG_A = `"${A}"`;
const ETAG_B = `"${B}"`;
const ETAG_D = `"${D}"`;
const NOW = "2026-07-14T12:00:00Z";

export interface FixtureProviderOptions {
  startOnline?: boolean;
  seedCompletedRun?: boolean;
  seedFailedRun?: boolean;
  artifactTruncated?: boolean;
  degraded?: boolean;
  stepDelayMs?: number;
  newUser?: boolean;
  releaseExecutionModes?: boolean;
  projectExecutionMode?: ProjectV1["execution"]["mode"];
  includeParametricMemory?: boolean;
}

export class FixtureDesktopProductProvider implements DesktopProductProvider {
  readonly providerKind = "contract_simulator" as const;
  private readonly listeners = new Set<(signal: ProductSubscriptionSignal) => void>();
  private readonly timers = new Set<ReturnType<typeof setTimeout>>();
  private readonly stepDelayMs: number;
  private readonly artifactTruncated: boolean;
  private readonly includeParametricMemory: boolean;
  private state: DesktopStateV1;
  private readonly executionModeCapabilities: DesktopStateV1["execution_mode_capabilities"];
  private profiles: RemoteProfileV1[];
  private projects: ProjectV1[];
  private runs: RunV1[] = [];
  private timelines: Record<string, TimelineEntryV1[]> = {};
  private logs: Record<string, LogEntryV1[]> = {};
  private artifacts: ArtifactV1[] = [];
  private artifactCollection: DesktopProductSnapshot["artifactCollection"] = { status: "complete" };
  private services: ServiceV1[];
  private capabilities: ProjectCapabilitiesV1 | null;
  private validation: ProjectValidationV1 | null;
  private stream: DesktopProductSnapshot["stream"] = { status: "fresh", epoch: 1, lastEventId: null };
  private readonly actionSignatures = new Map<string, string>();
  private failProjectSave = false;
  private failProjectSaveWithUnknownError = false;
  private failProfileSaveWithUnknownError = false;
  private failProfileCreateWithUnknownError = false;
  private loseProfileCreateResponseAfterCommit = false;
  private failProjectCreateWithUnknownError = false;
  private failRefresh = false;
  private restoreCapabilitiesOnRefresh: ProjectCapabilitiesV1 | null = null;
  private capabilityRefreshesBeforeRestore = 0;
  private nextProjectSaveStatus: 412 | null = null;
  private nextProjectActivationStatus: 409 | 412 | null = null;
  private nextRunStartStatus: 409 | 410 | null = null;
  private nextRunStartConflict: {
    code: string;
    message: string;
    retryable: boolean;
    repairAction: "openevo_can_retry" | "openevo_can_install" | "openevo_can_reconfigure" | "user_action_required" | "unsupported";
    addEquivalentRun: boolean;
  } | null = null;
  private refreshAttempts = 0;
  private projectSaveAttempts = 0;
  private runAdmissionAttempts = 0;
  private readonly profileCreateActions: string[] = [];
  private readonly profileUpdateActions: string[] = [];
  private readonly projectCreateActions: string[] = [];
  private readonly projectUpdateActions: string[] = [];
  private readonly projectActivationActions: string[] = [];
  private activeOperation: LocalOperationV1 | null = null;
  private readonly contents = new Map<string, ArtifactContentV1>();
  private readonly diffs = new Map<string, ArtifactDiffV1>();

  constructor(options: FixtureProviderOptions = {}) {
    if (import.meta.env.PROD) {
      throw new Error("The contract simulator is unavailable in release builds.");
    }
    this.stepDelayMs = options.stepDelayMs ?? 80;
    this.artifactTruncated = options.artifactTruncated ?? false;
    this.includeParametricMemory = options.includeParametricMemory ?? false;
    if (options.seedCompletedRun && options.seedFailedRun) {
      throw new Error("A fixture cannot seed both completed and failed runs.");
    }
    const online = options.startOnline ?? false;
    const newUser = options.newUser ?? (!online && !options.seedCompletedRun && !options.seedFailedRun);
    const releaseExecutionModes = executionModeCapabilitiesV1Schema.parse(RELEASE_EXECUTION_MODE_CAPABILITIES_FIXTURE_V1);
    this.executionModeCapabilities = options.releaseExecutionModes
      ? releaseExecutionModes
      : executionModeCapabilitiesV1Schema.parse({
          ...releaseExecutionModes,
          modes: releaseExecutionModes.modes.map((capability) => capability.mode === "self-deployed"
            ? {
                ...capability,
                support_state: "supported",
                reason_code: null,
                message: "Available in the contract simulator.",
              }
            : capability),
        });
    this.profiles = newUser ? [] : [this.makeProfile(online ? "connected" : "disconnected")];
    this.projects = newUser ? [] : [this.makeProjectFixture(options.projectExecutionMode)];
    this.state = newUser ? this.makeNewUserState() : this.makeState(online ? "online" : "offline");
    this.capabilities = online
      ? this.makeCapabilities(
          this.projects[0]?.project_id ?? "project-fixture-1",
          this.projects[0]?.execution.mode ?? "self-deployed",
        )
      : null;
    this.validation = this.capabilities && this.projects[0]
      ? this.makeValidation(this.projects[0], this.capabilities)
      : null;
    this.services = newUser ? [] : this.makeServices(online, options.degraded ?? false);

    if (options.seedCompletedRun) {
      this.seedCompletedRun();
    } else if (options.seedFailedRun) {
      this.seedFailedRun();
    }
  }

  async refresh(): Promise<ProductRefreshResult> {
    this.refreshAttempts += 1;
    if (this.failRefresh) {
      this.failRefresh = false;
      throw new Error("internal refresh details");
    }
    if (this.stream.status !== "fresh") {
      this.stream = { status: "fresh", epoch: this.stream.epoch + 1, lastEventId: null };
    }
    if (this.restoreCapabilitiesOnRefresh && this.capabilityRefreshesBeforeRestore > 0) {
      this.capabilityRefreshesBeforeRestore -= 1;
    } else if (this.restoreCapabilitiesOnRefresh) {
      this.capabilities = this.restoreCapabilitiesOnRefresh;
      this.restoreCapabilitiesOnRefresh = null;
      const project = this.projects.find((item) => item.project_id === this.capabilities?.project_id);
      this.validation = project && this.capabilities ? this.makeValidation(project, this.capabilities) : null;
    }
    return { status: "fresh", snapshot: this.snapshot() };
  }

  private snapshot(): DesktopProductSnapshot {
    const contextProject = this.state.active_project
      ? this.projects.find((item) => item.project_id === this.state.active_project?.project_id) ?? null
      : this.projects[0] ?? null;
    const capability = this.capabilities
      ? { status: "ready" as const, projectId: this.capabilities.project_id, executionMode: capabilityExecutionMode(this.capabilities), value: this.capabilities }
      : contextProject
        ? { status: "unavailable" as const, projectId: contextProject.project_id, executionMode: contextProject.execution.mode, error: null }
        : null;
    const project = this.capabilities
      ? this.projects.find((item) => item.project_id === this.capabilities?.project_id)
      : null;
    const validation = this.validation && project
      ? { status: "ready" as const, projectId: project.project_id, executionMode: project.execution.mode, projectEtag: project.etag, value: this.validation }
      : contextProject
        ? { status: "unavailable" as const, projectId: contextProject.project_id, executionMode: contextProject.execution.mode, projectEtag: contextProject.etag, error: null }
        : null;
    return structuredClone({
      state: this.state,
      executionModeCapabilities: this.state.execution_mode_capabilities,
      profiles: this.profiles,
      projects: this.projects,
      runs: this.runs,
      timelines: this.timelines,
      artifacts: this.artifacts,
      artifactCollection: this.artifactCollection,
      services: this.services,
      capability,
      validation,
      activeOperation: this.activeOperation,
      stream: this.stream,
    });
  }

  subscribe(listener: (signal: ProductSubscriptionSignal) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  async createProfile(input: ProfileCreateV1, intent: ProductMutationIntent): Promise<RemoteProfileV1> {
    this.profileCreateActions.push(intent.actionId);
    this.checkIntent(intent, "profile:create");
    if (this.failProfileCreateWithUnknownError) {
      this.failProfileCreateWithUnknownError = false;
      throw new Error("profile response was lost");
    }
    if ((input.authentication_kind ?? "ssh_agent") !== "ssh_agent") {
      throw new Error("SSH agent is the only authentication method supported by this release.");
    }
    const profile = remoteProfileV1Schema.parse({
      schema_version: "1",
      profile_id: `profile-fixture-${this.profiles.length + 1}`,
      ...input,
      credential_slots: [],
      connection_state: "disconnected",
      host_key_fingerprint: null,
      etag: ETAG_A,
      created_at: NOW,
      updated_at: NOW,
    });
    this.profiles = [...this.profiles, profile];
    this.state = desktopStateV1Schema.parse({
      ...this.state,
      core: {
        state: "disconnected",
        profile_id: null,
        active_tunnel: false,
        operation_id: null,
        host_key_review: null,
        core: null,
        failure: null,
      },
    });
    this.emit();
    if (this.loseProfileCreateResponseAfterCommit) {
      this.loseProfileCreateResponseAfterCommit = false;
      throw new Error("profile response was lost after commit");
    }
    return structuredClone(profile);
  }

  async updateProfile(profileId: string, input: ProfilePatchV1, intent: ProductResourceMutationIntent): Promise<RemoteProfileV1> {
    this.profileUpdateActions.push(intent.actionId);
    const current = this.requireProfile(profileId);
    this.checkIntent(intent, `profile:update:${profileId}`, current.etag);
    if (this.failProfileSaveWithUnknownError) {
      this.failProfileSaveWithUnknownError = false;
      throw new Error("profile response was lost");
    }
    const authenticationKind = input.authentication_kind ?? current.authentication_kind;
    const proxy = input.proxy ?? current.proxy;
    if (authenticationKind !== "ssh_agent") {
      throw new Error("SSH agent is the only authentication method supported by this release.");
    }
    const updated = remoteProfileV1Schema.parse({
      ...current,
      ...input,
      authentication_kind: authenticationKind,
      credential_slots: [],
      proxy,
      etag: ETAG_D,
      updated_at: NOW,
    });
    this.profiles = this.profiles.map((profile) => profile.profile_id === profileId ? updated : profile);
    this.emit();
    return structuredClone(updated);
  }

  async connectProfile(profileId: string, intent: ProductResourceMutationIntent): Promise<LocalOperationV1> {
    const profile = this.requireProfile(profileId);
    this.checkIntent(intent, `profile:connect:${profileId}`, profile.etag);
    if (profile.authentication_kind !== "ssh_agent") {
      throw new Error("SSH agent is the only authentication method supported by this release.");
    }
    this.activeOperation = this.makeOperation("profile_connect", "running", "Connecting securely", 1, 4);
    this.state = this.connectionState("connecting", { operationId: this.activeOperation.operation_id });
    this.updateProfileConnection(profileId, "connecting");
    this.emit();
    this.schedule(1, () => {
      this.state = this.connectionState("host_key_review", {
        operationId: this.activeOperation?.operation_id ?? "operation-connect-fixture",
      });
      this.updateProfileConnection(profileId, "host_key_required");
      this.activeOperation = this.makeOperation("profile_connect", "running", "Confirm server identity", 2, 4);
      this.emit();
    });
    return structuredClone(this.activeOperation);
  }

  async acceptHostKey(profileId: string, input: HostKeyAcceptV1, intent: ProductResourceMutationIntent): Promise<LocalOperationV1> {
    const profile = this.requireProfile(profileId);
    this.checkIntent(intent, `profile:host-key:${profileId}`, profile.etag);
    const review = this.state.core.host_key_review;
    if (!review || review.algorithm !== input.algorithm || review.fingerprint !== input.fingerprint) {
      throw new Error("The server identity changed before it was accepted.");
    }
    this.activeOperation = this.makeOperation("host_key_accept", "running", "Checking environment", 2, 4);
    this.state = this.connectionState("checking", { operationId: this.activeOperation.operation_id });
    this.emit();
    this.schedule(1, () => {
      this.activeOperation = this.makeOperation("bootstrap", "running", "Preparing OpenEvo", 3, 4);
      this.state = this.connectionState("bootstrapping", { operationId: this.activeOperation.operation_id });
      this.emit();
    });
    this.schedule(2, () => {
      this.activeOperation = null;
      this.state = this.connectionState("online");
      this.updateProfileConnection(profileId, "connected");
      this.projects = this.projects.map((project) => ({ ...project, state: "active" }));
      const activeProject = this.projects[0];
      this.capabilities = this.makeCapabilities(
        activeProject?.project_id ?? "project-fixture-1",
        activeProject?.execution.mode ?? "self-deployed",
      );
      this.validation = activeProject ? this.makeValidation(activeProject, this.capabilities) : null;
      this.services = this.makeServices(true, false);
      this.emit();
    });
    return structuredClone(this.activeOperation);
  }

  async createProject(input: ProjectCreateV1, intent: ProductMutationIntent): Promise<ProjectV1> {
    this.projectCreateActions.push(intent.actionId);
    this.checkIntent(intent, "project:create");
    if (this.failProjectCreateWithUnknownError) {
      this.failProjectCreateWithUnknownError = false;
      throw new Error("project response was lost");
    }
    const project = projectV1Schema.parse({
      schema_version: "1",
      project_id: `project-fixture-${this.projects.length + 1}`,
      ...input,
      state: "draft",
      etag: ETAG_D,
      created_at: NOW,
      updated_at: NOW,
    });
    this.projects = [...this.projects, project];
    this.capabilities = this.state.core.state === "online"
      ? this.makeCapabilities(project.project_id, project.execution.mode)
      : null;
    this.validation = this.capabilities ? this.makeValidation(project, this.capabilities) : null;
    this.emit();
    return structuredClone(project);
  }

  async updateProject(projectId: string, input: ProjectPatchV1, intent: ProductResourceMutationIntent): Promise<ProjectV1> {
    this.projectSaveAttempts += 1;
    this.projectUpdateActions.push(intent.actionId);
    const current = this.requireProject(projectId);
    this.checkIntent(intent, `project:update:${projectId}`, current.etag);
    if (this.nextProjectSaveStatus) {
      const status = this.nextProjectSaveStatus;
      this.nextProjectSaveStatus = null;
      const changed = projectV1Schema.parse({ ...current, etag: ETAG_B, updated_at: NOW });
      this.projects = this.projects.map((project) => project.project_id === projectId ? changed : project);
      if (this.state.active_project?.project_id === projectId) {
        this.state = desktopStateV1Schema.parse({ ...this.state, active_project: { ...this.state.active_project, project_etag: changed.etag } });
      }
      throw this.apiError(status, "etag_precondition_failed", "The project changed remotely.", "project");
    }
    if (this.failProjectSave) {
      this.failProjectSave = false;
      throw new DesktopProductUserError("The project could not be saved. Try again.");
    }
    if (this.failProjectSaveWithUnknownError) {
      this.failProjectSaveWithUnknownError = false;
      throw new Error("internal host path and process details");
    }
    const wasActive = this.state.active_project?.project_id === projectId;
    const updated = projectV1Schema.parse({
      ...current,
      ...input,
      state: wasActive ? "draft" : current.state,
      remote: wasActive ? null : current.remote,
      etag: ETAG_D,
      updated_at: NOW,
    });
    this.projects = this.projects.map((project) => (project.project_id === projectId ? updated : project));
    if (wasActive) {
      this.capabilities = null;
      this.validation = null;
      this.state = desktopStateV1Schema.parse({
        ...this.state,
        core: {
          state: "offline",
          profile_id: updated.profile_id,
          active_tunnel: false,
          operation_id: null,
          host_key_review: null,
          core: null,
          failure: {
            code: "core_not_started",
            message: "SSH is connected; OpenEvo Core has not been started for a project.",
            retryable: true,
            next_action: "Create or activate a project to prepare OpenEvo Core.",
          },
        },
        active_project: null,
      });
    } else {
      this.capabilities = this.state.core.state === "online" ? this.makeCapabilities(projectId, updated.execution.mode) : null;
      this.validation = this.capabilities ? this.makeValidation(updated, this.capabilities) : null;
    }
    this.emit();
    return structuredClone(updated);
  }

  async activateProject(projectId: string, intent: ProductResourceMutationIntent): Promise<LocalOperationV1> {
    const project = this.requireProject(projectId);
    this.projectActivationActions.push(intent.actionId);
    this.checkIntent(intent, `project:activate:${projectId}`, project.etag);
    if (this.nextProjectActivationStatus) {
      const status = this.nextProjectActivationStatus;
      this.nextProjectActivationStatus = null;
      if (status === 412) {
        this.projects = this.projects.map((item) => item.project_id === projectId
          ? projectV1Schema.parse({ ...item, etag: ETAG_B, updated_at: NOW })
          : item);
      }
      this.emit();
      throw this.apiError(status, "project_activation_conflict", "The project changed before activation.", "project");
    }
    const coreProjectId = project.remote?.core_project_id ?? this.fixtureCoreProjectId(project.project_id);
    const activated = projectV1Schema.parse({
      ...project,
      state: "active",
      remote: project.remote ?? {
        ...structuredClone(CONTRACT_FIXTURE_V1.project.remote),
        core_project_id: coreProjectId,
        active_revision: this.revision(1, coreProjectId),
      },
      updated_at: NOW,
    });
    const onlineState = this.connectionState("online");
    this.state = desktopStateV1Schema.parse({ ...this.state, core: onlineState.core });
    this.updateProfileConnection(project.profile_id, "connected");
    this.projects = this.projects.map((item) => item.project_id === projectId ? activated : item);
    this.capabilities = this.makeCapabilities(projectId, activated.execution.mode);
    this.validation = this.capabilities ? this.makeValidation(activated, this.capabilities) : null;
    this.activeOperation = this.makeOperation("project_activate", "succeeded", "Project ready", 1, 1, project.project_id);
    this.state = desktopStateV1Schema.parse({
      ...this.state,
      active_project: {
        project_id: project.project_id,
        project_etag: activated.etag,
        profile_id: project.profile_id,
        connection_state: "ready",
      },
    });
    this.emit();
    return structuredClone(this.activeOperation);
  }

  async selectProjectSource(intent: ProjectSourceSelectionIntent): Promise<ProjectSourceV1> {
    this.checkIntent(intent, "project:source-select");
    return projectSourceV1Schema.parse({
      kind: "native_folder_snapshot",
      display_name: "Selected research folder",
      import_ref: { ...structuredClone(CONTRACT_FIXTURE_V1.workspaceImport), import_id: "source-fixture-1", content_sha256: C, byte_size: 4096 },
    });
  }

  async cancelProjectSource(_actionId: string): Promise<void> {}

  async settleProjectSource(
    _actionId: string,
    _outcome: "adopt" | "discard",
  ): Promise<void> {}

  async startRun(intent: ProductRunIntent): Promise<RunV1> {
    this.runAdmissionAttempts += 1;
    if (this.nextRunStartConflict) {
      const conflict = this.nextRunStartConflict;
      this.nextRunStartConflict = null;
      if (conflict.addEquivalentRun) this.addEquivalentQueuedRun(intent.projectId);
      throw this.apiError(409, conflict.code, conflict.message, "run", {
        retryable: conflict.retryable,
        repairAction: conflict.repairAction,
      });
    }
    if (this.nextRunStartStatus) {
      const status = this.nextRunStartStatus;
      this.nextRunStartStatus = null;
      if (status === 410) {
        this.stream = { status: "cursor_reset", epoch: this.stream.epoch, resumeFromEventId: null };
        throw this.apiError(status, "event_cursor_expired", "The event cursor expired.", "run");
      }
      throw this.apiError(status, "run_admission_conflict", "The required revision changed before admission.", "run");
    }
    const projectId = intent.projectId;
    const project = this.requireProject(projectId);
    this.checkIntent(intent, `run:start:${projectId}`, project.etag);
    if (this.state.core.state !== "online") {
      throw new Error("Reconnect the remote workspace before starting a session.");
    }
    const coreProjectId = this.requireCoreProjectId(project);
    if (this.runs.some((run) => run.project_id === coreProjectId && !isTerminal(run.status))) {
      throw new Error("A session is already active.");
    }

    const generation = this.currentGeneration(project);
    const runNumber = this.runs.length + 1;
    const runId = `run-fixture-${runNumber}`;
    const revision = this.revision(generation, coreProjectId);
    const attempt = {
      id: `attempt-fixture-${runNumber}`,
      run_id: runId,
      number: 1,
      status: "queued" as const,
      queued_reason: {
        code: "service_starting" as const,
        summary: "Preparing the remote workspace.",
        retry_after_seconds: 1,
      },
      created_at: NOW,
      updated_at: NOW,
      started_at: null,
      finished_at: null,
      error: null,
    };
    const run = runV1Schema.parse({
      ...structuredClone(CONTRACT_FIXTURE_V1.run),
      id: runId,
      project_id: coreProjectId,
      project_snapshot: { id: `project-snapshot-${runNumber}`, kind: "project", content_sha256: A, created_at: NOW },
      task_snapshot: { id: `task-snapshot-${runNumber}`, kind: "task", content_sha256: B, created_at: NOW },
      workspace_snapshot: { id: `workspace-snapshot-${runNumber}`, kind: "workspace", content_sha256: C, created_at: NOW },
      registry_digest: B,
      execution_mode: project.execution.mode,
      status: "queued",
      queued_reason: attempt.queued_reason,
      current_attempt_id: attempt.id,
      current_attempt: attempt,
      attempt_count: 1,
      current_error: null,
      pinned_revision: revision,
      required_revision: { revision, reachable_from_revision_id: revision.id, relation: "active" },
      revision_transition: null,
      attempts: [attempt],
      created_at: NOW,
      updated_at: NOW,
      admitted_at: NOW,
      started_at: null,
      finished_at: null,
      etag: ETAG_A,
    });
    this.runs = [run, ...this.runs];
    this.timelines[runId] = [
      this.timeline(runNumber, "admission", "pending", "Session admitted", `Waiting for Revision ${generation}.`),
    ];
    this.logs[runId] = [
      this.log(run, "core", "Session admitted with an immutable project snapshot."),
    ];
    this.emit();

    this.schedule(1, () => this.whileRunActive(runId, () => {
      this.transitionRun(
        runId,
        "preparing",
        "preparation",
        "running",
        "Preparing workspace",
        "The project snapshot is being prepared.",
      );
    }));
    this.schedule(2, () => this.whileRunActive(runId, () => {
      this.transitionRun(
        runId,
        "running",
        "execution",
        "running",
        "Research task",
        "The task is running with its pinned revision.",
      );
      this.appendLog(
        runId,
        "agent",
        "Research execution is using the selected workspace and evidence sources.",
      );
    }));
    this.schedule(3, () => this.whileRunActive(runId, () => {
      this.appendTimeline(
        runId,
        "capture",
        "succeeded",
        "Session captured",
        "The session record is sealed.",
      );
      this.appendLog(
        runId,
        "agent",
        "Evidence synthesis completed with three supported findings.",
      );
    }));
    this.schedule(4, () => this.whileRunActive(runId, () => {
      this.appendTimeline(
        runId,
        "evolution",
        "running",
        "Updating evolution targets",
        "Memory, skills, and instructions are being updated.",
      );
      this.appendLog(
        runId,
        "evolution",
        "Memory and skills were prepared for the next session.",
      );
    }));
    this.schedule(5, () => this.whileRunActive(runId, () => {
      this.appendTimeline(
        runId,
        "materialization",
        "running",
        "Preparing next revision",
        "Validated outputs are being assembled atomically.",
      );
    }));
    this.schedule(6, () => this.whileRunActive(
      runId,
      () => this.finishRun(runId, generation + 1),
    ));
    return structuredClone(run);
  }

  async retryRun(runId: string, intent: ProductResourceMutationIntent): Promise<RunV1> {
    const run = this.requireRun(runId);
    this.checkIntent(intent, `run:retry:${runId}`, run.etag);
    if (run.status !== "failed" || !run.current_attempt) {
      throw new DesktopProductUserError("Only a failed session can be retried.");
    }
    const attemptNumber = run.attempt_count + 1;
    const queuedReason = {
      code: "admission_pending" as const,
      summary: "The retry was admitted on the same session.",
      retry_after_seconds: 1,
    };
    const attempt = {
      ...run.current_attempt,
      id: `attempt-${runId}-${attemptNumber}`,
      number: attemptNumber,
      status: "queued" as const,
      queued_reason: queuedReason,
      created_at: NOW,
      updated_at: NOW,
      started_at: null,
      finished_at: null,
      error: null,
    };
    const retried = runV1Schema.parse({
      ...run,
      status: "queued",
      queued_reason: queuedReason,
      current_attempt_id: attempt.id,
      current_attempt: attempt,
      attempt_count: attemptNumber,
      current_error: null,
      attempts: [...run.attempts, attempt],
      updated_at: NOW,
      started_at: null,
      finished_at: null,
      etag: run.etag === ETAG_A ? ETAG_B : ETAG_A,
    });
    this.replaceRun(retried);
    this.appendTimeline(
      runId,
      "admission",
      "pending",
      "Retry admitted",
      "A new attempt was appended to the same session.",
    );
    return structuredClone(retried);
  }

  async cancelRun(runId: string, intent: ProductResourceMutationIntent): Promise<RunV1> {
    const run = this.requireRun(runId);
    this.checkIntent(intent, `run:cancel:${runId}`, run.etag);
    if (isTerminal(run.status)) return structuredClone(run);
    const attempt = {
      ...run.current_attempt!,
      status: "cancelled" as const,
      queued_reason: null,
      updated_at: NOW,
      finished_at: NOW,
    };
    const cancelled = runV1Schema.parse({
      ...run,
      status: "cancelled",
      queued_reason: null,
      current_attempt: attempt,
      attempts: [...run.attempts.slice(0, -1), attempt],
      finished_at: NOW,
      updated_at: NOW,
    });
    this.replaceRun(cancelled);
    this.appendTimeline(runId, "terminal", "cancelled", "Session cancelled", "No successor revision was activated.");
    return structuredClone(cancelled);
  }

  async cancelOperation(
    operationId: string,
    intent: ProductResourceMutationIntent,
  ): Promise<LocalOperationV1> {
    const operation = this.activeOperation;
    if (!operation || operation.operation_id !== operationId) {
      throw new Error("The local operation is no longer active.");
    }
    this.checkIntent(intent, `operation:cancel:${operationId}`, operation.etag);
    const cancelled = localOperationV1Schema.parse({
      ...operation,
      state: "cancelled",
      finished_at: NOW,
      etag: ETAG_B,
    });
    this.activeOperation = null;
    this.state = this.connectionState("disconnected");
    this.updateProfileConnection(operation.resource.resource_id, "disconnected");
    this.emit();
    return structuredClone(cancelled);
  }

  async getRunLogs(runId: string): Promise<readonly LogEntryV1[]> {
    this.requireRun(runId);
    return structuredClone(this.logs[runId] ?? []);
  }

  async getArtifactContent(artifactId: string): Promise<ArtifactContentV1> {
    const value = this.contents.get(artifactId);
    if (!value) throw new Error("Artifact content is unavailable.");
    return structuredClone(value);
  }

  async getArtifactDiff(artifactId: string): Promise<ArtifactDiffV1> {
    const value = this.diffs.get(artifactId);
    if (!value) throw new Error("Artifact changes are unavailable.");
    return structuredClone(value);
  }

  dispose(): void {
    for (const timer of this.timers) clearTimeout(timer);
    this.timers.clear();
    this.listeners.clear();
  }

  private seedCompletedRun(): void {
    const project = this.projects[0];
    if (!project) return;
    const coreProjectId = this.requireCoreProjectId(project);
    const predecessor = this.revision(1, coreProjectId);
    const successor = this.revision(2, coreProjectId);
    const base = structuredClone(CONTRACT_FIXTURE_V1.run);
    const attempt = { ...base.attempts[0], status: "succeeded" as const, finished_at: NOW };
    const transition = revisionTransitionV1Schema.parse({
      state: "active",
      predecessor_revision: predecessor,
      successor_revision: successor,
      progress_completed: 4,
      progress_total: 4,
      message: "The successor revision is active.",
      error: null,
      updated_at: NOW,
    });
    const run = {
      ...runV1Schema.parse({
        ...base,
        project_id: coreProjectId,
        execution_mode: project.execution.mode,
        status: "succeeded",
        queued_reason: null,
        current_attempt: attempt,
        current_error: null,
        pinned_revision: predecessor,
        required_revision: { revision: predecessor, reachable_from_revision_id: predecessor.id, relation: "active" },
        revision_transition: null,
        attempts: [attempt],
        finished_at: NOW,
      }),
      revision_transition: transition,
    } satisfies RunV1;
    this.runs = [run];
    this.timelines[run.id] = [
      this.timeline(1, "revision", "succeeded", "Revision 2 active", "The next session will use the new revision.", run.id),
    ];
    this.logs[run.id] = [
      this.log(run, "agent", "Evidence synthesis completed with three supported findings."),
    ];
    this.logs[run.id] = [
      ...this.logs[run.id],
      this.log(run, "evolution", "Memory and skills were prepared for the next session."),
    ];
    this.projects = [projectV1Schema.parse({
      ...project,
      remote: project.remote ? { ...project.remote, active_revision: successor, observed_at: NOW } : null,
    })];
    this.createArtifacts(null, 1);
    this.createArtifacts(run.id, 2);
  }

  private seedFailedRun(): void {
    const project = this.projects[0];
    if (!project) return;
    const coreProjectId = this.requireCoreProjectId(project);
    const predecessor = this.revision(1, coreProjectId);
    const base = structuredClone(CONTRACT_FIXTURE_V1.run);
    const error = this.apiError(
      409,
      "session_execution_failed",
      "The research session failed before evolution outputs were committed.",
      "run",
    ).apiError;
    const attempt = {
      ...base.attempts[0],
      status: "failed" as const,
      queued_reason: null,
      updated_at: NOW,
      finished_at: NOW,
      error,
    };
    const run = runV1Schema.parse({
      ...base,
      project_id: coreProjectId,
      execution_mode: project.execution.mode,
      status: "failed",
      queued_reason: null,
      current_attempt: attempt,
      current_error: error,
      pinned_revision: predecessor,
      required_revision: { revision: predecessor, reachable_from_revision_id: predecessor.id, relation: "active" },
      revision_transition: null,
      attempts: [attempt],
      updated_at: NOW,
      finished_at: NOW,
    });
    this.runs = [run];
    this.timelines[run.id] = [
      this.timeline(1, "terminal", "failed", "Session failed", "Revision 1 remains active and no successor was committed.", run.id, error),
    ];
    this.logs[run.id] = [
      this.log(run, "core", "The session stopped before evolution outputs were committed."),
    ];
  }

  private finishRun(runId: string, successorGeneration: number): void {
    const run = this.requireRun(runId);
    const predecessor = run.pinned_revision;
    if (!predecessor || predecessor.generation + 1 !== successorGeneration) {
      throw new Error("The successor must directly follow the run's pinned revision.");
    }
    const successor = this.revision(successorGeneration, run.project_id);
    const transition = revisionTransitionV1Schema.parse({
      state: "active",
      predecessor_revision: predecessor,
      successor_revision: successor,
      progress_completed: 4,
      progress_total: 4,
      message: "The successor revision is active.",
      error: null,
      updated_at: NOW,
    });
    const attempt = {
      ...run.current_attempt!,
      status: "succeeded" as const,
      queued_reason: null,
      updated_at: NOW,
      finished_at: NOW,
    };
    const succeeded = {
      ...runV1Schema.parse({
        ...run,
        status: "succeeded",
        queued_reason: null,
        current_attempt: attempt,
        revision_transition: null,
        attempts: [...run.attempts.slice(0, -1), attempt],
        updated_at: NOW,
        finished_at: NOW,
      }),
      revision_transition: transition,
    } satisfies RunV1;
    this.replaceRun(succeeded);
    this.projects = this.projects.map((project) =>
      project.remote?.core_project_id === run.project_id
        ? projectV1Schema.parse({
            ...project,
            remote: project.remote ? { ...project.remote, active_revision: successor, observed_at: NOW } : null,
            updated_at: NOW,
          })
        : project,
    );
    this.createArtifacts(runId, successorGeneration);
    this.appendTimeline(runId, "revision", "succeeded", `Revision ${successorGeneration} active`, "The successor revision is ready for the next session.");
  }

  private createArtifacts(runId: string | null, generation: number): void {
    const run = runId === null ? null : this.requireRun(runId);
    const project = run
      ? this.projects.find((candidate) => candidate.remote?.core_project_id === run.project_id)
      : this.projects[0];
    if (!project) throw new Error("The artifact fixture project was not found.");
    const coreProjectId = run?.project_id ?? this.requireCoreProjectId(project);
    const modelRef = project.execution.codex_model ?? project.execution.hf_model;
    if (!modelRef) throw new Error("The artifact fixture project does not select a model.");
    const prior = this.artifacts;
    const variants: Array<{
      artifact_type: ArtifactV1["artifact_type"];
      target_id: string;
      display_name: string;
      summary: string;
    }> = [
      { artifact_type: "text_memory" as const, target_id: "text_memory", display_name: "Research memory", summary: "Durable findings and constraints from this session." },
      { artifact_type: "skill_bundle" as const, target_id: "skill_bundle", display_name: "Research skills", summary: "Reusable analysis and validation routines." },
      { artifact_type: "agent_system" as const, target_id: "agent_system", display_name: "Agent guidance", summary: "Updated operating guidance for the next session." },
    ];
    if (this.includeParametricMemory) {
      variants.push({ artifact_type: "parametric_memory", target_id: "parametric_memory", display_name: "Parametric memory", summary: "Selected adapter state for the next session." });
    }

    for (const variant of variants) {
      const artifactId = `artifact-${variant.target_id}-${generation}`;
      const parent = prior.find((artifact) => artifact.target_id === variant.target_id);
      const revision = this.revision(generation, coreProjectId);
      const sourceDatasetIds = runId === null ? [] : [`dataset-fixture-${generation}`];
      const metadata = variant.artifact_type === "text_memory"
        ? { record_count: generation, source_dataset_ids: sourceDatasetIds }
        : variant.artifact_type === "skill_bundle"
          ? { document_count: 2, root_document: "SKILL.md" as const }
          : variant.artifact_type === "agent_system"
            ? { target_path: "AGENTS.md" }
            : { adapter_id: `adapter-fixture-${generation}`, base_model_ref: modelRef, adapter_format: "lora" as const };
      const artifact = artifactV1Schema.parse({
        id: artifactId,
        project_id: coreProjectId,
        run_id: runId,
        target_id: variant.target_id,
        artifact_type: variant.artifact_type,
        display_name: variant.display_name,
        summary: variant.summary,
        content_sha256: generation % 2 === 0 ? A : D,
        byte_size: 640 + generation,
        produced_revision: revision,
        membership_revisions: [revision],
        lineage: {
          method_id: runId === null ? "fixture_seed" : `reference_${variant.target_id}`,
          job_id: runId === null ? `job-fixture-seed-${variant.target_id}` : `job-fixture-${generation}-${variant.target_id}`,
          source_dataset_ids: sourceDatasetIds,
          source_artifact_ids: parent ? [parent.id] : [],
        },
        compatibility: {
          execution_modes: [project.execution.mode],
          harness_ids: ["codex"],
          base_model_refs: [modelRef],
        },
        scores: [{ name: "quality", value: 0.82 + generation / 100 }],
        selected: true,
        promoted: true,
        release_enabled: variant.artifact_type !== "parametric_memory",
        metadata,
        created_at: NOW,
      });
      this.artifacts = [artifact, ...this.artifacts];
      this.contents.set(artifactId, this.makeContent(artifact, generation));
      this.diffs.set(artifactId, this.makeDiff(artifact, parent ?? null, generation));
    }
    this.emit();
  }

  private makeContent(artifact: ArtifactV1, generation: number): ArtifactContentV1 {
    const rawDocuments = artifact.artifact_type === "skill_bundle"
      ? [
          { document_id: `workflow-${generation}`, display_name: "Analysis workflow", relative_path: "SKILL.md", content: "# Analysis workflow\n\nValidate assumptions before comparing candidates." },
          { document_id: `verification-${generation}`, display_name: "Result verification", relative_path: "verification.md", content: "# Result verification\n\nRecord evidence and unresolved uncertainty." },
        ]
      : [{
          document_id: `${artifact.target_id}-${generation}`,
          display_name: artifact.display_name,
          relative_path: artifact.artifact_type === "agent_system" ? "AGENTS.md" : artifact.artifact_type === "text_memory" ? "memory.md" : "adapter.md",
          content: artifact.artifact_type === "text_memory"
            ? "# Research memory\n\n- Preserve validated constraints across sessions.\n- Recheck uncertain measurements before promotion."
            : "# Agent guidance\n\nPrefer reproducible evidence, surface uncertainty, and keep the final report concise.",
        }];
    const documents = rawDocuments.map((document) => ({
      ...document,
      mime_type: "text/markdown",
      content_sha256: A,
      byte_size: utf8ByteLength(document.content),
      truncated: false,
    }));
    const returnedBytes = documents.reduce((total, document) => total + document.byte_size, 0);
    return artifactContentV1Schema.parse({
      schema_version: "1",
      artifact_id: artifact.id,
      artifact_type: artifact.artifact_type,
      documents,
      total_documents: this.artifactTruncated ? documents.length + 2 : documents.length,
      total_utf8_bytes: this.artifactTruncated ? returnedBytes + 128 : returnedBytes,
      returned_utf8_bytes: returnedBytes,
      truncated: this.artifactTruncated,
    });
  }

  private makeDiff(artifact: ArtifactV1, parent: ArtifactV1 | null, generation: number): ArtifactDiffV1 {
    const previousArtifactId = parent?.id ?? `artifact-${artifact.target_id}-${Math.max(0, generation - 1)}`;
    const previousDigest = parent?.content_sha256 ?? B;
    const relativePath = artifact.artifact_type === "skill_bundle" ? "SKILL.md" : artifact.artifact_type === "agent_system" ? "AGENTS.md" : artifact.artifact_type === "text_memory" ? "memory.md" : "adapter.md";
    const oldDocument = { artifact_id: previousArtifactId, artifact_content_sha256: previousDigest, document_id: `document-${artifact.target_id}`, relative_path: relativePath, content_sha256: previousDigest };
    const newDocument = { artifact_id: artifact.id, artifact_content_sha256: artifact.content_sha256, document_id: `document-${artifact.target_id}`, relative_path: relativePath, content_sha256: artifact.content_sha256 };
    return artifactDiffV1Schema.parse({
      schema_version: "1",
      artifact_id: artifact.id,
      artifact_content_sha256: artifact.content_sha256,
      previous_artifact_id: previousArtifactId,
      previous_artifact_content_sha256: previousDigest,
      document_changes: [{
        kind: "modified",
        old_document: oldDocument,
        new_document: newDocument,
        hunks: [{
          old_document: oldDocument,
          new_document: newDocument,
          old_start: 0,
          old_count: 0,
          new_start: 1,
          new_count: 1,
          lines: [{ kind: "added", old_line_number: null, new_line_number: 1, text: `Added for Revision ${generation}: preserve evidence and uncertainty.` }],
        }],
      }],
      total_document_changes: 1,
      total_hunks: 1,
      total_lines: 1,
      truncated: false,
    });
  }

  private transitionRun(
    runId: string,
    state: "preparing" | "running",
    phase: TimelineEntryV1["phase"],
    timelineStatus: TimelineEntryV1["status"],
    title: string,
    summary: string,
  ): void {
    const run = this.requireRun(runId);
    const attempt = {
      ...run.current_attempt!,
      status: state,
      queued_reason: null,
      updated_at: NOW,
      started_at: run.current_attempt!.started_at ?? NOW,
      finished_at: null,
      error: null,
    };
    const next = runV1Schema.parse({
      ...run,
      status: state,
      queued_reason: null,
      current_attempt: attempt,
      current_error: null,
      attempts: [...run.attempts.slice(0, -1), attempt],
      started_at: run.started_at ?? NOW,
      finished_at: null,
      updated_at: NOW,
    });
    this.replaceRun(next);
    this.appendTimeline(runId, phase, timelineStatus, title, summary);
  }

  private appendTimeline(
    runId: string,
    phase: TimelineEntryV1["phase"],
    status: TimelineEntryV1["status"],
    title: string,
    summary: string,
  ): void {
    const sequence = (this.timelines[runId]?.length ?? 0) + 1;
    this.timelines[runId] = [
      ...(this.timelines[runId] ?? []),
      this.timeline(sequence, phase, status, title, summary, runId),
    ];
    this.emit();
  }

  private appendLog(
    runId: string,
    stream: LogEntryV1["stream"],
    message: string,
  ): void {
    const run = this.requireRun(runId);
    const observed = runV1Schema.parse({
      ...run,
      updated_at: this.fixtureEventTimestamp(runId),
    });
    this.replaceRun(observed);
    this.logs[runId] = [...(this.logs[runId] ?? []), this.log(observed, stream, message)];
    this.emit();
  }

  private whileRunActive(runId: string, action: () => void): void {
    const run = this.runs.find((item) => item.id === runId);
    if (!run || isTerminal(run.status)) return;
    action();
  }

  private fixtureEventTimestamp(runId: string): string {
    const eventCount = (this.timelines[runId]?.length ?? 0) + (this.logs[runId]?.length ?? 0) + 1;
    return new Date(Date.parse(NOW) + eventCount * 1_000).toISOString();
  }

  private log(
    run: RunV1,
    stream: LogEntryV1["stream"],
    message: string,
  ): LogEntryV1 {
    const sequence = (this.logs[run.id]?.length ?? 0) + 1;
    return logEntryV1Schema.parse({
      id: `log-fixture-${run.id}-${sequence}`,
      sequence,
      occurred_at: NOW,
      stream,
      level: "info",
      message,
      run_id: run.id,
      attempt_id: run.current_attempt_id,
      service_id: "service-control-fixture-1",
      content_sha256: sequence % 2 === 0 ? B : A,
    });
  }

  private timeline(
    sequence: number,
    phase: TimelineEntryV1["phase"],
    status: TimelineEntryV1["status"],
    title: string,
    message: string,
    runId = "run-fixture-1",
    error: RunV1["current_error"] = null,
  ): TimelineEntryV1 {
    const run = this.runs.find((item) => item.id === runId);
    return timelineEntryV1Schema.parse({
      id: `timeline-fixture-${sequence}-${phase}`,
      run_id: runId,
      attempt_id: run?.current_attempt_id ?? null,
      sequence,
      service_id: "service-control-fixture-1",
      phase,
      status,
      title,
      message,
      occurred_at: NOW,
      artifact_ids: [],
      content_sha256: sequence % 2 === 0 ? B : A,
      error,
    });
  }

  private makeProfile(connectionState: RemoteProfileV1["connection_state"]): RemoteProfileV1 {
    return remoteProfileV1Schema.parse({ ...structuredClone(CONTRACT_FIXTURE_V1.profile), connection_state: connectionState });
  }

  private makeProjectFixture(
    executionMode: ProjectV1["execution"]["mode"] = "self-deployed",
  ): ProjectV1 {
    const project = structuredClone(CONTRACT_FIXTURE_V1.project);
    const coreProjectId = this.fixtureCoreProjectId(project.project_id);
    const subscription = executionMode === "codex_subscription_transcript";
    const modelRef = subscription ? "gpt-5.5" : project.execution.hf_model;
    return projectV1Schema.parse({
      ...project,
      execution: {
        mode: executionMode,
        capture_mode: "transcript",
        token_level_metrics_available: false,
        codex_model: subscription ? modelRef : null,
        hf_model: subscription ? null : modelRef,
      },
      remote: project.remote ? {
        ...project.remote,
        core_project_id: coreProjectId,
        model_preparation: {
          ...project.remote.model_preparation,
          model_ref: modelRef,
        },
        active_revision: project.remote.active_revision
          ? { ...project.remote.active_revision, project_id: coreProjectId }
          : null,
      } : null,
      evolution: {
        targets: {
          text_memory: { enabled: true, method: "reference_text_memory", config: {} },
          skill_bundle: { enabled: true, method: "reference_skill_bundle", config: {} },
          agent_system: { enabled: true, method: "reference_agent_system", config: {} },
        },
      },
    });
  }

  private makeCapabilities(
    projectId: string,
    executionMode: ProjectV1["execution"]["mode"] = "self-deployed",
  ): ProjectCapabilitiesV1 {
    const envelope = structuredClone(CONTRACT_FIXTURE_V1.capabilities);
    const base = envelope.capabilities.targets[0];
    const target = (
      targetId: "text_memory" | "skill_bundle" | "agent_system",
      displayName: string,
      description: string,
    ) => {
      const methodId = `reference_${targetId}`;
      return {
        ...base,
        target_id: targetId,
        display_name: displayName,
        description,
        artifact_type: targetId,
        configured_default_method_id: methodId,
        effective_default_method_id: methodId,
        methods: base.methods.map((method) => ({
          ...method,
          method_id: methodId,
          display_name: displayName,
          output_artifact_types: [targetId],
          execution_modes: [executionMode === "self-deployed" ? "self_deployed" as const : "subscription" as const],
        })),
        accepted_methods: base.accepted_methods.map((method) => ({ ...method, method_id: methodId })),
      };
    };
    return projectCapabilitiesV1Schema.parse({
      ...envelope,
      project_id: projectId,
      capabilities: {
        ...envelope.capabilities,
        evaluated_profile: {
          ...envelope.capabilities.evaluated_profile,
          execution_mode: executionMode === "self-deployed" ? "self_deployed" : "subscription",
        },
        targets: [
          target("agent_system", "Agent guidance", "Improve instructions for future sessions."),
          target("skill_bundle", "Skills", "Build reusable research workflows."),
          target("text_memory", "Text memory", "Preserve durable findings across sessions."),
        ],
      },
    });
  }

  private makeValidation(project: ProjectV1, capabilities: ProjectCapabilitiesV1): ProjectValidationV1 {
    return projectValidationV1Schema.parse({
      schema_version: "1",
      project_id: project.project_id,
      project_etag: project.etag,
      registry_digest: capabilities.capabilities.registry_digest,
      valid: true,
      checks: [{ id: "project.valid", status: "ok", message: "Project validation succeeded.", target_id: null, method_id: null }],
      validated_at: NOW,
    });
  }

  private makeState(connection: "offline" | "online"): DesktopStateV1 {
    return desktopStateV1Schema.parse({
      ...structuredClone(CONTRACT_FIXTURE_V1.state),
      execution_mode_capabilities: this.executionModeCapabilities,
      core: connection === "online"
        ? structuredClone(CONTRACT_FIXTURE_V1.state.core)
        : {
            state: "offline",
            profile_id: CONTRACT_FIXTURE_V1.profile.profile_id,
            active_tunnel: false,
            operation_id: null,
            host_key_review: null,
            core: null,
            failure: {
              code: "connection_required",
              message: "Connect the remote workspace to continue.",
              retryable: true,
              next_action: "Connect when the remote server is available.",
            },
          },
      active_project: {
        ...structuredClone(CONTRACT_FIXTURE_V1.state.active_project),
        connection_state: connection === "online" ? "ready" : "offline",
      },
      pending_operation_ids: [],
    });
  }

  private makeNewUserState(): DesktopStateV1 {
    return desktopStateV1Schema.parse({
      schema_version: "1",
      observed_at: NOW,
      contract: { selected_major: 1, desktop_openapi_sha256: A, core_openapi_sha256: null, compatible: false },
      execution_mode_capabilities: this.executionModeCapabilities,
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
    });
  }

  private connectionState(
    state: DesktopStateV1["core"]["state"],
    options: { operationId?: string } = {},
  ): DesktopStateV1 {
    const operationStates = ["connecting", "host_key_review", "checking", "bootstrapping", "core_starting", "reconnecting"];
    return desktopStateV1Schema.parse({
      ...this.state,
      core: {
        state,
        profile_id: CONTRACT_FIXTURE_V1.profile.profile_id,
        active_tunnel: state === "online",
        operation_id: operationStates.includes(state) ? options.operationId ?? "operation-connect-fixture" : null,
        host_key_review: state === "host_key_review"
          ? {
              algorithm: "ssh-ed25519",
              fingerprint: "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            }
          : null,
        core: state === "online"
          ? { contract_version: "1", contract_digest: B, core_version: "1.0.0" }
          : null,
        failure: state === "offline"
          ? { code: "connection_required", message: "Connect the remote workspace to continue.", retryable: true, next_action: "Connect when available." }
          : null,
      },
      active_project: this.state.active_project
        ? { ...this.state.active_project, connection_state: state === "online" ? "ready" : "connecting" }
        : null,
    });
  }

  private makeServices(online: boolean, degraded: boolean): ServiceV1[] {
    const status: ServiceV1["status"] = online ? (degraded ? "degraded" : "running") : "unavailable";
    const execution = this.projects[0]?.execution;
    const modelRef = execution?.codex_model ?? execution?.hf_model ?? "open-models/research-model-fixture-1";
    return [
      { id: "service-runtime-fixture", display_name: "OpenEvo runtime", kind: "control" as const },
      { id: "service-model-fixture", display_name: "Model service", kind: "inference" as const },
      { id: "service-artifacts-fixture", display_name: "Evolution storage", kind: "artifact_store" as const },
    ].map((service) => serviceV1Schema.parse({
      ...service,
      status,
      status_message: online ? (degraded ? "Needs attention." : "Ready.") : "Available after connection.",
      restartable: service.kind !== "artifact_store",
      error: null,
      model_preparation: service.kind === "inference" ? {
        ...structuredClone(CONTRACT_FIXTURE_V1.project.remote.model_preparation),
        model_ref: modelRef,
        status: online ? "ready" : "unresolved",
        downloaded_bytes: online ? 1_024 : null,
        total_bytes: online ? 1_024 : null,
      } : null,
      updated_at: NOW,
      observed_at: NOW,
      etag: ETAG_D,
    }));
  }

  private makeOperation(
    kind: LocalOperationV1["operation_kind"],
    state: LocalOperationV1["state"],
    label: string,
    current: number,
    total: number,
    projectId = this.projects[0]?.project_id ?? "project-fixture-1",
  ): LocalOperationV1 {
    const terminal = ["succeeded", "failed", "cancelled"].includes(state);
    return localOperationV1Schema.parse({
      schema_version: "1",
      operation_id: `operation-${kind}-fixture`,
      operation_kind: kind,
      state,
      resource: { resource_type: kind.startsWith("profile") || kind === "host_key_accept" ? "profile" : "project", resource_id: kind.startsWith("profile") || kind === "host_key_accept" ? this.profiles[0]?.profile_id ?? "profile-fixture-1" : projectId },
      progress: { current, total, label },
      checks: [],
      result: null,
      error: null,
      created_at: NOW,
      started_at: NOW,
      finished_at: terminal ? NOW : null,
      etag: ETAG_D,
    });
  }

  private apiError(
    httpStatus: 409 | 410 | 412,
    code: string,
    message: string,
    category: "project" | "run",
    options: {
      retryable?: boolean;
      repairAction?: "openevo_can_retry" | "openevo_can_install" | "openevo_can_reconfigure" | "user_action_required" | "unsupported";
    } = {},
  ): DesktopApiError {
    return new DesktopApiError(apiErrorV1Schema.parse({
      schema_version: "1",
      request_id: `request-fixture-${httpStatus}`,
      code,
      http_status: httpStatus,
      message,
      severity: "blocking",
      category,
      retryable: options.retryable ?? true,
      repair_action: options.repairAction ?? "openevo_can_retry",
      next_action: httpStatus === 412 ? "Review the refreshed project and save again." : "Reload the current snapshot before retrying.",
      details: {},
      logs_ref: null,
    }));
  }

  private addEquivalentQueuedRun(projectId: string): void {
    const project = this.requireProject(projectId);
    const coreProjectId = this.requireCoreProjectId(project);
    const base = structuredClone(CONTRACT_FIXTURE_V1.run);
    const id = "run-equivalent-pending";
    const revision = this.revision(this.currentGeneration(project), coreProjectId);
    const queuedReason = { code: "admission_pending" as const, summary: "The original session is already queued.", retry_after_seconds: null };
    const attempt = {
      ...base.attempts[0],
      id: "attempt-equivalent-pending",
      run_id: id,
      status: "queued" as const,
      queued_reason: queuedReason,
      started_at: null,
      finished_at: null,
      error: null,
    };
    const run = runV1Schema.parse({
      ...base,
      id,
      project_id: coreProjectId,
      status: "queued",
      queued_reason: queuedReason,
      current_attempt_id: attempt.id,
      current_attempt: attempt,
      current_error: null,
      pinned_revision: revision,
      required_revision: { revision, reachable_from_revision_id: revision.id, relation: "active" },
      revision_transition: null,
      attempts: [attempt],
      created_at: NOW,
      updated_at: NOW,
      admitted_at: NOW,
      started_at: null,
      finished_at: null,
    });
    this.runs = [run, ...this.runs];
    this.timelines[run.id] = [this.timeline(1, "admission", "pending", "Session admitted", "The original session is already queued.", run.id)];
  }

  private revision(generation: number, projectId = this.projects[0]?.remote?.core_project_id ?? "core-project-fixture-1") {
    return {
      id: `revision-fixture-${generation}`,
      project_id: projectId,
      generation,
      manifest_sha256: generation % 2 === 0 ? A : C,
    } as const;
  }

  private currentGeneration(project: ProjectV1): number {
    return project.remote?.active_revision?.generation ?? 1;
  }

  private fixtureCoreProjectId(projectId: string): string {
    return `core-${projectId}`;
  }

  private requireCoreProjectId(project: ProjectV1): string {
    if (!project.remote) throw new Error("Project does not have a remote Core identity.");
    return project.remote.core_project_id;
  }

  private requireProfile(profileId: string): RemoteProfileV1 {
    const profile = this.profiles.find((item) => item.profile_id === profileId);
    if (!profile) throw new Error("Remote workspace was not found.");
    return profile;
  }

  private requireProject(projectId: string): ProjectV1 {
    const project = this.projects.find((item) => item.project_id === projectId);
    if (!project) throw new Error("Project was not found.");
    return project;
  }

  private requireRun(runId: string): RunV1 {
    const run = this.runs.find((item) => item.id === runId);
    if (!run) throw new Error("Session was not found.");
    return run;
  }

  private updateProfileConnection(profileId: string, connectionState: RemoteProfileV1["connection_state"]): void {
    this.profiles = this.profiles.map((profile) =>
      profile.profile_id === profileId ? remoteProfileV1Schema.parse({ ...profile, connection_state: connectionState, updated_at: NOW }) : profile,
    );
  }

  private replaceRun(run: RunV1): void {
    this.runs = this.runs.map((item) => (item.id === run.id ? run : item));
    this.emit();
  }

  private schedule(steps: number, action: () => void): void {
    const timer = setTimeout(() => {
      this.timers.delete(timer);
      action();
    }, steps * this.stepDelayMs);
    this.timers.add(timer);
  }

  private emit(): void {
    for (const listener of this.listeners) listener({ kind: "snapshot_changed" });
  }

  private checkIntent(
    intent: ProductMutationIntent | ProductResourceMutationIntent,
    signature: string,
    expectedEtag?: string,
  ): void {
    if (this.stream.status !== "fresh" || intent.streamEpoch !== this.stream.epoch) {
      throw new DesktopProductUserError("This view is out of date. Refresh before trying again.");
    }
    if (expectedEtag !== undefined && (!("etag" in intent) || intent.etag !== expectedEtag)) {
      throw new DesktopProductUserError("This item changed remotely. Refresh before trying again.");
    }
    const previous = this.actionSignatures.get(intent.actionId);
    if (previous !== undefined && previous !== signature) {
      throw new DesktopProductUserError("This action identity was already used for a different request.");
    }
    this.actionSignatures.set(intent.actionId, signature);
  }

  markStreamStale(): void {
    this.stream = { status: "stale", epoch: this.stream.epoch, reason: "event_gap" };
    for (const listener of this.listeners) listener({ kind: "stream_stale", reason: "event_gap" });
  }

  resetEventCursor(): void {
    this.stream = { status: "cursor_reset", epoch: this.stream.epoch, resumeFromEventId: null };
    for (const listener of this.listeners) listener({ kind: "cursor_reset", resumeFromEventId: null });
  }

  failNextProjectSave(): void {
    this.failProjectSave = true;
  }

  failNextProjectSaveWithUnknownError(): void {
    this.failProjectSaveWithUnknownError = true;
  }

  failNextProjectActivation(status: 409 | 412): void {
    this.nextProjectActivationStatus = status;
  }

  projectActivationActionIds(): readonly string[] {
    return [...this.projectActivationActions];
  }

  failNextRefresh(): void {
    this.failRefresh = true;
  }

  setCapabilitiesUnavailableUntilRefresh(): void {
    this.restoreCapabilitiesOnRefresh = this.capabilities;
    this.capabilityRefreshesBeforeRestore = 1;
    this.capabilities = null;
    this.validation = null;
    this.emit();
  }

  useEditableMethodSchema(): void {
    const project = this.projects[0];
    const capabilities = this.capabilities;
    if (!project || !capabilities) return;
    const defaultConfig = {
      prompt: "Keep durable findings.",
      iterations: 3,
      temperature: 0.1,
      strategy: "balanced",
      include_failures: false,
      advanced: { minimum_score: 0.5 },
      tags: ["research"],
    };
    const configSchema = {
      type: "object",
      additionalProperties: false,
      required: ["prompt", "iterations", "strategy", "include_failures", "advanced", "tags"],
      properties: {
        prompt: { type: "string", title: "Reflection prompt", minLength: 1, maxLength: 512 },
        iterations: { type: "integer", title: "Iterations", minimum: 1, maximum: 20 },
        temperature: { type: "number", title: "Temperature", minimum: 0, maximum: 2 },
        strategy: { type: "string", title: "Strategy", enum: ["balanced", "strict"] },
        include_failures: { type: "boolean", title: "Include failures" },
        advanced: {
          type: "object",
          title: "Advanced",
          additionalProperties: false,
          required: ["minimum_score"],
          properties: {
            minimum_score: { type: "number", title: "Minimum score", minimum: 0, maximum: 1 },
          },
        },
        tags: { type: "array", title: "Tags", items: { type: "string", maxLength: 64 }, maxItems: 8 },
      },
    };
    this.capabilities = projectCapabilitiesV1Schema.parse({
      ...capabilities,
      capabilities: {
        ...capabilities.capabilities,
        targets: capabilities.capabilities.targets.map((target) => target.target_id === "text_memory"
          ? {
              ...target,
              methods: target.methods.map((method) => method.method_id === "reference_text_memory"
                ? { ...method, config_schema_json: canonicalJsonString(configSchema), default_config_json: canonicalJsonString(defaultConfig) }
                : method),
            }
          : target),
      },
    });
    const updated = projectV1Schema.parse({
      ...project,
      evolution: {
        targets: {
          ...project.evolution.targets,
          text_memory: { enabled: true, method: "reference_text_memory", config: defaultConfig },
        },
      },
    });
    this.projects = [updated, ...this.projects.slice(1)];
    this.validation = this.makeValidation(updated, this.capabilities);
  }

  useEditableMethodSchemaWithPartialOverride(): void {
    this.useEditableMethodSchema();
    const project = this.projects[0];
    if (!project || !this.capabilities) return;
    const updated = projectV1Schema.parse({
      ...project,
      evolution: {
        targets: {
          ...project.evolution.targets,
          text_memory: { enabled: true, method: "reference_text_memory", config: { iterations: 5 } },
        },
      },
    });
    this.projects = [updated, ...this.projects.slice(1)];
    this.validation = this.makeValidation(updated, this.capabilities);
  }

  useNullEffectiveDefault(): void {
    const project = this.projects[0];
    const capabilities = this.capabilities;
    if (!project || !capabilities) return;
    this.capabilities = projectCapabilitiesV1Schema.parse({
      ...capabilities,
      capabilities: {
        ...capabilities.capabilities,
        targets: capabilities.capabilities.targets.map((target) => target.target_id === "text_memory"
          ? { ...target, effective_default_method_id: null }
          : target),
      },
    });
    const updated = projectV1Schema.parse({
      ...project,
      evolution: {
        targets: {
          ...project.evolution.targets,
          text_memory: { enabled: false, method: "reference_text_memory", config: {} },
        },
      },
    });
    this.projects = [updated, ...this.projects.slice(1)];
    this.validation = this.makeValidation(updated, this.capabilities);
  }

  useNullEffectiveDefaultWithoutSavedMethod(): void {
    this.useNullEffectiveDefault();
    const project = this.projects[0];
    if (!project || !this.capabilities) return;
    const updated = projectV1Schema.parse({
      ...project,
      evolution: {
        targets: {
          ...project.evolution.targets,
          text_memory: { enabled: false, method: null, config: {} },
        },
      },
    });
    this.projects = [updated, ...this.projects.slice(1)];
    this.validation = this.makeValidation(updated, this.capabilities);
  }

  useRunStateReviewScenario(): void {
    const project = this.projects[0];
    if (!project) return;
    const coreProjectId = this.requireCoreProjectId(project);
    const pinned = this.revision(2, coreProjectId);
    const base = structuredClone(CONTRACT_FIXTURE_V1.run);
    const makeRun = (
      id: string,
      status: RunV1["status"],
      updatedAt: string,
      options: { queued?: boolean; failed?: boolean } = {},
    ) => {
      const error = options.failed ? this.apiError(409, "model_load_failed", "The model worker could not load the selected model.", "run").apiError : null;
      const queuedReason = options.queued ? {
        code: "service_starting",
        summary: "The selected model is being prepared.",
        retry_after_seconds: 5,
      } as const : null;
      const attempt = {
        ...base.attempts[0],
        id: `attempt-${id}`,
        run_id: id,
        status,
        queued_reason: queuedReason,
        updated_at: updatedAt,
        started_at: status === "queued" ? null : updatedAt,
        finished_at: isTerminal(status) ? updatedAt : null,
        error,
      };
      return runV1Schema.parse({
        ...base,
        id,
        project_id: coreProjectId,
        status,
        queued_reason: queuedReason,
        current_attempt_id: attempt.id,
        current_attempt: attempt,
        current_error: error,
        pinned_revision: pinned,
        required_revision: { revision: pinned, reachable_from_revision_id: pinned.id, relation: "active" },
        revision_transition: null,
        attempts: [attempt],
        created_at: "2026-07-14T10:00:00Z",
        updated_at: updatedAt,
        admitted_at: NOW,
        started_at: status === "queued" ? null : updatedAt,
        finished_at: isTerminal(status) ? updatedAt : null,
      });
    };
    this.runs = [
      makeRun("run-succeeded", "succeeded", "2026-07-14T10:10:00Z"),
      makeRun("run-queued-model", "queued", "2026-07-14T10:40:00Z", { queued: true }),
      makeRun("run-cancelled", "cancelled", "2026-07-14T10:20:00Z"),
      makeRun("run-failed-model", "failed", "2026-07-14T10:30:00Z", { failed: true }),
    ];
    this.timelines["run-queued-model"] = [this.timeline(1, "admission", "pending", "Model preparation", "The selected model is being prepared.", "run-queued-model")];
    this.services = this.services.map((service) => service.kind === "inference"
      ? serviceV1Schema.parse({ ...service, status: "starting", status_message: "Preparing the selected model.", model_preparation: service.model_preparation ? { ...service.model_preparation, status: "downloading", downloaded_bytes: 512, total_bytes: 1_024 } : null })
      : service);
  }

  useEmptyServicesScenario(): void {
    this.services = [];
    this.emit();
  }

  useAuthoritativeArtifactOrderingScenario(): void {
    const project = this.projects[0];
    if (!project) return;
    const coreProjectId = this.requireCoreProjectId(project);
    const runId = "run-revision-4";
    const base = structuredClone(CONTRACT_FIXTURE_V1.run);
    const pinned = this.revision(2, coreProjectId);
    const attempt = { ...base.attempts[0], id: "attempt-revision-4", run_id: runId, status: "succeeded" as const, finished_at: NOW };
    const run = runV1Schema.parse({
      ...base,
      id: runId,
      project_id: coreProjectId,
      status: "succeeded",
      queued_reason: null,
      current_attempt_id: attempt.id,
      current_attempt: attempt,
      current_error: null,
      pinned_revision: pinned,
      required_revision: { revision: pinned, reachable_from_revision_id: pinned.id, relation: "active" },
      revision_transition: null,
      attempts: [attempt],
      finished_at: NOW,
    });
    this.runs = [run, ...this.runs];
    this.projects = [projectV1Schema.parse({ ...project, remote: project.remote ? { ...project.remote, active_revision: this.revision(4, coreProjectId) } : null }), ...this.projects.slice(1)];
    this.createArtifacts(runId, 4);
    const times: Record<string, string> = {
      parametric_memory: "2026-07-14T12:04:00Z",
      skill_bundle: "2026-07-14T12:03:00Z",
      text_memory: "2026-07-14T12:02:00Z",
      agent_system: "2026-07-14T12:01:00Z",
    };
    this.artifacts = this.artifacts.map((artifact) => artifact.membership_revisions.some((revision) => revision.id === "revision-fixture-4")
      ? artifactV1Schema.parse({ ...artifact, created_at: times[artifact.target_id] ?? NOW })
      : artifact);
    const source = this.artifacts.find((artifact) => artifact.target_id === "text_memory" && artifact.membership_revisions.some((revision) => revision.id === "revision-fixture-4"));
    if (source) {
      const additionalSelected = artifactV1Schema.parse({
        ...source,
        id: "artifact-selected-additional-memory",
        display_name: "Additional selected memory",
        summary: "Additional selected memory",
        lineage: { ...source.lineage, source_artifact_ids: [source.id] },
        selected: true,
        created_at: "2026-07-14T12:02:00Z",
      });
      this.contents.set(additionalSelected.id, this.makeContent(additionalSelected, 4));
      this.diffs.set(additionalSelected.id, this.makeDiff(additionalSelected, source, 4));
      this.artifacts = [artifactV1Schema.parse({
        ...source,
        id: "artifact-unselected-newer",
        display_name: "Unselected newer artifact",
        selected: false,
        created_at: "2026-07-14T12:04:00Z",
      }), additionalSelected, ...this.artifacts];
    }
  }

  markArtifactCollectionIncomplete(): void {
    this.artifactCollection = { status: "incomplete", reason: "pagination_pending" };
  }

  makeRevisionEvidenceUnknown(): void {
    const project = this.projects[0];
    if (!project) return;
    this.projects = [projectV1Schema.parse({ ...project, remote: project.remote ? { ...project.remote, status: "blocked", active_revision: null } : null }), ...this.projects.slice(1)];
    this.emit();
  }

  useRequiredRevisionIdentityConflict(): void {
    const project = this.projects[0];
    const activeRevision = project?.remote?.active_revision;
    const base = this.runs[0];
    if (!project || !activeRevision || !base?.current_attempt) return;
    const conflictingRevision = { ...activeRevision, manifest_sha256: activeRevision.manifest_sha256 === B ? C : B };
    const queuedReason = { code: "admission_pending" as const, summary: "Admission is being reconciled.", retry_after_seconds: null };
    const attempt = {
      ...base.current_attempt,
      status: "queued" as const,
      queued_reason: queuedReason,
      updated_at: NOW,
      started_at: null,
      finished_at: null,
      error: null,
    };
    this.runs = [runV1Schema.parse({
      ...base,
      status: "queued",
      queued_reason: queuedReason,
      current_attempt: attempt,
      current_error: null,
      pinned_revision: null,
      required_revision: { revision: conflictingRevision, reachable_from_revision_id: conflictingRevision.id, relation: "active" },
      revision_transition: null,
      attempts: [...base.attempts.slice(0, -1), attempt],
      admitted_at: null,
      started_at: null,
      finished_at: null,
      updated_at: NOW,
    })];
  }

  useTransitionPredecessorIdentityConflict(): void {
    const project = this.projects[0];
    const activeRevision = project?.remote?.active_revision;
    const base = this.runs[0];
    if (!project || !activeRevision || !base?.current_attempt) return;
    const predecessor = { ...activeRevision, manifest_sha256: activeRevision.manifest_sha256 === B ? C : B };
    const successor = this.revision(activeRevision.generation + 1, activeRevision.project_id);
    const queuedReason = { code: "required_revision_uncommitted" as const, summary: "The required successor is still committing.", retry_after_seconds: 1 };
    const attempt = {
      ...base.current_attempt,
      status: "queued" as const,
      queued_reason: queuedReason,
      updated_at: NOW,
      started_at: null,
      finished_at: null,
      error: null,
    };
    this.runs = [runV1Schema.parse({
      ...base,
      status: "queued",
      queued_reason: queuedReason,
      current_attempt: attempt,
      current_error: null,
      pinned_revision: null,
      required_revision: { revision: successor, reachable_from_revision_id: predecessor.id, relation: "successor" },
      revision_transition: {
        state: "committing",
        predecessor_revision: predecessor,
        successor_revision: successor,
        progress_completed: 3,
        progress_total: 4,
        message: "The successor revision is being committed.",
        error: null,
        updated_at: NOW,
      },
      attempts: [...base.attempts.slice(0, -1), attempt],
      admitted_at: null,
      started_at: null,
      finished_at: null,
      updated_at: NOW,
    })];
  }

  useArtifactMembershipIdentityConflict(): void {
    const activeRevision = this.projects[0]?.remote?.active_revision;
    if (!activeRevision) return;
    const conflictingRevision = { ...activeRevision, manifest_sha256: activeRevision.manifest_sha256 === B ? C : B };
    this.artifacts = this.artifacts.map((artifact) => artifact.membership_revisions.some((revision) => revision.id === activeRevision.id)
      ? artifactV1Schema.parse({
          ...artifact,
          membership_revisions: artifact.membership_revisions.map((revision) => revision.id === activeRevision.id ? conflictingRevision : revision),
        })
      : artifact);
  }

  useCrossWiredArtifactPayloads(): void {
    const selected = this.activeSelectedArtifacts();
    const current = selected[0];
    const other = selected[1];
    const otherContent = other ? this.contents.get(other.id) : null;
    const otherDiff = other ? this.diffs.get(other.id) : null;
    if (!current || !otherContent || !otherDiff) return;
    this.contents.set(current.id, structuredClone(otherContent));
    this.diffs.set(current.id, structuredClone(otherDiff));
  }

  useMismatchedArtifactDiffPreviousIdentity(): void {
    const selected = this.activeSelectedArtifacts();
    const current = selected[0];
    const wrongPrevious = this.artifacts.find((artifact) => artifact.id !== current?.id && artifact.target_id !== current?.target_id);
    if (!current || !wrongPrevious) return;
    const generation = this.projects[0]?.remote?.active_revision?.generation ?? 1;
    this.diffs.set(current.id, this.makeDiff(current, wrongPrevious, generation));
  }

  useDocumentLevelArtifactDiff(): void {
    const current = this.activeSelectedArtifacts()[0];
    const previousId = current?.lineage.source_artifact_ids[0];
    const previous = this.artifacts.find((artifact) => artifact.id === previousId);
    if (!current || !previous) return;
    const oldDocument = (relativePath: string, contentSha256: string) => ({
      artifact_id: previous.id,
      artifact_content_sha256: previous.content_sha256,
      document_id: `old-${relativePath}`,
      relative_path: relativePath,
      content_sha256: contentSha256,
    });
    const newDocument = (relativePath: string, contentSha256: string) => ({
      artifact_id: current.id,
      artifact_content_sha256: current.content_sha256,
      document_id: `new-${relativePath}`,
      relative_path: relativePath,
      content_sha256: contentSha256,
    });
    this.diffs.set(current.id, artifactDiffV1Schema.parse({
      schema_version: "1",
      artifact_id: current.id,
      artifact_content_sha256: current.content_sha256,
      previous_artifact_id: previous.id,
      previous_artifact_content_sha256: previous.content_sha256,
      document_changes: [
        { kind: "renamed", old_document: oldDocument("notes.md", A), new_document: newDocument("evidence.md", A), hunks: [] },
        { kind: "added", new_document: newDocument("empty-added.md", B), hunks: [] },
        { kind: "removed", old_document: oldDocument("empty-removed.md", B), hunks: [] },
      ],
      total_document_changes: 3,
      total_hunks: 0,
      total_lines: 0,
      truncated: false,
    }));
  }

  failNextProjectSaveWithStatus(status: 412): void {
    this.nextProjectSaveStatus = status;
  }

  failNextRunStartWithStatus(status: 409 | 410): void {
    this.nextRunStartStatus = status;
  }

  failNextRunStartWithConflict(options: {
    code: string;
    retryable: boolean;
    repairAction: "openevo_can_retry" | "openevo_can_install" | "openevo_can_reconfigure" | "user_action_required" | "unsupported";
    addEquivalentRun?: boolean;
  }): void {
    this.nextRunStartConflict = {
      ...options,
      message: options.addEquivalentRun
        ? "The original session is already queued."
        : "That action identity belongs to another request.",
      addEquivalentRun: options.addEquivalentRun ?? false,
    };
  }

  failNextProfileCreateWithUnknownError(): void {
    this.failProfileCreateWithUnknownError = true;
  }

  loseNextProfileCreateResponseAfterCommit(): void {
    this.loseProfileCreateResponseAfterCommit = true;
  }

  emitAuthoritativeRefresh(): void {
    this.emit();
  }

  restoreOnlineActiveProject(): void {
    const project = this.projects[0];
    if (!project) return;
    const remote = project.remote ?? structuredClone(CONTRACT_FIXTURE_V1.project.remote);
    const active = projectV1Schema.parse({
      ...project,
      state: "active",
      remote,
    });
    this.projects = this.projects.map((item) => item.project_id === active.project_id ? active : item);
    this.state = desktopStateV1Schema.parse({
      ...this.makeState("online"),
      active_project: {
        project_id: active.project_id,
        project_etag: active.etag,
        profile_id: active.profile_id,
        connection_state: "ready",
      },
    });
    this.capabilities = this.makeCapabilities(active.project_id, active.execution.mode);
    this.validation = this.makeValidation(active, this.capabilities);
    this.emit();
  }

  failNextProfileSaveWithUnknownError(): void {
    this.failProfileSaveWithUnknownError = true;
  }

  failNextProjectCreateWithUnknownError(): void {
    this.failProjectCreateWithUnknownError = true;
  }

  refreshCount(): number {
    return this.refreshAttempts;
  }

  projectUpdateAttempts(): number {
    return this.projectSaveAttempts;
  }

  profileCreateActionIds(): readonly string[] {
    return [...this.profileCreateActions];
  }

  profileUpdateActionIds(): readonly string[] {
    return [...this.profileUpdateActions];
  }

  projectCreateActionIds(): readonly string[] {
    return [...this.projectCreateActions];
  }

  projectUpdateActionIds(): readonly string[] {
    return [...this.projectUpdateActions];
  }

  runStartAttempts(): number {
    return this.runAdmissionAttempts;
  }

  private activeSelectedArtifacts(): ArtifactV1[] {
    const activeRevision = this.projects[0]?.remote?.active_revision;
    if (!activeRevision) return [];
    return this.artifacts
      .filter((artifact) => artifact.selected && artifact.membership_revisions.some((revision) => revision.id === activeRevision.id))
      .sort((left, right) => {
        const time = Date.parse(right.created_at) - Date.parse(left.created_at);
        return time || left.id.localeCompare(right.id);
      });
  }

  addDraftProject(options: { subscription?: boolean } = {}): ProjectV1 {
    const base = this.makeProjectFixture();
    const subscriptionFields = options.subscription ? {
      execution: {
        mode: "codex_subscription_transcript",
        capture_mode: "transcript",
        token_level_metrics_available: false,
        codex_model: "gpt-5.5",
        hf_model: null,
      },
      evolution: { targets: {} },
      evolution_configuration_state: "pending" as const,
    } : {};
    const project = projectV1Schema.parse({
      ...base,
      project_id: "project-fixture-2",
      name: "Second research project",
      task: {
        ...base.task,
        title: "Second research task",
        ...(options.subscription ? { objective: "Second project objective." } : {}),
      },
      ...subscriptionFields,
      state: "draft",
      remote: null,
      etag: ETAG_A,
    });
    this.projects = [...this.projects, project];
    this.emit();
    return structuredClone(project);
  }

  loseActiveCoreSession(): void {
    this.state = desktopStateV1Schema.parse({
      ...this.state,
      core: {
        state: "offline",
        profile_id: this.state.active_project?.profile_id ?? CONTRACT_FIXTURE_V1.profile.profile_id,
        active_tunnel: false,
        operation_id: null,
        host_key_review: null,
        core: null,
        failure: {
          code: "core_client_closed",
          message: "The active Core client closed.",
          retryable: true,
          next_action: "Reactivate the project.",
        },
      },
      active_project: this.state.active_project
        ? { ...this.state.active_project, connection_state: "offline" }
        : null,
    });
    this.emit();
  }

  useUnsupportedSavedMethod(): void {
    const project = this.projects[0];
    if (!project) return;
    const updated = projectV1Schema.parse({
      ...project,
      evolution_configuration_state: "configured",
      evolution: {
        targets: {
          ...project.evolution.targets,
          text_memory: { enabled: true, method: "removed_text_memory", config: { retained: true } },
        },
      },
    });
    this.projects = [updated, ...this.projects.slice(1)];
    this.validation = this.capabilities ? this.makeValidation(updated, this.capabilities) : null;
    this.emit();
  }

  useAcceptedSavedMethod(): void {
    const project = this.projects[0];
    const capabilities = this.capabilities;
    const target = capabilities?.capabilities.targets.find((item) => item.target_id === "text_memory");
    const accepted = target?.accepted_methods[0];
    if (!project || !capabilities || !target || !accepted) return;
    this.capabilities = projectCapabilitiesV1Schema.parse({
      ...capabilities,
      capabilities: {
        ...capabilities.capabilities,
        targets: capabilities.capabilities.targets.map((item) => item.target_id === target.target_id
          ? { ...item, accepted_methods: [...item.accepted_methods, { ...accepted, method_id: "hidden_text_memory" }].sort((left, right) => left.method_id.localeCompare(right.method_id)) }
          : item),
      },
    });
    const updated = projectV1Schema.parse({
      ...project,
      evolution: { targets: { ...project.evolution.targets, text_memory: { enabled: true, method: "hidden_text_memory", config: { retained: true } } } },
    });
    this.projects = [updated, ...this.projects.slice(1)];
    this.validation = this.makeValidation(updated, this.capabilities);
    this.emit();
  }

  useResolverSavedMethod(): void {
    const project = this.projects[0];
    const capabilities = this.capabilities;
    const target = capabilities?.capabilities.targets.find((item) => item.target_id === "text_memory");
    const resolved = target?.accepted_methods[0];
    if (!project || !capabilities || !target || !resolved) return;
    this.capabilities = projectCapabilitiesV1Schema.parse({
      ...capabilities,
      capabilities: {
        ...capabilities.capabilities,
        targets: capabilities.capabilities.targets.map((item) => item.target_id === target.target_id
          ? { ...item, selection_resolvers: [{ selection_value: "auto", display_name: "Automatic", description: "Core selects from accepted methods.", resolved_methods: [resolved] }] }
          : item),
      },
    });
    const updated = projectV1Schema.parse({
      ...project,
      evolution: { targets: { ...project.evolution.targets, text_memory: { enabled: true, method: "auto", config: {} } } },
    });
    this.projects = [updated, ...this.projects.slice(1)];
    this.validation = this.makeValidation(updated, this.capabilities);
    this.emit();
  }

  clearEvolutionSelections(state: "pending" | "configured" = "configured"): void {
    const project = this.projects[0];
    if (!project) return;
    const updated = projectV1Schema.parse({
      ...project,
      evolution: { targets: {} },
      evolution_configuration_state: state,
    });
    this.projects = [updated, ...this.projects.slice(1)];
    this.validation = this.capabilities ? this.makeValidation(updated, this.capabilities) : null;
    this.emit();
  }
}

export function createFixtureDesktopProductProvider(
  options?: FixtureProviderOptions,
): FixtureDesktopProductProvider {
  return new FixtureDesktopProductProvider(options);
}

function isTerminal(state: RunV1["status"]): boolean {
  return state === "succeeded" || state === "failed" || state === "cancelled";
}

function capabilityExecutionMode(capabilities: ProjectCapabilitiesV1): ProjectV1["execution"]["mode"] {
  return capabilities.capabilities.evaluated_profile.execution_mode === "self_deployed"
    ? "self-deployed"
    : "codex_subscription_transcript";
}

function utf8ByteLength(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function canonicalJsonString(value: unknown): string {
  return JSON.stringify(sortCanonicalJson(value));
}

function sortCanonicalJson(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortCanonicalJson);
  if (typeof value !== "object" || value === null) return value;
  return Object.fromEntries(
    Object.entries(value)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, child]) => [key, sortCanonicalJson(child)]),
  );
}
