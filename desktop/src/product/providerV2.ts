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
  readonly projectId?: string;
}

export interface ProjectDraftV2 {
  readonly profileId: string;
  readonly displayName: string;
  readonly config: ScienceProjectConfigV2;
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
  selectNativeWorkspace(intent: NativeWorkspaceSelectionIntentV2): Promise<NativeWorkspaceSourceV2>;
  cancelNativeWorkspace(actionId: string): Promise<void>;
  settleNativeWorkspace(actionId: string, outcome: "adopt" | "discard"): Promise<void>;
  createProject(draft: ProjectDraftV2, intent: ProductMutationIntentV2): Promise<ProjectV2>;
  updateProject(projectId: string, displayName: string, config: ScienceProjectConfigV2, intent: ProductMutationIntentV2): Promise<ProjectV2>;
  activateProject(projectId: string, intent: ProductMutationIntentV2): Promise<LifecycleOperationV2>;
  loadProjectCapabilities(projectId: string): Promise<ProjectCapabilityProjectionV2>;
  validateProject(projectId: string, intent: ProductMutationIntentV2): Promise<ProjectValidationV2>;
  submitTask(projectId: string, intent: ProductMutationIntentV2): Promise<TaskV2>;
  cancelTask(taskId: string, intent: ProductMutationIntentV2): Promise<OperationV2>;
  retryTask(taskId: string, intent: ProductMutationIntentV2): Promise<LocalOperationV2>;
  getProjectHead(projectHeadId: string): Promise<ProjectHeadRefV2>;
  getEvolutionRevision(evolutionRevisionId: string): Promise<EvolutionRevisionRefV2>;
  getRuntimeContext(runtimeContextSnapshotId: string): Promise<RuntimeContextSnapshotRefV2>;
  retryTransition(transitionId: string, intent: ProductMutationIntentV2): Promise<OperationV2>;
  replaceTransition(transitionId: string, intent: ProductMutationIntentV2): Promise<LocalOperationV2>;
  abandonTransition(transitionId: string, intent: ProductMutationIntentV2): Promise<OperationV2>;
  getArtifactContent(artifactId: string): Promise<ArtifactContentV2>;
  getArtifactDiff(artifactId: string, previousArtifactId?: string): Promise<ArtifactDiffV2>;
  restartService(serviceId: string, intent: ProductMutationIntentV2): Promise<OperationV2>;
  createDiagnostic(input: Omit<DiagnosticRequestV2, "schema_version" | "profile_id" | "profile_connection_generation">, intent: ProductMutationIntentV2): Promise<DiagnosticV2>;
  getDiagnostic(diagnosticId: string): Promise<DiagnosticV2>;
}

export function isDesktopProductProviderV2(value: unknown): value is DesktopProductProviderV2 {
  return typeof value === "object" && value !== null && (value as { apiVersion?: unknown }).apiVersion === 2;
}

export class DesktopProductProviderUnavailableErrorV2 extends Error {
  constructor() {
    super("OpenEvo Desktop could not reach its local v2 service.");
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
  getProjectHead: unavailable,
  getEvolutionRevision: unavailable,
  getRuntimeContext: unavailable,
  retryTransition: unavailable,
  replaceTransition: unavailable,
  abandonTransition: unavailable,
  getArtifactContent: unavailable,
  getArtifactDiff: unavailable,
  restartService: unavailable,
  createDiagnostic: unavailable,
  getDiagnostic: unavailable,
};

export type { ProjectCreateV2, ProjectPatchV2 };
