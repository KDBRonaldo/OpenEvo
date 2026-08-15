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
} from "./providerV2";

const NOW = "2026-08-13T08:30:00Z";
const DIGEST = "a".repeat(64);
const ETAG = `"${"b".repeat(64)}"`;

export interface DevelopmentAgentTurnRequest {
  readonly projectId: string;
  readonly projectName: string;
  readonly taskTitle: string;
  readonly instruction: string;
}

export interface DevelopmentAgentTurnResult {
  readonly sessionId: string;
  readonly responseText: string;
  readonly model: string | null;
  readonly durationMs: number;
  readonly logMessages: readonly string[];
  readonly evolutionArtifacts: readonly PersistedDevelopmentArtifact[];
  readonly evolutionErrors: readonly PersistedDevelopmentEvolutionError[];
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
  readonly createdAt: string;
}

export interface PersistedDevelopmentEvolutionJob {
  readonly jobId: string;
  readonly sessionId: string;
  readonly targetId: string;
  readonly methodId: string;
  readonly config: Readonly<Record<string, unknown>>;
  readonly state: "queued" | "running" | "completed" | "failed";
  readonly artifactIds: readonly string[];
  readonly error: string | null;
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
  readonly taskTitle: string;
  readonly instruction: string;
  readonly response: string | null;
  readonly model: string | null;
  readonly state: "running" | "completed" | "failed";
  readonly durationMs: number | null;
  readonly logMessages: readonly string[];
  readonly selectedEvolution: readonly PersistedDevelopmentEvolutionSelection[];
  readonly evolutionErrors: readonly PersistedDevelopmentEvolutionError[];
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
  runAgentTurn(request: DevelopmentAgentTurnRequest): Promise<DevelopmentAgentTurnResult>;
}

interface InMemoryProviderOptions {
  readonly initialSnapshot?: DesktopProductSnapshotV2;
  readonly runAgentTurn?: (request: DevelopmentAgentTurnRequest) => Promise<DevelopmentAgentTurnResult>;
  readonly developmentBackend?: DevelopmentAgentBackend;
  readonly simulateEvolution?: boolean;
}

const FIXTURE_EVOLUTION_TARGETS = [
  {
    targetId: "text_memory",
    artifactType: "text_memory",
    methodId: "fixture_text_memory",
    displayName: "Text memory",
    description: "Adds one durable research-memory sentence after every simulated Session.",
    title: "Research memory",
    path: "memory.md",
    targetPath: null,
    heading: "# Research memory",
    sentence: (ordinal: number, taskTitle: string) => `- Session ${ordinal}: Remember the simulated finding from “${taskTitle}”.`,
  },
  {
    targetId: "skill_bundle",
    artifactType: "skill_bundle",
    methodId: "fixture_skill_bundle",
    displayName: "Skill bundle",
    description: "Adds one reusable workflow sentence to SKILL.md after every simulated Session.",
    title: "Research skill",
    path: "SKILL.md",
    targetPath: "skills/research-loop/SKILL.md",
    heading: "# Research skill",
    sentence: (ordinal: number, taskTitle: string) => `- Session ${ordinal}: Reuse the successful workflow from “${taskTitle}”.`,
  },
  {
    targetId: "agent_system",
    artifactType: "agent_system",
    methodId: "fixture_agent_system",
    displayName: "Agent system",
    description: "Adds one operating instruction to AGENTS.md after every simulated Session.",
    title: "Agent guidance",
    path: "AGENTS.md",
    targetPath: "AGENTS.md",
    heading: "# Agent guidance",
    sentence: (ordinal: number, taskTitle: string) => `- Session ${ordinal}: Apply the lesson learned while running “${taskTitle}”.`,
  },
] as const;

/**
 * Development-only current-contract scenario for UI work. This provider is imported only by
 * product-preview.html and is never selected by the release bootstrap path.
 */
export function createFixtureDesktopProductProvider(): DesktopProductProviderV2 {
  return createInMemoryDesktopProductProvider();
}

/**
 * Development-only provider that obtains agent replies and real document-evolution artifacts
 * from the supplied remote runner.
 */
export function createDevelopmentAgentDesktopProductProvider(
  backend: DevelopmentAgentBackend,
): DesktopProductProviderV2 {
  return createInMemoryDesktopProductProvider({
    initialSnapshot: createDevelopmentAgentSnapshot(),
    runAgentTurn: backend.runAgentTurn,
    developmentBackend: backend,
    simulateEvolution: false,
  });
}

function createInMemoryDesktopProductProvider(
  options: InMemoryProviderOptions = {},
): DesktopProductProviderV2 {
  let snapshot = options.initialSnapshot ?? createFixtureSnapshot();
  let developmentCapabilities = snapshot.capability?.capabilities;
  const simulateEvolution = options.simulateEvolution ?? true;
  const taskLogs = new Map<string, readonly string[]>();
  let developmentStateLoaded = options.developmentBackend === undefined;
  let developmentStateLoad: Promise<void> | null = null;

  const ensureDevelopmentState = async (force = false): Promise<void> => {
    if (developmentStateLoaded && !force) return;
    if (developmentStateLoad) {
      await developmentStateLoad;
      return;
    }
    developmentStateLoad ??= options.developmentBackend!.loadState().then((state) => {
      developmentCapabilities = state.capabilities;
      snapshot = createDevelopmentAgentSnapshot(state);
      taskLogs.clear();
      for (const session of state.sessions) taskLogs.set(session.sessionId, session.logMessages);
      developmentStateLoaded = true;
    }).finally(() => {
      developmentStateLoad = null;
    });
    await developmentStateLoad;
  };

  return {
    ...unavailableDesktopProductProviderV2,
    featureFlags: simulateEvolution
      ? ["system_openssh_profiles"]
      : ["system_openssh_profiles", "development_agent_bridge"],
    refresh: async () => {
      await ensureDevelopmentState(options.developmentBackend !== undefined);
      return { status: "fresh", snapshot };
    },
    subscribe: () => () => undefined,
    createProject: async (draft) => {
      await ensureDevelopmentState();
      const sequence = snapshot.projects.length + 1;
      const projectId = `${simulateEvolution ? "fixture" : "development"}-project-${sequence}`;
      const config = simulateEvolution
        ? withFixtureEvolutionDefaults(draft.config)
        : draft.config;
      const head = fixtureGenesisHead(projectId, config, sequence);
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
      await options.developmentBackend?.createProject({
        projectId,
        displayName: project.display_name,
        config: project.config,
      });
      snapshot = {
        ...snapshot,
        projects: [...snapshot.projects, project],
        state: { ...snapshot.state, active_project_id: projectId, updated_at: NOW },
        profiles: snapshot.profiles.map((profile) => ({ ...profile, active_project_id: projectId })) as never,
        capability: simulateEvolution
          ? fixtureCapability(projectId, config.execution.mode)
          : developmentAgentCapability(
              projectId,
              config.execution.mode,
              developmentCapabilities,
            ),
        validation: null,
        stream: { ...snapshot.stream, epoch: snapshot.stream.epoch + 1 },
      };
      return {
        schema_version: "2",
        operation_id: `fixture-project-create-${sequence}`,
        kind: "project_create",
        resource: { resource_kind: "project", resource_id: projectId },
        request_sha256: fixtureDigest(sequence + 10),
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
      if (!project) throw new Error("Fixture project is missing.");
      const persistedConfig = config;
      const updated = {
        ...project,
        display_name: displayName,
        config: persistedConfig,
        project_config_sha256: scienceProjectConfigSha256ForV2(persistedConfig),
        updated_at: NOW,
      };
      await options.developmentBackend?.updateProject({
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
      if (!project?.active_project_head) throw new Error("Fixture project is missing or not activatable.");
      await options.developmentBackend?.activateProject(projectId);
      snapshot = {
        ...snapshot,
        state: { ...snapshot.state, active_project_id: projectId, updated_at: NOW },
        profiles: snapshot.profiles.map((profile) => ({ ...profile, active_project_id: projectId })) as never,
        capability: simulateEvolution
          ? fixtureCapability(projectId, project.config.execution.mode)
          : developmentAgentCapability(
              projectId,
              project.config.execution.mode,
              developmentCapabilities,
            ),
        validation: null,
        stream: { ...snapshot.stream, epoch: snapshot.stream.epoch + 1 },
      };
      return {
        schema_version: "2",
        operation_id: `fixture-project-activate-${projectId}`,
        kind: "project_activate",
        resource: { resource_kind: "project", resource_id: projectId },
        request_sha256: fixtureDigest(snapshot.projects.indexOf(project) + 32),
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
    submitTask: async (projectId) => {
      await ensureDevelopmentState();
      const project = snapshot.projects.find((candidate) => candidate.project_id === projectId);
      if (!project?.active_project_head || project.state !== "ready") throw new Error("Fixture project is not ready.");
      const ordinal = snapshot.tasks.filter((candidate) => candidate.project_id === projectId).length + 1;
      const agentTurn = options.runAgentTurn
        ? await options.runAgentTurn({
            projectId,
            projectName: project.display_name,
            taskTitle: project.config.task.title,
            instruction: project.config.task.objective,
          })
        : null;
      const admittedTask = fixtureTask(project, ordinal, agentTurn?.sessionId);
      const evolutionRound = simulateEvolution
        ? fixtureEvolutionRound(snapshot, project, admittedTask, ordinal)
        : developmentEvolutionRound(snapshot, agentTurn?.evolutionArtifacts ?? []);
      const successorHead = fixtureSuccessorHead(
        project.active_project_head,
        project.config,
        evolutionRound.artifacts.length,
      );
      const transitionRef = {
        schema_version: "2" as const,
        successor_transition_id: `${projectId}-transition-${successorHead.generation}`,
        project_id: projectId,
        kind: "run_result" as const,
        predecessor_project_head: project.active_project_head,
        expected_successor_generation: successorHead.generation,
        plan_sha256: fixtureDigest(successorHead.generation + 40),
        task_admission: admittedTask.admission,
        accepted_attempt: admittedTask.attempts[0]!,
        successor_project_head: successorHead,
      };
      const task: TaskV2 = {
        ...admittedTask,
        successor_transition: transitionRef,
        state: "closed",
      };
      const updatedProject = {
        ...project,
        active_project_head: successorHead,
        updated_at: NOW,
      };
      const agentResponse = agentTurn?.responseText
        ?? "The fixture session was admitted and completed successfully.";
      const outputFiles = agentTurn
        ? []
        : [{
            name: "results/fixture-result.md",
            summary: "Simulated session result.",
            content: `# Fixture result\n\nSession ${ordinal} completed successfully and published ${evolutionRound.artifacts.length} evolution artifacts.\n`,
            previousName: ordinal > 1 ? "results/fixture-result.md" : null,
            diffLines: [
              { kind: "context" as const, text: "# Fixture result" },
              { kind: "added" as const, text: `Session ${ordinal} published ${evolutionRound.artifacts.length} evolution artifacts.` },
            ],
          }];
      taskLogs.set(task.task_id, agentTurn?.logMessages ?? [
        "The immutable Task admission was accepted.",
        "The simulated agent completed the session.",
      ]);
      snapshot = {
        ...snapshot,
        projects: snapshot.projects.map((candidate) => candidate.project_id === projectId ? updatedProject : candidate) as never,
        tasks: [task, ...snapshot.tasks],
        transitions: {
          ...snapshot.transitions,
          [transitionRef.successor_transition_id]: {
            schema_version: "2",
            transition: transitionRef,
            state: "committed",
            progress_completed: 5,
            progress_total: 5,
            error: null,
            created_at: NOW,
            updated_at: NOW,
          },
        } as never,
        artifacts: [...evolutionRound.artifacts, ...snapshot.artifacts] as never,
        fixturePresentation: {
          tasks: {
            ...(snapshot.fixturePresentation?.tasks ?? {}),
            [task.task_id]: {
              instruction: project.config.task,
              transcript: [
                { speaker: "user", text: project.config.task.objective },
                { speaker: "agent", text: agentResponse },
              ],
              outputFiles,
              selectedEvolution: developmentEvolutionSelections(project.config),
              evolutionErrors: agentTurn?.evolutionErrors ?? [],
              usedArtifactIds: evolutionRound.usedArtifactIds,
              producedArtifactIds: evolutionRound.artifacts.map((artifact) => artifact.artifact_id),
            },
          },
          artifacts: {
            ...(snapshot.fixturePresentation?.artifacts ?? {}),
            ...evolutionRound.presentation,
          },
        },
        stream: { ...snapshot.stream, epoch: snapshot.stream.epoch + 1 },
      };
      return task;
    },
    loadTaskLogs: async (taskId) => ({
      schema_version: "2",
      items: (taskLogs.get(taskId) ?? ["No development runner logs were recorded."]).map((message, index) => ({
        sequence: index + 1,
        occurred_at: NOW,
        stream: index === 0 ? "system" : "transcript",
        message,
      })),
      next_cursor: null,
      has_more: false,
    }),
    getArtifactContent: async (artifactId) => {
      const artifact = snapshot.artifacts.find((candidate) => candidate.artifact_id === artifactId);
      if (!artifact) throw new Error("Fixture artifact is missing.");
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
      if (!artifact) throw new Error("Fixture artifact is missing.");
      const previousArtifactId = snapshot.fixturePresentation?.artifacts[artifactId]?.previousArtifactId ?? null;
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

export function createFixtureSnapshot(): DesktopProductSnapshotV2 {
  const workspace = {
    schema_version: "2",
    workspace_snapshot_id: "workspace-snapshot-7",
    project_id: "project-evidence",
    manifest_sha256: "c".repeat(64),
    entry_count: 18,
    byte_size: 82_944,
  } as const;
  const evolution = {
    schema_version: "2",
    evolution_revision_id: "evolution-revision-7",
    project_id: "project-evidence",
    manifest_sha256: "d".repeat(64),
    artifact_count: 3,
  } as const;
  const runtimeContext = {
    schema_version: "2",
    runtime_context_snapshot_id: "runtime-context-7",
    project_id: "project-evidence",
    evolution_revision_id: evolution.evolution_revision_id,
    evolution_revision_manifest_sha256: evolution.manifest_sha256,
    registry_sha256: DIGEST,
    runtime_contract_sha256: "e".repeat(64),
    manifest_sha256: "f".repeat(64),
  } as const;
  const execution = {
    schema_version: "2",
    effective_execution_snapshot_id: "execution-snapshot-7",
    project_id: "project-evidence",
    execution_mode: "codex_subscription_transcript",
    capture_mode: "transcript",
    token_level_metrics_available: false,
    producer_id: "subscription-issuer-demo",
    snapshot_sha256: "1".repeat(64),
  } as const;
  const head = {
    schema_version: "2",
    project_head_id: "project-head-7",
    project_id: "project-evidence",
    generation: 7,
    predecessor_project_head_id: "project-head-6",
    workspace_snapshot: workspace,
    evolution_revision: evolution,
    runtime_context_snapshot: runtimeContext,
    effective_execution_snapshot: execution,
    registry_sha256: DIGEST,
    manifest_sha256: "2".repeat(64),
  } as const;
  const admission = {
    schema_version: "2",
    task_admission_id: "task-admission-evidence-7",
    task_id: "task-evidence-7",
    project_id: "project-evidence",
    predecessor_project_head: head,
    workspace_snapshot: workspace,
    project_config_sha256: "3".repeat(64),
    task_envelope_sha256: "4".repeat(64),
    normalized_evolution_intent_sha256: "5".repeat(64),
    registry_sha256: DIGEST,
    admission_sha256: "6".repeat(64),
    admitted_at: NOW,
  } as const;
  const attempt1 = {
    schema_version: "2",
    attempt_id: "attempt-evidence-1",
    ordinal: 1,
    task_id: admission.task_id,
    task_admission_id: admission.task_admission_id,
    admission_sha256: admission.admission_sha256,
    project_id: admission.project_id,
    predecessor_project_head_id: head.project_head_id,
    created_at: "2026-08-13T08:20:00Z",
  } as const;
  const attempt2 = { ...attempt1, attempt_id: "attempt-evidence-2", ordinal: 2, created_at: NOW } as const;
  const transitionRef = {
    schema_version: "2",
    successor_transition_id: "successor-transition-8",
    project_id: admission.project_id,
    kind: "run_result",
    predecessor_project_head: head,
    expected_successor_generation: 8,
    plan_sha256: "7".repeat(64),
    task_admission: admission,
    accepted_attempt: attempt2,
    successor_project_head: null,
  } as const;
  const config: ScienceProjectConfigV2 = {
    schema_version: "2",
    task: {
      title: "Review the evidence",
      objective: "Check every claim against the evidence table, correct unsupported conclusions, and write a reproducible report.",
    },
    workspace: { kind: "scratch", display_name: "Evidence review workspace" },
    execution: {
      mode: "codex_subscription_transcript",
      capture_mode: "transcript",
      token_level_metrics_available: false,
      harness_id: "codex",
      codex_model: "gpt-5.3-codex-spark",
      reasoning_effort: "high",
      token_limit: 32_000,
      task_network_allow_internet: true,
    },
    evolution: { targets: fixtureEvolutionSelections() },
  };
  const profile = {
    schema_version: "2",
    profile_kind: "system_openssh",
    profile_id: "profile-gpu-lab",
    display_name: "GPU lab",
    connection_authority: "system_openssh",
    ssh_host_alias: "gpu-lab",
    catalog_generation: 1,
    connection_generation: 4,
    connection_state: "connected",
    prompt: null,
    trust: {
      schema_version: "2",
      connection_generation: 4,
      state: "trusted",
      review_id: null,
      review_sha256: null,
      key_fingerprints: [],
      repair_support: "not_needed",
    },
    failure: null,
    active_project_id: admission.project_id,
    core_api_major: 2,
    core_openapi_sha256: DIGEST,
    core_event_schema_sha256: DIGEST,
    core_registry_sha256: DIGEST,
    created_at: NOW,
    updated_at: NOW,
    etag: ETAG,
  } as const;
  const project = {
    schema_version: "2",
    project_id: admission.project_id,
    display_name: "Protein evidence study",
    config,
    project_config_sha256: admission.project_config_sha256,
    active_project_head: head,
    admission_etag: ETAG,
    state: "ready",
    created_at: NOW,
    updated_at: NOW,
    etag: ETAG,
  } as const;
  const artifacts = [
    artifact("artifact-memory-2", "text_memory", "8", 1_248, NOW),
    artifact("artifact-skill-2", "skill_bundle", "9", 2_816, NOW),
    artifact("artifact-agent-system-2", "agent_system", "0", 936, NOW),
    artifact("artifact-memory-1", "text_memory", "b", 864, "2026-08-10T08:30:00Z"),
    artifact("artifact-skill-1", "skill_bundle", "c", 1_908, "2026-08-10T08:30:00Z"),
  ] as const;

  return {
    state: {
      schema_version: "2",
      profiles: [profile] as never,
      active_profile_id: profile.profile_id,
      active_project_id: project.project_id,
      pending_operations: [],
      last_event_id: null,
      updated_at: NOW,
    },
    catalog: { schema_version: "2", catalog_generation: 1, hosts: [], warnings: [], scanned_at: NOW },
    profiles: [profile] as never,
    projects: [project] as never,
    tasks: [{
      schema_version: "2",
      task_id: admission.task_id,
      project_id: admission.project_id,
      admission,
      attempts: [attempt1, attempt2],
      authoritative_attempt_id: attempt2.attempt_id,
      successor_transition: transitionRef,
      state: "closed",
      created_at: "2026-08-13T08:20:00Z",
      updated_at: NOW,
      etag: ETAG,
    }] as never,
    transitions: {
      [transitionRef.successor_transition_id]: {
        schema_version: "2",
        transition: transitionRef,
        state: "committed",
        progress_completed: 5,
        progress_total: 5,
        error: null,
        created_at: NOW,
        updated_at: NOW,
      },
    } as never,
    timelines: {},
    artifacts: artifacts as never,
    services: [],
    capability: {
      schema_version: "2",
      project_id: admission.project_id,
      execution_mode: "codex_subscription_transcript",
      registry_sha256: DIGEST,
      capabilities_sha256: DIGEST,
      capabilities: {
        schema_version: "1",
        core_version: "0.1.10",
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
      fetched_at: NOW,
    } as never,
    validation: null,
    activeOperation: null,
    stream: { status: "fresh", epoch: 1, lastEventId: null },
    fixturePresentation: {
      tasks: {
        [admission.task_id]: {
          instruction: config.task,
          transcript: [
            { speaker: "user", text: config.task.objective },
            { speaker: "agent", text: "I checked the evidence table, corrected one unsupported claim, and saved a reproducible review." },
            { speaker: "system", text: "The accepted workspace result and three evolution artifacts were committed." },
          ],
          outputFiles: [
            {
              name: "results/evidence-review.md",
              summary: "Reviewed claims, evidence links, and unresolved hypotheses.",
              content: "# Evidence review\n\nThe supported claim remains. The unsupported conclusion is now marked as a hypothesis.\n",
              previousName: "workspace-before-session/results/evidence-review.md",
              diffLines: [
                { kind: "context", text: "# Evidence review" },
                { kind: "removed", text: "The evidence proves the proposed mechanism." },
                { kind: "added", text: "The supported claim remains." },
                { kind: "added", text: "The unsupported conclusion is now marked as a hypothesis." },
              ],
            },
            {
              name: "results/evidence-index.json",
              summary: "Machine-readable claim-to-evidence index.",
              content: "{\n  \"supported_claims\": 4,\n  \"hypotheses\": 1\n}\n",
              previousName: null,
              diffLines: [
                { kind: "added", text: "{\"supported_claims\": 4, \"hypotheses\": 1}" },
              ],
            },
          ],
          usedArtifactIds: ["artifact-memory-1", "artifact-skill-1"],
          producedArtifactIds: ["artifact-memory-2", "artifact-skill-2", "artifact-agent-system-2"],
        },
      },
      artifacts: {
        "artifact-memory-2": {
          title: "Evidence review memory",
          sourceTaskId: admission.task_id,
          targetPath: null,
          status: "updated",
          statusDetail: "Added a durable rule for separating observed evidence from inference.",
          documents: [{ path: "memory.md", content: "# Research memory\n\n- Mark every unsupported conclusion as a hypothesis.\n- Preserve sample and assay identifiers when summarizing evidence." }],
          previousArtifactId: "artifact-memory-1",
          diffLines: [
            { kind: "context", text: "# Research memory" },
            { kind: "removed", text: "Summarize the strongest conclusion." },
            { kind: "added", text: "Mark every unsupported conclusion as a hypothesis." },
          ],
        },
        "artifact-skill-2": {
          title: "Evidence audit skill",
          sourceTaskId: admission.task_id,
          targetPath: "skills/evidence-audit/SKILL.md",
          status: "created",
          statusDetail: "Created a reusable evidence-audit workflow from the successful trajectory.",
          documents: [{ path: "SKILL.md", content: "# Evidence audit\n\n1. Enumerate claims.\n2. Bind each claim to an observed result.\n3. Flag missing or contradictory evidence." }],
          previousArtifactId: null,
          diffLines: [{ kind: "added", text: "Created SKILL.md with a three-step evidence audit." }],
        },
        "artifact-agent-system-2": {
          title: "Scientific evidence instruction",
          sourceTaskId: admission.task_id,
          targetPath: "AGENTS.md",
          status: "unchanged",
          statusDetail: "The existing agent instruction already covered the observed behavior.",
          documents: [{ path: "AGENTS.md", content: "# Scientific workflow\n\nState the evidence boundary before drawing a conclusion." }],
          previousArtifactId: null,
          diffLines: [],
        },
        "artifact-memory-1": historicalPreview("Previous research memory", "memory.md", "# Research memory\n\nSummarize the strongest conclusion."),
        "artifact-skill-1": historicalPreview("Previous evidence skill", "SKILL.md", "# Evidence summary\n\nSummarize the selected results."),
      },
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
    capabilities: fixtureCapability("development", "codex_subscription_transcript")!.capabilities,
  },
): DesktopProductSnapshotV2 {
  const fixture = createFixtureSnapshot();
  const persistedProjectIds = new Set(persisted.projects.map((project) => project.projectId));
  const activeProjectId = persisted.activeProjectId && persistedProjectIds.has(persisted.activeProjectId)
    ? persisted.activeProjectId
    : persisted.projects.at(-1)?.projectId ?? null;
  const profile = {
    ...fixture.profiles[0]!,
    profile_id: "development-agent-profile",
    display_name: "GPU lab (development tunnel)",
    ssh_host_alias: "openevo-lab",
    active_project_id: activeProjectId,
  };
  const tasks: TaskV2[] = [];
  const taskPresentation: Record<
    string,
    NonNullable<DesktopProductSnapshotV2["fixturePresentation"]>["tasks"][string]
  > = {};
  const projects = persisted.projects.map((storedProject, projectIndex) => {
    const config = storedProject.config;
    let activeHead = fixtureGenesisHead(storedProject.projectId, config, projectIndex + 1);
    const projectSessions = persisted.sessions.filter((session) => session.projectId === storedProject.projectId);
    for (const [sessionIndex, session] of projectSessions.entries()) {
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
        active_project_head: activeHead,
        admission_etag: ETAG,
        state: "ready" as const,
        created_at: storedProject.createdAt,
        updated_at: storedProject.updatedAt,
        etag: ETAG,
      };
      const hydratedTask = fixtureTask(taskProject, sessionIndex + 1, session.sessionId);
      tasks.unshift({
        ...hydratedTask,
        successor_transition: null,
        state: session.state === "completed" ? "closed" : session.state,
        created_at: session.createdAt,
        updated_at: session.updatedAt,
      });
      const produced = persisted.artifacts.filter((artifact) => artifact.sessionId === session.sessionId);
      taskPresentation[session.sessionId] = {
        instruction: sessionConfig.task,
        transcript: [
          { speaker: "user", text: session.instruction },
          ...(session.response ? [{ speaker: "agent" as const, text: session.response }] : []),
          ...(session.error ? [{ speaker: "system" as const, text: session.error }] : []),
        ],
        outputFiles: [],
        selectedEvolution: session.selectedEvolution,
        evolutionErrors: session.evolutionErrors,
        evolutionJobs: persisted.evolutionJobs
          .filter((job) => job.sessionId === session.sessionId),
        usedArtifactIds: produced.flatMap((artifact) => artifact.previousArtifactId ? [artifact.previousArtifactId] : []),
        producedArtifactIds: produced.map((artifact) => artifact.artifactId),
      };
      if (session.state === "completed") {
        activeHead = fixtureSuccessorHead(activeHead, config, produced.length);
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
    return [stored.artifactId, developmentArtifactPresentation(stored, previous?.documents ?? null)];
  }));
  return {
    ...fixture,
    state: {
      ...fixture.state,
      profiles: [profile] as never,
      active_profile_id: profile.profile_id,
      active_project_id: activeProjectId,
      pending_operations: [],
    },
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
    fixturePresentation: { tasks: taskPresentation, artifacts: artifactPresentation },
    stream: { status: "fresh", epoch: 1, lastEventId: null },
  };
}

function artifact(
  artifactId: string,
  artifactType: "text_memory" | "skill_bundle" | "agent_system",
  digestCharacter: string,
  byteSize: number,
  createdAt: string,
  projectId = "project-evidence",
) {
  return {
    schema_version: "2" as const,
    artifact_id: artifactId,
    project_id: projectId,
    artifact_type: artifactType,
    manifest_sha256: digestCharacter.repeat(64),
    byte_size: byteSize,
    created_at: createdAt,
  };
}

function historicalPreview(title: string, path: string, content: string) {
  return {
    title,
    sourceTaskId: "task-evidence-previous",
    targetPath: null,
    status: "unchanged" as const,
    statusDetail: "Historical context used by the selected Task.",
    documents: [{ path, content }],
    previousArtifactId: null,
    diffLines: [],
  };
}

function fixtureDigest(seed: number): string {
  return (seed % 16).toString(16).repeat(64);
}

function fixtureGenesisHead(
  projectId: string,
  config: ScienceProjectConfigV2,
  seed: number,
): ProjectHeadRefV2 {
  const evolutionRevision = {
    schema_version: "2" as const,
    evolution_revision_id: `${projectId}-evolution-0`,
    project_id: projectId,
    manifest_sha256: fixtureDigest(seed + 1),
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
      manifest_sha256: fixtureDigest(seed + 2),
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
      runtime_contract_sha256: fixtureDigest(seed + 3),
      manifest_sha256: fixtureDigest(seed + 4),
    },
    effective_execution_snapshot: {
      schema_version: "2",
      effective_execution_snapshot_id: `${projectId}-execution-0`,
      project_id: projectId,
      execution_mode: config.execution.mode,
      capture_mode: config.execution.capture_mode,
      token_level_metrics_available: config.execution.token_level_metrics_available,
      producer_id: "fixture-verified-producer",
      snapshot_sha256: fixtureDigest(seed + 5),
    },
    registry_sha256: DIGEST,
    manifest_sha256: fixtureDigest(seed + 6),
  };
}

function fixtureCapability(
  projectId: string,
  executionMode: ScienceProjectConfigV2["execution"]["mode"],
): DesktopProductSnapshotV2["capability"] {
  const frameworkExecutionMode = executionMode === "codex_subscription_transcript" ? "subscription" : "self_deployed";
  const supportedAxis = {
    state: "supported" as const,
    message: "Supported by the development fixture.",
    reason_code: null,
    missing_requirements: [],
  };
  const support = {
    overall: "supported" as const,
    execution: supportedAxis,
    capture: supportedAxis,
    harness: supportedAxis,
    runtime: supportedAxis,
  };
  return {
    schema_version: "2",
    project_id: projectId,
    execution_mode: executionMode,
    registry_sha256: DIGEST,
    capabilities_sha256: DIGEST,
    capabilities: {
      schema_version: "1",
      core_version: "fixture",
      registry_digest: DIGEST,
      evaluated_profile: {
        execution_mode: frameworkExecutionMode,
        capture_mode: "transcript",
        harness_id: "codex",
        harness_capabilities: [],
        runtime_capabilities: [],
      },
      targets: FIXTURE_EVOLUTION_TARGETS.map((target, index) => ({
        target_id: target.targetId,
        display_name: target.displayName,
        description: target.description,
        artifact_type: target.artifactType,
        exposure: "desktop",
        maturity: "stable",
        handler_id: `fixture_${target.targetId}_handler`,
        configured_default_method_id: target.methodId,
        effective_default_method_id: target.methodId,
        configured_default_support: support,
        renderer_kind: target.artifactType === "skill_bundle" ? "file_bundle" : "markdown",
        renderer_contract_version: "1",
        contribution_contract_version: "2",
        context_order: (index + 1) * 10,
        implementation_identity_digest: fixtureDigest(index + 50),
        handler_identity_digest: fixtureDigest(index + 54),
        accepted_methods: [{
          method_id: target.methodId,
          implementation_identity_digest: fixtureDigest(index + 58),
          support,
        }],
        selection_resolvers: [],
        methods: [{
          method_id: target.methodId,
          display_name: `Fixture ${target.displayName}`,
          description: target.description,
          exposure: "desktop",
          maturity: "stable",
          execution_modes: [frameworkExecutionMode],
          capture_modes: ["transcript"],
          supported_harness_ids: ["codex"],
          harness_requirements: [],
          runtime_requirements: [],
          input_bindings: [{
            binding_id: "current_dataset",
            source: "current_dataset",
            artifact_type: "dataset",
            min_count: 1,
            max_count: null,
          }],
          output_artifact_types: [target.artifactType],
          config_schema_json: '{"additionalProperties":false,"properties":{},"type":"object"}',
          default_config_json: "{}",
          implementation_identity_digest: fixtureDigest(index + 58),
          support,
        }],
      })),
    },
    fetched_at: NOW,
  } as never;
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

function fixtureEvolutionSelections(): ScienceProjectConfigV2["evolution"]["targets"] {
  return Object.fromEntries(FIXTURE_EVOLUTION_TARGETS.map((target) => [target.targetId, {
    enabled: true,
    method: target.methodId,
    config: {},
  }])) as ScienceProjectConfigV2["evolution"]["targets"];
}

function withFixtureEvolutionDefaults(config: ScienceProjectConfigV2): ScienceProjectConfigV2 {
  if (Object.keys(config.evolution.targets).length > 0) return config;
  return { ...config, evolution: { targets: fixtureEvolutionSelections() } };
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
        ? snapshot.fixturePresentation?.artifacts[artifact.previousArtifactId]?.documents.map((document) => ({
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

function fixtureEvolutionRound(
  snapshot: DesktopProductSnapshotV2,
  project: DesktopProductSnapshotV2["projects"][number],
  task: TaskV2,
  ordinal: number,
) {
  const generation = project.active_project_head!.generation + 1;
  const presentation: Record<string, NonNullable<DesktopProductSnapshotV2["fixturePresentation"]>["artifacts"][string]> = {};
  const usedArtifactIds: string[] = [];
  const artifacts = FIXTURE_EVOLUTION_TARGETS.flatMap((target, index) => {
    const selection = project.config.evolution.targets[target.targetId];
    if (selection?.enabled !== true || selection.method === null) return [];

    const previous = snapshot.artifacts.find((candidate) => (
      candidate.project_id === project.project_id
      && candidate.artifact_type === target.artifactType
    ));
    if (previous) usedArtifactIds.push(previous.artifact_id);
    const previousDocument = previous
      ? snapshot.fixturePresentation?.artifacts[previous.artifact_id]?.documents.find((document) => document.path === target.path)
      : undefined;
    const sentence = target.sentence(ordinal, project.config.task.title);
    const previousContent = previousDocument?.content.trimEnd() ?? target.heading;
    const content = `${previousContent}\n${sentence}\n`;
    const artifactId = `${project.project_id}-${target.targetId}-${generation}`;
    const produced = artifact(
      artifactId,
      target.artifactType,
      ((generation * 3 + index + 1) % 16).toString(16),
      new TextEncoder().encode(content).byteLength,
      NOW,
      project.project_id,
    );
    presentation[artifactId] = {
      title: target.title,
      sourceTaskId: task.task_id,
      targetPath: target.targetPath,
      status: previous ? "updated" : "created",
      statusDetail: `Fixture Session ${ordinal} appended one sentence for Project Head ${generation}.`,
      documents: [{ path: target.path, content }],
      previousArtifactId: previous?.artifact_id ?? null,
      diffLines: previous
        ? [{ kind: "context", text: target.heading }, { kind: "added", text: sentence }]
        : [{ kind: "added", text: target.heading }, { kind: "added", text: sentence }],
    };
    return [produced];
  });
  return { artifacts, presentation, usedArtifactIds };
}

function fixtureSuccessorHead(
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
    manifest_sha256: fixtureDigest(seed + 1),
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
      manifest_sha256: fixtureDigest(seed + 2),
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
      runtime_contract_sha256: fixtureDigest(seed + 3),
      manifest_sha256: fixtureDigest(seed + 4),
    },
    effective_execution_snapshot: {
      schema_version: "2",
      effective_execution_snapshot_id: `${projectId}-execution-${generation}`,
      project_id: projectId,
      execution_mode: config.execution.mode,
      capture_mode: config.execution.capture_mode,
      token_level_metrics_available: config.execution.token_level_metrics_available,
      producer_id: "fixture-verified-producer",
      snapshot_sha256: fixtureDigest(seed + 5),
    },
    registry_sha256: DIGEST,
    manifest_sha256: fixtureDigest(seed + 6),
  };
}

function fixtureTask(
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
    task_envelope_sha256: fixtureDigest(ordinal + 7),
    normalized_evolution_intent_sha256: fixtureDigest(ordinal + 8),
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
