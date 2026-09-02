// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DesktopProductSnapshotV2, WorkspaceFileUploadV2 } from "./providerV2";
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
    runtimePresentation: {
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
    subscribe: vi.fn((
      _listener: Parameters<DesktopProductProviderV2["subscribe"]>[0],
    ) => () => undefined),
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
    deleteProfile: vi.fn(async (profileId: string) => {
      const profiles = current.profiles.filter(
        (profile) => profile.profile_id !== profileId,
      );
      current = baseSnapshot({
        ...current,
        profiles: profiles as never,
        state: { ...current.state, profiles: profiles as never },
        stream: {
          status: "fresh",
          epoch: current.stream.epoch + 1,
          lastEventId: null,
        },
      });
    }),
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
      async (projectId: string) => {
        const validation = {
          schema_version: "2",
          project_id: projectId,
          valid: true,
          registry_sha256: DIGEST,
          checks: [],
          validated_at: NOW,
        } as never;
        // Formal validation advances the authoritative Desktop event stream.
        // A caller must refresh before binding the subsequent Task admission.
        current = {
          ...current,
          validation,
          stream: {
            status: "fresh",
            epoch: current.stream.epoch + 1,
            lastEventId: null,
          },
        };
        return validation;
      },
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
        current.runtimePresentation?.artifacts[artifactId]
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
    window.sessionStorage.clear();
    window.localStorage.removeItem("openevo.desktop.layout.project-pane-width");
    window.localStorage.removeItem("openevo.desktop.layout.session-pane-width");
    window.localStorage.removeItem("openevo.desktop.layout.session-inspector-width");
    window.localStorage.removeItem("openevo.desktop.navigation.project-session-selections");
    window.localStorage.removeItem("openevo.desktop.navigation.project-session-scrolls");
    window.history.replaceState(null, "", window.location.pathname);
    (
      globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
    ).IS_REACT_ACT_ENVIRONMENT = true;
  });

  afterEach(async () => {
    if (root) await act(async () => root?.unmount());
    root = null;
    vi.useRealTimers();
    document.body.innerHTML = "";
    window.sessionStorage.clear();
    window.history.replaceState(null, "", window.location.pathname);
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("shows a passive startup screen while the workspace loads", async () => {
    const provider = {
      ...unavailableDesktopProductProviderV2,
      refresh: vi.fn(() => new Promise<never>(() => undefined)),
    } satisfies DesktopProductProviderV2;

    root = await render(provider);

    expect(document.body.textContent).toContain("EvoLab is starting.");
    expect(document.body.textContent).toContain("Preparing your workspace");
    expect(document.querySelector(".initial-launch-title-shimmer")).toBeTruthy();
    expect(document.querySelector(".initial-launch-preparing")).toBeTruthy();
    expect(document.body.textContent).not.toContain("Add remote workspace");
    expect(document.body.textContent).not.toContain("STARTING EVOLAB");
    expect(document.body.textContent).not.toContain("Agent evolution workspace");
    expect(document.querySelector(".product-sidebar")).toBeNull();
    expect(document.querySelector(".product-topbar")).toBeNull();
  });

  it("explains a lost local API connection with a compact actionable status", async () => {
    let refreshCount = 0;
    let listener: Parameters<DesktopProductProviderV2["subscribe"]>[0] | null = null;
    const provider = {
      ...providerFixture(authoritySnapshot()),
      refresh: vi.fn(async () => {
        refreshCount += 1;
        if (refreshCount > 1) throw new Error("Desktop Local API request failed");
        return { status: "fresh" as const, snapshot: authoritySnapshot() };
      }),
      subscribe: vi.fn((next: Parameters<DesktopProductProviderV2["subscribe"]>[0]) => {
        listener = next;
        return () => { listener = null; };
      }),
    } satisfies DesktopProductProviderV2;
    root = await render(provider);

    await act(async () => {
      listener?.({ kind: "snapshot_changed" });
      await Promise.resolve();
    });
    await vi.waitFor(() => expect(document.body.textContent).toContain("Connection to EvoLab was lost"));

    expect(document.body.textContent).toContain("cannot reach the local API");
    expect(document.body.textContent).toContain("Keep the `openevo webui` launcher terminal running");
    expect(document.body.textContent).not.toContain("Refresh failed");
    expect(document.querySelector(".product-feedback-stack .v2-notice")).toBeTruthy();
    expect([...document.querySelectorAll("button")].some((button) => button.textContent?.trim() === "Retry")).toBe(true);
  });

  it("uses configured OpenSSH aliases in the localhost browser host", async () => {
    window.history.replaceState(
      null,
      "",
      `${window.location.pathname}#browser-bootstrap=${"a1".repeat(32)}`,
    );
    root = await render(providerFixture(baseSnapshot()));

    await click("Add remote workspace");

    expect(dialog()?.textContent).toContain("Configured SSH host");
    expect(dialog()?.textContent).toContain("gpu-lab");
    const fieldLabels = [...(dialog()?.querySelectorAll("label") ?? [])]
      .map((label) => label.textContent ?? "")
      .join(" ");
    expect(fieldLabels).not.toMatch(
      /server address|user name|username|ssh port|private key|password/i,
    );
  });

  it("opens configured-host setup immediately and never renders manual connection fields", async () => {
    const provider = providerFixture(baseSnapshot());
    root = await render(provider);

    await click("Add remote workspace");

    expect(dialog()?.textContent).toContain("Configured SSH host");
    expect(dialog()?.textContent).toContain("gpu-lab");
    expect(dialog()?.textContent).toContain(
      "Some SSH configuration entries could not be listed.",
    );
    const fieldLabels = [...(dialog()?.querySelectorAll("label") ?? [])]
      .map((label) => label.textContent ?? "")
      .join(" ");
    expect(fieldLabels).not.toMatch(
      /server address|user name|username|ssh port|private key|password/i,
    );
    expect(
      [...(dialog()?.querySelectorAll("button") ?? [])].some((candidate) =>
        candidate.textContent?.includes("Use another SSH alias"),
      ),
    ).toBe(false);
  });

  it("shows an explicit empty project state without demo authority", async () => {
    root = await render(providerFixture(baseSnapshot()));

    expect(document.body.textContent).toContain("Active Project Head");
    expect(document.body.textContent).toContain("No project yet");
    expect(document.body.textContent).not.toContain("Demo Project Head");
  });

  it("uses project files and Sessions as persistent sidebars around the central workspace", async () => {
    const base = authoritySnapshot();
    const snapshot: DesktopProductSnapshotV2 = {
      ...base,
      runtimePresentation: {
        ...base.runtimePresentation!,
        workspaces: {
          "project-1": {
            entries: [
              {
                path: "results",
                kind: "directory",
                byteSize: 0,
                contentSha256: null,
                mediaType: null,
                content: null,
                modifiedAt: NOW,
              },
              {
                path: "results/report.md",
                kind: "file",
                byteSize: 42,
                contentSha256: DIGEST,
                mediaType: "text/markdown",
                content: "# Remote report\n\nA project workspace file.",
                modifiedAt: NOW,
              },
            ],
            truncated: false,
          },
        },
      },
    };
    root = await render(providerFixture(snapshot));

    expect(document.querySelector(".product-activitybar")).toBeTruthy();
    expect(document.querySelector(".project-explorer")).toBeTruthy();
    expect(document.querySelector(".session-explorer")).toBeTruthy();
    expect(document.querySelector(".product-topbar")).toBeNull();
    expect(document.querySelector("button.activitybar-settings")).toBeTruthy();
    const projectResizer = document.querySelector<HTMLElement>('[role="separator"][aria-label="Resize Project pane"]');
    expect(projectResizer).toBeTruthy();
    await act(async () => projectResizer!.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })));
    expect(document.querySelector<HTMLElement>(".product-v2-shell")?.style.getPropertyValue("--project-pane-width")).toBe("260px");
    expect(window.localStorage.getItem("openevo.desktop.layout.project-pane-width")).toBe("260");
    expect(document.body.textContent).toContain("report.md");
    expect(document.body.textContent).toContain("Review evidence");
    const reportTreeItem = document.querySelector<HTMLElement>('[role="treeitem"][title="results/report.md"]');
    expect(reportTreeItem?.getAttribute("aria-level")).toBe("2");
    expect(reportTreeItem?.querySelector(".explorer-file-type-icon.markdown")).toBeTruthy();

    await click("report.md");
    expect(document.querySelector('[data-testid="project-file-workspace"]')).toBeTruthy();
    expect(document.body.textContent).toContain("A project workspace file.");

    await click("New Session");
    expect(document.querySelector('[data-testid="project-file-workspace"]')).toBeNull();
    expect(document.querySelector('[data-testid="session-composer"]')).toBeTruthy();
    expect(input("Task title").value).toBe("");
    expect(textarea("Task instructions").value).toBe("");

    await click("Review evidence");
    expect(document.querySelector('[data-testid="session-detail-workspace"]')).toBeTruthy();
    expect(document.querySelector('[aria-label="Session inspector"]')).toBeTruthy();
    expect(document.querySelector('[role="separator"][aria-label="Resize Session inspector"]')).toBeTruthy();
    expect(document.body.textContent).toContain("Review the evidence and update the workspace.");
    expect(document.body.textContent).toContain("I checked the evidence table");
    expect(document.body.textContent).toContain("Applied Evolution Context");
    expect(document.body.textContent).toContain("Previous research memory");

    await click("Evolution");
    expect(document.body.textContent).toContain("Improve future Sessions");
    expect(document.querySelector(".project-explorer")).toBeTruthy();
    expect(document.querySelector(".session-explorer")).toBeTruthy();
  });

  it("offers confirmed deletion actions for Projects, terminal Sessions, and workspace files", async () => {
    const base = authoritySnapshot();
    const snapshot: DesktopProductSnapshotV2 = {
      ...base,
      runtimePresentation: {
        ...base.runtimePresentation!,
        workspaces: {
          "project-1": {
            entries: [{
              path: "results/report.md",
              kind: "file",
              byteSize: 42,
              contentSha256: DIGEST,
              mediaType: "text/markdown",
              content: "# Report",
              modifiedAt: NOW,
            }],
            truncated: false,
          },
        },
      },
    };
    const deleteProject = vi.fn(async () => undefined);
    const deleteTask = vi.fn(async () => undefined);
    const deleteWorkspaceFile = vi.fn(async () => undefined);
    const provider = {
      ...providerFixture(snapshot),
      deleteProject,
      deleteTask,
      deleteWorkspaceFile,
    } satisfies DesktopProductProviderV2;
    const browserConfirm = vi.fn(() => true);
    vi.stubGlobal("confirm", browserConfirm);
    root = await render(provider);

    const confirmDialog = () => document.querySelector<HTMLElement>('[role="alertdialog"]');
    const confirmInDialog = async (label: string): Promise<void> => {
      const confirmation = [...(confirmDialog()?.querySelectorAll<HTMLButtonElement>("button") ?? [])]
        .find((candidate) => candidate.textContent?.trim() === label);
      if (!confirmation) throw new Error(`confirmation button not found: ${label}`);
      await act(async () => {
        confirmation.click();
        await Promise.resolve();
        await Promise.resolve();
      });
    };

    await act(async () => {
      document.querySelector<HTMLButtonElement>('[aria-label="Delete results/report.md"]')?.click();
      await Promise.resolve();
    });
    expect(confirmDialog()?.textContent).toContain("Delete file?");
    expect(confirmDialog()?.textContent).toContain("results/report.md");
    expect(deleteWorkspaceFile).not.toHaveBeenCalled();
    await confirmInDialog("Delete file");
    await vi.waitFor(() => expect(deleteWorkspaceFile).toHaveBeenCalledWith(
      "project-1",
      "results/report.md",
      expect.objectContaining({ actionId: expect.any(String) }),
    ));
    expect(document.querySelector('[aria-label="Delete results/report.md"]')).toBeNull();

    await act(async () => {
      document.querySelector<HTMLButtonElement>('[aria-label="More actions for Review evidence"]')?.click();
      await Promise.resolve();
    });
    await click("Delete session");
    expect(confirmDialog()?.textContent).toContain("Delete Session?");
    expect(confirmDialog()?.textContent).toContain("Review evidence");
    expect(deleteTask).not.toHaveBeenCalled();
    await confirmInDialog("Delete Session");
    await vi.waitFor(() => expect(deleteTask).toHaveBeenCalledWith(
      "task-1",
      expect.objectContaining({ actionId: expect.any(String) }),
    ));
    expect(document.querySelector('[aria-label="More actions for Review evidence"]')).toBeNull();

    await act(async () => {
      document.querySelector<HTMLButtonElement>("#v2-project-switcher")?.click();
      await Promise.resolve();
    });
    await act(async () => {
      document.querySelector<HTMLButtonElement>('[aria-label="More actions for Protein study"]')?.click();
      await Promise.resolve();
    });
    await click("Delete project");
    expect(confirmDialog()?.textContent).toContain("Delete Project?");
    expect(confirmDialog()?.textContent).toContain("Protein study");
    expect(deleteProject).not.toHaveBeenCalled();
    await confirmInDialog("Delete Project");
    await vi.waitFor(() => expect(deleteProject).toHaveBeenCalledWith(
      "project-1",
      expect.objectContaining({ actionId: expect.any(String) }),
    ));
    expect(document.body.textContent).toContain("No project yet");
    expect(browserConfirm).not.toHaveBeenCalled();
  });

  it("uploads a selected folder while preserving its relative file paths", async () => {
    const uploadWorkspaceFile = vi.fn(async () => undefined);
    const provider = {
      ...providerFixture(authoritySnapshot()),
      uploadWorkspaceFile,
      downloadWorkspaceFile: vi.fn(async () => ({
        fileName: "unused.txt",
        mediaType: "text/plain",
        data: new Blob(),
      })),
    } satisfies DesktopProductProviderV2;
    root = await render(provider);

    const uploadButton = document.querySelector<HTMLButtonElement>('[aria-label="Upload to workspace"]');
    expect(uploadButton).toBeTruthy();
    expect(document.querySelector('[role="menu"]')).toBeNull();
    await act(async () => uploadButton!.click());
    const uploadOptions = [...document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')];
    expect(uploadOptions.map((option) => option.textContent?.trim())).toEqual(["Upload files", "Upload folder"]);
    await act(async () => uploadOptions[1]!.click());
    expect(document.querySelector('[role="menu"]')).toBeNull();

    const folderInput = document.querySelector<HTMLInputElement>('[aria-label="Choose folder to upload"]');
    expect(folderInput).toBeTruthy();
    expect(folderInput?.hasAttribute("webkitdirectory")).toBe(true);
    const pythonFile = new File(["print('OpenEvo')\n"], "main.py", { type: "text/x-python" });
    const markdownFile = new File(["# Notes\n"], "notes.md", { type: "text/markdown" });
    Object.defineProperty(pythonFile, "webkitRelativePath", { value: "research/src/main.py" });
    Object.defineProperty(markdownFile, "webkitRelativePath", { value: "research/docs/notes.md" });
    Object.defineProperty(folderInput!, "files", { configurable: true, value: [pythonFile, markdownFile] });

    await act(async () => {
      folderInput!.dispatchEvent(new Event("change", { bubbles: true }));
      await Promise.resolve();
    });
    await vi.waitFor(() => expect(uploadWorkspaceFile).toHaveBeenCalledTimes(2));
    expect(uploadWorkspaceFile).toHaveBeenNthCalledWith(
      1,
      "project-1",
      expect.objectContaining({ path: "research/src/main.py", data: pythonFile, overwrite: false }),
      expect.anything(),
    );
    expect(uploadWorkspaceFile).toHaveBeenNthCalledWith(
      2,
      "project-1",
      expect.objectContaining({ path: "research/docs/notes.md", data: markdownFile, overwrite: false }),
      expect.anything(),
    );
  });

  it("shows a selected file in the Workspace tree with live upload progress", async () => {
    let finishUpload: (() => void) | undefined;
    const uploadWorkspaceFile = vi.fn(async (
      _projectId: string,
      upload: WorkspaceFileUploadV2,
    ) => new Promise<void>((resolve) => {
      finishUpload = resolve;
      upload.onProgress?.(37);
    }));
    const provider = {
      ...providerFixture(authoritySnapshot()),
      uploadWorkspaceFile,
      downloadWorkspaceFile: vi.fn(async () => ({
        fileName: "unused.txt",
        mediaType: "text/plain",
        data: new Blob(),
      })),
    } satisfies DesktopProductProviderV2;
    root = await render(provider);

    const uploadButton = document.querySelector<HTMLButtonElement>('[aria-label="Upload to workspace"]')!;
    await act(async () => uploadButton.click());
    const uploadFilesOption = [...document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')][0]!;
    await act(async () => uploadFilesOption.click());
    const fileInput = document.querySelector<HTMLInputElement>('[aria-label="Choose files to upload"]')!;
    const file = new File(["a,b\n1,2\n"], "large-dataset.csv", { type: "text/csv" });
    Object.defineProperty(fileInput, "files", { configurable: true, value: [file] });

    await act(async () => {
      fileInput.dispatchEvent(new Event("change", { bubbles: true }));
      await Promise.resolve();
    });

    await vi.waitFor(() => expect(uploadWorkspaceFile).toHaveBeenCalledTimes(1));
    const pendingTreeItem = document.querySelector<HTMLElement>('[role="treeitem"][title="large-dataset.csv"]');
    expect(pendingTreeItem).toBeTruthy();
    expect(pendingTreeItem?.textContent).toContain("37% uploaded");
    expect(pendingTreeItem?.getAttribute("aria-busy")).toBe("true");
    expect(pendingTreeItem?.querySelector('[role="progressbar"]')?.getAttribute("aria-valuenow")).toBe("37");

    await act(async () => {
      finishUpload?.();
      await Promise.resolve();
    });
    const completedProgress = document.querySelector<HTMLElement>('[aria-label="Uploading large-dataset.csv"]');
    expect(completedProgress?.getAttribute("aria-valuenow")).toBe("100");
    expect(document.querySelector('[role="treeitem"][title="large-dataset.csv"]')?.textContent)
      .toContain("Finishing upload");
    await vi.waitFor(
      () => expect(document.querySelector('[aria-label="Uploading large-dataset.csv"]')).toBeNull(),
      { timeout: 2_000 },
    );
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

  it("reuses an existing profile for the same SSH alias", async () => {
    const existing = systemProfile({
      profile_id: "profile-existing",
      connection_state: "disconnected",
    });
    const snapshot = baseSnapshot({
      profiles: [existing] as never,
      state: { ...baseSnapshot().state, profiles: [existing] as never },
    });
    const provider = providerFixture(snapshot);
    root = await render(provider);
    await click("Add remote workspace");
    await click("Save and connect");

    expect(provider.createProfile).not.toHaveBeenCalled();
    expect(provider.connectProfile).toHaveBeenCalledWith(
      "profile-existing",
      expect.objectContaining({ streamEpoch: 1 }),
    );
  });

  it("shows one saved workspace per SSH alias and hides its rebound Preview record", async () => {
    const stale = systemProfile({
      profile_id: "profile-stale-alias",
      display_name: "GPU lab",
      updated_at: "2026-07-23T05:00:00Z",
    });
    const connected = systemProfile({
      profile_id: "profile-connected-alias",
      display_name: "GPU lab",
      connection_state: "connected",
      updated_at: "2026-07-23T06:00:01Z",
    });
    const legacy = {
      schema_version: "2",
      profile_kind: "legacy_explicit",
      profile_id: "legacy-profile-rebound",
      display_name: "GPU lab",
      connectable: false,
      migration_state: "rebind_required",
      created_at: NOW,
      updated_at: NOW,
      etag: ETAG,
    } as const;
    const profiles = [stale, legacy, connected] as never;
    const snapshot = baseSnapshot({
      profiles,
      state: { ...baseSnapshot().state, profiles },
    });
    const provider = providerFixture(snapshot);
    root = await render(provider);
    const settings = document.querySelector<HTMLButtonElement>('button[aria-label="Remote workspace settings"]');
    expect(settings).toBeTruthy();
    await act(async () => {
      settings!.click();
      await Promise.resolve();
    });

    expect(dialog()?.querySelectorAll(".v2-profile-card")).toHaveLength(1);
    expect(dialog()?.textContent).toContain("connected");
    expect(dialog()?.textContent).not.toContain("Retained Preview profile");
  });

  it("lets the user remove a disconnected saved workspace", async () => {
    const existing = systemProfile({
      profile_id: "profile-stale",
      connection_state: "disconnected",
    });
    const snapshot = baseSnapshot({
      profiles: [existing] as never,
      state: { ...baseSnapshot().state, profiles: [existing] as never },
    });
    const provider = providerFixture(snapshot);
    root = await render(provider);
    await click("Add remote workspace");
    await click("Remove");

    expect(provider.deleteProfile).toHaveBeenCalledWith(
      "profile-stale",
      expect.objectContaining({ streamEpoch: 1 }),
    );
  });

  it("explains how to configure SSH when no literal alias is available", async () => {
    const snapshot = baseSnapshot({
      catalog: { ...baseSnapshot().catalog, hosts: [], warnings: [] },
    });
    const provider = providerFixture(snapshot);
    root = await render(provider);
    await click("Add remote workspace");

    expect(dialog()?.textContent).toContain("No usable SSH aliases were found");
    expect(dialog()?.textContent).toContain("Host gpu-lab");
    const save = button("Save and connect") as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    expect(provider.createProfile).not.toHaveBeenCalled();
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

  it("keeps internal execution identities out of the Session inspector", async () => {
    root = await render(providerFixture(authoritySnapshot()));

    await click("Review evidence");

    expect(document.body.textContent).toContain("Project Head 7");
    expect(document.body.textContent).not.toContain("Technical details");
    expect(document.body.textContent).not.toContain("task-admission-1");
    expect(document.body.textContent).not.toContain("attempt-2");
    expect(document.body.textContent).not.toContain("project-head-7");
    expect(document.body.textContent).not.toContain("evolution-revision-1");
    expect(document.body.textContent).not.toContain("runtime-context-1");
    expect(document.body.textContent).not.toContain("effective-execution-1");
    expect(document.body.textContent).not.toContain("successor-transition-8");
  });

  it("opens one Task as a result detail with transcript, files, artifacts, and transition", async () => {
    root = await render(providerFixture(authoritySnapshot()));

    await click("Review evidence");

    expect(
      document.querySelector('[data-testid="session-detail-workspace"]'),
    ).toBeTruthy();
    expect(document.body.textContent).toContain("Review evidence");
    expect(document.body.textContent).toContain("I checked the evidence table");
    expect(document.body.textContent).toContain("results/evidence-review.md");
    expect(document.body.textContent).toContain("Evolution produced");
    expect(document.body.textContent).not.toContain("Conversation");
    const chatCanvas = document.querySelector('[aria-label="Session conversation"]');
    expect(chatCanvas).toBeTruthy();
    expect(chatCanvas?.querySelector(".v2-session-module-heading")).toBeNull();
    expect(chatCanvas?.querySelector("article.user")?.textContent).toContain("Review the evidence");
    expect(chatCanvas?.querySelector("article.agent")?.textContent).toContain("I checked the evidence table");
    expect(document.body.textContent).toContain("Output Files");
    expect(document.body.textContent).not.toContain("Workspace Changes");
    expect(document.body.textContent).toContain("Applied Evolution Context");
    expect(document.body.textContent).toContain("Available for Evolution");
    expect(document.body.textContent).toContain("Project Head 7");
    const sessionInspector = document.querySelector(".session-inspector-pane");
    expect(sessionInspector?.textContent).not.toContain("Baseline pinned when this Session started");
    expect(sessionInspector?.textContent).not.toContain("Applied. The real");
    expect(document.body.textContent).not.toContain("Technical details");
    expect(document.body.textContent).toContain("Memory · Update");
    expect(document.body.textContent).toContain("Skill · Update");
    expect(document.body.textContent).toContain("Agent system · Update");
    expect(document.body.textContent).not.toContain("artifact-memory-2");
    expect(document.body.textContent).toContain("Previous research memory");
    expect(document.body.textContent).toContain("Previous evidence skill");
    expect(document.body.textContent).not.toContain("authoritative · closed");
    expect(document.body.textContent).not.toContain("superseded");
    expect(document.body.textContent).not.toContain("successor-transition-8");
    expect(
      [...document.querySelectorAll("[data-session-priority]")].map((node) =>
        node.getAttribute("data-session-priority"),
      ),
    ).toEqual(["conversation", "outputs", "context", "evolution"]);

    await click("Project Head 7");
    expect(
      document.querySelector('[data-testid="project-head-inspector"]'),
    ).toBeTruthy();
    expect(document.body.textContent).toContain("Included evolution context");
    expect(document.body.textContent).toContain("2 artifacts");
    expect(document.body.textContent).toContain("2 files");
    expect(document.body.textContent).toContain("Codex subscription");
    expect(document.body.textContent).not.toContain("workspace-snapshot-1");
    expect(document.body.textContent).not.toContain(DIGEST);
    await click("Session details");

    await click("Memory · Update");
    expect(
      document.querySelector('[data-testid="session-result-inspector"]'),
    ).toBeTruthy();
    expect(document.body.textContent).toContain(
      "Compared with artifact-memory-1",
    );
    expect(document.body.textContent).toContain(
      "Mark every unsupported conclusion as a hypothesis",
    );

    await click("Session details");
    await click("results/evidence-review.md");
    expect(document.body.textContent).toContain(
      "workspace-before-session/results/evidence-review.md",
    );
    expect(document.body.textContent).toContain("The mechanism is proven");
    await click("Current content");
    expect(document.body.textContent).toContain(
      "Unsupported conclusions are marked as hypotheses",
    );
    expect(document.querySelector('[data-testid="session-composer"]')).toBeFalsy();
    expect(document.querySelector(".session-explorer")).toBeTruthy();

    await click("New Session");

    expect(
      document.querySelector('[data-testid="session-detail-workspace"]'),
    ).toBeFalsy();
    expect(document.querySelector('[data-testid="session-composer"]')).toBeTruthy();
    expect(document.querySelector(".session-explorer-list")).toBeTruthy();
    expect(input("Task title").value).toBe("");
    expect(textarea("Task instructions").value).toBe("");
  });

  it("browses memory, skill, and agent-system previews and their changes", async () => {
    const provider = providerFixture(authoritySnapshot());
    root = await render(provider);
    await click("Evolution");

    expect(document.body.textContent).toContain("Improve future Sessions");
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

    expect(input("Task title").value).toBe("");
    expect(textarea("Task instructions").value).toBe("");
    expect(button("Start session").disabled).toBe(true);
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
      expect.objectContaining({ streamEpoch: 3 }),
      expect.objectContaining({ project_head_id: "project-head-7", generation: 7 }),
    );
    expect(
      document.querySelector('[data-testid="session-detail-workspace"]'),
    ).toBeTruthy();
  });

  it("uploads an image pasted into the Session composer and references it in the task", async () => {
    const snapshot = authoritySnapshot();
    const uploadWorkspaceFile = vi.fn(async (
      _projectId: string,
      _upload: WorkspaceFileUploadV2,
    ) => undefined);
    const provider = {
      ...providerFixture(snapshot),
      uploadWorkspaceFile,
    } satisfies DesktopProductProviderV2;
    root = await render(provider);

    setInput("Task title", "Inspect pasted screenshot");
    setTextarea("Task instructions", "Explain what is visible in this screenshot.");
    const screenshot = new File(["png-image"], "screenshot.png", { type: "image/png" });
    const paste = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(paste, "clipboardData", { value: { files: [screenshot] } });
    await act(async () => {
      textarea("Task instructions").dispatchEvent(paste);
      await Promise.resolve();
    });

    expect(document.querySelector('[aria-label="Session attachments"]')).toBeTruthy();
    expect(document.body.textContent).toContain("screenshot.png");
    let resumeRefresh = (): void => undefined;
    provider.refresh.mockImplementationOnce(async () => {
      await new Promise<void>((resolve) => { resumeRefresh = resolve; });
      return { status: "fresh" as const, snapshot };
    });
    await click("Start session");

    expect(textarea("Task instructions").value).toBe("Explain what is visible in this screenshot.");
    expect(document.body.textContent).not.toContain("Attached files are available in the project workspace");
    expect(document.body.textContent).not.toContain("session-attachments/");
    await act(async () => {
      resumeRefresh();
      await Promise.resolve();
    });

    await vi.waitFor(() => expect(uploadWorkspaceFile).toHaveBeenCalledTimes(1));
    const upload = uploadWorkspaceFile.mock.calls[0]![1];
    expect(upload).toMatchObject({
      data: screenshot,
      mediaType: "image/png",
      overwrite: true,
    });
    expect(upload.path).toMatch(/^session-attachments\/attachment-[a-z0-9-]+-image\.png$/);
    expect(provider.updateProject).toHaveBeenCalledWith(
      "project-1",
      "Protein study",
      expect.objectContaining({
        task: expect.objectContaining({
          objective: expect.stringContaining(upload.path),
        }),
      }),
      expect.anything(),
    );
  });

  it("shows live attachment upload progress while a Session is starting", async () => {
    const snapshot = authoritySnapshot();
    let finishUpload = (): void => undefined;
    const uploadWorkspaceFile = vi.fn(async (
      _projectId: string,
      upload: WorkspaceFileUploadV2,
    ) => {
      upload.onProgress?.(37);
      await new Promise<void>((resolve) => { finishUpload = resolve; });
    });
    const provider = {
      ...providerFixture(snapshot),
      uploadWorkspaceFile,
    } satisfies DesktopProductProviderV2;
    root = await render(provider);

    setInput("Task title", "Inspect upload progress");
    setTextarea("Task instructions", "Explain this image.");
    const screenshot = new File(["png-image"], "progress.png", { type: "image/png" });
    const paste = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(paste, "clipboardData", { value: { files: [screenshot] } });
    await act(async () => {
      textarea("Task instructions").dispatchEvent(paste);
      await Promise.resolve();
    });
    await click("Start session");

    await vi.waitFor(() => expect(document.body.textContent).toContain("Uploading attachment 1 of 1"));
    expect(document.body.textContent).toContain("37% uploaded");
    expect(document.querySelector('[role="progressbar"]')?.getAttribute("aria-valuenow")).toBe("37");

    await act(async () => {
      finishUpload();
      await Promise.resolve();
    });
    await vi.waitFor(() => expect(provider.submitTask).toHaveBeenCalledTimes(1));
  });

  it("opens the attachment menu and uploads a selected file with the Session", async () => {
    const snapshot = authoritySnapshot();
    const uploadWorkspaceFile = vi.fn(async (
      _projectId: string,
      _upload: WorkspaceFileUploadV2,
    ) => undefined);
    const provider = {
      ...providerFixture(snapshot),
      uploadWorkspaceFile,
    } satisfies DesktopProductProviderV2;
    root = await render(provider);

    const trigger = document.querySelector<HTMLButtonElement>('[aria-label="Add attachment"]')!;
    await act(async () => trigger.click());
    const menu = document.querySelector('[role="menu"][aria-label="Add to Session"]');
    expect(menu?.textContent).toContain("Upload images");
    expect(menu?.textContent).toContain("Upload files");
    await click("Upload files");

    const input = document.querySelector<HTMLInputElement>('[aria-label="Choose files for Session"]')!;
    const report = new File(["finding,value\nanswer,42\n"], "evidence.csv", { type: "text/csv" });
    Object.defineProperty(input, "files", { configurable: true, value: [report] });
    await act(async () => {
      input.dispatchEvent(new Event("change", { bubbles: true }));
      await Promise.resolve();
    });
    expect(document.querySelector('[aria-label="Session attachments"]')?.textContent).toContain("evidence.csv");

    setInput("Task title", "Review uploaded evidence");
    setTextarea("Task instructions", "Summarize the attached table.");
    await click("Start session");

    await vi.waitFor(() => expect(uploadWorkspaceFile).toHaveBeenCalledTimes(1));
    const upload = uploadWorkspaceFile.mock.calls[0]![1];
    expect(upload).toMatchObject({ data: report, mediaType: "text/csv", overwrite: true });
    expect(upload.path).toMatch(/^session-attachments\/attachment-[a-z0-9-]+-evidence\.csv$/);
    expect(provider.updateProject).toHaveBeenCalledWith(
      "project-1",
      "Protein study",
      expect.objectContaining({
        task: expect.objectContaining({ objective: expect.stringContaining(upload.path) }),
      }),
      expect.anything(),
    );
  });

  it("refreshes remote authority once after uploading all Session attachments", async () => {
    const snapshot = authoritySnapshot();
    const uploadWorkspaceFile = vi.fn(async () => undefined);
    const provider = {
      ...providerFixture(snapshot),
      uploadWorkspaceFile,
    } satisfies DesktopProductProviderV2;
    root = await render(provider);
    const initialRefreshCount = provider.refresh.mock.calls.length;

    await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="Add attachment"]')!.click());
    await click("Upload files");
    const inputElement = document.querySelector<HTMLInputElement>('[aria-label="Choose files for Session"]')!;
    const first = new File(["one"], "one.txt", { type: "text/plain" });
    const second = new File(["two"], "two.txt", { type: "text/plain" });
    Object.defineProperty(inputElement, "files", { configurable: true, value: [first, second] });
    await act(async () => {
      inputElement.dispatchEvent(new Event("change", { bubbles: true }));
      await Promise.resolve();
    });
    setInput("Task title", "Review two files");
    setTextarea("Task instructions", "Compare both attached files.");
    await click("Start session");

    await vi.waitFor(() => expect(provider.submitTask).toHaveBeenCalledTimes(1));
    expect(uploadWorkspaceFile).toHaveBeenCalledTimes(2);
    // Prepare, one post-upload rebase, post-update, post-validation, and
    // post-admission refreshes. The attachment count no longer adds refreshes.
    expect(provider.refresh).toHaveBeenCalledTimes(initialRefreshCount + 5);
  });

  it("hides internal attachment paths from the completed Session transcript", async () => {
    const base = authoritySnapshot();
    const taskPresentation = base.runtimePresentation!.tasks["task-1"]!;
    const snapshot: DesktopProductSnapshotV2 = {
      ...base,
      runtimePresentation: {
        ...base.runtimePresentation!,
        tasks: {
          ...base.runtimePresentation!.tasks,
          "task-1": {
            ...taskPresentation,
            transcript: [
              {
                speaker: "user",
                text: "Explain this screenshot.\n\nAttached files are available in the project workspace:\n- session-attachments/internal-image.png",
              },
              { speaker: "agent", text: "The screenshot shows a completed run." },
            ],
          },
        },
      },
    };
    root = await render(providerFixture(snapshot));

    await click("Review evidence");
    expect(document.querySelector('article[aria-label="You"]')?.textContent).toContain("Explain this screenshot.");
    expect(document.body.textContent).not.toContain("Attached files are available in the project workspace");
    expect(document.body.textContent).not.toContain("session-attachments/internal-image.png");
  });

  it("starts a session with the historical Project Head selected by the user", async () => {
    const original = authoritySnapshot();
    const historicalHead = {
      ...original.tasks[0]!.admission.predecessor_project_head,
      project_head_id: "project-head-6",
      generation: 6,
      predecessor_project_head_id: "project-head-5",
    };
    const snapshot = {
      ...original,
      tasks: [{
        ...original.tasks[0]!,
        admission: {
          ...original.tasks[0]!.admission,
          predecessor_project_head: historicalHead,
        },
      }],
    };
    const provider = providerFixture(snapshot);
    root = await render(provider);
    const picker = document.querySelector<HTMLButtonElement>('.next-task-fields .soft-select-trigger[aria-label="Evolution context"]');
    expect(picker).toBeTruthy();

    setInput("Task title", "Review historical context");
    setTextarea("Task instructions", "Review the evidence with the selected historical context.");
    await selectSoftOption("Evolution context", "Project Head 6");
    await click("Start session");

    expect(provider.submitTask).toHaveBeenCalledWith(
      "project-1",
      expect.any(Object),
      expect.objectContaining({ project_head_id: "project-head-6", generation: 6 }),
    );
  });

  it("starts the Session from the composer with Enter and keeps Shift+Enter for a new line", async () => {
    const provider = providerFixture(authoritySnapshot());
    root = await render(provider);
    setInput("Task title", "Review evidence");
    setTextarea("Task instructions", "Review the evidence and update the workspace.");
    const instructions = textarea("Task instructions");

    await act(async () => {
      instructions.dispatchEvent(new KeyboardEvent("keydown", {
        key: "Enter",
        shiftKey: true,
        bubbles: true,
      }));
      await Promise.resolve();
    });
    expect(provider.submitTask).not.toHaveBeenCalled();

    await act(async () => {
      instructions.dispatchEvent(new KeyboardEvent("keydown", {
        key: "Enter",
        bubbles: true,
      }));
      await Promise.resolve();
      await Promise.resolve();
    });

    await vi.waitFor(() => expect(provider.submitTask).toHaveBeenCalled());
  });

  it("starts the Session from the composer with Enter and keeps Shift+Enter for a new line", async () => {
    const provider = providerFixture(authoritySnapshot());
    root = await render(provider);
    setInput("Task title", "Review evidence");
    setTextarea("Task instructions", "Review the evidence and update the workspace.");
    const instructions = textarea("Task instructions");

    await act(async () => {
      instructions.dispatchEvent(new KeyboardEvent("keydown", {
        key: "Enter",
        shiftKey: true,
        bubbles: true,
      }));
      await Promise.resolve();
    });
    expect(provider.submitTask).not.toHaveBeenCalled();

    await act(async () => {
      instructions.dispatchEvent(new KeyboardEvent("keydown", {
        key: "Enter",
        bubbles: true,
      }));
      await Promise.resolve();
      await Promise.resolve();
    });

    await vi.waitFor(() => expect(provider.submitTask).toHaveBeenCalled());
  });

  it("keeps the Session draft when admission fails", async () => {
    const fixture = providerFixture(authoritySnapshot());
    const provider = {
      ...fixture,
      validateProject: vi.fn(async () => {
        throw new Error("Remote validation failed.");
      }),
    } satisfies DesktopProductProviderV2;
    root = await render(provider);

    setInput("Task title", "Keep this draft title");
    setTextarea("Task instructions", "Keep this draft objective after the error.");
    await click("Start session");

    await vi.waitFor(() => expect(document.body.textContent).toContain("Remote validation failed."));
    expect(input("Task title").value).toBe("Keep this draft title");
    expect(textarea("Task instructions").value).toBe("Keep this draft objective after the error.");
  });

  it("turns an unresolved Session mutation into a clear recovery action", async () => {
    const fixture = providerFixture(authoritySnapshot());
    const provider = {
      ...fixture,
      validateProject: vi.fn(async () => {
        throw new Error("An unresolved mutation for this resource has different request or authority");
      }),
    } satisfies DesktopProductProviderV2;
    root = await render(provider);

    setInput("Task title", "Retry this Session");
    setTextarea("Task instructions", "Preserve this draft while refreshing state.");
    await click("Start session");

    await vi.waitFor(() => expect(document.body.textContent).toContain("Previous action was not fully confirmed"));
    expect(document.body.textContent).toContain("Refresh remote state, then submit the preserved draft again.");
    expect(document.body.textContent).not.toContain("different request or authority");
    const refreshCount = provider.refresh.mock.calls.length;
    await click("Refresh state");
    expect(provider.refresh).toHaveBeenCalledTimes(refreshCount + 1);
    expect(input("Task title").value).toBe("Retry this Session");
    expect(textarea("Task instructions").value).toBe("Preserve this draft while refreshing state.");
  });

  it("opens the chat view while the remote Session is being admitted", async () => {
    const snapshot = authoritySnapshot();
    let releaseSubmission!: (task: DesktopProductSnapshotV2["tasks"][number]) => void;
    const submission = new Promise<DesktopProductSnapshotV2["tasks"][number]>((resolve) => {
      releaseSubmission = resolve;
    });
    const fixture = providerFixture(snapshot);
    const provider = {
      ...fixture,
      submitTask: vi.fn(async () => submission),
    } satisfies DesktopProductProviderV2;
    root = await render(provider);
    setInput("Task title", "Review evidence");
    setTextarea("Task instructions", "Review the evidence and update the workspace.");

    await act(async () => {
      button("Start session").click();
      await Promise.resolve();
    });

    expect(document.querySelector('[data-testid="starting-session-workspace"]')).toBeTruthy();
    expect(document.querySelector('[data-testid="research-workspace"]')).toBeNull();
    expect(document.querySelector('[data-testid="starting-session-workspace"] .product-panel')).toBeNull();
    expect(document.querySelector('[data-testid="starting-session-workspace"] .v2-starting-session-card')).toBeTruthy();
    expect(document.body.textContent).toContain("Review the evidence and update the workspace.");
    expect(document.body.textContent).toContain("Creating Session");

    await vi.waitFor(() => expect(provider.submitTask).toHaveBeenCalledTimes(1));
    await act(async () => {
      releaseSubmission(snapshot.tasks[0]!);
      await submission;
      await Promise.resolve();
      await Promise.resolve();
    });
    await vi.waitFor(() => {
      expect(document.querySelector('[data-testid="starting-session-workspace"]')).toBeNull();
      expect(document.querySelector('[data-testid="session-detail-workspace"]')).toBeTruthy();
    });
  });

  it("switches from admission progress to the live chat as soon as the Session is running", async () => {
    const snapshot = authoritySnapshot();
    const runningTask = {
      ...snapshot.tasks[0]!,
      task_id: "task-running-new",
      state: "running" as const,
      updated_at: "2026-07-23T06:00:01Z",
    };
    let current = snapshot;
    let notify = (): void => undefined;
    let releaseSubmission!: (task: typeof runningTask) => void;
    const submission = new Promise<typeof runningTask>((resolve) => {
      releaseSubmission = resolve;
    });
    const fixture = providerFixture(snapshot);
    const provider = {
      ...fixture,
      refresh: vi.fn(async () => ({ status: "fresh" as const, snapshot: current })),
      subscribe: vi.fn((listener: Parameters<DesktopProductProviderV2["subscribe"]>[0]) => {
        notify = () => listener({ kind: "snapshot_changed" });
        return () => undefined;
      }),
      submitTask: vi.fn(async () => submission),
    } satisfies DesktopProductProviderV2;
    root = await render(provider);
    setInput("Task title", "Live research session");
    setTextarea("Task instructions", "Stream the Agent response in the conversation view.");

    await act(async () => {
      button("Start session").click();
      await Promise.resolve();
    });
    await vi.waitFor(() => expect(provider.submitTask).toHaveBeenCalledTimes(1));
    expect(document.querySelector('[data-testid="starting-session-workspace"]')).toBeTruthy();

    current = {
      ...current,
      tasks: [runningTask, ...current.tasks],
      runtimePresentation: {
        ...current.runtimePresentation!,
        tasks: {
          ...current.runtimePresentation!.tasks,
          [runningTask.task_id]: {
            instruction: {
              title: "Live research session",
              objective: "Stream the Agent response in the conversation view.",
            },
            transcript: [],
            outputFiles: [],
            usedArtifactIds: [],
            producedArtifactIds: [],
          },
        },
      },
    };
    await act(async () => {
      notify();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    await vi.waitFor(() => {
      expect(document.querySelector('[data-testid="starting-session-workspace"]')).toBeNull();
      expect(document.querySelector('[data-testid="session-detail-workspace"]')).toBeTruthy();
      expect(document.body.textContent).toContain("Live research session");
      expect(document.body.textContent).toContain("Running");
    });

    await act(async () => {
      releaseSubmission(runningTask);
      await submission;
      await Promise.resolve();
      await Promise.resolve();
    });
  });

  it("starts the next immutable Session from the conversation composer", async () => {
    const snapshot = authoritySnapshot();
    const provider = providerFixture(snapshot);
    root = await render(provider);
    await click("Review evidence");

    const composer = document.querySelector<HTMLTextAreaElement>(
      'textarea[aria-label="Message for the next Session"]',
    );
    const submit = document.querySelector<HTMLButtonElement>(
      'button[aria-label="Start next Session"]',
    );
    expect(composer).toBeTruthy();
    expect(submit).toBeTruthy();

    setAriaTextarea("Message for the next Session", "Check the revised report\nand list any remaining gaps.");
    let resumeRefresh = (): void => undefined;
    provider.refresh.mockImplementationOnce(async () => {
      await new Promise<void>((resolve) => { resumeRefresh = resolve; });
      return { status: "fresh" as const, snapshot };
    });
    await act(async () => {
      submit!.click();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(document.querySelector<HTMLInputElement>('input[placeholder="Name this Session"]')?.value)
      .toBe("Check the revised report");
    expect(document.querySelector<HTMLTextAreaElement>('textarea[placeholder="What should the Agent do next?"]')?.value)
      .toBe("Check the revised report\nand list any remaining gaps.");

    await act(async () => {
      resumeRefresh();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(provider.updateProject).toHaveBeenCalledWith(
      "project-1",
      "Protein study",
      {
        ...snapshot.projects[0]!.config,
        task: {
          title: "Check the revised report",
          objective: "Check the revised report\nand list any remaining gaps.",
        },
      },
      expect.objectContaining({ streamEpoch: 1 }),
    );
    expect(provider.validateProject).toHaveBeenCalled();
    expect(provider.submitTask).toHaveBeenCalled();
  });

  it("accepts a pasted screenshot in the Session conversation composer", async () => {
    const snapshot = authoritySnapshot();
    const uploadWorkspaceFile = vi.fn(async (
      _projectId: string,
      _upload: WorkspaceFileUploadV2,
    ) => undefined);
    const provider = {
      ...providerFixture(snapshot),
      uploadWorkspaceFile,
    } satisfies DesktopProductProviderV2;
    root = await render(provider);
    await click("Review evidence");

    const composer = document.querySelector<HTMLTextAreaElement>(
      'textarea[aria-label="Message for the next Session"]',
    )!;
    const screenshot = new File(["clipboard-png"], "image.png", { type: "image/png" });
    const paste = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(paste, "clipboardData", { value: { files: [screenshot] } });
    await act(async () => {
      composer.dispatchEvent(paste);
      await Promise.resolve();
    });
    setAriaTextarea("Message for the next Session", "Inspect this screenshot for errors.");

    expect(document.querySelector('[aria-label="Session attachments"]')).toBeTruthy();
    expect(document.querySelector<HTMLButtonElement>('[aria-label="Start next Session"]')?.disabled).toBe(false);
    await act(async () => {
      document.querySelector<HTMLButtonElement>('[aria-label="Start next Session"]')!.click();
      await Promise.resolve();
    });

    await vi.waitFor(() => expect(uploadWorkspaceFile).toHaveBeenCalledTimes(1));
    const upload = uploadWorkspaceFile.mock.calls[0]![1];
    expect(upload.path).toMatch(/^session-attachments\/attachment-[a-z0-9-]+-image\.png$/);
    await vi.waitFor(() => expect(provider.updateProject).toHaveBeenCalledWith(
      "project-1",
      "Protein study",
      expect.objectContaining({
        task: expect.objectContaining({ objective: expect.stringContaining(upload.path) }),
      }),
      expect.anything(),
    ));
  });

  it("separates Evolution history from run details and opens the latest run by default", async () => {
    const base = authoritySnapshot();
    const snapshot = {
      ...base,
      runtimePresentation: {
        ...base.runtimePresentation!,
        evolutionRuns: [
          {
            runId: "evolution-run-previous",
            projectId: "project-1",
            sourceTaskIds: ["task-1"],
            selections: [{ targetId: "text_memory", method: "text_memory_reflector", config: {} }],
            state: "applied" as const,
            artifactIds: ["artifact-memory-1"],
            jobIds: [],
            error: null,
            createdAt: "2026-07-22T06:00:00Z",
            updatedAt: "2026-07-22T06:00:00Z",
          },
          {
            runId: "evolution-run-current",
            projectId: "project-1",
            sourceTaskIds: ["task-1"],
            selections: [
              { targetId: "text_memory", method: "text_memory_reflector", config: {} },
              { targetId: "skill_bundle", method: "skill_bundle_reflector", config: {} },
            ],
            state: "candidate_ready" as const,
            artifactIds: ["artifact-memory-2", "artifact-skill-2"],
            jobIds: [],
            error: null,
            createdAt: NOW,
            updatedAt: NOW,
          },
        ],
      },
    };
    const provider = {
      ...providerFixture(snapshot),
      startEvolutionRun: vi.fn(async () => undefined),
      applyEvolutionRun: vi.fn(async () => undefined),
    } satisfies DesktopProductProviderV2;
    root = await render(provider);

    await click("Evolution");

    expect(document.body.textContent).toContain("Evolution Details");
    expect(document.body.textContent).toContain(
      "Mark every unsupported conclusion as a hypothesis",
    );
    expect(document.querySelector(".v2-evolution-run-selector")).toBeNull();
    expect(document.querySelector(".v2-all-evolution-artifacts")).toBeNull();
    expect(document.body.textContent).toContain("Apply to future Sessions");

    const skillArtifact = [
      ...document.querySelectorAll<HTMLButtonElement>(".v2-current-evolution-tabs > button"),
    ].find((candidate) => candidate.textContent?.includes("Skill bundle"));
    await act(async () => {
      skillArtifact?.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(document.body.textContent).toContain("Enumerate claims");

    await click("Evolution History");

    const historyRuns = [
      ...document.querySelectorAll<HTMLButtonElement>(".v2-evolution-run-selector > button"),
    ];
    expect(historyRuns).toHaveLength(2);
    expect(historyRuns[0]?.getAttribute("aria-pressed")).toBe("true");
    expect(document.querySelector(".v2-current-evolution-run")).toBeNull();
    expect(document.body.textContent).not.toContain("Enumerate claims");

    await act(async () => {
      historyRuns[1]!.click();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    const historicalViewer = document.querySelector(".v2-current-evolution-viewer");
    expect(document.querySelector(".v2-evolution-run-selector")).toBeNull();
    expect(document.body.textContent).toContain("Evolution Details");
    expect(historicalViewer?.textContent).toContain("Previous research memory");
    expect(historicalViewer?.textContent).toContain("Summarize the strongest conclusion");
  });

  it("shows a recoverable loading state while candidate artifacts refresh", async () => {
    const base = authoritySnapshot();
    const snapshot: DesktopProductSnapshotV2 = {
      ...base,
      artifacts: [],
      runtimePresentation: {
        ...base.runtimePresentation!,
        evolutionRuns: [{
          runId: "evolution-run-awaiting-artifacts",
          projectId: "project-1",
          sourceTaskIds: ["task-1"],
          selections: [{ targetId: "text_memory", method: "text_memory_reflector", config: {} }],
          state: "candidate_ready",
          artifactIds: ["candidate-artifact-not-loaded"],
          jobIds: [],
          error: null,
          createdAt: NOW,
          updatedAt: NOW,
        }],
      },
    };
    const provider = {
      ...providerFixture(snapshot),
      startEvolutionRun: vi.fn(async () => undefined),
      applyEvolutionRun: vi.fn(async () => undefined),
    } satisfies DesktopProductProviderV2;
    root = await render(provider);

    await click("Evolution");

    expect(document.body.textContent).toContain("Loading result artifacts");
    expect(document.body.textContent).not.toContain("without a readable textual artifact");
    expect(button("Apply to future Sessions").disabled).toBe(true);
  });

  it("uses an active Evolution job for the page heading and Session admission guard", async () => {
    const base = authoritySnapshot();
    const snapshot: DesktopProductSnapshotV2 = {
      ...base,
      runtimePresentation: {
        ...base.runtimePresentation!,
        evolutionRuns: [],
        tasks: {
          ...base.runtimePresentation!.tasks,
          "task-1": {
            ...base.runtimePresentation!.tasks["task-1"]!,
            evolutionJobs: [{
              jobId: "evolution-job-active",
              targetId: "text_memory",
              methodId: "text_memory_reflector",
              requestedMethodId: "text_memory_reflector",
              resolverInputArtifactIds: [],
              previousArtifactId: null,
              config: {},
              state: "running",
              artifactIds: [],
              error: null,
              attempts: [],
              createdAt: NOW,
              updatedAt: NOW,
            }],
          },
        },
      },
    };
    const provider = {
      ...providerFixture(snapshot),
      featureFlags: ["system_openssh_profiles", "development_agent_bridge"],
    } satisfies DesktopProductProviderV2;
    root = await render(provider);

    const blocked = button("Evolution running");
    expect(blocked.disabled).toBe(true);
    expect(blocked.querySelector(".spin")).toBeNull();
    expect(document.body.textContent).not.toContain("View immutable Project authority");

    await click("Evolution");
    expect(document.querySelector(".evolution-workspace-heading h1")?.textContent).toBe(
      "Evolution Running",
    );
    expect(document.querySelector(".evolution-heading-running-icon .spin")).toBeTruthy();
    expect(document.body.textContent).toContain("Creating improvements from 1 selected Session.");
  });

  it("coalesces an SSE refresh with the Start session authority preflight", async () => {
    const snapshot = authoritySnapshot();
    const provider = providerFixture(snapshot);
    let signal: Parameters<DesktopProductProviderV2["subscribe"]>[0] | null = null;
    provider.subscribe.mockImplementation((next) => {
      signal = next;
      return () => {
        signal = null;
      };
    });
    root = await render(provider);

    let releasePreflight!: () => void;
    const preflightBlocked = new Promise<void>((resolve) => {
      releasePreflight = resolve;
    });
    const refreshCallsBeforeStart = provider.refresh.mock.calls.length;
    provider.refresh.mockImplementationOnce(async () => {
      await preflightBlocked;
      return { status: "fresh" as const, snapshot };
    });
    setInput("Task title", "Review evidence");
    setTextarea("Task instructions", "Review the evidence and update the workspace.");

    await act(async () => {
      button("Start session").click();
      await vi.waitFor(() => {
        expect(provider.refresh.mock.calls.length).toBe(refreshCallsBeforeStart + 1);
      });
      signal?.({ kind: "snapshot_changed" });
      releasePreflight();
      await vi.waitFor(() => {
        expect(provider.submitTask).toHaveBeenCalledTimes(1);
      });
    });

    expect(document.body.textContent).not.toContain(
      "The current remote project state could not be loaded before starting the session.",
    );
    expect(provider.refresh.mock.calls.length).toBeGreaterThan(
      refreshCallsBeforeStart + 1,
    );
  });

  it("keeps the v1 create-project entry beside the project switcher", async () => {
    const provider = providerFixture(authoritySnapshot());
    root = await render(provider);

    const createProject = document.querySelector<HTMLButtonElement>(
      'button[aria-label="Create project"]',
    );
    expect(createProject).toBeTruthy();
    await act(async () => createProject!.click());

    expect(dialog()?.textContent).toContain("Create a project");
    expect(input("Project name").value).toBe("New research project");
    expect(dialog()?.textContent).not.toContain("Task title");
    expect(dialog()?.textContent).not.toContain("Task objective");
  });

  it("keeps Session task fields out of Project settings", async () => {
    const snapshot = authoritySnapshot();
    const provider = providerFixture(snapshot);
    root = await render(provider);

    await click("Edit project");
    expect(dialog()?.textContent).toContain("Edit project");
    expect(dialog()?.textContent).not.toContain("Task title");
    expect(dialog()?.textContent).not.toContain("Task objective");

    setInput("Project name", "Renamed protein study");
    await click("Save changes");
    expect(provider.updateProject).toHaveBeenCalledWith(
      "project-1",
      "Renamed protein study",
      expect.objectContaining({ task: snapshot.projects[0]!.config.task }),
      expect.objectContaining({ streamEpoch: 1 }),
    );
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
      runtimePresentation: {
        ...initial.runtimePresentation!,
        tasks: {
          ...initial.runtimePresentation!.tasks,
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

    const switcher = document.querySelector<HTMLButtonElement>(
      "#v2-project-switcher",
    )!;
    await act(async () => {
      switcher.click();
      await Promise.resolve();
    });
    expect([...document.querySelectorAll('[role="option"]')].map((option) => option.textContent)).toEqual(
      expect.arrayContaining(["Protein study", "Second protein study"]),
    );
    await act(async () => {
      [...document.querySelectorAll<HTMLButtonElement>('[role="option"]')]
        .find((option) => option.textContent?.includes("Second protein study"))!
        .click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(provider.activateProject).toHaveBeenCalledWith(
      "project-2",
      expect.objectContaining({ actionId: expect.any(String) }),
    );
    expect(
      document.querySelector('[data-testid="session-detail-workspace"]'),
    ).toBeFalsy();
  });

  it("opens a blank composer for each Project while restoring Session-list scroll position", async () => {
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
    let current: DesktopProductSnapshotV2 = {
      ...initial,
      projects: [...initial.projects, secondProject] as never,
      tasks: [...initial.tasks, secondTask] as never,
      runtimePresentation: {
        ...initial.runtimePresentation!,
        tasks: {
          ...initial.runtimePresentation!.tasks,
          "task-2": {
            instruction: { title: "Second project session", objective: "Project two." },
            transcript: [], outputFiles: [], usedArtifactIds: [], producedArtifactIds: [],
          },
        },
      },
    };
    const fixture = providerFixture(current);
    const provider = {
      ...fixture,
      refresh: vi.fn(async () => ({ status: "fresh" as const, snapshot: current })),
      activateProject: vi.fn(async (projectId: string) => {
        current = { ...current, state: { ...current.state, active_project_id: projectId } };
        return { schema_version: "2" } as never;
      }),
    } satisfies DesktopProductProviderV2;
    root = await render(provider);

    await click("Review evidence");
    const firstList = document.querySelector<HTMLElement>(".session-explorer-list")!;
    firstList.scrollTop = 120;
    firstList.dispatchEvent(new Event("scroll"));

    const switcher = document.querySelector<HTMLButtonElement>("#v2-project-switcher")!;
    await selectProject("Second protein study");
    await vi.waitFor(() => expect(switcher.disabled).toBe(false));
    await click("Second project session");

    await selectProject("Protein study");
    await vi.waitFor(() => expect(switcher.disabled).toBe(false));
    await vi.waitFor(() => expect(document.querySelector('[data-testid="session-composer"]')).toBeTruthy());
    expect(document.querySelector<HTMLButtonElement>('button[title="Review evidence"]')?.classList.contains("active")).toBe(false);
    expect(input("Task title").value).toBe("");
    expect(textarea("Task instructions").value).toBe("");
    await vi.waitFor(() => expect(document.querySelector<HTMLElement>(".session-explorer-list")?.scrollTop).toBe(120));
  });

  it("sorts Sessions newest-first and supports search and status filters", async () => {
    const base = authoritySnapshot();
    const baseTask = base.tasks[0]!;
    const running = { ...baseTask, task_id: "task-running", state: "running" as const, updated_at: "2026-07-24T06:00:00Z" };
    const failed = { ...baseTask, task_id: "task-failed", state: "failed" as const, updated_at: "2026-07-22T06:00:00Z" };
    const snapshot: DesktopProductSnapshotV2 = {
      ...base,
      tasks: [baseTask, failed, running] as never,
      runtimePresentation: {
        ...base.runtimePresentation!,
        tasks: {
          ...base.runtimePresentation!.tasks,
          "task-running": { instruction: { title: "Newest running session", objective: "Run." }, transcript: [], outputFiles: [], usedArtifactIds: [], producedArtifactIds: [] },
          "task-failed": { instruction: { title: "Older failed session", objective: "Fail." }, transcript: [], outputFiles: [], usedArtifactIds: [], producedArtifactIds: [] },
        },
      },
    };
    root = await render(providerFixture(snapshot));

    const sessionButtons = () => [...document.querySelectorAll<HTMLButtonElement>(".session-explorer-item-main")];
    expect(sessionButtons()[0]?.title).toBe("Newest running session");

    const search = document.querySelector<HTMLInputElement>('input[aria-label="Search Sessions"]')!;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      setter?.call(search, "failed");
      search.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(sessionButtons().map((item) => item.title)).toEqual(["Older failed session"]);

    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      setter?.call(search, "");
      search.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await selectSoftOption("Filter Sessions by status", "Active");
    expect(sessionButtons().map((item) => item.title)).toEqual(["Newest running session"]);
  });

  it("presents closed Sessions as completed with an English timestamp", async () => {
    root = await render(providerFixture(authoritySnapshot()));

    const session = document.querySelector<HTMLButtonElement>(".session-explorer-item-main")!;
    expect(session.querySelector("em")?.textContent).toBe("Completed");
    expect(session.querySelector("small")?.textContent).toMatch(/^Jul 23, \d{2}:\d{2}$/);
    expect(session.querySelector("small")?.textContent).not.toMatch(/[\u6708\u65e5]/);
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

    expect(document.querySelector(".evolution-stepper")).toBeNull();
    const method = document.querySelector<HTMLButtonElement>(
      ".v2-target-list .soft-select-trigger",
    );
    expect(method?.dataset.value).toBe("auto");
    expect(method?.textContent).toContain("Automatic");
    await act(async () => {
      method?.click();
      await Promise.resolve();
    });
    expect([...document.querySelectorAll('[role="option"]')].map((option) => option.textContent)).toContain("Automatic");
    expect(document.body.textContent).not.toContain("blocks Task admission");
  });

  it("blocks a new task while the successor is not ready and exposes transition recovery", async () => {
    const provider = providerFixture(authoritySnapshot("not_ready"));
    root = await render(provider);

    expect(
      document.querySelector('[data-testid="session-detail-workspace"]'),
    ).toBeTruthy();
    expect(document.querySelector('[data-testid="session-composer"]')).toBeFalsy();
    expect(
      [...document.querySelectorAll("button")].some((candidate) =>
        candidate.textContent?.includes("Start session"),
      ),
    ).toBe(false);
    expect(button("Retry successor transition")).toBeTruthy();
    expect(document.body.textContent).toContain("Project Head update failed");
    expect(document.body.textContent).not.toContain("Technical details");
    expect(document.body.textContent).not.toContain("Successor state: failed");
    expect(provider.submitTask).not.toHaveBeenCalled();
  });

  it("keeps generation-zero projects recoverable until task admission is ready", async () => {
    const readySnapshot = authoritySnapshot();
    const pendingProject = {
      ...readySnapshot.projects[0]!,
      active_project_head: null,
      admission_etag: null,
      state: "not_ready" as const,
    };
    const pendingSnapshot: DesktopProductSnapshotV2 = {
      ...readySnapshot,
      projects: [pendingProject],
      tasks: [],
      transitions: {},
      runtimePresentation: {
        ...readySnapshot.runtimePresentation!,
        tasks: {},
      },
    };
    const provider = providerFixture(pendingSnapshot);
    root = await render(provider);

    expect(button("Start session").disabled).toBe(true);
    expect(document.body.textContent).toContain(
      "EvoLab is preparing the remote service and initial Project Head",
    );

    const refreshCalls = provider.refresh.mock.calls.length;
    await click("Retry now");
    expect(provider.refresh.mock.calls.length).toBeGreaterThan(refreshCalls);
    expect(provider.submitTask).not.toHaveBeenCalled();
  });

  it("keeps an active Session focused on Agent activity and cancellation", async () => {
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

    expect(loadTaskLogs).toHaveBeenCalledWith("task-1", { limit: 100 });
    expect(document.body.textContent).toContain(
      "Daemon started the managed Task attempt.",
    );
    expect(document.body.textContent).toContain("Running");
    expect(document.body.textContent).toContain("Cancel session");
    expect(document.querySelector('[data-testid="session-agent-activity"]')).toBeTruthy();
    expect(document.body.textContent).not.toContain("Technical details");
    expect(document.body.textContent).not.toContain("Task state:");
  });

  it("cancels an active Session from the chat and returns to the new Session composer", async () => {
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
    const cancelTask = vi.fn(async () => ({
      schema_version: "2",
      operation_id: "development-session-cancel-task-1",
      kind: "task_cancel",
      resource: { resource_kind: "task", resource_id: "task-1" },
      status: "accepted",
      created_at: NOW,
      updated_at: NOW,
    } as never));
    const provider = {
      ...providerFixture(runningSnapshot),
      cancelTask,
    } satisfies DesktopProductProviderV2;
    root = await render(provider);

    expect(document.body.textContent).toContain("Cancel session");
    await click("Cancel session");
    expect(cancelTask).toHaveBeenCalledWith("task-1", expect.objectContaining({ streamEpoch: 1 }));

    await click("New Session");
    expect(document.querySelector('[data-testid="session-composer"]')).toBeTruthy();
    expect(document.body.textContent).not.toContain("Open live Session");
    expect(document.body.textContent).not.toContain("Cancel Session");
  });

  it("hides the transient Agent activity after the Session is complete", async () => {
    root = await render(providerFixture(authoritySnapshot()));

    expect(document.querySelector('[data-testid="session-agent-activity"]')).toBeNull();
    expect(document.body.textContent).not.toContain("Agent working");
  });

  it("loads the final formal transcript when a completed Session is opened", async () => {
    const snapshot = authoritySnapshot();
    const formalSnapshot: DesktopProductSnapshotV2 = {
      ...snapshot,
      runtimePresentation: undefined,
    };
    const loadTaskLogs = vi.fn(async () => ({
      schema_version: "2" as const,
      items: [
        {
          sequence: 1,
          occurred_at: NOW,
          stream: "system" as const,
          message: "Task closed after successor activation.",
        },
        {
          sequence: 2,
          occurred_at: NOW,
          stream: "transcript" as const,
          message: "assistant: The formal agent response was captured.",
        },
      ],
      next_cursor: null,
      has_more: false,
    }));
    const provider = {
      ...providerFixture(formalSnapshot),
      loadTaskLogs,
    } satisfies DesktopProductProviderV2;

    root = await render(provider);
    await click("Session 1");
    await vi.waitFor(() => {
      expect(loadTaskLogs).toHaveBeenCalledWith("task-1", { limit: 100 });
      expect(document.querySelector(".v2-conversation-section")?.textContent).toContain(
        "The formal agent response was captured.",
      );
    });

    const conversation = document.querySelector(".v2-conversation-section")!;
    expect(conversation.textContent).toContain("Review the evidence and update the workspace.");
    expect(conversation.textContent).not.toContain("Conversation");
    expect(conversation.textContent).not.toContain("Task closed after successor activation.");
  });

  it("persists formal Core evolution selections before starting a Session", async () => {
    const snapshot = authoritySnapshot();
    snapshot.capability!.capabilities.targets = [
      {
        target_id: "text_memory",
        display_name: "Memory",
        description: "Learn durable research memory from the transcript.",
        exposure: "desktop",
        effective_default_method_id: "trajectory_to_memory",
        methods: [
          {
            method_id: "trajectory_to_memory",
            display_name: "Trajectory to memory",
            default_config_json: "{}",
            support: { overall: "supported" },
          },
        ],
        accepted_methods: [],
        selection_resolvers: [],
      },
    ] as never;
    const provider = providerFixture(snapshot);
    root = await render(provider);
    setInput("Task title", "Review evidence");
    setTextarea("Task instructions", "Review the evidence and update the workspace.");

    const checkbox = document.querySelector<HTMLInputElement>(
      '.session-evolution-picker input[type="checkbox"]',
    );
    expect(checkbox).toBeTruthy();
    await act(async () => {
      checkbox!.click();
      await Promise.resolve();
    });
    await click("Start session");

    expect(provider.updateProject).toHaveBeenCalledWith(
      "project-1",
      "Protein study",
      expect.objectContaining({
        evolution: {
          targets: {
            text_memory: {
              enabled: true,
              method: "trajectory_to_memory",
              config: {},
            },
          },
        },
      }),
      expect.anything(),
    );
  });

  it("keeps Core-owned background operations out of the ordinary workspace", async () => {
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
    const provider = {
      ...providerFixture(authoritySnapshot()),
      listCoreOperations: () => [operation],
    } satisfies DesktopProductProviderV2;

    root = await render(provider);

    expect(document.querySelector('[aria-label="Active operations"]')).toBeNull();
    expect(document.body.textContent).not.toContain("Operation progress");
    expect(document.body.textContent).not.toContain("Restart remote service");
    expect(document.body.textContent).not.toContain("Process log");
  });

  it("only offers the supported Codex Subscription execution profile", async () => {
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
      operation_id: "project-create-subscription-1",
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
    expect(document.body.textContent).not.toContain("Self-Deployed");
    expect(document.body.textContent).toContain("Codex Subscription");
    await click("Create project");

    expect(createProject).toHaveBeenCalledWith(
      expect.objectContaining({
        config: expect.objectContaining({
          task: {
            title: "Untitled Session",
            objective: "Task details are provided when the Session starts.",
          },
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
        }),
      }),
      expect.objectContaining({ streamEpoch: 1 }),
    );
  });

  it("pins a ready Hugging Face model when creating a project", async () => {
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
    const createProject = vi.fn(async () => ({ status: "queued" } as never));
    const provider = {
      ...unavailableDesktopProductProviderV2,
      featureFlags: ["system_openssh_profiles", "huggingface_model_management_v2"],
      refresh: vi.fn(async () => ({ status: "fresh" as const, snapshot })),
      createProject,
      listModelResources: vi.fn(async () => [{
        model_resource_id: "model-ready",
        repository_id: "OpenEvo/Fixture-0.1B",
        requested_revision: "main",
        resolved_revision: "a".repeat(40),
        manifest_sha256: DIGEST,
        state: "ready" as const,
        downloaded_bytes: 128,
        total_bytes: 128,
        error: null,
        created_at: NOW,
        updated_at: NOW,
      }]),
    } satisfies DesktopProductProviderV2;
    root = await render(provider);

    await click("New project");
    await act(async () => Promise.resolve());
    await click("Hugging FaceDownloaded and served on this server");
    await click("OpenEvo/Fixture-0.1Baaaaaaaaaa");
    await click("Create project");

    expect(createProject).toHaveBeenCalledWith(
      expect.objectContaining({
        config: expect.objectContaining({
          execution: expect.objectContaining({
            mode: "self-deployed",
            model_profile_id: null,
            model_resource_id: "model-ready",
            repository_id: "OpenEvo/Fixture-0.1B",
            model_revision: "a".repeat(40),
          }),
        }),
      }),
      expect.anything(),
    );
  });

  it("closes project setup after HTTP 202 while work continues silently", async () => {
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
    await click("Create project");
    await act(async () => vi.advanceTimersByTime(16_000));

    expect(createProject).toHaveBeenCalledTimes(1);
    expect(document.body.textContent).not.toContain(
      "Creating or loading the remote project",
    );
    expect(document.body.textContent).not.toContain(
      "Remote project request accepted",
    );
    expect(document.body.textContent).not.toContain(
      "Materializing workspace snapshot",
    );
    expect(document.body.textContent).not.toContain("Elapsed 16s");
    expect(document.body.textContent).not.toContain(
      "Desktop Local API request timed out",
    );
    expect(document.querySelector('[aria-label="Active operations"]')).toBeNull();
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
    await click("Import a folder");
    expect(selectNativeWorkspace).toHaveBeenCalledTimes(1);
    await click("Cancel");

    expect(cancelNativeWorkspace).toHaveBeenCalledWith(expect.any(String));
  });

  it("creates a browser folder snapshot and uploads its contents at the workspace root", async () => {
    const connected = systemProfile({
      connection_state: "connected",
      core_api_major: 2,
      core_openapi_sha256: DIGEST,
      core_event_schema_sha256: DIGEST,
      core_registry_sha256: DIGEST,
    });
    const initialSnapshot = baseSnapshot({
      profiles: [connected] as never,
      state: {
        ...baseSnapshot().state,
        profiles: [connected] as never,
        active_profile_id: connected.profile_id,
      },
    });
    let created = false;
    const createProject = vi.fn(async () => {
      created = true;
      return {
        schema_version: "2" as const,
        operation_id: "project-create-browser-folder-1",
        kind: "project_create" as const,
        resource: { resource_kind: "project" as const, resource_id: "project-1" },
        request_sha256: DIGEST,
        status: "succeeded" as const,
        phase: "finalizing" as const,
        phase_index: 16,
        phase_total: 17,
        progress: { kind: "items" as const, completed: 1, total: 1 },
        cancellable: false,
        result: { result_kind: "project" as const, project_id: "project-1" },
        failure: null,
        log_sequence_high_watermark: 0,
        created_at: NOW,
        started_at: NOW,
        updated_at: NOW,
        finished_at: NOW,
        etag: ETAG,
      };
    });
    const uploadWorkspaceFile = vi.fn(async (
      _projectId: string,
      upload: WorkspaceFileUploadV2,
    ) => {
      upload.onProgress?.(50);
      upload.onProgress?.(100);
    });
    const updateProject = vi.fn(async () => authoritySnapshot().projects[0]!);
    const provider = {
      ...unavailableDesktopProductProviderV2,
      featureFlags: ["system_openssh_profiles", "browser_folder_snapshot"],
      refresh: vi.fn(async () => ({
        status: "fresh" as const,
        snapshot: created ? authoritySnapshot() : initialSnapshot,
      })),
      createProject,
      updateProject,
      uploadWorkspaceFile,
      deleteProject: vi.fn(async () => {}),
    } satisfies DesktopProductProviderV2;
    root = await render(provider);

    await click("New project");
    const folderInput = document.querySelector<HTMLInputElement>('[aria-label="Choose project folder snapshot"]');
    expect(folderInput).toBeTruthy();
    expect(folderInput?.hasAttribute("webkitdirectory")).toBe(true);
    const source = new File(["export const answer = 42;\n"], "index.ts", { type: "text/typescript" });
    const notes = new File(["# Notes\n"], "notes.md", { type: "text/markdown" });
    Object.defineProperty(source, "webkitRelativePath", { value: "my-project/src/index.ts" });
    Object.defineProperty(notes, "webkitRelativePath", { value: "my-project/docs/notes.md" });
    Object.defineProperty(folderInput!, "files", { configurable: true, value: [source, notes] });
    await act(async () => {
      folderInput!.dispatchEvent(new Event("change", { bubbles: true }));
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("my-project · 2 files");
    await click("Create project");
    await vi.waitFor(() => expect(updateProject).toHaveBeenCalledTimes(1));

    expect(createProject).toHaveBeenCalledWith(
      expect.objectContaining({ config: expect.objectContaining({ workspace: expect.objectContaining({ kind: "scratch" }) }) }),
      expect.anything(),
    );
    expect(uploadWorkspaceFile).toHaveBeenNthCalledWith(
      1,
      "project-1",
      expect.objectContaining({ path: "src/index.ts", data: source, overwrite: false }),
      expect.anything(),
    );
    expect(uploadWorkspaceFile).toHaveBeenNthCalledWith(
      2,
      "project-1",
      expect.objectContaining({ path: "docs/notes.md", data: notes, overwrite: false }),
      expect.anything(),
    );
    expect(updateProject).toHaveBeenCalledWith(
      "project-1",
      "New research project",
      expect.objectContaining({ workspace: { kind: "native_folder_snapshot", display_name: "my-project" } }),
      expect.anything(),
    );
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

const TEST_LABEL_ALIASES: Readonly<Record<string, string>> = {};

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

async function selectProject(label: string): Promise<void> {
  await act(async () => {
    document.querySelector<HTMLButtonElement>("#v2-project-switcher")?.click();
    await Promise.resolve();
  });
  const option = [...document.querySelectorAll<HTMLButtonElement>('[role="option"]')]
    .find((candidate) => candidate.textContent?.trim().includes(label));
  if (!option) throw new Error(`project option not found: ${label}`);
  await act(async () => {
    option.click();
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function selectSoftOption(ariaLabel: string, optionLabel: string): Promise<void> {
  const trigger = [...document.querySelectorAll<HTMLButtonElement>(".soft-select-trigger")]
    .find((candidate) => candidate.getAttribute("aria-label") === ariaLabel);
  if (!trigger) throw new Error(`select trigger not found: ${ariaLabel}`);
  await act(async () => {
    trigger.click();
    await Promise.resolve();
  });
  const listbox = [...document.querySelectorAll<HTMLElement>('[role="listbox"]')]
    .find((candidate) => candidate.getAttribute("aria-label") === ariaLabel);
  const option = [...(listbox?.querySelectorAll<HTMLButtonElement>('[role="option"]') ?? [])]
    .find((candidate) => candidate.textContent?.trim().includes(optionLabel));
  if (!option) throw new Error(`select option not found: ${optionLabel}`);
  await act(async () => {
    option.click();
    await Promise.resolve();
    await Promise.resolve();
  });
}

function dialog(): HTMLElement | null {
  return document.querySelector('[role="dialog"]');
}

function setInput(label: string, value: string): void {
  const localized = TEST_LABEL_ALIASES[label] ?? label;
  const labels = [...document.querySelectorAll<HTMLLabelElement>("label")];
  const owner = labels.find((candidate) =>
    candidate.textContent?.includes(localized),
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
  const localized = TEST_LABEL_ALIASES[label] ?? label;
  const labels = [...document.querySelectorAll<HTMLLabelElement>("label")];
  const owner = labels.find((candidate) =>
    candidate.textContent?.includes(localized),
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

function setAriaTextarea(label: string, value: string): void {
  const input = document.querySelector<HTMLTextAreaElement>(
    `textarea[aria-label="${label}"]`,
  );
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
  const localized = TEST_LABEL_ALIASES[label] ?? label;
  const owner = [...document.querySelectorAll<HTMLLabelElement>("label")].find(
    (candidate) => candidate.textContent?.includes(localized),
  );
  const control = owner?.querySelector<HTMLInputElement>("input");
  if (!control) throw new Error(`input not found: ${label}`);
  return control;
}

function textarea(label: string): HTMLTextAreaElement {
  const localized = TEST_LABEL_ALIASES[label] ?? label;
  const owner = [...document.querySelectorAll<HTMLLabelElement>("label")].find(
    (candidate) => candidate.textContent?.includes(localized),
  );
  const control = owner?.querySelector<HTMLTextAreaElement>("textarea");
  if (!control) throw new Error(`textarea not found: ${label}`);
  return control;
}
