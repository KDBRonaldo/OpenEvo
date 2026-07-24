import { invoke } from "@tauri-apps/api/core";

export type DesktopLogAvailability = "available" | "memory_only" | "unavailable";

export type DesktopLogEntryV1 = {
  readonly schema_version: "1";
  readonly sequence: number;
  readonly occurred_at: string;
  readonly source: "native" | "startup" | "sidecar" | "renderer";
  readonly level: "info" | "warning" | "error";
  readonly event: string;
  readonly code: string | null;
  readonly exit_code: number | null;
  readonly signal: number | null;
  readonly errno: number | null;
};

export type DesktopLogTailV1 = {
  readonly schema_version: "1";
  readonly availability: DesktopLogAvailability;
  readonly entries: readonly DesktopLogEntryV1[];
  readonly dropped_count: number;
};

export type DesktopLogDirectoryResult = { readonly status: "revealed" | "unavailable" };
export type DesktopDiagnosticsExportResult = {
  readonly status: "exported" | "cancelled" | "unavailable";
};

const unavailableTail: DesktopLogTailV1 = Object.freeze({
  schema_version: "1",
  availability: "unavailable",
  entries: Object.freeze([]),
  dropped_count: 0,
});

const LOG_EVENTS = new Set([
  "application_started",
  "app_translocation_detected",
  "sidecar_start_requested",
  "sidecar_start_succeeded",
  "sidecar_start_failed",
  "sidecar_startup_diagnostic",
  "legacy_startup_diagnostic",
  "sidecar_exited_before_ready",
  "sidecar_pre_python_exit",
  "sidecar_unstructured_output_discarded",
  "sidecar_stop_requested",
  "sidecar_stop_succeeded",
  "sidecar_stop_failed",
  "sidecar_runtime_exited",
  "renderer_stage",
  "log_directory_revealed",
  "diagnostics_exported",
]);
const LOG_CODE = /^[a-z0-9_/-]{1,96}$/;
const ISO_TIMESTAMP = /^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d\.\d{3}Z$/;

function isTauriRuntime(): boolean {
  if (typeof window === "undefined") return false;
  const host = window as Window & { __TAURI__?: unknown; __TAURI_INTERNALS__?: unknown };
  return Boolean(host.__TAURI__ || host.__TAURI_INTERNALS__);
}

function record(value: unknown, keys: readonly string[]): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const candidate = value as Record<string, unknown>;
  if (
    Object.keys(candidate).length !== keys.length
    || keys.some((key) => !Object.prototype.hasOwnProperty.call(candidate, key))
  ) {
    return null;
  }
  return candidate;
}

function safeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function nullableInteger(value: unknown): value is number | null {
  return value === null || safeInteger(value);
}

function parseEntry(value: unknown): DesktopLogEntryV1 | null {
  const candidate = record(value, [
    "schema_version", "sequence", "occurred_at", "source", "level", "event", "code", "exit_code", "signal", "errno",
  ]);
  if (
    candidate === null
    || candidate.schema_version !== "1"
    || !safeInteger(candidate.sequence)
    || typeof candidate.occurred_at !== "string"
    || !ISO_TIMESTAMP.test(candidate.occurred_at)
    || !["native", "startup", "sidecar", "renderer"].includes(candidate.source as string)
    || !["info", "warning", "error"].includes(candidate.level as string)
    || typeof candidate.event !== "string"
    || !LOG_EVENTS.has(candidate.event)
    || (candidate.code !== null && (typeof candidate.code !== "string" || !LOG_CODE.test(candidate.code)))
    || !nullableInteger(candidate.exit_code)
    || !nullableInteger(candidate.signal)
    || !nullableInteger(candidate.errno)
  ) {
    return null;
  }
  return {
    schema_version: "1",
    sequence: candidate.sequence,
    occurred_at: candidate.occurred_at,
    source: candidate.source as DesktopLogEntryV1["source"],
    level: candidate.level as DesktopLogEntryV1["level"],
    event: candidate.event,
    code: candidate.code,
    exit_code: candidate.exit_code,
    signal: candidate.signal,
    errno: candidate.errno,
  };
}

function parseTail(value: unknown): DesktopLogTailV1 | null {
  const candidate = record(value, ["schema_version", "availability", "entries", "dropped_count"]);
  if (
    candidate === null
    || candidate.schema_version !== "1"
    || !["available", "memory_only", "unavailable"].includes(candidate.availability as string)
    || !Array.isArray(candidate.entries)
    || candidate.entries.length > 200
    || !safeInteger(candidate.dropped_count)
  ) {
    return null;
  }
  const entries = candidate.entries.map(parseEntry);
  if (entries.some((entry) => entry === null)) return null;
  return {
    schema_version: "1",
    availability: candidate.availability as DesktopLogAvailability,
    entries: entries as DesktopLogEntryV1[],
    dropped_count: candidate.dropped_count,
  };
}

export async function getDesktopLogTail(limit?: number): Promise<DesktopLogTailV1> {
  if (!isTauriRuntime() || (limit !== undefined && (!safeInteger(limit) || limit < 1 || limit > 200))) {
    return unavailableTail;
  }
  try {
    const response = await (limit === undefined
      ? invoke<unknown>("get_desktop_log_tail")
      : invoke<unknown>("get_desktop_log_tail", { limit }));
    return parseTail(response) ?? unavailableTail;
  } catch {
    return unavailableTail;
  }
}

export async function revealDesktopLogDirectory(): Promise<DesktopLogDirectoryResult> {
  if (!isTauriRuntime()) return { status: "unavailable" };
  try {
    const response = await invoke<unknown>("reveal_desktop_log_directory");
    return record(response, ["status"])?.status === "revealed"
      ? { status: "revealed" }
      : { status: "unavailable" };
  } catch {
    return { status: "unavailable" };
  }
}

export async function exportDesktopDiagnostics(): Promise<DesktopDiagnosticsExportResult> {
  if (!isTauriRuntime()) return { status: "unavailable" };
  try {
    const response = await invoke<unknown>("export_desktop_diagnostics");
    const status = record(response, ["status"])?.status;
    return status === "exported" || status === "cancelled"
      ? { status }
      : { status: "unavailable" };
  } catch {
    return { status: "unavailable" };
  }
}
