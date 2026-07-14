import { CONTRACT_FIXTURE_V1 } from "../api/v1/fixtures";
import { DesktopApiError } from "../api/v1/client";
import {
  apiErrorV1Schema,
  artifactContentV1Schema,
  artifactDiffV1Schema,
  artifactV1Schema,
  desktopStateV1Schema,
  diagnosticReportV1Schema,
  localOperationV1Schema,
  projectCapabilitiesV1Schema,
  projectSourceV1Schema,
  projectValidationV1Schema,
  projectV1Schema,
  remoteProfileV1Schema,
  runV1Schema,
  serviceV1Schema,
  timelineEntryV1Schema,
  type ArtifactContentV1,
  type ArtifactDiffV1,
  type ArtifactV1,
  type DesktopStateV1,
  type DiagnosticReportV1,
  type HostKeyAcceptV1,
  type LocalOperationV1,
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
const ETAG_D = `"${D}"`;
const NOW = "2026-07-14T12:00:00Z";

export interface FixtureProviderOptions {
  startOnline?: boolean;
  seedCompletedRun?: boolean;
  artifactTruncated?: boolean;
  degraded?: boolean;
  stepDelayMs?: number;
  newUser?: boolean;
}

export class FixtureDesktopProductProvider implements DesktopProductProvider {
  readonly providerKind = "contract_simulator" as const;
  private readonly listeners = new Set<(signal: ProductSubscriptionSignal) => void>();
  private readonly timers = new Set<ReturnType<typeof setTimeout>>();
  private readonly stepDelayMs: number;
  private readonly artifactTruncated: boolean;
  private state: DesktopStateV1;
  private profiles: RemoteProfileV1[];
  private projects: ProjectV1[];
  private runs: RunV1[] = [];
  private timelines: Record<string, TimelineEntryV1[]> = {};
  private artifacts: ArtifactV1[] = [];
  private services: ServiceV1[];
  private capabilities: ProjectCapabilitiesV1 | null;
  private validation: ProjectValidationV1 | null;
  private stream: DesktopProductSnapshot["stream"] = { status: "fresh", epoch: 1, lastEventId: null };
  private readonly actionSignatures = new Map<string, string>();
  private failProjectSave = false;
  private failProjectSaveWithUnknownError = false;
  private failRefresh = false;
  private restoreCapabilitiesOnRefresh: ProjectCapabilitiesV1 | null = null;
  private capabilityRefreshesBeforeRestore = 0;
  private nextProjectSaveStatus: 412 | null = null;
  private nextRunStartStatus: 409 | 410 | null = null;
  private refreshAttempts = 0;
  private projectSaveAttempts = 0;
  private runAdmissionAttempts = 0;
  private diagnostic: DiagnosticReportV1 | null;
  private activeOperation: LocalOperationV1 | null = null;
  private readonly contents = new Map<string, ArtifactContentV1>();
  private readonly diffs = new Map<string, ArtifactDiffV1>();

  constructor(options: FixtureProviderOptions = {}) {
    if (import.meta.env.PROD) {
      throw new Error("The contract simulator is unavailable in release builds.");
    }
    this.stepDelayMs = options.stepDelayMs ?? 80;
    this.artifactTruncated = options.artifactTruncated ?? false;
    const online = options.startOnline ?? false;
    const newUser = options.newUser ?? (!online && !options.seedCompletedRun);
    this.profiles = newUser ? [] : [this.makeProfile(online ? "connected" : "disconnected")];
    this.projects = newUser ? [] : [this.makeProjectFixture()];
    this.state = newUser ? this.makeNewUserState() : this.makeState(online ? "online" : "offline");
    this.capabilities = online
      ? this.makeCapabilities(this.projects[0]?.project_id ?? "project-fixture-1")
      : null;
    this.validation = this.capabilities && this.projects[0]
      ? this.makeValidation(this.projects[0], this.capabilities)
      : null;
    this.services = newUser ? [] : this.makeServices(online, options.degraded ?? false);
    this.diagnostic = online ? this.makeDiagnostic(options.degraded ?? false) : null;

    if (options.seedCompletedRun) {
      this.seedCompletedRun();
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
      ? { status: "ready" as const, projectId: this.capabilities.project_id, executionMode: this.capabilities.execution_mode, value: this.capabilities }
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
      profiles: this.profiles,
      projects: this.projects,
      runs: this.runs,
      timelines: this.timelines,
      artifacts: this.artifacts,
      services: this.services,
      capability,
      validation,
      diagnostic: this.diagnostic,
      activeOperation: this.activeOperation,
      stream: this.stream,
    });
  }

  subscribe(listener: (signal: ProductSubscriptionSignal) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  async createProfile(input: ProfileCreateV1, intent: ProductMutationIntent): Promise<RemoteProfileV1> {
    this.checkIntent(intent, "profile:create");
    const credentialKinds = credentialKindsForProfile(input.authentication_kind ?? "ssh_agent", input.proxy);
    const profile = remoteProfileV1Schema.parse({
      schema_version: "1",
      profile_id: `profile-fixture-${this.profiles.length + 1}`,
      ...input,
      credential_slots: credentialKinds.map((kind) => ({ kind, status: "empty", updated_at: null })),
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
    return structuredClone(profile);
  }

  async updateProfile(profileId: string, input: ProfilePatchV1, intent: ProductResourceMutationIntent): Promise<RemoteProfileV1> {
    const current = this.requireProfile(profileId);
    this.checkIntent(intent, `profile:update:${profileId}`, current.etag);
    const authenticationKind = input.authentication_kind ?? current.authentication_kind;
    const proxy = input.proxy ?? current.proxy;
    const requiredKinds = credentialKindsForProfile(authenticationKind, proxy);
    const credentialSlots = requiredKinds.map((kind) =>
      current.credential_slots.find((slot) => slot.kind === kind) ?? { kind, status: "empty" as const, updated_at: null },
    );
    const updated = remoteProfileV1Schema.parse({
      ...current,
      ...input,
      authentication_kind: authenticationKind,
      credential_slots: credentialSlots,
      proxy,
      etag: ETAG_D,
      updated_at: NOW,
    });
    this.profiles = this.profiles.map((profile) => profile.profile_id === profileId ? updated : profile);
    this.emit();
    return structuredClone(updated);
  }

  async configureCredential(
    profileId: string,
    slotKind: RemoteProfileV1["credential_slots"][number]["kind"],
    intent: ProductResourceMutationIntent,
  ): Promise<RemoteProfileV1> {
    const current = this.requireProfile(profileId);
    this.checkIntent(intent, `profile:credential:${profileId}:${slotKind}`, current.etag);
    const slot = current.credential_slots.find((item) => item.kind === slotKind);
    if (!slot) throw new Error("This credential is not used by the selected authentication method.");
    const updated = remoteProfileV1Schema.parse({
      ...current,
      credential_slots: current.credential_slots.map((item) => item.kind === slotKind ? { ...item, status: "stored", updated_at: NOW } : item),
      updated_at: NOW,
      etag: ETAG_D,
    });
    this.profiles = this.profiles.map((profile) => profile.profile_id === profileId ? updated : profile);
    this.emit();
    return structuredClone(updated);
  }

  async connectProfile(profileId: string, intent: ProductResourceMutationIntent): Promise<LocalOperationV1> {
    const profile = this.requireProfile(profileId);
    this.checkIntent(intent, `profile:connect:${profileId}`, profile.etag);
    const requiredCredential = credentialKindsForAuth(profile.authentication_kind)[0];
    if (requiredCredential && profile.credential_slots.find((slot) => slot.kind === requiredCredential)?.status !== "stored") {
      throw new Error("Configure the required credential before connecting.");
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
      this.capabilities = this.makeCapabilities(this.projects[0]?.project_id ?? "project-fixture-1");
      const activeProject = this.projects[0];
      this.validation = activeProject ? this.makeValidation(activeProject, this.capabilities) : null;
      this.services = this.makeServices(true, false);
      this.diagnostic = this.makeDiagnostic(false);
      this.emit();
    });
    return structuredClone(this.activeOperation);
  }

  async createProject(input: ProjectCreateV1, intent: ProductMutationIntent): Promise<ProjectV1> {
    this.checkIntent(intent, "project:create");
    const project = projectV1Schema.parse({
      schema_version: "1",
      project_id: `project-fixture-${this.projects.length + 1}`,
      ...input,
      state: "draft",
      current_revision_id: "revision-fixture-1",
      etag: ETAG_D,
      created_at: NOW,
      updated_at: NOW,
    });
    this.projects = [...this.projects, project];
    this.capabilities = this.state.core.state === "online"
      ? this.makeCapabilities(project.project_id)
      : null;
    this.validation = this.capabilities ? this.makeValidation(project, this.capabilities) : null;
    this.emit();
    return structuredClone(project);
  }

  async updateProject(projectId: string, input: ProjectPatchV1, intent: ProductResourceMutationIntent): Promise<ProjectV1> {
    this.projectSaveAttempts += 1;
    const current = this.requireProject(projectId);
    this.checkIntent(intent, `project:update:${projectId}`, current.etag);
    if (this.nextProjectSaveStatus) {
      const status = this.nextProjectSaveStatus;
      this.nextProjectSaveStatus = null;
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
    const updated = projectV1Schema.parse({ ...current, ...input, etag: ETAG_D, updated_at: NOW });
    this.projects = this.projects.map((project) => (project.project_id === projectId ? updated : project));
    this.capabilities = this.state.core.state === "online" ? this.makeCapabilities(projectId, updated.execution.mode) : null;
    this.validation = this.capabilities ? this.makeValidation(updated, this.capabilities) : null;
    if (this.state.active_project?.project_id === projectId) {
      this.state = desktopStateV1Schema.parse({ ...this.state, active_project: { ...this.state.active_project, project_etag: updated.etag } });
    }
    this.emit();
    return structuredClone(updated);
  }

  async activateProject(projectId: string, intent: ProductResourceMutationIntent): Promise<LocalOperationV1> {
    const project = this.requireProject(projectId);
    this.checkIntent(intent, `project:activate:${projectId}`, project.etag);
    const activated = projectV1Schema.parse({ ...project, state: "active", updated_at: NOW });
    this.projects = this.projects.map((item) => item.project_id === projectId ? activated : item);
    this.capabilities = this.state.core.state === "online" ? this.makeCapabilities(projectId, activated.execution.mode) : null;
    this.validation = this.capabilities ? this.makeValidation(activated, this.capabilities) : null;
    this.activeOperation = this.makeOperation("project_activate", "succeeded", "Project ready", 1, 1, project.project_id);
    this.state = desktopStateV1Schema.parse({
      ...this.state,
      active_project: {
        project_id: project.project_id,
        project_etag: activated.etag,
        profile_id: project.profile_id,
        connection_state: this.state.core.state === "online" ? "ready" : "offline",
      },
    });
    this.emit();
    return structuredClone(this.activeOperation);
  }

  async syncProjectWorkspace(projectId: string, intent: ProductResourceMutationIntent): Promise<LocalOperationV1> {
    const project = this.requireProject(projectId);
    this.checkIntent(intent, `project:workspace-sync:${projectId}`, project.etag);
    this.activeOperation = this.makeOperation("workspace_sync", "succeeded", "Workspace ready", 1, 1, projectId);
    this.emit();
    return structuredClone(this.activeOperation);
  }

  async selectProjectSource(intent: ProjectSourceSelectionIntent): Promise<ProjectSourceV1> {
    this.checkIntent(intent, "project:source-select");
    return projectSourceV1Schema.parse({
      kind: "native_folder_snapshot",
      display_name: "Selected research folder",
      source_ref: { content_id: "source-fixture-1", sha256: C, byte_size: 4096 },
    });
  }

  async startRun(intent: ProductRunIntent): Promise<RunV1> {
    this.runAdmissionAttempts += 1;
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
    if (this.runs.some((run) => !isTerminal(run.state))) {
      throw new Error("A session is already active.");
    }

    const generation = this.currentGeneration(project);
    const runNumber = this.runs.length + 1;
    const runId = `run-fixture-${runNumber}`;
    const run = runV1Schema.parse({
      schema_version: "1",
      run_id: runId,
      project_id: projectId,
      state: "queued",
      queued_reason: {
        code: "service_starting",
        summary: "Preparing the remote workspace.",
        retry_after_seconds: 1,
      },
      project_snapshot: { snapshot_id: `project-snapshot-${runNumber}`, digest: A },
      task_snapshot: { snapshot_id: `task-snapshot-${runNumber}`, digest: B },
      workspace_snapshot: { snapshot_id: `workspace-snapshot-${runNumber}`, digest: C },
      capability_registry_digest: B,
      pinned_revision: this.revision(generation, "active"),
      successor_revision: null,
      latest_attempt: {
        attempt_id: `attempt-fixture-${runNumber}`,
        number: 1,
        state: "queued",
        started_at: null,
        finished_at: null,
      },
      created_at: NOW,
      updated_at: NOW,
      etag: ETAG_A,
      error: null,
    });
    this.runs = [run, ...this.runs];
    this.timelines[runId] = [
      this.timeline(runNumber, "admission", "queued", "Session admitted", `Waiting for Revision ${generation}.`),
    ];
    this.emit();

    this.schedule(1, () => this.transitionRun(runId, "preparing", "workspace", "running", "Preparing workspace", "The project snapshot is being prepared."));
    this.schedule(2, () => this.transitionRun(runId, "running", "agent", "running", "Research task", "The task is running with its pinned revision."));
    this.schedule(3, () => this.appendTimeline(runId, "capture", "succeeded", "Session captured", "The session record is sealed."));
    this.schedule(4, () => {
      this.setSuccessor(runId, generation + 1, "queued");
      this.appendTimeline(runId, "evolution", "running", "Updating evolution targets", "Memory, skills, and instructions are being updated.");
    });
    this.schedule(5, () => {
      this.setSuccessor(runId, generation + 1, "preparing");
      this.appendTimeline(runId, "materialization", "running", "Preparing next revision", "Validated outputs are being assembled atomically.");
    });
    this.schedule(6, () => this.finishRun(runId, generation + 1));
    return structuredClone(run);
  }

  async cancelRun(runId: string, intent: ProductResourceMutationIntent): Promise<RunV1> {
    const run = this.requireRun(runId);
    this.checkIntent(intent, `run:cancel:${runId}`, run.etag);
    if (isTerminal(run.state)) return structuredClone(run);
    const cancelled = runV1Schema.parse({
      ...run,
      state: "cancelled",
      queued_reason: null,
      successor_revision: run.successor_revision
        ? { ...run.successor_revision, state: "cancelled" }
        : null,
      latest_attempt: { ...run.latest_attempt, state: "cancelled", finished_at: NOW },
      updated_at: NOW,
    });
    this.replaceRun(cancelled);
    this.appendTimeline(runId, "agent", "cancelled", "Session cancelled", "No successor revision was activated.");
    return structuredClone(cancelled);
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

  async repairProject(projectId: string, intent: ProductResourceMutationIntent): Promise<LocalOperationV1> {
    const project = this.requireProject(projectId);
    this.checkIntent(intent, `project:repair:${projectId}`, project.etag);
    this.activeOperation = this.makeOperation("project_repair", "running", "Applying repair", 1, 2, projectId);
    this.emit();
    this.schedule(1, () => {
      this.services = this.makeServices(true, false);
      this.diagnostic = this.makeDiagnostic(false);
      this.state = this.connectionState("online");
      this.activeOperation = null;
      this.emit();
    });
    return structuredClone(this.activeOperation);
  }

  async restartService(serviceId: string, intent: ProductResourceMutationIntent): Promise<LocalOperationV1> {
    const service = this.services.find((item) => item.service_id === serviceId);
    if (!service || !service.restart_supported) throw new Error("This service cannot be restarted here.");
    this.checkIntent(intent, `service:restart:${serviceId}`, service.etag);
    this.services = this.services.map((item) =>
      item.service_id === serviceId ? serviceV1Schema.parse({ ...item, state: "starting", health_summary: "Restarting." }) : item,
    );
    this.activeOperation = this.makeOperation("service_restart", "running", "Restarting service", 1, 2);
    this.emit();
    this.schedule(1, () => {
      this.services = this.services.map((item) =>
        item.service_id === serviceId ? serviceV1Schema.parse({ ...item, state: "healthy", health_summary: "Ready." }) : item,
      );
      this.activeOperation = null;
      this.emit();
    });
    return structuredClone(this.activeOperation);
  }

  dispose(): void {
    for (const timer of this.timers) clearTimeout(timer);
    this.timers.clear();
    this.listeners.clear();
  }

  private seedCompletedRun(): void {
    const project = this.projects[0];
    if (!project) return;
    const run = runV1Schema.parse({
      ...structuredClone(CONTRACT_FIXTURE_V1.run),
      state: "succeeded",
      queued_reason: null,
      pinned_revision: this.revision(1, "active"),
      successor_revision: this.revision(2, "active"),
      latest_attempt: {
        ...structuredClone(CONTRACT_FIXTURE_V1.run.latest_attempt),
        state: "succeeded",
        finished_at: NOW,
      },
    });
    this.runs = [run];
    this.timelines[run.run_id] = [
      this.timeline(1, "revision", "succeeded", "Revision 2 active", "The next session will use the new revision."),
    ];
    this.projects = [{ ...project, current_revision_id: "revision-fixture-2" }];
    this.createArtifacts(run.run_id, 2);
  }

  private finishRun(runId: string, successorGeneration: number): void {
    const run = this.requireRun(runId);
    const succeeded = runV1Schema.parse({
      ...run,
      state: "succeeded",
      queued_reason: null,
      successor_revision: this.revision(successorGeneration, "active"),
      latest_attempt: {
        ...run.latest_attempt,
        state: "succeeded",
        finished_at: NOW,
      },
      updated_at: NOW,
    });
    this.replaceRun(succeeded);
    this.projects = this.projects.map((project) =>
      project.project_id === run.project_id
        ? { ...project, current_revision_id: `revision-fixture-${successorGeneration}`, updated_at: NOW }
        : project,
    );
    this.createArtifacts(runId, successorGeneration);
    this.appendTimeline(runId, "revision", "succeeded", `Revision ${successorGeneration} active`, "The successor revision is ready for the next session.");
  }

  private createArtifacts(runId: string, generation: number): void {
    const prior = this.artifacts;
    const variants = [
      {
        artifact_type: "text_memory" as const,
        format: "markdown" as const,
        target_id: "text_memory",
        display_name: "Research memory",
        summary: "Durable findings and constraints from this session.",
      },
      {
        artifact_type: "skill_bundle" as const,
        skill_count: 2,
        target_id: "skill_bundle",
        display_name: "Research skills",
        summary: "Reusable analysis and validation routines.",
      },
      {
        artifact_type: "agent_system" as const,
        instruction_kind: "agents" as const,
        target_id: "agent_system",
        display_name: "Agent guidance",
        summary: "Updated operating guidance for the next session.",
      },
    ];

    for (const variant of variants) {
      const artifactId = `artifact-${variant.target_id}-${generation}`;
      const parent = prior.find((artifact) => artifact.target_id === variant.target_id);
      const artifact = artifactV1Schema.parse({
        schema_version: "1",
        artifact_id: artifactId,
        project_id: this.projects[0]?.project_id ?? "project-fixture-1",
        run_id: runId,
        content_digest: generation % 2 === 0 ? A : D,
        byte_size: 640 + generation,
        lineage: {
          source_dataset_ids: [`dataset-fixture-${generation}`],
          parent_artifact_ids: parent ? [parent.artifact_id] : [],
          producing_job_id: `job-fixture-${generation}-${variant.target_id}`,
        },
        compatibility: {
          execution_modes: ["self-deployed"],
          harness_ids: ["codex"],
          base_model_ids: [],
        },
        scores: [{ name: "quality", value: 0.82 + generation / 100 }],
        selected: true,
        promoted: true,
        revision_ids: [`revision-fixture-${generation}`],
        created_at: NOW,
        ...variant,
      });
      this.artifacts = [artifact, ...this.artifacts];
      this.contents.set(artifactId, this.makeContent(artifact, generation));
      this.diffs.set(artifactId, this.makeDiff(artifact, parent ?? null, generation));
    }
    this.emit();
  }

  private makeContent(artifact: ArtifactV1, generation: number): ArtifactContentV1 {
    const documents = artifact.artifact_type === "skill_bundle"
      ? [
          { document_id: `workflow-${generation}`, title: "Analysis workflow", media_type: "text/markdown" as const, content: "# Analysis workflow\n\nValidate assumptions before comparing candidates." },
          { document_id: `verification-${generation}`, title: "Result verification", media_type: "text/markdown" as const, content: "# Result verification\n\nRecord evidence and unresolved uncertainty." },
        ]
      : [{
          document_id: `${artifact.target_id}-${generation}`,
          title: artifact.display_name,
          media_type: "text/markdown" as const,
          content: artifact.artifact_type === "text_memory"
            ? "# Research memory\n\n- Preserve validated constraints across sessions.\n- Recheck uncertain measurements before promotion."
            : "# Agent guidance\n\nPrefer reproducible evidence, surface uncertainty, and keep the final report concise.",
        }];
    return artifactContentV1Schema.parse({
      schema_version: "1",
      artifact_id: artifact.artifact_id,
      content_digest: artifact.content_digest,
      documents,
      total_documents: this.artifactTruncated ? documents.length + 2 : documents.length,
      truncated: this.artifactTruncated,
    });
  }

  private makeDiff(artifact: ArtifactV1, parent: ArtifactV1 | null, generation: number): ArtifactDiffV1 {
    return artifactDiffV1Schema.parse({
      schema_version: "1",
      artifact_id: artifact.artifact_id,
      base_artifact_id: parent?.artifact_id ?? null,
      hunks: [{
        hunk_id: `hunk-${artifact.target_id}-${generation}`,
        heading: artifact.display_name,
        lines: [
          ...(parent ? [{ kind: "context" as const, old_line: 1, new_line: 1, text: "Validated guidance" }] : []),
          { kind: "added" as const, old_line: null, new_line: parent ? 2 : 1, text: `Added for Revision ${generation}: preserve evidence and uncertainty.` },
        ],
      }],
      truncated: false,
    });
  }

  private transitionRun(
    runId: string,
    state: "preparing" | "running",
    stage: TimelineEntryV1["stage"],
    timelineState: TimelineEntryV1["state"],
    title: string,
    summary: string,
  ): void {
    const run = this.requireRun(runId);
    const next = runV1Schema.parse({
      ...run,
      state,
      queued_reason: null,
      latest_attempt: {
        ...run.latest_attempt,
        state,
        started_at: run.latest_attempt.started_at ?? NOW,
      },
      updated_at: NOW,
    });
    this.replaceRun(next);
    this.appendTimeline(runId, stage, timelineState, title, summary);
  }

  private setSuccessor(runId: string, generation: number, state: "queued" | "preparing"): void {
    const run = this.requireRun(runId);
    this.replaceRun(runV1Schema.parse({ ...run, successor_revision: this.revision(generation, state), updated_at: NOW }));
  }

  private appendTimeline(
    runId: string,
    stage: TimelineEntryV1["stage"],
    state: TimelineEntryV1["state"],
    title: string,
    summary: string,
  ): void {
    const sequence = (this.timelines[runId]?.length ?? 0) + 1;
    this.timelines[runId] = [
      ...(this.timelines[runId] ?? []),
      this.timeline(sequence, stage, state, title, summary),
    ];
    this.emit();
  }

  private timeline(
    sequence: number,
    stage: TimelineEntryV1["stage"],
    state: TimelineEntryV1["state"],
    title: string,
    summary: string,
  ): TimelineEntryV1 {
    return timelineEntryV1Schema.parse({
      entry_id: `timeline-fixture-${sequence}-${stage}`,
      occurred_at: NOW,
      stage,
      state,
      title,
      summary,
      progress: null,
    });
  }

  private makeProfile(connectionState: RemoteProfileV1["connection_state"]): RemoteProfileV1 {
    return remoteProfileV1Schema.parse({ ...structuredClone(CONTRACT_FIXTURE_V1.profile), connection_state: connectionState });
  }

  private makeProjectFixture(): ProjectV1 {
    return projectV1Schema.parse({
      ...structuredClone(CONTRACT_FIXTURE_V1.project),
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
    const base = structuredClone(CONTRACT_FIXTURE_V1.capabilities.targets[0]);
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
        methods: base.methods.map((method) => ({ ...method, method_id: methodId, display_name: displayName })),
        accepted_methods: base.accepted_methods.map((method) => ({ ...method, method_id: methodId })),
      };
    };
    return projectCapabilitiesV1Schema.parse({
      ...structuredClone(CONTRACT_FIXTURE_V1.capabilities),
      project_id: projectId,
      execution_mode: executionMode,
      targets: [
        target("text_memory", "Text memory", "Preserve durable findings across sessions."),
        target("skill_bundle", "Skills", "Build reusable research workflows."),
        target("agent_system", "Agent guidance", "Improve instructions for future sessions."),
      ],
    });
  }

  private makeValidation(project: ProjectV1, capabilities: ProjectCapabilitiesV1): ProjectValidationV1 {
    return projectValidationV1Schema.parse({
      schema_version: "1",
      project_id: project.project_id,
      project_etag: project.etag,
      capability_registry_digest: capabilities.registry_digest,
      valid: true,
      issues: [],
      validated_at: NOW,
    });
  }

  private makeState(connection: "offline" | "online"): DesktopStateV1 {
    return desktopStateV1Schema.parse({
      ...structuredClone(CONTRACT_FIXTURE_V1.state),
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
    const state: ServiceV1["state"] = online ? (degraded ? "degraded" : "healthy") : "unavailable";
    return [
      { service_id: "service-runtime-fixture", display_name: "OpenEvo runtime", kind: "core" as const },
      { service_id: "service-model-fixture", display_name: "Model service", kind: "model" as const },
      { service_id: "service-artifacts-fixture", display_name: "Evolution storage", kind: "artifact_store" as const },
    ].map((service) => serviceV1Schema.parse({
      schema_version: "1",
      ...service,
      state,
      health_summary: online ? (degraded ? "Needs attention." : "Ready.") : "Available after connection.",
      restart_supported: service.kind !== "artifact_store",
      observed_at: NOW,
      etag: ETAG_D,
    }));
  }

  private makeDiagnostic(degraded: boolean): DiagnosticReportV1 {
    return diagnosticReportV1Schema.parse({
      schema_version: "1",
      diagnostic_id: "diagnostic-fixture-1",
      status: degraded ? "degraded" : "healthy",
      generated_at: NOW,
      checks: [
        { check_id: "connection", label: "Remote connection", status: "passed", summary: "Secure connection is ready.", repair_action: "none" },
        { check_id: "environment", label: "Research environment", status: degraded ? "warning" : "passed", summary: degraded ? "A service should be restarted." : "Required components are available.", repair_action: degraded ? "openevo_can_retry" : "none" },
        { check_id: "model", label: "Model service", status: degraded ? "warning" : "passed", summary: degraded ? "Model service response is delayed." : "Model service is ready.", repair_action: degraded ? "openevo_can_retry" : "none" },
      ],
      findings: degraded
        ? [{ finding_id: "model-latency", severity: "warning", category: "model_service", summary: "Model service response is delayed.", next_action: "Restart the model service." }]
        : [],
      etag: ETAG_D,
    });
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
  ): DesktopApiError {
    return new DesktopApiError(apiErrorV1Schema.parse({
      schema_version: "1",
      request_id: `request-fixture-${httpStatus}`,
      code,
      http_status: httpStatus,
      message,
      severity: "blocking",
      category,
      retryable: true,
      repair_action: "openevo_can_retry",
      next_action: httpStatus === 412 ? "Review the refreshed project and save again." : "Reload the current snapshot before retrying.",
      details: {},
      logs_ref: null,
    }));
  }

  private revision(generation: number, state: "active" | "queued" | "preparing" | "cancelled") {
    return {
      revision_id: `revision-fixture-${generation}`,
      generation,
      manifest_digest: generation % 2 === 0 ? C : A,
      state,
    } as const;
  }

  private currentGeneration(project: ProjectV1): number {
    const activeSuccessor = this.runs.find((run) => run.successor_revision?.revision_id === project.current_revision_id)?.successor_revision;
    return activeSuccessor?.generation ?? 1;
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
    const run = this.runs.find((item) => item.run_id === runId);
    if (!run) throw new Error("Session was not found.");
    return run;
  }

  private updateProfileConnection(profileId: string, connectionState: RemoteProfileV1["connection_state"]): void {
    this.profiles = this.profiles.map((profile) =>
      profile.profile_id === profileId ? remoteProfileV1Schema.parse({ ...profile, connection_state: connectionState, updated_at: NOW }) : profile,
    );
  }

  private replaceRun(run: RunV1): void {
    this.runs = this.runs.map((item) => (item.run_id === run.run_id ? run : item));
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
      targets: capabilities.targets.map((target) => target.target_id === "text_memory"
        ? {
            ...target,
            methods: target.methods.map((method) => method.method_id === "reference_text_memory"
              ? { ...method, config_schema: configSchema, default_config: defaultConfig }
              : method),
          }
        : target),
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

  useNullEffectiveDefault(): void {
    const project = this.projects[0];
    const capabilities = this.capabilities;
    if (!project || !capabilities) return;
    this.capabilities = projectCapabilitiesV1Schema.parse({
      ...capabilities,
      targets: capabilities.targets.map((target) => target.target_id === "text_memory"
        ? { ...target, effective_default_method_id: null }
        : target),
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

  useRunStateReviewScenario(): void {
    const project = this.projects[0];
    if (!project) return;
    const pinned = this.revision(2, "active");
    const base = structuredClone(CONTRACT_FIXTURE_V1.run);
    const makeRun = (
      id: string,
      state: RunV1["state"],
      updatedAt: string,
      options: { queued?: boolean; failed?: boolean } = {},
    ) => runV1Schema.parse({
      ...base,
      run_id: id,
      project_id: project.project_id,
      state,
      queued_reason: options.queued ? {
        code: "service_starting",
        summary: "The selected model is being prepared.",
        retry_after_seconds: 5,
      } : null,
      pinned_revision: pinned,
      successor_revision: null,
      latest_attempt: {
        ...base.latest_attempt,
        attempt_id: `attempt-${id}`,
        state,
        started_at: state === "queued" ? null : updatedAt,
        finished_at: isTerminal(state) ? updatedAt : null,
      },
      created_at: "2026-07-14T10:00:00Z",
      updated_at: updatedAt,
      error: options.failed ? this.apiError(409, "model_load_failed", "The model worker could not load the selected model.", "run").apiError : null,
    });
    this.runs = [
      makeRun("run-succeeded", "succeeded", "2026-07-14T10:10:00Z"),
      makeRun("run-queued-model", "queued", "2026-07-14T10:40:00Z", { queued: true }),
      makeRun("run-cancelled", "cancelled", "2026-07-14T10:20:00Z"),
      makeRun("run-failed-model", "failed", "2026-07-14T10:30:00Z", { failed: true }),
    ];
    this.timelines["run-queued-model"] = [this.timeline(1, "admission", "queued", "Model preparation", "The selected model is being prepared.")];
    this.services = this.services.map((service) => service.kind === "model"
      ? serviceV1Schema.parse({ ...service, state: "starting", health_summary: "Preparing the selected model." })
      : service);
  }

  useAuthoritativeArtifactOrderingScenario(): void {
    const project = this.projects[0];
    if (!project) return;
    const runId = "run-revision-4";
    const base = structuredClone(CONTRACT_FIXTURE_V1.run);
    const run = runV1Schema.parse({
      ...base,
      run_id: runId,
      project_id: project.project_id,
      state: "succeeded",
      queued_reason: null,
      pinned_revision: this.revision(2, "active"),
      successor_revision: this.revision(4, "active"),
      latest_attempt: { ...base.latest_attempt, attempt_id: "attempt-revision-4", state: "succeeded", finished_at: NOW },
      error: null,
    });
    this.runs = [run, ...this.runs];
    this.projects = [{ ...project, current_revision_id: "revision-fixture-4" }, ...this.projects.slice(1)];
    this.createArtifacts(runId, 4);
    const times: Record<string, string> = {
      skill_bundle: "2026-07-14T12:03:00Z",
      text_memory: "2026-07-14T12:02:00Z",
      agent_system: "2026-07-14T12:01:00Z",
    };
    this.artifacts = this.artifacts.map((artifact) => artifact.revision_ids.includes("revision-fixture-4")
      ? artifactV1Schema.parse({ ...artifact, created_at: times[artifact.target_id] ?? NOW })
      : artifact);
    const source = this.artifacts.find((artifact) => artifact.target_id === "text_memory" && artifact.revision_ids.includes("revision-fixture-4"));
    if (source) {
      this.artifacts = [artifactV1Schema.parse({
        ...source,
        artifact_id: "artifact-unselected-newer",
        display_name: "Unselected newer artifact",
        selected: false,
        created_at: "2026-07-14T12:04:00Z",
      }), ...this.artifacts];
    }
  }

  makeRevisionEvidenceUnknown(): void {
    const project = this.projects[0];
    if (!project) return;
    this.projects = [{ ...project, current_revision_id: "revision-unknown" }, ...this.projects.slice(1)];
    this.emit();
  }

  failNextProjectSaveWithStatus(status: 412): void {
    this.nextProjectSaveStatus = status;
  }

  failNextRunStartWithStatus(status: 409 | 410): void {
    this.nextRunStartStatus = status;
  }

  refreshCount(): number {
    return this.refreshAttempts;
  }

  projectUpdateAttempts(): number {
    return this.projectSaveAttempts;
  }

  runStartAttempts(): number {
    return this.runAdmissionAttempts;
  }

  addDraftProject(): ProjectV1 {
    const base = this.makeProjectFixture();
    const project = projectV1Schema.parse({
      ...base,
      project_id: "project-fixture-2",
      name: "Second research project",
      task: { ...base.task, title: "Second research task" },
      state: "draft",
      current_revision_id: null,
      etag: ETAG_A,
    });
    this.projects = [...this.projects, project];
    this.emit();
    return structuredClone(project);
  }

  useUnsupportedSavedMethod(): void {
    const project = this.projects[0];
    if (!project) return;
    const updated = projectV1Schema.parse({
      ...project,
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
    const target = capabilities?.targets.find((item) => item.target_id === "text_memory");
    const accepted = target?.accepted_methods[0];
    if (!project || !capabilities || !target || !accepted) return;
    this.capabilities = projectCapabilitiesV1Schema.parse({
      ...capabilities,
      targets: capabilities.targets.map((item) => item.target_id === target.target_id
        ? { ...item, accepted_methods: [...item.accepted_methods, { ...accepted, method_id: "hidden_text_memory" }] }
        : item),
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
    const target = capabilities?.targets.find((item) => item.target_id === "text_memory");
    const resolved = target?.accepted_methods[0];
    if (!project || !capabilities || !target || !resolved) return;
    this.capabilities = projectCapabilitiesV1Schema.parse({
      ...capabilities,
      targets: capabilities.targets.map((item) => item.target_id === target.target_id
        ? { ...item, selection_resolvers: [{ selection_value: "auto", display_name: "Automatic", description: "Core selects from accepted methods.", resolved_methods: [resolved] }] }
        : item),
    });
    const updated = projectV1Schema.parse({
      ...project,
      evolution: { targets: { ...project.evolution.targets, text_memory: { enabled: true, method: "auto", config: {} } } },
    });
    this.projects = [updated, ...this.projects.slice(1)];
    this.validation = this.makeValidation(updated, this.capabilities);
    this.emit();
  }

  clearEvolutionSelections(): void {
    const project = this.projects[0];
    if (!project) return;
    const updated = projectV1Schema.parse({ ...project, evolution: { targets: {} } });
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

function isTerminal(state: RunV1["state"]): boolean {
  return state === "succeeded" || state === "failed" || state === "cancelled";
}

function credentialKindsForAuth(
  authenticationKind: RemoteProfileV1["authentication_kind"],
): RemoteProfileV1["credential_slots"][number]["kind"][] {
  if (authenticationKind === "native_password") return ["ssh_password"];
  if (authenticationKind === "native_private_key") return ["ssh_private_key", "ssh_private_key_passphrase"];
  return [];
}

function credentialKindsForProfile(
  authenticationKind: RemoteProfileV1["authentication_kind"],
  proxy: { http_url?: string | null; https_url?: string | null } | undefined,
): RemoteProfileV1["credential_slots"][number]["kind"][] {
  return [
    ...credentialKindsForAuth(authenticationKind),
    ...(proxy?.http_url ? ["http_proxy_password" as const] : []),
    ...(proxy?.https_url ? ["https_proxy_password" as const] : []),
  ];
}
