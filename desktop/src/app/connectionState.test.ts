import { describe, expect, it } from "vitest";
import {
  connectionActions,
  initialConnectionState,
  reconcileConnectionSnapshot,
  validateConnectionSnapshot,
  type ConnectionSnapshot,
} from "./connectionState";

function snapshot(
  overrides: Partial<ConnectionSnapshot> = {},
): ConnectionSnapshot {
  return {
    sequence: 1,
    phase: "disconnected",
    profileId: "profile-1",
    operationId: null,
    hostKeyReview: null,
    core: null,
    failure: null,
    ...overrides,
  };
}

describe("connection snapshot reconciliation", () => {
  it("ignores stale delivery and accepts identical at-least-once replay", () => {
    const online = snapshot({
      sequence: 8,
      phase: "online",
      core: {
        contractVersion: "1",
        contractDigest: "a".repeat(64),
        coreVersion: "0.1.0",
      },
    });
    const current = reconcileConnectionSnapshot(initialConnectionState, online);

    expect(reconcileConnectionSnapshot(current, snapshot({ sequence: 7 }))).toBe(
      current,
    );
    expect(reconcileConnectionSnapshot(current, { ...online })).toBe(current);
  });

  it("rejects a conflicting replay at the same sequence", () => {
    const current = snapshot({ sequence: 4 });
    expect(() =>
      reconcileConnectionSnapshot(
        current,
        snapshot({
          sequence: 4,
          phase: "connecting",
          operationId: "operation-1",
        }),
      ),
    ).toThrow("Conflicting connection snapshot");
  });

  it("freezes accepted snapshots and their nested values", () => {
    const incoming = snapshot({
      phase: "host_key_review",
      operationId: "operation-1",
      hostKeyReview: {
        algorithm: "ssh-ed25519",
        fingerprint: "SHA256:trusted",
      },
    });
    const accepted = reconcileConnectionSnapshot(initialConnectionState, incoming);

    expect(Object.isFrozen(accepted)).toBe(true);
    expect(Object.isFrozen(accepted.hostKeyReview)).toBe(true);
  });
});

describe("connection snapshot invariants", () => {
  it("requires a long-running operation reference", () => {
    expect(() =>
      validateConnectionSnapshot(snapshot({ phase: "bootstrapping" })),
    ).toThrow("bootstrapping requires an operation ID");
  });

  it("requires explicit host-key review data", () => {
    expect(() =>
      validateConnectionSnapshot(
        snapshot({
          phase: "host_key_review",
          operationId: "operation-1",
        }),
      ),
    ).toThrow("host_key_review requires a host key fingerprint");
  });

  it("requires compatible Core metadata before online", () => {
    expect(() =>
      validateConnectionSnapshot(snapshot({ phase: "online" })),
    ).toThrow("online requires compatible Core contract metadata");
  });

  it("requires typed failure state for degraded and offline snapshots", () => {
    expect(() =>
      validateConnectionSnapshot(snapshot({ phase: "offline" })),
    ).toThrow("offline requires a typed failure");
  });
});

describe("connection actions", () => {
  it("allows run only from a compatible online snapshot", () => {
    const online = snapshot({
      phase: "online",
      core: {
        contractVersion: "1",
        contractDigest: "b".repeat(64),
        coreVersion: "0.1.0",
      },
    });
    expect(connectionActions(online)).toEqual({
      canConnect: false,
      canDisconnect: true,
      canEditProfile: false,
      canRetry: false,
      canRun: true,
    });
  });

  it("offers retry only for a retryable typed failure", () => {
    const offline = snapshot({
      phase: "offline",
      failure: {
        code: "tunnel_lost",
        message: "The SSH tunnel closed.",
        retryable: true,
        nextAction: "Reconnect to the server.",
      },
    });
    expect(connectionActions(offline)).toEqual({
      canConnect: true,
      canDisconnect: false,
      canEditProfile: true,
      canRetry: true,
      canRun: false,
    });
  });
});
