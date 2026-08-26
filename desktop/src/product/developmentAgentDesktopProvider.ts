import {
  evolutionCapabilitiesSha256ForV2,
  scienceProjectConfigSha256ForV2,
  taskAdmissionSha256ForV2,
  type EvolutionCapabilitiesV2,
  type ProjectHeadRefV2,
  type ScienceProjectConfigV2,
  type TaskV2,
} from "../api/v2/schemas";
import {
  unavailableDesktopProductProviderV2,
  type DesktopProductProviderV2,
  type DesktopProductSnapshotV2,
  type ProductSubscriptionSignalV2,
} from "./providerV2";

const NOW = "2026-08-13T08:30:00Z";
const DIGEST = "a".repeat(64);
const ETAG = `"${"b".repeat(64)}"`;

export interface DevelopmentAgentTurnRequest {
  readonly projectId: string;
  readonly projectHeadId?: string;
  readonly projectName: string;
  readonly taskTitle: string;
  readonly instruction: string;
}

export interface DevelopmentAgentTurnSubmission {
  readonly sessionId: string;
  readonly state: "running";
}

export interface PersistedDevelopmentWorkspaceEntry {
  readonly path: string;
  readonly kind: "file" | "directory" | "symlink" | "unreadable";
  readonly byteSize: number;
  readonly contentSha256: string | null;
  readonly mediaType: string | null;
  readonly content: string | null;
  readonly modifiedAt: string;
}

export interface PersistedDevelopmentWorkspace {
  readonly projectId: string;
  readonly entries: readonly PersistedDevelopmentWorkspaceEntry[];
  readonly truncated: boolean;
}

export interface PersistedDevelopmentWorkspaceChange {
  readonly path: string;
  readonly changeType: "created" | "modified" | "deleted";
  readonly byteSize: number;
  readonly mediaType: string | null;
  readonly content: string | null;
  readonly previousPath: string | null;
  readonly diffLines: readonly { readonly kind: "added" | "removed" | "context"; readonly text: string }[];
}

export interface PersistedDevelopmentEvolutionSelection {
  readonly targetId: string;
  readonly method: string;
  readonly config: Readonly<Record<string, unknown>>;
}

export interface PersistedDevelopmentEvolutionError {
  readonly targetId: string;
  readonly method: string;
  readonly message: string;
}

export interface PersistedDevelopmentArtifact {
  readonly artifactId: string;
  readonly projectId: string;
  readonly sessionId: string;
  readonly runId: string | null;
  readonly targetId: string;
  readonly artifactType: "text_memory" | "skill_bundle" | "agent_system" | "parametric_memory" | "report";
  readonly method: string;
  readonly rendererKind: "markdown" | "file_bundle" | "structured_summary" | "adapter";
  readonly documents: readonly { readonly path: string; readonly mediaType: string; readonly content: string }[];
  readonly manifest: Readonly<Record<string, unknown>>;
  readonly contentPath: string | null;
  readonly content: string | null;
  readonly contentSha256: string;
  readonly byteSize: number;
  readonly previousArtifactId: string | null;
  readonly promoted: boolean;
  readonly createdAt: string;
}

export interface PersistedDevelopmentEvolutionJob {
  readonly jobId: string;
  readonly sessionId: string;
  readonly runId: string | null;
  readonly targetId: string;
  readonly methodId: string;
  readonly requestedMethodId: string;
  readonly resolverInputArtifactIds: readonly string[];
  readonly previousArtifactId: string | null;
  readonly config: Readonly<Record<string, unknown>>;
  readonly state: "queued" | "running" | "completed" | "failed";
  readonly artifactIds: readonly string[];
  readonly error: string | null;
  readonly attempts: readonly PersistedDevelopmentEvolutionAttempt[];
  readonly createdAt: string;
  readonly updatedAt: string;
}

export interface PersistedDevelopmentEvolutionRun {
  readonly runId: string;
  readonly projectId: string;
  readonly sourceSessionIds: readonly string[];
  readonly selections: readonly PersistedDevelopmentEvolutionSelection[];
  readonly state: "running" | "candidate_ready" | "applied" | "failed";
  readonly artifactIds: readonly string[];
  readonly error: string | null;
  readonly createdAt: string;
  readonly updatedAt: string;
}

export interface PersistedDevelopmentEvolutionAttempt {
  readonly attemptId: string;
  readonly jobId: string;
  readonly ordinal: number;
  readonly state: "queued" | "running" | "completed" | "failed" | "cancelled";
  readonly stage: string;
  readonly artifactIds: readonly string[];
  readonly errorCode: string | null;
  readonly errorMessage: string | null;
  readonly logs: readonly string[];
  readonly createdAt: string;
  readonly startedAt: string | null;
  readonly completedAt: string | null;
  readonly updatedAt: string;
}

export interface PersistedDevelopmentProject {
  readonly projectId: string;
  readonly displayName: string;
  readonly config: ScienceProjectConfigV2;
  readonly createdAt: string;
  readonly updatedAt: string;
}

export interface PersistedDevelopmentSession {
  readonly sessionId: string;
  readonly projectId: string;
  readonly projectHeadId?: string | null;
  readonly taskTitle: string;
  readonly instruction: string;
  readonly response: string | null;
  readonly model: string | null;
  readonly state: "running" | "cancelling" | "completed" | "failed" | "cancelled";
  readonly durationMs: number | null;
  readonly logMessages: readonly string[];
  readonly selectedEvolution: readonly PersistedDevelopmentEvolutionSelection[];
  readonly evolutionErrors: readonly PersistedDevelopmentEvolutionError[];
  readonly evolutionEvidenceReady: boolean;
  readonly workspaceChanges: readonly PersistedDevelopmentWorkspaceChange[];
  readonly contextArtifactIds: readonly string[];
  readonly error: string | null;
  readonly createdAt: string;
  readonly updatedAt: string;
}

export interface PersistedDevelopmentState {
  readonly activeProjectId: string | null;
  readonly projects: readonly PersistedDevelopmentProject[];
  readonly sessions: readonly PersistedDevelopmentSession[];
  readonly artifacts: readonly PersistedDevelopmentArtifact[];
  readonly evolutionJobs: readonly PersistedDevelopmentEvolutionJob[];
  readonly evolutionRuns: readonly PersistedDevelopmentEvolutionRun[];
  readonly workspaces: readonly PersistedDevelopmentWorkspace[];
  readonly capabilities: EvolutionCapabilitiesV2;
}

export interface DevelopmentAgentBackend {
  loadState(): Promise<PersistedDevelopmentState>;
  createProject(project: {
    readonly projectId: string;
    readonly displayName: string;
    readonly config: ScienceProjectConfigV2;
  }): Promise<void>;
  updateProject(project: {
    readonly projectId: string;
    readonly displayName: string;
    readonly config: ScienceProjectConfigV2;
  }): Promise<void>;
  activateProject(projectId: string): Promise<void>;
  submitAgentTurn(request: DevelopmentAgentTurnRequest): Promise<DevelopmentAgentTurnSubmission>;
  cancelAgentTurn(sessionId: string): Promise<void>;
  retryEvolutionJob(jobId: string): Promise<void>;
  startEvolutionRun(
    projectId: string,
    sourceSessionIds: readonly string[],
    selections: readonly PersistedDevelopmentEvolutionSelection[],
  ): Promise<void>;
  applyEvolutionRun(runId: string): Promise<void>;
  uploadWorkspaceFile(
    projectId: string,
    path: string,
    data: Blob,
    mediaType: string,
    overwrite: boolean,
  ): Promise<void>;
  downloadWorkspaceFile(
    projectId: string,
    path: string,
  ): Promise<{ readonly fileName: string; readonly mediaType: string; readonly data: Blob }>;
}

interface DevelopmentProviderOptions {
  readonly developmentBackend: DevelopmentAgentBackend;
}

/**
 * Development-only current-contract scenario for UI work. This provider is imported only by
 * product-preview.html and is never selected by the release bootstrap path.
 */
/**
 * Development-only provider that obtains agent replies and real document-evolution artifacts
 * from the supplied remote runner.
 */
export function createDevelopmentAgentDesktopProductProvider(
  backend: DevelopmentAgentBackend,
): DesktopProductProviderV2 {
  return createRemoteBackedDesktopProductProvider({
    developmentBackend: backend,
  });
}

function createRemoteBackedDesktopProductProvider(
  options: DevelopmentProviderOptions,
): DesktopProductProviderV2 {
  let snapshot = createDevelopmentAgentSnapshot();
  let developmentCapabilities = snapshot.capability?.capabilities;
  const taskLogs = new Map<string, readonly string[]>();
  const subscribers = new Set<(signal: ProductSubscriptionSignalV2) => void>();
  let developmentStateLoaded = false;
  let developmentStateLoad: Promise<void> | null = null;
  let pollingTimer: ReturnType<typeof globalThis.setTimeout> | null = null;

  const notifySubscribers = () => {
    for (const listener of subscribers) listener({ kind: "cursor_reset", resumeFromEventId: null });
  };

  const hasActiveSession = () => snapshot.tasks.some(
    (task) => task.state === "running" || task.state === "cancelling",
  ) || Object.values(snapshot.runtimePresentation?.tasks ?? {}).some(
    (presentation) => presentation.evolutionJobs?.some(
      (job) => job.state === "queued" || job.state === "running",
    ),
  ) || snapshot.runtimePresentation?.evolutionRuns?.some((run) => run.state === "running") === true;

  const schedulePolling = () => {
    if (pollingTimer !== null || !hasActiveSession()) return;
    pollingTimer = globalThis.setTimeout(async () => {
      pollingTimer = null;
      try {
        await ensureDevelopmentState(true);
        notifySubscribers();
      } catch {
        // A transient tunnel failure is surfaced by the normal refresh path; keep the polling
        // loop alive so a restored tunnel can resume the same persisted Session.
      } finally {
        schedulePolling();
      }
    }, 750);
  };

  const ensureDevelopmentState = async (force = false): Promise<void> => {
    if (developmentStateLoaded && !force) return;
    if (developmentStateLoad) {
      await developmentStateLoad;
      return;
    }
    developmentStateLoad ??= options.developmentBackend.loadState().then((state) => {
      developmentCapabilities = state.capabilities;
      snapshot = createDevelopmentAgentSnapshot(state);
      taskLogs.clear();
      for (const session of state.sessions) taskLogs.set(session.sessionId, session.logMessages);
      developmentStateLoaded = true;
    }).finally(() => {
      developmentStateLoad = null;
    });
    await developmentStateLoad;
    schedulePolling();
  };

  return {
    ...unavailableDesktopProductProviderV2,
    featureFlags: ["system_openssh_profiles", "development_agent_bridge"],
    refresh: async () => {
      await ensureDevelopmentState(true);
      return { status: "fresh", snapshot };
    },
    subscribe: (listener) => {
      subscribers.add(listener);
      return () => subscribers.delete(listener);
    },
    createProject: async (draft) => {
      await ensureDevelopmentState();
      const sequence = snapshot.projects.length + 1;
      const projectId = `development-project-${sequence}`;
      const config = draft.config;
      const head = developmentGenesisHead(projectId, config, sequence);
      const project = {
        schema_version: "2" as const,
        project_id: projectId,
        display_name: draft.displayName,
        config,
        project_config_sha256: scienceProjectConfigSha256ForV2(config),
        active_project_head: head,
        admission_etag: ETAG,
        state: "ready" as const,
        created_at: NOW,
        updated_at: NOW,
        etag: ETAG,
      };
      await options.developmentBackend.createProject({
        projectId,
        displayName: project.display_name,
        config: project.config,
      });
      snapshot = {
        ...snapshot,
        projects: [...snapshot.projects, project],
        state: { ...snapshot.state, active_project_id: projectId, updated_at: NOW },
        profiles: snapshot.profiles.map((profile) => ({ ...profile, active_project_id: projectId })) as never,
        capability: developmentAgentCapability(
          projectId,
          config.execution.mode,
          developmentCapabilities,
        ),
        validation: null,
        stream: { ...snapshot.stream, epoch: snapshot.stream.epoch + 1 },
      };
      return {
        schema_version: "2",
        operation_id: `development-project-create-${sequence}`,
        kind: "project_create",
        resource: { resource_kind: "project", resource_id: projectId },
        request_sha256: developmentDigest(sequence + 10),
        status: "queued",
        phase: "validating_input",
        phase_index: 0,
        phase_total: 17,
        progress: null,
        cancellable: true,
        result: null,
        failure: null,
        created_at: NOW,
        updated_at: NOW,
        started_at: null,
        finished_at: null,
      } as never;
    },
    updateProject: async (projectId, displayName, config) => {
      await ensureDevelopmentState();
      const project = snapshot.projects.find((candidate) => candidate.project_id === projectId);
      if (!project) throw new Error("Remote project is missing.");
      const persistedConfig = config;
      const updated = {
        ...project,
        display_name: displayName,
        config: persistedConfig,
        project_config_sha256: scienceProjectConfigSha256ForV2(persistedConfig),
        updated_at: NOW,
      };
      await options.developmentBackend.updateProject({
        projectId,
        displayName,
        config: persistedConfig,
      });
      snapshot = {
        ...snapshot,
        projects: snapshot.projects.map((candidate) => candidate.project_id === projectId ? updated : candidate),
        stream: { ...snapshot.stream, epoch: snapshot.stream.epoch + 1 },
      };
      return updated;
    },
    activateProject: async (projectId) => {
      await ensureDevelopmentState();
      const project = snapshot.projects.find((candidate) => candidate.project_id === projectId);
      if (!project?.active_project_head) throw new Error("Remote project is missing or not activatable.");
      await options.developmentBackend.activateProject(projectId);
      snapshot = {
        ...snapshot,
        state: { ...snapshot.state, active_project_id: projectId, updated_at: NOW },
        profiles: snapshot.profiles.map((profile) => ({ ...profile, active_project_id: projectId })) as never,
        capability: developmentAgentCapability(
          projectId,
          project.config.execution.mode,
          developmentCapabilities,
        ),
        validation: null,
        stream: { ...snapshot.stream, epoch: snapshot.stream.epoch + 1 },
      };
      return {
        schema_version: "2",
        operation_id: `development-project-activate-${projectId}`,
        kind: "project_activate",
        resource: { resource_kind: "project", resource_id: projectId },
        request_sha256: developmentDigest(snapshot.projects.indexOf(project) + 32),
        status: "succeeded",
        phase: "ready",
        phase_index: 1,
        phase_total: 1,
        progress: null,
        cancellable: false,
        result: null,
        failure: null,
        created_at: NOW,
        updated_at: NOW,
        started_at: NOW,
        finished_at: NOW,
      } as never;
    },
    validateProject: async (projectId) => ({
      schema_version: "2",
      project_id: projectId,
      valid: true,
      registry_sha256: DIGEST,
      checks: [],
      validated_at: NOW,
    } as never),
    submitTask: async (projectId, _intent, selectedProjectHead) => {
      await ensureDevelopmentState();
      const project = snapshot.projects.find((candidate) => candidate.project_id === projectId);
      if (!project?.active_project_head || project.state !== "ready") throw new Error("Remote project is not ready.");
      const head = selectedProjectHead ?? project.active_project_head;
      if (head.project_id !== projectId) throw new Error("Selected Project Head belongs to another Project.");
      const submission = await options.developmentBackend.submitAgentTurn({
        projectId,
        projectHeadId: head.project_head_id,
        projectName: project.display_name,
        taskTitle: project.config.task.title,
        instruction: project.config.task.objective,
      });
      await ensureDevelopmentState(true);
      const task = snapshot.tasks.find((candidate) => candidate.task_id === submission.sessionId);
      if (!task) throw new Error("The admitted remote Session was not visible after submission.");
      notifySubscribers();
      schedulePolling();
      return task;
    },
    cancelTask: async (taskId) => {
      await options.developmentBackend.cancelAgentTurn(taskId);
      await ensureDevelopmentState(true);
      notifySubscribers();
      schedulePolling();
      return {
        schema_version: "2",
        operation_id: `development-session-cancel-${taskId}`,
        kind: "task_cancel",
        resource: { resource_kind: "task", resource_id: taskId },
        request_sha256: developmentDigest(91),
        status: "queued",
        phase: "cancelling",
        phase_index: 0,
        phase_total: 1,
        progress: null,
        cancellable: false,
        result: null,
        failure: null,
        created_at: NOW,
        updated_at: NOW,
        started_at: NOW,
        finished_at: null,
      } as never;
    },
    retryEvolutionJob: async (jobId) => {
      await ensureDevelopmentState();
      await options.developmentBackend.retryEvolutionJob(jobId);
      await ensureDevelopmentState(true);
      notifySubscribers();
      schedulePolling();
    },
    startEvolutionRun: async (projectId, sourceTaskIds, selections) => {
      await ensureDevelopmentState();
      await options.developmentBackend.startEvolutionRun(
        projectId,
        sourceTaskIds,
        selections.map((selection) => ({
          targetId: selection.targetId,
          method: selection.method,
          config: selection.config,
        })),
      );
      await ensureDevelopmentState(true);
      notifySubscribers();
      schedulePolling();
    },
    applyEvolutionRun: async (runId) => {
      await ensureDevelopmentState();
      await options.developmentBackend.applyEvolutionRun(runId);
      await ensureDevelopmentState(true);
      notifySubscribers();
    },
    uploadWorkspaceFile: async (projectId, upload) => {
      await ensureDevelopmentState();
      await options.developmentBackend.uploadWorkspaceFile(
        projectId,
        upload.path,
        upload.data,
        upload.mediaType,
        upload.overwrite,
      );
      await ensureDevelopmentState(true);
      notifySubscribers();
    },
    downloadWorkspaceFile: async (projectId, path) => (
      options.developmentBackend.downloadWorkspaceFile(projectId, path)
    ),
    loadTaskLogs: async (taskId) => {
      await ensureDevelopmentState(true);
      return {
        schema_version: "2",
        items: (taskLogs.get(taskId) ?? ["No development runner logs were recorded."]).map((message, index) => ({
          sequence: index + 1,
          occurred_at: NOW,
          stream: index === 0 ? "system" : "transcript",
          message,
        })),
        next_cursor: null,
        has_more: false,
      };
    },
    getArtifactContent: async (artifactId) => {
      const artifact = snapshot.artifacts.find((candidate) => candidate.artifact_id === artifactId);
      if (!artifact) throw new Error("Remote artifact is missing.");
      return {
        schema_version: "2",
        artifact,
        media_type: "text/markdown",
        content_sha256: artifact.manifest_sha256,
        byte_size: artifact.byte_size,
      };
    },
    getArtifactDiff: async (artifactId) => {
      const artifact = snapshot.artifacts.find((candidate) => candidate.artifact_id === artifactId);
      if (!artifact) throw new Error("Remote artifact is missing.");
      const previousArtifactId = snapshot.runtimePresentation?.artifacts[artifactId]?.previousArtifactId ?? null;
      const previous = snapshot.artifacts.find((candidate) => candidate.artifact_id === previousArtifactId);
      return {
        schema_version: "2",
        artifact_id: artifactId,
        previous_artifact_id: previous?.artifact_id ?? null,
        current_manifest_sha256: artifact.manifest_sha256,
        previous_manifest_sha256: previous?.manifest_sha256 ?? null,
        status: previous ? "available" : "unavailable",
      };
    },
  };
}

function createDevelopmentAgentSnapshot(
  persisted: PersistedDevelopmentState = {
    activeProjectId: null,
    projects: [],
    sessions: [],
    artifacts: [],
    evolutionJobs: [],
    evolutionRuns: [],
    workspaces: [],
    capabilities: {
      schema_version: "1",
      core_version: "unavailable",
      registry_digest: DIGEST,
      evaluated_profile: {
        execution_mode: "subscription",
        capture_mode: "transcript",
        harness_id: "codex",
        harness_capabilities: [],
        runtime_capabilities: [],
      },
      targets: [],
    },
  },
): DesktopProductSnapshotV2 {
  const persistedProjectIds = new Set(persisted.projects.map((project) => project.projectId));
  const activeProjectId = persisted.activeProjectId && persistedProjectIds.has(persisted.activeProjectId)
    ? persisted.activeProjectId
    : persisted.projects.at(-1)?.projectId ?? null;
  const profile = {
    schema_version: "2" as const,
    profile_kind: "system_openssh" as const,
    profile_id: "development-agent-profile",
    display_name: "GPU lab (development tunnel)",
    connection_authority: "system_openssh" as const,
    ssh_host_alias: "openevo-lab",
    catalog_generation: 1,
    connection_generation: 1,
    connection_state: "connected" as const,
    prompt: null,
    trust: {
      schema_version: "2" as const,
      connection_generation: 1,
      state: "trusted" as const,
      review_id: null,
      review_sha256: null,
      key_fingerprints: [],
      repair_support: "not_needed" as const,
    },
    failure: null,
    active_project_id: activeProjectId,
    core_api_major: 2,
    core_openapi_sha256: DIGEST,
    core_event_schema_sha256: DIGEST,
    core_registry_sha256: persisted.capabilities.registry_digest,
    created_at: NOW,
    updated_at: NOW,
    etag: ETAG,
  };
  const tasks: TaskV2[] = [];
  const taskPresentation: Record<
    string,
    NonNullable<DesktopProductSnapshotV2["runtimePresentation"]>["tasks"][string]
  > = {};
  const projects = persisted.projects.map((storedProject, projectIndex) => {
    const config = storedProject.config;
    let activeHead = developmentGenesisHead(storedProject.projectId, config, projectIndex + 1);
    const projectHeads = new Map([[activeHead.project_head_id, activeHead]]);
    const projectSessions = persisted.sessions.filter((session) => session.projectId === storedProject.projectId);
    for (const [sessionIndex, session] of projectSessions.entries()) {
      const admissionHead = session.projectHeadId == null
        ? activeHead
        : projectHeads.get(session.projectHeadId) ?? activeHead;
      const sessionConfig = {
        ...config,
        task: { title: session.taskTitle, objective: session.instruction },
      };
      const taskProject = {
        schema_version: "2" as const,
        project_id: storedProject.projectId,
        display_name: storedProject.displayName,
        config: sessionConfig,
        project_config_sha256: scienceProjectConfigSha256ForV2(sessionConfig),
        active_project_head: admissionHead,
        admission_etag: ETAG,
        state: "ready" as const,
        created_at: storedProject.createdAt,
        updated_at: storedProject.updatedAt,
        etag: ETAG,
      };
      const hydratedTask = developmentTask(taskProject, sessionIndex + 1, session.sessionId);
      tasks.unshift({
        ...hydratedTask,
        successor_transition: null,
        state: session.state === "completed" ? "closed" : session.state,
        created_at: session.createdAt,
        updated_at: session.updatedAt,
      });
      const produced = persisted.artifacts.filter((artifact) => (
        artifact.sessionId === session.sessionId && artifact.runId === null
      ));
      taskPresentation[session.sessionId] = {
        instruction: sessionConfig.task,
        transcript: [
          { speaker: "user", text: session.instruction },
          ...(session.response ? [{ speaker: "agent" as const, text: session.response }] : []),
          ...(session.error ? [{ speaker: "system" as const, text: session.error }] : []),
        ],
        outputFiles: workspaceChangeOutputFiles(session.workspaceChanges),
        selectedEvolution: session.selectedEvolution,
        evolutionErrors: session.evolutionErrors,
        evolutionEvidenceReady: session.evolutionEvidenceReady,
        evolutionJobs: persisted.evolutionJobs
          .filter((job) => job.sessionId === session.sessionId),
        usedArtifactIds: [...new Set([
          ...session.contextArtifactIds,
          ...produced.flatMap((artifact) => artifact.previousArtifactId ? [artifact.previousArtifactId] : []),
        ])],
        producedArtifactIds: produced.map((artifact) => artifact.artifactId),
      };
      if (session.state === "completed") {
        activeHead = developmentSuccessorHead(activeHead, config, produced.length);
        projectHeads.set(activeHead.project_head_id, activeHead);
      }
    }
    return {
      schema_version: "2" as const,
      project_id: storedProject.projectId,
      display_name: storedProject.displayName,
      config,
      project_config_sha256: scienceProjectConfigSha256ForV2(config),
      active_project_head: activeHead,
      admission_etag: ETAG,
      state: "ready" as const,
      created_at: storedProject.createdAt,
      updated_at: storedProject.updatedAt,
      etag: ETAG,
    };
  });
  const activeProject = projects.find((project) => project.project_id === activeProjectId) ?? null;
  const artifacts = persisted.artifacts.map((stored) => ({
    schema_version: "2" as const,
    artifact_id: stored.artifactId,
    project_id: stored.projectId,
    artifact_type: stored.artifactType,
    manifest_sha256: stored.contentSha256,
    byte_size: stored.byteSize,
    created_at: stored.createdAt,
  }));
  const artifactPresentation = Object.fromEntries(persisted.artifacts.map((stored) => {
    const previous = persisted.artifacts.find((candidate) => candidate.artifactId === stored.previousArtifactId);
    const presentation = developmentArtifactPresentation(stored, previous?.documents ?? null);
    const runApplied = stored.runId === null
      ? stored.promoted
      : persisted.evolutionRuns.some((run) => run.runId === stored.runId && run.state === "applied");
    return [stored.artifactId, {
      ...presentation,
      statusDetail: stored.runId === null
        ? presentation.statusDetail
        : `${runApplied ? "Applied" : "Candidate · not applied"}. ${presentation.statusDetail}`,
      evolutionRunId: stored.runId,
      applied: runApplied,
    }];
  }));
  return {
    state: {
      schema_version: "2",
      profiles: [profile] as never,
      active_profile_id: profile.profile_id,
      active_project_id: activeProjectId,
      pending_operations: [],
      last_event_id: null,
      updated_at: NOW,
    },
    catalog: { schema_version: "2", catalog_generation: 1, hosts: [], warnings: [], scanned_at: NOW },
    profiles: [profile] as never,
    projects,
    tasks,
    transitions: {},
    timelines: {},
    artifacts,
    services: [],
    capability: activeProject
      ? developmentAgentCapability(
          activeProject.project_id,
          activeProject.config.execution.mode,
          persisted.capabilities,
        )
      : null,
    validation: null,
    activeOperation: null,
    runtimePresentation: {
      evolutionRuns: persisted.evolutionRuns.map((run) => ({
        runId: run.runId,
        projectId: run.projectId,
        sourceTaskIds: run.sourceSessionIds,
        selections: run.selections,
        state: run.state,
        artifactIds: run.artifactIds,
        jobIds: persisted.evolutionJobs
          .filter((job) => job.runId === run.runId)
          .map((job) => job.jobId),
        error: run.error,
        createdAt: run.createdAt,
        updatedAt: run.updatedAt,
      })),
      tasks: taskPresentation,
      artifacts: artifactPresentation,
      workspaces: Object.fromEntries(persisted.workspaces.map((workspace) => [
        workspace.projectId,
        {
          entries: workspace.entries,
          truncated: workspace.truncated,
        },
      ])),
    },
    stream: { status: "fresh", epoch: 1, lastEventId: null },
  };
}

function workspaceChangeOutputFiles(
  changes: readonly PersistedDevelopmentWorkspaceChange[],
): NonNullable<DesktopProductSnapshotV2["runtimePresentation"]>["tasks"][string]["outputFiles"] {
  return changes.map((change) => ({
    name: change.path,
    summary: `${change.changeType[0]!.toUpperCase()}${change.changeType.slice(1)} in the remote project workspace · ${formatDevelopmentBytes(change.byteSize)}`,
    ...(change.content !== null ? { content: change.content } : {}),
    previousName: change.previousPath,
    diffLines: change.diffLines,
  }));
}

function formatDevelopmentBytes(bytes: number): string {
  if (bytes < 1_024) return `${bytes} B`;
  if (bytes < 1_048_576) return `${(bytes / 1_024).toFixed(1)} KB`;
  return `${(bytes / 1_048_576).toFixed(1)} MB`;
}

function developmentDigest(seed: number): string {
  return (seed % 16).toString(16).repeat(64);
}

function developmentGenesisHead(
  projectId: string,
  config: ScienceProjectConfigV2,
  seed: number,
): ProjectHeadRefV2 {
  const evolutionRevision = {
    schema_version: "2" as const,
    evolution_revision_id: `${projectId}-evolution-0`,
    project_id: projectId,
    manifest_sha256: developmentDigest(seed + 1),
    artifact_count: 0,
  };
  return {
    schema_version: "2",
    project_head_id: `${projectId}-head-0`,
    project_id: projectId,
    generation: 0,
    predecessor_project_head_id: null,
    workspace_snapshot: {
      schema_version: "2",
      workspace_snapshot_id: `${projectId}-workspace-0`,
      project_id: projectId,
      manifest_sha256: developmentDigest(seed + 2),
      entry_count: 0,
      byte_size: 0,
    },
    evolution_revision: evolutionRevision,
    runtime_context_snapshot: {
      schema_version: "2",
      runtime_context_snapshot_id: `${projectId}-context-0`,
      project_id: projectId,
      evolution_revision_id: evolutionRevision.evolution_revision_id,
      evolution_revision_manifest_sha256: evolutionRevision.manifest_sha256,
      registry_sha256: DIGEST,
      runtime_contract_sha256: developmentDigest(seed + 3),
      manifest_sha256: developmentDigest(seed + 4),
    },
    effective_execution_snapshot: {
      schema_version: "2",
      effective_execution_snapshot_id: `${projectId}-execution-0`,
      project_id: projectId,
      execution_mode: config.execution.mode,
      capture_mode: config.execution.capture_mode,
      token_level_metrics_available: config.execution.token_level_metrics_available,
      producer_id: "development-daemon",
      snapshot_sha256: developmentDigest(seed + 5),
    },
    registry_sha256: DIGEST,
    manifest_sha256: developmentDigest(seed + 6),
  };
}

function developmentAgentCapability(
  projectId: string,
  executionMode: ScienceProjectConfigV2["execution"]["mode"],
  capabilities?: EvolutionCapabilitiesV2,
): DesktopProductSnapshotV2["capability"] {
  if (!capabilities) return null;
  return {
    schema_version: "2",
    project_id: projectId,
    execution_mode: executionMode,
    registry_sha256: capabilities.registry_digest,
    capabilities_sha256: evolutionCapabilitiesSha256ForV2(capabilities),
    capabilities,
    fetched_at: new Date().toISOString(),
  };
}

function developmentEvolutionSelections(
  config: ScienceProjectConfigV2,
): readonly PersistedDevelopmentEvolutionSelection[] {
  return Object.entries(config.evolution.targets).flatMap(([targetId, selection]) => (
    selection.enabled && selection.method
      ? [{ targetId, method: selection.method, config: selection.config }]
      : []
  ));
}

function developmentEvolutionRound(
  snapshot: DesktopProductSnapshotV2,
  artifacts: readonly PersistedDevelopmentArtifact[],
) {
  if (artifacts.length === 0) return { artifacts: [], presentation: {}, usedArtifactIds: [] };
  return {
    artifacts: artifacts.map((artifact) => ({
      schema_version: "2" as const,
      artifact_id: artifact.artifactId,
      project_id: artifact.projectId,
      artifact_type: artifact.artifactType,
      manifest_sha256: artifact.contentSha256,
      byte_size: artifact.byteSize,
      created_at: artifact.createdAt,
    })),
    presentation: Object.fromEntries(artifacts.map((artifact) => {
      const previousDocuments = artifact.previousArtifactId
        ? snapshot.runtimePresentation?.artifacts[artifact.previousArtifactId]?.documents.map((document) => ({
            path: document.path,
            mediaType: "text/markdown",
            content: document.content,
          })) ?? null
        : null;
      return [artifact.artifactId, developmentArtifactPresentation(artifact, previousDocuments)];
    })),
    usedArtifactIds: artifacts.flatMap((artifact) => artifact.previousArtifactId ? [artifact.previousArtifactId] : []),
  };
}

function developmentArtifactPresentation(
  artifact: PersistedDevelopmentArtifact,
  previousDocuments: PersistedDevelopmentArtifact["documents"] | null,
) {
  const title = `Evolved ${artifact.targetId.replaceAll("_", " ")}`;
  const previousContent = previousDocuments?.map((document) => document.content).join("\n") ?? null;
  const currentContent = artifact.documents.map((document) => document.content).join("\n");
  return {
    title,
    sourceTaskId: artifact.sessionId,
    targetPath: artifact.contentPath,
    status: previousContent ? "updated" as const : "created" as const,
    statusDetail: previousContent
      ? `The real ${artifact.method} updated this ${artifact.rendererKind} artifact from the Session transcript.`
      : `The real ${artifact.method} created this ${artifact.rendererKind} artifact from the Session transcript.`,
    documents: artifact.documents.map((document) => ({ path: document.path, content: document.content })),
    previousArtifactId: artifact.previousArtifactId,
    diffLines: previousContent
      ? [
          ...previousContent.trimEnd().split("\n").map((text) => ({ kind: "removed" as const, text })),
          ...currentContent.trimEnd().split("\n").map((text) => ({ kind: "added" as const, text })),
        ]
      : currentContent.trimEnd().split("\n").map((text) => ({ kind: "added" as const, text })),
  };
}

function developmentSuccessorHead(
  predecessor: ProjectHeadRefV2,
  config: ScienceProjectConfigV2,
  artifactCount: number,
): ProjectHeadRefV2 {
  const generation = predecessor.generation + 1;
  const projectId = predecessor.project_id;
  const seed = generation * 11;
  const evolutionRevision = {
    schema_version: "2" as const,
    evolution_revision_id: `${projectId}-evolution-${generation}`,
    project_id: projectId,
    manifest_sha256: developmentDigest(seed + 1),
    artifact_count: artifactCount,
  };
  return {
    schema_version: "2",
    project_head_id: `${projectId}-head-${generation}`,
    project_id: projectId,
    generation,
    predecessor_project_head_id: predecessor.project_head_id,
    workspace_snapshot: {
      schema_version: "2",
      workspace_snapshot_id: `${projectId}-workspace-${generation}`,
      project_id: projectId,
      manifest_sha256: developmentDigest(seed + 2),
      entry_count: predecessor.workspace_snapshot.entry_count + 1,
      byte_size: predecessor.workspace_snapshot.byte_size + 512,
    },
    evolution_revision: evolutionRevision,
    runtime_context_snapshot: {
      schema_version: "2",
      runtime_context_snapshot_id: `${projectId}-context-${generation}`,
      project_id: projectId,
      evolution_revision_id: evolutionRevision.evolution_revision_id,
      evolution_revision_manifest_sha256: evolutionRevision.manifest_sha256,
      registry_sha256: DIGEST,
      runtime_contract_sha256: developmentDigest(seed + 3),
      manifest_sha256: developmentDigest(seed + 4),
    },
    effective_execution_snapshot: {
      schema_version: "2",
      effective_execution_snapshot_id: `${projectId}-execution-${generation}`,
      project_id: projectId,
      execution_mode: config.execution.mode,
      capture_mode: config.execution.capture_mode,
      token_level_metrics_available: config.execution.token_level_metrics_available,
      producer_id: "development-daemon",
      snapshot_sha256: developmentDigest(seed + 5),
    },
    registry_sha256: DIGEST,
    manifest_sha256: developmentDigest(seed + 6),
  };
}

function developmentTask(
  project: DesktopProductSnapshotV2["projects"][number],
  ordinal: number,
  persistedTaskId?: string,
): TaskV2 {
  const head = project.active_project_head!;
  const taskId = persistedTaskId ?? `${project.project_id}-task-${ordinal}`;
  const admissionWithoutDigest = {
    schema_version: "2" as const,
    task_admission_id: `${project.project_id}-admission-${ordinal}`,
    task_id: taskId,
    project_id: project.project_id,
    predecessor_project_head: head,
    workspace_snapshot: head.workspace_snapshot,
    project_config_sha256: project.project_config_sha256,
    task_envelope_sha256: developmentDigest(ordinal + 7),
    normalized_evolution_intent_sha256: developmentDigest(ordinal + 8),
    registry_sha256: head.registry_sha256,
    admitted_at: NOW,
  };
  const admission = {
    ...admissionWithoutDigest,
    admission_sha256: taskAdmissionSha256ForV2({ ...admissionWithoutDigest, admission_sha256: "" }),
  };
  const attempt = {
    schema_version: "2" as const,
    attempt_id: `${project.project_id}-attempt-${ordinal}`,
    ordinal: 1,
    task_id: taskId,
    task_admission_id: admission.task_admission_id,
    admission_sha256: admission.admission_sha256,
    project_id: project.project_id,
    predecessor_project_head_id: head.project_head_id,
    created_at: NOW,
  };
  return {
    schema_version: "2",
    task_id: taskId,
    project_id: project.project_id,
    admission,
    attempts: [attempt],
    authoritative_attempt_id: attempt.attempt_id,
    successor_transition: null,
    state: "completed",
    created_at: NOW,
    updated_at: NOW,
    etag: ETAG,
  };
}
