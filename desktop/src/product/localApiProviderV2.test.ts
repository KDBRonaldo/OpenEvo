import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import { DesktopApiErrorV2, type DesktopApiClientV2 } from "../api/v2/client";
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
    getProfile: vi.fn(),
    getProject: vi.fn(),
    getCoreOperation: vi.fn(),
    cancelCoreOperation: vi.fn(),
    restartService: vi.fn(),
    serviceLogs: vi.fn(),
    cleanupCaches: vi.fn(),
    createDiagnostic: vi.fn(),
    getDiagnostic: vi.fn(),
    createProfile: vi.fn(),
    createProject: vi.fn(),
    connectProfile: vi.fn(),
    getLifecycleOperationByAction: vi.fn().mockRejectedValue(new DesktopApiErrorV2(404, {
      schema_version: "2",
      code: "resource_not_found",
      summary: "The requested local resource was not found.",
      retryable: false,
      action: "none",
      affected_resource_id: null,
    })),
    getLifecycleOperation: vi.fn(),
    lifecycleOperationLogs: vi.fn(),
    acknowledgeLifecycleOperation: vi.fn(),
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
      "cancelLifecycleOperation",
      "createProject", "updateProject", "activateProject", "validateProject", "submitTask",
      "cancelTask", "retryTask", "retryTransition", "replaceTransition", "abandonTransition",
      "restartService", "cancelCoreOperation", "cleanupCaches", "createDiagnostic",
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
    const created = profile();
    vi.mocked(client.createProfile).mockResolvedValue(created);
    vi.mocked(client.getProfile).mockResolvedValue(created);
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
    const created = profile();
    vi.mocked(secondClient.createProfile).mockResolvedValue(created);
    vi.mocked(secondClient.getProfile).mockResolvedValue(created);
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

  it("keeps a direct success retryable until its exact resource is authoritatively re-read", async () => {
    const native = nativeFixture();
    const created = profile();
    const firstClient = clientFixture();
    vi.mocked(firstClient.createProfile).mockResolvedValue(created);
    vi.mocked(firstClient.getProfile).mockRejectedValue(new TypeError("authority refresh interrupted"));
    const first = createLocalApiDesktopProductProviderV2({
      client: firstClient,
      native,
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });
    const firstRefresh = await first.refresh();
    if (firstRefresh.status !== "fresh") throw new Error("fixture refresh failed");

    await expect(first.createProfile("Lab GPU", "lab-gpu", {
      actionId: "create-profile-verify-0001",
      streamEpoch: firstRefresh.snapshot.stream.epoch,
    })).rejects.toThrow(/authority refresh interrupted/i);
    expect(native.journalValue()).toContain("create-profile-verify-0001");

    const secondClient = clientFixture();
    vi.mocked(secondClient.createProfile).mockResolvedValue(created);
    vi.mocked(secondClient.getProfile).mockResolvedValue(created);
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
      idempotencyKey: "create-profile-verify-0001",
    }));
    expect(secondClient.getProfile).toHaveBeenCalledWith(created.profile_id);
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

  it("releases an exact deterministic rejection because it proves no side effect can publish", async () => {
    const native = nativeFixture();
    const client = clientFixture();
    vi.mocked(client.createProfile).mockRejectedValue(new DesktopApiErrorV2(409, {
      schema_version: "2",
      code: "profile_already_exists",
      summary: "A profile already uses this SSH alias.",
      retryable: false,
      action: "none",
      affected_resource_id: null,
    }));
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native,
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });
    const result = await provider.refresh();
    if (result.status !== "fresh") throw new Error("fixture refresh failed");

    await expect(provider.createProfile("Lab GPU", "lab-gpu", {
      actionId: "create-profile-rejected-0001",
      streamEpoch: result.snapshot.stream.epoch,
    })).rejects.toThrow(/already uses/i);

    expect(native.journalValue()).toBeNull();
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
    vi.mocked(client.getProject).mockResolvedValue({
      project_id: "project-created-1",
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
    await expect(provider.createProject(draft, {
      actionId,
      streamEpoch: secondRefresh.snapshot.stream.epoch,
    })).resolves.toMatchObject({ operation_id: projectOperation.operation_id });

    expect(client.createProject).toHaveBeenCalledWith(expect.anything(), expect.objectContaining({
      idempotencyKey: actionId,
    }));
    expect(native.settleProjectSource).not.toHaveBeenCalledWith(actionId, "adopt");
    await expect(provider.refresh()).resolves.toMatchObject({ status: "fresh" });
    expect(native.settleProjectSource).toHaveBeenCalledWith(actionId, "adopt");
    expect(native.journalValue()).toBeNull();
  });

  it("returns project creation authority immediately after HTTP 202", async () => {
    const connected = profile({ connection_state: "connected" });
    const client = clientFixture([connected]);
    const accepted = {
      ...lifecycleOperation(),
      operation_id: "lifecycle-project-create-running-1",
      kind: "project_create" as const,
      resource: { resource_kind: "project" as const, resource_id: "project-running-1" },
      status: "running" as const,
      phase: "creating_remote_project" as const,
      phase_index: 13,
      started_at: NOW,
    };
    vi.mocked(client.createProject).mockResolvedValue(accepted);
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native: nativeFixture(),
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });
    const refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("fixture refresh failed");

    await expect(provider.createProject({
      profileId: connected.profile_id,
      displayName: "Long project",
      config: {
        ...nativeProjectConfig(),
        workspace: { kind: "scratch" as const, display_name: "Long workspace" },
      },
    }, {
      actionId: "project-create-return-after-202",
      streamEpoch: refreshed.snapshot.stream.epoch,
    })).resolves.toEqual(accepted);
    expect(client.getLifecycleOperation).not.toHaveBeenCalled();
    expect(client.listProjects).not.toHaveBeenCalled();
  });

  it("recovers a lifecycle operation when its HTTP 202 response was lost", async () => {
    const connected = profile({ connection_state: "connected" });
    const native = nativeFixture();
    const firstClient = clientFixture([connected]);
    vi.mocked(firstClient.createProject).mockRejectedValue(new TypeError("response connection closed"));
    const firstProvider = createLocalApiDesktopProductProviderV2({
      client: firstClient,
      native,
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });
    const firstRefresh = await firstProvider.refresh();
    if (firstRefresh.status !== "fresh") throw new Error("fixture refresh failed");
    const actionId = "project-create-lost-response-0001";
    const draft = {
      profileId: connected.profile_id,
      displayName: "Recovered project",
      config: {
        ...nativeProjectConfig(),
        workspace: { kind: "scratch" as const, display_name: "Recovered workspace" },
      },
    };

    await expect(firstProvider.createProject(draft, {
      actionId,
      streamEpoch: firstRefresh.snapshot.stream.epoch,
    })).rejects.toThrow(/response connection closed/i);
    expect(JSON.parse(native.journalValue()!)).toMatchObject({
      entries: [{ action_id: actionId, state: "reserved", accepted_operation_id: null }],
    });

    const terminal = {
      ...lifecycleOperation(),
      operation_id: "lifecycle-project-lost-response-1",
      kind: "project_create" as const,
      resource: { resource_kind: "project" as const, resource_id: "project-recovered-1" },
      status: "succeeded" as const,
      phase: "finalizing" as const,
      phase_index: 16,
      cancellable: false,
      result: { result_kind: "project" as const, project_id: "project-recovered-1" },
      finished_at: NOW,
    };
    const recoveredClient = clientFixture([connected]);
    vi.mocked(recoveredClient.getLifecycleOperationByAction).mockResolvedValue(terminal);
    vi.mocked(recoveredClient.getProject).mockResolvedValue({
      project_id: "project-recovered-1",
    } as never);
    const recoveredProvider = createLocalApiDesktopProductProviderV2({
      client: recoveredClient,
      native,
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });

    await expect(recoveredProvider.refresh()).resolves.toMatchObject({ status: "fresh" });
    expect(recoveredClient.getLifecycleOperationByAction).toHaveBeenCalledWith(
      actionId,
      "project_create",
    );
    expect(recoveredClient.acknowledgeLifecycleOperation).toHaveBeenCalledWith(
      terminal.operation_id,
      expect.objectContaining({ expected_terminal_status: "succeeded" }),
      expect.objectContaining({ ifMatch: terminal.etag }),
    );
    expect(native.journalValue()).toBeNull();
  });

  it("recovers a terminal lifecycle operation, clears native intent first, and acknowledges once", async () => {
    const disconnected = profile();
    const connected = profile({
      connection_generation: 5,
      connection_state: "connected",
      etag: `"${"d".repeat(64)}"`,
      updated_at: "2026-07-23T06:00:01Z",
    });
    const running = lifecycleOperation();
    const terminal = {
      ...running,
      status: "succeeded" as const,
      phase: "finalizing" as const,
      phase_index: 16,
      progress: null,
      cancellable: false,
      result: {
        result_kind: "profile" as const,
        profile_id: connected.profile_id,
        connection_generation: connected.connection_generation,
      },
      updated_at: "2026-07-23T06:00:01Z",
      finished_at: "2026-07-23T06:00:01Z",
      etag: `"${"e".repeat(64)}"`,
    };
    const native = nativeFixture();
    const client = clientFixture([disconnected]);
    vi.mocked(client.connectProfile).mockResolvedValue(running);
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native,
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });
    const first = await provider.refresh();
    if (first.status !== "fresh") throw new Error("fixture refresh failed");
    await provider.connectProfile(disconnected.profile_id, {
      actionId: "connect-profile-recovery-0001",
      streamEpoch: first.snapshot.stream.epoch,
    });

    vi.mocked(client.state).mockResolvedValue({
      ...state([connected]),
      pending_operations: [{
        schema_version: "2",
        operation_id: terminal.operation_id,
        kind: terminal.kind,
        resource: terminal.resource,
        request_sha256: terminal.request_sha256,
        status: terminal.status,
        phase: terminal.phase,
        phase_index: terminal.phase_index,
        phase_total: terminal.phase_total,
        log_sequence_high_watermark: terminal.log_sequence_high_watermark,
        updated_at: terminal.updated_at,
        etag: terminal.etag,
      }],
      updated_at: terminal.updated_at,
    });
    vi.mocked(client.listProfiles).mockResolvedValue({
      schema_version: "2",
      items: [connected],
      next_cursor: null,
      has_more: false,
    });
    vi.mocked(client.getLifecycleOperation).mockResolvedValue(terminal);
    vi.mocked(client.lifecycleOperationLogs).mockResolvedValue({
      schema_version: "2",
      operation_id: terminal.operation_id,
      dropped_before_sequence: 0,
      items: [],
      next_cursor: null,
      has_more: false,
    });
    vi.mocked(client.getProfile).mockResolvedValue(connected);
    vi.mocked(client.acknowledgeLifecycleOperation).mockImplementation(async () => {
      native.callOrder.push("acknowledge");
    });

    const recovered = await provider.refresh();

    expect(recovered.status).toBe("fresh");
    expect(native.journalValue()).toBeNull();
    expect(client.acknowledgeLifecycleOperation).toHaveBeenCalledTimes(1);
    const lastCas = native.callOrder.lastIndexOf("journal-cas");
    expect(lastCas).toBeLessThan(native.callOrder.indexOf("acknowledge"));
  });

  it("retains diagnostic identity until the exact terminal diagnostic is reconciled", async () => {
    const connected = profile({ connection_state: "connected" });
    const native = nativeFixture();
    const client = clientFixture([connected]);
    vi.mocked(client.state).mockResolvedValue({
      ...state([connected]),
      active_profile_id: connected.profile_id,
    });
    const diagnostic = {
      schema_version: "2" as const,
      diagnostic_id: "diagnostic-system-1",
      scope: "system" as const,
      resource_id: null,
      status: "ready" as const,
      artifact_id: "artifact-diagnostic-1",
      created_at: NOW,
      updated_at: NOW,
      etag: ETAG,
    };
    vi.mocked(client.createDiagnostic).mockResolvedValue(diagnostic);
    vi.mocked(client.getDiagnostic).mockResolvedValue(diagnostic);
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native,
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });
    const initial = await provider.refresh();
    if (initial.status !== "fresh") throw new Error("fixture refresh failed");

    await provider.createDiagnostic(
      { scope: "system", resource_id: null },
      { actionId: "diagnostic-system-action-0001", streamEpoch: initial.snapshot.stream.epoch },
    );

    expect(JSON.parse(native.journalValue()!)).toMatchObject({
      entries: [{
        action_id: "diagnostic-system-action-0001",
        accepted_operation_id: diagnostic.diagnostic_id,
        state: "accepted",
      }],
    });
    await provider.refresh();
    expect(native.journalValue()).toBeNull();
    expect(provider.listDiagnostics()).toEqual([diagnostic]);
    expect(client.getDiagnostic).toHaveBeenCalledWith(diagnostic.diagnostic_id);
  });
});
