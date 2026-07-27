import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import type { DesktopApiClientV2 } from "../api/v2/client";
import type {
  DesktopStateV2,
  RemoteWorkspaceProfileV2,
  ScienceProjectConfigV2,
  SshHostCatalogV2,
} from "../api/v2/schemas";
import {
  createLocalApiDesktopProductProviderV2,
  type LocalApiNativeBridgeV2,
} from "./localApiProviderV2";

const NOW = "2026-07-23T06:00:00Z";
const DIGEST = "a".repeat(64);
const ETAG = `"${"b".repeat(64)}"`;

function profile(
  overrides: Partial<RemoteWorkspaceProfileV2> = {},
): RemoteWorkspaceProfileV2 {
  return {
    schema_version: "2",
    profile_kind: "system_openssh",
    profile_id: "profile-lab",
    display_name: "Lab GPU",
    connection_authority: "system_openssh",
    ssh_host_alias: "lab-gpu",
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

function catalog(): SshHostCatalogV2 {
  return {
    schema_version: "2",
    catalog_generation: 3,
    hosts: [{
      schema_version: "2",
      ssh_host_alias: "lab-gpu",
      availability: "selectable",
      source_kind: "literal_host",
    }],
    warnings: [],
    scanned_at: NOW,
  };
}

function state(profiles: RemoteWorkspaceProfileV2[] = []): DesktopStateV2 {
  return {
    schema_version: "2",
    profiles,
    active_profile_id: null,
    active_project_id: null,
    pending_operations: [],
    last_event_id: null,
    updated_at: NOW,
  };
}

function nativeProjectConfig(): ScienceProjectConfigV2 {
  return {
    schema_version: "2",
    task: { title: "Research task", objective: "Analyze the selected workspace." },
    workspace: { kind: "native_folder_snapshot", display_name: "Research workspace" },
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
  };
}

function lifecycleOperation() {
  return {
    schema_version: "2" as const,
    operation_id: "lifecycle-profile-connect-1",
    kind: "profile_connect" as const,
    resource: { resource_kind: "profile" as const, resource_id: "profile-lab" },
    request_sha256: DIGEST,
    status: "queued" as const,
    phase: "queued" as const,
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
  };
}

function nativeLifecycleOperation(overrides: Record<string, unknown> = {}) {
  return {
    ...lifecycleOperation(),
    operation_id: "lifecycle-native-workspace-1",
    kind: "native_workspace_prepare",
    resource: { resource_kind: "native_workspace", resource_id: "workspace-import-1" },
    status: "succeeded",
    phase: "finalizing",
    phase_index: 16,
    cancellable: false,
    result: {
      result_kind: "native_workspace",
      import_id: "workspace-import-1",
      content_sha256: DIGEST,
      byte_size: 1_024,
      entry_count: 1,
      extracted_byte_size: 32,
      display_name: "My workspace",
    },
    finished_at: NOW,
    ...overrides,
  };
}

function clientFixture(profiles: RemoteWorkspaceProfileV2[] = []) {
  const client = {
    state: vi.fn().mockResolvedValue(state(profiles)),
    listSshHosts: vi.fn().mockResolvedValue(catalog()),
    listProfiles: vi.fn().mockResolvedValue({
      schema_version: "2",
      items: profiles,
      next_cursor: null,
      has_more: false,
    }),
    listProjects: vi.fn(),
    listTasks: vi.fn(),
    listServices: vi.fn(),
    projectCapabilities: vi.fn(),
    taskTimeline: vi.fn(),
    taskArtifacts: vi.fn(),
    getTransition: vi.fn(),
    createProfile: vi.fn(),
    createProject: vi.fn(),
    connectProfile: vi.fn(),
    getLifecycleOperation: vi.fn(),
    eventStreamRequest: vi.fn(),
  } as unknown as DesktopApiClientV2;
  return client;
}

function nativeFixture(
  selected: unknown = nativeLifecycleOperation(),
): LocalApiNativeBridgeV2 & { readonly journalValue: () => string | null; readonly callOrder: string[] } {
  let journalValue: string | null = null;
  const callOrder: string[] = [];
  return {
    callOrder,
    journalValue: () => journalValue,
    selectProjectSource: vi.fn().mockResolvedValue(selected),
    cancelProjectSource: vi.fn(),
    settleProjectSource: vi.fn(),
    readMutationIntentJournalV2: vi.fn(async () => {
      callOrder.push("journal-read");
      return journalValue;
    }),
    compareAndSwapMutationIntentJournalV2: vi.fn(async (expectedValue, newValue) => {
      callOrder.push("journal-cas");
      if (expectedValue !== journalValue) throw { code: "mutation_intent_journal_conflict" };
      journalValue = newValue;
    }),
  };
}

describe("Desktop v2 product provider", () => {
  it("routes every request-creating v2 provider mutation through the durable coordinator", () => {
    const source = readFileSync(new URL("./localApiProviderV2.ts", import.meta.url), "utf8");
    const coordinatedMethods = [
      "rescanSshHosts", "createProfile", "renameProfile", "deleteProfile", "rebindProfile",
      "connectProfile", "disconnectProfile", "reviewHostKey", "selectNativeWorkspace",
      "createProject", "updateProject", "activateProject", "validateProject", "submitTask",
      "cancelTask", "retryTask", "retryTransition", "replaceTransition", "abandonTransition",
      "restartService", "createDiagnostic",
    ] as const;
    for (const method of coordinatedMethods) {
      const start = source.indexOf(`  async ${method}(`);
      const nextMethod = source.indexOf("\n  async ", start + 1);
      expect(start, `${method} implementation`).toBeGreaterThanOrEqual(0);
      expect(source.slice(start, nextMethod < 0 ? undefined : nextMethod), `${method} coordinator coverage`).toContain("this.dispatchMutationV2({");
    }
  });

  it("does not call Core collections before an active project tunnel exists", async () => {
    const client = clientFixture();
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native: nativeFixture(),
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });

    const result = await provider.refresh();

    expect(result.status).toBe("fresh");
    expect(client.listProjects).not.toHaveBeenCalled();
    expect(client.listTasks).not.toHaveBeenCalled();
    expect(client.listServices).not.toHaveBeenCalled();
  });

  it("refreshes completed tasks without calling the unavailable artifact collection", async () => {
    const current = profile({
      connection_state: "connected",
      active_project_id: "project-1",
      core_api_major: 2,
      core_openapi_sha256: DIGEST,
      core_event_schema_sha256: DIGEST,
      core_registry_sha256: DIGEST,
    });
    const client = clientFixture([current]);
    vi.mocked(client.state).mockResolvedValue({
      ...state([current]),
      active_profile_id: current.profile_id,
      active_project_id: "project-1",
    });
    vi.mocked(client.listProjects).mockResolvedValue({
      schema_version: "2",
      items: [{
        project_id: "project-1",
        config: { execution: { mode: "codex_subscription_transcript" } },
      }],
      next_cursor: null,
      has_more: false,
    } as never);
    vi.mocked(client.listTasks).mockResolvedValue({
      schema_version: "2",
      items: [{
        task_id: "task-1",
        project_id: "project-1",
        state: "completed",
        successor_transition: null,
      }],
      next_cursor: null,
      has_more: false,
    } as never);
    vi.mocked(client.listServices).mockResolvedValue({
      schema_version: "2",
      items: [],
      next_cursor: null,
      has_more: false,
    });
    vi.mocked(client.projectCapabilities).mockResolvedValue({
      project_id: "project-1",
      execution_mode: "codex_subscription_transcript",
      registry_sha256: DIGEST,
    } as never);
    vi.mocked(client.taskTimeline).mockResolvedValue({
      schema_version: "2",
      items: [],
      next_cursor: null,
      has_more: false,
    });
    vi.mocked(client.taskArtifacts).mockRejectedValue(new Error("route unavailable"));

    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native: nativeFixture(),
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });

    const result = await provider.refresh();

    expect(result.status).toBe("fresh");
    if (result.status !== "fresh") throw new Error("fixture refresh failed");
    expect(result.snapshot.tasks).toHaveLength(1);
    expect(result.snapshot.artifacts).toEqual([]);
    expect(client.taskArtifacts).not.toHaveBeenCalled();
  });

  it("creates a profile from an alias only and uses catalog generation authority", async () => {
    const client = clientFixture();
    vi.mocked(client.createProfile).mockResolvedValue(profile());
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native: nativeFixture(),
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });
    const result = await provider.refresh();
    if (result.status !== "fresh") throw new Error("fixture refresh failed");

    await provider.createProfile("Lab GPU", "lab-gpu", {
      actionId: "create-profile-lab-0001",
      streamEpoch: result.snapshot.stream.epoch,
    });

    expect(client.createProfile).toHaveBeenCalledWith({
      schema_version: "2",
      display_name: "Lab GPU",
      connection_authority: "system_openssh",
      ssh_host_alias: "lab-gpu",
    }, {
      resourceGeneration: 3,
      idempotencyKey: "create-profile-lab-0001",
    });
    expect(JSON.stringify(vi.mocked(client.createProfile).mock.calls[0])).not.toMatch(
      /password|username|identity|private.key|host_path/i,
    );
  });

  it("derives connect CAS authority from the current profile", async () => {
    const current = profile();
    const client = clientFixture([current]);
    vi.mocked(client.connectProfile).mockResolvedValue(lifecycleOperation());
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native: nativeFixture(),
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });
    const result = await provider.refresh();
    if (result.status !== "fresh") throw new Error("fixture refresh failed");

    await provider.connectProfile(current.profile_id, {
      actionId: "connect-profile-lab-0001",
      streamEpoch: result.snapshot.stream.epoch,
    });

    expect(client.connectProfile).toHaveBeenCalledWith(current.profile_id, {
      schema_version: "2",
      expected_connection_generation: 4,
    }, {
      resourceGeneration: 4,
      ifMatch: ETAG,
      idempotencyKey: "connect-profile-lab-0001",
    });
  });

  it("keeps native import references out of the renderer model and rejects path canaries", async () => {
    const connected = profile({ connection_state: "connected" });
    const provider = createLocalApiDesktopProductProviderV2({
      client: clientFixture([connected]),
      native: nativeFixture(),
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });
    const result = await provider.refresh();
    if (result.status !== "fresh") throw new Error("fixture refresh failed");
    const intent = {
      kind: "native_folder_snapshot" as const,
      actionId: "select-workspace-0001",
      streamEpoch: result.snapshot.stream.epoch,
      draft: {
        profileId: "profile-lab",
        displayName: "Native project",
        config: nativeProjectConfig(),
      },
      profileAuthority: {
        profileId: "profile-lab",
        connectionGeneration: 4,
        etag: ETAG,
      },
    };

    await expect(provider.selectNativeWorkspace(intent)).resolves.toEqual({
      kind: "native_folder_snapshot",
      display_name: "My workspace",
    });

    const poisoned = createLocalApiDesktopProductProviderV2({
      client: clientFixture([connected]),
      native: nativeFixture(nativeLifecycleOperation({
        selected_path: "/Users/researcher/secret-project",
      })),
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });
    const poisonedRefresh = await poisoned.refresh();
    if (poisonedRefresh.status !== "fresh") throw new Error("fixture refresh failed");
    await expect(poisoned.selectNativeWorkspace({
      ...intent,
      streamEpoch: poisonedRefresh.snapshot.stream.epoch,
    })).rejects.toThrow();
  });

  it("rejects a stale renderer action before issuing a request", async () => {
    const client = clientFixture();
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native: nativeFixture(),
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });
    const result = await provider.refresh();
    if (result.status !== "fresh") throw new Error("fixture refresh failed");

    await expect(provider.createProfile("Lab GPU", "lab-gpu", {
      actionId: "create-profile-lab-0001",
      streamEpoch: result.snapshot.stream.epoch + 1,
    })).rejects.toThrow(/state changed/i);
    expect(client.createProfile).not.toHaveBeenCalled();
  });

  it("persists before transport and reuses the exact action identity after an ambiguous relaunch", async () => {
    const native = nativeFixture();
    const callOrder = native.callOrder;
    const firstClient = clientFixture();
    vi.mocked(firstClient.createProfile).mockImplementation(async () => {
      callOrder.push("transport");
      throw new TypeError("connection reset after request upload");
    });
    const first = createLocalApiDesktopProductProviderV2({
      client: firstClient,
      native,
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });
    const firstRefresh = await first.refresh();
    if (firstRefresh.status !== "fresh") throw new Error("fixture refresh failed");

    await expect(first.createProfile("Lab GPU", "lab-gpu", {
      actionId: "create-profile-ambiguous-0001",
      streamEpoch: firstRefresh.snapshot.stream.epoch,
    })).rejects.toThrow(/connection reset/i);

    expect(callOrder.indexOf("journal-cas")).toBeLessThan(callOrder.indexOf("transport"));
    expect(native.journalValue()).toContain("create-profile-ambiguous-0001");

    const secondClient = clientFixture();
    vi.mocked(secondClient.createProfile).mockResolvedValue(profile());
    const relaunched = createLocalApiDesktopProductProviderV2({
      client: secondClient,
      native,
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });
    const secondRefresh = await relaunched.refresh();
    if (secondRefresh.status !== "fresh") throw new Error("fixture refresh failed");
    await relaunched.createProfile("Lab GPU", "lab-gpu", {
      actionId: "create-profile-new-click-0002",
      streamEpoch: secondRefresh.snapshot.stream.epoch,
    });

    expect(secondClient.createProfile).toHaveBeenCalledWith(expect.anything(), expect.objectContaining({
      idempotencyKey: "create-profile-ambiguous-0001",
    }));
    expect(native.journalValue()).toBeNull();
  });

  it("rejects changed intent on an unresolved scope before a second transport", async () => {
    const native = nativeFixture();
    const client = clientFixture();
    vi.mocked(client.createProfile).mockRejectedValue(new TypeError("ambiguous transport"));
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native,
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });
    const result = await provider.refresh();
    if (result.status !== "fresh") throw new Error("fixture refresh failed");
    await expect(provider.createProfile("Lab GPU", "lab-gpu", {
      actionId: "create-profile-ambiguous-0001",
      streamEpoch: result.snapshot.stream.epoch,
    })).rejects.toThrow();

    await expect(provider.createProfile("Changed name", "lab-gpu", {
      actionId: "create-profile-changed-0002",
      streamEpoch: result.snapshot.stream.epoch,
    })).rejects.toThrow(/different request or authority/i);
    expect(client.createProfile).toHaveBeenCalledTimes(1);
  });

  it("durably binds an accepted lifecycle operation before returning it", async () => {
    const current = profile();
    const native = nativeFixture();
    const client = clientFixture([current]);
    vi.mocked(client.connectProfile).mockResolvedValue(lifecycleOperation());
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native,
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });
    const result = await provider.refresh();
    if (result.status !== "fresh") throw new Error("fixture refresh failed");

    await provider.connectProfile(current.profile_id, {
      actionId: "connect-profile-durable-0001",
      streamEpoch: result.snapshot.stream.epoch,
    });

    expect(JSON.parse(native.journalValue()!)).toMatchObject({
      entries: [{
        action_id: "connect-profile-durable-0001",
        accepted_operation_id: "lifecycle-profile-connect-1",
        state: "accepted",
      }],
    });
  });

  it("carries one durable action identity across native preparation and remote project creation", async () => {
    const connected = profile({ connection_state: "connected" });
    const native = nativeFixture();
    const client = clientFixture([connected]);
    const projectOperation = {
      ...lifecycleOperation(),
      operation_id: "lifecycle-project-create-1",
      kind: "project_create" as const,
      resource: { resource_kind: "project" as const, resource_id: "project-created-1" },
      status: "succeeded" as const,
      phase: "finalizing" as const,
      phase_index: 16,
      cancellable: false,
      result: { result_kind: "project" as const, project_id: "project-created-1" },
      finished_at: NOW,
    };
    vi.mocked(client.createProject).mockResolvedValue(projectOperation);
    vi.mocked(client.listProjects).mockResolvedValue({
      schema_version: "2",
      items: [{ project_id: "project-created-1" }],
      next_cursor: null,
      has_more: false,
    } as never);
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native,
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });
    const firstRefresh = await provider.refresh();
    if (firstRefresh.status !== "fresh") throw new Error("fixture refresh failed");
    const actionId = "project-native-chain-0001";
    const draft = {
      profileId: connected.profile_id,
      displayName: "Native project",
      config: nativeProjectConfig(),
    };

    await provider.selectNativeWorkspace({
      kind: "native_folder_snapshot",
      actionId,
      streamEpoch: firstRefresh.snapshot.stream.epoch,
      draft,
      profileAuthority: {
        profileId: connected.profile_id,
        connectionGeneration: connected.connection_generation,
        etag: connected.etag,
      },
    });
    expect(JSON.parse(native.journalValue()!)).toMatchObject({
      entries: [{
        action_id: actionId,
        mutation_kind: "project_create",
        chain_step: "project_create",
        completed_operation_ids: ["lifecycle-native-workspace-1"],
      }],
    });

    const secondRefresh = await provider.refresh();
    if (secondRefresh.status !== "fresh") throw new Error("fixture refresh failed");
    await provider.createProject(draft, {
      actionId,
      streamEpoch: secondRefresh.snapshot.stream.epoch,
    });

    expect(client.createProject).toHaveBeenCalledWith(expect.anything(), expect.objectContaining({
      idempotencyKey: actionId,
    }));
    expect(native.settleProjectSource).toHaveBeenCalledWith(actionId, "adopt");
    expect(native.journalValue()).toBeNull();
  });
});
