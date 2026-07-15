import { describe, expect, it, vi } from "vitest";
import type { DesktopApiClientV1, FetchLike, ListRequestOptions } from "../api/v1/client";
import { DesktopApiError } from "../api/v1/client";
import { CONTRACT_FIXTURE_V1, RELEASE_EXECUTION_MODE_CAPABILITIES_FIXTURE_V1 } from "../api/v1/fixtures";
import {
  apiErrorV1Schema,
  artifactContentV1Schema,
  artifactDiffV1Schema,
  artifactV1Schema,
  desktopStateV1Schema,
  diagnosticReportV1Schema,
  localOperationV1Schema,
  logEntryV1Schema,
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
  type LocalOperationV1,
} from "../api/v1/schemas";
import { LocalApiDesktopProductProvider } from "./localApiProvider";
import { DesktopProductAmbiguousMutationError } from "./provider";
import type { ProductRunRetryRecoveryStore } from "./runRetryRecovery";

const A = "a".repeat(64);
const B = "b".repeat(64);
const ETAG_A = `"${A}"`;
const ETAG_B = `"${B}"`;
const NOW = "2026-07-14T12:00:00Z";

describe("LocalApiDesktopProductProvider", () => {
  it("fails closed without persistent retry recovery and preserves an invalid saved record", () => {
    const client = mockClient();
    const native = {
      selectProjectSource: vi.fn(),
      cancelProjectSource: vi.fn(),
      settleProjectSource: vi.fn(),
    };
    expect(() => new LocalApiDesktopProductProvider({
      client,
      native,
      fetch: vi.fn<FetchLike>(),
    })).toThrow(/persistent.*retry recovery.*unavailable/i);

    let value: string | null = "{not-json";
    const store: ProductRunRetryRecoveryStore = {
      read: () => value,
      write: (next) => { value = next; },
    };
    expect(() => new LocalApiDesktopProductProvider({
      client,
      native,
      fetch: vi.fn<FetchLike>(),
      retryRecoveryStore: store,
    })).toThrow(/recovery state is invalid/i);
    expect(value).toBe("{not-json");
  });

  it("loads every page, exact run details, deterministic operations, and active project authority", async () => {
    const client = mockClient();
    const secondService = serviceV1Schema.parse({
      ...CONTRACT_FIXTURE_V1.service,
      id: "service-gateway-fixture-2",
      kind: "gateway",
    });
    client.state = vi.fn().mockResolvedValue(onlineState(["operation-z", "operation-a", "operation-z"]));
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
    expect(result.snapshot.executionModeCapabilities).toEqual(result.snapshot.state.execution_mode_capabilities);
    expect(result.snapshot.activeOperation?.operation_id).toBe("operation-a");
    expect(vi.mocked(client.getOperation).mock.calls.map(([operationId]) => operationId)).toEqual(["operation-a", "operation-z"]);
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

  it("checks the deduplicated pending-operation budget before issuing any operation request", async () => {
    const client = mockClient();
    client.state = vi.fn().mockResolvedValue(onlineState(
      Array.from({ length: 20_001 }, (_, index) => `pending-operation-${index}`),
    ));

    const first = await createProvider(client).refresh();
    const second = await createProvider(client).refresh();

    expect(first.status).toBe("error");
    expect(second.status).toBe("error");
    expect(client.getOperation).not.toHaveBeenCalled();
  });

  it("rejects schema-valid refresh resources with cross-wired collection or parent identities", async () => {
    const duplicateProfile = mockClient();
    duplicateProfile.listProfiles = vi.fn().mockResolvedValue(page([profile(), profile()]));
    expect((await createProvider(duplicateProfile).refresh()).status).toBe("error");

    const unknownProfile = mockClient();
    unknownProfile.listProjects = vi.fn().mockResolvedValue(page([project({ profile_id: "profile-outside-page" })]));
    expect((await createProvider(unknownProfile).refresh()).status).toBe("error");

    const mismatchedDetail = mockClient();
    mismatchedDetail.getRun = vi.fn().mockResolvedValue(run({ updated_at: "2026-07-14T12:00:01Z" }));
    expect((await createProvider(mismatchedDetail).refresh()).status).toBe("error");

    const unknownRunProject = mockClient();
    const foreignRun = runForProject("project-outside-page");
    unknownRunProject.listRuns = vi.fn().mockResolvedValue(page([runSummary(foreignRun)]));
    unknownRunProject.getRun = vi.fn().mockResolvedValue(foreignRun);
    expect((await createProvider(unknownRunProject).refresh()).status).toBe("error");

    const duplicateService = mockClient();
    duplicateService.listServices = vi.fn().mockResolvedValue(page([
      serviceV1Schema.parse(CONTRACT_FIXTURE_V1.service),
      serviceV1Schema.parse(CONTRACT_FIXTURE_V1.service),
    ]));
    expect((await createProvider(duplicateService).refresh()).status).toBe("error");
  });

  it("rejects schema-valid timeline entries outside the requested run, attempt, service, or artifact parent", async () => {
    const cases = [
      timelineEntryV1Schema.parse({ ...CONTRACT_FIXTURE_V1.timeline, run_id: "run-cross-wired" }),
      timelineEntryV1Schema.parse({ ...CONTRACT_FIXTURE_V1.timeline, attempt_id: "attempt-cross-wired" }),
      timelineEntryV1Schema.parse({ ...CONTRACT_FIXTURE_V1.timeline, service_id: "service-cross-wired" }),
      timelineEntryV1Schema.parse({ ...CONTRACT_FIXTURE_V1.timeline, artifact_ids: ["artifact-cross-wired"] }),
    ];
    for (const entry of cases) {
      const client = mockClient();
      client.runTimeline = vi.fn().mockResolvedValue(page([entry]));
      expect((await createProvider(client).refresh()).status).toBe("error");
    }
  });

  it("loads every run log page on demand and verifies exact run ownership", async () => {
    const client = mockClient();
    const first = logEntryV1Schema.parse(CONTRACT_FIXTURE_V1.log);
    const second = logEntryV1Schema.parse({
      ...CONTRACT_FIXTURE_V1.log,
      id: "log-fixture-2",
      sequence: first.sequence + 1,
      stream: "evolution",
      message: "Evolution outputs were materialized.",
    });
    client.runLogs = runPagedMock([[first], [second]]);
    const provider = createProvider(client);
    expect((await provider.refresh()).status).toBe("fresh");

    const logs = await provider.getRunLogs(CONTRACT_FIXTURE_V1.run.id);

    expect(logs).toEqual([first, second]);
    expect(client.runLogs).toHaveBeenNthCalledWith(1, CONTRACT_FIXTURE_V1.run.id, {
      limit: 100,
      sort: "sequence",
      direction: "asc",
    });
    expect(client.runLogs).toHaveBeenNthCalledWith(2, CONTRACT_FIXTURE_V1.run.id, {
      limit: 100,
      after: "cursor-1",
      sort: "sequence",
      direction: "asc",
    });
  });

  it("rejects run logs with cross-wired or non-monotonic identities", async () => {
    const base = logEntryV1Schema.parse(CONTRACT_FIXTURE_V1.log);
    const cases = [
      [logEntryV1Schema.parse({ ...base, run_id: "run-cross-wired" })],
      [logEntryV1Schema.parse({ ...base, attempt_id: "attempt-cross-wired" })],
      [logEntryV1Schema.parse({ ...base, service_id: "service-cross-wired" })],
      [base, logEntryV1Schema.parse({ ...base, id: "log-fixture-2" })],
      [
        logEntryV1Schema.parse({ ...base, sequence: 9 }),
        logEntryV1Schema.parse({ ...base, id: "log-fixture-2", sequence: 8 }),
      ],
    ];
    for (const entries of cases) {
      const client = mockClient();
      client.runLogs = vi.fn().mockResolvedValue(page(entries));
      const provider = createProvider(client);
      expect((await provider.refresh()).status).toBe("fresh");
      await expect(provider.getRunLogs(CONTRACT_FIXTURE_V1.run.id)).rejects.toThrow();
    }
  });

  it("requires every artifact page item to belong to the requested run and project", async () => {
    const withoutRun = mockClient();
    withoutRun.runArtifacts = vi.fn().mockResolvedValue(page([
      artifactV1Schema.parse({ ...CONTRACT_FIXTURE_V1.artifacts[0], run_id: null }),
    ]));
    expect((await createProvider(withoutRun).refresh()).status).toBe("error");

    const otherRun = mockClient();
    otherRun.runArtifacts = vi.fn().mockResolvedValue(page([
      artifactV1Schema.parse({ ...CONTRACT_FIXTURE_V1.artifacts[0], run_id: "run-cross-wired" }),
    ]));
    expect((await createProvider(otherRun).refresh()).status).toBe("error");

    const otherProject = mockClient();
    otherProject.runArtifacts = vi.fn().mockResolvedValue(page([artifactForProject("project-cross-wired")]));
    expect((await createProvider(otherProject).refresh()).status).toBe("error");
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
    const retrySource = failedRetrySourceRun();
    client.listRuns = pagedMock([[runSummary(retrySource)]]);
    client.getRun = vi.fn().mockResolvedValue(retrySource);
    client.retryRun = vi.fn().mockResolvedValue(advancedRetryRun(retrySource));
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

    const afterStart = await provider.refresh();
    if (afterStart.status !== "fresh") throw new Error("expected a fresh fixture after starting a run");
    const retried = await provider.retryRun("run-fixture-1", {
      actionId: "renderer-action-retry-0001",
      streamEpoch: afterStart.snapshot.stream.epoch,
      etag: ETAG_A,
    });
    expect(retried.id).toBe("run-fixture-1");
    expect(client.retryRun).toHaveBeenCalledWith(
      "run-fixture-1",
      { terminal_attempt_id: retrySource.current_attempt_id },
      { idempotencyKey: "renderer-action-retry-0001", ifMatch: ETAG_A },
    );
    expect(client.createRun).toHaveBeenCalledTimes(1);

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

  it("replays an ambiguous run retry with its exact stale renderer intent", async () => {
    const client = mockClient();
    const retrySource = failedRetrySourceRun();
    const accepted = advancedRetryRun(retrySource);
    client.listRuns = pagedMock([[runSummary(retrySource)]]);
    client.getRun = vi.fn().mockResolvedValue(retrySource);
    client.retryRun = vi.fn()
      .mockRejectedValueOnce(new TypeError("retry response was lost"))
      .mockResolvedValueOnce(accepted);
    const provider = createProvider(client);
    const before = await provider.refresh();
    if (before.status !== "fresh") throw new Error("expected a fresh fixture");
    const intent = {
      actionId: "renderer-action-retry-ambiguous-0001",
      streamEpoch: before.snapshot.stream.epoch,
      etag: ETAG_A,
    };

    await expect(provider.retryRun("run-fixture-1", intent))
      .rejects.toBeInstanceOf(DesktopProductAmbiguousMutationError);
    const afterUnknown = await provider.refresh();
    if (afterUnknown.status !== "fresh") throw new Error("expected a fresh fixture after ambiguity");
    expect(afterUnknown.snapshot.stream.epoch).toBeGreaterThan(intent.streamEpoch);

    await expect(provider.retryRun("run-fixture-1", intent)).resolves.toEqual(accepted);
    expect(client.retryRun).toHaveBeenNthCalledWith(
      1,
      "run-fixture-1",
      { terminal_attempt_id: retrySource.current_attempt_id },
      { idempotencyKey: intent.actionId, ifMatch: intent.etag },
    );
    expect(client.retryRun).toHaveBeenNthCalledWith(
      2,
      "run-fixture-1",
      { terminal_attempt_id: retrySource.current_attempt_id },
      { idempotencyKey: intent.actionId, ifMatch: intent.etag },
    );
  });

  it("retains exact replay authority when a 2xx retry rewrites the original prefix", async () => {
    const client = mockClient();
    const retrySource = failedRetrySourceRun();
    const accepted = advancedRetryRun(retrySource);
    const rewritten = runV1Schema.parse({
      ...accepted,
      attempts: accepted.attempts.map((attempt, index) => index === 0
        ? { ...attempt, updated_at: "2026-07-15T00:00:00Z" }
        : attempt),
    });
    client.listRuns = pagedMock([[runSummary(retrySource)]]);
    client.getRun = vi.fn().mockResolvedValue(retrySource);
    client.retryRun = vi.fn()
      .mockResolvedValueOnce(rewritten)
      .mockResolvedValueOnce(accepted);
    const provider = createProvider(client);
    const before = await provider.refresh();
    if (before.status !== "fresh") throw new Error("expected a fresh fixture");
    const intent = {
      actionId: "renderer-action-retry-prefix-0001",
      streamEpoch: before.snapshot.stream.epoch,
      etag: retrySource.etag,
    };

    await expect(provider.retryRun(retrySource.id, intent))
      .rejects.toBeInstanceOf(DesktopProductAmbiguousMutationError);
    expect(provider.getRunRetryRecovery()?.intent).toEqual(intent);
    await provider.refresh();
    await expect(provider.retryRun(retrySource.id, intent)).resolves.toEqual(accepted);
    expect(client.retryRun).toHaveBeenCalledTimes(2);
  });

  it("does not accept a 2xx retry response with two appended attempts", async () => {
    const client = mockClient();
    const retrySource = failedRetrySourceRun();
    client.listRuns = pagedMock([[runSummary(retrySource)]]);
    client.getRun = vi.fn().mockResolvedValue(retrySource);
    client.retryRun = vi.fn().mockResolvedValue(twoAdvancedRetryRuns(retrySource));
    const provider = createProvider(client);
    const before = await provider.refresh();
    if (before.status !== "fresh") throw new Error("expected a fresh fixture");

    await expect(provider.retryRun(retrySource.id, {
      actionId: "renderer-action-retry-multiple-0001",
      streamEpoch: before.snapshot.stream.epoch,
      etag: retrySource.etag,
    })).rejects.toBeInstanceOf(DesktopProductAmbiguousMutationError);
    expect(provider.getRunRetryRecovery()?.acceptedRun).toBeNull();
  });

  it("does not accept a 2xx retry response that preserves or rewrites admission authority", async () => {
    const client = mockClient();
    const retrySource = failedRetrySourceRun();
    const accepted = advancedRetryRun(retrySource);
    const rewrittenAdmission = runV1Schema.parse({
      ...accepted,
      pinned_revision: retrySource.required_revision.revision,
      admitted_at: NOW,
    });
    client.listRuns = pagedMock([[runSummary(retrySource)]]);
    client.getRun = vi.fn().mockResolvedValue(retrySource);
    client.retryRun = vi.fn().mockResolvedValue(rewrittenAdmission);
    const provider = createProvider(client);
    const before = await provider.refresh();
    if (before.status !== "fresh") throw new Error("expected a fresh fixture");

    await expect(provider.retryRun(retrySource.id, {
      actionId: "renderer-action-retry-admission-rewrite-0001",
      streamEpoch: before.snapshot.stream.epoch,
      etag: retrySource.etag,
    })).rejects.toBeInstanceOf(DesktopProductAmbiguousMutationError);
    expect(provider.getRunRetryRecovery()?.acceptedRun).toBeNull();
  });

  it("binds every accepted exact replay to the first appended attempt", async () => {
    const client = mockClient();
    const retrySource = failedRetrySourceRun();
    const accepted = advancedRetryRun(retrySource);
    const crossWiredReplay = advancedRetryRun(retrySource, "attempt-fixture-cross-wired-replay-2");
    client.listRuns = pagedMock([[runSummary(retrySource)]]);
    client.getRun = vi.fn().mockResolvedValue(retrySource);
    client.retryRun = vi.fn()
      .mockResolvedValueOnce(accepted)
      .mockResolvedValueOnce(crossWiredReplay);
    const provider = createProvider(client);
    const before = await provider.refresh();
    if (before.status !== "fresh") throw new Error("expected a fresh fixture");
    const intent = {
      actionId: "renderer-action-retry-accepted-replay-0001",
      streamEpoch: before.snapshot.stream.epoch,
      etag: retrySource.etag,
    };

    await expect(provider.retryRun(retrySource.id, intent)).resolves.toEqual(accepted);
    await expect(provider.retryRun(retrySource.id, intent))
      .rejects.toBeInstanceOf(DesktopProductAmbiguousMutationError);
    expect(provider.getRunRetryRecovery()?.acceptedRun).toEqual(accepted);
  });

  it("does not overwrite an unresolved retry with another intent", async () => {
    const client = mockClient();
    const retrySource = failedRetrySourceRun();
    client.listRuns = pagedMock([[runSummary(retrySource)]]);
    client.getRun = vi.fn().mockResolvedValue(retrySource);
    client.retryRun = vi.fn().mockRejectedValue(new TypeError("response lost"));
    const provider = createProvider(client);
    const before = await provider.refresh();
    if (before.status !== "fresh") throw new Error("expected a fresh fixture");
    const firstIntent = {
      actionId: "renderer-action-retry-unresolved-0001",
      streamEpoch: before.snapshot.stream.epoch,
      etag: retrySource.etag,
    };
    await expect(provider.retryRun(retrySource.id, firstIntent))
      .rejects.toBeInstanceOf(DesktopProductAmbiguousMutationError);

    await expect(provider.retryRun(retrySource.id, {
      ...firstIntent,
      actionId: "renderer-action-retry-overwrite-0002",
    })).rejects.toThrow(/still reconciling/i);
    expect(client.retryRun).toHaveBeenCalledTimes(1);
    expect(provider.getRunRetryRecovery()?.intent).toEqual(firstIntent);
  });

  it("does not reconcile durable retry authority while its transport is still in flight", async () => {
    const client = mockClient();
    const retrySource = failedRetrySourceRun();
    const accepted = advancedRetryRun(retrySource);
    const response = deferred<ReturnType<typeof advancedRetryRun>>();
    client.listRuns = pagedMock([[runSummary(retrySource)]]);
    client.getRun = vi.fn().mockResolvedValue(retrySource);
    client.retryRun = vi.fn(() => response.promise);
    const provider = createProvider(client);
    const before = await provider.refresh();
    if (before.status !== "fresh") throw new Error("expected a fresh fixture");
    const intent = {
      actionId: "renderer-action-retry-in-flight-0001",
      streamEpoch: before.snapshot.stream.epoch,
      etag: retrySource.etag,
    };

    const request = provider.retryRun(retrySource.id, intent);
    await Promise.resolve();
    client.listRuns = pagedMock([[runSummary(accepted)]]);
    client.getRun = vi.fn().mockResolvedValue(accepted);
    const concurrent = await provider.refresh();
    expect(concurrent.status).toBe("fresh");
    expect(provider.getRunRetryRecovery()?.intent).toEqual(intent);
    await expect(provider.retryRun(retrySource.id, intent)).rejects.toThrow(/already sending/i);

    response.resolve(accepted);
    await expect(request).resolves.toEqual(accepted);
    expect(provider.getRunRetryRecovery()?.acceptedRun).toEqual(accepted);
  });

  it("serializes refresh reconciliation against exact retry transport", async () => {
    const client = mockClient();
    const retrySource = failedRetrySourceRun();
    const accepted = advancedRetryRun(retrySource);
    const clearStarted = deferred<void>();
    const clearWrite = deferred<void>();
    let durable: string | null = null;
    client.listRuns = pagedMock([[runSummary(retrySource)]]);
    client.getRun = vi.fn().mockResolvedValue(retrySource);
    client.retryRun = vi.fn().mockRejectedValueOnce(new TypeError("retry response was lost"));
    const provider = createProvider(client, undefined, {
      read: () => durable,
      write: async (next) => {
        durable = next;
        if (next === null) {
          clearStarted.resolve(undefined);
          await clearWrite.promise;
        }
      },
    });
    const before = await provider.refresh();
    if (before.status !== "fresh") throw new Error("expected a fresh fixture");
    const intent = {
      actionId: "renderer-action-retry-refresh-clear-0001",
      streamEpoch: before.snapshot.stream.epoch,
      etag: retrySource.etag,
    };
    await expect(provider.retryRun(retrySource.id, intent))
      .rejects.toBeInstanceOf(DesktopProductAmbiguousMutationError);

    const retryTransport = vi.fn().mockResolvedValue(accepted);
    client.retryRun = retryTransport;
    client.listRuns = pagedMock([[runSummary(accepted)]]);
    client.getRun = vi.fn().mockResolvedValue(accepted);
    const reconciliation = provider.refresh();
    await clearStarted.promise;

    await expect(provider.retryRun(retrySource.id, intent)).rejects.toThrow(/wait|updating/i);
    expect(retryTransport).not.toHaveBeenCalled();
    clearWrite.reject(new Error("clear directory fsync failed"));
    expect((await reconciliation).status).toBe("error");
    expect(() => provider.getRunRetryRecovery()).toThrow(/restart/i);
    await expect(provider.retryRun(retrySource.id, intent)).rejects.toThrow(/restart/i);
    expect(retryTransport).not.toHaveBeenCalled();
  });

  it("claims the first retry synchronously before its durable write settles", async () => {
    const client = mockClient();
    const retrySource = failedRetrySourceRun();
    const accepted = advancedRetryRun(retrySource);
    const firstWrite = deferred<void>();
    const writes = vi.fn(() => firstWrite.promise);
    client.listRuns = pagedMock([[runSummary(retrySource)]]);
    client.getRun = vi.fn().mockResolvedValue(retrySource);
    client.retryRun = vi.fn().mockResolvedValue(accepted);
    const provider = createProvider(client, undefined, {
      read: () => null,
      write: writes,
    });
    const before = await provider.refresh();
    if (before.status !== "fresh") throw new Error("expected a fresh fixture");
    const firstIntent = {
      actionId: "renderer-action-retry-first-writer-0001",
      streamEpoch: before.snapshot.stream.epoch,
      etag: retrySource.etag,
    };

    const first = provider.retryRun(retrySource.id, firstIntent);
    await Promise.resolve();
    expect(writes).toHaveBeenCalledTimes(1);
    await expect(provider.retryRun(retrySource.id, {
      ...firstIntent,
      actionId: "renderer-action-retry-second-writer-0002",
    })).rejects.toThrow(/already sending/i);
    expect(writes).toHaveBeenCalledTimes(1);
    expect(client.retryRun).not.toHaveBeenCalled();

    firstWrite.resolve();
    await expect(first).resolves.toEqual(accepted);
    expect(client.retryRun).toHaveBeenCalledTimes(1);
  });

  it("overlays an accepted retry across missing aggregates and keeps provider operations coherent", async () => {
    const client = mockClient();
    const retrySource = failedRetrySourceRun();
    const accepted = advancedRetryRun(retrySource);
    client.listRuns = pagedMock([[runSummary(retrySource)]]);
    client.getRun = vi.fn().mockResolvedValue(retrySource);
    client.retryRun = vi.fn().mockResolvedValue(accepted);
    const provider = createProvider(client);
    const before = await provider.refresh();
    if (before.status !== "fresh") throw new Error("expected a fresh fixture");
    await provider.retryRun(retrySource.id, {
      actionId: "renderer-action-retry-overlay-0001",
      streamEpoch: before.snapshot.stream.epoch,
      etag: retrySource.etag,
    });
    client.listRuns = pagedMock([[]]);

    const lagging = await provider.refresh();
    if (lagging.status !== "fresh") throw new Error("expected a fresh lagging fixture");
    expect(lagging.snapshot.runs).toEqual([accepted]);
    expect(provider.getRunRetryRecovery()?.acceptedRun).toEqual(accepted);

    const otherAppend = advancedRetryRun(retrySource, "attempt-fixture-other-retry-2");
    client.listRuns = pagedMock([[runSummary(otherAppend)]]);
    client.getRun = vi.fn().mockResolvedValue(otherAppend);
    const otherAppendRefresh = await provider.refresh();
    if (otherAppendRefresh.status !== "fresh") throw new Error("expected a fresh cross-wired fixture");
    expect(otherAppendRefresh.snapshot.runs).toEqual([accepted]);
    expect(provider.getRunRetryRecovery()?.acceptedRun).toEqual(accepted);

    const conflicting = twoAdvancedRetryRuns(retrySource);
    client.listRuns = pagedMock([[runSummary(conflicting)]]);
    client.getRun = vi.fn().mockResolvedValue(conflicting);
    const conflictingRefresh = await provider.refresh();
    if (conflictingRefresh.status !== "fresh") throw new Error("expected a fresh conflicting fixture");
    expect(conflictingRefresh.snapshot.runs).toEqual([accepted]);
    expect(provider.getRunRetryRecovery()?.acceptedRun).toEqual(accepted);

    client.cancelRun = vi.fn().mockResolvedValue(accepted);
    await expect(provider.cancelRun(accepted.id, {
      actionId: "renderer-action-cancel-overlay-0001",
      streamEpoch: conflictingRefresh.snapshot.stream.epoch,
      etag: accepted.etag,
    })).resolves.toEqual(accepted);

    client.listRuns = pagedMock([[runSummary(accepted)]]);
    client.getRun = vi.fn().mockResolvedValue(accepted);
    const converged = await provider.refresh();
    expect(converged.status).toBe("fresh");
    expect(provider.getRunRetryRecovery()).toBeNull();
  });

  it("restores an ambiguous exact retry after provider recreation", async () => {
    const retrySource = failedRetrySourceRun();
    const accepted = advancedRetryRun(retrySource);
    const store = memoryRetryRecoveryStore();
    const firstClient = mockClient();
    firstClient.listRuns = pagedMock([[runSummary(retrySource)]]);
    firstClient.getRun = vi.fn().mockResolvedValue(retrySource);
    firstClient.retryRun = vi.fn().mockRejectedValue(new TypeError("response lost"));
    const first = createProvider(firstClient, undefined, store);
    const before = await first.refresh();
    if (before.status !== "fresh") throw new Error("expected a fresh fixture");
    const intent = {
      actionId: "renderer-action-retry-restart-0001",
      streamEpoch: before.snapshot.stream.epoch,
      etag: retrySource.etag,
    };
    await expect(first.retryRun(retrySource.id, intent))
      .rejects.toBeInstanceOf(DesktopProductAmbiguousMutationError);

    const secondClient = mockClient();
    secondClient.listRuns = pagedMock([[runSummary(retrySource)]]);
    secondClient.getRun = vi.fn().mockResolvedValue(retrySource);
    secondClient.retryRun = vi.fn().mockResolvedValue(accepted);
    const second = createProvider(secondClient, undefined, store);
    const afterRestart = await second.refresh();
    if (afterRestart.status !== "fresh") throw new Error("expected a fresh restarted fixture");

    expect(second.getRunRetryRecovery()?.intent).toEqual(intent);
    await expect(second.retryRun(retrySource.id, intent)).resolves.toEqual(accepted);
    expect(secondClient.retryRun).toHaveBeenCalledWith(
      retrySource.id,
      { terminal_attempt_id: retrySource.current_attempt_id },
      { idempotencyKey: intent.actionId, ifMatch: intent.etag },
    );
  });

  it("does not send a retry when durable recovery cannot be written", async () => {
    const client = mockClient();
    const retrySource = failedRetrySourceRun();
    client.listRuns = pagedMock([[runSummary(retrySource)]]);
    client.getRun = vi.fn().mockResolvedValue(retrySource);
    const provider = createProvider(client, undefined, {
      read: () => null,
      write: async () => { throw new Error("native sync failed"); },
    });
    const before = await provider.refresh();
    if (before.status !== "fresh") throw new Error("expected a fresh fixture");

    await expect(provider.retryRun(retrySource.id, {
      actionId: "renderer-action-retry-storage-0001",
      streamEpoch: before.snapshot.stream.epoch,
      etag: retrySource.etag,
    })).rejects.toThrow(/could not save local retry recovery/i);
    expect(client.retryRun).not.toHaveBeenCalled();
  });

  it("blocks exact replay after persisting an accepted retry becomes uncertain", async () => {
    const client = mockClient();
    const retrySource = failedRetrySourceRun();
    const accepted = advancedRetryRun(retrySource);
    let durable: string | null = null;
    let writes = 0;
    client.listRuns = pagedMock([[runSummary(retrySource)]]);
    client.getRun = vi.fn().mockResolvedValue(retrySource);
    client.retryRun = vi.fn().mockResolvedValue(accepted);
    const provider = createProvider(client, undefined, {
      read: () => durable,
      write: async (next) => {
        writes += 1;
        durable = next;
        if (writes === 2) throw new Error("accepted recovery response was lost");
      },
    });
    const before = await provider.refresh();
    if (before.status !== "fresh") throw new Error("expected a fresh fixture");
    const intent = {
      actionId: "renderer-action-retry-accepted-storage-0001",
      streamEpoch: before.snapshot.stream.epoch,
      etag: retrySource.etag,
    };

    await expect(provider.retryRun(retrySource.id, intent)).rejects.toThrow(/restart/i);
    expect(client.retryRun).toHaveBeenCalledTimes(1);
    expect(() => provider.getRunRetryRecovery()).toThrow(/restart/i);
    expect((await provider.refresh()).status).toBe("fresh");
    await expect(provider.retryRun(retrySource.id, intent)).rejects.toThrow(/restart/i);
    expect(client.retryRun).toHaveBeenCalledTimes(1);
  });

  it("blocks exact replay after clearing a rejected retry becomes uncertain", async () => {
    const client = mockClient();
    const retrySource = failedRetrySourceRun();
    let durable: string | null = null;
    let writes = 0;
    client.listRuns = pagedMock([[runSummary(retrySource)]]);
    client.getRun = vi.fn().mockResolvedValue(retrySource);
    client.retryRun = vi.fn().mockRejectedValue(new DesktopApiError(apiErrorV1Schema.parse({
      ...CONTRACT_FIXTURE_V1.error,
      code: "run_retry_rejected",
      message: "The failed run cannot be retried.",
      category: "run",
      retryable: false,
      repair_action: "user_action_required",
      details: {},
    })));
    const provider = createProvider(client, undefined, {
      read: () => durable,
      write: async (next) => {
        writes += 1;
        durable = next;
        if (writes === 2) throw new Error("clear recovery response was lost");
      },
    });
    const before = await provider.refresh();
    if (before.status !== "fresh") throw new Error("expected a fresh fixture");
    const intent = {
      actionId: "renderer-action-retry-clear-storage-0001",
      streamEpoch: before.snapshot.stream.epoch,
      etag: retrySource.etag,
    };

    await expect(provider.retryRun(retrySource.id, intent)).rejects.toThrow(/restart/i);
    expect(client.retryRun).toHaveBeenCalledTimes(1);
    expect(() => provider.getRunRetryRecovery()).toThrow(/restart/i);
    expect((await provider.refresh()).status).toBe("fresh");
    await expect(provider.retryRun(retrySource.id, intent)).rejects.toThrow(/restart/i);
    expect(client.retryRun).toHaveBeenCalledTimes(1);
  });

  it("does not retain exact replay authority after a deterministic retry rejection", async () => {
    const client = mockClient();
    client.retryRun = vi.fn().mockRejectedValue(new DesktopApiError(apiErrorV1Schema.parse({
      ...CONTRACT_FIXTURE_V1.error,
      code: "run_retry_rejected",
      message: "The failed run cannot be retried.",
      category: "run",
      retryable: false,
      repair_action: "user_action_required",
      details: {},
    })));
    const provider = createProvider(client);
    const before = await provider.refresh();
    if (before.status !== "fresh") throw new Error("expected a fresh fixture");
    const intent = {
      actionId: "renderer-action-retry-rejected-0001",
      streamEpoch: before.snapshot.stream.epoch,
      etag: ETAG_A,
    };

    await expect(provider.retryRun("run-fixture-1", intent)).rejects.toBeInstanceOf(DesktopApiError);
    await provider.refresh();
    await expect(provider.retryRun("run-fixture-1", intent)).rejects.toThrow(/refresh/i);
    expect(client.retryRun).toHaveBeenCalledTimes(1);
  });

  it("retains exact replay authority when Core reports an unknown mutation outcome", async () => {
    const client = mockClient();
    const retrySource = failedRetrySourceRun();
    client.listRuns = pagedMock([[runSummary(retrySource)]]);
    client.getRun = vi.fn().mockResolvedValue(retrySource);
    client.retryRun = vi.fn().mockRejectedValue(new DesktopApiError(apiErrorV1Schema.parse({
      ...CONTRACT_FIXTURE_V1.error,
      code: "core_mutation_outcome_unknown",
      message: "OpenEvo Core mutation outcome is unknown.",
      category: "run",
      http_status: 503,
      retryable: true,
      repair_action: "openevo_can_retry",
      details: {},
    })));
    const provider = createProvider(client);
    const before = await provider.refresh();
    if (before.status !== "fresh") throw new Error("expected a fresh fixture");
    const intent = {
      actionId: "renderer-action-retry-core-unknown-0001",
      streamEpoch: before.snapshot.stream.epoch,
      etag: retrySource.etag,
    };

    await expect(provider.retryRun(retrySource.id, intent))
      .rejects.toBeInstanceOf(DesktopProductAmbiguousMutationError);
    expect(provider.getRunRetryRecovery()).toMatchObject({
      runId: retrySource.id,
      intent,
      originalRun: { current_attempt_id: retrySource.current_attempt_id },
    });
    await expect(provider.retryRun(retrySource.id, intent))
      .rejects.toBeInstanceOf(DesktopProductAmbiguousMutationError);
    expect(client.retryRun).toHaveBeenNthCalledWith(
      2,
      retrySource.id,
      { terminal_attempt_id: retrySource.current_attempt_id },
      { idempotencyKey: intent.actionId, ifMatch: intent.etag },
    );
  });

  it("rejects schema-valid cross-wired mutation, action, content, and diff responses", async () => {
    const client = mockClient();
    const provider = createProvider(client);
    const refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("expected a fresh fixture");
    const resourceIntent = { actionId: "renderer-attack-action-0001", streamEpoch: refreshed.snapshot.stream.epoch, etag: ETAG_A };
    const createIntent = { actionId: "renderer-attack-create-0001", streamEpoch: refreshed.snapshot.stream.epoch };

    client.createProfile = vi.fn().mockResolvedValue(profile({ name: "Cross-wired profile" }));
    await expect(provider.createProfile(profileInput(), createIntent)).rejects.toThrow(/does not match the request/i);
    client.updateProfile = vi.fn().mockResolvedValue(profile({ profile_id: "profile-cross-wired" }));
    await expect(provider.updateProfile("profile-fixture-1", { name: "Updated" }, resourceIntent)).rejects.toThrow(/wrong profile/i);

    client.createProject = vi.fn().mockResolvedValue(project({ name: "Cross-wired project" }));
    await expect(provider.createProject(projectInput(), createIntent)).rejects.toThrow(/does not match the request/i);
    client.createProject = vi.fn().mockResolvedValue(project({ evolution_configuration_state: "configured" }));
    await expect(provider.createProject({
      ...projectInput(),
      evolution_configuration_state: "pending",
    }, createIntent)).rejects.toThrow(/does not match the request/i);
    client.updateProject = vi.fn().mockResolvedValue(project({ project_id: "project-cross-wired" }));
    await expect(provider.updateProject("project-fixture-1", { name: "Updated" }, resourceIntent)).rejects.toThrow(/wrong project/i);

    client.createRun = vi.fn().mockResolvedValue(runForProject("project-cross-wired"));
    await expect(provider.startRun({ ...resourceIntent, projectId: "project-fixture-1" })).rejects.toThrow(/another project/i);
    client.cancelRun = vi.fn().mockResolvedValue(runWithId("run-cross-wired"));
    await expect(provider.cancelRun("run-fixture-1", resourceIntent)).rejects.toThrow(/wrong run/i);
    client.cancelRun = vi.fn().mockResolvedValue(runForProject("project-cross-wired"));
    await expect(provider.cancelRun("run-fixture-1", resourceIntent)).rejects.toThrow(/wrong run/i);
    client.retryRun = vi.fn().mockResolvedValue(runWithId("run-cross-wired"));
    await expect(provider.retryRun("run-fixture-1", resourceIntent)).rejects.toMatchObject({
      name: "DesktopProductAmbiguousMutationError",
      cause: expect.objectContaining({ message: expect.stringMatching(/wrong run/i) }),
    });
    client.retryRun = vi.fn().mockResolvedValue(runForProject("project-cross-wired"));
    await expect(provider.retryRun("run-fixture-1", resourceIntent)).rejects.toMatchObject({
      name: "DesktopProductAmbiguousMutationError",
      cause: expect.objectContaining({ message: expect.stringMatching(/wrong run/i) }),
    });

    client.connectProfile = vi.fn().mockResolvedValue(localOperation("profile_connect", "profile", "profile-cross-wired"));
    await expect(provider.connectProfile("profile-fixture-1", resourceIntent)).rejects.toThrow(/another resource/i);
    client.connectProfile = vi.fn().mockResolvedValue(crossWiredProfileOperationResult());
    await expect(provider.connectProfile("profile-fixture-1", resourceIntent)).rejects.toThrow(/result does not match/i);
    client.acceptProfileHostKey = vi.fn().mockResolvedValue(localOperation("host_key_accept", "profile", "profile-cross-wired"));
    await expect(provider.acceptHostKey("profile-fixture-1", {
      algorithm: "ssh-ed25519",
      fingerprint: "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    }, resourceIntent)).rejects.toThrow(/another resource/i);
    client.activateProject = vi.fn().mockResolvedValue(localOperation("project_activate", "project", "project-cross-wired"));
    await expect(provider.activateProject("project-fixture-1", resourceIntent)).rejects.toThrow(/another resource/i);
    client.cancelOperation = vi.fn().mockResolvedValue(localOperation("project_repair", "project", "project-fixture-1", "operation-cross-wired"));
    await expect(provider.cancelOperation("operation-fixture-1", resourceIntent)).rejects.toThrow(/wrong operation/i);
    client.artifactContent = vi.fn().mockResolvedValue(artifactContentV1Schema.parse({
      ...CONTRACT_FIXTURE_V1.artifactContent,
      artifact_id: "artifact-cross-wired",
    }));
    await expect(provider.getArtifactContent("artifact-memory-fixture-1")).rejects.toThrow(/wrong artifact/i);
    client.artifactDiff = vi.fn().mockResolvedValue(emptyArtifactDiff("artifact-cross-wired"));
    await expect(provider.getArtifactDiff("artifact-memory-fixture-1")).rejects.toThrow(/wrong artifact/i);
  });

  it("uses only strict native source bridge results", async () => {
    const client = mockClient();
    const native = {
      selectProjectSource: vi.fn().mockResolvedValue({
        kind: "native_folder_snapshot",
        display_name: "Selected source",
        import_ref: CONTRACT_FIXTURE_V1.workspaceImport,
      }),
      cancelProjectSource: vi.fn(),
      settleProjectSource: vi.fn(),
    };
    const provider = new LocalApiDesktopProductProvider({
      client,
      native,
      fetch: vi.fn<FetchLike>(),
      retryRecoveryStore: memoryRetryRecoveryStore(),
    });
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
    await provider.cancelProjectSource("renderer-action-source-0001");
    expect(native.cancelProjectSource).toHaveBeenCalledWith("renderer-action-source-0001");

    native.selectProjectSource.mockResolvedValueOnce({
      kind: "scratch",
      display_name: "Cross-wired scratch source",
      import_ref: null,
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

  it("loads a fresh install without requesting project-bound Core collections", async () => {
    const client = mockClient();
    client.state = vi.fn().mockResolvedValue(disconnectedState());
    client.listProfiles = vi.fn().mockResolvedValue(page([]));
    client.listProjects = vi.fn().mockResolvedValue(page([]));

    const result = await createProvider(client).refresh();

    expect(result.status).toBe("fresh");
    if (result.status !== "fresh") return;
    expect(result.snapshot.profiles).toEqual([]);
    expect(result.snapshot.projects).toEqual([]);
    expect(result.snapshot.runs).toEqual([]);
    expect(result.snapshot.services).toEqual([]);
    expect(result.snapshot.capability).toBeNull();
    expect(result.snapshot.validation).toBeNull();
    expect(client.listRuns).not.toHaveBeenCalled();
    expect(client.listServices).not.toHaveBeenCalled();
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
    runLogs: vi.fn().mockResolvedValue(page([logEntryV1Schema.parse(CONTRACT_FIXTURE_V1.log)])),
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
    retryRun: vi.fn().mockResolvedValue(runV1Schema.parse(CONTRACT_FIXTURE_V1.run)),
    cancelOperation: vi.fn().mockResolvedValue(localOperationV1Schema.parse(CONTRACT_FIXTURE_V1.operation)),
    createDiagnostic: vi.fn().mockResolvedValue(diagnosticReportV1Schema.parse(CONTRACT_FIXTURE_V1.diagnostic)),
    getDiagnostic: vi.fn().mockResolvedValue(diagnosticReportV1Schema.parse(CONTRACT_FIXTURE_V1.diagnostic)),
    artifactContent: vi.fn().mockResolvedValue(CONTRACT_FIXTURE_V1.artifactContent),
    artifactDiff: vi.fn().mockResolvedValue(CONTRACT_FIXTURE_V1.artifactDiff),
    repairProject: vi.fn().mockResolvedValue(localOperationV1Schema.parse(CONTRACT_FIXTURE_V1.operation)),
    eventStreamRequest: vi.fn().mockResolvedValue({ url: "http://127.0.0.1/events", headers: {} }),
  } as unknown as DesktopApiClientV1 & Record<string, ReturnType<typeof vi.fn>>;
}

function createProvider(
  client: DesktopApiClientV1,
  fetch: FetchLike = vi.fn<FetchLike>(),
  retryRecoveryStore: ProductRunRetryRecoveryStore | null = memoryRetryRecoveryStore(),
) {
  return new LocalApiDesktopProductProvider({
    client,
    native: {
      selectProjectSource: vi.fn(),
      cancelProjectSource: vi.fn(),
      settleProjectSource: vi.fn(),
    },
    fetch,
    reconnectDelaysMs: [],
    retryRecoveryStore,
  });
}

function memoryRetryRecoveryStore(): ProductRunRetryRecoveryStore {
  let value: string | null = null;
  return {
    read: () => value,
    write: (next) => { value = next; },
  };
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
    execution_mode_capabilities: RELEASE_EXECUTION_MODE_CAPABILITIES_FIXTURE_V1,
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

function disconnectedState() {
  return desktopStateV1Schema.parse({
    schema_version: "1",
    observed_at: NOW,
    contract: {
      selected_major: 1,
      desktop_openapi_sha256: A,
      core_openapi_sha256: null,
      compatible: true,
    },
    execution_mode_capabilities: RELEASE_EXECUTION_MODE_CAPABILITIES_FIXTURE_V1,
    core: {
      state: "disconnected",
      profile_id: null,
      active_tunnel: false,
      operation_id: null,
      host_key_review: null,
      core: null,
      failure: null,
    },
    active_project: null,
    pending_operation_ids: [],
  });
}

function profile(overrides: Record<string, unknown> = {}) {
  return remoteProfileV1Schema.parse({ ...CONTRACT_FIXTURE_V1.profile, ...overrides });
}

function project(overrides: Record<string, unknown> = {}) {
  return projectV1Schema.parse({ ...CONTRACT_FIXTURE_V1.project, ...overrides });
}

function profileInput() {
  const value = profile();
  return {
    name: value.name,
    host: value.host,
    port: value.port,
    user: value.user,
    authentication_kind: value.authentication_kind,
    proxy: value.proxy,
  };
}

function projectInput() {
  const value = project();
  return {
    name: value.name,
    profile_id: value.profile_id,
    task: value.task,
    source: value.source,
    execution: value.execution,
    evolution: value.evolution,
    evolution_configuration_state: value.evolution_configuration_state,
  };
}

function run(overrides: Record<string, unknown> = {}) {
  return runV1Schema.parse({ ...CONTRACT_FIXTURE_V1.run, ...overrides });
}

function advancedRetryRun(source = run(), attemptId = "attempt-fixture-retry-2") {
  const current = source.current_attempt ?? source.attempts.at(-1);
  if (!current) throw new Error("A retry fixture requires an original attempt.");
  const queuedReason = {
    code: "admission_pending" as const,
    summary: "The retry was admitted.",
    retry_after_seconds: null,
  };
  const appended = {
    ...current,
    id: attemptId,
    number: source.attempt_count + 1,
    status: "queued" as const,
    queued_reason: queuedReason,
    error: null,
    started_at: null,
    finished_at: null,
  };
  return runV1Schema.parse({
    ...source,
    status: "queued",
    queued_reason: queuedReason,
    current_attempt_id: appended.id,
    current_attempt: appended,
    current_error: null,
    attempt_count: source.attempt_count + 1,
    attempts: [...source.attempts, appended],
    pinned_revision: null,
    admitted_at: null,
    started_at: null,
    finished_at: null,
    etag: `"${"e".repeat(64)}"`,
  });
}

function failedRetrySourceRun() {
  const source = run();
  const current = source.current_attempt ?? source.attempts.at(-1);
  if (!current) throw new Error("A failed retry fixture requires an attempt.");
  const error = apiErrorV1Schema.parse({
    ...CONTRACT_FIXTURE_V1.error,
    code: "session_execution_failed",
    message: "The session failed.",
    category: "run",
  });
  const failed = {
    ...current,
    status: "failed" as const,
    queued_reason: null,
    error,
    started_at: current.started_at ?? NOW,
    finished_at: NOW,
  };
  return runV1Schema.parse({
    ...source,
    status: "failed",
    queued_reason: null,
    current_attempt_id: failed.id,
    current_attempt: failed,
    current_error: error,
    attempts: [failed],
    finished_at: NOW,
  });
}

function twoAdvancedRetryRuns(source: ReturnType<typeof failedRetrySourceRun>) {
  const once = advancedRetryRun(source);
  const firstRetry = once.current_attempt;
  if (!firstRetry) throw new Error("A two-retry fixture requires its first retry.");
  const failedError = apiErrorV1Schema.parse({
    ...CONTRACT_FIXTURE_V1.error,
    code: "session_execution_failed",
    message: "The first retry failed.",
    category: "run",
  });
  const failedFirstRetry = {
    ...firstRetry,
    status: "failed" as const,
    queued_reason: null,
    error: failedError,
    started_at: NOW,
    finished_at: NOW,
  };
  const queuedReason = {
    code: "admission_pending" as const,
    summary: "Another retry was admitted.",
    retry_after_seconds: null,
  };
  const secondRetry = {
    ...firstRetry,
    id: "attempt-fixture-retry-3",
    number: once.attempt_count + 1,
    status: "queued" as const,
    queued_reason: queuedReason,
    error: null,
    started_at: null,
    finished_at: null,
  };
  return runV1Schema.parse({
    ...once,
    status: "queued",
    queued_reason: queuedReason,
    current_attempt_id: secondRetry.id,
    current_attempt: secondRetry,
    current_error: null,
    attempt_count: once.attempt_count + 1,
    attempts: [...once.attempts.slice(0, -1), failedFirstRetry, secondRetry],
    etag: `"${"d".repeat(64)}"`,
  });
}

function runWithId(id: string) {
  const value = run();
  const attempts = value.attempts.map((attempt) => ({ ...attempt, run_id: id }));
  return runV1Schema.parse({
    ...value,
    id,
    attempts,
    current_attempt: attempts.at(-1) ?? null,
  });
}

function runForProject(projectId: string) {
  const value = run();
  const revision = { ...value.required_revision.revision, project_id: projectId };
  return runV1Schema.parse({
    ...value,
    project_id: projectId,
    pinned_revision: revision,
    required_revision: { ...value.required_revision, revision },
  });
}

function runSummary(value: ReturnType<typeof run>) {
  const { attempts: _attempts, ...summary } = value;
  return summary;
}

function artifactForProject(projectId: string) {
  const value = artifactV1Schema.parse(CONTRACT_FIXTURE_V1.artifacts[0]);
  return artifactV1Schema.parse({
    ...value,
    project_id: projectId,
    produced_revision: { ...value.produced_revision, project_id: projectId },
    membership_revisions: value.membership_revisions.map((revision) => ({ ...revision, project_id: projectId })),
  });
}

function localOperation(
  operationKind: LocalOperationV1["operation_kind"],
  resourceType: "profile" | "project",
  resourceId: string,
  operationId = "operation-fixture-1",
) {
  return localOperationV1Schema.parse({
    ...CONTRACT_FIXTURE_V1.operation,
    operation_id: operationId,
    operation_kind: operationKind,
    resource: { resource_type: resourceType, resource_id: resourceId },
  });
}

function crossWiredProfileOperationResult() {
  return localOperationV1Schema.parse({
    ...CONTRACT_FIXTURE_V1.operation,
    operation_kind: "profile_connect",
    state: "succeeded",
    resource: { resource_type: "profile", resource_id: "profile-fixture-1" },
    result: { kind: "connection", profile_id: "profile-cross-wired", connection_state: "connected" },
    started_at: NOW,
    finished_at: NOW,
  });
}

function emptyArtifactDiff(artifactId: string) {
  return artifactDiffV1Schema.parse({
    schema_version: "1",
    artifact_id: artifactId,
    artifact_content_sha256: A,
    previous_artifact_id: "artifact-previous-fixture-1",
    previous_artifact_content_sha256: B,
    document_changes: [],
    total_document_changes: 0,
    total_hunks: 0,
    total_lines: 0,
    truncated: false,
  });
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
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

async function settle(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
}
