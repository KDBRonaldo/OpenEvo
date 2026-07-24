import { describe, expect, it } from "vitest";
import { DesktopApiErrorV2, DesktopContractErrorV2 } from "./client";
import { DesktopEventReplayAuthorityV2, parseEventStreamFailureV2, parseSseFrameV2 } from "./sse";

const EVENT = {
  schema_version: "2",
  event_id: "event-3",
  sequence: 3,
  occurred_at: "2026-07-23T06:00:00Z",
  event_type: "ssh_host_catalog_changed",
  payload_sha256: "a0e03db5caadb43ec812f99759f0ba45ef2e7f981508b4d5ed0a0870be25e63e",
  payload: {
    payload_kind: "ssh_host_catalog_changed",
    catalog_generation: 3,
    host_count: 2,
    warning_count: 1,
  },
} as const;

describe("Desktop Local API v2 SSE", () => {
  it("validates frame/envelope identity and canonical payload digest", () => {
    const parsed = parseSseFrameV2([
      `id: ${EVENT.event_id}`,
      `event: ${EVENT.event_type}`,
      `data: ${JSON.stringify(EVENT)}`,
      "",
      "",
    ].join("\n"));
    expect(parsed).toMatchObject({ kind: "event", id: "event-3" });
    expect(() => parseSseFrameV2({
      id: EVENT.event_id,
      event: EVENT.event_type,
      data: JSON.stringify({ ...EVENT, payload: { ...EVENT.payload, host_count: 99 } }),
    })).toThrow(/digest/i);
  });

  it("enforces monotonic replay while permitting exact at-least-once duplicates", () => {
    const authority = new DesktopEventReplayAuthorityV2();
    const parsed = parseSseFrameV2({ id: EVENT.event_id, event: EVENT.event_type, data: JSON.stringify(EVENT) });
    if (parsed.kind !== "event") throw new Error("fixture must be an event");
    expect(authority.observe(parsed.envelope)).toEqual({ kind: "accepted", event: parsed.envelope });
    expect(authority.observe(parsed.envelope)).toEqual({ kind: "duplicate", event: parsed.envelope });
    expect(() => authority.observe({ ...parsed.envelope, sequence: 2, event_id: "event-2" })).toThrow(/sequence/i);
    expect(() => authority.observe({ ...parsed.envelope, payload_sha256: "f".repeat(64) })).toThrow(/replay/i);
  });

  it("turns only the typed 410 cursor expiry into a snapshot reload", () => {
    const expired = {
      schema_version: "2",
      code: "event_cursor_expired",
      summary: "The retained replay window no longer contains this cursor.",
      retryable: true,
      action: "retry",
      affected_resource_id: null,
    };
    expect(parseEventStreamFailureV2(410, expired)).toMatchObject({
      kind: "cursor_expired",
      reloadSnapshots: true,
      resumeFromEventId: null,
    });
    expect(() => parseEventStreamFailureV2(409, { ...expired, code: "idempotency_conflict" }))
      .toThrow(DesktopApiErrorV2);
    expect(() => parseEventStreamFailureV2(410, { ...expired, secret_path: "/tmp/canary" }))
      .toThrow(DesktopContractErrorV2);
  });
});
