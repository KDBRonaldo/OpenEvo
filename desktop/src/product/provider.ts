import type {
  ApiErrorV1,
  ArtifactContentV1,
  ArtifactDiffV1,
  ArtifactV1,
  DesktopStateV1,
  HostKeyAcceptV1,
  LocalOperationV1,
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

export interface ProjectSourceSelectionIntent extends ProductMutationIntent {
  readonly kind: "native_folder_snapshot";
  readonly projectId?: string;
}

export interface DesktopProductProvider {
  readonly providerKind: VersionInfoV1["provider_kind"];
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
  cancelRun(runId: string, intent: ProductResourceMutationIntent): Promise<RunV1>;
  cancelOperation(operationId: string, intent: ProductResourceMutationIntent): Promise<LocalOperationV1>;
  getRunLogs(runId: string): Promise<readonly LogEntryV1[]>;
  getArtifactContent(artifactId: string): Promise<ArtifactContentV1>;
  getArtifactDiff(artifactId: string): Promise<ArtifactDiffV1>;
}

export interface ReleaseDesktopProductProvider extends DesktopProductProvider {
  retryRun(runId: string, intent: ProductResourceMutationIntent): Promise<RunV1>;
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
  cancelRun: unavailable,
  cancelOperation: unavailable,
  getRunLogs: unavailable,
  getArtifactContent: unavailable,
  getArtifactDiff: unavailable,
};
