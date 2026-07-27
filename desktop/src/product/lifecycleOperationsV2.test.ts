import { describe, expect, it, vi } from "vitest";
import type {
  LifecycleLogPageV2,
  LifecycleOperationRefV2,
  LifecycleOperationV2,
  OperationV2,
} from "../api/v2/schemas";
import {
  CoreOperationControllerV2,
  LifecycleOperationControllerV2,
} from "./lifecycleOperationsV2";

const NOW = "2026-07-27T08:00:00Z";
const LATER = "2026-07-27T08:00:01Z";
const DIGEST = "a".repeat(64);
const ETAG = `"${"b".repeat(64)}"`;
const OTHER_ETAG = `"${"c".repeat(64)}"`;

function lifecycle(
  overrides: Partial<LifecycleOperationV2> = {},
): LifecycleOperationV2 {
  return {
    schema_version: "2",
    operation_id: "lifecycle-connect-1",
    kind: "profile_connect",
    resource: { resource_kind: "profile", resource_id: "profile-lab" },
    request_sha256: DIGEST,
    status: "running",
    phase: "connecting",
    phase_index: 3,
    phase_total: 17,
    progress: { kind: "indeterminate" },
    cancellable: true,
    result: null,
    failure: null,
    log_sequence_high_watermark: 0,
    created_at: NOW,
    started_at: NOW,
    updated_at: NOW,
    finished_at: null,
    etag: ETAG,
    ...overrides,
  };
}

function lifecycleRef(operation: LifecycleOperationV2): LifecycleOperationRefV2 {
  return {
    schema_version: "2",
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

function logPage(
  operationId: string,
  start: number,
  end: number,
  nextCursor: string | null,
): LifecycleLogPageV2 {
  return {
    schema_version: "2",
    operation_id: operationId,
    dropped_before_sequence: 0,
    items: Array.from({ length: end - start + 1 }, (_, offset) => ({
      schema_version: "2" as const,
      operation_id: operationId,
      sequence: start + offset,
      occurred_at: NOW,
      source: (start + offset) % 2 === 0 ? "ssh_stdout" as const : "daemon_stderr" as const,
      text: `process line ${start + offset}`,
      truncated: false,
    })),
    next_cursor: nextCursor,
    has_more: nextCursor !== null,
  };
}

function coreOperation(overrides: Partial<OperationV2> = {}): OperationV2 {
  return {
    schema_version: "2",
    operation_id: "core-operation-1",
    kind: "service_restart",
    status: "running",
    progress_completed: 1,
    progress_total: 4,
    error: null,
    created_at: NOW,
    updated_at: NOW,
    etag: ETAG,
    ...overrides,
  };
}

describe("lifecycle operation controller v2", () => {
  it("discovers pending operations, fetches authoritative logs, and retains a 200-line tail", async () => {
    const operation = lifecycle({ log_sequence_high_watermark: 250 });
    const transport = {
      getLifecycleOperation: vi.fn().mockResolvedValue(operation),
      lifecycleOperationLogs: vi.fn(async (_operationId: string, options?: { after?: string }) => {
        if (options?.after === "cursor-100") return logPage(operation.operation_id, 101, 200, "cursor-200");
        if (options?.after === "cursor-200") return logPage(operation.operation_id, 201, 250, null);
        return logPage(operation.operation_id, 1, 100, "cursor-100");
      }),
      cancelLifecycleOperation: vi.fn(),
    };
    const controller = new LifecycleOperationControllerV2(transport);

    await controller.synchronize([lifecycleRef(operation)]);

    const observed = controller.get(operation.operation_id)!;
    expect(observed.operation).toEqual(operation);
    expect(observed.logs).toHaveLength(200);
    expect(observed.logs[0]?.sequence).toBe(51);
    expect(observed.logs.at(-1)?.sequence).toBe(250);
    expect(observed.droppedBeforeSequence).toBe(0);
    expect(observed.hasOlderLogs).toBe(true);
    expect(observed.hasNewerLogs).toBe(false);

    const older = await controller.loadOlderLogs(operation.operation_id);
    expect(older.logs.map((entry) => entry.sequence)).toEqual(
      Array.from({ length: 50 }, (_, index) => index + 1),
    );
    expect(older.hasOlderLogs).toBe(false);
    expect(older.hasNewerLogs).toBe(true);

    const latest = await controller.loadLatestLogs(operation.operation_id);
    expect(latest.logs[0]?.sequence).toBe(51);
    expect(latest.logs.at(-1)?.sequence).toBe(250);
    expect(latest.hasOlderLogs).toBe(true);
    expect(latest.hasNewerLogs).toBe(false);
  });

  it("fails closed when lifecycle status, phase, progress, or log authority regresses", () => {
    const controller = new LifecycleOperationControllerV2({
      getLifecycleOperation: vi.fn(),
      lifecycleOperationLogs: vi.fn(),
      cancelLifecycleOperation: vi.fn(),
    });
    controller.observe(lifecycle({
      phase: "transferring",
      phase_index: 6,
      progress: { kind: "bytes", completed: 50, total: 100 },
      log_sequence_high_watermark: 5,
    }));

    expect(() => controller.observe(lifecycle({
      phase: "remote_preflight",
      phase_index: 5,
      progress: { kind: "bytes", completed: 49, total: 100 },
      log_sequence_high_watermark: 4,
      updated_at: LATER,
      etag: OTHER_ETAG,
    }))).toThrow(/regressed/i);
  });

  it("polls with bounded exponential delays and resets after authoritative progress", async () => {
    const observations = [
      lifecycle(),
      lifecycle({ updated_at: LATER, etag: OTHER_ETAG }),
      lifecycle({
        phase: "remote_preflight",
        phase_index: 5,
        updated_at: "2026-07-27T08:00:02Z",
        etag: `"${"d".repeat(64)}"`,
      }),
      lifecycle({
        status: "succeeded",
        phase: "finalizing",
        phase_index: 16,
        progress: null,
        cancellable: false,
        result: { result_kind: "profile", profile_id: "profile-lab", connection_generation: 5 },
        updated_at: "2026-07-27T08:00:03Z",
        finished_at: "2026-07-27T08:00:03Z",
        etag: `"${"e".repeat(64)}"`,
      }),
    ];
    const waits: number[] = [];
    const transport = {
      getLifecycleOperation: vi.fn(async () => observations.shift()!),
      lifecycleOperationLogs: vi.fn(async () => logPage("lifecycle-connect-1", 1, 0, null)),
      cancelLifecycleOperation: vi.fn(),
    };
    const controller = new LifecycleOperationControllerV2(transport, {
      wait: async (milliseconds) => { waits.push(milliseconds); },
    });
    controller.observe(lifecycle());

    const terminal = await controller.pollUntilTerminal("lifecycle-connect-1");

    expect(terminal.status).toBe("succeeded");
    expect(waits).toEqual([500, 1_000, 2_000, 500]);
  });

  it("restarts log pagination once when a signed cursor expires", async () => {
    const operation = lifecycle({ log_sequence_high_watermark: 2 });
    let expired = false;
    const transport = {
      getLifecycleOperation: vi.fn().mockResolvedValue(operation),
      lifecycleOperationLogs: vi.fn(async (_operationId: string, options?: { after?: string }) => {
        if (options?.after === "cursor-1" && !expired) {
          expired = true;
          throw { status: 410 };
        }
        if (options?.after === "cursor-1") return logPage(operation.operation_id, 2, 2, null);
        return logPage(operation.operation_id, 1, 1, "cursor-1");
      }),
      cancelLifecycleOperation: vi.fn(),
    };
    const controller = new LifecycleOperationControllerV2(transport);
    controller.observe(operation);

    await controller.loadLogs(operation.operation_id);

    expect(controller.get(operation.operation_id)?.logs.map((entry) => entry.sequence)).toEqual([1, 2]);
    expect(transport.lifecycleOperationLogs).toHaveBeenCalledTimes(4);
  });
});

describe("Core operation controller v2", () => {
  it("preserves Core progress authority and fails closed when the active tunnel changes", async () => {
    let authority = { key: "project-1:head-1", resourceGeneration: 1 };
    const transport = {
      getCoreOperation: vi.fn().mockResolvedValue(coreOperation({
        progress_completed: 2,
        updated_at: LATER,
        etag: OTHER_ETAG,
      })),
      cancelCoreOperation: vi.fn(),
    };
    const controller = new CoreOperationControllerV2(transport, () => authority);
    controller.observe(coreOperation());

    await expect(controller.refresh("core-operation-1")).resolves.toMatchObject({ progress_completed: 2 });
    authority = { key: "project-2:head-1", resourceGeneration: 1 };
    await expect(controller.refresh("core-operation-1")).rejects.toThrow(/authority changed/i);
    expect(transport.getCoreOperation).toHaveBeenCalledTimes(1);
  });
});
