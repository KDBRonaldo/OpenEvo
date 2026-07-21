import { describe, expect, it } from "vitest";
import {
  drainPendingSnapshot,
  InFlightCaptureWindow,
} from "./release-live-capture";

function deferred(): { promise: Promise<void>; resolve: () => void } {
  let resolve!: () => void;
  const promise = new Promise<void>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

describe("release live response capture", () => {
  it("waits for cutoff requests without accepting later polling", async () => {
    const window = new InFlightCaptureWindow<string>();
    expect(window.begin("cutoff-request-1")).toBe(true);
    expect(window.begin("cutoff-request-2")).toBe(true);

    let closed = false;
    const closing = window.close().then(() => {
      closed = true;
    });
    expect(window.begin("later-poll")).toBe(false);
    expect(window.accepts("cutoff-request-1")).toBe(true);
    expect(window.accepts("cutoff-request-2")).toBe(true);
    await Promise.resolve();
    expect(closed).toBe(false);

    window.finish("cutoff-request-1");
    await Promise.resolve();
    expect(closed).toBe(false);

    window.finish("cutoff-request-2");
    await closing;
    expect(closed).toBe(true);
    expect(window.accepts("cutoff-request-1")).toBe(false);
    expect(window.accepts("cutoff-request-2")).toBe(false);
    window.finish("later-poll");
  });

  it("drains only the pending generation present at invocation", async () => {
    const initial = deferred();
    const later = deferred();
    const pending = new Set([initial.promise]);

    let drained = false;
    const draining = drainPendingSnapshot(pending).then(() => {
      drained = true;
    });
    pending.add(later.promise);
    initial.resolve();
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
});
