export const connectionPhases = [
  "local_starting",
  "local_ready",
  "disconnected",
  "connecting",
  "host_key_review",
  "checking",
  "bootstrapping",
  "core_starting",
  "online",
  "degraded",
  "reconnecting",
  "offline",
] as const;

export type ConnectionPhase = (typeof connectionPhases)[number];

export interface ConnectionFailure {
  code: string;
  message: string;
  retryable: boolean;
  nextAction: string | null;
}

export interface HostKeyReview {
  algorithm: string;
  fingerprint: string;
}

export interface CoreCompatibility {
  contractVersion: "1";
  contractDigest: string;
  coreVersion: string;
}

export interface ConnectionSnapshot {
  sequence: number;
  phase: ConnectionPhase;
  profileId: string | null;
  operationId: string | null;
  hostKeyReview: HostKeyReview | null;
  core: CoreCompatibility | null;
  failure: ConnectionFailure | null;
}

export interface ConnectionActions {
  canConnect: boolean;
  canDisconnect: boolean;
  canEditProfile: boolean;
  canRetry: boolean;
  canRun: boolean;
}

const operationPhases = new Set<ConnectionPhase>([
  "connecting",
  "host_key_review",
  "checking",
  "bootstrapping",
  "core_starting",
  "reconnecting",
]);

const activePhases = new Set<ConnectionPhase>([
  "connecting",
  "host_key_review",
  "checking",
  "bootstrapping",
  "core_starting",
  "online",
  "degraded",
  "reconnecting",
]);

export const initialConnectionState: ConnectionSnapshot = Object.freeze({
  sequence: -1,
  phase: "local_starting",
  profileId: null,
  operationId: null,
  hostKeyReview: null,
  core: null,
  failure: null,
});

/**
 * Reconciles authoritative sidecar snapshots without inventing local progress.
 * Older events are ignored; a conflicting replay at the same sequence is a
 * protocol violation because event delivery is at least once.
 */
export function reconcileConnectionSnapshot(
  current: ConnectionSnapshot,
  incoming: ConnectionSnapshot,
): ConnectionSnapshot {
  validateConnectionSnapshot(incoming);
  if (incoming.sequence < current.sequence) {
    return current;
  }
  if (incoming.sequence === current.sequence) {
    if (connectionSnapshotKey(incoming) !== connectionSnapshotKey(current)) {
      throw new Error(
        `Conflicting connection snapshot at sequence ${incoming.sequence}.`,
      );
    }
    return current;
  }
  return freezeConnectionSnapshot(incoming);
}

export function connectionActions(
  snapshot: ConnectionSnapshot,
): ConnectionActions {
  validateConnectionSnapshot(snapshot);
  const hasProfile = snapshot.profileId !== null;
  return {
    canConnect:
      hasProfile &&
      (snapshot.phase === "disconnected" || snapshot.phase === "offline"),
    canDisconnect: activePhases.has(snapshot.phase),
    canEditProfile:
      snapshot.phase === "local_ready" ||
      snapshot.phase === "disconnected" ||
      snapshot.phase === "offline",
    canRetry:
      (snapshot.phase === "degraded" || snapshot.phase === "offline") &&
      snapshot.failure?.retryable === true,
    canRun: snapshot.phase === "online",
  };
}

export function validateConnectionSnapshot(
  snapshot: ConnectionSnapshot,
): void {
  if (!Number.isSafeInteger(snapshot.sequence) || snapshot.sequence < -1) {
    throw new Error(
      "Connection sequence must be a safe integer greater than or equal to -1.",
    );
  }
  if (operationPhases.has(snapshot.phase) && snapshot.operationId === null) {
    throw new Error(`${snapshot.phase} requires an operation ID.`);
  }
  if (activePhases.has(snapshot.phase) && snapshot.profileId === null) {
    throw new Error(`${snapshot.phase} requires an active profile ID.`);
  }
  if (
    snapshot.phase === "host_key_review" &&
    snapshot.hostKeyReview === null
  ) {
    throw new Error("host_key_review requires a host key fingerprint.");
  }
  if (
    snapshot.phase !== "host_key_review" &&
    snapshot.hostKeyReview !== null
  ) {
    throw new Error(
      "Host key review data is only valid during host_key_review.",
    );
  }
  if (snapshot.phase === "online" && snapshot.core === null) {
    throw new Error("online requires compatible Core contract metadata.");
  }
  if (
    snapshot.phase !== "online" &&
    snapshot.phase !== "degraded" &&
    snapshot.phase !== "reconnecting" &&
    snapshot.core !== null
  ) {
    throw new Error(
      "Core metadata is only valid after a compatible connection.",
    );
  }
  if (
    (snapshot.phase === "degraded" || snapshot.phase === "offline") &&
    snapshot.failure === null
  ) {
    throw new Error(`${snapshot.phase} requires a typed failure.`);
  }
  if (
    snapshot.phase !== "degraded" &&
    snapshot.phase !== "offline" &&
    snapshot.failure !== null
  ) {
    throw new Error("Failure data is only valid for degraded or offline state.");
  }
}

function freezeConnectionSnapshot(
  snapshot: ConnectionSnapshot,
): ConnectionSnapshot {
  return Object.freeze({
    ...snapshot,
    hostKeyReview:
      snapshot.hostKeyReview === null
        ? null
        : Object.freeze({ ...snapshot.hostKeyReview }),
    core: snapshot.core === null ? null : Object.freeze({ ...snapshot.core }),
    failure:
      snapshot.failure === null
        ? null
        : Object.freeze({ ...snapshot.failure }),
  });
}

function connectionSnapshotKey(snapshot: ConnectionSnapshot): string {
  return JSON.stringify([
    snapshot.sequence,
    snapshot.phase,
    snapshot.profileId,
    snapshot.operationId,
    snapshot.hostKeyReview?.algorithm ?? null,
    snapshot.hostKeyReview?.fingerprint ?? null,
    snapshot.core?.contractVersion ?? null,
    snapshot.core?.contractDigest ?? null,
    snapshot.core?.coreVersion ?? null,
    snapshot.failure?.code ?? null,
    snapshot.failure?.message ?? null,
    snapshot.failure?.retryable ?? null,
    snapshot.failure?.nextAction ?? null,
  ]);
}
