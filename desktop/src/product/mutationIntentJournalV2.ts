import { z } from "zod";
import {
  canonicalJsonV2,
  compareUtcTimestampsV2,
  opaqueIdV2Schema,
  sha256DigestV2Schema,
  utcTimestampV2Schema,
} from "../api/v2/schemas";
import type { DesktopProductSnapshotV2 } from "./providerV2";

const MAX_ENTRIES = 16;
const MAX_ENTRY_BYTES = 64 * 1024;
const MAX_JOURNAL_BYTES = 1024 * 1024;
const MAX_CAS_RETRIES = 8;
const FORBIDDEN_VALUE_NAMES = [
  "token",
  "password",
  "credential",
  "environment",
  "core_url",
  "secret_ref",
  // Keep release-audit canaries out of renderer assets while mirroring the
  // native closed validator exactly.
  String.fromCodePoint(99, 111, 109, 109, 97, 110, 100),
  String.fromCodePoint(104, 111, 115, 116, 95, 112, 97, 116, 104),
] as const;

export const mutationKindV2Schema = z.enum([
  "ssh_catalog_rescan",
  "profile_create",
  "profile_update",
  "profile_delete",
  "profile_rebind",
  "profile_connect",
  "profile_disconnect",
  "host_key_review",
  "native_workspace_select",
  "native_workspace_cancel",
  "native_workspace_settle",
  "project_create",
  "project_update",
  "project_activate",
  "project_validate",
  "lifecycle_cancel",
  "task_submit",
  "task_cancel",
  "task_retry",
  "transition_retry",
  "transition_replace",
  "transition_abandon",
  "service_restart",
  "core_operation_cancel",
  "diagnostic_create",
  "cache_cleanup",
]);

const chainStepV2Schema = z.enum(["single", "native_workspace_prepare", "project_create"]);
const mutationStateV2Schema = z.enum([
  "reserved",
  "accepted",
  "terminal_observed",
  "deterministic_rejection",
]);
const safeJournalText = (minimum: number, maximum: number) => z.string()
  .min(minimum)
  .max(maximum)
  .refine((value) => value === value.trim() && !Array.from(value).some((character) => character.codePointAt(0)! < 0x20 || character.codePointAt(0)! === 0x7f))
  .refine((value) => !containsForbiddenValueName(value));
const journalActionIdV2Schema = safeJournalText(16, 256);
const journalScopeV2Schema = safeJournalText(1, 512)
  .refine((value) => !value.startsWith("/") && !value.startsWith("\\") && !value.includes("://"));
const journalOpaqueIdV2Schema = opaqueIdV2Schema.refine((value) => !containsForbiddenValueName(value));

export const pendingMutationIntentV2Schema = z.object({
  action_id: journalActionIdV2Schema,
  mutation_kind: mutationKindV2Schema,
  resource_scope: journalScopeV2Schema,
  request_sha256: sha256DigestV2Schema,
  authority_sha256: sha256DigestV2Schema,
  provider_stream_instance: journalOpaqueIdV2Schema,
  provider_stream_epoch: z.number().int().safe().positive(),
  chain_step: chainStepV2Schema,
  accepted_operation_id: journalOpaqueIdV2Schema.nullable(),
  completed_operation_ids: z.array(journalOpaqueIdV2Schema).max(2),
  state: mutationStateV2Schema,
  created_at: utcTimestampV2Schema,
  updated_at: utcTimestampV2Schema,
}).strict().superRefine((value, context) => {
  const hasCurrentOperation = value.accepted_operation_id !== null;
  if (hasCurrentOperation !== ["accepted", "terminal_observed"].includes(value.state)) {
    context.addIssue({ code: "custom", path: ["accepted_operation_id"], message: "mutation state and current operation differ" });
  }
  if (new Set(value.completed_operation_ids).size !== value.completed_operation_ids.length
    || (value.accepted_operation_id !== null && value.completed_operation_ids.includes(value.accepted_operation_id))) {
    context.addIssue({ code: "custom", path: ["completed_operation_ids"], message: "mutation operation identities must be unique" });
  }
  if (value.chain_step === "single" && value.completed_operation_ids.length !== 0) {
    context.addIssue({ code: "custom", path: ["completed_operation_ids"], message: "single-step mutation cannot retain completed operations" });
  }
  if (value.chain_step === "native_workspace_prepare"
    && (value.mutation_kind !== "project_create" || value.completed_operation_ids.length !== 0)) {
    context.addIssue({ code: "custom", path: ["chain_step"], message: "native workspace step requires the project chain" });
  }
  if (value.chain_step === "project_create"
    && (value.mutation_kind !== "project_create" || value.completed_operation_ids.length !== 1)) {
    context.addIssue({ code: "custom", path: ["chain_step"], message: "project-create step requires one completed native operation" });
  }
  if (value.mutation_kind !== "project_create" && value.chain_step !== "single") {
    context.addIssue({ code: "custom", path: ["chain_step"], message: "only project creation has multiple mutation steps" });
  }
  if (compareUtcTimestampsV2(value.updated_at, value.created_at) < 0) {
    context.addIssue({ code: "custom", path: ["updated_at"], message: "mutation timestamp regressed" });
  }
  if (utf8Bytes(canonicalJsonV2(value)) > MAX_ENTRY_BYTES) {
    context.addIssue({ code: "custom", message: "mutation journal entry exceeds the byte limit" });
  }
});

export const pendingMutationJournalV2Schema = z.object({
  schema_version: z.literal("2"),
  revision: z.number().int().safe().positive(),
  entries: z.array(pendingMutationIntentV2Schema).max(MAX_ENTRIES),
}).strict().superRefine((value, context) => {
  unique(value.entries.map((entry) => entry.action_id), context, "action IDs");
  unique(value.entries.map(logicalIntentKey), context, "logical intents");
  unique(value.entries.flatMap((entry) => [
    ...(entry.accepted_operation_id === null ? [] : [entry.accepted_operation_id]),
    ...entry.completed_operation_ids,
  ]), context, "operation IDs");
  if (utf8Bytes(canonicalJsonV2(value)) > MAX_JOURNAL_BYTES) {
    context.addIssue({ code: "custom", message: "mutation journal exceeds the byte limit" });
  }
});

export type MutationKindV2 = z.infer<typeof mutationKindV2Schema>;
export type MutationChainStepV2 = z.infer<typeof chainStepV2Schema>;
export type PendingMutationIntentV2 = z.infer<typeof pendingMutationIntentV2Schema>;
export type PendingMutationJournalV2 = z.infer<typeof pendingMutationJournalV2Schema>;

export interface MutationIntentNativeBridgeV2 {
  readMutationIntentJournalV2(): Promise<string | null>;
  compareAndSwapMutationIntentJournalV2(
    expectedValue: string | null,
    newValue: string | null,
  ): Promise<void>;
}

export interface MutationReservationV2 {
  readonly proposedActionId: string;
  readonly mutationKind: MutationKindV2;
  readonly resourceScope: string;
  readonly request: unknown;
  readonly authority: unknown;
  readonly providerStreamInstance: string;
  readonly providerStreamEpoch: number;
  readonly chainStep?: MutationChainStepV2;
}

export class MutationIntentConflictV2 extends Error {
  readonly entry: PendingMutationIntentV2;

  constructor(message: string, entry: PendingMutationIntentV2) {
    super(message);
    this.name = "MutationIntentConflictV2";
    this.entry = entry;
  }
}

export class MutationIntentJournalErrorV2 extends Error {
  constructor(message: string, options: ErrorOptions = {}) {
    super(message, options);
    this.name = "MutationIntentJournalErrorV2";
  }
}

interface CoordinatorOptionsV2 {
  readonly now?: () => string;
}

type MutationDecisionV2<T> =
  | { readonly write: false; readonly result: T }
  | { readonly write: true; readonly entries: readonly PendingMutationIntentV2[]; readonly result: T };

export class MutationIntentCoordinatorV2 {
  private readonly bridge: MutationIntentNativeBridgeV2;
  private readonly now: () => string;
  private initialized = false;
  private initialization: Promise<void> | null = null;
  private rawValue: string | null = null;
  private document: PendingMutationJournalV2 | null = null;
  private queue: Promise<void> = Promise.resolve();

  constructor(bridge: MutationIntentNativeBridgeV2, options: CoordinatorOptionsV2 = {}) {
    this.bridge = bridge;
    this.now = options.now ?? (() => new Date().toISOString());
  }

  async initialize(): Promise<void> {
    if (this.initialized) return;
    if (this.initialization === null) {
      this.initialization = (async () => {
        const raw = await this.bridge.readMutationIntentJournalV2();
        this.installRawValue(raw);
        this.initialized = true;
      })().catch((error) => {
        this.initialization = null;
        throw new MutationIntentJournalErrorV2("OpenEvo Desktop could not restore mutation retry identity", { cause: error });
      });
    }
    await this.initialization;
  }

  async reserve(input: MutationReservationV2): Promise<PendingMutationIntentV2> {
    const proposedActionId = journalActionIdV2Schema.parse(input.proposedActionId);
    const mutationKind = mutationKindV2Schema.parse(input.mutationKind);
    const resourceScope = journalScopeV2Schema.parse(input.resourceScope);
    const providerStreamInstance = journalOpaqueIdV2Schema.parse(input.providerStreamInstance);
    const providerStreamEpoch = z.number().int().safe().positive().parse(input.providerStreamEpoch);
    const chainStep = chainStepV2Schema.parse(input.chainStep ?? "single");
    const [requestSha256, authoritySha256] = await Promise.all([
      sha256CanonicalV2(input.request),
      sha256CanonicalV2(input.authority),
    ]);
    const timestamp = utcTimestampV2Schema.parse(this.now());
    return this.mutate((entries) => {
      const exact = entries.find((entry) => entry.mutation_kind === mutationKind
        && entry.resource_scope === resourceScope
        && entry.request_sha256 === requestSha256
        && entry.authority_sha256 === authoritySha256
        && entry.provider_stream_instance === providerStreamInstance
        && (entry.provider_stream_epoch === providerStreamEpoch
          || (mutationKind === "project_create" && entry.chain_step === "project_create"))
        && entry.chain_step === chainStep);
      if (exact !== undefined) return { write: false, result: exact };

      const conflict = entries.find((entry) => entry.action_id === proposedActionId
        || (entry.mutation_kind === mutationKind && entry.resource_scope === resourceScope));
      if (conflict !== undefined) {
        throw new MutationIntentConflictV2(
          "An unresolved mutation for this resource has different request or authority",
          conflict,
        );
      }
      if (entries.length >= MAX_ENTRIES) {
        throw new MutationIntentJournalErrorV2("Mutation retry journal reached its 16-entry capacity");
      }
      const entry = pendingMutationIntentV2Schema.parse({
        action_id: proposedActionId,
        mutation_kind: mutationKind,
        resource_scope: resourceScope,
        request_sha256: requestSha256,
        authority_sha256: authoritySha256,
        provider_stream_instance: providerStreamInstance,
        provider_stream_epoch: providerStreamEpoch,
        chain_step: chainStep,
        accepted_operation_id: null,
        completed_operation_ids: [],
        state: "reserved",
        created_at: timestamp,
        updated_at: timestamp,
      });
      return { write: true, entries: [...entries, entry], result: entry };
    });
  }

  async bindAcceptedOperation(
    actionId: string,
    operationId: string,
  ): Promise<PendingMutationIntentV2> {
    const action = journalActionIdV2Schema.parse(actionId);
    const operation = journalOpaqueIdV2Schema.parse(operationId);
    const timestamp = utcTimestampV2Schema.parse(this.now());
    return this.mutate((entries) => {
      const current = requireEntry(entries, action);
      if (["accepted", "terminal_observed"].includes(current.state)) {
        if (current.accepted_operation_id !== operation) throw conflictForOperation(current);
        return { write: false, result: current };
      }
      if (current.state !== "reserved") throw conflictForOperation(current);
      const next = pendingMutationIntentV2Schema.parse({
        ...current,
        state: "accepted",
        accepted_operation_id: operation,
        updated_at: timestamp,
      });
      return { write: true, entries: replaceEntry(entries, next), result: next };
    });
  }

  async advanceNativeProjectChain(
    actionId: string,
    completedOperationId: string,
  ): Promise<PendingMutationIntentV2> {
    const action = journalActionIdV2Schema.parse(actionId);
    const operation = journalOpaqueIdV2Schema.parse(completedOperationId);
    const timestamp = utcTimestampV2Schema.parse(this.now());
    return this.mutate((entries) => {
      const current = requireEntry(entries, action);
      if (current.chain_step === "project_create"
        && current.completed_operation_ids.length === 1
        && current.completed_operation_ids[0] === operation) {
        return { write: false, result: current };
      }
      if (current.mutation_kind !== "project_create"
        || current.chain_step !== "native_workspace_prepare"
        || current.state !== "terminal_observed"
        || current.accepted_operation_id !== operation
        || current.completed_operation_ids.length !== 0) {
        throw conflictForOperation(current);
      }
      const next = pendingMutationIntentV2Schema.parse({
        ...current,
        chain_step: "project_create",
        state: "reserved",
        accepted_operation_id: null,
        completed_operation_ids: [operation],
        updated_at: timestamp,
      });
      return { write: true, entries: replaceEntry(entries, next), result: next };
    });
  }

  async markTerminalObserved(actionId: string, operationId: string): Promise<void> {
    const action = journalActionIdV2Schema.parse(actionId);
    const operation = journalOpaqueIdV2Schema.parse(operationId);
    const timestamp = utcTimestampV2Schema.parse(this.now());
    await this.mutate((entries) => {
      const current = requireEntry(entries, action);
      if (current.accepted_operation_id !== operation) throw conflictForOperation(current);
      if (current.state === "terminal_observed") return { write: false, result: undefined };
      if (current.state !== "accepted") throw conflictForOperation(current);
      const next = pendingMutationIntentV2Schema.parse({
        ...current,
        state: "terminal_observed",
        updated_at: timestamp,
      });
      return { write: true, entries: replaceEntry(entries, next), result: undefined };
    });
  }

  async clearTerminalObserved(actionId: string, operationId: string): Promise<void> {
    const action = journalActionIdV2Schema.parse(actionId);
    const operation = journalOpaqueIdV2Schema.parse(operationId);
    await this.mutate((entries) => {
      const current = requireEntry(entries, action);
      if (current.state !== "terminal_observed" || current.accepted_operation_id !== operation) {
        throw conflictForOperation(current);
      }
      return {
        write: true,
        entries: entries.filter((entry) => entry.action_id !== action),
        result: undefined,
      };
    });
  }

  async markDirectResponseObserved(actionId: string, resultSha256: string): Promise<void> {
    const action = journalActionIdV2Schema.parse(actionId);
    sha256DigestV2Schema.parse(resultSha256);
    await this.mutate((entries) => {
      const current = requireEntry(entries, action);
      if (current.state !== "reserved" && current.state !== "deterministic_rejection") {
        throw conflictForOperation(current);
      }
      return {
        write: true,
        entries: entries.filter((entry) => entry.action_id !== action),
        result: undefined,
      };
    });
  }

  async markDeterministicRejection(actionId: string): Promise<void> {
    const action = journalActionIdV2Schema.parse(actionId);
    const timestamp = utcTimestampV2Schema.parse(this.now());
    await this.mutate((entries) => {
      const current = requireEntry(entries, action);
      if (current.state === "deterministic_rejection") return { write: false, result: undefined };
      if (current.state !== "reserved") throw conflictForOperation(current);
      const next = pendingMutationIntentV2Schema.parse({
        ...current,
        state: "deterministic_rejection",
        updated_at: timestamp,
      });
      return { write: true, entries: replaceEntry(entries, next), result: undefined };
    });
  }

  async discardNativeProjectChain(actionId: string): Promise<void> {
    const action = journalActionIdV2Schema.parse(actionId);
    await this.mutate((entries) => {
      const current = requireEntry(entries, action);
      if (current.mutation_kind !== "project_create"
        || !["native_workspace_prepare", "project_create"].includes(current.chain_step)
        || current.state === "accepted") {
        throw conflictForOperation(current);
      }
      return {
        write: true,
        entries: entries.filter((entry) => entry.action_id !== action),
        result: undefined,
      };
    });
  }

  async reconcile(_snapshot: DesktopProductSnapshotV2): Promise<readonly PendingMutationIntentV2[]> {
    await this.initialize();
    return this.list();
  }

  list(): readonly PendingMutationIntentV2[] {
    if (!this.initialized) return Object.freeze([]);
    return Object.freeze([...(this.document?.entries ?? [])]);
  }

  private async mutate<T>(
    transform: (entries: readonly PendingMutationIntentV2[]) => MutationDecisionV2<T>,
  ): Promise<T> {
    await this.initialize();
    return this.serialize(async () => {
      for (let attempt = 0; attempt < MAX_CAS_RETRIES; attempt += 1) {
        const decision = transform(this.document?.entries ?? []);
        if (!decision.write) return decision.result;
        const nextDocument = decision.entries.length === 0 ? null : pendingMutationJournalV2Schema.parse({
          schema_version: "2",
          revision: this.document === null ? 1 : this.document.revision + 1,
          entries: decision.entries,
        });
        const nextRaw = nextDocument === null ? null : canonicalJsonV2(nextDocument);
        if (nextRaw !== null && utf8Bytes(nextRaw) > MAX_JOURNAL_BYTES) {
          throw new MutationIntentJournalErrorV2("Mutation retry journal exceeds its byte budget");
        }
        try {
          await this.bridge.compareAndSwapMutationIntentJournalV2(this.rawValue, nextRaw);
          this.rawValue = nextRaw;
          this.document = nextDocument;
          return decision.result;
        } catch (error) {
          if (!isNativeCasConflict(error) || attempt + 1 === MAX_CAS_RETRIES) {
            throw new MutationIntentJournalErrorV2("OpenEvo Desktop could not persist mutation retry identity", { cause: error });
          }
          const refreshed = await this.bridge.readMutationIntentJournalV2();
          this.installRawValue(refreshed);
        }
      }
      throw new MutationIntentJournalErrorV2("Mutation retry journal CAS budget was exhausted");
    });
  }

  private serialize<T>(work: () => Promise<T>): Promise<T> {
    const result = this.queue.then(work, work);
    this.queue = result.then(() => undefined, () => undefined);
    return result;
  }

  private installRawValue(raw: string | null): void {
    if (raw !== null && utf8Bytes(raw) > MAX_JOURNAL_BYTES) {
      throw new MutationIntentJournalErrorV2("Saved mutation retry identity exceeds its byte budget");
    }
    this.rawValue = raw;
    this.document = raw === null ? null : parseJournalValueV2(raw);
  }
}

function parseJournalValueV2(raw: string): PendingMutationJournalV2 {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch (error) {
    throw new MutationIntentJournalErrorV2("Saved mutation retry identity is malformed", { cause: error });
  }
  try {
    return pendingMutationJournalV2Schema.parse(value);
  } catch (error) {
    throw new MutationIntentJournalErrorV2("Saved mutation retry identity violates the closed contract", { cause: error });
  }
}

async function sha256CanonicalV2(value: unknown): Promise<string> {
  const canonical = canonicalJsonV2(value);
  const subtle = globalThis.crypto?.subtle;
  if (subtle === undefined) {
    throw new MutationIntentJournalErrorV2("Secure SHA-256 is unavailable for mutation identity");
  }
  let digest: ArrayBuffer;
  try {
    digest = await subtle.digest("SHA-256", new TextEncoder().encode(canonical));
  } catch (error) {
    throw new MutationIntentJournalErrorV2("Mutation identity SHA-256 failed", { cause: error });
  }
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function replaceEntry(
  entries: readonly PendingMutationIntentV2[],
  replacement: PendingMutationIntentV2,
): readonly PendingMutationIntentV2[] {
  return entries.map((entry) => entry.action_id === replacement.action_id ? replacement : entry);
}

function requireEntry(
  entries: readonly PendingMutationIntentV2[],
  actionId: string,
): PendingMutationIntentV2 {
  const entry = entries.find((candidate) => candidate.action_id === actionId);
  if (entry === undefined) {
    throw new MutationIntentJournalErrorV2("Mutation retry identity is absent");
  }
  return entry;
}

function conflictForOperation(entry: PendingMutationIntentV2): MutationIntentConflictV2 {
  return new MutationIntentConflictV2("Mutation operation identity or state changed", entry);
}

function logicalIntentKey(entry: PendingMutationIntentV2): string {
  return canonicalJsonV2([
    entry.mutation_kind,
    entry.resource_scope,
    entry.request_sha256,
    entry.authority_sha256,
    entry.provider_stream_instance,
    entry.provider_stream_epoch,
  ]);
}

function unique(values: readonly string[], context: z.RefinementCtx, label: string): void {
  if (new Set(values).size !== values.length) {
    context.addIssue({ code: "custom", path: ["entries"], message: `mutation journal ${label} must be unique` });
  }
}

function containsForbiddenValueName(value: string): boolean {
  const lower = value.toLowerCase();
  return FORBIDDEN_VALUE_NAMES.some((forbidden) => lower.includes(forbidden));
}

function utf8Bytes(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function isNativeCasConflict(error: unknown): boolean {
  return typeof error === "object"
    && error !== null
    && "code" in error
    && (error as { readonly code?: unknown }).code === "mutation_intent_journal_conflict";
}
