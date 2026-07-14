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

  it("enforces the frame limit by UTF-8 bytes at a multibyte boundary", () => {
    const exactLimit = `:${"a".repeat(1_048_576 - 5)}é\n\n`;
    const overLimit = `:${"a".repeat(1_048_576 - 4)}é\n\n`;

    expect(new TextEncoder().encode(exactLimit)).toHaveLength(1_048_576);
    expect(parseSseFrame(exactLimit)).toEqual({ kind: "heartbeat" });
    expect(new TextEncoder().encode(overLimit)).toHaveLength(1_048_577);
    expect(() => parseSseFrame(overLimit)).toThrow(/exceeds the payload limit/i);
  });

  it("enforces the structured data limit by UTF-8 bytes", () => {
    expect(() =>
      parseSseFrame({
        id: EVENT_FIXTURE_V1.event_id,
        event: EVENT_FIXTURE_V1.event_name,
        data: "é".repeat(524_289),
      }),
    ).toThrow(/exceeds the payload limit/i);
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

  it("rejects event error envelopes whose status does not match the response", () => {
    expect(() => parseEventStreamFailure(410, CONTRACT_FIXTURE_V1.error)).toThrow(
      /status does not match/i,
    );
  });
});
