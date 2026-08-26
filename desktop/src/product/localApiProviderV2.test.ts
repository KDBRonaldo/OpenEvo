import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import {
  DesktopApiErrorV2,
  type DesktopApiClientV2,
  type FetchLikeV2,
} from "../api/v2/client";
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

function runningProjectCreateOperation(operationId: string, projectId: string) {
  return {
    ...lifecycleOperation(),
    operation_id: operationId,
    kind: "project_create" as const,
    resource: { resource_kind: "project" as const, resource_id: projectId },
    status: "running" as const,
    phase: "opening_project_tunnel" as const,
    phase_index: 10,
    progress: { kind: "indeterminate" as const },
    cancellable: true,
    started_at: NOW,
  };
}

function lifecycleReference(operation: ReturnType<typeof runningProjectCreateOperation>) {
  return {
    schema_version: operation.schema_version,
    operation_id: operation.operation_id,
    kind: operation.kind,
    resource: operation.resource,
    request_sha256: operation.request_sha256,
    status: operation.status,
    phase: operation.phase,
    phase_index: operation.phase_index,
    phase_total: operation.phase_total,
    log_sequence_high_watermark: operation.log_sequence_high_watermark,
    updated_at: operation.updated_at,
    etag: operation.etag,
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
    cancelLifecycleOperation: vi.fn(),
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

  it("keeps local connection authority readable while a persisted project tunnel is disconnected", async () => {
    const disconnected = profile({ active_project_id: "project-lab" });
    const client = clientFixture([disconnected]);
    vi.mocked(client.state).mockResolvedValue({
      ...state([disconnected]),
      active_profile_id: disconnected.profile_id,
      active_project_id: "project-lab",
    });
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native: nativeFixture(),
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });

    const result = await provider.refresh();

    expect(result.status).toBe("fresh");
    if (result.status !== "fresh") throw new Error("expected a fresh local-only snapshot");
    expect(result.snapshot.state.active_project_id).toBe("project-lab");
    expect(result.snapshot.projects).toEqual([]);
    expect(client.listProjects).not.toHaveBeenCalled();
    expect(client.listTasks).not.toHaveBeenCalled();
    expect(client.listServices).not.toHaveBeenCalled();
  });

  it("does not read the previous project through a project-create tunnel hand-off", async () => {
    const current = profile({
      connection_state: "connected",
      active_project_id: "project-old",
      core_api_major: 2,
      core_openapi_sha256: DIGEST,
      core_event_schema_sha256: DIGEST,
      core_registry_sha256: DIGEST,
    });
    const client = clientFixture([current]);
    const running = runningProjectCreateOperation(
      "lifecycle-project-create-handoff-1",
      "project-new",
    );
    vi.mocked(client.state).mockResolvedValue({
      ...state([current]),
      active_profile_id: current.profile_id,
      active_project_id: "project-old",
      pending_operations: [lifecycleReference(running)],
    });
    vi.mocked(client.getLifecycleOperation).mockResolvedValue(running);
    vi.mocked(client.lifecycleOperationLogs).mockResolvedValue({
      schema_version: "2",
      operation_id: running.operation_id,
      dropped_before_sequence: 0,
      items: [],
      next_cursor: null,
      has_more: false,
    });
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native: nativeFixture(),
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });

    const result = await provider.refresh();

    expect(result.status).toBe("fresh");
    if (result.status !== "fresh") throw new Error("expected a fresh transition snapshot");
    expect(result.snapshot.state.active_project_id).toBe("project-old");
    expect(result.snapshot.projects).toEqual([]);
    expect(client.listProjects).not.toHaveBeenCalled();
    expect(client.listTasks).not.toHaveBeenCalled();
    expect(client.listServices).not.toHaveBeenCalled();
  });

  it("re-reads local authority when a project tunnel changes after state was read", async () => {
    const current = profile({
      connection_state: "connected",
      active_project_id: "project-old",
      core_api_major: 2,
      core_openapi_sha256: DIGEST,
      core_event_schema_sha256: DIGEST,
      core_registry_sha256: DIGEST,
    });
    const client = clientFixture([current]);
    const running = runningProjectCreateOperation(
      "lifecycle-project-create-race-1",
      "project-new",
    );
    const stableState = {
      ...state([current]),
      active_profile_id: current.profile_id,
      active_project_id: "project-old",
    };
    const transitionState = {
      ...stableState,
      pending_operations: [lifecycleReference(running)],
    };
    vi.mocked(client.state)
      .mockResolvedValueOnce(stableState)
      .mockResolvedValue(transitionState);
    vi.mocked(client.listProjects).mockRejectedValue(new DesktopApiErrorV2(409, {
      schema_version: "2",
      code: "active_project_mismatch",
      summary: "The requested resource does not belong to the active project tunnel.",
      retryable: true,
      action: "reconnect",
      affected_resource_id: "project-old",
    }));
    vi.mocked(client.getLifecycleOperation).mockResolvedValue(running);
    vi.mocked(client.lifecycleOperationLogs).mockResolvedValue({
      schema_version: "2",
      operation_id: running.operation_id,
      dropped_before_sequence: 0,
      items: [],
      next_cursor: null,
      has_more: false,
    });
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native: nativeFixture(),
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });

    const result = await provider.refresh();

    expect(result.status).toBe("fresh");
    expect(client.state).toHaveBeenCalledTimes(2);
    expect(client.listProjects).toHaveBeenCalledTimes(1);
  });

  it("absorbs a retryable Core snapshot outage instead of failing the user action", async () => {
    const connected = profile({
      connection_state: "connected",
      active_project_id: "project-old",
      core_api_major: 2,
      core_openapi_sha256: DIGEST,
      core_event_schema_sha256: DIGEST,
      core_registry_sha256: DIGEST,
    });
    const disconnected = profile();
    const client = clientFixture([connected]);
    vi.mocked(client.state)
      .mockResolvedValueOnce({
        ...state([connected]),
        active_profile_id: connected.profile_id,
        active_project_id: "project-old",
      })
      .mockResolvedValue(state([disconnected]));
    vi.mocked(client.listProfiles)
      .mockResolvedValueOnce({
        schema_version: "2",
        items: [connected],
        next_cursor: null,
        has_more: false,
      })
      .mockResolvedValue({
        schema_version: "2",
        items: [disconnected],
        next_cursor: null,
        has_more: false,
      });
    vi.mocked(client.listProjects).mockRejectedValueOnce(new DesktopApiErrorV2(503, {
      schema_version: "2",
      code: "core_connection_failed",
      summary: "Desktop could not reach the active project tunnel.",
      retryable: true,
      action: "reconnect",
      affected_resource_id: "project-old",
    }));
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native: nativeFixture(),
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });

    const result = await provider.refresh();

    expect(result.status).toBe("fresh");
    if (result.status !== "fresh") throw new Error("expected the transient outage to recover");
    expect(result.snapshot.state.active_project_id).toBeNull();
    expect(client.state).toHaveBeenCalledTimes(2);
    expect(client.listProjects).toHaveBeenCalledTimes(1);
  });

  it("loads one active-project tunnel collection at a time", () => {
    const source = readFileSync(new URL("./localApiProviderV2.ts", import.meta.url), "utf8");
    const start = source.indexOf("const projects = await collectPages");
    const tasks = source.indexOf("const tasks = await collectPages", start);
    const services = source.indexOf("const services = await collectPages", tasks);
    const capability = source.indexOf("const capability = await", services);

    expect(start).toBeGreaterThanOrEqual(0);
    expect(tasks).toBeGreaterThan(start);
    expect(services).toBeGreaterThan(tasks);
    expect(capability).toBeGreaterThan(services);
    expect(source.slice(start, capability)).not.toContain("Promise.all");
  });

  it("keeps inactive projects in the catalog while loading active-project authority", async () => {
    const current = profile({
      connection_state: "connected",
      active_project_id: "project-active",
      core_api_major: 2,
      core_openapi_sha256: DIGEST,
      core_event_schema_sha256: DIGEST,
      core_registry_sha256: DIGEST,
    });
    const client = clientFixture([current]);
    vi.mocked(client.state).mockResolvedValue({
      ...state([current]),
      active_profile_id: current.profile_id,
      active_project_id: "project-active",
    });
    vi.mocked(client.listProjects).mockResolvedValue({
      schema_version: "2",
      items: [
        {
          project_id: "project-old",
          display_name: "Older project",
          config: { execution: { mode: "codex_subscription_transcript" } },
        },
        {
          project_id: "project-active",
          display_name: "Active project",
          config: { execution: { mode: "codex_subscription_transcript" } },
        },
      ],
      next_cursor: null,
      has_more: false,
    } as never);
    vi.mocked(client.listTasks).mockResolvedValue({
      schema_version: "2",
      items: [],
      next_cursor: null,
      has_more: false,
    });
    vi.mocked(client.listServices).mockResolvedValue({
      schema_version: "2",
      items: [],
      next_cursor: null,
      has_more: false,
    });
    vi.mocked(client.projectCapabilities).mockResolvedValue({
      project_id: "project-active",
      execution_mode: "codex_subscription_transcript",
      registry_sha256: DIGEST,
    } as never);

    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native: nativeFixture(),
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });

    const result = await provider.refresh();

    expect(result.status).toBe("fresh");
    if (result.status !== "fresh") throw new Error("fixture refresh failed");
    expect(result.snapshot.projects.map((project) => project.project_id)).toEqual([
      "project-old",
      "project-active",
    ]);
    expect(client.listTasks).toHaveBeenCalledWith({ limit: 100 });
    expect(client.projectCapabilities).toHaveBeenCalledWith("project-active");
  });

  it("preserves contract validation details in refresh failures", async () => {
    const localProfile = profile();
    const client = clientFixture([]);
    vi.mocked(client.state).mockResolvedValue(state([localProfile]));
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native: nativeFixture(),
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });

    const result = await provider.refresh();

    expect(result.status).toBe("error");
    if (result.status !== "error") throw new Error("expected an error snapshot");
    expect(result.stream.error).toMatchObject({
      code: "desktop_snapshot_invalid",
      summary: "Desktop state and profile collection disagree",
      retryable: true,
      action: "retry",
    });
  });

  it("retries one transient local authority race before failing the refresh", async () => {
    const localProfile = profile();
    const client = clientFixture([localProfile]);
    vi.mocked(client.listProfiles)
      .mockResolvedValueOnce({
        schema_version: "2",
        items: [],
        next_cursor: null,
        has_more: false,
      })
      .mockResolvedValue({
        schema_version: "2",
        items: [localProfile],
        next_cursor: null,
        has_more: false,
      });
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native: nativeFixture(),
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });

    const result = await provider.refresh();

    expect(result.status).toBe("fresh");
    expect(client.state).toHaveBeenCalledTimes(2);
    expect(client.listSshHosts).toHaveBeenCalledTimes(2);
    expect(client.listProfiles).toHaveBeenCalledTimes(2);
  });

  it("does not let an event subscription stale the first snapshot refresh", async () => {
    let resolveState!: (value: DesktopStateV2) => void;
    const pendingState = new Promise<DesktopStateV2>((resolve) => {
      resolveState = resolve;
    });
    const client = clientFixture();
    vi.mocked(client.state)
      .mockImplementationOnce(() => pendingState)
      .mockResolvedValue(state());
    vi.mocked(client.eventStreamRequest).mockResolvedValue({
      url: "http://127.0.0.1/desktop/v2/events",
      headers: {},
    } as never);
    const eventPayload = {
      payload_kind: "ssh_host_catalog_changed",
      catalog_generation: 3,
      host_count: 2,
      warning_count: 1,
    } as const;
    const event = {
      schema_version: "2",
      event_id: "event-3",
      sequence: 3,
      occurred_at: NOW,
      event_type: "ssh_host_catalog_changed",
      payload_sha256: "a0e03db5caadb43ec812f99759f0ba45ef2e7f981508b4d5ed0a0870be25e63e",
      payload: eventPayload,
    } as const;
    const fetch = vi.fn<FetchLikeV2>()
      .mockResolvedValueOnce(new Response([
        `id: ${event.event_id}`,
        `event: ${event.event_type}`,
        `data: ${JSON.stringify(event)}`,
        "",
        "",
      ].join("\n"), {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      }))
      .mockResolvedValue(new Response("", {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      }));
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native: nativeFixture(),
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
      fetch,
      reconnectDelaysMs: [0],
    });
    const firstRefresh = provider.refresh();
    const listenerRefreshes: Promise<unknown>[] = [];
    const unsubscribe = provider.subscribe(() => {
      listenerRefreshes.push(provider.refresh());
    });
    await new Promise((resolve) => setTimeout(resolve, 0));
    resolveState(state());
    const result = await firstRefresh;
    unsubscribe();
    await Promise.allSettled(listenerRefreshes);

    expect(result.status).toBe("fresh");
  });

  it("loads authoritative v2 artifacts for completed tasks", async () => {
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
    vi.mocked(client.taskArtifacts).mockResolvedValue({
      schema_version: "2",
      items: [{
        schema_version: "2",
        artifact_id: "artifact-memory-1",
        project_id: "project-1",
        artifact_type: "text_memory",
        manifest_sha256: DIGEST,
        byte_size: 864,
        created_at: NOW,
      }],
      next_cursor: null,
      has_more: false,
    });

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
    expect(result.snapshot.artifacts).toEqual([expect.objectContaining({
      artifact_id: "artifact-memory-1",
      artifact_type: "text_memory",
    })]);
    expect(client.taskArtifacts).toHaveBeenCalledWith("task-1", { limit: 100 });
  });

  it("loads Session details with bounded concurrency", async () => {
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
    const tasks = Array.from({ length: 9 }, (_, index) => ({
      task_id: `task-${index + 1}`,
      project_id: "project-1",
      state: "completed",
      successor_transition: null,
    }));
    vi.mocked(client.listTasks).mockResolvedValue({
      schema_version: "2",
      items: tasks,
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
    let activeRequests = 0;
    let maximumActiveRequests = 0;
    const delayedPage = async () => {
      activeRequests += 1;
      maximumActiveRequests = Math.max(maximumActiveRequests, activeRequests);
      await new Promise<void>((resolve) => setTimeout(resolve, 5));
      activeRequests -= 1;
      return {
        schema_version: "2" as const,
        items: [],
        next_cursor: null,
        has_more: false,
      };
    };
    vi.mocked(client.taskTimeline).mockImplementation(delayedPage);
    vi.mocked(client.taskArtifacts).mockImplementation(delayedPage);

    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native: nativeFixture(),
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });
    const result = await provider.refresh();

    expect(result.status).toBe("fresh");
    if (result.status !== "fresh") throw new Error("fixture refresh failed");
    expect(result.snapshot.tasks.map((task) => task.task_id)).toEqual(
      tasks.map((task) => task.task_id),
    );
    expect(maximumActiveRequests).toBe(4);
  });

  it("preloads every Project and reuses unchanged Session details across Project switches", async () => {
    const current = profile({
      connection_state: "connected",
      active_project_id: "project-1",
      core_api_major: 2,
      core_openapi_sha256: DIGEST,
      core_event_schema_sha256: DIGEST,
      core_registry_sha256: DIGEST,
    });
    let activeProjectId = "project-1";
    const client = clientFixture([current]);
    vi.mocked(client.state).mockImplementation(async () => ({
      ...state([current]),
      active_profile_id: current.profile_id,
      active_project_id: activeProjectId,
    }));
    vi.mocked(client.listProjects).mockResolvedValue({
      schema_version: "2",
      items: ["project-1", "project-2"].map((projectId) => ({
        project_id: projectId,
        config: { execution: { mode: "codex_subscription_transcript" } },
      })),
      next_cursor: null,
      has_more: false,
    } as never);
    const tasks = ["project-1", "project-2"].map((projectId, index) => ({
      task_id: `task-${index + 1}`,
      project_id: projectId,
      state: "closed",
      successor_transition: null,
      updated_at: NOW,
    }));
    vi.mocked(client.listTasks).mockImplementation(async () => ({
      schema_version: "2",
      items: tasks,
      next_cursor: null,
      has_more: false,
    } as never));
    vi.mocked(client.listServices).mockResolvedValue({
      schema_version: "2",
      items: [],
      next_cursor: null,
      has_more: false,
    });
    vi.mocked(client.projectCapabilities).mockImplementation(async (projectId) => ({
      project_id: projectId,
      execution_mode: "codex_subscription_transcript",
      registry_sha256: DIGEST,
    } as never));
    vi.mocked(client.taskTimeline).mockResolvedValue({
      schema_version: "2",
      items: [],
      next_cursor: null,
      has_more: false,
    });
    vi.mocked(client.taskArtifacts).mockResolvedValue({
      schema_version: "2",
      items: [],
      next_cursor: null,
      has_more: false,
    });

    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native: nativeFixture(),
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });
    const first = await provider.refresh();
    expect(first.status).toBe("fresh");
    if (first.status !== "fresh") throw new Error("fixture refresh failed");
    expect(first.snapshot.tasks.map((task) => task.project_id)).toEqual(["project-1", "project-2"]);
    expect(client.taskTimeline).toHaveBeenCalledTimes(2);
    expect(client.taskArtifacts).toHaveBeenCalledTimes(2);

    activeProjectId = "project-2";
    (current as { active_project_id: string | null }).active_project_id = activeProjectId;
    const second = await provider.refresh();
    expect(second.status).toBe("fresh");
    if (second.status !== "fresh") throw new Error("fixture refresh failed");
    expect(second.snapshot.state.active_project_id).toBe("project-2");
    expect(client.taskTimeline).toHaveBeenCalledTimes(2);
    expect(client.taskArtifacts).toHaveBeenCalledTimes(2);

    tasks[1]!.updated_at = "2026-07-23T06:00:01Z";
    await provider.refresh();
    expect(client.taskTimeline).toHaveBeenCalledTimes(3);
    expect(client.taskArtifacts).toHaveBeenCalledTimes(3);
    expect(client.listTasks).toHaveBeenLastCalledWith({ limit: 100 });
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
    vi.mocked(client.getProject).mockRejectedValue(new Error("409 Conflict"));
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
    expect(client.getProject).not.toHaveBeenCalled();
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

  it("replays a reserved lifecycle cancellation after an ambiguous relaunch", async () => {
    const native = nativeFixture();
    const running = {
      ...lifecycleOperation(),
      status: "running" as const,
      phase: "connecting" as const,
      phase_index: 3,
      started_at: NOW,
    };
    const firstClient = clientFixture();
    vi.mocked(firstClient.getLifecycleOperation).mockResolvedValue(running);
    vi.mocked(firstClient.cancelLifecycleOperation).mockRejectedValue(
      new TypeError("cancellation response connection closed"),
    );
    const firstProvider = createLocalApiDesktopProductProviderV2({
      client: firstClient,
      native,
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });
    const firstRefresh = await firstProvider.refresh();
    if (firstRefresh.status !== "fresh") throw new Error("fixture refresh failed");

    await expect(firstProvider.cancelLifecycleOperation(running.operation_id, {
      actionId: "cancel-lifecycle-ambiguous-0001",
      streamEpoch: firstRefresh.snapshot.stream.epoch,
    })).rejects.toThrow(/response connection closed/i);
    expect(JSON.parse(native.journalValue()!)).toMatchObject({
      entries: [{
        action_id: "cancel-lifecycle-ambiguous-0001",
        mutation_kind: "lifecycle_cancel",
        state: "reserved",
        accepted_operation_id: null,
      }],
    });

    const advanced = {
      ...running,
      progress: { kind: "indeterminate" as const },
      updated_at: "2026-07-23T06:00:01Z",
      etag: `"${"c".repeat(64)}"`,
    };
    const accepted = {
      ...advanced,
      cancellable: false,
      updated_at: "2026-07-23T06:00:02Z",
      etag: `"${"d".repeat(64)}"`,
    };
    const recoveredClient = clientFixture();
    vi.mocked(recoveredClient.getLifecycleOperation)
      .mockResolvedValueOnce(advanced)
      .mockResolvedValue(accepted);
    vi.mocked(recoveredClient.cancelLifecycleOperation).mockResolvedValue(accepted);
    const recoveredProvider = createLocalApiDesktopProductProviderV2({
      client: recoveredClient,
      native,
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });

    await expect(recoveredProvider.refresh()).resolves.toMatchObject({ status: "fresh" });
    expect(recoveredClient.cancelLifecycleOperation).toHaveBeenCalledWith(
      running.operation_id,
      { schema_version: "2", expected_operation_id: running.operation_id },
      {
        resourceGeneration: 0,
        ifMatch: advanced.etag,
        idempotencyKey: "cancel-lifecycle-ambiguous-0001",
      },
    );
    expect(native.journalValue()).toBeNull();
  });

  it("replays an initial lifecycle cancellation ETag race with the same action", async () => {
    const native = nativeFixture();
    const running = {
      ...lifecycleOperation(),
      status: "running" as const,
      phase: "connecting" as const,
      phase_index: 3,
      started_at: NOW,
    };
    const advanced = {
      ...running,
      progress: { kind: "indeterminate" as const },
      updated_at: "2026-07-23T06:00:01Z",
      etag: `"${"c".repeat(64)}"`,
    };
    const accepted = {
      ...advanced,
      cancellable: false,
      updated_at: "2026-07-23T06:00:02Z",
      etag: `"${"d".repeat(64)}"`,
    };
    const actionId = "cancel-lifecycle-initial-race-0001";
    const client = clientFixture();
    vi.mocked(client.getLifecycleOperation)
      .mockResolvedValueOnce(running)
      .mockResolvedValueOnce(advanced)
      .mockResolvedValue(accepted);
    vi.mocked(client.cancelLifecycleOperation)
      .mockRejectedValueOnce(new DesktopApiErrorV2(412, {
        schema_version: "2",
        code: "resource_changed",
        summary: "The lifecycle operation ETag changed.",
        retryable: true,
        action: "retry",
        affected_resource_id: running.operation_id,
      }))
      .mockResolvedValue(accepted);
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native,
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });
    const refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("fixture refresh failed");

    await expect(provider.cancelLifecycleOperation(running.operation_id, {
      actionId,
      streamEpoch: refreshed.snapshot.stream.epoch,
    })).resolves.toEqual(accepted);
    expect(client.cancelLifecycleOperation).toHaveBeenNthCalledWith(
      1,
      running.operation_id,
      { schema_version: "2", expected_operation_id: running.operation_id },
      {
        resourceGeneration: 0,
        ifMatch: running.etag,
        idempotencyKey: actionId,
      },
    );
    expect(client.cancelLifecycleOperation).toHaveBeenNthCalledWith(
      2,
      running.operation_id,
      { schema_version: "2", expected_operation_id: running.operation_id },
      {
        resourceGeneration: 0,
        ifMatch: advanced.etag,
        idempotencyKey: actionId,
      },
    );
    expect(native.journalValue()).toBeNull();
  });

  it("reuses a reserved lifecycle cancellation on explicit retry after the ETag advances", async () => {
    const native = nativeFixture();
    const running = {
      ...lifecycleOperation(),
      status: "running" as const,
      phase: "connecting" as const,
      phase_index: 3,
      started_at: NOW,
    };
    const advanced = {
      ...running,
      progress: { kind: "indeterminate" as const },
      updated_at: "2026-07-23T06:00:01Z",
      etag: `"${"c".repeat(64)}"`,
    };
    const accepted = {
      ...advanced,
      cancellable: false,
      updated_at: "2026-07-23T06:00:03Z",
      etag: `"${"e".repeat(64)}"`,
    };
    const fresher = {
      ...advanced,
      updated_at: "2026-07-23T06:00:02Z",
      etag: `"${"d".repeat(64)}"`,
    };
    const client = clientFixture();
    vi.mocked(client.getLifecycleOperation)
      .mockResolvedValueOnce(running)
      .mockResolvedValueOnce(advanced)
      .mockResolvedValueOnce(fresher)
      .mockResolvedValue(accepted);
    vi.mocked(client.cancelLifecycleOperation)
      .mockRejectedValueOnce(new TypeError("cancellation response connection closed"))
      .mockRejectedValueOnce(new DesktopApiErrorV2(412, {
        schema_version: "2",
        code: "resource_changed",
        summary: "The lifecycle operation ETag changed.",
        retryable: true,
        action: "retry",
        affected_resource_id: running.operation_id,
      }))
      .mockResolvedValue(accepted);
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native,
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });
    const refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("fixture refresh failed");

    await expect(provider.cancelLifecycleOperation(running.operation_id, {
      actionId: "cancel-lifecycle-explicit-original-0001",
      streamEpoch: refreshed.snapshot.stream.epoch,
    })).rejects.toThrow(/response connection closed/i);
    await expect(provider.getLifecycleOperation(running.operation_id)).resolves.toEqual(advanced);

    await expect(provider.cancelLifecycleOperation(running.operation_id, {
      actionId: "cancel-lifecycle-explicit-new-0002",
      streamEpoch: refreshed.snapshot.stream.epoch,
    })).resolves.toEqual(accepted);
    expect(client.cancelLifecycleOperation).toHaveBeenNthCalledWith(
      2,
      running.operation_id,
      { schema_version: "2", expected_operation_id: running.operation_id },
      {
        resourceGeneration: 0,
        ifMatch: advanced.etag,
        idempotencyKey: "cancel-lifecycle-explicit-original-0001",
      },
    );
    expect(client.cancelLifecycleOperation).toHaveBeenNthCalledWith(
      3,
      running.operation_id,
      { schema_version: "2", expected_operation_id: running.operation_id },
      {
        resourceGeneration: 0,
        ifMatch: fresher.etag,
        idempotencyKey: "cancel-lifecycle-explicit-original-0001",
      },
    );
    expect(native.journalValue()).toBeNull();
  });

  it("reconciles a terminal lifecycle operation when a reserved cancellation loses its ETag race", async () => {
    const native = nativeFixture();
    const running = {
      ...lifecycleOperation(),
      status: "running" as const,
      phase: "connecting" as const,
      phase_index: 3,
      started_at: NOW,
    };
    const advanced = {
      ...running,
      updated_at: "2026-07-23T06:00:01Z",
      etag: `"${"c".repeat(64)}"`,
    };
    const terminal = {
      ...advanced,
      status: "cancelled" as const,
      cancellable: false,
      updated_at: "2026-07-23T06:00:02Z",
      finished_at: "2026-07-23T06:00:02Z",
      etag: `"${"d".repeat(64)}"`,
    };
    const client = clientFixture();
    vi.mocked(client.getLifecycleOperation)
      .mockResolvedValueOnce(running)
      .mockResolvedValueOnce(advanced)
      .mockResolvedValue(terminal);
    vi.mocked(client.cancelLifecycleOperation)
      .mockRejectedValueOnce(new TypeError("cancellation response connection closed"))
      .mockRejectedValueOnce(new DesktopApiErrorV2(412, {
        schema_version: "2",
        code: "resource_changed",
        summary: "The lifecycle operation ETag changed.",
        retryable: true,
        action: "retry",
        affected_resource_id: running.operation_id,
      }));
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native,
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });
    const refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("fixture refresh failed");

    await expect(provider.cancelLifecycleOperation(running.operation_id, {
      actionId: "cancel-lifecycle-terminal-race-original-0001",
      streamEpoch: refreshed.snapshot.stream.epoch,
    })).rejects.toThrow(/response connection closed/i);
    await expect(provider.getLifecycleOperation(running.operation_id)).resolves.toEqual(advanced);

    await expect(provider.cancelLifecycleOperation(running.operation_id, {
      actionId: "cancel-lifecycle-terminal-race-new-0002",
      streamEpoch: refreshed.snapshot.stream.epoch,
    })).resolves.toEqual(terminal);
    expect(client.cancelLifecycleOperation).toHaveBeenCalledTimes(2);
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

  it("reconciles a historical profile lifecycle result after the profile generation advances", async () => {
    const current = profile({
      connection_generation: 6,
      connection_state: "connected",
      trust: {
        ...profile().trust,
        connection_generation: 6,
      },
      etag: `"${"f".repeat(64)}"`,
      updated_at: "2026-07-23T06:00:02Z",
    });
    const terminal = {
      ...lifecycleOperation(),
      status: "succeeded" as const,
      phase: "finalizing" as const,
      phase_index: 16,
      progress: null,
      cancellable: false,
      result: {
        result_kind: "profile" as const,
        profile_id: current.profile_id,
        connection_generation: 5,
      },
      updated_at: "2026-07-23T06:00:01Z",
      finished_at: "2026-07-23T06:00:01Z",
      etag: `"${"e".repeat(64)}"`,
    };
    const client = clientFixture([current]);
    vi.mocked(client.state).mockResolvedValue({
      ...state([current]),
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
      updated_at: current.updated_at,
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
    vi.mocked(client.getProfile).mockResolvedValue(current);
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native: nativeFixture(),
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });

    const recovered = await provider.refresh();

    expect(recovered.status).toBe("fresh");
    expect(client.getProfile).toHaveBeenCalledWith(current.profile_id);
    expect(client.acknowledgeLifecycleOperation).toHaveBeenCalledTimes(1);
  });

  it("acknowledges an inactive historical project result without querying the active-project endpoint", async () => {
    const terminal = {
      ...lifecycleOperation(),
      operation_id: "lifecycle-project-create-history-1",
      kind: "project_create" as const,
      resource: {
        resource_kind: "project" as const,
        resource_id: "project-historical",
      },
      status: "succeeded" as const,
      phase: "finalizing" as const,
      phase_index: 16,
      progress: null,
      cancellable: false,
      result: {
        result_kind: "project" as const,
        project_id: "project-historical",
      },
      updated_at: "2026-07-23T06:00:01Z",
      finished_at: "2026-07-23T06:00:01Z",
      etag: `"${"e".repeat(64)}"`,
    };
    const client = clientFixture();
    vi.mocked(client.state).mockResolvedValue({
      ...state(),
      active_project_id: null,
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
    vi.mocked(client.getProject).mockRejectedValue(new Error("inactive project returns 409"));
    const provider = createLocalApiDesktopProductProviderV2({
      client,
      native: nativeFixture(),
      featureFlags: ["system_openssh_profiles"],
      providerStreamInstance: "provider-instance-test",
    });

    const recovered = await provider.refresh();

    expect(recovered.status).toBe("fresh");
    expect(client.getProject).not.toHaveBeenCalled();
    expect(client.acknowledgeLifecycleOperation).toHaveBeenCalledTimes(1);
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
