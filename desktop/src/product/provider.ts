import { DesktopContractError } from "../api/v1/client";
import {
  diagnosticReportV1Schema,
  localOperationV1Schema,
  operationV1Schema,
  type DiagnosticReportV1,
  type LocalOperationV1,
  type OperationV1,
} from "../api/v1/schemas";
import type {
  ApiErrorV1,
  ArtifactContentV1,
  ArtifactDiffV1,
  ArtifactV1,
  CacheCleanupRequestV1,
  DesktopStateV1,
  DiagnosticCreateV1,
  HostKeyAcceptV1,
  LogEntryV1,
  ProfileCreateV1,
  ProfilePatchV1,
  ProjectCapabilitiesV1,
  ProjectCreateV1,
  ProjectPatchV1,
  ProjectSourceV1,
  ProjectValidationV1,
  ProjectV1,
  RemoteProfileV1,
  RunV1,
  ServiceV1,
  TimelineEntryV1,
  VersionInfoV1,
} from "../api/v1/schemas";

export interface DesktopProductReleaseContract {
  readonly acceptedOpenApiDigests: readonly [string, ...string[]];
  readonly allowedProviderKinds: readonly ["desktop_sidecar"];
  readonly requiredFeatureFlags: readonly VersionInfoV1["feature_flags"][number][];
}

export function defineDesktopProductReleaseContract(
  contract: DesktopProductReleaseContract,
): DesktopProductReleaseContract {
  if (contract.acceptedOpenApiDigests.length === 0) {
    throw new Error("Desktop release contract requires a checked-in OpenAPI digest.");
  }
  for (const digest of contract.acceptedOpenApiDigests) {
    if (!/^[0-9a-f]{64}$/.test(digest)) {
      throw new Error("Desktop release contract contains an invalid OpenAPI digest.");
    }
  }
  if (contract.allowedProviderKinds.length !== 1 || contract.allowedProviderKinds[0] !== "desktop_sidecar") {
    throw new Error("Desktop release contract only permits the native sidecar provider.");
  }
  return Object.freeze({
    ...contract,
    acceptedOpenApiDigests: Object.freeze([...contract.acceptedOpenApiDigests]) as unknown as readonly [string, ...string[]],
    allowedProviderKinds: Object.freeze(["desktop_sidecar"] as const),
    requiredFeatureFlags: Object.freeze([...contract.requiredFeatureFlags]),
  });
}

export type ProductStreamState =
  | { readonly status: "fresh"; readonly epoch: number; readonly lastEventId: string | null }
  | { readonly status: "stale"; readonly epoch: number; readonly reason: "event_gap" | "refresh_pending" }
  | { readonly status: "error"; readonly epoch: number; readonly error: ApiErrorV1 | null }
  | { readonly status: "cursor_reset"; readonly epoch: number; readonly resumeFromEventId: null };

export type ProjectCapabilityState =
  | { readonly status: "loading"; readonly projectId: string; readonly executionMode: ProjectV1["execution"]["mode"] }
  | { readonly status: "unavailable"; readonly projectId: string; readonly executionMode: ProjectV1["execution"]["mode"]; readonly error: ApiErrorV1 | null }
  | { readonly status: "ready"; readonly projectId: string; readonly executionMode: ProjectV1["execution"]["mode"]; readonly value: ProjectCapabilitiesV1 };

export type ProjectValidationState =
  | { readonly status: "loading"; readonly projectId: string; readonly executionMode: ProjectV1["execution"]["mode"]; readonly projectEtag: string }
  | { readonly status: "unavailable"; readonly projectId: string; readonly executionMode: ProjectV1["execution"]["mode"]; readonly projectEtag: string; readonly error: ApiErrorV1 | null }
  | { readonly status: "ready"; readonly projectId: string; readonly executionMode: ProjectV1["execution"]["mode"]; readonly projectEtag: string; readonly value: ProjectValidationV1 };

export type ProductArtifactCollectionState =
  | { readonly status: "complete" }
  | { readonly status: "incomplete"; readonly reason: "pagination_pending" | "refresh_failed" };

export interface DesktopProductSnapshot {
  readonly state: DesktopStateV1;
  readonly executionModeCapabilities: DesktopStateV1["execution_mode_capabilities"];
  readonly profiles: readonly RemoteProfileV1[];
  readonly projects: readonly ProjectV1[];
  readonly runs: readonly RunV1[];
  readonly timelines: Readonly<Record<string, readonly TimelineEntryV1[]>>;
  readonly artifacts: readonly ArtifactV1[];
  readonly artifactCollection: ProductArtifactCollectionState;
  readonly services: readonly ServiceV1[];
  readonly capability: ProjectCapabilityState | null;
  readonly validation: ProjectValidationState | null;
  readonly activeOperation: LocalOperationV1 | null;
  readonly stream: ProductStreamState;
}

export type ProductRefreshResult =
  | { readonly status: "fresh"; readonly snapshot: DesktopProductSnapshot }
  | { readonly status: "stale"; readonly stream: Extract<ProductStreamState, { status: "stale" }> }
  | { readonly status: "error"; readonly stream: Extract<ProductStreamState, { status: "error" }> }
  | { readonly status: "cursor_reset"; readonly stream: Extract<ProductStreamState, { status: "cursor_reset" }> };

export type ProductSubscriptionSignal =
  | { readonly kind: "snapshot_changed" }
  | { readonly kind: "stream_stale"; readonly reason: "event_gap" | "refresh_pending" }
  | { readonly kind: "stream_error"; readonly error: ApiErrorV1 | null }
  | { readonly kind: "cursor_reset"; readonly resumeFromEventId: null };

export interface ProductMutationIntent {
  readonly actionId: string;
  readonly streamEpoch: number;
}

export interface ProductResourceMutationIntent extends ProductMutationIntent {
  readonly etag: string;
}

export interface ProductRunIntent extends ProductResourceMutationIntent {
  readonly projectId: string;
}

export interface ProductRunRetryRecovery {
  readonly schemaVersion: 1;
  readonly runId: string;
  readonly projectId: string;
  readonly intent: ProductResourceMutationIntent;
  readonly originalRun: RunV1;
  readonly acceptedRun: RunV1 | null;
}

export class ProductRefreshOrder {
  private sequence = 0;

  begin(): number {
    this.sequence += 1;
    return this.sequence;
  }

  isCurrent(sequence: number): boolean {
    return sequence === this.sequence;
  }
}

export const OPERATION_CONTINUATION_MAX_RESOURCES = 256;
export const OPERATION_CONTINUATION_MAX_ETAGS_PER_RESOURCE = 8;

export interface OperationContinuationAuthorityOptions {
  readonly maxResources?: number;
  readonly maxEtagsPerResource?: number;
}

type ContinuationValue = LocalOperationV1 | OperationV1 | DiagnosticReportV1;
type ContinuationFamily = "local operation" | "Core operation" | "diagnostic";

interface ContinuationObservation<T extends ContinuationValue> {
  readonly family: ContinuationFamily;
  readonly id: string;
  readonly etag: string;
  readonly status: string;
  readonly terminal: boolean;
  readonly identity: unknown;
  readonly value: T;
  readonly canonical: string;
  readonly updatedAt: string | null;
  readonly observedAt: string | null;
}

interface ContinuationEntry {
  readonly family: ContinuationFamily;
  readonly identity: string;
  readonly etags: Map<string, string>;
  currentCanonical: string;
  currentStatus: string;
  currentTerminal: boolean;
  currentUpdatedAt: string | null;
  currentObservedAt: string | null;
}

export class OperationContinuationAuthority {
  private readonly maxResources: number;
  private readonly maxEtagsPerResource: number;
  private readonly entries = new Map<string, ContinuationEntry>();

  constructor(options: OperationContinuationAuthorityOptions = {}) {
    this.maxResources = positiveBound(
      options.maxResources ?? OPERATION_CONTINUATION_MAX_RESOURCES,
      "resource",
    );
    this.maxEtagsPerResource = positiveBound(
      options.maxEtagsPerResource ?? OPERATION_CONTINUATION_MAX_ETAGS_PER_RESOURCE,
      "ETag history",
    );
  }

  get cachedResourceCount(): number {
    return this.entries.size;
  }

  get cachedRepresentationCount(): number {
    let count = 0;
    for (const entry of this.entries.values()) count += entry.etags.size;
    return count;
  }

  observeLocal(value: unknown): LocalOperationV1 {
    const operation = localOperationV1Schema.parse(value);
    assertLocalOperationTimeShape(operation);
    return this.observe({
      family: "local operation",
      id: operation.operation_id,
      etag: operation.etag,
      status: operation.state,
      terminal: localOperationTerminal(operation.state),
      identity: {
        id: operation.operation_id,
        kind: operation.operation_kind,
        resource: operation.resource,
        created_at: operation.created_at,
      },
      value: operation,
      canonical: canonicalJson(operation),
      updatedAt: null,
      observedAt: null,
    }, LOCAL_OPERATION_TRANSITIONS);
  }

  observeCore(value: unknown): OperationV1 {
    const operation = operationV1Schema.parse(value);
    assertRemoteTimeShape(
      "Core operation",
      operation.created_at,
      operation.updated_at,
      operation.observed_at,
      operation.finished_at,
    );
    if (operation.cancellation !== null) {
      assertTimestampBetween(
        "Core operation cancellation requested_at",
        operation.created_at,
        operation.cancellation.requested_at,
        operation.updated_at,
      );
    }
    return this.observe({
      family: "Core operation",
      id: operation.id,
      etag: operation.etag,
      status: operation.status,
      terminal: coreOperationTerminal(operation.status),
      identity: {
        id: operation.id,
        kind: operation.kind,
        request: operation.request,
        descriptor: operation.descriptor,
        logs_ref: operation.logs_ref,
        created_at: operation.created_at,
      },
      value: operation,
      canonical: canonicalJson(operation),
      updatedAt: operation.updated_at,
      observedAt: operation.observed_at,
    }, CORE_OPERATION_TRANSITIONS);
  }

  observeDiagnostic(value: unknown): DiagnosticReportV1 {
    const diagnostic = diagnosticReportV1Schema.parse(value);
    assertRemoteTimeShape(
      "diagnostic",
      diagnostic.created_at,
      diagnostic.updated_at,
      diagnostic.observed_at,
      diagnostic.finished_at,
    );
    return this.observe({
      family: "diagnostic",
      id: diagnostic.id,
      etag: diagnostic.etag,
      status: diagnostic.status,
      terminal: diagnostic.status === "succeeded" || diagnostic.status === "failed",
      identity: {
        id: diagnostic.id,
        scopes: diagnostic.scopes,
        target: diagnostic.target,
        created_at: diagnostic.created_at,
      },
      value: diagnostic,
      canonical: canonicalJson(diagnostic),
      updatedAt: diagnostic.updated_at,
      observedAt: diagnostic.observed_at,
    }, DIAGNOSTIC_TRANSITIONS);
  }

  private observe<T extends ContinuationValue>(
    observation: ContinuationObservation<T>,
    transitions: Readonly<Record<string, readonly string[]>>,
  ): T {
    const cacheKey = `${observation.family}:${observation.id}`;
    const identity = canonicalJson(observation.identity);
    const current = this.entries.get(cacheKey);
    if (current === undefined) {
      this.insert(cacheKey, {
        family: observation.family,
        identity,
        etags: new Map([[observation.etag, observation.canonical]]),
        currentCanonical: observation.canonical,
        currentStatus: observation.status,
        currentTerminal: observation.terminal,
        currentUpdatedAt: observation.updatedAt,
        currentObservedAt: observation.observedAt,
      });
      return observation.value;
    }

    if (current.identity !== identity) {
      throw continuationError(observation.family, "changed immutable identity fields");
    }
    const seenRepresentation = current.etags.get(observation.etag);
    if (seenRepresentation !== undefined && seenRepresentation !== observation.canonical) {
      throw continuationError(observation.family, "changed its canonical representation without changing ETag");
    }
    if (seenRepresentation !== undefined && current.currentCanonical !== observation.canonical) {
      throw continuationError(observation.family, "replayed a superseded ETag representation");
    }
    if (current.currentTerminal && current.currentCanonical !== observation.canonical) {
      throw continuationError(observation.family, "rewrote a terminal representation");
    }
    if (!(transitions[current.currentStatus] ?? []).includes(observation.status)) {
      throw continuationError(observation.family, "moved backwards or across terminal states");
    }
    assertContinuationTime(
      observation.family,
      current.currentUpdatedAt,
      observation.updatedAt,
      current.currentObservedAt,
      observation.observedAt,
      current.currentCanonical !== observation.canonical,
    );

    const nextEtags = new Map(current.etags);
    if (!nextEtags.has(observation.etag)) {
      nextEtags.set(observation.etag, observation.canonical);
      while (nextEtags.size > this.maxEtagsPerResource) {
        nextEtags.delete(nextEtags.keys().next().value as string);
      }
    }
    current.etags.clear();
    for (const [etag, canonical] of nextEtags) current.etags.set(etag, canonical);
    current.currentCanonical = observation.canonical;
    current.currentStatus = observation.status;
    current.currentTerminal = observation.terminal;
    current.currentUpdatedAt = observation.updatedAt;
    current.currentObservedAt = observation.observedAt;
    this.entries.delete(cacheKey);
    this.entries.set(cacheKey, current);
    return observation.value;
  }

  private insert(cacheKey: string, entry: ContinuationEntry): void {
    while (this.entries.size >= this.maxResources) {
      this.entries.delete(this.entries.keys().next().value as string);
    }
    this.entries.set(cacheKey, entry);
  }
}

const LOCAL_OPERATION_TRANSITIONS: Readonly<Record<LocalOperationV1["state"], readonly LocalOperationV1["state"][]>> = {
  queued: ["queued", "running", "cancelling", "succeeded", "failed", "cancelled"],
  running: ["running", "cancelling", "succeeded", "failed", "cancelled"],
  cancelling: ["cancelling", "failed", "cancelled"],
  succeeded: ["succeeded"],
  failed: ["failed"],
  cancelled: ["cancelled"],
};

const CORE_OPERATION_TRANSITIONS: Readonly<Record<OperationV1["status"], readonly OperationV1["status"][]>> = {
  queued: ["queued", "running", "cancelling", "succeeded", "failed", "cancelled"],
  running: ["running", "cancelling", "succeeded", "failed", "cancelled"],
  cancelling: ["cancelling", "cancelled"],
  succeeded: ["succeeded"],
  failed: ["failed"],
  cancelled: ["cancelled"],
};

const DIAGNOSTIC_TRANSITIONS: Readonly<Record<DiagnosticReportV1["status"], readonly DiagnosticReportV1["status"][]>> = {
  queued: ["queued", "running", "succeeded", "failed"],
  running: ["running", "succeeded", "failed"],
  succeeded: ["succeeded"],
  failed: ["failed"],
};

function positiveBound(value: number, label: string): number {
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new DesktopContractError(`Operation continuation ${label} bound is invalid`);
  }
  return value;
}

function continuationError(family: ContinuationFamily, message: string): DesktopContractError {
  return new DesktopContractError(`${family} continuation ${message}`);
}

function localOperationTerminal(state: LocalOperationV1["state"]): boolean {
  return state === "succeeded" || state === "failed" || state === "cancelled";
}

function coreOperationTerminal(status: OperationV1["status"]): boolean {
  return status === "succeeded" || status === "failed" || status === "cancelled";
}

function assertLocalOperationTimeShape(operation: LocalOperationV1): void {
  timestampKey(operation.created_at, "Local operation created_at");
  if (operation.started_at !== null) {
    assertTimestampOrder(
      "Local operation started_at",
      operation.created_at,
      operation.started_at,
    );
  }
  if (operation.finished_at !== null) {
    assertTimestampOrder(
      "Local operation finished_at",
      operation.started_at ?? operation.created_at,
      operation.finished_at,
    );
  }
}

function assertRemoteTimeShape(
  family: ContinuationFamily,
  createdAt: string,
  updatedAt: string,
  observedAt: string,
  finishedAt: string | null,
): void {
  assertTimestampOrder(`${family} updated_at`, createdAt, updatedAt);
  assertTimestampOrder(`${family} observed_at`, updatedAt, observedAt);
  if (finishedAt !== null) {
    assertTimestampBetween(`${family} finished_at`, createdAt, finishedAt, updatedAt);
  }
}

function assertContinuationTime(
  family: ContinuationFamily,
  previousUpdatedAt: string | null,
  observedUpdatedAt: string | null,
  previousObservedAt: string | null,
  observedObservedAt: string | null,
  changed: boolean,
): void {
  if (previousUpdatedAt === null || observedUpdatedAt === null
    || previousObservedAt === null || observedObservedAt === null) {
    return;
  }
  const updatedComparison = compareTimestamps(previousUpdatedAt, observedUpdatedAt);
  if (updatedComparison > 0
    || compareTimestamps(previousObservedAt, observedObservedAt) > 0) {
    throw continuationError(family, "moved its timestamps backwards");
  }
  if (changed && updatedComparison === 0) {
    throw continuationError(family, "changed without a strictly newer updated_at");
  }
}

function assertTimestampBetween(
  label: string,
  minimum: string,
  value: string,
  maximum: string,
): void {
  if (compareTimestamps(minimum, value) > 0 || compareTimestamps(value, maximum) > 0) {
    throw new DesktopContractError(`${label} is outside the authoritative resource lifetime`);
  }
}

function assertTimestampOrder(label: string, earlier: string, later: string): void {
  if (compareTimestamps(earlier, later) > 0) {
    throw new DesktopContractError(`${label} moved before its predecessor timestamp`);
  }
}

function compareTimestamps(left: string, right: string): number {
  return timestampKey(left, "timestamp").localeCompare(timestampKey(right, "timestamp"));
}

function timestampKey(value: string, label: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?Z$/.exec(value);
  if (match === null) throw new DesktopContractError(`${label} is not a UTC RFC 3339 timestamp`);
  const [, yearText, monthText, dayText, hourText, minuteText, secondText, fraction = ""] = match;
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const hour = Number(hourText);
  const minute = Number(minuteText);
  const second = Number(secondText);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const monthDays = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (month < 1 || month > 12 || day < 1 || day > monthDays[month - 1]!
    || hour > 23 || minute > 59 || second > 59) {
    throw new DesktopContractError(`${label} is not a valid UTC calendar timestamp`);
  }
  return `${yearText}${monthText}${dayText}${hourText}${minuteText}${secondText}${fraction.padEnd(9, "0")}`;
}

function canonicalJson(value: unknown): string {
  return JSON.stringify(sortCanonicalValue(value));
}

function sortCanonicalValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortCanonicalValue);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, sortCanonicalValue(item)]),
    );
  }
  return value;
}

export interface ProjectSourceSelectionIntent extends ProductMutationIntent {
  readonly kind: "native_folder_snapshot";
  readonly projectId?: string;
}

export interface DesktopProductProvider {
  readonly providerKind: VersionInfoV1["provider_kind"];
  readonly systemMaintenanceAvailable: boolean;
  refresh(): Promise<ProductRefreshResult>;
  subscribe(listener: (signal: ProductSubscriptionSignal) => void): () => void;
  createProfile(input: ProfileCreateV1, intent: ProductMutationIntent): Promise<RemoteProfileV1>;
  updateProfile(profileId: string, input: ProfilePatchV1, intent: ProductResourceMutationIntent): Promise<RemoteProfileV1>;
  connectProfile(profileId: string, intent: ProductResourceMutationIntent): Promise<LocalOperationV1>;
  acceptHostKey(profileId: string, input: HostKeyAcceptV1, intent: ProductResourceMutationIntent): Promise<LocalOperationV1>;
  createProject(input: ProjectCreateV1, intent: ProductMutationIntent): Promise<ProjectV1>;
  updateProject(projectId: string, input: ProjectPatchV1, intent: ProductResourceMutationIntent): Promise<ProjectV1>;
  activateProject(projectId: string, intent: ProductResourceMutationIntent): Promise<LocalOperationV1>;
  selectProjectSource(intent: ProjectSourceSelectionIntent): Promise<ProjectSourceV1>;
  cancelProjectSource(actionId: string): Promise<void>;
  settleProjectSource(actionId: string, outcome: "adopt" | "discard"): Promise<void>;
  startRun(intent: ProductRunIntent): Promise<RunV1>;
  retryRun?(runId: string, intent: ProductResourceMutationIntent): Promise<RunV1>;
  getRunRetryRecovery?(): ProductRunRetryRecovery | null;
  cancelRun(runId: string, intent: ProductResourceMutationIntent): Promise<RunV1>;
  cancelOperation(operationId: string, intent: ProductResourceMutationIntent): Promise<LocalOperationV1>;
  getLocalOperation(operationId: string): Promise<LocalOperationV1>;
  doctorProject(projectId: string, intent: ProductResourceMutationIntent): Promise<LocalOperationV1>;
  repairProject(projectId: string, intent: ProductResourceMutationIntent): Promise<LocalOperationV1>;
  restartService(serviceId: string, intent: ProductResourceMutationIntent): Promise<OperationV1>;
  getCoreOperation(operationId: string): Promise<OperationV1>;
  createDiagnostic(input: DiagnosticCreateV1, intent: ProductMutationIntent): Promise<DiagnosticReportV1>;
  getDiagnostic(diagnosticId: string): Promise<DiagnosticReportV1>;
  cleanupCaches(input: CacheCleanupRequestV1, intent: ProductMutationIntent): Promise<OperationV1>;
  getRunLogs(runId: string): Promise<readonly LogEntryV1[]>;
  getArtifactContent(artifactId: string): Promise<ArtifactContentV1>;
  getArtifactDiff(artifactId: string): Promise<ArtifactDiffV1>;
}

export interface ReleaseDesktopProductProvider extends DesktopProductProvider {
  retryRun(runId: string, intent: ProductResourceMutationIntent): Promise<RunV1>;
  getRunRetryRecovery(): ProductRunRetryRecovery | null;
}

const CONTINUATION_AUTHORITY_WRAPPED = Symbol("openevo.operation-continuation-authority");

export function withOperationContinuationAuthority<T extends DesktopProductProvider>(
  provider: T,
  options: OperationContinuationAuthorityOptions = {},
): T {
  if ((provider as T & { [CONTINUATION_AUTHORITY_WRAPPED]?: boolean })[CONTINUATION_AUTHORITY_WRAPPED]) {
    return provider;
  }
  const authority = new OperationContinuationAuthority(options);
  const overrides: Partial<DesktopProductProvider> = {
    refresh: async (): Promise<ProductRefreshResult> => {
      const result = await provider.refresh();
      if (result.status !== "fresh" || result.snapshot.activeOperation === null) return result;
      return {
        ...result,
        snapshot: {
          ...result.snapshot,
          activeOperation: authority.observeLocal(result.snapshot.activeOperation),
        },
      };
    },
    connectProfile: async (profileId, intent) => {
      const operation = localOperationV1Schema.parse(await provider.connectProfile(profileId, intent));
      assertLocalOperationBinding(operation, "profile_connect", "profile", profileId);
      return authority.observeLocal(operation);
    },
    acceptHostKey: async (profileId, input, intent) => {
      const operation = localOperationV1Schema.parse(await provider.acceptHostKey(profileId, input, intent));
      assertLocalOperationBinding(operation, "host_key_accept", "profile", profileId);
      return authority.observeLocal(operation);
    },
    activateProject: async (projectId, intent) => {
      const operation = localOperationV1Schema.parse(await provider.activateProject(projectId, intent));
      assertLocalOperationBinding(operation, "project_activate", "project", projectId);
      return authority.observeLocal(operation);
    },
    cancelOperation: async (operationId, intent) => {
      const operation = localOperationV1Schema.parse(await provider.cancelOperation(operationId, intent));
      assertReturnedIdentity("Local operation cancellation", operation.operation_id, operationId);
      return authority.observeLocal(operation);
    },
    getLocalOperation: async (operationId) => {
      const operation = localOperationV1Schema.parse(await provider.getLocalOperation(operationId));
      assertReturnedIdentity("Local operation lookup", operation.operation_id, operationId);
      return authority.observeLocal(operation);
    },
    doctorProject: async (projectId, intent) => {
      const operation = localOperationV1Schema.parse(await provider.doctorProject(projectId, intent));
      assertLocalOperationBinding(operation, "project_doctor", "project", projectId);
      return authority.observeLocal(operation);
    },
    repairProject: async (projectId, intent) => {
      const operation = localOperationV1Schema.parse(await provider.repairProject(projectId, intent));
      assertLocalOperationBinding(operation, "project_repair", "project", projectId);
      return authority.observeLocal(operation);
    },
    restartService: async (serviceId, intent) => {
      const operation = operationV1Schema.parse(await provider.restartService(serviceId, intent));
      if (operation.kind !== "service_restart"
        || operation.request.kind !== "service_restart"
        || operation.request.service_id !== serviceId) {
        throw new DesktopContractError("Service restart returned an operation for another service");
      }
      return authority.observeCore(operation);
    },
    getCoreOperation: async (operationId) => {
      const operation = operationV1Schema.parse(await provider.getCoreOperation(operationId));
      assertReturnedIdentity("Core operation lookup", operation.id, operationId);
      return authority.observeCore(operation);
    },
    createDiagnostic: async (input, intent) => {
      const diagnostic = diagnosticReportV1Schema.parse(await provider.createDiagnostic(input, intent));
      if (canonicalJson(diagnostic.scopes) !== canonicalJson(input.scopes)
        || canonicalJson(diagnostic.target) !== canonicalJson(input.target)) {
        throw new DesktopContractError("Diagnostic report does not match the requested scope");
      }
      return authority.observeDiagnostic(diagnostic);
    },
    getDiagnostic: async (diagnosticId) => {
      const diagnostic = diagnosticReportV1Schema.parse(await provider.getDiagnostic(diagnosticId));
      assertReturnedIdentity("Diagnostic lookup", diagnostic.id, diagnosticId);
      return authority.observeDiagnostic(diagnostic);
    },
    cleanupCaches: async (input, intent) => {
      const operation = operationV1Schema.parse(await provider.cleanupCaches(input, intent));
      if (operation.kind !== "cache_cleanup"
        || operation.request.kind !== "cache_cleanup"
        || canonicalJson(operation.request.request) !== canonicalJson(input)) {
        throw new DesktopContractError("Cache cleanup operation does not match the request");
      }
      return authority.observeCore(operation);
    },
  };
  return new Proxy(provider, {
    get(target, property) {
      if (property === CONTINUATION_AUTHORITY_WRAPPED) return true;
      const override = overrides[property as keyof DesktopProductProvider];
      if (override !== undefined) return override;
      const value: unknown = Reflect.get(target, property, target);
      return typeof value === "function" ? value.bind(target) : value;
    },
  }) as T;
}

function assertLocalOperationBinding(
  operation: LocalOperationV1,
  kind: LocalOperationV1["operation_kind"],
  resourceType: LocalOperationV1["resource"]["resource_type"],
  resourceId: string,
): void {
  if (operation.operation_kind !== kind
    || operation.resource.resource_type !== resourceType
    || operation.resource.resource_id !== resourceId) {
    throw new DesktopContractError("Local operation does not match the requested resource");
  }
}

function assertReturnedIdentity(label: string, observed: string, expected: string): void {
  if (observed !== expected) {
    throw new DesktopContractError(`${label} returned the wrong resource`);
  }
}

export class DesktopProductProviderUnavailableError extends Error {
  constructor() {
    super("OpenEvo Desktop could not reach its local service.");
    this.name = "DesktopProductProviderUnavailableError";
  }
}

export class DesktopProductUserError extends Error {
  constructor(readonly userMessage: string) {
    super(userMessage);
    this.name = "DesktopProductUserError";
  }
}

export class DesktopProductAmbiguousMutationError extends Error {
  readonly cause: unknown;

  constructor(
    readonly userMessage = "The retry outcome is not yet confirmed. OpenEvo will keep checking the remote session.",
    cause: unknown = null,
  ) {
    super(userMessage);
    this.name = "DesktopProductAmbiguousMutationError";
    this.cause = cause;
  }
}

const unavailable = async (): Promise<never> => {
  throw new DesktopProductProviderUnavailableError();
};

export const unavailableDesktopProductProvider: DesktopProductProvider = {
  providerKind: "desktop_sidecar",
  systemMaintenanceAvailable: false,
  refresh: unavailable,
  subscribe() {
    return () => undefined;
  },
  createProfile: unavailable,
  updateProfile: unavailable,
  connectProfile: unavailable,
  acceptHostKey: unavailable,
  createProject: unavailable,
  updateProject: unavailable,
  activateProject: unavailable,
  selectProjectSource: unavailable,
  cancelProjectSource: unavailable,
  settleProjectSource: unavailable,
  startRun: unavailable,
  retryRun: unavailable,
  getRunRetryRecovery: () => null,
  cancelRun: unavailable,
  cancelOperation: unavailable,
  getLocalOperation: unavailable,
  doctorProject: unavailable,
  repairProject: unavailable,
  restartService: unavailable,
  getCoreOperation: unavailable,
  createDiagnostic: unavailable,
  getDiagnostic: unavailable,
  cleanupCaches: unavailable,
  getRunLogs: unavailable,
  getArtifactContent: unavailable,
  getArtifactDiff: unavailable,
};
