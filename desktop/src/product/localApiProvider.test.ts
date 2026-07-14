import { describe, expect, it, vi } from "vitest";
import type { DesktopApiClientV1, FetchLike, ListRequestOptions } from "../api/v1/client";
import { DesktopApiError } from "../api/v1/client";
import { CONTRACT_FIXTURE_V1 } from "../api/v1/fixtures";
import {
  apiErrorV1Schema,
  artifactV1Schema,
  desktopStateV1Schema,
  localOperationV1Schema,
  operationV1Schema,
  projectCapabilitiesV1Schema,
  projectSourceV1Schema,
  projectValidationV1Schema,
  projectV1Schema,
  remoteProfileV1Schema,
  runV1Schema,
  serviceV1Schema,
  timelineEntryV1Schema,
  type ArtifactV1,
} from "../api/v1/schemas";
import { createLocalApiDesktopProductProvider } from "./localApiProvider";

const A = "a".repeat(64);
const B = "b".repeat(64);
const ETAG_A = `"${A}"`;
const ETAG_B = `"${B}"`;
const NOW = "2026-07-14T12:00:00Z";

describe("LocalApiDesktopProductProvider", () => {
  it("loads every page, exact run details, deterministic operations, and active project authority", async () => {
    const client = mockClient();
    const secondService = serviceV1Schema.parse({
      ...CONTRACT_FIXTURE_V1.service,
      id: "service-gateway-fixture-2",
      kind: "gateway",
    });
    client.state = vi.fn().mockResolvedValue(onlineState(["operation-z", "operation-a"]));
    client.listProfiles = pagedMock([[profile()], [profile({ profile_id: "profile-fixture-2" })]]);
    client.listProjects = pagedMock([[project()]]);
    client.listRuns = pagedMock([[CONTRACT_FIXTURE_V1.runSummary], []]);
    client.listServices = pagedMock([[serviceV1Schema.parse(CONTRACT_FIXTURE_V1.service)], [secondService]]);
    client.runTimeline = runPagedMock([[timelineEntryV1Schema.parse(CONTRACT_FIXTURE_V1.timeline)], []]);
    client.runArtifacts = runPagedMock<ArtifactV1>([
      [artifactV1Schema.parse(CONTRACT_FIXTURE_V1.artifacts[0])],
      [artifactV1Schema.parse(CONTRACT_FIXTURE_V1.artifacts[1])],
    ]);
    client.getOperation = vi.fn(async (id) => localOperationV1Schema.parse({
      ...CONTRACT_FIXTURE_V1.operation,
      operation_id: id,
    }));

    const provider = createProvider(client);
    const result = await provider.refresh();

    expect(result.status).toBe("fresh");
    if (result.status !== "fresh") return;
    expect(result.snapshot.profiles).toHaveLength(2);
    expect(result.snapshot.runs).toEqual([runV1Schema.parse(CONTRACT_FIXTURE_V1.run)]);
    expect(result.snapshot.timelines[CONTRACT_FIXTURE_V1.run.id]).toHaveLength(1);
    expect(result.snapshot.artifacts.map((artifact) => artifact.id)).toEqual([
      "artifact-memory-fixture-1",
      "artifact-skill-fixture-1",
    ]);
    expect(result.snapshot.artifactCollection).toEqual({ status: "complete" });
    expect(result.snapshot.services).toHaveLength(2);
    expect(result.snapshot.activeOperation?.operation_id).toBe("operation-a");
    expect(result.snapshot.capability?.status).toBe("ready");
    expect(result.snapshot.validation?.status).toBe("ready");
    expect(client.getRun).toHaveBeenCalledWith(CONTRACT_FIXTURE_V1.run.id);
    expect(client.validateProject).toHaveBeenCalledWith("project-fixture-1", {
      ifMatch: ETAG_B,
      idempotencyKey: `desktop-validation-${B}`,
    });
    expect(client.listProfiles).toHaveBeenNthCalledWith(2, { limit: 100, after: "cursor-1" });
  });

  it("fails a cyclic collection and discards a partial artifact collection", async () => {
    const cyclic = mockClient();
    cyclic.listProfiles = vi.fn(async (options?: ListRequestOptions) => ({
      schema_version: "1" as const,
      items: [profile()],
      next_cursor: options?.after ?? "same-cursor",
      has_more: true,
    }));
    const cycleResult = await createProvider(cyclic).refresh();
    expect(cycleResult.status).toBe("error");

    const partial = mockClient();
    partial.runArtifacts = vi.fn()
      .mockResolvedValueOnce(page([CONTRACT_FIXTURE_V1.artifacts[0]], "artifact-next"))
      .mockRejectedValueOnce(new TypeError("connection lost"));
    const partialResult = await createProvider(partial).refresh();
    expect(partialResult.status).toBe("fresh");
    if (partialResult.status !== "fresh") return;
    expect(partialResult.snapshot.artifacts).toEqual([]);
    expect(partialResult.snapshot.artifactCollection).toEqual({ status: "incomplete", reason: "refresh_failed" });
  });

  it("does not let an older refresh overwrite a newer snapshot", async () => {
    const client = mockClient();
    const first = deferred<ReturnType<typeof onlineState>>();
    client.state = vi.fn()
      .mockReturnValueOnce(first.promise)
      .mockResolvedValueOnce(onlineState());
    const provider = createProvider(client);

    const older = provider.refresh();
    const newer = await provider.refresh();
    first.resolve(onlineState());

    expect(newer.status).toBe("fresh");
    expect((await older).status).toBe("stale");
  });

  it("maps epochs, ETags, idempotency keys, request bodies, and Core operations exactly", async () => {
    const client = mockClient();
    const provider = createProvider(client);
    const refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("expected a fresh fixture");
    const epoch = refreshed.snapshot.stream.epoch;

    await expect(provider.startRun({
      projectId: "project-fixture-1",
      actionId: "renderer-action-start-0001",
      streamEpoch: epoch - 1,
      etag: ETAG_B,
    })).rejects.toThrow(/refresh/i);
    expect(client.createRun).not.toHaveBeenCalled();

    const run = await provider.startRun({
      projectId: "project-fixture-1",
      actionId: "renderer-action-start-0001",
      streamEpoch: epoch,
      etag: ETAG_B,
    });
    expect(run).toEqual(runV1Schema.parse(CONTRACT_FIXTURE_V1.run));
    expect(client.createRun).toHaveBeenCalledWith(
      { project_id: "project-fixture-1" },
      { idempotencyKey: "renderer-action-start-0001", ifMatch: ETAG_B },
    );

    const refreshedAgain = await provider.refresh();
    if (refreshedAgain.status !== "fresh") throw new Error("expected a fresh fixture");
    const operation = await provider.restartService("service-control-fixture-1", {
      actionId: "renderer-action-restart-0001",
      streamEpoch: refreshedAgain.snapshot.stream.epoch,
      etag: ETAG_B,
    });
    expect(operation).toEqual(operationV1Schema.parse(CONTRACT_FIXTURE_V1.serviceOperation));
    expect(client.restartService).toHaveBeenCalledWith("service-control-fixture-1", {
      idempotencyKey: "renderer-action-restart-0001",
      ifMatch: ETAG_B,
    });

    const unknown = mockClient();
    unknown.createRun = vi.fn().mockRejectedValue(new TypeError("unknown network outcome"));
    const unknownProvider = createProvider(unknown);
    const unknownRefresh = await unknownProvider.refresh();
    if (unknownRefresh.status !== "fresh") throw new Error("expected a fresh fixture");
    await expect(unknownProvider.startRun({
      projectId: "project-fixture-1",
      actionId: "renderer-action-unknown-0001",
      streamEpoch: unknownRefresh.snapshot.stream.epoch,
      etag: ETAG_B,
    })).rejects.toThrow("unknown network outcome");
    expect(unknown.createRun).toHaveBeenCalledTimes(1);
  });

  it("uses only strict native source and credential bridge results", async () => {
    const client = mockClient();
    const native = {
      selectProjectSource: vi.fn().mockResolvedValue({
        kind: "native_folder_snapshot",
        display_name: "Selected source",
        import_ref: CONTRACT_FIXTURE_V1.workspaceImport,
      }),
      configureCredential: vi.fn().mockResolvedValue(profile()),
    };
    const provider = createLocalApiDesktopProductProvider({ client, native, fetch: vi.fn<FetchLike>() });
    const refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("expected a fresh fixture");
    const epoch = refreshed.snapshot.stream.epoch;
    const source = await provider.selectProjectSource({
      kind: "native_folder_snapshot",
      actionId: "renderer-action-source-0001",
      streamEpoch: epoch,
    });
    expect(source).toEqual(projectSourceV1Schema.parse({
      kind: "native_folder_snapshot",
      display_name: "Selected source",
      import_ref: CONTRACT_FIXTURE_V1.workspaceImport,
    }));

    const next = await provider.refresh();
    if (next.status !== "fresh") throw new Error("expected a fresh fixture");
    await provider.configureCredential("profile-fixture-1", "ssh_private_key", {
      actionId: "renderer-action-credential-0001",
      streamEpoch: next.snapshot.stream.epoch,
      etag: ETAG_A,
    });
    expect(native.configureCredential).toHaveBeenCalledWith(
      "profile-fixture-1",
      "ssh_private_key",
      ETAG_A,
      "renderer-action-credential-0001",
    );

    native.selectProjectSource.mockResolvedValueOnce({
      kind: "scratch",
      display_name: "Unsafe",
      import_ref: null,
      path: "/private/source",
    });
    const finalRefresh = await provider.refresh();
    if (finalRefresh.status !== "fresh") throw new Error("expected a fresh fixture");
    await expect(provider.selectProjectSource({
      kind: "native_folder_snapshot",
      actionId: "renderer-action-source-0002",
      streamEpoch: finalRefresh.snapshot.stream.epoch,
    })).rejects.toThrow();
  });

  it("treats duplicate events as safe and an out-of-order sequence as a reload gap", async () => {
    const client = mockClient();
    const body = [
      eventFrame("event-2", 2),
      eventFrame("event-2", 2),
      eventFrame("event-4", 4),
    ].join("");
    const fetch = vi.fn<FetchLike>().mockResolvedValue(eventResponse(body));
    const provider = createProvider(client, fetch);
    const refreshed = await provider.refresh();
    expect(refreshed.status).toBe("fresh");
    const signals: string[] = [];
    const unsubscribe = provider.subscribe((signal) => signals.push(signal.kind));
    await settle();

    expect(signals.filter((kind) => kind === "snapshot_changed")).toHaveLength(2);
    expect(signals).toContain("stream_stale");
    unsubscribe();
  });

  it("resets an expired cursor and aborts the sole stream on unsubscribe", async () => {
    const expiredClient = mockClient();
    expiredClient.eventStreamRequest = vi.fn().mockResolvedValue({
      url: "http://127.0.0.1/events",
      headers: { "X-OpenEvo-Desktop-Session": "session" },
    });
    const expiredFetch = vi.fn<FetchLike>().mockResolvedValue(new Response(
      JSON.stringify(CONTRACT_FIXTURE_V1.cursorExpiredError),
      { status: 410, headers: { "Content-Type": "application/json" } },
    ));
    const expiredProvider = createProvider(expiredClient, expiredFetch);
    await expiredProvider.refresh();
    const expiredSignals: string[] = [];
    const stopExpired = expiredProvider.subscribe((signal) => expiredSignals.push(signal.kind));
    await settle();
    expect(expiredSignals).toEqual(["cursor_reset"]);
    stopExpired();

    let observedSignal: AbortSignal | undefined;
    const openFetch = vi.fn<FetchLike>(async (_input, init) => {
      observedSignal = init?.signal ?? undefined;
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          observedSignal?.addEventListener("abort", () => controller.error(new DOMException("Aborted", "AbortError")));
        },
      });
      return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
    });
    const openProvider = createProvider(mockClient(), openFetch);
    await openProvider.refresh();
    const unsubscribe = openProvider.subscribe(() => undefined);
    await settle();
    unsubscribe();
    expect(observedSignal?.aborted).toBe(true);
  });

  it("maps only transport and 503 capability failures to unavailable", async () => {
    const client = mockClient();
    client.projectCapabilities = vi.fn().mockRejectedValue(new DesktopApiError(apiErrorV1Schema.parse({
      ...CONTRACT_FIXTURE_V1.error,
      code: "core_unavailable",
      http_status: 503,
    })));
    const result = await createProvider(client).refresh();
    expect(result.status).toBe("fresh");
    if (result.status !== "fresh") return;
    expect(result.snapshot.capability?.status).toBe("unavailable");
    expect(result.snapshot.validation?.status).toBe("unavailable");
    expect(client.validateProject).not.toHaveBeenCalled();
  });
});

function mockClient(): DesktopApiClientV1 & Record<string, ReturnType<typeof vi.fn>> {
  return {
    state: vi.fn().mockResolvedValue(onlineState()),
    listProfiles: vi.fn().mockResolvedValue(page([profile()])),
    listProjects: vi.fn().mockResolvedValue(page([project()])),
    listRuns: vi.fn().mockResolvedValue(page([CONTRACT_FIXTURE_V1.runSummary])),
    getRun: vi.fn().mockResolvedValue(runV1Schema.parse(CONTRACT_FIXTURE_V1.run)),
    runTimeline: vi.fn().mockResolvedValue(page([timelineEntryV1Schema.parse(CONTRACT_FIXTURE_V1.timeline)])),
    runArtifacts: vi.fn().mockResolvedValue(page([CONTRACT_FIXTURE_V1.artifacts[0]])),
    listServices: vi.fn().mockResolvedValue(page([serviceV1Schema.parse(CONTRACT_FIXTURE_V1.service)])),
    projectCapabilities: vi.fn().mockResolvedValue(projectCapabilitiesV1Schema.parse(CONTRACT_FIXTURE_V1.capabilities)),
    validateProject: vi.fn().mockResolvedValue(projectValidationV1Schema.parse(CONTRACT_FIXTURE_V1.validation)),
    getOperation: vi.fn().mockResolvedValue(localOperationV1Schema.parse(CONTRACT_FIXTURE_V1.operation)),
    createRun: vi.fn().mockResolvedValue(runV1Schema.parse(CONTRACT_FIXTURE_V1.run)),
    restartService: vi.fn().mockResolvedValue(operationV1Schema.parse(CONTRACT_FIXTURE_V1.serviceOperation)),
    createProfile: vi.fn().mockResolvedValue(profile()),
    updateProfile: vi.fn().mockResolvedValue(profile()),
    connectProfile: vi.fn().mockResolvedValue(localOperationV1Schema.parse(CONTRACT_FIXTURE_V1.operation)),
    acceptProfileHostKey: vi.fn().mockResolvedValue(localOperationV1Schema.parse(CONTRACT_FIXTURE_V1.operation)),
    createProject: vi.fn().mockResolvedValue(project()),
    updateProject: vi.fn().mockResolvedValue(project()),
    activateProject: vi.fn().mockResolvedValue(localOperationV1Schema.parse(CONTRACT_FIXTURE_V1.operation)),
    syncProjectWorkspace: vi.fn().mockResolvedValue(localOperationV1Schema.parse(CONTRACT_FIXTURE_V1.operation)),
    cancelRun: vi.fn().mockResolvedValue(runV1Schema.parse(CONTRACT_FIXTURE_V1.run)),
    artifactContent: vi.fn().mockResolvedValue(CONTRACT_FIXTURE_V1.artifactContent),
    artifactDiff: vi.fn().mockResolvedValue(CONTRACT_FIXTURE_V1.artifactDiff),
    repairProject: vi.fn().mockResolvedValue(localOperationV1Schema.parse(CONTRACT_FIXTURE_V1.operation)),
    eventStreamRequest: vi.fn().mockResolvedValue({ url: "http://127.0.0.1/events", headers: {} }),
  } as unknown as DesktopApiClientV1 & Record<string, ReturnType<typeof vi.fn>>;
}

function createProvider(client: DesktopApiClientV1, fetch: FetchLike = vi.fn<FetchLike>()) {
  return createLocalApiDesktopProductProvider({
    client,
    native: {
      selectProjectSource: vi.fn(),
      configureCredential: vi.fn(),
    },
    fetch,
    reconnectDelaysMs: [],
  });
}

function onlineState(pendingOperationIds: string[] = []) {
  return desktopStateV1Schema.parse({
    schema_version: "1",
    observed_at: NOW,
    contract: {
      selected_major: 1,
      desktop_openapi_sha256: A,
      core_openapi_sha256: B,
      compatible: true,
    },
    core: {
      state: "online",
      profile_id: "profile-fixture-1",
      active_tunnel: true,
      operation_id: null,
      host_key_review: null,
      core: { contract_version: "1", contract_digest: B, core_version: "1.0.0" },
      failure: null,
    },
    active_project: {
      project_id: "project-fixture-1",
      project_etag: ETAG_B,
      profile_id: "profile-fixture-1",
      connection_state: "ready",
    },
    pending_operation_ids: pendingOperationIds,
  });
}

function profile(overrides: Record<string, unknown> = {}) {
  return remoteProfileV1Schema.parse({ ...CONTRACT_FIXTURE_V1.profile, ...overrides });
}

function project() {
  return projectV1Schema.parse(CONTRACT_FIXTURE_V1.project);
}

function page<T>(items: readonly T[], nextCursor: string | null = null) {
  return {
    schema_version: "1" as const,
    items: [...items],
    next_cursor: nextCursor,
    has_more: nextCursor !== null,
  };
}

function pagedMock<T>(pages: readonly (readonly T[])[]) {
  return vi.fn(async (options?: ListRequestOptions) => {
    const index = options?.after === undefined ? 0 : Number(options.after.replace("cursor-", ""));
    const next = index + 1 < pages.length ? `cursor-${index + 1}` : null;
    return page(pages[index] ?? [], next);
  });
}

function runPagedMock<T>(pages: readonly (readonly T[])[]) {
  const load = pagedMock(pages);
  return vi.fn(async (_runId: string, options?: ListRequestOptions) => load(options));
}

function eventFrame(eventId: string, sequence: number): string {
  const envelope = {
    schema_version: "1",
    event_id: eventId,
    event_name: "desktop.v1.resource.changed",
    occurred_at: NOW,
    sequence,
    data: {
      kind: "resource_changed",
      authority: "core",
      resource: { resource_type: "run", resource_id: "run-fixture-1" },
      change: "updated",
      change_id: `change-${sequence}`,
      resource_etag: ETAG_A,
      content_sha256: null,
    },
  };
  return `id: ${eventId}\nevent: desktop.v1.resource.changed\ndata: ${JSON.stringify(envelope)}\n\n`;
}

function eventResponse(body: string): Response {
  const bytes = new TextEncoder().encode(body);
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(bytes);
      controller.close();
    },
  }), { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

async function settle(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
}
