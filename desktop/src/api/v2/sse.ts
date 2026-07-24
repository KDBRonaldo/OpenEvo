import { z } from "zod";
import { DesktopApiErrorV2, DesktopContractErrorV2 } from "./client";
import {
  canonicalJsonV2,
  desktopErrorV2Schema,
  desktopEventEnvelopeV2Schema,
  desktopEventTypeV2Schema,
  opaqueIdV2Schema,
  type DesktopErrorV2,
  type DesktopEventEnvelopeV2,
} from "./schemas";

export const MAX_DESKTOP_SSE_FRAME_BYTES_V2 = 1_048_576;
export const MAX_DESKTOP_EVENT_REPLAY_RECORDS_V2 = 10_000;

export interface SseFramePayloadV2 {
  readonly id: string;
  readonly event: string;
  readonly data: string;
  readonly retry?: number;
}

export type ParsedSseFrameV2 =
  | { readonly kind: "heartbeat" }
  | {
      readonly kind: "event";
      readonly id: string;
      readonly event: DesktopEventEnvelopeV2["event_type"];
      readonly envelope: DesktopEventEnvelopeV2;
      readonly retry: number | null;
    };

export interface CursorExpiredRecoverySignalV2 {
  readonly kind: "cursor_expired";
  readonly reloadSnapshots: true;
  readonly resumeFromEventId: null;
  readonly error: DesktopErrorV2;
}

export type DesktopEventReplayObservationV2 =
  | { readonly kind: "accepted"; readonly event: DesktopEventEnvelopeV2 }
  | { readonly kind: "duplicate"; readonly event: DesktopEventEnvelopeV2 };

const sseFramePayloadV2Schema = z.object({
  id: opaqueIdV2Schema,
  event: desktopEventTypeV2Schema,
  data: z.string().min(1).refine((value) => utf8ByteLength(value) <= MAX_DESKTOP_SSE_FRAME_BYTES_V2),
  retry: z.number().int().min(1_000).max(60_000).optional(),
}).strict();

export function parseSseFrameV2(input: string | SseFramePayloadV2): ParsedSseFrameV2 {
  const payload = typeof input === "string" ? parseRawSseFrameV2(input) : parseFramePayload(input);
  if (payload === null) return { kind: "heartbeat" };
  let decoded: unknown;
  try {
    decoded = JSON.parse(payload.data);
  } catch (error) {
    throw new DesktopContractErrorV2("Desktop v2 event frame contains malformed JSON", { cause: error });
  }
  const parsed = desktopEventEnvelopeV2Schema.safeParse(decoded);
  if (!parsed.success) {
    throw new DesktopContractErrorV2("Desktop v2 event frame contains an invalid event envelope or payload digest", { cause: parsed.error });
  }
  if (parsed.data.event_id !== payload.id) {
    throw new DesktopContractErrorV2("Desktop v2 event frame id does not match its envelope");
  }
  if (parsed.data.event_type !== payload.event) {
    throw new DesktopContractErrorV2("Desktop v2 event frame name does not match its envelope");
  }
  return {
    kind: "event",
    id: payload.id,
    event: payload.event,
    envelope: parsed.data,
    retry: payload.retry ?? null,
  };
}

export function parseEventStreamFailureV2(status: number, payload: unknown): CursorExpiredRecoverySignalV2 {
  const parsed = desktopErrorV2Schema.safeParse(payload);
  if (!parsed.success) {
    throw new DesktopContractErrorV2("Desktop v2 event stream returned an invalid error envelope", {
      cause: parsed.error,
      status,
    });
  }
  if (status === 410 && parsed.data.code === "event_cursor_expired") {
    return {
      kind: "cursor_expired",
      reloadSnapshots: true,
      resumeFromEventId: null,
      error: parsed.data,
    };
  }
  throw new DesktopApiErrorV2(status, parsed.data);
}

export class DesktopEventReplayAuthorityV2 {
  private readonly maxRecords: number;
  private readonly records = new Map<string, { readonly sequence: number; readonly canonical: string; readonly event: DesktopEventEnvelopeV2 }>();
  private lastEventIdValue: string | null;
  private lastSequenceValue: number | null;

  constructor(options: {
    readonly maxRecords?: number;
    readonly lastEventId?: string | null;
    readonly lastSequence?: number | null;
  } = {}) {
    this.maxRecords = z.number().int().min(1).max(MAX_DESKTOP_EVENT_REPLAY_RECORDS_V2)
      .parse(options.maxRecords ?? MAX_DESKTOP_EVENT_REPLAY_RECORDS_V2);
    const lastEventId = options.lastEventId ?? null;
    const lastSequence = options.lastSequence ?? null;
    if ((lastEventId === null) !== (lastSequence === null)) {
      throw new DesktopContractErrorV2("Desktop event replay seed must contain both event ID and sequence");
    }
    this.lastEventIdValue = lastEventId === null ? null : opaqueIdV2Schema.parse(lastEventId);
    this.lastSequenceValue = lastSequence === null ? null : z.number().int().safe().min(1).parse(lastSequence);
  }

  get lastEventId(): string | null {
    return this.lastEventIdValue;
  }

  get lastSequence(): number | null {
    return this.lastSequenceValue;
  }

  observe(input: unknown): DesktopEventReplayObservationV2 {
    const rawEventId = rawOpaqueEventId(input);
    const parsed = desktopEventEnvelopeV2Schema.safeParse(input);
    if (!parsed.success) {
      if (rawEventId !== null && this.records.has(rawEventId)) {
        throw new DesktopContractErrorV2("Desktop event replay differs from the previously accepted event", { cause: parsed.error });
      }
      throw new DesktopContractErrorV2("Desktop event replay contains an invalid event envelope", { cause: parsed.error });
    }
    const event = parsed.data;
    const canonical = canonicalJsonV2(event);
    const replay = this.records.get(event.event_id);
    if (replay !== undefined) {
      if (replay.sequence !== event.sequence || replay.canonical !== canonical) {
        throw new DesktopContractErrorV2("Desktop event replay differs from the previously accepted event");
      }
      return { kind: "duplicate", event: replay.event };
    }
    if (this.lastSequenceValue !== null) {
      if (event.sequence <= this.lastSequenceValue) {
        throw new DesktopContractErrorV2("Desktop event sequence regressed outside the retained replay authority");
      }
      if (event.sequence !== this.lastSequenceValue + 1) {
        throw new DesktopContractErrorV2("Desktop event sequence contains a gap; reload authoritative snapshots");
      }
    }
    this.records.set(event.event_id, { sequence: event.sequence, canonical, event });
    while (this.records.size > this.maxRecords) {
      const oldest = this.records.keys().next().value as string | undefined;
      if (oldest === undefined) break;
      this.records.delete(oldest);
    }
    this.lastEventIdValue = event.event_id;
    this.lastSequenceValue = event.sequence;
    return { kind: "accepted", event };
  }

  reset(): void {
    this.records.clear();
    this.lastEventIdValue = null;
    this.lastSequenceValue = null;
  }
}

function parseFramePayload(input: SseFramePayloadV2): z.output<typeof sseFramePayloadV2Schema> {
  if (utf8ByteLength(input.data) > MAX_DESKTOP_SSE_FRAME_BYTES_V2) {
    throw new DesktopContractErrorV2("Desktop v2 event frame exceeds the payload limit");
  }
  const parsed = sseFramePayloadV2Schema.safeParse(input);
  if (!parsed.success) {
    throw new DesktopContractErrorV2("Desktop v2 event frame has an invalid shape", { cause: parsed.error });
  }
  return parsed.data;
}

function parseRawSseFrameV2(frame: string): z.output<typeof sseFramePayloadV2Schema> | null {
  if (utf8ByteLength(frame) > MAX_DESKTOP_SSE_FRAME_BYTES_V2) {
    throw new DesktopContractErrorV2("Desktop v2 event frame exceeds the payload limit");
  }
  const lines = frame.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
  let id: string | undefined;
  let event: string | undefined;
  let retry: number | undefined;
  const data: string[] = [];
  let sawComment = false;
  for (const line of lines) {
    if (line === "") continue;
    if (line.startsWith(":")) {
      sawComment = true;
      continue;
    }
    const separator = line.indexOf(":");
    const field = separator === -1 ? line : line.slice(0, separator);
    const rawValue = separator === -1 ? "" : line.slice(separator + 1);
    const value = rawValue.startsWith(" ") ? rawValue.slice(1) : rawValue;
    if (field === "id") {
      if (id !== undefined) throw new DesktopContractErrorV2("Desktop v2 event frame contains duplicate id fields");
      id = value;
    } else if (field === "event") {
      if (event !== undefined) throw new DesktopContractErrorV2("Desktop v2 event frame contains duplicate event fields");
      event = value;
    } else if (field === "data") {
      data.push(value);
    } else if (field === "retry") {
      if (retry !== undefined || !/^[0-9]+$/.test(value)) throw new DesktopContractErrorV2("Desktop v2 event frame contains an invalid retry field");
      retry = Number(value);
    } else {
      throw new DesktopContractErrorV2(`Desktop v2 event frame contains unsupported field: ${field}`);
    }
  }
  if (id === undefined && event === undefined && data.length === 0 && retry === undefined && sawComment) return null;
  return parseFramePayload({ id: id!, event: event!, data: data.join("\n"), ...(retry === undefined ? {} : { retry }) });
}

function rawOpaqueEventId(input: unknown): string | null {
  if (input === null || typeof input !== "object" || Array.isArray(input)) return null;
  const eventId = (input as Record<string, unknown>).event_id;
  return typeof eventId === "string" && opaqueIdV2Schema.safeParse(eventId).success ? eventId : null;
}

function utf8ByteLength(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}
