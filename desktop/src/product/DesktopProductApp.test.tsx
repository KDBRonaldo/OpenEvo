// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DesktopProductSnapshotV2 } from "./providerV2";
import type { OperationV2 } from "../api/v2/schemas";
import {
  unavailableDesktopProductProviderV2,
  type DesktopProductProviderV2,
} from "./providerV2";
import type { LifecycleOperationStateV2 } from "./lifecycleOperationsV2";
import { DesktopProductApp } from "./DesktopProductApp";

const NOW = "2026-07-23T06:00:00Z";
const DIGEST = "a".repeat(64);
const ETAG = `"${"b".repeat(64)}"`;

function baseSnapshot(
  overrides: Partial<DesktopProductSnapshotV2> = {},
): DesktopProductSnapshotV2 {
  return {
    state: {
      schema_version: "2",
      profiles: [],
      active_profile_id: null,
      active_project_id: null,
      pending_operations: [],
      last_event_id: null,
      updated_at: NOW,
    },
    catalog: {
      schema_version: "2",
      catalog_generation: 3,
      hosts: [
        {
          schema_version: "2",
          ssh_host_alias: "gpu-lab",
          availability: "selectable",
          source_kind: "literal_host",
        },
      ],
      warnings: [
        {
          schema_version: "2",
          code: "dynamic_hosts_not_enumerated",
          action: "manual_alias_available",
          affected_entry_count: 1,
        },
      ],
      scanned_at: NOW,
    },
    profiles: [],
    projects: [],
    tasks: [],
    transitions: {},
    timelines: {},
    artifacts: [],
    services: [],
    capability: null,
    validation: null,
    activeOperation: null,
    stream: { status: "fresh", epoch: 1, lastEventId: null },
    ...overrides,
  };
}

function systemProfile(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: "2",
    profile_kind: "system_openssh",
    profile_id: "profile-gpu",
    display_name: "GPU lab",
    connection_authority: "system_openssh",
    ssh_host_alias: "gpu-lab",
    catalog_generation: 3,
    connection_generation: 4,
    connection_state: "disconnected",
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
    active_project_id: null,
    core_api_major: null,
    core_openapi_sha256: null,
    core_event_schema_sha256: null,
    core_registry_sha256: null,
    created_at: NOW,
    updated_at: NOW,
    etag: ETAG,
    ...overrides,
  };
}

function authoritySnapshot(
  state: "ready" | "not_ready" = "ready",
): DesktopProductSnapshotV2 {
  const profile = systemProfile({
    connection_state: "connected",
    active_project_id: "project-1",
    core_api_major: 2,
    core_openapi_sha256: DIGEST,
    core_event_schema_sha256: DIGEST,
    core_registry_sha256: DIGEST,
  });
  const workspace = {
    schema_version: "2",
    workspace_snapshot_id: "workspace-snapshot-1",
    project_id: "project-1",
    manifest_sha256: DIGEST,
    entry_count: 2,
    byte_size: 64,
  };
  const evolution = {
    schema_version: "2",
    evolution_revision_id: "evolution-revision-1",
    project_id: "project-1",
    manifest_sha256: "c".repeat(64),
    artifact_count: 2,
  };
  const context = {
    schema_version: "2",
    runtime_context_snapshot_id: "runtime-context-1",
    project_id: "project-1",
    evolution_revision_id: evolution.evolution_revision_id,
    evolution_revision_manifest_sha256: evolution.manifest_sha256,
    registry_sha256: DIGEST,
    runtime_contract_sha256: "d".repeat(64),
    manifest_sha256: "e".repeat(64),
  };
  const execution = {
    schema_version: "2",
    effective_execution_snapshot_id: "effective-execution-1",
    project_id: "project-1",
    execution_mode: "codex_subscription_transcript",
    capture_mode: "transcript",
    token_level_metrics_available: false,
    producer_id: "subscription-issuer-1",
    snapshot_sha256: "f".repeat(64),
  };
  const head = {
    schema_version: "2",
    project_head_id: "project-head-7",
    project_id: "project-1",
    generation: 7,
    predecessor_project_head_id: "project-head-6",
    workspace_snapshot: workspace,
    evolution_revision: evolution,
    runtime_context_snapshot: context,
    effective_execution_snapshot: execution,
    registry_sha256: DIGEST,
    manifest_sha256: "1".repeat(64),
  };
  const admission = {
    schema_version: "2",
    task_admission_id: "task-admission-1",
    task_id: "task-1",
    project_id: "project-1",
    predecessor_project_head: head,
    workspace_snapshot: workspace,
    project_config_sha256: "2".repeat(64),
    task_envelope_sha256: "3".repeat(64),
    normalized_evolution_intent_sha256: "4".repeat(64),
    registry_sha256: DIGEST,
    admission_sha256: "5".repeat(64),
    admitted_at: NOW,
  };
  const attempt = {
    schema_version: "2",
    attempt_id: "attempt-2",
    ordinal: 2,
    task_id: "task-1",
    task_admission_id: "task-admission-1",
    admission_sha256: admission.admission_sha256,
    project_id: "project-1",
    predecessor_project_head_id: head.project_head_id,
    created_at: NOW,
  };
  const transitionRef = {
    schema_version: "2",
    successor_transition_id: "successor-transition-8",
    project_id: "project-1",
    kind: "run_result",
    predecessor_project_head: head,
    expected_successor_generation: 8,
    plan_sha256: "6".repeat(64),
    task_admission: admission,
    accepted_attempt: attempt,
    successor_project_head: null,
  };
  const project = {
    schema_version: "2",
    project_id: "project-1",
    display_name: "Protein study",
    config: {
      schema_version: "2",
      task: {
        title: "Review evidence",
        objective: "Review the evidence and update the workspace.",
      },
      workspace: { kind: "scratch", display_name: "Research workspace" },
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
      evolution: { targets: {} },
    },
    project_config_sha256: admission.project_config_sha256,
    active_project_head: head,
    admission_etag: ETAG,
    state,
    created_at: NOW,
    updated_at: NOW,
    etag: ETAG,
  };
  const transition = {
    schema_version: "2",
    transition: transitionRef,
    state: state === "ready" ? "committed" : "failed",
    progress_completed: state === "ready" ? 5 : 2,
    progress_total: 5,
    error:
      state === "ready"
        ? null
        : {
            schema_version: "2",
            request_id: "request-1",
            code: "successor_materialization_failed",
            http_status: 503,
            message: "The successor could not be materialized.",
            category: "transition",
            retryable: true,
            repair_action: "retry",
            next_action: "Retry the successor transition.",
          },
    created_at: NOW,
    updated_at: NOW,
  };
  const artifacts = [
    {
      schema_version: "2",
      artifact_id: "artifact-memory-2",
      project_id: "project-1",
      artifact_type: "text_memory",
      manifest_sha256: "7".repeat(64),
      byte_size: 1_248,
      created_at: NOW,
    },
    {
      schema_version: "2",
      artifact_id: "artifact-skill-2",
      project_id: "project-1",
      artifact_type: "skill_bundle",
      manifest_sha256: "8".repeat(64),
      byte_size: 2_816,
      created_at: NOW,
    },
    {
      schema_version: "2",
      artifact_id: "artifact-agent-system-2",
      project_id: "project-1",
      artifact_type: "agent_system",
      manifest_sha256: "9".repeat(64),
      byte_size: 936,
      created_at: NOW,
    },
    {
      schema_version: "2",
      artifact_id: "artifact-memory-1",
      project_id: "project-1",
      artifact_type: "text_memory",
      manifest_sha256: "a".repeat(64),
      byte_size: 824,
      created_at: "2026-07-22T06:00:00Z",
    },
    {
      schema_version: "2",
      artifact_id: "artifact-skill-1",
      project_id: "project-1",
      artifact_type: "skill_bundle",
      manifest_sha256: "b".repeat(64),
      byte_size: 1_712,
      created_at: "2026-07-22T06:00:00Z",
    },
  ] as const;
  return baseSnapshot({
    state: {
      schema_version: "2",
      profiles: [profile] as never,
      active_profile_id: profile.profile_id,
      active_project_id: project.project_id,
      pending_operations: [],
      last_event_id: null,
      updated_at: NOW,
    },
    profiles: [profile] as never,
    projects: [project] as never,
    tasks: [
      {
        schema_version: "2",
        task_id: "task-1",
        project_id: "project-1",
        admission,
        attempts: [
          { ...attempt, attempt_id: "attempt-1", ordinal: 1 },
          attempt,
        ],
        authoritative_attempt_id: attempt.attempt_id,
        successor_transition: transitionRef,
        state: state === "ready" ? "closed" : "waiting_for_successor",
        created_at: NOW,
        updated_at: NOW,
        etag: ETAG,
      },
    ] as never,
    transitions: {
      [transitionRef.successor_transition_id]: transition,
    } as never,
    artifacts: artifacts as never,
    fixturePresentation: {
      tasks: {
        "task-1": {
          instruction: project.config.task,
          transcript: [
            {
              speaker: "user",
              text: "Review the evidence and update the workspace.",
            },
            {
              speaker: "agent",
              text: "I checked the evidence table, corrected the unsupported claim, and saved a reproducible report.",
            },
          ],
          outputFiles: [
            {
              name: "results/evidence-review.md",
              summary: "Reviewed claims and supporting evidence.",
              content:
                "# Evidence review\n\nUnsupported conclusions are marked as hypotheses.",
              previousName:
                "workspace-before-session/results/evidence-review.md",
              diffLines: [
                { kind: "removed" as const, text: "The mechanism is proven." },
                {
                  kind: "added" as const,
                  text: "Unsupported conclusions are marked as hypotheses.",
                },
              ],
            },
          ],
          usedArtifactIds: ["artifact-memory-1", "artifact-skill-1"],
          producedArtifactIds: [
            "artifact-memory-2",
            "artifact-skill-2",
            "artifact-agent-system-2",
          ],
        },
      },
      artifacts: {
        "artifact-memory-2": {
          title: "Evidence review memory",
          sourceTaskId: "task-1",
          targetPath: null,
          status: "updated",
          statusDetail:
            "Added a durable rule for distinguishing measured evidence from inference.",
          documents: [
            {
              path: "memory.md",
              content:
                "# Research memory\n\n- Mark every unsupported conclusion as a hypothesis.\n- Preserve sample and assay identifiers when summarizing evidence.",
            },
          ],
          previousArtifactId: "artifact-memory-1",
          diffLines: [
            { kind: "context", text: "# Research memory" },
            { kind: "removed", text: "Summarize the strongest conclusion." },
            {
              kind: "added",
              text: "Mark every unsupported conclusion as a hypothesis.",
            },
          ],
        },
        "artifact-skill-2": {
          title: "Trajectory-to-skill: evidence audit",
          sourceTaskId: "task-1",
          targetPath: "skills/evidence-audit/SKILL.md",
          status: "created",
          statusDetail:
            "Created a reusable evidence-audit workflow from the successful trajectory.",
          documents: [
            {
              path: "SKILL.md",
              content:
                "# Evidence audit\n\n1. Enumerate claims.\n2. Bind each claim to an observed result.\n3. Flag missing or contradictory evidence.",
            },
          ],
          previousArtifactId: null,
          diffLines: [
            {
              kind: "added",
              text: "Created SKILL.md with a three-step evidence audit.",
            },
          ],
        },
        "artifact-agent-system-2": {
          title: "Scientific evidence instruction",
          sourceTaskId: "task-1",
          targetPath: "AGENTS.md",
          status: "unchanged",
          statusDetail:
            "The existing agent instruction already covered the observed behavior.",
          documents: [
            {
              path: "AGENTS.md",
              content:
                "# Scientific workflow\n\nState the evidence boundary before drawing a conclusion.",
            },
          ],
          previousArtifactId: "artifact-agent-system-1",
          diffLines: [],
        },
        "artifact-memory-1": {
          title: "Previous research memory",
          sourceTaskId: "task-previous",
          targetPath: null,
          status: "unchanged",
          statusDetail: "Historical context used by the selected Task.",
          documents: [
            {
              path: "memory.md",
              content:
                "# Research memory\n\nSummarize the strongest conclusion.",
            },
          ],
          previousArtifactId: null,
          diffLines: [],
        },
        "artifact-skill-1": {
          title: "Previous evidence skill",
          sourceTaskId: "task-previous",
          targetPath: "skills/evidence-summary/SKILL.md",
          status: "unchanged",
          statusDetail: "Historical context used by the selected Task.",
          documents: [
            {
              path: "SKILL.md",
              content: "# Evidence summary\n\nSummarize the selected results.",
            },
          ],
          previousArtifactId: null,
          diffLines: [],
        },
      },
    },
    capability: {
      schema_version: "2",
      project_id: "project-1",
      execution_mode: "codex_subscription_transcript",
      registry_sha256: DIGEST,
      capabilities_sha256: DIGEST,
      capabilities: {
        schema_version: "1",
        core_version: "0.1.9",
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
  });
}

function providerFixture(initial: DesktopProductSnapshotV2) {
  let current = initial;
  const provider = {
    ...unavailableDesktopProductProviderV2,
    featureFlags: ["system_openssh_profiles"],
    refresh: vi.fn(async () => ({
      status: "fresh" as const,
      snapshot: current,
    })),
    subscribe: vi.fn(() => () => undefined),
    createProfile: vi.fn(async (displayName: string, alias: string) => {
      const created = systemProfile({
        display_name: displayName,
        ssh_host_alias: alias,
      });
      current = baseSnapshot({
        ...current,
        profiles: [created] as never,
        state: { ...current.state, profiles: [created] as never },
        stream: {
          status: "fresh",
          epoch: current.stream.epoch + 1,
          lastEventId: null,
        },
      });
      return created as never;
    }),
    connectProfile: vi.fn(
      async () =>
        ({
          schema_version: "2",
          operation_id: "operation-connect-1",
          kind: "profile_connect",
          status: "queued",
          failure: null,
          created_at: NOW,
          updated_at: NOW,
        }) as never,
    ),
    disconnectProfile: vi.fn(
      async () =>
        ({
          schema_version: "2",
          operation_id: "operation-disconnect-1",
          kind: "profile_disconnect",
          status: "queued",
          failure: null,
          created_at: NOW,
          updated_at: NOW,
        }) as never,
    ),
    rebindProfile: vi.fn(async () => systemProfile() as never),
    reviewHostKey: vi.fn(
      async () =>
        ({
          schema_version: "2",
          operation_id: "operation-review-1",
          kind: "host_key_review",
          status: "queued",
          failure: null,
          created_at: NOW,
          updated_at: NOW,
        }) as never,
    ),
    rescanSshHosts: vi.fn(async () => current.catalog),
    updateProject: vi.fn(
      async (
        projectId: string,
        displayName: string,
        config: DesktopProductSnapshotV2["projects"][number]["config"],
      ) => {
        const existing = current.projects.find(
          (project) => project.project_id === projectId,
        );
        if (!existing) throw new Error("project is missing");
        const updated = { ...existing, display_name: displayName, config };
        current = {
          ...current,
          projects: current.projects.map((project) =>
            project.project_id === projectId ? updated : project,
          ),
          stream: {
            status: "fresh",
            epoch: current.stream.epoch + 1,
            lastEventId: null,
          },
        };
        return updated;
      },
    ),
    validateProject: vi.fn(
      async (projectId: string) =>
        ({
          schema_version: "2",
          project_id: projectId,
          valid: true,
          registry_sha256: DIGEST,
          checks: [],
          validated_at: NOW,
        }) as never,
    ),
    submitTask: vi.fn(async () => current.tasks[0] as never),
    getArtifactContent: vi.fn(async (artifactId: string) => {
      const artifact = current.artifacts.find(
        (item) => item.artifact_id === artifactId,
      );
      if (!artifact) throw new Error("artifact is missing");
      return {
        schema_version: "2",
        artifact,
        media_type: "text/markdown",
        content_sha256: artifact.manifest_sha256,
        byte_size: artifact.byte_size,
      } as never;
    }),
    getArtifactDiff: vi.fn(async (artifactId: string) => {
      const artifact = current.artifacts.find(
        (item) => item.artifact_id === artifactId,
      );
      if (!artifact) throw new Error("artifact is missing");
      const previous =
        current.fixturePresentation?.artifacts[artifactId]
          ?.previousArtifactId ?? null;
      return {
        schema_version: "2",
        artifact_id: artifactId,
        previous_artifact_id: previous,
        current_manifest_sha256: artifact.manifest_sha256,
        previous_manifest_sha256: previous ? DIGEST : null,
        status: previous ? "available" : "unavailable",
      } as never;
    }),
  } satisfies DesktopProductProviderV2;
  return provider;
}

describe("Desktop v2 product renderer", () => {
  let root: Root | null = null;

  beforeEach(() => {
    document.body.innerHTML = '<div id="root"></div>';
    (
      globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
    ).IS_REACT_ACT_ENVIRONMENT = true;
  });

  afterEach(async () => {
    if (root) await act(async () => root?.unmount());
    root = null;
    vi.useRealTimers();
    document.body.innerHTML = "";
  });

  it("opens configured-host setup immediately and never renders manual connection fields", async () => {
    const provider = providerFixture(baseSnapshot());
    root = await render(provider);

    await click("Add remote workspace");

    expect(dialog()?.textContent).toContain("Configured SSH host");
    expect(dialog()?.textContent).toContain("gpu-lab");
    expect(dialog()?.textContent).toContain(
      "Some configured hosts cannot be listed",
    );
    const fieldLabels = [...(dialog()?.querySelectorAll("label") ?? [])]
      .map((label) => label.textContent ?? "")
      .join(" ");
    expect(fieldLabels).not.toMatch(
      /server address|user name|port|private key|password/i,
    );
    expect(button("Use another SSH alias")).toBeTruthy();
  });

  it("labels the offline sample head as demo authority instead of an active remote head", async () => {
    root = await render(providerFixture(baseSnapshot()));

    expect(document.body.textContent).toContain("Demo Project Head");
    expect(document.body.textContent).not.toContain("Active Project Head");
  });

  it("keeps one authoritative event subscription across the initial refresh", async () => {
    const provider = providerFixture(baseSnapshot());
    root = await render(provider);

    expect(provider.refresh).toHaveBeenCalledTimes(1);
    expect(provider.subscribe).toHaveBeenCalledTimes(1);
  });

  it("creates and connects using only the selected SSH alias", async () => {
    const provider = providerFixture(baseSnapshot());
    root = await render(provider);
    await click("Add remote workspace");
    setInput("Workspace name", "Main GPU");
    await click("Save and connect");

    expect(provider.createProfile).toHaveBeenCalledWith(
      "Main GPU",
      "gpu-lab",
      expect.objectContaining({ streamEpoch: 1 }),
    );
    expect(provider.connectProfile).toHaveBeenCalledWith(
      "profile-gpu",
      expect.objectContaining({ streamEpoch: 2 }),
    );
    expect(JSON.stringify(provider.createProfile.mock.calls)).not.toMatch(
      /username|password|identity|host_path/i,
    );
  });

  it("offers explicit rebind for retained Preview profiles", async () => {
    const legacy = {
      schema_version: "2",
      profile_kind: "legacy_explicit",
      profile_id: "legacy-profile-1",
      display_name: "Old server",
      connectable: false,
      migration_state: "rebind_required",
      created_at: NOW,
      updated_at: NOW,
      etag: ETAG,
    } as const;
    const snapshot = baseSnapshot({
      profiles: [legacy],
      state: { ...baseSnapshot().state, profiles: [legacy] },
    });
    const provider = providerFixture(snapshot);
    root = await render(provider);
    await click("Add remote workspace");
    await click("Rebind to configured SSH host");

    expect(provider.rebindProfile).toHaveBeenCalledWith(
      "legacy-profile-1",
      "gpu-lab",
      expect.objectContaining({ streamEpoch: 1 }),
    );
  });

  it("renders changed-host-key evidence and requires an explicit review action", async () => {
    const profile = systemProfile({
      connection_state: "host_key_review",
      trust: {
        schema_version: "2",
        connection_generation: 4,
        state: "changed_key_blocked",
        review_id: "review-1",
        review_sha256: DIGEST,
        key_fingerprints: [
          {
            schema_version: "2",
            algorithm: "ssh-ed25519",
            sha256_fingerprint: `SHA256:${"A".repeat(43)}`,
            role: "presented",
          },
        ],
        repair_support: "automatic_replacement_available",
      },
    });
    const snapshot = baseSnapshot({
      profiles: [profile] as never,
      state: { ...baseSnapshot().state, profiles: [profile] as never },
    });
    const provider = providerFixture(snapshot);
    root = await render(provider);
    await click("Add remote workspace");

    expect(dialog()?.textContent).toContain("Changed host key blocked");
    expect(dialog()?.textContent).toContain(`SHA256:${"A".repeat(43)}`);
    await click("Replace changed key and reconnect");
    expect(provider.reviewHostKey).toHaveBeenCalledWith(
      "profile-gpu",
      "replace_changed_key",
      expect.objectContaining({ streamEpoch: 1 }),
    );
  });

  it("retries failed SSH cleanup as disconnect and keeps lost authority blocked", async () => {
    const retryable = systemProfile({
      connection_state: "failed",
      failure: {
        schema_version: "2",
        code: "ssh_cleanup_failed",
        summary: "The system OpenSSH connection could not be closed safely.",
        retryable: true,
        action: "retry",
        affected_resource_id: "profile-gpu",
      },
    });
    const retrySnapshot = baseSnapshot({
      profiles: [retryable] as never,
      state: { ...baseSnapshot().state, profiles: [retryable] as never },
    });
    const retryProvider = providerFixture(retrySnapshot);
    root = await render(retryProvider);
    await click("Add remote workspace");

    await click("Retry disconnect");
    expect(retryProvider.disconnectProfile).toHaveBeenCalledWith(
      "profile-gpu",
      expect.objectContaining({ streamEpoch: 1 }),
    );

    await act(async () => root?.unmount());
    root = null;
    document.body.innerHTML = '<div id="root"></div>';
    const quarantined = systemProfile({
      connection_generation: 5,
      connection_state: "failed",
      trust: {
        ...retryable.trust,
        connection_generation: 5,
        state: "unverified",
      },
      failure: {
        schema_version: "2",
        code: "ssh_cleanup_authority_lost",
        summary:
          "Desktop cannot prove that the previous system OpenSSH master stopped.",
        retryable: false,
        action: "administrator_action",
        affected_resource_id: "profile-gpu",
      },
    });
    root = await render(
      providerFixture(
        baseSnapshot({
          profiles: [quarantined] as never,
          state: { ...baseSnapshot().state, profiles: [quarantined] as never },
        }),
      ),
    );
    await click("Add remote workspace");

    expect(dialog()?.textContent).toContain(
      "Administrator action is required before this workspace can reconnect.",
    );
    const actionLabels = [...(dialog()?.querySelectorAll("button") ?? [])].map(
      (candidate) => candidate.textContent?.trim(),
    );
    expect(actionLabels).not.toContain("Connect");
    expect(actionLabels).not.toContain("Retry disconnect");
  });

  it("distinguishes task, admission, attempt, project head, evolution, runtime, and execution identities", async () => {
    root = await render(providerFixture(authoritySnapshot()));

    await click("Review evidence");

    expect(document.body.textContent).toContain("Task task-1");
    expect(document.body.textContent).toContain("Task Admission");
    expect(document.body.textContent).toContain("task-admission-1");
    expect(document.body.textContent).toContain("Attempt 2");
    expect(document.body.textContent).toContain("attempt-2");
    expect(document.body.textContent).toContain("Project Head");
    expect(document.body.textContent).toContain("project-head-7");
    expect(document.body.textContent).toContain("Generation 7");
    expect(document.body.textContent).toContain("Evolution Revision");
    expect(document.body.textContent).toContain("evolution-revision-1");
    expect(document.body.textContent).toContain("Runtime Context Snapshot");
    expect(document.body.textContent).toContain("runtime-context-1");
    expect(document.body.textContent).toContain("Effective Execution Snapshot");
    expect(document.body.textContent).toContain("effective-execution-1");
    expect(document.body.textContent).toContain("Successor Transition");
    expect(document.body.textContent).toContain("successor-transition-8");
  });

  it("opens one Task as a result detail with transcript, files, artifacts, and transition", async () => {
    root = await render(providerFixture(authoritySnapshot()));

    await click("Review evidence");

    expect(
      document.querySelector('[data-testid="session-detail-workspace"]'),
    ).toBeTruthy();
    expect(document.body.textContent).toContain("Task result");
    expect(document.body.textContent).toContain("Review evidence");
    expect(document.body.textContent).toContain("I checked the evidence table");
    expect(document.body.textContent).toContain("results/evidence-review.md");
    expect(document.body.textContent).toContain("Evolution produced");
    expect(document.body.textContent).toContain("artifact-memory-2");
    expect(document.body.textContent).toContain("Context used");
    expect(document.body.textContent).toContain("artifact-memory-1");
    expect(document.body.textContent).toContain("authoritative · closed");
    expect(document.body.textContent).toContain("superseded");
    expect(document.body.textContent).toContain("successor-transition-8");

    await click("artifact-memory-2");
    expect(
      document.querySelector('[data-testid="session-result-inspector"]'),
    ).toBeTruthy();
    expect(document.body.textContent).toContain(
      "Compared with artifact-memory-1",
    );
    expect(document.body.textContent).toContain(
      "Mark every unsupported conclusion as a hypothesis",
    );

    await click("results/evidence-review.md");
    expect(document.body.textContent).toContain(
      "workspace-before-session/results/evidence-review.md",
    );
    expect(document.body.textContent).toContain("The mechanism is proven");
    await click("Current content");
    expect(document.body.textContent).toContain(
      "Unsupported conclusions are marked as hypotheses",
    );
    expect(document.body.textContent).not.toContain("Task draft");
    expect(document.body.textContent).not.toContain("Session history");

    await click("Back to Protein study");

    expect(
      document.querySelector('[data-testid="session-detail-workspace"]'),
    ).toBeFalsy();
    expect(document.body.textContent).toContain("Task draft");
    expect(document.body.textContent).toContain("Session history");
  });

  it("browses memory, skill, and agent-system previews and their changes", async () => {
    const provider = providerFixture(authoritySnapshot());
    root = await render(provider);
    await click("Evolution");

    expect(document.body.textContent).toContain("Cross-session changes");
    expect(document.body.textContent).toContain("Evidence review memory");
    expect(document.body.textContent).toContain(
      "Trajectory-to-skill: evidence audit",
    );
    expect(document.body.textContent).toContain(
      "Scientific evidence instruction",
    );
    expect(document.body.textContent).toContain(
      "Mark every unsupported conclusion as a hypothesis",
    );

    await click("Changes");
    expect(document.body.textContent).toContain(
      "Compared with artifact-memory-1",
    );
    expect(document.body.textContent).toContain(
      "Summarize the strongest conclusion",
    );

    await click("Trajectory-to-skill: evidence audit");
    expect(document.body.textContent).toContain(
      "skills/evidence-audit/SKILL.md",
    );
    expect(document.body.textContent).toContain("Enumerate claims");
    expect(provider.getArtifactContent).toHaveBeenCalled();
    expect(provider.getArtifactDiff).toHaveBeenCalled();
  });

  it("updates the entered task and starts the session with one user action", async () => {
    const snapshot = authoritySnapshot();
    const provider = providerFixture(snapshot);
    root = await render(provider);

    expect(input("Task title").value).toBe("Review evidence");
    expect(textarea("Task instructions").value).toBe(
      "Review the evidence and update the workspace.",
    );
    expect(button("Start session").disabled).toBe(false);
    expect(document.body.textContent).not.toContain("Save task");

    setInput("Task title", "Verify the selected evidence");
    setTextarea(
      "Task instructions",
      "Run the existing checks and summarize any unsupported claims.",
    );
    expect(button("Start session").disabled).toBe(false);

    await click("Start session");

    expect(provider.updateProject).toHaveBeenCalledWith(
      "project-1",
      "Protein study",
      {
        ...snapshot.projects[0]!.config,
        task: {
          title: "Verify the selected evidence",
          objective:
            "Run the existing checks and summarize any unsupported claims.",
        },
      },
      expect.objectContaining({ streamEpoch: 1 }),
    );
    expect(provider.validateProject).toHaveBeenCalledWith(
      "project-1",
      expect.objectContaining({ streamEpoch: 2 }),
    );
    expect(provider.submitTask).toHaveBeenCalledWith(
      "project-1",
      expect.objectContaining({ streamEpoch: 2 }),
    );
    expect(
      document.querySelector('[data-testid="session-detail-workspace"]'),
    ).toBeTruthy();
  });

  it("keeps the v1 create-project entry beside the project switcher", async () => {
    const provider = providerFixture(authoritySnapshot());
    root = await render(provider);

    const createProject = document.querySelector<HTMLButtonElement>(
      'button[aria-label="Create project"]',
    );
    expect(createProject).toBeTruthy();
    await act(async () => createProject!.click());

    expect(dialog()?.textContent).toContain("Create science project");
    expect(input("Project name").value).toBe("New research project");
  });

  it("lists every real project and activates the selected one without mixing Sessions", async () => {
    const initial = authoritySnapshot();
    const secondProject = {
      ...initial.projects[0]!,
      project_id: "project-2",
      display_name: "Second protein study",
      active_project_head: {
        ...initial.projects[0]!.active_project_head!,
        project_head_id: "project-head-second",
        project_id: "project-2",
      },
    };
    const secondTask = {
      ...initial.tasks[0]!,
      task_id: "task-2",
      project_id: "project-2",
      admission: {
        ...initial.tasks[0]!.admission,
        task_id: "task-2",
        project_id: "project-2",
        predecessor_project_head: secondProject.active_project_head,
      },
    };
    const snapshot: DesktopProductSnapshotV2 = {
      ...initial,
      projects: [...initial.projects, secondProject] as never,
      tasks: [...initial.tasks, secondTask] as never,
      fixturePresentation: {
        ...initial.fixturePresentation!,
        tasks: {
          ...initial.fixturePresentation!.tasks,
          "task-2": {
            instruction: {
              title: "Second project session",
              objective: "Only belongs to project 2.",
            },
            transcript: [],
            outputFiles: [],
            usedArtifactIds: [],
            producedArtifactIds: [],
          },
        },
      },
    };
    const provider = providerFixture(snapshot);
    provider.activateProject = vi.fn(
      async () => ({ schema_version: "2" }) as never,
    );
    root = await render(provider);

    const switcher = document.querySelector<HTMLSelectElement>(
      "#v2-project-switcher",
    )!;
    expect([...switcher.options].map((option) => option.textContent)).toEqual(
      expect.arrayContaining(["Protein study", "Second protein study"]),
    );

    await act(async () => {
      switcher.value = "project:project-2";
      switcher.dispatchEvent(new Event("change", { bubbles: true }));
    });

    expect(provider.activateProject).toHaveBeenCalledWith(
      "project-2",
      expect.objectContaining({ actionId: expect.any(String) }),
    );
    expect(
      document.querySelector('[data-testid="session-detail-workspace"]'),
    ).toBeFalsy();
  });

  it("renders a supported Core-owned selection resolver as the saved method", async () => {
    const snapshot = authoritySnapshot();
    const project = snapshot.projects[0]!;
    (project.config.evolution.targets as Record<string, unknown>).agent_system =
      {
        enabled: true,
        method: "auto",
        config: { target_path: "AGENTS.md" },
      };
    snapshot.capability!.capabilities.targets = [
      {
        target_id: "agent_system",
        display_name: "Agent system",
        description: "Core-owned agent-system evolution.",
        exposure: "desktop",
        effective_default_method_id: "concrete_agent_system",
        methods: [
          {
            method_id: "concrete_agent_system",
            display_name: "Concrete method",
            default_config_json: "{}",
          },
        ],
        accepted_methods: [
          {
            method_id: "concrete_agent_system",
            implementation_identity_digest: DIGEST,
            support: { overall: "supported" },
          },
        ],
        selection_resolvers: [
          {
            selection_value: "auto",
            display_name: "Automatic",
            description: "Core selects an accepted concrete method.",
            resolved_methods: [
              {
                method_id: "concrete_agent_system",
                implementation_identity_digest: DIGEST,
                support: { overall: "supported" },
              },
            ],
          },
        ],
      },
    ] as never;
    root = await render(providerFixture(snapshot));

    await click("Evolution");

    const method = document.querySelector<HTMLSelectElement>(
      ".v2-target-list select",
    );
    expect(method?.value).toBe("auto");
    expect([...method!.options].map((option) => option.textContent)).toContain(
      "Automatic",
    );
    expect(document.body.textContent).not.toContain("blocks Task admission");
  });

  it("blocks a new task while the successor is not ready and exposes transition recovery", async () => {
    const provider = providerFixture(authoritySnapshot("not_ready"));
    root = await render(provider);

    expect(
      document.querySelector('[data-testid="session-detail-workspace"]'),
    ).toBeTruthy();
    expect(document.body.textContent).not.toContain("Task draft");
    expect(
      [...document.querySelectorAll("button")].some((candidate) =>
        candidate.textContent?.includes("Start session"),
      ),
    ).toBe(false);
    expect(button("Retry successor transition")).toBeTruthy();
    expect(document.body.textContent).toContain("Build successor Project Head");
    expect(document.body.textContent).toContain("Successor state: failed");
    expect(document.body.textContent).toContain("2 of 5 items");
    expect(provider.submitTask).not.toHaveBeenCalled();
  });

  it("renders an active Task through the shared long-operation presentation", async () => {
    const snapshot = authoritySnapshot();
    const runningTask = {
      ...snapshot.tasks[0]!,
      state: "running" as const,
      successor_transition: null,
    };
    const runningSnapshot: DesktopProductSnapshotV2 = {
      ...snapshot,
      tasks: [runningTask],
      transitions: {},
    };
    const loadTaskLogs = vi.fn(async () => ({
      schema_version: "2" as const,
      items: [
        {
          sequence: 1,
          occurred_at: NOW,
          stream: "system" as const,
          message: "Daemon started the managed Task attempt.",
        },
      ],
      next_cursor: null,
      has_more: false,
    }));
    const provider = {
      ...providerFixture(runningSnapshot),
      loadTaskLogs,
    } satisfies DesktopProductProviderV2;

    root = await render(provider);

    expect(document.body.textContent).toContain("Run science Task");
    expect(document.body.textContent).toContain("Task state: running");
    expect(document.body.textContent).toContain(
      "Working — progress is not measurable for this phase",
    );
    expect(loadTaskLogs).toHaveBeenCalledWith("task-1", { limit: 100 });
    expect(document.body.textContent).toContain(
      "Daemon started the managed Task attempt.",
    );
    expect(document.body.textContent).toContain("Task state");
  });

  it("shows and controls Core-owned long operations without a Desktop lifecycle shadow", async () => {
    const operation: OperationV2 = {
      schema_version: "2",
      operation_id: "core-service-restart-1",
      kind: "service_restart",
      status: "running",
      progress_completed: 2,
      progress_total: 4,
      error: null,
      created_at: NOW,
      updated_at: NOW,
      etag: ETAG,
    };
    const cancelCoreOperation = vi.fn(async () => ({
      ...operation,
      status: "cancelled" as const,
      updated_at: "2026-07-23T06:00:01Z",
    }));
    const provider = {
      ...providerFixture(authoritySnapshot()),
      listCoreOperations: () => [operation],
      cancelCoreOperation,
    } satisfies DesktopProductProviderV2;

    root = await render(provider);

    expect(document.body.textContent).toContain("Restart remote service");
    expect(document.body.textContent).toContain("Core status: running");
    expect(document.body.textContent).toContain("2 of 4 items");
    await click("Cancel operation");
    expect(cancelCoreOperation).toHaveBeenCalledWith(
      operation.operation_id,
      expect.objectContaining({ streamEpoch: 1 }),
    );
  });

  it("offers reconciliation for a reserved lifecycle cancellation", async () => {
    const operation = {
      schema_version: "2" as const,
      operation_id: "lifecycle-cancel-ambiguous-1",
      kind: "profile_connect" as const,
      resource: {
        resource_kind: "profile" as const,
        resource_id: "profile-gpu",
      },
      request_sha256: DIGEST,
      status: "running" as const,
      phase: "connecting" as const,
      phase_index: 3,
      phase_total: 17,
      progress: { kind: "indeterminate" as const },
      cancellable: false,
      result: null,
      failure: null,
      log_sequence_high_watermark: 0,
      created_at: NOW,
      started_at: NOW,
      updated_at: NOW,
      finished_at: null,
      etag: ETAG,
    };
    const resumeMutationIntent = vi.fn(async () => {});
    const provider = {
      ...providerFixture(baseSnapshot()),
      listLifecycleOperations: () => [
        {
          operation,
          logs: [],
          droppedBeforeSequence: 0,
          hasOlderLogs: false,
          hasNewerLogs: false,
        },
      ],
      listMutationIntents: () => [
        {
          action_id: "connect-lifecycle-original-ui-0001",
          mutation_kind: "profile_connect" as const,
          resource_scope: "profile:profile-gpu",
          request_sha256: DIGEST,
          authority_sha256: DIGEST,
          provider_stream_instance: "provider-instance-test",
          provider_stream_epoch: 1,
          chain_step: "single" as const,
          accepted_operation_id: operation.operation_id,
          completed_operation_ids: [],
          state: "accepted" as const,
          created_at: NOW,
          updated_at: NOW,
        },
        {
          action_id: "cancel-lifecycle-ambiguous-ui-0001",
          mutation_kind: "lifecycle_cancel" as const,
          resource_scope: `lifecycle_operation:${operation.operation_id}`,
          request_sha256: DIGEST,
          authority_sha256: DIGEST,
          provider_stream_instance: "provider-instance-test",
          provider_stream_epoch: 1,
          chain_step: "single" as const,
          accepted_operation_id: null,
          completed_operation_ids: [],
          state: "reserved" as const,
          created_at: NOW,
          updated_at: NOW,
        },
      ],
      resumeMutationIntent,
    } satisfies DesktopProductProviderV2;

    root = await render(provider);

    await click("Resume / reconcile");
    expect(resumeMutationIntent).toHaveBeenCalledWith(
      "cancel-lifecycle-ambiguous-ui-0001",
    );
  });

  it("keeps diagnostic collection observable as its own Core resource", async () => {
    const snapshot = authoritySnapshot();
    const diagnostic = {
      schema_version: "2" as const,
      diagnostic_id: "diagnostic-system-1",
      scope: "system" as const,
      resource_id: null,
      status: "running" as const,
      artifact_id: null,
      created_at: NOW,
      updated_at: NOW,
      etag: ETAG,
    };
    const createDiagnostic = vi.fn(async () => diagnostic);
    const provider = {
      ...providerFixture(snapshot),
      listDiagnostics: () => [diagnostic],
      createDiagnostic,
    } satisfies DesktopProductProviderV2;

    root = await render(provider);
    await click("System");

    expect(document.body.textContent).toContain("Diagnostic status: running");
    await click("Collect system diagnostics");
    expect(createDiagnostic).toHaveBeenCalledWith(
      { scope: "system", resource_id: null },
      expect.objectContaining({ streamEpoch: 1 }),
    );
  });

  it("loads remote service output and starts idempotent safe-cache cleanup", async () => {
    const base = authoritySnapshot();
    const service = {
      schema_version: "2" as const,
      service_id: "service-daemon-1",
      kind: "daemon" as const,
      status: "ready" as const,
      updated_at: NOW,
      etag: ETAG,
    };
    const snapshot: DesktopProductSnapshotV2 = { ...base, services: [service] };
    const loadServiceLogs = vi.fn(async () => ({
      schema_version: "2" as const,
      items: [
        {
          sequence: 1,
          occurred_at: NOW,
          stream: "stdout" as const,
          message: "Daemon registry is ready",
        },
      ],
      next_cursor: null,
      has_more: false,
    }));
    const cleanupCaches = vi.fn(async () => ({
      schema_version: "2" as const,
      operation_id: "core-cache-cleanup-1",
      kind: "cache_cleanup" as const,
      status: "queued" as const,
      progress_completed: 0,
      progress_total: 0,
      error: null,
      created_at: NOW,
      updated_at: NOW,
      etag: ETAG,
    }));
    const provider = {
      ...providerFixture(snapshot),
      loadServiceLogs,
      cleanupCaches,
    } satisfies DesktopProductProviderV2;

    root = await render(provider);
    await click("System");
    await click("View logs");

    expect(document.body.textContent).toContain("Daemon registry is ready");
    expect(document.body.textContent).toContain("Service output");
    await click("Clean safe caches");
    expect(cleanupCaches).toHaveBeenCalledWith(
      expect.objectContaining({ streamEpoch: 1 }),
    );
  });

  it("creates a project with the release-owned Self-Deployed execution profile", async () => {
    const connected = systemProfile({
      connection_state: "connected",
      core_api_major: 2,
      core_openapi_sha256: DIGEST,
      core_event_schema_sha256: DIGEST,
      core_registry_sha256: DIGEST,
    });
    const snapshot = baseSnapshot({
      profiles: [connected] as never,
      state: {
        ...baseSnapshot().state,
        profiles: [connected] as never,
        active_profile_id: connected.profile_id,
      },
    });
    const createProject = vi.fn(async () => ({
      schema_version: "2" as const,
      operation_id: "project-create-self-deployed-1",
      kind: "project_create" as const,
      resource: {
        resource_kind: "project" as const,
        resource_id: "project-pending-1",
      },
      request_sha256: DIGEST,
      status: "queued" as const,
      phase: "queued" as const,
      phase_index: 1,
      phase_total: 17,
      progress: { kind: "indeterminate" as const },
      cancellable: true,
      result: null,
      failure: null,
      log_sequence_high_watermark: 0,
      created_at: NOW,
      started_at: null,
      updated_at: NOW,
      finished_at: null,
      etag: ETAG,
    }));
    const provider = {
      ...unavailableDesktopProductProviderV2,
      featureFlags: ["system_openssh_profiles"],
      refresh: vi.fn(async () => ({ status: "fresh" as const, snapshot })),
      createProject,
    } satisfies DesktopProductProviderV2;
    root = await render(provider);

    await click("New project");
    await click("Self-Deployed");
    expect(document.body.textContent).toContain("Qwen3 0.6B");
    setTextarea(
      "Task objective",
      "Run this task through the managed local model.",
    );
    await click("Create project");

    expect(createProject).toHaveBeenCalledWith(
      expect.objectContaining({
        config: expect.objectContaining({
          execution: {
            mode: "self-deployed",
            capture_mode: "transcript",
            token_level_metrics_available: false,
            harness_id: "codex",
            model_profile_id: "qwen3-0.6b-v1",
            token_limit: 8_192,
            task_network_allow_internet: true,
          },
        }),
      }),
      expect.objectContaining({ streamEpoch: 1 }),
    );
  });

  it("closes project setup after HTTP 202 while progress and logs stay visible", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-27T08:00:00Z"));
    const connected = systemProfile({
      connection_state: "connected",
      core_api_major: 2,
      core_openapi_sha256: DIGEST,
      core_event_schema_sha256: DIGEST,
      core_registry_sha256: DIGEST,
    });
    const snapshot = baseSnapshot({
      profiles: [connected] as never,
      state: {
        ...baseSnapshot().state,
        profiles: [connected] as never,
        active_profile_id: connected.profile_id,
      },
    });
    const lifecycleState = {
      operation: {
        schema_version: "2",
        operation_id: "project-create-long-1",
        kind: "project_create",
        resource: {
          resource_kind: "project",
          resource_id: "project-pending-1",
        },
        request_sha256: DIGEST,
        status: "running",
        phase: "creating_remote_project",
        phase_index: 13,
        phase_total: 17,
        progress: { kind: "indeterminate" },
        cancellable: true,
        result: null,
        failure: null,
        log_sequence_high_watermark: 2,
        created_at: "2026-07-27T08:00:00Z",
        started_at: "2026-07-27T08:00:00Z",
        updated_at: "2026-07-27T08:00:00Z",
        finished_at: null,
        etag: ETAG,
      },
      logs: [
        {
          schema_version: "2",
          operation_id: "project-create-long-1",
          sequence: 1,
          occurred_at: "2026-07-27T08:00:00Z",
          source: "ssh_stdout",
          text: "Remote project request accepted",
          truncated: false,
        },
        {
          schema_version: "2",
          operation_id: "project-create-long-1",
          sequence: 2,
          occurred_at: "2026-07-27T08:00:00Z",
          source: "daemon_stdout",
          text: "Materializing workspace snapshot",
          truncated: false,
        },
      ],
      droppedBeforeSequence: 0,
      hasOlderLogs: false,
      hasNewerLogs: false,
    } satisfies LifecycleOperationStateV2;
    let operationVisible = false;
    let listener: (() => void) | null = null;
    const createProject = vi.fn(async () => {
      operationVisible = true;
      listener?.();
      return lifecycleState.operation;
    });
    const provider = {
      ...unavailableDesktopProductProviderV2,
      featureFlags: ["system_openssh_profiles"],
      refresh: vi.fn(async () => ({ status: "fresh" as const, snapshot })),
      subscribe: vi.fn((next: () => void) => {
        listener = next;
        return () => {
          listener = null;
        };
      }),
      listLifecycleOperations: () => (operationVisible ? [lifecycleState] : []),
      listMutationIntents: () =>
        operationVisible
          ? [
              {
                action_id: "create-project-long-running-0001",
                mutation_kind: "project_create" as const,
                resource_scope: "project:new:profile-gpu",
                request_sha256: DIGEST,
                authority_sha256: DIGEST,
                provider_stream_instance: "provider-instance-test",
                provider_stream_epoch: 1,
                chain_step: "single" as const,
                accepted_operation_id: "project-create-long-1",
                completed_operation_ids: [],
                state: "accepted" as const,
                created_at: "2026-07-27T08:00:00Z",
                updated_at: "2026-07-27T08:00:00Z",
              },
            ]
          : [],
      createProject,
    } satisfies DesktopProductProviderV2;
    root = await render(provider);

    await click("New project");
    setTextarea(
      "Task objective",
      "Create a reproducible result from this workspace.",
    );
    await click("Create project");
    await act(async () => vi.advanceTimersByTime(16_000));

    expect(createProject).toHaveBeenCalledTimes(1);
    expect(document.body.textContent).toContain(
      "Creating or loading the remote project",
    );
    expect(document.body.textContent).toContain(
      "Remote project request accepted",
    );
    expect(document.body.textContent).toContain(
      "Materializing workspace snapshot",
    );
    expect(document.body.textContent).toContain("Elapsed 16s");
    expect(document.body.textContent).not.toContain(
      "Desktop Local API request timed out",
    );
    expect(document.body.textContent).toContain("Project creation started");
    expect(
      Array.from(document.querySelectorAll("button")).some(
        (candidate) => candidate.textContent?.trim() === "Create project",
      ),
    ).toBe(false);
  });

  it("can cancel native workspace preparation while the lifecycle operation is running", async () => {
    const connected = systemProfile({
      connection_state: "connected",
      core_api_major: 2,
      core_openapi_sha256: DIGEST,
      core_event_schema_sha256: DIGEST,
      core_registry_sha256: DIGEST,
    });
    const snapshot = baseSnapshot({
      profiles: [connected] as never,
      state: {
        ...baseSnapshot().state,
        profiles: [connected] as never,
        active_profile_id: connected.profile_id,
      },
    });
    const selectNativeWorkspace = vi.fn(
      async () => new Promise<never>(() => {}),
    );
    const cancelNativeWorkspace = vi.fn(async () => {});
    const provider = {
      ...unavailableDesktopProductProviderV2,
      featureFlags: ["system_openssh_profiles"],
      refresh: vi.fn(async () => ({ status: "fresh" as const, snapshot })),
      selectNativeWorkspace,
      cancelNativeWorkspace,
      settleNativeWorkspace: vi.fn(async () => {}),
    } satisfies DesktopProductProviderV2;
    root = await render(provider);

    await click("New project");
    setTextarea("Task objective", "Prepare a native workspace safely.");
    await click("Choose folder snapshot");
    expect(selectNativeWorkspace).toHaveBeenCalledTimes(1);
    await click("Cancel");

    expect(cancelNativeWorkspace).toHaveBeenCalledWith(expect.any(String));
  });
});

async function render(provider: DesktopProductProviderV2): Promise<Root> {
  const container = document.querySelector("#root");
  if (!container) throw new Error("root is missing");
  const root = createRoot(container);
  await act(async () => {
    root.render(<DesktopProductApp provider={provider} />);
    await Promise.resolve();
    await Promise.resolve();
  });
  return root;
}

function button(label: string): HTMLButtonElement {
  const match = [
    ...document.querySelectorAll<HTMLButtonElement>("button"),
  ].find((candidate) => candidate.textContent?.trim().includes(label));
  if (!match) throw new Error(`button not found: ${label}`);
  return match;
}

async function click(label: string): Promise<void> {
  await act(async () => {
    button(label).click();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

function dialog(): HTMLElement | null {
  return document.querySelector('[role="dialog"]');
}

function setInput(label: string, value: string): void {
  const labels = [...document.querySelectorAll<HTMLLabelElement>("label")];
  const owner = labels.find((candidate) =>
    candidate.textContent?.includes(label),
  );
  const input = owner?.querySelector<HTMLInputElement>("input");
  if (!input) throw new Error(`input not found: ${label}`);
  act(() => {
    const setter = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )?.set;
    setter?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

function setTextarea(label: string, value: string): void {
  const labels = [...document.querySelectorAll<HTMLLabelElement>("label")];
  const owner = labels.find((candidate) =>
    candidate.textContent?.includes(label),
  );
  const input = owner?.querySelector<HTMLTextAreaElement>("textarea");
  if (!input) throw new Error(`textarea not found: ${label}`);
  act(() => {
    const setter = Object.getOwnPropertyDescriptor(
      HTMLTextAreaElement.prototype,
      "value",
    )?.set;
    setter?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

function input(label: string): HTMLInputElement {
  const owner = [...document.querySelectorAll<HTMLLabelElement>("label")].find(
    (candidate) => candidate.textContent?.includes(label),
  );
  const control = owner?.querySelector<HTMLInputElement>("input");
  if (!control) throw new Error(`input not found: ${label}`);
  return control;
}

function textarea(label: string): HTMLTextAreaElement {
  const owner = [...document.querySelectorAll<HTMLLabelElement>("label")].find(
    (candidate) => candidate.textContent?.includes(label),
  );
  const control = owner?.querySelector<HTMLTextAreaElement>("textarea");
  if (!control) throw new Error(`textarea not found: ${label}`);
  return control;
}
