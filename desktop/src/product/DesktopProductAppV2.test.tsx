// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DesktopProductSnapshotV2 } from "./providerV2";
import {
  unavailableDesktopProductProviderV2,
  type DesktopProductProviderV2,
} from "./providerV2";
import { DesktopProductAppV2 } from "./DesktopProductAppV2";

const NOW = "2026-07-23T06:00:00Z";
const DIGEST = "a".repeat(64);
const ETAG = `"${"b".repeat(64)}"`;

function baseSnapshot(overrides: Partial<DesktopProductSnapshotV2> = {}): DesktopProductSnapshotV2 {
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
      hosts: [{
        schema_version: "2",
        ssh_host_alias: "gpu-lab",
        availability: "selectable",
        source_kind: "literal_host",
      }],
      warnings: [{
        schema_version: "2",
        code: "dynamic_hosts_not_enumerated",
        action: "manual_alias_available",
        affected_entry_count: 1,
      }],
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

function authoritySnapshot(state: "ready" | "not_ready" = "ready"): DesktopProductSnapshotV2 {
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
      task: { title: "Review evidence", objective: "Review the evidence and update the workspace." },
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
    error: state === "ready" ? null : {
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
    tasks: [{
      schema_version: "2",
      task_id: "task-1",
      project_id: "project-1",
      admission,
      attempts: [{ ...attempt, attempt_id: "attempt-1", ordinal: 1 }, attempt],
      authoritative_attempt_id: attempt.attempt_id,
      successor_transition: transitionRef,
      state: state === "ready" ? "closed" : "waiting_for_successor",
      created_at: NOW,
      updated_at: NOW,
      etag: ETAG,
    }] as never,
    transitions: { [transitionRef.successor_transition_id]: transition } as never,
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
    refresh: vi.fn(async () => ({ status: "fresh" as const, snapshot: current })),
    subscribe: vi.fn(() => () => undefined),
    createProfile: vi.fn(async (displayName: string, alias: string) => {
      const created = systemProfile({ display_name: displayName, ssh_host_alias: alias });
      current = baseSnapshot({
        ...current,
        profiles: [created] as never,
        state: { ...current.state, profiles: [created] as never },
        stream: { status: "fresh", epoch: current.stream.epoch + 1, lastEventId: null },
      });
      return created as never;
    }),
    connectProfile: vi.fn(async () => ({
      schema_version: "2",
      operation_id: "operation-connect-1",
      kind: "profile_connect",
      status: "queued",
      failure: null,
      created_at: NOW,
      updated_at: NOW,
    } as never)),
    rebindProfile: vi.fn(async () => systemProfile() as never),
    reviewHostKey: vi.fn(async () => ({
      schema_version: "2",
      operation_id: "operation-review-1",
      kind: "host_key_review",
      status: "queued",
      failure: null,
      created_at: NOW,
      updated_at: NOW,
    } as never)),
    rescanSshHosts: vi.fn(async () => current.catalog),
    submitTask: vi.fn(),
  } satisfies DesktopProductProviderV2;
  return provider;
}

describe("Desktop v2 product renderer", () => {
  let root: Root | null = null;

  beforeEach(() => {
    document.body.innerHTML = "<div id=\"root\"></div>";
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  });

  afterEach(async () => {
    if (root) await act(async () => root?.unmount());
    root = null;
    document.body.innerHTML = "";
  });

  it("opens configured-host setup immediately and never renders manual connection fields", async () => {
    const provider = providerFixture(baseSnapshot());
    root = await render(provider);

    await click("Add remote workspace");

    expect(dialog()?.textContent).toContain("Configured SSH host");
    expect(dialog()?.textContent).toContain("gpu-lab");
    expect(dialog()?.textContent).toContain("Some configured hosts cannot be listed");
    const fieldLabels = [...(dialog()?.querySelectorAll("label") ?? [])].map((label) => label.textContent ?? "").join(" ");
    expect(fieldLabels).not.toMatch(/server address|user name|port|private key|password/i);
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
    expect(JSON.stringify(provider.createProfile.mock.calls)).not.toMatch(/username|password|identity|host_path/i);
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
        key_fingerprints: [{
          schema_version: "2",
          algorithm: "ssh-ed25519",
          sha256_fingerprint: `SHA256:${"A".repeat(43)}`,
          role: "presented",
        }],
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

  it("distinguishes task, admission, attempt, project head, evolution, runtime, and execution identities", async () => {
    root = await render(providerFixture(authoritySnapshot()));

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

  it("renders a supported Core-owned selection resolver as the saved method", async () => {
    const snapshot = authoritySnapshot();
    const project = snapshot.projects[0]!;
    (project.config.evolution.targets as Record<string, unknown>).agent_system = {
      enabled: true,
      method: "auto",
      config: { target_path: "AGENTS.md" },
    };
    snapshot.capability!.capabilities.targets = [{
      target_id: "agent_system",
      display_name: "Agent system",
      description: "Core-owned agent-system evolution.",
      exposure: "desktop",
      effective_default_method_id: "concrete_agent_system",
      methods: [{
        method_id: "concrete_agent_system",
        display_name: "Concrete method",
        default_config_json: "{}",
      }],
      accepted_methods: [{
        method_id: "concrete_agent_system",
        implementation_identity_digest: DIGEST,
        support: { overall: "supported" },
      }],
      selection_resolvers: [{
        selection_value: "auto",
        display_name: "Automatic",
        description: "Core selects an accepted concrete method.",
        resolved_methods: [{
          method_id: "concrete_agent_system",
          implementation_identity_digest: DIGEST,
          support: { overall: "supported" },
        }],
      }],
    }] as never;
    root = await render(providerFixture(snapshot));

    await click("Evolution");

    const method = document.querySelector<HTMLSelectElement>(".v2-target-list select");
    expect(method?.value).toBe("auto");
    expect([...method!.options].map((option) => option.textContent)).toContain("Automatic");
    expect(document.body.textContent).not.toContain("blocks Task admission");
  });

  it("blocks a new task while the successor is not ready and exposes transition recovery", async () => {
    const provider = providerFixture(authoritySnapshot("not_ready"));
    root = await render(provider);

    expect(document.body.textContent).toContain("Next task is not ready");
    expect(button("Validate and run task").disabled).toBe(true);
    expect(button("Retry successor transition")).toBeTruthy();
    expect(provider.submitTask).not.toHaveBeenCalled();
  });
});

async function render(provider: DesktopProductProviderV2): Promise<Root> {
  const container = document.querySelector("#root");
  if (!container) throw new Error("root is missing");
  const root = createRoot(container);
  await act(async () => {
    root.render(<DesktopProductAppV2 provider={provider} />);
    await Promise.resolve();
    await Promise.resolve();
  });
  return root;
}

function button(label: string): HTMLButtonElement {
  const match = [...document.querySelectorAll<HTMLButtonElement>("button")]
    .find((candidate) => candidate.textContent?.trim().includes(label));
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
  const owner = labels.find((candidate) => candidate.textContent?.includes(label));
  const input = owner?.querySelector<HTMLInputElement>("input");
  if (!input) throw new Error(`input not found: ${label}`);
  act(() => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setter?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
}
