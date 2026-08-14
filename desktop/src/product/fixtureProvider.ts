import {
  scienceProjectConfigSha256ForV2,
  taskAdmissionSha256ForV2,
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
  let snapshot = createFixtureSnapshot();

  return {
    ...unavailableDesktopProductProviderV2,
    featureFlags: ["system_openssh_profiles"],
    refresh: async () => ({ status: "fresh", snapshot }),
    subscribe: () => () => undefined,
    createProject: async (draft) => {
      const sequence = snapshot.projects.length + 1;
      const projectId = `fixture-project-${sequence}`;
      const config = withFixtureEvolutionDefaults(draft.config);
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
      snapshot = {
        ...snapshot,
        projects: [...snapshot.projects, project],
        state: { ...snapshot.state, active_project_id: projectId, updated_at: NOW },
        capability: fixtureCapability(projectId, config.execution.mode),
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
      const project = snapshot.projects.find((candidate) => candidate.project_id === projectId);
      if (!project) throw new Error("Fixture project is missing.");
      const updated = {
        ...project,
        display_name: displayName,
        config,
        project_config_sha256: scienceProjectConfigSha256ForV2(config),
        updated_at: NOW,
      };
      snapshot = {
        ...snapshot,
        projects: snapshot.projects.map((candidate) => candidate.project_id === projectId ? updated : candidate),
        stream: { ...snapshot.stream, epoch: snapshot.stream.epoch + 1 },
      };
      return updated;
    },
    activateProject: async (projectId) => {
      const project = snapshot.projects.find((candidate) => candidate.project_id === projectId);
      if (!project?.active_project_head) throw new Error("Fixture project is missing or not activatable.");
      snapshot = {
        ...snapshot,
        state: { ...snapshot.state, active_project_id: projectId, updated_at: NOW },
        profiles: snapshot.profiles.map((profile) => ({ ...profile, active_project_id: projectId })) as never,
        capability: fixtureCapability(projectId, project.config.execution.mode),
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
      const project = snapshot.projects.find((candidate) => candidate.project_id === projectId);
      if (!project?.active_project_head || project.state !== "ready") throw new Error("Fixture project is not ready.");
      const ordinal = snapshot.tasks.filter((candidate) => candidate.project_id === projectId).length + 1;
      const admittedTask = fixtureTask(project, ordinal);
      const evolutionRound = fixtureEvolutionRound(snapshot, project, admittedTask, ordinal);
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
                { speaker: "agent", text: "The fixture session was admitted and completed successfully." },
              ],
              outputFiles: [{
                name: "results/fixture-result.md",
                summary: "Simulated session result.",
                content: `# Fixture result\n\nSession ${ordinal} completed successfully and published ${evolutionRound.artifacts.length} evolution artifacts.\n`,
                previousName: ordinal > 1 ? "results/fixture-result.md" : null,
                diffLines: [
                  { kind: "context", text: "# Fixture result" },
                  { kind: "added", text: `Session ${ordinal} published ${evolutionRound.artifacts.length} evolution artifacts.` },
                ],
              }],
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
    loadTaskLogs: async () => ({
      schema_version: "2",
      items: [
        { sequence: 1, occurred_at: NOW, stream: "system", message: "The immutable Task admission was accepted." },
        { sequence: 2, occurred_at: NOW, stream: "transcript", message: "The agent completed the evidence review." },
      ],
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
): TaskV2 {
  const head = project.active_project_head!;
  const taskId = `${project.project_id}-task-${ordinal}`;
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
