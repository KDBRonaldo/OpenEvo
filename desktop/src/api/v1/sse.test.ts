import { describe, expect, it } from "vitest";
import { DesktopApiError, DesktopContractError } from "./client";
import { CONTRACT_FIXTURE_V1, EVENT_FIXTURE_V1 } from "./fixtures";
import { parseEventStreamFailure, parseSseFrame } from "./sse";

describe("Desktop Local API v1 SSE parser", () => {
  it("parses a strict event frame and its versioned payload", () => {
    const frame = [
      `id: ${EVENT_FIXTURE_V1.event_id}`,
      `event: ${EVENT_FIXTURE_V1.event_name}`,
      `data: ${JSON.stringify(EVENT_FIXTURE_V1)}`,
      "",
      "",
    ].join("\n");

    const parsed = parseSseFrame(frame);

    expect(parsed.kind).toBe("event");
    if (parsed.kind === "event") {
      expect(parsed.envelope.data.kind).toBe("run_changed");
      expect(parsed.id).toBe(EVENT_FIXTURE_V1.event_id);
    }
  });

  it("accepts comment-only heartbeats", () => {
    expect(parseSseFrame(": heartbeat\n\n")).toEqual({ kind: "heartbeat" });
  });

  it("rejects frame/envelope identity mismatches and extra payload fields", () => {
    expect(() =>
      parseSseFrame({ id: "different-event", event: EVENT_FIXTURE_V1.event_name, data: JSON.stringify(EVENT_FIXTURE_V1) }),
    ).toThrow(/id does not match/i);
    expect(() =>
      parseSseFrame({
        id: EVENT_FIXTURE_V1.event_id,
        event: EVENT_FIXTURE_V1.event_name,
        data: JSON.stringify({ ...EVENT_FIXTURE_V1, secret: "leak" }),
      }),
    ).toThrow(DesktopContractError);
  });

  it("turns an expired cursor into an explicit snapshot-reload signal", () => {
    expect(parseEventStreamFailure(410, CONTRACT_FIXTURE_V1.cursorExpiredError)).toEqual({
      kind: "cursor_expired",
      reloadSnapshots: true,
      resumeFromEventId: null,
      error: CONTRACT_FIXTURE_V1.cursorExpiredError,
    });
  });

  it("keeps non-expiry event errors typed", () => {
    expect(() => parseEventStreamFailure(409, CONTRACT_FIXTURE_V1.error)).toThrow(DesktopApiError);
  });
});
