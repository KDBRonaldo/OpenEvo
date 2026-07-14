import type {
  ArtifactContentV1,
  ArtifactDiffV1,
  ArtifactV1,
  DesktopStateV1,
  DiagnosticReportV1,
  HostKeyAcceptV1,
  LocalOperationV1,
  ProfileCreateV1,
  ProfilePatchV1,
  ProjectCapabilitiesV1,
  ProjectCreateV1,
  ProjectPatchV1,
  ProjectV1,
  RemoteProfileV1,
  RunV1,
  ServiceV1,
  TimelineEntryV1,
  VersionInfoV1,
} from "../api/v1/schemas";

export interface DesktopProductReleaseContract {
  // Populated from checked-in release metadata, never from provider discovery.
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

export interface DesktopProductSnapshot {
  state: DesktopStateV1;
  profiles: readonly RemoteProfileV1[];
  projects: readonly ProjectV1[];
  runs: readonly RunV1[];
  timelines: Readonly<Record<string, readonly TimelineEntryV1[]>>;
  artifacts: readonly ArtifactV1[];
  services: readonly ServiceV1[];
  capabilities: ProjectCapabilitiesV1 | null;
  diagnostic: DiagnosticReportV1 | null;
  activeOperation: LocalOperationV1 | null;
}

export interface DesktopProductProvider {
  readonly providerKind: VersionInfoV1["provider_kind"];
  getSnapshot(): Promise<DesktopProductSnapshot>;
  subscribe(listener: () => void): () => void;
  createProfile(input: ProfileCreateV1): Promise<RemoteProfileV1>;
  updateProfile(profileId: string, input: ProfilePatchV1): Promise<RemoteProfileV1>;
  configureCredential(
    profileId: string,
    slotKind: RemoteProfileV1["credential_slots"][number]["kind"],
  ): Promise<RemoteProfileV1>;
  connectProfile(profileId: string): Promise<LocalOperationV1>;
  acceptHostKey(profileId: string, input: HostKeyAcceptV1): Promise<LocalOperationV1>;
  createProject(input: ProjectCreateV1): Promise<ProjectV1>;
  updateProject(projectId: string, input: ProjectPatchV1): Promise<ProjectV1>;
  activateProject(projectId: string): Promise<LocalOperationV1>;
  startRun(projectId: string): Promise<RunV1>;
  cancelRun(runId: string): Promise<RunV1>;
  getArtifactContent(artifactId: string): Promise<ArtifactContentV1>;
  getArtifactDiff(artifactId: string): Promise<ArtifactDiffV1>;
  repairProject(projectId: string): Promise<LocalOperationV1>;
  restartService(serviceId: string): Promise<LocalOperationV1>;
}

export class DesktopProductProviderUnavailableError extends Error {
  constructor() {
    super("OpenEvo Desktop could not reach its local service.");
    this.name = "DesktopProductProviderUnavailableError";
  }
}

export const unavailableDesktopProductProvider: DesktopProductProvider = {
  providerKind: "desktop_sidecar",
  async getSnapshot() {
    throw new DesktopProductProviderUnavailableError();
  },
  subscribe() {
    return () => undefined;
  },
  async createProfile() {
    throw new DesktopProductProviderUnavailableError();
  },
  async updateProfile() {
    throw new DesktopProductProviderUnavailableError();
  },
  async configureCredential() {
    throw new DesktopProductProviderUnavailableError();
  },
  async connectProfile() {
    throw new DesktopProductProviderUnavailableError();
  },
  async acceptHostKey() {
    throw new DesktopProductProviderUnavailableError();
  },
  async createProject() {
    throw new DesktopProductProviderUnavailableError();
  },
  async updateProject() {
    throw new DesktopProductProviderUnavailableError();
  },
  async activateProject() {
    throw new DesktopProductProviderUnavailableError();
  },
  async startRun() {
    throw new DesktopProductProviderUnavailableError();
  },
  async cancelRun() {
    throw new DesktopProductProviderUnavailableError();
  },
  async getArtifactContent() {
    throw new DesktopProductProviderUnavailableError();
  },
  async getArtifactDiff() {
    throw new DesktopProductProviderUnavailableError();
  },
  async repairProject() {
    throw new DesktopProductProviderUnavailableError();
  },
  async restartService() {
    throw new DesktopProductProviderUnavailableError();
  },
};
