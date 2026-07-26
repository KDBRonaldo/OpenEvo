import { describe, expect, it } from "vitest";
import {
  desktopStateV2Schema,
  desktopVersionV2Schema,
  projectHeadRefV2Schema,
  remoteProfileV2Schema,
  scienceProjectConfigV2Schema,
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

describe("Desktop Local API v2 schemas", () => {
  it("pins a sorted feature set to its canonical digest", () => {
    const version = {
      schema_version: "2",
      api_name: "openevo-desktop-local-api",
      preferred_major: 2,
      supported_majors: [2],
      mutation_major: 2,
      openapi_sha256: "987116bff9919930af0177567b4e2a549b3acc2e4dcf1780a1bccccc6530f672",
      event_schema_sha256: "bc1dbc7b3bf7a68e02ba87adf35bd75f511382bf665afc33cae436110d8aea28",
      release_version: "0.1.9",
      build_id: DIGEST,
      source_commit: "abcdef1",
      build_channel: "release",
      provider_kind: "desktop_sidecar",
      feature_flags: [
        "core_control_v2",
        "daemon_bundle_v2",
        "event_replay_v2",
        "host_key_review",
        "native_askpass",
        "system_openssh_profiles",
        "task_admission_v2",
      ],
      feature_set_sha256: "026eb1f1eecd219a6bf282f6e0063bf2e19d018619a934487eec3f151b66af9b",
      required_core_api_major: 2,
      mutation_compatible: true,
    };

    expect(desktopVersionV2Schema.parse(version).release_version).toBe("0.1.9");
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
      last_event_id: null,
      updated_at: NOW,
    };
    expect(desktopStateV2Schema.parse(state).profiles).toHaveLength(1);
    expect(() => desktopStateV2Schema.parse({ ...state, active_profile_id: "profile-missing" })).toThrow();
    expect(() => desktopStateV2Schema.parse({ ...state, ssh_config_path: "/Users/example/.ssh/config" })).toThrow();
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
});
