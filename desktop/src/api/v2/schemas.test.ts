import { describe, expect, it } from "vitest";
import {
  cacheCleanupRequestV2Schema,
  canonicalJsonV2,
  coreOperationV2Schema,
  desktopEventEnvelopeV2Schema,
  desktopStateV2Schema,
  desktopVersionV2Schema,
  lifecycleLogPageV2Schema,
  lifecycleOperationV2Schema,
  projectHeadRefV2Schema,
  remoteProfileV2Schema,
  scienceProjectConfigV2Schema,
  sha256Utf8V2,
  sshHostCatalogV2Schema,
} from "./schemas";

const DIGEST = "a".repeat(64);
const ETAG = `"${"b".repeat(64)}"`;
const NOW = "2026-07-23T06:00:00Z";

function trust(connectionGeneration = 1) {
  return {
    schema_version: "2",
    connection_generation: connectionGeneration,
    state: "trusted",
    review_id: null,
    review_sha256: null,
    key_fingerprints: [],
    repair_support: "not_needed",
  };
}

function profile() {
  return {
    schema_version: "2",
    profile_kind: "system_openssh",
    profile_id: "profile-lab",
    display_name: "Lab GPU",
    connection_authority: "system_openssh",
    ssh_host_alias: "lab-gpu",
    catalog_generation: 3,
    connection_generation: 1,
    connection_state: "disconnected",
    prompt: null,
    trust: trust(),
    failure: null,
    active_project_id: null,
    core_api_major: null,
    core_openapi_sha256: null,
    core_event_schema_sha256: null,
    core_registry_sha256: null,
    created_at: NOW,
    updated_at: NOW,
    etag: ETAG,
  };
}

function projectHead() {
  return {
    schema_version: "2",
    project_head_id: "head-1",
    project_id: "project-1",
    generation: 0,
    predecessor_project_head_id: null,
    workspace_snapshot: {
      schema_version: "2",
      workspace_snapshot_id: "workspace-1",
      project_id: "project-1",
      manifest_sha256: DIGEST,
      entry_count: 2,
      byte_size: 64,
    },
    evolution_revision: {
      schema_version: "2",
      evolution_revision_id: "evolution-1",
      project_id: "project-1",
      manifest_sha256: DIGEST,
      artifact_count: 0,
    },
    runtime_context_snapshot: {
      schema_version: "2",
      runtime_context_snapshot_id: "context-1",
      project_id: "project-1",
      evolution_revision_id: "evolution-1",
      evolution_revision_manifest_sha256: DIGEST,
      registry_sha256: DIGEST,
      runtime_contract_sha256: DIGEST,
      manifest_sha256: DIGEST,
    },
    effective_execution_snapshot: {
      schema_version: "2",
      effective_execution_snapshot_id: "execution-1",
      project_id: "project-1",
      execution_mode: "codex_subscription_transcript",
      capture_mode: "transcript",
      token_level_metrics_available: false,
      producer_id: "producer-1",
      snapshot_sha256: DIGEST,
    },
    registry_sha256: DIGEST,
    manifest_sha256: DIGEST,
  };
}

function lifecycleOperation(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: "2",
    operation_id: "lifecycle-profile-connect-1",
    kind: "profile_connect",
    resource: { resource_kind: "profile", resource_id: "profile-lab" },
    request_sha256: DIGEST,
    status: "queued",
    phase: "queued",
    phase_index: 1,
    phase_total: 17,
    progress: null,
    cancellable: true,
    result: null,
    failure: null,
    log_sequence_high_watermark: 0,
    created_at: NOW,
    started_at: null,
    updated_at: NOW,
    finished_at: null,
    etag: ETAG,
    ...overrides,
  };
}

describe("Desktop Local API v2 schemas", () => {
  it("pins a sorted feature set to its canonical digest", () => {
    const version = {
      schema_version: "2",
      api_name: "openevo-desktop-local-api",
      preferred_major: 2,
      supported_majors: [2],
      mutation_major: 2,
      openapi_sha256: "f0996184595992a22ec6abd257d9040342c9d2f7a31a9882b4a0597061594760",
      event_schema_sha256: "515b6d90e9ebdf3f5b4f7c4a57a1924dc85011536d9396b1ab3a5dc73fc48b6b",
      release_version: "0.1.10",
      build_id: DIGEST,
      source_commit: "abcdef1",
      build_channel: "release",
      provider_kind: "desktop_sidecar",
      feature_flags: [
        "core_control_v2",
        "daemon_bundle_v2",
        "event_replay_v2",
        "host_key_review",
        "lifecycle_operations_v2",
        "lifecycle_process_logs_v2",
        "mutation_idempotency_v2",
        "native_askpass",
        "system_openssh_profiles",
        "task_admission_v2",
      ],
      feature_set_sha256: "67b6ad24f67de611f32c365079fcf8384c800d0855effaa64e1ff24251a7acda",
      required_core_api_major: 2,
      mutation_compatible: true,
    };

    expect(desktopVersionV2Schema.parse(version).release_version).toBe("0.1.10");
    expect(() => desktopVersionV2Schema.parse({ ...version, feature_set_sha256: DIGEST })).toThrow();
    expect(() => desktopVersionV2Schema.parse({ ...version, feature_flags: [...version.feature_flags].reverse() })).toThrow();
  });

  it("rejects unknown secret, path, and flattened SSH authority fields", () => {
    for (const forbidden of [
      { password: "canary-secret" },
      { identity_file: "/Users/example/.ssh/id_ed25519" },
      { hostname: "10.0.0.9" },
      { username: "researcher" },
      { port: 22 },
      { core_url: "http://127.0.0.1:9000" },
    ]) {
      expect(() => remoteProfileV2Schema.parse({ ...profile(), ...forbidden })).toThrow();
    }
  });

  it("rejects unsafe generations, oversized catalogs, and invalid discriminators", () => {
    expect(() => remoteProfileV2Schema.parse({ ...profile(), catalog_generation: Number.MAX_SAFE_INTEGER + 1 })).toThrow();
    expect(() => remoteProfileV2Schema.parse({ ...profile(), profile_kind: "manual_tcp" })).toThrow();
    expect(() => sshHostCatalogV2Schema.parse({
      schema_version: "2",
      catalog_generation: 1,
      hosts: Array.from({ length: 513 }, (_, index) => ({
        schema_version: "2",
        ssh_host_alias: `host-${String(index).padStart(3, "0")}`,
        availability: "selectable",
        source_kind: "literal_host",
      })),
      warnings: [],
      scanned_at: NOW,
    })).toThrow();
  });

  it("rejects profile and project-head identity drift", () => {
    expect(() => remoteProfileV2Schema.parse({ ...profile(), trust: trust(2) })).toThrow(/generation/i);
    expect(() => projectHeadRefV2Schema.parse({
      ...projectHead(),
      runtime_context_snapshot: {
        ...projectHead().runtime_context_snapshot,
        evolution_revision_id: "evolution-other",
      },
    })).toThrow(/evolution revision/i);
  });

  it("requires state active-profile references to resolve without exposing hidden authority", () => {
    const state = {
      schema_version: "2",
      profiles: [profile()],
      active_profile_id: "profile-lab",
      active_project_id: null,
      pending_operations: [],
      last_event_id: null,
      updated_at: NOW,
    };
    expect(desktopStateV2Schema.parse(state).profiles).toHaveLength(1);
    expect(() => desktopStateV2Schema.parse({ ...state, active_profile_id: "profile-missing" })).toThrow();
    expect(() => desktopStateV2Schema.parse({ ...state, ssh_config_path: "/Users/example/.ssh/config" })).toThrow();
  });

  it("binds lifecycle phases, progress, terminal results, and pending state identity", () => {
    expect(lifecycleOperationV2Schema.parse(lifecycleOperation()).status).toBe("queued");
    expect(() => lifecycleOperationV2Schema.parse(lifecycleOperation({
      created_at: "2026-07-27T08:00:00.000000002Z",
      updated_at: "2026-07-27T08:00:00.000000001Z",
    }))).toThrow(/timestamp|regress/i);
    expect(() => lifecycleOperationV2Schema.parse(lifecycleOperation({
      created_at: "2026-02-31T08:00:00Z",
      updated_at: "2026-02-31T08:00:00Z",
    }))).toThrow(/timestamp/i);
    expect(() => lifecycleOperationV2Schema.parse(lifecycleOperation({
      progress: { kind: "bytes", completed: 5, total: 4 },
    }))).toThrow(/progress|completed/i);
    expect(() => lifecycleOperationV2Schema.parse(lifecycleOperation({
      phase: "connecting",
    }))).toThrow(/phase/i);
    expect(() => lifecycleOperationV2Schema.parse(lifecycleOperation({
      status: "succeeded",
      phase: "finalizing",
      phase_index: 16,
      cancellable: false,
      finished_at: NOW,
    }))).toThrow(/result/i);

    const reference = {
      schema_version: "2",
      operation_id: "lifecycle-profile-connect-1",
      kind: "profile_connect",
      resource: { resource_kind: "profile", resource_id: "profile-lab" },
      request_sha256: DIGEST,
      status: "queued",
      phase: "queued",
      phase_index: 1,
      phase_total: 17,
      log_sequence_high_watermark: 0,
      updated_at: NOW,
      etag: ETAG,
    };
    const state = {
      schema_version: "2",
      profiles: [profile()],
      active_profile_id: "profile-lab",
      active_project_id: null,
      pending_operations: [reference],
      last_event_id: null,
      updated_at: NOW,
    };
    expect(desktopStateV2Schema.parse(state).pending_operations).toHaveLength(1);
    expect(() => desktopStateV2Schema.parse({
      ...state,
      pending_operations: [reference, reference],
    })).toThrow(/unique/i);
  });

  it("binds lifecycle log pages to one operation and a retained cursor boundary", () => {
    const page = {
      schema_version: "2",
      operation_id: "lifecycle-profile-connect-1",
      dropped_before_sequence: 3,
      items: [{
        schema_version: "2",
        operation_id: "lifecycle-profile-connect-1",
        sequence: 4,
        occurred_at: NOW,
        source: "ssh_stdout",
        text: "Checking remote runtime\n",
        truncated: false,
      }],
      next_cursor: null,
      has_more: false,
    };
    expect(lifecycleLogPageV2Schema.parse(page).items[0]?.source).toBe("ssh_stdout");
    expect(() => lifecycleLogPageV2Schema.parse({
      ...page,
      items: [{ ...page.items[0], operation_id: "lifecycle-other" }],
    })).toThrow(/another operation/i);
    expect(() => lifecycleLogPageV2Schema.parse({
      ...page,
      items: [{ ...page.items[0], sequence: 3 }],
    })).toThrow(/dropped/i);
  });

  it("strictly projects Core operations, service logs, and cache cleanup", () => {
    expect(coreOperationV2Schema.parse({
      schema_version: "2",
      operation_id: "core-operation-1",
      kind: "service_restart",
      status: "running",
      progress_completed: 2,
      progress_total: 4,
      error: null,
      created_at: NOW,
      updated_at: NOW,
      etag: ETAG,
    }).progress_total).toBe(4);
    expect(() => coreOperationV2Schema.parse({
      schema_version: "2",
      operation_id: "core-operation-1",
      kind: "service_restart",
      status: "running",
      progress_completed: 10_001,
      progress_total: 10_001,
      error: null,
      created_at: NOW,
      updated_at: NOW,
      etag: ETAG,
    })).toThrow();
    expect(cacheCleanupRequestV2Schema.parse({ schema_version: "2" }).scope)
      .toBe("safe_unreferenced");
    expect(() => cacheCleanupRequestV2Schema.parse({ schema_version: "2", scope: "all" }))
      .toThrow();
  });

  it("accepts only digest-bound lifecycle invalidation events", () => {
    const payload = {
      payload_kind: "lifecycle_operation_changed",
      operation_id: "lifecycle-profile-connect-1",
      kind: "profile_connect",
      status: "running",
      phase: "connecting",
      etag: ETAG,
      log_sequence_high_watermark: 4,
    };
    const event = {
      schema_version: "2",
      event_id: "event-lifecycle-1",
      sequence: 5,
      occurred_at: NOW,
      event_type: "lifecycle_operation_changed",
      payload_sha256: sha256Utf8V2(canonicalJsonV2(payload)),
      payload,
    };
    expect(desktopEventEnvelopeV2Schema.parse(event).event_type)
      .toBe("lifecycle_operation_changed");
    expect(() => desktopEventEnvelopeV2Schema.parse({
      ...event,
      payload: { ...payload, phase: "finalizing" },
    })).toThrow(/digest/i);
  });

  it("accepts multiline science objectives while rejecting unsafe text controls", () => {
    const config = {
      schema_version: "2",
      task: {
        title: "Compare two candidate mechanisms",
        objective: "Analyze the evidence from both mechanisms.\n\nWrite a concise conclusion with limitations.",
      },
      workspace: { kind: "scratch", display_name: "Mechanism study" },
      execution: {
        mode: "codex_subscription_transcript",
        capture_mode: "transcript",
        token_level_metrics_available: false,
        harness_id: "codex",
        codex_model: "gpt-5.6-codex",
        reasoning_effort: "high",
        token_limit: 8_192,
        task_network_allow_internet: true,
      },
      evolution: { targets: {} },
    };

    expect(scienceProjectConfigV2Schema.parse(config).task.objective).toContain("\n\n");
    expect(() => scienceProjectConfigV2Schema.parse({
      ...config,
      task: { ...config.task, objective: "Analyze\u0000hidden" },
    })).toThrow();
  });

  it("accepts only the release-owned Self-Deployed model profile", () => {
    const config = {
      schema_version: "2",
      task: { title: "Local inference", objective: "Solve the task through Core Gateway." },
      workspace: { kind: "scratch", display_name: "Self-Deployed workspace" },
      execution: {
        mode: "self-deployed",
        capture_mode: "transcript",
        token_level_metrics_available: false,
        harness_id: "codex",
        model_profile_id: "qwen3-0.6b-v1",
        token_limit: 8_192,
        task_network_allow_internet: false,
      },
      evolution: { targets: {} },
    };

    expect(scienceProjectConfigV2Schema.parse(config).execution.mode).toBe("self-deployed");
    expect(() => scienceProjectConfigV2Schema.parse({
      ...config,
      execution: { ...config.execution, model_profile_id: "Qwen/Qwen3-0.6B" },
    })).toThrow();
    expect(() => scienceProjectConfigV2Schema.parse({
      ...config,
      execution: { ...config.execution, hf_model: "Qwen/Qwen3-0.6B" },
    })).toThrow();
  });
});
