import { runV1Schema, type RunV1 } from "../api/v1/schemas";
import { DesktopContractError } from "../api/v1/client";
import type { ProductResourceMutationIntent, ProductRunRetryRecovery } from "./provider";

export const MAX_RUN_RETRY_RECOVERY_BYTES = 1_048_576;

export interface ProductRunRetryRecoveryStore {
  read(): string | null;
  write(value: string | null): void | Promise<void>;
}

export function createRunRetryRecovery(
  run: RunV1,
  intent: ProductResourceMutationIntent,
): ProductRunRetryRecovery {
  const originalRun = runV1Schema.parse(run);
  if (!originalRunProvesRetryAuthority(originalRun)) {
    throw new DesktopContractError("Run retry requires a terminal attempt under immutable admission");
  }
  return {
    schemaVersion: 1,
    runId: originalRun.id,
    projectId: originalRun.project_id,
    intent: { ...intent },
    originalRun,
    acceptedRun: null,
  };
}

export function withAcceptedRetryRun(
  recovery: ProductRunRetryRecovery,
  run: RunV1,
): ProductRunRetryRecovery {
  if (!retryRunProvesSingleAppend(run, recovery)
    || !retryResponsePreservesAdmission(run, recovery.originalRun)
    || (recovery.acceptedRun !== null
      && canonicalJsonSnapshot(run) !== canonicalJsonSnapshot(recovery.acceptedRun))) {
    throw new DesktopContractError("Run retry response does not prove one canonical appended attempt");
  }
  return { ...recovery, acceptedRun: runV1Schema.parse(run) };
}

export function retryRunProvesSingleAppend(
  run: RunV1,
  recovery: ProductRunRetryRecovery,
  bindAccepted = true,
): boolean {
  const original = recovery.originalRun;
  if (run.id !== recovery.runId
    || run.id !== original.id
    || run.project_id !== recovery.projectId
    || original.project_id !== recovery.projectId
    || original.attempts.length !== original.attempt_count
    || run.attempt_count !== original.attempt_count + 1
    || run.attempts.length !== run.attempt_count
    || run.current_attempt_id === null
    || run.current_attempt?.id !== run.current_attempt_id
    || run.attempts[original.attempt_count]?.id !== run.current_attempt_id
    || run.current_attempt_id === original.current_attempt_id
    || original.attempts.some((attempt) => attempt.id === run.current_attempt_id)) {
    return false;
  }
  if (canonicalJsonSnapshot(immutableRunIdentity(run))
    !== canonicalJsonSnapshot(immutableRunIdentity(original))) {
    return false;
  }
  if (!original.attempts.every((attempt, index) =>
    run.attempts[index]?.id === attempt.id
      && canonicalJsonSnapshot(run.attempts[index]) === canonicalJsonSnapshot(attempt)
  )) {
    return false;
  }
  if (!bindAccepted || recovery.acceptedRun === null) return true;
  const acceptedAttempt = recovery.acceptedRun.attempts[original.attempt_count];
  const observedAttempt = run.attempts[original.attempt_count];
  return acceptedAttempt !== undefined
    && observedAttempt !== undefined
    && acceptedAttempt.id === observedAttempt.id
    && acceptedAttempt.run_id === observedAttempt.run_id
    && acceptedAttempt.number === observedAttempt.number
    && acceptedAttempt.created_at === observedAttempt.created_at;
}

export function retryRunProvesApplied(
  run: RunV1,
  recovery: ProductRunRetryRecovery,
): boolean {
  if (!retryRunProvesSingleAppend(run, recovery)) return false;
  if (!retryRunPreservesAdmission(run, recovery.originalRun)) return false;
  if (run.status !== "queued") return true;
  return run.queued_reason?.code === "capacity"
    && run.current_attempt?.queued_reason?.code === "capacity"
    && canonicalJsonSnapshot(run.queued_reason)
      === canonicalJsonSnapshot(run.current_attempt.queued_reason);
}

export function overlayAcceptedRetryRun(
  runs: readonly RunV1[],
  recovery: ProductRunRetryRecovery,
): RunV1[] {
  const accepted = recovery.acceptedRun;
  if (!accepted) return [...runs];
  const index = runs.findIndex((run) => run.id === recovery.runId);
  if (index < 0) return [accepted, ...runs];
  if (retryRunProvesApplied(runs[index]!, recovery)) return [...runs];
  const next = [...runs];
  next[index] = accepted;
  return next;
}

export function serializeRunRetryRecovery(recovery: ProductRunRetryRecovery): string {
  const value = JSON.stringify({
    schema_version: "1",
    run_id: recovery.runId,
    project_id: recovery.projectId,
    intent: {
      action_id: recovery.intent.actionId,
      stream_epoch: recovery.intent.streamEpoch,
      etag: recovery.intent.etag,
    },
    original_run: recovery.originalRun,
    accepted_run: recovery.acceptedRun,
  });
  if (new TextEncoder().encode(value).byteLength > MAX_RUN_RETRY_RECOVERY_BYTES) {
    throw new DesktopContractError("Run retry recovery state exceeds its native journal budget");
  }
  return value;
}

export function parseRunRetryRecovery(value: string): ProductRunRetryRecovery {
  if (new TextEncoder().encode(value).byteLength > MAX_RUN_RETRY_RECOVERY_BYTES) {
    throw new DesktopContractError("Run retry recovery state exceeds its native journal budget");
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch (error) {
    throw new DesktopContractError("Run retry recovery state is not valid JSON", { cause: error });
  }
  if (!isRecord(parsed)
    || parsed.schema_version !== "1"
    || typeof parsed.run_id !== "string"
    || typeof parsed.project_id !== "string"
    || !isRecord(parsed.intent)
    || typeof parsed.intent.action_id !== "string"
    || parsed.intent.action_id.length < 16
    || parsed.intent.action_id.length > 256
    || parsed.intent.action_id.trim() !== parsed.intent.action_id
    || /[\u0000-\u001f\u007f]/.test(parsed.intent.action_id)
    || !Number.isSafeInteger(parsed.intent.stream_epoch)
    || Number(parsed.intent.stream_epoch) < 0
    || typeof parsed.intent.etag !== "string"
    || !/^"[0-9a-f]{64}"$/.test(parsed.intent.etag)) {
    throw new DesktopContractError("Run retry recovery state has an invalid identity");
  }
  const originalRun = runV1Schema.parse(parsed.original_run);
  const acceptedRun = parsed.accepted_run === null ? null : runV1Schema.parse(parsed.accepted_run);
  const recoveryWithoutAccepted: ProductRunRetryRecovery = {
    schemaVersion: 1,
    runId: parsed.run_id,
    projectId: parsed.project_id,
    intent: {
      actionId: parsed.intent.action_id,
      streamEpoch: Number(parsed.intent.stream_epoch),
      etag: parsed.intent.etag,
    },
    originalRun,
    acceptedRun: null,
  };
  if (originalRun.id !== recoveryWithoutAccepted.runId
    || originalRun.project_id !== recoveryWithoutAccepted.projectId
    || originalRun.etag !== recoveryWithoutAccepted.intent.etag
    || !originalRunProvesRetryAuthority(originalRun)
    || (acceptedRun !== null
      && (!retryRunProvesSingleAppend(acceptedRun, recoveryWithoutAccepted)
        || !retryResponsePreservesAdmission(acceptedRun, originalRun)))) {
    throw new DesktopContractError("Run retry recovery state does not match its run authority");
  }
  return { ...recoveryWithoutAccepted, acceptedRun };
}

export function sameRunRetryIntent(
  left: ProductResourceMutationIntent,
  right: ProductResourceMutationIntent,
): boolean {
  return left.actionId === right.actionId
    && left.streamEpoch === right.streamEpoch
    && left.etag === right.etag;
}

function canonicalJsonSnapshot(value: unknown): string {
  return JSON.stringify(sortCanonicalJsonValue(value));
}

function immutableRunIdentity(run: RunV1): unknown {
  const {
    status: _status,
    queued_reason: _queuedReason,
    current_attempt_id: _currentAttemptId,
    current_attempt: _currentAttempt,
    attempt_count: _attemptCount,
    current_error: _currentError,
    updated_at: _updatedAt,
    started_at: _startedAt,
    finished_at: _finishedAt,
    etag: _etag,
    attempts: _attempts,
    ...identity
  } = run;
  return identity;
}

function retryResponsePreservesAdmission(run: RunV1, original: RunV1): boolean {
  const appended = run.attempts[original.attempt_count];
  return appended !== undefined
    && retryRunPreservesAdmission(run, original)
    && run.status === "queued"
    && run.queued_reason?.code === "capacity"
    && run.current_error === null
    && run.started_at === null
    && run.finished_at === null
    && appended.run_id === run.id
    && appended.number === original.attempt_count + 1
    && appended.status === "queued"
    && appended.queued_reason?.code === "capacity"
    && appended.error === null
    && appended.started_at === null
    && appended.finished_at === null
    && canonicalJsonSnapshot(run.queued_reason) === canonicalJsonSnapshot(appended.queued_reason);
}

function retryRunPreservesAdmission(run: RunV1, original: RunV1): boolean {
  return original.pinned_revision !== null
    && original.admitted_at !== null
    && canonicalJsonSnapshot(run.pinned_revision) === canonicalJsonSnapshot(original.pinned_revision)
    && run.admitted_at === original.admitted_at;
}

function originalRunProvesRetryAuthority(run: RunV1): boolean {
  return (run.status === "failed" || run.status === "cancelled")
    && run.current_attempt_id !== null
    && run.current_attempt?.id === run.current_attempt_id
    && run.current_attempt.status === run.status
    && run.pinned_revision !== null
    && run.admitted_at !== null;
}

function sortCanonicalJsonValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortCanonicalJsonValue);
  if (value === null || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, child]) => [key, sortCanonicalJsonValue(child)]),
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
