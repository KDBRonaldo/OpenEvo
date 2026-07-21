import { describe, expect, it } from "vitest";
import {
  drainPendingSnapshot,
  InFlightCaptureCutoff,
  selectLatestArtifactPredecessor,
} from "./release-live-capture";

function deferred(): { promise: Promise<void>; resolve: () => void } {
  let resolve!: () => void;
  const promise = new Promise<void>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

describe("release live response capture", () => {
  it("waits for requests begun before cutoff and rejects later polling", async () => {
    const cutoff = new InFlightCaptureCutoff<string>();
    expect(cutoff.begin("first")).toBe(true);
    expect(cutoff.begin("second")).toBe(true);

    let closed = false;
    const closing = cutoff.close(1_000).then((unresolved) => {
      expect(unresolved).toEqual([]);
      closed = true;
    });
    expect(cutoff.begin("later")).toBe(false);
    expect(cutoff.accepts("first")).toBe(true);
    expect(cutoff.accepts("second")).toBe(true);

    cutoff.finish("first");
    await Promise.resolve();
    expect(closed).toBe(false);
    cutoff.finish("second");
    await closing;

    expect(closed).toBe(true);
    expect(cutoff.accepts("first")).toBe(false);
    expect(cutoff.accepts("second")).toBe(false);
  });

  it("returns and releases requests that exceed the bounded cutoff", async () => {
    const cutoff = new InFlightCaptureCutoff<string>();
    expect(cutoff.begin("stalled")).toBe(true);

    await expect(cutoff.close(10)).resolves.toEqual(["stalled"]);
    expect(cutoff.accepts("stalled")).toBe(false);
  });

  it("drains only the pending generation present at invocation", async () => {
    const first = deferred();
    const second = deferred();
    const later = deferred();
    const pending = new Set([first.promise, second.promise]);

    let drained = false;
    const draining = drainPendingSnapshot(pending).then(() => {
      drained = true;
    });
    pending.add(later.promise);
    first.resolve();
    await Promise.resolve();
    expect(drained).toBe(false);
    second.resolve();
    await draining;

    expect(drained).toBe(true);
    later.resolve();
  });

  it("propagates failure from the pending generation", async () => {
    const failure = Promise.reject(new Error("capture failed"));
    await expect(drainPendingSnapshot(new Set([failure]))).rejects.toThrow(
      "capture failed",
    );
  });

  it("selects the same latest compatible predecessor ordering as Core", () => {
    const artifact = (
      id: string,
      generation: number,
      createdAt: string,
      overrides: Partial<{ project_id: string; target_id: string; artifact_type: string }> = {},
    ) => ({
      id,
      project_id: "project-1",
      target_id: "skill_bundle",
      artifact_type: "skill_bundle",
      created_at: createdAt,
      produced_revision: { generation },
      ...overrides,
    });
    const current = artifact("current", 4, "2026-07-21T03:00:00Z");
    const exactSecond = artifact("exact", 3, "2026-07-21T02:00:00Z");
    const fractionalSecond = artifact("fractional", 3, "2026-07-21T02:00:00.1Z");
    const olderGeneration = artifact("older", 2, "2026-07-21T02:59:59.999999999Z");
    const wrongTarget = artifact("wrong-target", 3, "2026-07-21T02:59:59Z", {
      target_id: "text_memory",
    });

    expect(selectLatestArtifactPredecessor(current, [
      fractionalSecond,
      wrongTarget,
      olderGeneration,
      exactSecond,
    ])).toEqual(exactSecond);

    const upper = artifact("B", 3, "2026-07-21T02:00:00Z");
    const lower = artifact("a", 3, "2026-07-21T02:00:00Z");
    expect(selectLatestArtifactPredecessor(current, [lower, upper])).toEqual(lower);
  });
});
