import type {
  ArtifactContentV2,
  ArtifactDiffV2,
  ArtifactV2,
  CoreEventEnvelopeV2,
  DesktopErrorV2,
  DesktopStateV2,
  DiagnosticRequestV2,
  DiagnosticV2,
  EvolutionRevisionRefV2,
  HostKeyReviewRequestV2,
  LifecycleOperationV2,
  LocalOperationV2,
  OperationV2,
  ProfileDisplayNamePatchV2,
  ProjectCapabilityProjectionV2,
  ProjectCreateV2,
  ProjectHeadRefV2,
  ProjectPatchV2,
  ProjectValidationV2,
  ProjectV2,
  RemoteProfileV2,
  RemoteWorkspaceProfileV2,
  RuntimeContextSnapshotRefV2,
  ScienceProjectConfigV2,
  ServiceV2,
  SshHostCatalogV2,
  SuccessorTransitionV2,
  TaskV2,
} from "../api/v2/schemas";
import type { LogPageV2 } from "../api/v2/logs";
import type { LifecycleOperationStateV2 } from "./lifecycleOperationsV2";
import type { PendingMutationIntentV2 } from "./mutationIntentJournalV2";

export type ProductOperationV2 = LocalOperationV2 | LifecycleOperationV2 | OperationV2;

export type ProductStreamStateV2 =
  | { readonly status: "fresh"; readonly epoch: number; readonly lastEventId: string | null }
  | { readonly status: "stale"; readonly epoch: number; readonly reason: "event_gap" | "refresh_pending" }
  | { readonly status: "error"; readonly epoch: number; readonly error: DesktopErrorV2 | null }
  | { readonly status: "cursor_reset"; readonly epoch: number; readonly resumeFromEventId: null };

export interface DesktopProductSnapshotV2 {
  readonly state: DesktopStateV2;
  readonly catalog: SshHostCatalogV2;
  readonly profiles: readonly RemoteProfileV2[];
  readonly projects: readonly ProjectV2[];
  readonly tasks: readonly TaskV2[];
  readonly transitions: Readonly<Record<string, SuccessorTransitionV2>>;
  readonly timelines: Readonly<Record<string, readonly CoreEventEnvelopeV2[]>>;
  readonly artifacts: readonly ArtifactV2[];
  readonly services: readonly ServiceV2[];
  readonly capability: ProjectCapabilityProjectionV2 | null;
  readonly validation: ProjectValidationV2 | null;
  readonly activeOperation: ProductOperationV2 | null;
  readonly stream: ProductStreamStateV2;
  /** Readable task, artifact, and workspace projections supplied by the active backend. */
  readonly runtimePresentation?: RuntimePresentationV2;
}

export interface RuntimePresentationV2 {
  readonly evolutionRuns?: readonly {
    readonly runId: string;
    readonly projectId: string;
    readonly baseProjectHeadId?: string;
    readonly appliedProjectHeadId?: string | null;
    readonly sourceTaskIds: readonly string[];
    readonly selections: readonly {
      readonly targetId: string;
      readonly method: string;
      readonly config: Readonly<Record<string, unknown>>;
    }[];
    readonly state: "running" | "candidate_ready" | "applied" | "failed";
    readonly artifactIds: readonly string[];
    readonly jobIds: readonly string[];
    readonly error: string | null;
    readonly createdAt: string;
    readonly updatedAt: string;
  }[];
  readonly tasks: Readonly<Record<string, {
    readonly instruction: { readonly title: string; readonly objective: string } | null;
    readonly transcript: readonly { readonly speaker: "user" | "agent" | "system"; readonly text: string }[];
    readonly outputFiles: readonly {
      readonly name: string;
      readonly summary: string;
      readonly content?: string;
      readonly previousName?: string | null;
      readonly diffLines?: readonly { readonly kind: "added" | "removed" | "context"; readonly text: string }[];
    }[];
    readonly selectedEvolution?: readonly {
      readonly targetId: string;
      readonly method: string;
      readonly config?: Readonly<Record<string, unknown>>;
    }[];
    readonly evolutionErrors?: readonly {
      readonly targetId: string;
      readonly method: string;
      readonly message: string;
    }[];
    /** True only when the backend has durably sealed this Session's transcript dataset. */
    readonly evolutionEvidenceReady?: boolean;
    readonly evolutionJobs?: readonly {
      readonly jobId: string;
      readonly targetId: string;
      readonly methodId: string;
      readonly requestedMethodId: string;
      readonly resolverInputArtifactIds: readonly string[];
      readonly previousArtifactId: string | null;
      readonly config: Readonly<Record<string, unknown>>;
      readonly state: "queued" | "running" | "completed" | "failed";
      readonly artifactIds: readonly string[];
      readonly error: string | null;
      readonly attempts: readonly {
        readonly attemptId: string;
        readonly ordinal: number;
        readonly state: "queued" | "running" | "completed" | "failed" | "cancelled";
        readonly stage: string;
        readonly artifactIds: readonly string[];
        readonly errorCode: string | null;
        readonly errorMessage: string | null;
        readonly logs: readonly string[];
        readonly createdAt: string;
        readonly startedAt: string | null;
        readonly completedAt: string | null;
        readonly updatedAt: string;
      }[];
      readonly createdAt: string;
      readonly updatedAt: string;
    }[];
    readonly usedArtifactIds: readonly string[];
    readonly producedArtifactIds: readonly string[];
  }>>;
  readonly artifacts: Readonly<Record<string, {
    readonly title: string;
    readonly sourceTaskId: string | null;
    readonly targetPath: string | null;
    readonly status: "created" | "updated" | "unchanged" | "failed" | "incompatible" | "unavailable";
    readonly statusDetail: string;
    readonly documents: readonly { readonly path: string; readonly content: string }[];
    readonly previousArtifactId: string | null;
    readonly evolutionRunId?: string | null;
    readonly applied?: boolean;
    readonly diffLines: readonly { readonly kind: "added" | "removed" | "context"; readonly text: string }[];
  }>>;
  readonly workspaces?: Readonly<Record<string, {
    readonly entries: readonly {
      readonly path: string;
      readonly kind: "file" | "directory" | "symlink" | "unreadable";
      readonly byteSize: number;
      readonly contentSha256: string | null;
      readonly mediaType: string | null;
      readonly content: string | null;
      readonly modifiedAt: string;
    }[];
    readonly truncated: boolean;
  }>>;
}

export type ProductRefreshResultV2 =
  | { readonly status: "fresh"; readonly snapshot: DesktopProductSnapshotV2 }
  | { readonly status: "stale"; readonly stream: Extract<ProductStreamStateV2, { status: "stale" }> }
  | { readonly status: "error"; readonly stream: Extract<ProductStreamStateV2, { status: "error" }> }
  | { readonly status: "cursor_reset"; readonly stream: Extract<ProductStreamStateV2, { status: "cursor_reset" }> };

export type ProductSubscriptionSignalV2 =
  | { readonly kind: "snapshot_changed" }
  | { readonly kind: "stream_stale"; readonly reason: "event_gap" | "refresh_pending" }
  | { readonly kind: "stream_error"; readonly error: DesktopErrorV2 | null }
  | { readonly kind: "cursor_reset"; readonly resumeFromEventId: null };

export interface ProductMutationIntentV2 {
  readonly actionId: string;
  readonly streamEpoch: number;
}

export interface NativeWorkspaceSourceV2 {
  readonly kind: "native_folder_snapshot";
  readonly display_name: string;
}

export interface NativeWorkspaceSelectionIntentV2 extends ProductMutationIntentV2 {
  readonly kind: "native_folder_snapshot";
  readonly draft: ProjectDraftV2;
  readonly profileAuthority: {
    readonly profileId: string;
    readonly connectionGeneration: number;
    readonly etag: string;
  };
}

export interface ProjectDraftV2 {
  readonly profileId: string;
  readonly displayName: string;
  readonly config: ScienceProjectConfigV2;
}

export interface WorkspaceFileUploadV2 {
  readonly path: string;
  readonly data: Blob;
  readonly mediaType: string;
  readonly overwrite: boolean;
}

export interface WorkspaceFileDownloadV2 {
  readonly fileName: string;
  readonly mediaType: string;
  readonly data: Blob;
}

export interface DesktopProductProviderV2 {
  readonly apiVersion: 2;
  readonly providerKind: "desktop_sidecar";
  readonly featureFlags: readonly string[];
  refresh(): Promise<ProductRefreshResultV2>;
  subscribe(listener: (signal: ProductSubscriptionSignalV2) => void): () => void;
  rescanSshHosts(intent: ProductMutationIntentV2): Promise<SshHostCatalogV2>;
  createProfile(displayName: string, sshHostAlias: string, intent: ProductMutationIntentV2): Promise<RemoteWorkspaceProfileV2>;
  renameProfile(profileId: string, input: ProfileDisplayNamePatchV2, intent: ProductMutationIntentV2): Promise<RemoteProfileV2>;
  deleteProfile(profileId: string, intent: ProductMutationIntentV2): Promise<void>;
  rebindProfile(profileId: string, sshHostAlias: string, intent: ProductMutationIntentV2): Promise<RemoteWorkspaceProfileV2>;
  connectProfile(profileId: string, intent: ProductMutationIntentV2): Promise<LifecycleOperationV2>;
  disconnectProfile(profileId: string, intent: ProductMutationIntentV2): Promise<LifecycleOperationV2>;
  reviewHostKey(profileId: string, action: HostKeyReviewRequestV2["action"], intent: ProductMutationIntentV2): Promise<LifecycleOperationV2>;
  listLifecycleOperations(): readonly LifecycleOperationStateV2[];
  getLifecycleOperation(operationId: string): Promise<LifecycleOperationV2>;
  loadLifecycleLogs(operationId: string): Promise<LifecycleOperationStateV2>;
  loadOlderLifecycleLogs(operationId: string): Promise<LifecycleOperationStateV2>;
  loadLatestLifecycleLogs(operationId: string): Promise<LifecycleOperationStateV2>;
  cancelLifecycleOperation(operationId: string, intent: ProductMutationIntentV2): Promise<LifecycleOperationV2>;
  listMutationIntents(): readonly PendingMutationIntentV2[];
  resumeMutationIntent(actionId: string): Promise<void>;
  selectNativeWorkspace(intent: NativeWorkspaceSelectionIntentV2): Promise<NativeWorkspaceSourceV2>;
  cancelNativeWorkspace(actionId: string): Promise<void>;
  settleNativeWorkspace(actionId: string, outcome: "adopt" | "discard"): Promise<void>;
  createProject(draft: ProjectDraftV2, intent: ProductMutationIntentV2): Promise<LifecycleOperationV2>;
  updateProject(projectId: string, displayName: string, config: ScienceProjectConfigV2, intent: ProductMutationIntentV2): Promise<ProjectV2>;
  activateProject(projectId: string, intent: ProductMutationIntentV2): Promise<LifecycleOperationV2>;
  loadProjectCapabilities(projectId: string): Promise<ProjectCapabilityProjectionV2>;
  validateProject(projectId: string, intent: ProductMutationIntentV2): Promise<ProjectValidationV2>;
  submitTask(
    projectId: string,
    intent: ProductMutationIntentV2,
    projectHead?: ProjectHeadRefV2,
  ): Promise<TaskV2>;
  cancelTask(taskId: string, intent: ProductMutationIntentV2): Promise<OperationV2>;
  retryTask(taskId: string, intent: ProductMutationIntentV2): Promise<LocalOperationV2>;
  retryEvolutionJob?(jobId: string, intent: ProductMutationIntentV2): Promise<void>;
  startEvolutionRun?(
    projectId: string,
    sourceTaskIds: readonly string[],
    selections: readonly {
      readonly targetId: string;
      readonly method: string;
      readonly config: Readonly<Record<string, unknown>>;
    }[],
    intent: ProductMutationIntentV2,
    baseProjectHead?: ProjectHeadRefV2,
  ): Promise<void>;
  applyEvolutionRun?(runId: string, intent: ProductMutationIntentV2): Promise<void>;
  uploadWorkspaceFile?(
    projectId: string,
    upload: WorkspaceFileUploadV2,
    intent: ProductMutationIntentV2,
  ): Promise<void>;
  downloadWorkspaceFile?(
    projectId: string,
    path: string,
  ): Promise<WorkspaceFileDownloadV2>;
  loadTaskLogs(taskId: string, options?: { readonly limit?: number; readonly after?: string }): Promise<LogPageV2>;
  getProjectHead(projectHeadId: string): Promise<ProjectHeadRefV2>;
  getEvolutionRevision(evolutionRevisionId: string): Promise<EvolutionRevisionRefV2>;
  getRuntimeContext(runtimeContextSnapshotId: string): Promise<RuntimeContextSnapshotRefV2>;
  retryTransition(transitionId: string, intent: ProductMutationIntentV2): Promise<OperationV2>;
  replaceTransition(transitionId: string, intent: ProductMutationIntentV2): Promise<LocalOperationV2>;
  abandonTransition(transitionId: string, intent: ProductMutationIntentV2): Promise<OperationV2>;
  getArtifactContent(artifactId: string): Promise<ArtifactContentV2>;
  getArtifactDiff(artifactId: string, previousArtifactId?: string): Promise<ArtifactDiffV2>;
  restartService(serviceId: string, intent: ProductMutationIntentV2): Promise<OperationV2>;
  listCoreOperations(): readonly OperationV2[];
  getCoreOperation(operationId: string): Promise<OperationV2>;
  cancelCoreOperation(operationId: string, intent: ProductMutationIntentV2): Promise<OperationV2>;
  loadServiceLogs(serviceId: string, options?: { readonly limit?: number; readonly after?: string }): Promise<LogPageV2>;
  cleanupCaches(intent: ProductMutationIntentV2): Promise<OperationV2>;
  createDiagnostic(input: Omit<DiagnosticRequestV2, "schema_version" | "profile_id" | "profile_connection_generation">, intent: ProductMutationIntentV2): Promise<DiagnosticV2>;
  listDiagnostics(): readonly DiagnosticV2[];
  getDiagnostic(diagnosticId: string): Promise<DiagnosticV2>;
}

export function isDesktopProductProviderV2(value: unknown): value is DesktopProductProviderV2 {
  return typeof value === "object" && value !== null && (value as { apiVersion?: unknown }).apiVersion === 2;
}

export class DesktopProductProviderUnavailableErrorV2 extends Error {
  constructor() {
    super("EvoLab could not reach its local service.");
    this.name = "DesktopProductProviderUnavailableErrorV2";
  }
}

const unavailable = async (): Promise<never> => {
  throw new DesktopProductProviderUnavailableErrorV2();
};

export const unavailableDesktopProductProviderV2: DesktopProductProviderV2 = {
  apiVersion: 2,
  providerKind: "desktop_sidecar",
  featureFlags: [],
  refresh: unavailable,
  subscribe: () => () => undefined,
  rescanSshHosts: unavailable,
  createProfile: unavailable,
  renameProfile: unavailable,
  deleteProfile: unavailable,
  rebindProfile: unavailable,
  connectProfile: unavailable,
  disconnectProfile: unavailable,
  reviewHostKey: unavailable,
  listLifecycleOperations: () => [],
  getLifecycleOperation: unavailable,
  loadLifecycleLogs: unavailable,
  loadOlderLifecycleLogs: unavailable,
  loadLatestLifecycleLogs: unavailable,
  cancelLifecycleOperation: unavailable,
  listMutationIntents: () => [],
  resumeMutationIntent: unavailable,
  selectNativeWorkspace: unavailable,
  cancelNativeWorkspace: unavailable,
  settleNativeWorkspace: unavailable,
  createProject: unavailable,
  updateProject: unavailable,
  activateProject: unavailable,
  loadProjectCapabilities: unavailable,
  validateProject: unavailable,
  submitTask: unavailable,
  cancelTask: unavailable,
  retryTask: unavailable,
  retryEvolutionJob: unavailable,
  loadTaskLogs: unavailable,
  getProjectHead: unavailable,
  getEvolutionRevision: unavailable,
  getRuntimeContext: unavailable,
  retryTransition: unavailable,
  replaceTransition: unavailable,
  abandonTransition: unavailable,
  getArtifactContent: unavailable,
  getArtifactDiff: unavailable,
  restartService: unavailable,
  listCoreOperations: () => [],
  getCoreOperation: unavailable,
  cancelCoreOperation: unavailable,
  loadServiceLogs: unavailable,
  cleanupCaches: unavailable,
  createDiagnostic: unavailable,
  listDiagnostics: () => [],
  getDiagnostic: unavailable,
};

export type { ProjectCreateV2, ProjectPatchV2 };
