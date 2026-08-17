import {
  canonicalJsonV2,
  compareUtcTimestampsV2,
  type LifecycleLogEntryV2,
  type LifecycleOperationRefV2,
  type LifecycleOperationV2,
  type OperationV2,
} from "../api/v2/schemas";
import type {
  DesktopApiClientV2,
  LifecycleLogRequestOptionsV2,
} from "../api/v2/client";

const MAX_LOG_TAIL = 200;
const MAX_LOG_PAGES = 100;
const POLL_DELAYS_MS = [500, 1_000, 2_000, 4_000] as const;

type LifecycleTransportV2 = Pick<
  DesktopApiClientV2,
  "getLifecycleOperation" | "lifecycleOperationLogs" | "cancelLifecycleOperation"
>;

type CoreOperationTransportV2 = Pick<
  DesktopApiClientV2,
  "getCoreOperation" | "cancelCoreOperation"
>;

export interface LifecycleOperationStateV2 {
  readonly operation: LifecycleOperationV2;
  readonly logs: readonly LifecycleLogEntryV2[];
  readonly droppedBeforeSequence: number;
  readonly hasOlderLogs: boolean;
  readonly hasNewerLogs: boolean;
}

export interface LifecycleOperationControllerOptionsV2 {
  readonly wait?: (milliseconds: number) => Promise<void>;
}

export class LifecycleOperationContractErrorV2 extends Error {
  constructor(message: string) {
    super(message);
    this.name = "LifecycleOperationContractErrorV2";
  }
}

export class LifecycleOperationControllerV2 {
  private readonly transport: LifecycleTransportV2;
  private readonly wait: (milliseconds: number) => Promise<void>;
  private readonly states = new Map<string, LifecycleOperationStateV2>();
  private readonly listeners = new Set<() => void>();

  constructor(
    transport: LifecycleTransportV2,
    options: LifecycleOperationControllerOptionsV2 = {},
  ) {
    this.transport = transport;
    this.wait = options.wait ?? delayV2;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  list(): readonly LifecycleOperationStateV2[] {
    return Object.freeze([...this.states.values()]
      .sort((left, right) => compareUtcTimestampsV2(left.operation.created_at, right.operation.created_at)));
  }

  get(operationId: string): LifecycleOperationStateV2 | null {
    return this.states.get(operationId) ?? null;
  }

  async synchronize(references: readonly LifecycleOperationRefV2[]): Promise<readonly LifecycleOperationStateV2[]> {
    const identities = new Set<string>();
    for (const reference of references) {
      if (identities.has(reference.operation_id)) {
        throw new LifecycleOperationContractErrorV2("Lifecycle pending-operation identity is duplicated");
      }
      identities.add(reference.operation_id);
      const operation = await this.transport.getLifecycleOperation(reference.operation_id);
      assertReferenceCanAdvanceToOperationV2(reference, operation);
      this.observe(operation);
      await this.loadLogs(operation.operation_id);
    }
    return this.list();
  }

  observe(operation: LifecycleOperationV2): LifecycleOperationV2 {
    const previous = this.states.get(operation.operation_id);
    if (previous !== undefined) assertLifecycleOperationDoesNotRegressV2(previous.operation, operation);
    if (previous !== undefined && canonicalJsonV2(previous.operation) === canonicalJsonV2(operation)) {
      return previous.operation;
    }
    this.states.set(operation.operation_id, Object.freeze({
      operation,
      logs: previous?.logs ?? Object.freeze([]),
      droppedBeforeSequence: previous?.droppedBeforeSequence ?? 0,
      hasOlderLogs: previous?.hasOlderLogs ?? false,
      hasNewerLogs: previous?.hasNewerLogs ?? false,
    }));
    this.emit();
    return operation;
  }

  async refresh(operationId: string): Promise<LifecycleOperationV2> {
    const observedAtDispatch = this.states.get(operationId)?.operation;
    const operation = await this.transport.getLifecycleOperation(operationId);
    if (operation.operation_id !== operationId) {
      throw new LifecycleOperationContractErrorV2("Lifecycle lookup returned another operation");
    }
    if (observedAtDispatch !== undefined) {
      assertLifecycleOperationDoesNotRegressV2(observedAtDispatch, operation);
    }
    const current = this.states.get(operationId)?.operation;
    if (current !== undefined
      && observedAtDispatch !== undefined
      && canonicalJsonV2(current) !== canonicalJsonV2(observedAtDispatch)
      && canonicalJsonV2(current) !== canonicalJsonV2(operation)) {
      try {
        assertLifecycleOperationDoesNotRegressV2(operation, current);
        return current;
      } catch {
        // The response may be newer than the concurrently observed state. In
        // that case observe() performs the authoritative forward-only check.
      }
    }
    return this.observe(operation);
  }

  async loadLogs(operationId: string): Promise<LifecycleOperationStateV2> {
    return this.loadLogWindow(operationId, "preserve");
  }

  async loadOlderLogs(operationId: string): Promise<LifecycleOperationStateV2> {
    return this.loadLogWindow(operationId, "older");
  }

  async loadLatestLogs(operationId: string): Promise<LifecycleOperationStateV2> {
    return this.loadLogWindow(operationId, "latest");
  }

  private async loadLogWindow(
    operationId: string,
    mode: "preserve" | "older" | "latest",
  ): Promise<LifecycleOperationStateV2> {
    const current = this.states.get(operationId);
    if (current === undefined) {
      throw new LifecycleOperationContractErrorV2("Lifecycle logs reference an unobserved operation");
    }
    if (mode === "preserve" && current.hasNewerLogs && current.logs.length > 0) {
      return current;
    }
    let pages: Awaited<ReturnType<LifecycleTransportV2["lifecycleOperationLogs"]>>[];
    try {
      pages = mode === "older"
        ? await this.fetchAllLogPages(operationId)
        : await this.fetchRecentLogPages(current, mode);
    } catch (error) {
      if (!isCursorExpiredV2(error)) throw error;
      pages = mode === "older"
        ? await this.fetchAllLogPages(operationId)
        : await this.fetchRecentLogPages(current, mode);
    }
    const latest = this.states.get(operationId);
    if (latest === undefined) {
      throw new LifecycleOperationContractErrorV2("Lifecycle logs reference an unobserved operation");
    }
    assertLifecycleOperationDoesNotRegressV2(current.operation, latest.operation);
    const bySequence = new Map<number, LifecycleLogEntryV2>();
    if (mode !== "older") {
      for (const entry of latest.logs) bySequence.set(entry.sequence, entry);
    }
    let droppedBeforeSequence = latest.droppedBeforeSequence;
    for (const page of pages) {
      if (page.operation_id !== operationId) {
        throw new LifecycleOperationContractErrorV2("Lifecycle log page belongs to another operation");
      }
      droppedBeforeSequence = Math.max(droppedBeforeSequence, page.dropped_before_sequence);
      for (const entry of page.items) {
        const previous = bySequence.get(entry.sequence);
        if (previous !== undefined && canonicalJsonV2(previous) !== canonicalJsonV2(entry)) {
          throw new LifecycleOperationContractErrorV2("Lifecycle log sequence changed across pages");
        }
        bySequence.set(entry.sequence, entry);
      }
    }
    const retainedLogs = [...bySequence.values()]
      .sort((left, right) => left.sequence - right.sequence)
      .filter((entry) => entry.sequence > droppedBeforeSequence);
    if (retainedLogs.some((entry) => entry.sequence > latest.operation.log_sequence_high_watermark)) {
      throw new LifecycleOperationContractErrorV2("Lifecycle logs exceed the observed operation watermark");
    }
    let logs: readonly LifecycleLogEntryV2[];
    if (mode === "older") {
      const firstVisibleSequence = latest.logs[0]?.sequence
        ?? latest.operation.log_sequence_high_watermark + 1;
      const older = retainedLogs.filter((entry) => entry.sequence < firstVisibleSequence).slice(-MAX_LOG_TAIL);
      logs = older.length === 0 ? latest.logs : older;
    } else {
      logs = retainedLogs.slice(-MAX_LOG_TAIL);
    }
    const firstSequence = logs[0]?.sequence ?? null;
    const lastSequence = logs.at(-1)?.sequence ?? null;
    const next = Object.freeze({
      operation: latest.operation,
      logs: Object.freeze([...logs]),
      droppedBeforeSequence,
      hasOlderLogs: firstSequence !== null && firstSequence > droppedBeforeSequence + 1,
      hasNewerLogs: lastSequence === null
        ? latest.operation.log_sequence_high_watermark > droppedBeforeSequence
        : lastSequence < latest.operation.log_sequence_high_watermark,
    });
    if (canonicalJsonV2(latest.logs) === canonicalJsonV2(next.logs)
      && latest.droppedBeforeSequence === next.droppedBeforeSequence
      && latest.hasOlderLogs === next.hasOlderLogs
      && latest.hasNewerLogs === next.hasNewerLogs) {
      return latest;
    }
    this.states.set(operationId, next);
    this.emit();
    return next;
  }

  async cancel(operationId: string, actionId: string): Promise<LifecycleOperationV2> {
    const current = this.states.get(operationId)?.operation;
    if (current === undefined) {
      throw new LifecycleOperationContractErrorV2("Lifecycle cancellation references an unobserved operation");
    }
    const operation = await this.transport.cancelLifecycleOperation(
      operationId,
      { schema_version: "2", expected_operation_id: operationId },
      { resourceGeneration: 0, ifMatch: current.etag, idempotencyKey: actionId },
    );
    return this.observe(operation);
  }

  async pollUntilTerminal(
    operationId: string,
    signal?: AbortSignal,
    onObservation?: (operation: LifecycleOperationV2) => void | Promise<void>,
  ): Promise<LifecycleOperationV2> {
    let operation = this.states.get(operationId)?.operation;
    if (operation === undefined) operation = await this.refresh(operationId);
    let delayIndex = 0;
    while (!isLifecycleTerminalV2(operation)) {
      if (signal?.aborted) throw abortErrorV2();
      await this.wait(POLL_DELAYS_MS[delayIndex]!);
      if (signal?.aborted) throw abortErrorV2();
      const before = progressFingerprintV2(operation);
      operation = await this.refresh(operationId);
      await this.loadLogs(operationId);
      await onObservation?.(operation);
      delayIndex = progressFingerprintV2(operation) === before
        ? Math.min(delayIndex + 1, POLL_DELAYS_MS.length - 1)
        : 0;
    }
    return operation;
  }

  private async fetchRecentLogPages(
    current: LifecycleOperationStateV2,
    mode: "preserve" | "latest",
  ) {
    const operationId = current.operation.operation_id;
    const floor = Math.max(
      current.droppedBeforeSequence,
      current.operation.log_sequence_high_watermark - MAX_LOG_TAIL,
    );
    const lastVisible = current.logs.at(-1)?.sequence ?? floor;
    const afterSequence = mode === "preserve" ? Math.max(floor, lastVisible) : floor;
    const pages: Awaited<ReturnType<LifecycleTransportV2["lifecycleOperationLogs"]>>[] = [];
    const cursors = new Set<string>();
    let options: LifecycleLogRequestOptionsV2 = { limit: 100, afterSequence };
    for (let pageIndex = 0; pageIndex < 2; pageIndex += 1) {
      const page = await this.transport.lifecycleOperationLogs(operationId, options);
      pages.push(page);
      if (!page.has_more) return pages;
      if (page.next_cursor === null || cursors.has(page.next_cursor)) {
        throw new LifecycleOperationContractErrorV2("Lifecycle log pagination cursor cycled");
      }
      cursors.add(page.next_cursor);
      options = { limit: 100, after: page.next_cursor };
    }
    return pages;
  }

  private async fetchAllLogPages(operationId: string) {
    const pages: Awaited<ReturnType<LifecycleTransportV2["lifecycleOperationLogs"]>>[] = [];
    const cursors = new Set<string>();
    let options: LifecycleLogRequestOptionsV2 = { limit: 100 };
    for (let pageIndex = 0; pageIndex < MAX_LOG_PAGES; pageIndex += 1) {
      const page = await this.transport.lifecycleOperationLogs(operationId, options);
      pages.push(page);
      if (!page.has_more) return pages;
      if (page.next_cursor === null || cursors.has(page.next_cursor)) {
        throw new LifecycleOperationContractErrorV2("Lifecycle log pagination cursor cycled");
      }
      cursors.add(page.next_cursor);
      options = { limit: 100, after: page.next_cursor };
    }
    throw new LifecycleOperationContractErrorV2("Lifecycle logs exceeded the pagination budget");
  }

  private emit(): void {
    for (const listener of this.listeners) listener();
  }
}

export interface CoreOperationControllerOptionsV2 {
  readonly wait?: (milliseconds: number) => Promise<void>;
}

export interface CoreOperationAuthorityV2 {
  readonly key: string;
  readonly resourceGeneration: number;
}

export class CoreOperationControllerV2 {
  private readonly transport: CoreOperationTransportV2;
  private readonly activeAuthority: () => CoreOperationAuthorityV2 | null;
  private readonly wait: (milliseconds: number) => Promise<void>;
  private readonly operations = new Map<string, { readonly authority: string; readonly operation: OperationV2 }>();

  constructor(
    transport: CoreOperationTransportV2,
    activeAuthority: () => CoreOperationAuthorityV2 | null,
    options: CoreOperationControllerOptionsV2 = {},
  ) {
    this.transport = transport;
    this.activeAuthority = activeAuthority;
    this.wait = options.wait ?? delayV2;
  }

  list(): readonly OperationV2[] {
    return Object.freeze([...this.operations.values()].map((entry) => entry.operation));
  }

  get(operationId: string): OperationV2 | null {
    return this.operations.get(operationId)?.operation ?? null;
  }

  observe(operation: OperationV2): OperationV2 {
    const authority = this.requireAuthority();
    const previous = this.operations.get(operation.operation_id);
    if (previous !== undefined) {
      if (previous.authority !== authority.key) throw authorityChangedErrorV2();
      assertCoreOperationDoesNotRegressV2(previous.operation, operation);
    }
    this.operations.set(operation.operation_id, Object.freeze({ authority: authority.key, operation }));
    return operation;
  }

  async refresh(operationId: string): Promise<OperationV2> {
    const authority = this.requireAuthority();
    const previous = this.operations.get(operationId);
    if (previous !== undefined && previous.authority !== authority.key) throw authorityChangedErrorV2();
    const operation = await this.transport.getCoreOperation(operationId);
    if (this.requireAuthority().key !== authority.key) throw authorityChangedErrorV2();
    if (operation.operation_id !== operationId) {
      throw new LifecycleOperationContractErrorV2("Core lookup returned another operation");
    }
    return this.observe(operation);
  }

  async cancel(operationId: string, actionId: string): Promise<OperationV2> {
    const authority = this.requireAuthority();
    const current = this.operations.get(operationId);
    if (current === undefined || current.authority !== authority.key) throw authorityChangedErrorV2();
    const operation = await this.transport.cancelCoreOperation(operationId, {
      resourceGeneration: authority.resourceGeneration,
      ifMatch: current.operation.etag,
      idempotencyKey: actionId,
    });
    if (this.requireAuthority().key !== authority.key) throw authorityChangedErrorV2();
    return this.observe(operation);
  }

  async pollUntilTerminal(
    operationId: string,
    signal?: AbortSignal,
    onObservation?: (operation: OperationV2) => void | Promise<void>,
  ): Promise<OperationV2> {
    let operation = this.operations.get(operationId)?.operation;
    if (operation === undefined) operation = await this.refresh(operationId);
    let delayIndex = 0;
    while (!isCoreTerminalV2(operation)) {
      if (signal?.aborted) throw abortErrorV2();
      await this.wait(POLL_DELAYS_MS[delayIndex]!);
      const before = coreProgressFingerprintV2(operation);
      operation = await this.refresh(operationId);
      await onObservation?.(operation);
      delayIndex = coreProgressFingerprintV2(operation) === before
        ? Math.min(delayIndex + 1, POLL_DELAYS_MS.length - 1)
        : 0;
    }
    return operation;
  }

  private requireAuthority(): CoreOperationAuthorityV2 {
    const authority = this.activeAuthority();
    if (authority === null) throw authorityChangedErrorV2();
    return authority;
  }
}

export function isLifecycleTerminalV2(
  operation: LifecycleOperationV2,
): operation is LifecycleOperationV2 & { readonly status: "succeeded" | "failed" | "cancelled" } {
  return operation.status === "succeeded"
    || operation.status === "failed"
    || operation.status === "cancelled";
}

function assertReferenceCanAdvanceToOperationV2(
  reference: LifecycleOperationRefV2,
  operation: LifecycleOperationV2,
): void {
  if (reference.operation_id !== operation.operation_id
    || reference.kind !== operation.kind
    || reference.resource.resource_kind !== operation.resource.resource_kind
    || reference.resource.resource_id !== operation.resource.resource_id
    || reference.request_sha256 !== operation.request_sha256
    || operation.phase_total !== reference.phase_total
    || operation.phase_index < reference.phase_index
    || operation.log_sequence_high_watermark < reference.log_sequence_high_watermark
    || compareUtcTimestampsV2(operation.updated_at, reference.updated_at) < 0) {
    throw new LifecycleOperationContractErrorV2("Lifecycle operation regressed from its pending reference");
  }
}

function assertLifecycleOperationDoesNotRegressV2(
  previous: LifecycleOperationV2,
  next: LifecycleOperationV2,
): void {
  if (previous.operation_id !== next.operation_id
    || previous.kind !== next.kind
    || canonicalJsonV2(previous.resource) !== canonicalJsonV2(next.resource)
    || previous.request_sha256 !== next.request_sha256
    || previous.phase_total !== next.phase_total
    || next.phase_index < previous.phase_index
    || next.log_sequence_high_watermark < previous.log_sequence_high_watermark
    || compareUtcTimestampsV2(next.updated_at, previous.updated_at) < 0
    || statusRankV2(next.status) < statusRankV2(previous.status)
    || progressRegressedV2(previous, next)) {
    throw new LifecycleOperationContractErrorV2("Lifecycle operation authority regressed");
  }
  if (isLifecycleTerminalV2(previous) && canonicalJsonV2(previous) !== canonicalJsonV2(next)) {
    throw new LifecycleOperationContractErrorV2("Terminal lifecycle operation changed");
  }
  const sameDocument = canonicalJsonV2({ ...previous, etag: null }) === canonicalJsonV2({ ...next, etag: null });
  if (sameDocument !== (previous.etag === next.etag)) {
    throw new LifecycleOperationContractErrorV2("Lifecycle operation ETag authority drifted");
  }
}

function progressRegressedV2(previous: LifecycleOperationV2, next: LifecycleOperationV2): boolean {
  if (previous.phase_index !== next.phase_index || previous.progress === null) return false;
  if (next.progress === null) return true;
  if (previous.progress.kind === "indeterminate") return false;
  return next.progress.kind !== previous.progress.kind
    || next.progress.total !== previous.progress.total
    || next.progress.completed < previous.progress.completed;
}

function assertCoreOperationDoesNotRegressV2(previous: OperationV2, next: OperationV2): void {
  if (previous.operation_id !== next.operation_id
    || previous.kind !== next.kind
    || previous.created_at !== next.created_at
    || previous.progress_total !== next.progress_total
    || next.progress_completed < previous.progress_completed
    || compareUtcTimestampsV2(next.updated_at, previous.updated_at) < 0
    || statusRankV2(next.status) < statusRankV2(previous.status)) {
    throw new LifecycleOperationContractErrorV2("Core operation authority regressed");
  }
  if (isCoreTerminalV2(previous) && canonicalJsonV2(previous) !== canonicalJsonV2(next)) {
    throw new LifecycleOperationContractErrorV2("Terminal Core operation changed");
  }
}

function statusRankV2(status: LifecycleOperationV2["status"] | OperationV2["status"]): number {
  if (status === "queued") return 0;
  if (status === "running") return 1;
  return 2;
}

function isCoreTerminalV2(operation: OperationV2): boolean {
  return !["queued", "running"].includes(operation.status);
}

function progressFingerprintV2(operation: LifecycleOperationV2): string {
  return canonicalJsonV2({
    status: operation.status,
    phase_index: operation.phase_index,
    progress: operation.progress,
    log_sequence_high_watermark: operation.log_sequence_high_watermark,
  });
}

function coreProgressFingerprintV2(operation: OperationV2): string {
  return canonicalJsonV2({
    status: operation.status,
    progress_completed: operation.progress_completed,
    progress_total: operation.progress_total,
  });
}

function isCursorExpiredV2(error: unknown): boolean {
  return typeof error === "object"
    && error !== null
    && "status" in error
    && (error as { readonly status?: unknown }).status === 410;
}

function authorityChangedErrorV2(): LifecycleOperationContractErrorV2 {
  return new LifecycleOperationContractErrorV2("Active Core tunnel authority changed");
}

function abortErrorV2(): Error {
  return new DOMException("Operation polling was cancelled", "AbortError");
}

function delayV2(milliseconds: number): Promise<void> {
  return new Promise((resolve) => globalThis.setTimeout(resolve, milliseconds));
}
