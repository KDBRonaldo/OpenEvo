import { z } from "zod";
import { DesktopApiError, DesktopContractError } from "./client";
import { apiErrorV1Schema, eventEnvelopeV1Schema, type ApiErrorV1, type EventEnvelopeV1 } from "./schemas";

export interface SseFramePayload {
  readonly id: string;
  readonly event: string;
  readonly data: string;
}

export type ParsedSseFrame =
  | { readonly kind: "heartbeat" }
  | {
      readonly kind: "event";
      readonly id: string;
      readonly event: string;
      readonly envelope: EventEnvelopeV1;
    };

export interface CursorExpiredRecoverySignal {
  readonly kind: "cursor_expired";
  readonly reloadSnapshots: true;
  readonly resumeFromEventId: null;
  readonly error: ApiErrorV1;
}

const MAX_SSE_FRAME_BYTES = 1_048_576;

const sseFramePayloadSchema = z
  .object({
    id: z.string().min(1).max(256),
    event: z.string().min(1).max(128),
    data: z
      .string()
      .min(1)
      .refine((value) => utf8ByteLength(value) <= MAX_SSE_FRAME_BYTES, {
        message: "Desktop event frame exceeds the payload limit",
      }),
  })
  .strict();

export function parseSseFrame(input: string | SseFramePayload): ParsedSseFrame {
  const payload = typeof input === "string" ? parseRawSseFrame(input) : sseFramePayloadSchema.parse(input);
  if (payload === null) return { kind: "heartbeat" };

  let decoded: unknown;
  try {
    decoded = JSON.parse(payload.data);
  } catch (error) {
    throw new DesktopContractError("Desktop event frame contains malformed JSON", { cause: error });
  }
  const parsed = eventEnvelopeV1Schema.safeParse(decoded);
  if (!parsed.success) {
    throw new DesktopContractError("Desktop event frame contains an invalid event envelope", {
      cause: parsed.error,
    });
  }
  if (parsed.data.event_id !== payload.id) {
    throw new DesktopContractError("Desktop event frame id does not match its envelope");
  }
  if (parsed.data.event_name !== payload.event) {
    throw new DesktopContractError("Desktop event frame name does not match its envelope");
  }
  return {
    kind: "event",
    id: payload.id,
    event: payload.event,
    envelope: parsed.data,
  };
}

export function parseEventStreamFailure(status: number, payload: unknown): CursorExpiredRecoverySignal {
  const parsed = apiErrorV1Schema.safeParse(payload);
  if (!parsed.success) {
    throw new DesktopContractError("Desktop event stream returned an invalid error envelope", {
      cause: parsed.error,
      status,
    });
  }
  if (parsed.data.http_status !== status) {
    throw new DesktopContractError("Desktop event stream error status does not match its envelope", {
      status,
    });
  }
  if (
    status === 410 &&
    (parsed.data.code === "event_cursor_expired" || parsed.data.code === "cursor_expired")
  ) {
    return {
      kind: "cursor_expired",
      reloadSnapshots: true,
      resumeFromEventId: null,
      error: parsed.data,
    };
  }
  throw new DesktopApiError(parsed.data);
}

function parseRawSseFrame(frame: string): SseFramePayload | null {
  if (utf8ByteLength(frame) > MAX_SSE_FRAME_BYTES) {
    throw new DesktopContractError("Desktop event frame exceeds the payload limit");
  }
  const lines = frame.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
  let id: string | undefined;
  let event: string | undefined;
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
    switch (field) {
      case "id":
        if (id !== undefined) throw new DesktopContractError("Desktop event frame contains duplicate id fields");
        id = value;
        break;
      case "event":
        if (event !== undefined) throw new DesktopContractError("Desktop event frame contains duplicate event fields");
        event = value;
        break;
      case "data":
        data.push(value);
        break;
      default:
        throw new DesktopContractError(`Desktop event frame contains unsupported field: ${field}`);
    }
  }

  if (id === undefined && event === undefined && data.length === 0 && sawComment) return null;
  return sseFramePayloadSchema.parse({ id, event, data: data.join("\n") });
}

function utf8ByteLength(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}
