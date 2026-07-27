import { describe, expect, it, vi } from "vitest";
import { canonicalJsonV2, sha256Utf8V2 } from "../api/v2/schemas";
import {
  MutationIntentConflictV2,
  MutationIntentCoordinatorV2,
  type MutationIntentNativeBridgeV2,
  type MutationReservationV2,
} from "./mutationIntentJournalV2";

const NOW = "2026-07-27T08:00:00.000Z";

function reservation(overrides: Partial<MutationReservationV2> = {}): MutationReservationV2 {
  return {
    proposedActionId: "mutation-action-proposed-0001",
    mutationKind: "profile_connect",
    resourceScope: "profile:profile-lab",
    request: { schema_version: "2", expected_connection_generation: 4 },
    authority: { resource_generation: 4, etag: `"${"a".repeat(64)}"`, last_event_id: "event-4" },
    providerStreamInstance: "provider-instance-1",
    providerStreamEpoch: 4,
    chainStep: "single",
    ...overrides,
  };
}

class MemoryBridge implements MutationIntentNativeBridgeV2 {
  value: string | null;
  readonly calls: string[] = [];
  conflictOnce = false;
  conflictReplacement: string | null = null;

  constructor(value: string | null = null) {
    this.value = value;
  }

  async readMutationIntentJournalV2(): Promise<string | null> {
    this.calls.push("read");
    return this.value;
  }

  async compareAndSwapMutationIntentJournalV2(
    expectedValue: string | null,
    newValue: string | null,
  ): Promise<void> {
    this.calls.push("cas");
    if (this.conflictOnce) {
      this.conflictOnce = false;
      this.value = this.conflictReplacement;
      throw { code: "mutation_intent_journal_conflict" };
    }
    if (this.value !== expectedValue) throw { code: "mutation_intent_journal_conflict" };
    this.value = newValue;
  }
}

function coordinator(bridge: MutationIntentNativeBridgeV2) {
  return new MutationIntentCoordinatorV2(bridge, { now: () => NOW });
}

describe("mutation intent coordinator v2", () => {
  it("reserves canonical request and authority digests before transport", async () => {
    const bridge = new MemoryBridge();
    const journal = coordinator(bridge);

    const entry = await journal.reserve(reservation());

    expect(bridge.calls).toEqual(["read", "cas"]);
    expect(entry.action_id).toBe("mutation-action-proposed-0001");
    expect(entry.request_sha256).toBe(sha256Utf8V2(canonicalJsonV2(reservation().request)));
    expect(entry.authority_sha256).toBe(sha256Utf8V2(canonicalJsonV2(reservation().authority)));
    expect(JSON.parse(bridge.value!)).toMatchObject({ schema_version: "2", revision: 1 });
  });

  it("reuses exact unresolved identity across clicks and relaunch", async () => {
    const bridge = new MemoryBridge();
    const first = coordinator(bridge);
    const reserved = await first.reserve(reservation());
    const casCount = bridge.calls.filter((call) => call === "cas").length;

    const exactRetry = await first.reserve(reservation({
      proposedActionId: "mutation-action-proposed-0002",
    }));
    expect(exactRetry.action_id).toBe(reserved.action_id);
    expect(bridge.calls.filter((call) => call === "cas")).toHaveLength(casCount);

    const relaunched = coordinator(bridge);
    const restored = await relaunched.reserve(reservation({
      proposedActionId: "mutation-action-proposed-0003",
    }));
    expect(restored.action_id).toBe(reserved.action_id);
  });

  it("rejects changed request or authority while the logical scope is unresolved", async () => {
    const bridge = new MemoryBridge();
    const journal = coordinator(bridge);
    await journal.reserve(reservation());

    await expect(journal.reserve(reservation({
      proposedActionId: "mutation-action-proposed-0002",
      request: { schema_version: "2", expected_connection_generation: 5 },
    }))).rejects.toBeInstanceOf(MutationIntentConflictV2);
    await expect(journal.reserve(reservation({
      proposedActionId: "mutation-action-proposed-0003",
      authority: { resource_generation: 5, etag: `"${"b".repeat(64)}"` },
    }))).rejects.toBeInstanceOf(MutationIntentConflictV2);
    expect(journal.list()).toHaveLength(1);
  });

  it("retries native CAS conflicts and adopts an exact concurrently reserved row", async () => {
    const bridge = new MemoryBridge();
    const concurrent = coordinator(new MemoryBridge());
    const concurrentEntry = await concurrent.reserve(reservation({
      proposedActionId: "mutation-action-concurrent-0001",
    }));
    const concurrentRaw = (concurrent as unknown as { rawValue: string | null }).rawValue;
    bridge.conflictOnce = true;
    bridge.conflictReplacement = concurrentRaw;
    const journal = coordinator(bridge);

    const entry = await journal.reserve(reservation());

    expect(entry.action_id).toBe(concurrentEntry.action_id);
    expect(bridge.calls).toEqual(["read", "cas", "read"]);
  });

  it("binds accepted operations, terminal observation, and direct-response clearing", async () => {
    const bridge = new MemoryBridge();
    const journal = coordinator(bridge);
    const reserved = await journal.reserve(reservation());
    const accepted = await journal.bindAcceptedOperation(reserved.action_id, "operation-profile-connect-1");
    expect(accepted).toMatchObject({ state: "accepted", accepted_operation_id: "operation-profile-connect-1" });

    await journal.markTerminalObserved(reserved.action_id, "operation-profile-connect-1");
    expect(journal.list()[0]?.state).toBe("terminal_observed");
    await journal.clearTerminalObserved(reserved.action_id, "operation-profile-connect-1");
    expect(journal.list()).toEqual([]);

    const direct = await journal.reserve(reservation({
      proposedActionId: "mutation-action-direct-0001",
      mutationKind: "profile_update",
    }));
    await journal.markDirectResponseObserved(direct.action_id, "c".repeat(64));
    expect(journal.list()).toEqual([]);
  });

  it("retains deterministic rejection authority and blocks an exact transport replay", async () => {
    const bridge = new MemoryBridge();
    const journal = coordinator(bridge);
    const reserved = await journal.reserve(reservation());

    await journal.markDeterministicRejection(reserved.action_id);
    const rejected = await journal.reserve(reservation({
      proposedActionId: "mutation-action-proposed-0002",
    }));

    expect(rejected).toMatchObject({
      action_id: reserved.action_id,
      state: "deterministic_rejection",
    });
  });

  it("advances only the exact native-workspace project chain", async () => {
    const bridge = new MemoryBridge();
    const journal = coordinator(bridge);
    const reserved = await journal.reserve(reservation({
      mutationKind: "project_create",
      resourceScope: "project:new",
      chainStep: "native_workspace_prepare",
    }));
    await journal.bindAcceptedOperation(reserved.action_id, "native-operation-1");
    await journal.markTerminalObserved(reserved.action_id, "native-operation-1");

    const advanced = await journal.advanceNativeProjectChain(reserved.action_id, "native-operation-1");

    expect(advanced).toMatchObject({
      chain_step: "project_create",
      state: "reserved",
      accepted_operation_id: null,
      completed_operation_ids: ["native-operation-1"],
    });
    await journal.discardNativeProjectChain(reserved.action_id);
    expect(journal.list()).toEqual([]);
  });

  it("fails closed on corrupt restored state and the 16-row capacity", async () => {
    const corrupt = new MemoryBridge(JSON.stringify({
      schema_version: "2",
      revision: 1,
      entries: [],
      password: "canary",
    }));
    await expect(coordinator(corrupt).initialize()).rejects.toThrow();

    const bridge = new MemoryBridge();
    const journal = coordinator(bridge);
    for (let index = 0; index < 16; index += 1) {
      await journal.reserve(reservation({
        proposedActionId: `mutation-action-capacity-${String(index).padStart(4, "0")}`,
        resourceScope: `profile:profile-${String(index).padStart(4, "0")}`,
      }));
    }
    await expect(journal.reserve(reservation({
      proposedActionId: "mutation-action-capacity-overflow",
      resourceScope: "profile:profile-overflow",
    }))).rejects.toThrow(/capacity/i);
  });

  it("serializes concurrent reservations through one native CAS queue", async () => {
    const bridge = new MemoryBridge();
    const cas = vi.spyOn(bridge, "compareAndSwapMutationIntentJournalV2");
    const journal = coordinator(bridge);

    await Promise.all([
      journal.reserve(reservation({ resourceScope: "profile:first" })),
      journal.reserve(reservation({
        proposedActionId: "mutation-action-proposed-0002",
        resourceScope: "profile:second",
      })),
    ]);

    expect(journal.list()).toHaveLength(2);
    expect(cas).toHaveBeenCalledTimes(2);
  });
});
