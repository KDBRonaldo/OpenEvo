import { z, type ZodType } from "zod";
import {
  artifactContentV2Schema,
  artifactDiffV2Schema,
  artifactPageV2Schema,
  artifactV2Schema,
  cacheCleanupRequestV2Schema,
  coreOperationV2Schema,
  desktopBootstrapContextV2Schema,
  desktopErrorV2Schema,
  desktopHealthV2Schema,
  desktopStateV2Schema,
  desktopVersionV2Schema,
  diagnosticRequestV2Schema,
  diagnosticV2Schema,
  etagV2Schema,
  evolutionRevisionRefV2Schema,
  hostKeyReviewRequestV2Schema,
  lifecycleAcknowledgeV2Schema,
  lifecycleCancelV2Schema,
  lifecycleLogPageV2Schema,
  lifecycleOperationV2Schema,
  localOperationV2Schema,
  opaqueIdV2Schema,
  profileConnectionActionV2Schema,
  profileDisplayNamePatchV2Schema,
  profileRebindV2Schema,
  projectActionV2Schema,
  projectCapabilityProjectionV2Schema,
  projectCreateV2Schema,
  projectHeadRefV2Schema,
  projectPageV2Schema,
  projectPatchV2Schema,
  projectValidationRequestV2Schema,
  projectValidationV2Schema,
  projectV2Schema,
  remoteProfilePageV2Schema,
  remoteProfileV2Schema,
  remoteWorkspaceProfileV2Schema,
  runtimeContextSnapshotRefV2Schema,
  servicePageV2Schema,
  serviceRestartV2Schema,
  sha256DigestV2Schema,
  sshHostCatalogRescanV2Schema,
  sshHostCatalogV2Schema,
  systemOpenSshProfileCreateV2Schema,
  taskActionV2Schema,
  taskContextV2Schema,
  taskPageV2Schema,
  taskSubmitRequestV2Schema,
  taskV2Schema,
  timelinePageV2Schema,
  transitionActionV2Schema,
  transitionReplaceV2Schema,
  successorTransitionV2Schema,
  type ArtifactContentV2,
  type ArtifactDiffV2,
  type ArtifactPageV2,
  type ArtifactV2,
  type CacheCleanupRequestV2,
  type DesktopBootstrapContextV2,
  type DesktopErrorV2,
  type DesktopHealthV2,
  type DesktopStateV2,
  type DesktopVersionV2,
  type DiagnosticRequestV2,
  type DiagnosticV2,
  type EvolutionRevisionRefV2,
  type HostKeyReviewRequestV2,
  type LifecycleAcknowledgeV2,
  type LifecycleCancelV2,
  type LifecycleLogPageV2,
  type LifecycleOperationKindV2,
  type LifecycleOperationV2,
  type LocalOperationV2,
  type OperationV2,
  type ProfileConnectionActionV2,
  type ProfileDisplayNamePatchV2,
  type ProfileRebindV2,
  type ProjectActionV2,
  type ProjectCapabilityProjectionV2,
  type ProjectCreateV2,
  type ProjectHeadRefV2,
  type ProjectPageV2,
  type ProjectPatchV2,
  type ProjectValidationRequestV2,
  type ProjectValidationV2,
  type ProjectV2,
  type RemoteProfilePageV2,
  type RemoteProfileV2,
  type RemoteWorkspaceProfileV2,
  type RuntimeContextSnapshotRefV2,
  type ServicePageV2,
  type ServiceRestartV2,
  type SshHostCatalogRescanV2,
  type SshHostCatalogV2,
  type SystemOpenSshProfileCreateV2,
  type TaskActionV2,
  type TaskContextV2,
  type TaskPageV2,
  type TaskSubmitRequestV2,
  type TaskV2,
  type TimelinePageV2,
  type SuccessorTransitionV2,
} from "./schemas";
import { logPageV2Schema, type LogPageV2 } from "./logs";

export const DESKTOP_API_V2_PREFIX = "/desktop/v2";
export const DESKTOP_SESSION_HEADER = "X-OpenEvo-Desktop-Session";
export const DESKTOP_RESOURCE_GENERATION_HEADER = "X-OpenEvo-Resource-Generation";
export const IDEMPOTENCY_KEY_HEADER = "Idempotency-Key";
export const IF_MATCH_HEADER = "If-Match";
export const LAST_EVENT_ID_HEADER = "Last-Event-ID";

const DEFAULT_REQUEST_TIMEOUT_MS = 15_000;
const MAX_RESPONSE_BYTES = 1_048_576;

export type FetchLikeV2 = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
export type NonEmptyReadonlyArrayV2<T> = readonly [T, ...T[]];

export interface DesktopReleaseContractV2 {
  readonly releaseVersion: string;
  readonly acceptedOpenApiDigests: NonEmptyReadonlyArrayV2<string>;
  readonly acceptedEventSchemaDigests: NonEmptyReadonlyArrayV2<string>;
  readonly allowedProviderKinds: readonly ["desktop_sidecar"];
  readonly requiredFeatureFlags: readonly string[];
}

export interface DesktopClientOptionsV2 {
  readonly fetch: FetchLikeV2;
  readonly bootstrap: () => Promise<unknown>;
  readonly contract: DesktopReleaseContractV2;
  readonly requestTimeoutMs?: number;
}

export interface ListRequestOptionsV2 {
  readonly limit?: number;
  readonly after?: string;
}

export interface LifecycleLogRequestOptionsV2 extends ListRequestOptionsV2 {
  readonly afterSequence?: number;
}

export interface TaskListRequestOptionsV2 extends ListRequestOptionsV2 {
  readonly projectId?: string;
}

export interface ArtifactDiffRequestOptionsV2 {
  readonly previousArtifactId?: string;
}

export interface MutationRequestOptionsV2 {
  readonly resourceGeneration: number;
  readonly idempotencyKey: string;
}

export interface ResourceMutationRequestOptionsV2 extends MutationRequestOptionsV2 {
  readonly ifMatch: string;
}

export interface EventStreamRequestV2 {
  readonly url: string;
  readonly headers: Readonly<Record<string, string>>;
}

const releaseContractV2Schema = z.object({
  releaseVersion: z.string().min(1).max(512),
  acceptedOpenApiDigests: z.array(sha256DigestV2Schema).min(1),
  acceptedEventSchemaDigests: z.array(sha256DigestV2Schema).min(1),
  allowedProviderKinds: z.tuple([z.literal("desktop_sidecar")]),
  requiredFeatureFlags: z.array(opaqueIdV2Schema).min(1).max(128),
}).strict().superRefine((value, context) => {
  for (const key of ["acceptedOpenApiDigests", "acceptedEventSchemaDigests", "requiredFeatureFlags"] as const) {
    if (new Set(value[key]).size !== value[key].length) {
      context.addIssue({ code: z.ZodIssueCode.custom, path: [key], message: `${key} must be unique` });
    }
  }
});

const listRequestOptionsV2Schema = z.object({
  limit: z.number().int().min(1).max(100).optional(),
  after: z.string().min(1).max(512).optional(),
}).strict();
const lifecycleLogRequestOptionsV2Schema = listRequestOptionsV2Schema.extend({
  afterSequence: z.number().int().safe().min(0).optional(),
}).strict().refine(
  (value) => value.after === undefined || value.afterSequence === undefined,
);
const taskListRequestOptionsV2Schema = listRequestOptionsV2Schema.extend({
  projectId: opaqueIdV2Schema.optional(),
}).strict();
const idempotencyKeyV2Schema = z.string().min(16).max(256)
  .refine((value) => value === value.trim() && !/[\u0000-\u001f\u007f]/.test(value));
const resourceGenerationV2Schema = z.number().int().safe().min(0);
const requestTimeoutMsV2Schema = z.number().int().min(1).max(120_000);

export class DesktopApiErrorV2 extends Error {
  readonly apiError: DesktopErrorV2;
  readonly status: number;

  constructor(status: number, apiError: DesktopErrorV2) {
    super(apiError.summary);
    this.name = "DesktopApiErrorV2";
    this.status = status;
    this.apiError = apiError;
  }
}

export class DesktopContractErrorV2 extends Error {
  readonly cause: unknown;
  readonly status: number | null;

  constructor(message: string, options: { cause?: unknown; status?: number | null } = {}) {
    super(message);
    this.name = "DesktopContractErrorV2";
    this.cause = options.cause;
    this.status = options.status ?? null;
  }
}

export interface DesktopApiClientV2 {
  version(): Promise<DesktopVersionV2>;
  health(): Promise<DesktopHealthV2>;
  state(): Promise<DesktopStateV2>;
  listSshHosts(): Promise<SshHostCatalogV2>;
  rescanSshHosts(input: SshHostCatalogRescanV2, options: MutationRequestOptionsV2): Promise<SshHostCatalogV2>;
  listProfiles(options?: ListRequestOptionsV2): Promise<RemoteProfilePageV2>;
  createProfile(input: SystemOpenSshProfileCreateV2, options: MutationRequestOptionsV2): Promise<RemoteWorkspaceProfileV2>;
  getProfile(profileId: string): Promise<RemoteProfileV2>;
  updateProfile(profileId: string, input: ProfileDisplayNamePatchV2, options: ResourceMutationRequestOptionsV2): Promise<RemoteProfileV2>;
  deleteProfile(profileId: string, options: ResourceMutationRequestOptionsV2): Promise<void>;
  rebindProfile(profileId: string, input: ProfileRebindV2, options: ResourceMutationRequestOptionsV2): Promise<RemoteWorkspaceProfileV2>;
  connectProfile(profileId: string, input: ProfileConnectionActionV2, options: ResourceMutationRequestOptionsV2): Promise<LifecycleOperationV2>;
  disconnectProfile(profileId: string, input: ProfileConnectionActionV2, options: ResourceMutationRequestOptionsV2): Promise<LifecycleOperationV2>;
  reviewProfileHostKey(profileId: string, input: HostKeyReviewRequestV2, options: ResourceMutationRequestOptionsV2): Promise<LifecycleOperationV2>;
  listProjects(options?: ListRequestOptionsV2): Promise<ProjectPageV2>;
  createProject(input: ProjectCreateV2, options: MutationRequestOptionsV2): Promise<LifecycleOperationV2>;
  getProject(projectId: string): Promise<ProjectV2>;
  updateProject(projectId: string, input: ProjectPatchV2, options: ResourceMutationRequestOptionsV2): Promise<ProjectV2>;
  activateProject(projectId: string, input: ProjectActionV2, options: ResourceMutationRequestOptionsV2): Promise<LifecycleOperationV2>;
  getLifecycleOperationByAction(actionId: string, kind: LifecycleOperationKindV2): Promise<LifecycleOperationV2>;
  getLifecycleOperation(operationId: string): Promise<LifecycleOperationV2>;
  lifecycleOperationLogs(operationId: string, options?: LifecycleLogRequestOptionsV2): Promise<LifecycleLogPageV2>;
  cancelLifecycleOperation(operationId: string, input: LifecycleCancelV2, options: ResourceMutationRequestOptionsV2): Promise<LifecycleOperationV2>;
  acknowledgeLifecycleOperation(operationId: string, input: LifecycleAcknowledgeV2, options: ResourceMutationRequestOptionsV2): Promise<void>;
  projectCapabilities(projectId: string): Promise<ProjectCapabilityProjectionV2>;
  validateProject(projectId: string, input: ProjectValidationRequestV2, options: ResourceMutationRequestOptionsV2): Promise<ProjectValidationV2>;
  listTasks(options?: TaskListRequestOptionsV2): Promise<TaskPageV2>;
  submitTask(input: TaskSubmitRequestV2, options: MutationRequestOptionsV2): Promise<TaskV2>;
  getTask(taskId: string): Promise<TaskV2>;
  cancelTask(taskId: string, input: TaskActionV2, options: ResourceMutationRequestOptionsV2): Promise<OperationV2>;
  retryTask(taskId: string, input: TaskActionV2, options: ResourceMutationRequestOptionsV2): Promise<LocalOperationV2>;
  taskTimeline(taskId: string, options?: ListRequestOptionsV2): Promise<TimelinePageV2>;
  taskLogs(taskId: string, options?: ListRequestOptionsV2): Promise<LogPageV2>;
  taskContext(taskId: string): Promise<TaskContextV2>;
  taskArtifacts(taskId: string, options?: ListRequestOptionsV2): Promise<ArtifactPageV2>;
  getProjectHead(projectHeadId: string): Promise<ProjectHeadRefV2>;
  getEvolutionRevision(evolutionRevisionId: string): Promise<EvolutionRevisionRefV2>;
  getRuntimeContext(runtimeContextSnapshotId: string): Promise<RuntimeContextSnapshotRefV2>;
  getTransition(transitionId: string): Promise<SuccessorTransitionV2>;
  retryTransition(transitionId: string, input: z.input<typeof transitionActionV2Schema>, options: ResourceMutationRequestOptionsV2): Promise<OperationV2>;
  replaceTransition(transitionId: string, input: z.input<typeof transitionReplaceV2Schema>, options: ResourceMutationRequestOptionsV2): Promise<LocalOperationV2>;
  abandonTransition(transitionId: string, input: z.input<typeof transitionActionV2Schema>, options: ResourceMutationRequestOptionsV2): Promise<OperationV2>;
  getArtifact(artifactId: string): Promise<ArtifactV2>;
  artifactContent(artifactId: string): Promise<ArtifactContentV2>;
  artifactDiff(artifactId: string, options?: ArtifactDiffRequestOptionsV2): Promise<ArtifactDiffV2>;
  listServices(options?: ListRequestOptionsV2): Promise<ServicePageV2>;
  restartService(serviceId: string, input: ServiceRestartV2, options: ResourceMutationRequestOptionsV2): Promise<OperationV2>;
  getCoreOperation(operationId: string): Promise<OperationV2>;
  cancelCoreOperation(operationId: string, options: ResourceMutationRequestOptionsV2): Promise<OperationV2>;
  serviceLogs(serviceId: string, options?: ListRequestOptionsV2): Promise<LogPageV2>;
  cleanupCaches(input: CacheCleanupRequestV2, options: MutationRequestOptionsV2): Promise<OperationV2>;
  createDiagnostic(input: DiagnosticRequestV2, options: MutationRequestOptionsV2): Promise<DiagnosticV2>;
  getDiagnostic(diagnosticId: string): Promise<DiagnosticV2>;
  eventStreamRequest(lastEventId?: string): Promise<EventStreamRequestV2>;
}

export function validateDesktopBootstrapContextV2(
  input: unknown,
  contractInput: DesktopReleaseContractV2,
): DesktopBootstrapContextV2 {
  const contract = parseReleaseContract(contractInput);
  const bootstrap = parseEnvelope(desktopBootstrapContextV2Schema, input, null, "native bootstrap");
  const negotiated = bootstrap.negotiated_contract;
  if (!contract.acceptedOpenApiDigests.includes(negotiated.openapi_sha256)) {
    throw new DesktopContractErrorV2("Native bootstrap reported an unknown Desktop OpenAPI digest");
  }
  if (!contract.acceptedEventSchemaDigests.includes(negotiated.event_schema_sha256)) {
    throw new DesktopContractErrorV2("Native bootstrap reported an unknown Desktop event schema digest");
  }
  if (!contract.allowedProviderKinds.includes(negotiated.provider_kind)) {
    throw new DesktopContractErrorV2("Native bootstrap reported a forbidden provider kind");
  }
  if (negotiated.release_version !== contract.releaseVersion) {
    throw new DesktopContractErrorV2("Native bootstrap reported another release version");
  }
  if (!desktopBuildChannelIsAllowed(negotiated.build_channel)) {
    throw new DesktopContractErrorV2("Native bootstrap did not report an allowed build channel");
  }
  if (!negotiated.mutation_compatible) {
    throw new DesktopContractErrorV2("Native bootstrap did not negotiate Desktop v2 mutations");
  }
  assertRequiredFeatures(negotiated.feature_flags, contract.requiredFeatureFlags, "Native bootstrap");
  return bootstrap;
}

export function negotiateDesktopVersionV2(
  input: unknown,
  bootstrap: DesktopBootstrapContextV2,
  contractInput: DesktopReleaseContractV2,
): DesktopVersionV2 {
  const contract = parseReleaseContract(contractInput);
  const version = parseEnvelope(desktopVersionV2Schema, input, null, "version");
  if (!contract.acceptedOpenApiDigests.includes(version.openapi_sha256)) {
    throw new DesktopContractErrorV2("Desktop Local API reported an unknown OpenAPI digest");
  }
  if (!contract.acceptedEventSchemaDigests.includes(version.event_schema_sha256)) {
    throw new DesktopContractErrorV2("Desktop Local API reported an unknown event schema digest");
  }
  if (version.release_version !== contract.releaseVersion || !desktopBuildChannelIsAllowed(version.build_channel)) {
    throw new DesktopContractErrorV2("Desktop Local API reported another release identity");
  }
  if (!version.mutation_compatible) {
    throw new DesktopContractErrorV2("Desktop Local API did not enable v2 mutations");
  }
  assertRequiredFeatures(version.feature_flags, contract.requiredFeatureFlags, "Desktop Local API");
  const negotiated = bootstrap.negotiated_contract;
  const compared: Array<[string, unknown, unknown]> = [
    ["major version", version.preferred_major, negotiated.major],
    ["mutation major", version.mutation_major, negotiated.mutation_major],
    ["OpenAPI digest", version.openapi_sha256, negotiated.openapi_sha256],
    ["event schema digest", version.event_schema_sha256, negotiated.event_schema_sha256],
    ["release version", version.release_version, negotiated.release_version],
    ["build ID", version.build_id, negotiated.build_id],
    ["source commit", version.source_commit, negotiated.source_commit],
    ["build channel", version.build_channel, negotiated.build_channel],
    ["provider kind", version.provider_kind, negotiated.provider_kind],
    ["feature-set digest", version.feature_set_sha256, negotiated.feature_set_sha256],
    ["required Core API major", version.required_core_api_major, negotiated.required_core_api_major],
    ["mutation compatibility", version.mutation_compatible, negotiated.mutation_compatible],
  ];
  for (const [label, left, right] of compared) {
    if (left !== right) throw new DesktopContractErrorV2(`Native bootstrap and Desktop Local API disagree on the ${label}`);
  }
  if (version.feature_flags.length !== negotiated.feature_flags.length
    || version.feature_flags.some((feature, index) => feature !== negotiated.feature_flags[index])) {
    throw new DesktopContractErrorV2("Native bootstrap and Desktop Local API disagree on feature flags");
  }
  return version;
}

function desktopBuildChannelIsAllowed(buildChannel: string): boolean {
  const sourceDevelopmentBuild =
    import.meta.env.DEV || import.meta.env.VITE_OPENEVO_SOURCE_DEVELOPMENT === "1";
  return buildChannel === "release" || (sourceDevelopmentBuild && buildChannel === "development");
}

export function createDesktopApiClientV2(options: DesktopClientOptionsV2): DesktopApiClientV2 {
  const contract = parseReleaseContract(options.contract);
  const requestTimeoutMs = requestTimeoutMsV2Schema.parse(options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS);
  let bootstrapPromise: Promise<DesktopBootstrapContextV2> | null = null;

  async function context(): Promise<DesktopBootstrapContextV2> {
    if (bootstrapPromise === null) {
      const candidate = options.bootstrap()
        .then((input) => validateDesktopBootstrapContextV2(input, contract))
        .catch((error) => {
          if (bootstrapPromise === candidate) bootstrapPromise = null;
          throw error;
        });
      bootstrapPromise = candidate;
    }
    return bootstrapPromise;
  }

  async function request<S extends z.ZodTypeAny>(
    method: "GET" | "POST" | "PATCH" | "DELETE",
    path: string,
    responseSchema: S,
    expectedStatus: 200 | 201 | 202,
    requestOptions: {
      readonly body?: unknown;
      readonly bodySchema?: ZodType;
      readonly mutation?: MutationRequestOptionsV2;
      readonly ifMatch?: string;
      readonly authenticated?: boolean;
    } = {},
  ): Promise<z.output<S>> {
    const bootstrap = await context();
    const headers = requestHeaders(bootstrap, requestOptions);
    let body: string | undefined;
    if (requestOptions.body !== undefined) {
      const parsed = requestOptions.bodySchema?.parse(requestOptions.body) ?? requestOptions.body;
      body = JSON.stringify(parsed);
      if (new TextEncoder().encode(body).byteLength > MAX_RESPONSE_BYTES) {
        throw new DesktopContractErrorV2("Desktop Local API request body exceeds the byte limit");
      }
      headers.set("Content-Type", "application/json");
    }
    try {
      return await withRequestDeadline(requestTimeoutMs, async (signal) => {
        const response = await options.fetch(buildUrl(bootstrap.endpoint, path), {
          method,
          headers,
          body,
          credentials: "omit",
          cache: "no-store",
          redirect: "error",
          referrerPolicy: "no-referrer",
          signal,
        });
        if (!response.ok) throw await responseError(response);
        if (response.status !== expectedStatus) {
          throw new DesktopContractErrorV2(
            `Desktop Local API returned HTTP ${response.status}; expected HTTP ${expectedStatus}`,
            { status: response.status },
          );
        }
        return parseEnvelope(responseSchema, await readJsonBounded(response), response.status, "success");
      });
    } catch (error) {
      if (isTransportFailure(error) && bootstrapPromise !== null && await promiseResolvedTo(bootstrapPromise, bootstrap)) {
        bootstrapPromise = null;
      }
      throw normalizeTransportFailure(error);
    }
  }

  async function noContent(
    path: string,
    mutation: ResourceMutationRequestOptionsV2,
  ): Promise<void> {
    const bootstrap = await context();
    const headers = requestHeaders(bootstrap, { mutation, ifMatch: mutation.ifMatch });
    try {
      await withRequestDeadline(requestTimeoutMs, async (signal) => {
        const response = await options.fetch(buildUrl(bootstrap.endpoint, path), {
          method: "DELETE",
          headers,
          credentials: "omit",
          cache: "no-store",
          redirect: "error",
          referrerPolicy: "no-referrer",
          signal,
        });
        if (!response.ok) throw await responseError(response);
        if (response.status !== 204) throw new DesktopContractErrorV2("Desktop Local API delete did not return HTTP 204", { status: response.status });
        const text = await response.text();
        if (text.length !== 0) throw new DesktopContractErrorV2("Desktop Local API HTTP 204 response contained a body", { status: 204 });
      });
    } catch (error) {
      if (isTransportFailure(error) && bootstrapPromise !== null && await promiseResolvedTo(bootstrapPromise, bootstrap)) bootstrapPromise = null;
      throw normalizeTransportFailure(error);
    }
  }

  async function postNoContent(
    path: string,
    bodyInput: unknown,
    bodySchema: ZodType,
    mutation: ResourceMutationRequestOptionsV2,
  ): Promise<void> {
    const bootstrap = await context();
    const headers = requestHeaders(bootstrap, { mutation, ifMatch: mutation.ifMatch });
    const body = JSON.stringify(bodySchema.parse(bodyInput));
    if (new TextEncoder().encode(body).byteLength > MAX_RESPONSE_BYTES) {
      throw new DesktopContractErrorV2("Desktop Local API request body exceeds the byte limit");
    }
    headers.set("Content-Type", "application/json");
    try {
      await withRequestDeadline(requestTimeoutMs, async (signal) => {
        const response = await options.fetch(buildUrl(bootstrap.endpoint, path), {
          method: "POST",
          headers,
          body,
          credentials: "omit",
          cache: "no-store",
          redirect: "error",
          referrerPolicy: "no-referrer",
          signal,
        });
        if (!response.ok) throw await responseError(response);
        if (response.status !== 204) {
          throw new DesktopContractErrorV2("Desktop Local API action did not return HTTP 204", {
            status: response.status,
          });
        }
        const text = await response.text();
        if (text.length !== 0) {
          throw new DesktopContractErrorV2("Desktop Local API HTTP 204 response contained a body", {
            status: 204,
          });
        }
      });
    } catch (error) {
      if (isTransportFailure(error)
        && bootstrapPromise !== null
        && await promiseResolvedTo(bootstrapPromise, bootstrap)) {
        bootstrapPromise = null;
      }
      throw normalizeTransportFailure(error);
    }
  }

  const resourceAction = <S extends z.ZodTypeAny>(
    path: string,
    body: unknown,
    bodySchema: ZodType,
    responseSchema: S,
    options: ResourceMutationRequestOptionsV2,
  ) => request("POST", path, responseSchema, 202, {
    body,
    bodySchema,
    mutation: options,
    ifMatch: options.ifMatch,
  });

  return {
    version: async () => {
      const bootstrap = await context();
      const raw = await request("GET", "/version", desktopVersionV2Schema, 200, { authenticated: false });
      return negotiateDesktopVersionV2(raw, bootstrap, contract);
    },
    health: () => request("GET", "/health", desktopHealthV2Schema, 200, { authenticated: false }),
    state: () => request("GET", `${DESKTOP_API_V2_PREFIX}/state`, desktopStateV2Schema, 200),
    listSshHosts: () => request("GET", `${DESKTOP_API_V2_PREFIX}/ssh-hosts`, sshHostCatalogV2Schema, 200),
    rescanSshHosts: (input, mutation) => request("POST", `${DESKTOP_API_V2_PREFIX}/ssh-hosts/rescan`, sshHostCatalogV2Schema, 202, {
      body: input,
      bodySchema: sshHostCatalogRescanV2Schema,
      mutation,
    }),
    listProfiles: (listOptions) => request("GET", withListQuery(`${DESKTOP_API_V2_PREFIX}/profiles`, listOptions), remoteProfilePageV2Schema, 200),
    createProfile: async (input, mutation) => request("POST", `${DESKTOP_API_V2_PREFIX}/profiles`, remoteWorkspaceProfileV2Schema, 201, {
      body: input,
      bodySchema: systemOpenSshProfileCreateV2Schema,
      mutation,
    }),
    getProfile: async (profileId) => assertIdentity(
      await request("GET", `${DESKTOP_API_V2_PREFIX}/profiles/${segment(profileId)}`, remoteProfileV2Schema, 200),
      "profile_id",
      profileId,
      "profile lookup",
    ),
    updateProfile: async (profileId, input, mutation) => assertIdentity(
      await request("PATCH", `${DESKTOP_API_V2_PREFIX}/profiles/${segment(profileId)}`, remoteProfileV2Schema, 200, {
        body: input,
        bodySchema: profileDisplayNamePatchV2Schema,
        mutation,
        ifMatch: mutation.ifMatch,
      }),
      "profile_id",
      profileId,
      "profile update",
    ),
    deleteProfile: (profileId, mutation) => noContent(`${DESKTOP_API_V2_PREFIX}/profiles/${segment(profileId)}`, mutation),
    rebindProfile: async (profileId, input, mutation) => assertIdentity(
      await request("POST", `${DESKTOP_API_V2_PREFIX}/profiles/${segment(profileId)}/rebind`, remoteWorkspaceProfileV2Schema, 201, {
        body: input,
        bodySchema: profileRebindV2Schema,
        mutation,
        ifMatch: mutation.ifMatch,
      }),
      "profile_id",
      profileId,
      "profile rebind",
    ),
    connectProfile: (profileId, input, mutation) => resourceAction(
      `${DESKTOP_API_V2_PREFIX}/profiles/${segment(profileId)}/connect`, input, profileConnectionActionV2Schema, lifecycleOperationV2Schema, mutation,
    ),
    disconnectProfile: (profileId, input, mutation) => resourceAction(
      `${DESKTOP_API_V2_PREFIX}/profiles/${segment(profileId)}/disconnect`, input, profileConnectionActionV2Schema, lifecycleOperationV2Schema, mutation,
    ),
    reviewProfileHostKey: (profileId, input, mutation) => resourceAction(
      `${DESKTOP_API_V2_PREFIX}/profiles/${segment(profileId)}/host-key/review`, input, hostKeyReviewRequestV2Schema, lifecycleOperationV2Schema, mutation,
    ),
    listProjects: (listOptions) => request("GET", withListQuery(`${DESKTOP_API_V2_PREFIX}/projects`, listOptions), projectPageV2Schema, 200),
    createProject: (input, mutation) => request("POST", `${DESKTOP_API_V2_PREFIX}/projects`, lifecycleOperationV2Schema, 202, {
      body: input,
      bodySchema: projectCreateV2Schema,
      mutation,
    }),
    getProject: async (projectId) => assertIdentity(
      await request("GET", `${DESKTOP_API_V2_PREFIX}/projects/${segment(projectId)}`, projectV2Schema, 200),
      "project_id",
      projectId,
      "project lookup",
    ),
    updateProject: async (projectId, input, mutation) => assertIdentity(
      await request("PATCH", `${DESKTOP_API_V2_PREFIX}/projects/${segment(projectId)}`, projectV2Schema, 200, {
        body: input,
        bodySchema: projectPatchV2Schema,
        mutation,
        ifMatch: mutation.ifMatch,
      }),
      "project_id",
      projectId,
      "project update",
    ),
    activateProject: (projectId, input, mutation) => resourceAction(
      `${DESKTOP_API_V2_PREFIX}/projects/${segment(projectId)}/activate`, input, projectActionV2Schema, lifecycleOperationV2Schema, mutation,
    ),
    getLifecycleOperationByAction: (actionId, kind) => {
      const action = idempotencyKeyV2Schema.parse(actionId);
      return request(
        "GET",
        `${DESKTOP_API_V2_PREFIX}/operations/by-action?action_id=${encodeURIComponent(action)}&kind=${encodeURIComponent(kind)}`,
        lifecycleOperationV2Schema,
        200,
      );
    },
    getLifecycleOperation: async (operationId) => assertIdentity(
      await request("GET", `${DESKTOP_API_V2_PREFIX}/operations/${segment(operationId)}`, lifecycleOperationV2Schema, 200),
      "operation_id",
      operationId,
      "lifecycle operation lookup",
    ),
    lifecycleOperationLogs: async (operationId, listOptions) => assertIdentity(
      await request(
        "GET",
        withLifecycleLogQuery(
          `${DESKTOP_API_V2_PREFIX}/operations/${segment(operationId)}/logs`,
          listOptions,
        ),
        lifecycleLogPageV2Schema,
        200,
      ),
      "operation_id",
      operationId,
      "lifecycle operation logs",
    ),
    cancelLifecycleOperation: async (operationId, input, mutation) => assertIdentity(
      await resourceAction(
        `${DESKTOP_API_V2_PREFIX}/operations/${segment(operationId)}/cancel`,
        input,
        lifecycleCancelV2Schema,
        lifecycleOperationV2Schema,
        mutation,
      ),
      "operation_id",
      operationId,
      "lifecycle operation cancellation",
    ),
    acknowledgeLifecycleOperation: (operationId, input, mutation) => postNoContent(
      `${DESKTOP_API_V2_PREFIX}/operations/${segment(operationId)}/acknowledge`,
      input,
      lifecycleAcknowledgeV2Schema,
      mutation,
    ),
    projectCapabilities: async (projectId) => assertIdentity(
      await request("GET", `${DESKTOP_API_V2_PREFIX}/projects/${segment(projectId)}/capabilities`, projectCapabilityProjectionV2Schema, 200),
      "project_id",
      projectId,
      "project capabilities",
    ),
    validateProject: async (projectId, input, mutation) => assertIdentity(
      await request("POST", `${DESKTOP_API_V2_PREFIX}/projects/${segment(projectId)}/validate`, projectValidationV2Schema, 200, {
        body: input,
        bodySchema: projectValidationRequestV2Schema,
        mutation,
        ifMatch: mutation.ifMatch,
      }),
      "project_id",
      projectId,
      "project validation",
    ),
    listTasks: (listOptions) => request("GET", withTaskQuery(`${DESKTOP_API_V2_PREFIX}/tasks`, listOptions), taskPageV2Schema, 200),
    submitTask: async (input, mutation) => {
      const parsedInput = taskSubmitRequestV2Schema.parse(input);
      return assertIdentity(
        await request("POST", `${DESKTOP_API_V2_PREFIX}/tasks`, taskV2Schema, 202, {
          body: parsedInput,
          bodySchema: taskSubmitRequestV2Schema,
          mutation,
        }),
        "project_id",
        parsedInput.project_id,
        "task submission",
      );
    },
    getTask: async (taskId) => assertIdentity(
      await request("GET", `${DESKTOP_API_V2_PREFIX}/tasks/${segment(taskId)}`, taskV2Schema, 200),
      "task_id",
      taskId,
      "task lookup",
    ),
    cancelTask: (taskId, input, mutation) => resourceAction(
      `${DESKTOP_API_V2_PREFIX}/tasks/${segment(taskId)}/cancel`, input, taskActionV2Schema, coreOperationV2Schema, mutation,
    ),
    retryTask: (taskId, input, mutation) => resourceAction(
      `${DESKTOP_API_V2_PREFIX}/tasks/${segment(taskId)}/retry`, input, taskActionV2Schema, localOperationV2Schema, mutation,
    ),
    taskTimeline: (taskId, listOptions) => request("GET", withListQuery(`${DESKTOP_API_V2_PREFIX}/tasks/${segment(taskId)}/timeline`, listOptions), timelinePageV2Schema, 200),
    taskLogs: (taskId, listOptions) => request(
      "GET",
      withListQuery(`${DESKTOP_API_V2_PREFIX}/tasks/${segment(taskId)}/logs`, listOptions),
      logPageV2Schema,
      200,
    ),
    taskContext: async (taskId) => assertIdentity(
      await request("GET", `${DESKTOP_API_V2_PREFIX}/tasks/${segment(taskId)}/context`, taskContextV2Schema, 200),
      "task_id",
      taskId,
      "task context",
    ),
    taskArtifacts: (taskId, listOptions) => request("GET", withListQuery(`${DESKTOP_API_V2_PREFIX}/tasks/${segment(taskId)}/artifacts`, listOptions), artifactPageV2Schema, 200),
    getProjectHead: async (projectHeadId) => assertIdentity(
      await request("GET", `${DESKTOP_API_V2_PREFIX}/project-heads/${segment(projectHeadId)}`, projectHeadRefV2Schema, 200),
      "project_head_id",
      projectHeadId,
      "project head lookup",
    ),
    getEvolutionRevision: async (evolutionRevisionId) => assertIdentity(
      await request("GET", `${DESKTOP_API_V2_PREFIX}/evolution-revisions/${segment(evolutionRevisionId)}`, evolutionRevisionRefV2Schema, 200),
      "evolution_revision_id",
      evolutionRevisionId,
      "evolution revision lookup",
    ),
    getRuntimeContext: async (runtimeContextSnapshotId) => assertIdentity(
      await request("GET", `${DESKTOP_API_V2_PREFIX}/runtime-contexts/${segment(runtimeContextSnapshotId)}`, runtimeContextSnapshotRefV2Schema, 200),
      "runtime_context_snapshot_id",
      runtimeContextSnapshotId,
      "runtime context lookup",
    ),
    getTransition: async (transitionId) => {
      const value = await request("GET", `${DESKTOP_API_V2_PREFIX}/transitions/${segment(transitionId)}`, successorTransitionV2Schema, 200);
      if (value.transition.successor_transition_id !== transitionId) throw new DesktopContractErrorV2("transition lookup returned another transition");
      return value;
    },
    retryTransition: (transitionId, input, mutation) => resourceAction(
      `${DESKTOP_API_V2_PREFIX}/transitions/${segment(transitionId)}/retry`, input, transitionActionV2Schema, coreOperationV2Schema, mutation,
    ),
    replaceTransition: (transitionId, input, mutation) => resourceAction(
      `${DESKTOP_API_V2_PREFIX}/transitions/${segment(transitionId)}/replace`, input, transitionReplaceV2Schema, localOperationV2Schema, mutation,
    ),
    abandonTransition: (transitionId, input, mutation) => resourceAction(
      `${DESKTOP_API_V2_PREFIX}/transitions/${segment(transitionId)}/abandon`, input, transitionActionV2Schema, coreOperationV2Schema, mutation,
    ),
    getArtifact: async (artifactId) => assertIdentity(
      await request("GET", `${DESKTOP_API_V2_PREFIX}/artifacts/${segment(artifactId)}`, artifactV2Schema, 200),
      "artifact_id",
      artifactId,
      "artifact lookup",
    ),
    artifactContent: async (artifactId) => {
      const value = await request("GET", `${DESKTOP_API_V2_PREFIX}/artifacts/${segment(artifactId)}/content`, artifactContentV2Schema, 200);
      if (value.artifact.artifact_id !== artifactId) throw new DesktopContractErrorV2("artifact content returned another artifact");
      return value;
    },
    artifactDiff: async (artifactId, diffOptions) => assertIdentity(
      await request("GET", withArtifactDiffQuery(`${DESKTOP_API_V2_PREFIX}/artifacts/${segment(artifactId)}/diff`, diffOptions), artifactDiffV2Schema, 200),
      "artifact_id",
      artifactId,
      "artifact diff",
    ),
    listServices: (listOptions) => request("GET", withListQuery(`${DESKTOP_API_V2_PREFIX}/services`, listOptions), servicePageV2Schema, 200),
    restartService: (serviceId, input, mutation) => resourceAction(
      `${DESKTOP_API_V2_PREFIX}/services/${segment(serviceId)}/restart`, input, serviceRestartV2Schema, coreOperationV2Schema, mutation,
    ),
    getCoreOperation: async (operationId) => assertIdentity(
      await request("GET", `${DESKTOP_API_V2_PREFIX}/core-operations/${segment(operationId)}`, coreOperationV2Schema, 200),
      "operation_id",
      operationId,
      "Core operation lookup",
    ),
    cancelCoreOperation: async (operationId, mutation) => assertIdentity(
      await request(
        "POST",
        `${DESKTOP_API_V2_PREFIX}/core-operations/${segment(operationId)}/cancel`,
        coreOperationV2Schema,
        202,
        { mutation, ifMatch: mutation.ifMatch },
      ),
      "operation_id",
      operationId,
      "Core operation cancellation",
    ),
    serviceLogs: (serviceId, listOptions) => request(
      "GET",
      withListQuery(`${DESKTOP_API_V2_PREFIX}/services/${segment(serviceId)}/logs`, listOptions),
      logPageV2Schema,
      200,
    ),
    cleanupCaches: (input, mutation) => request(
      "POST",
      `${DESKTOP_API_V2_PREFIX}/maintenance/cache-cleanup`,
      coreOperationV2Schema,
      202,
      { body: input, bodySchema: cacheCleanupRequestV2Schema, mutation },
    ),
    createDiagnostic: (input, mutation) => request("POST", `${DESKTOP_API_V2_PREFIX}/diagnostics`, diagnosticV2Schema, 202, {
      body: input,
      bodySchema: diagnosticRequestV2Schema,
      mutation,
    }),
    getDiagnostic: async (diagnosticId) => assertIdentity(
      await request("GET", `${DESKTOP_API_V2_PREFIX}/diagnostics/${segment(diagnosticId)}`, diagnosticV2Schema, 200),
      "diagnostic_id",
      diagnosticId,
      "diagnostic lookup",
    ),
    eventStreamRequest: async (lastEventId) => {
      const bootstrap = await context();
      const headers: Record<string, string> = {
        Accept: "text/event-stream",
        [DESKTOP_SESSION_HEADER]: bootstrap.session_token,
      };
      if (lastEventId !== undefined) headers[LAST_EVENT_ID_HEADER] = opaqueIdV2Schema.parse(lastEventId);
      return { url: buildUrl(bootstrap.endpoint, `${DESKTOP_API_V2_PREFIX}/events`), headers };
    },
  };
}

function parseReleaseContract(input: DesktopReleaseContractV2): DesktopReleaseContractV2 {
  const parsed = releaseContractV2Schema.safeParse(input);
  if (!parsed.success) throw new DesktopContractErrorV2("Desktop v2 release contract is invalid", { cause: parsed.error });
  return {
    releaseVersion: parsed.data.releaseVersion,
    acceptedOpenApiDigests: [parsed.data.acceptedOpenApiDigests[0]!, ...parsed.data.acceptedOpenApiDigests.slice(1)],
    acceptedEventSchemaDigests: [parsed.data.acceptedEventSchemaDigests[0]!, ...parsed.data.acceptedEventSchemaDigests.slice(1)],
    allowedProviderKinds: ["desktop_sidecar"],
    requiredFeatureFlags: [...parsed.data.requiredFeatureFlags],
  };
}

function requestHeaders(
  bootstrap: DesktopBootstrapContextV2,
  options: {
    readonly authenticated?: boolean;
    readonly mutation?: MutationRequestOptionsV2;
    readonly ifMatch?: string;
  },
): Headers {
  const headers = new Headers({ Accept: "application/json" });
  if (options.authenticated !== false) headers.set(DESKTOP_SESSION_HEADER, bootstrap.session_token);
  if (options.mutation !== undefined) {
    headers.set(DESKTOP_RESOURCE_GENERATION_HEADER, String(resourceGenerationV2Schema.parse(options.mutation.resourceGeneration)));
    headers.set(IDEMPOTENCY_KEY_HEADER, idempotencyKeyV2Schema.parse(options.mutation.idempotencyKey));
  }
  if (options.ifMatch !== undefined) headers.set(IF_MATCH_HEADER, etagV2Schema.parse(options.ifMatch));
  return headers;
}

async function withRequestDeadline<T>(timeoutMs: number, operation: (signal: AbortSignal) => Promise<T>): Promise<T> {
  const controller = new AbortController();
  let timeout: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_resolve, reject) => {
    timeout = setTimeout(() => {
      controller.abort();
      reject(new DesktopContractErrorV2("Desktop Local API request timed out"));
    }, timeoutMs);
  });
  try {
    return await Promise.race([operation(controller.signal), deadline]);
  } finally {
    if (timeout !== undefined) clearTimeout(timeout);
  }
}

async function responseError(response: Response): Promise<Error> {
  const payload = await readJsonBounded(response);
  const parsed = desktopErrorV2Schema.safeParse(payload);
  if (!parsed.success) {
    return new DesktopContractErrorV2("Desktop Local API returned an invalid error envelope", {
      cause: parsed.error,
      status: response.status,
    });
  }
  return new DesktopApiErrorV2(response.status, parsed.data);
}

async function readJsonBounded(response: Response): Promise<unknown> {
  const contentType = response.headers.get("Content-Type") ?? "";
  if (!contentType.toLowerCase().includes("application/json")) {
    throw new DesktopContractErrorV2("Desktop Local API response is not JSON", { status: response.status });
  }
  const declaredLength = response.headers.get("Content-Length");
  if (declaredLength !== null) {
    const parsedLength = Number(declaredLength);
    if (!Number.isSafeInteger(parsedLength) || parsedLength < 0 || parsedLength > MAX_RESPONSE_BYTES) {
      throw new DesktopContractErrorV2("Desktop Local API response exceeds the byte limit", { status: response.status });
    }
  }
  let text: string;
  try {
    text = await response.text();
  } catch (error) {
    throw new DesktopContractErrorV2("Desktop Local API response body could not be read", { cause: error, status: response.status });
  }
  if (new TextEncoder().encode(text).byteLength > MAX_RESPONSE_BYTES) {
    throw new DesktopContractErrorV2("Desktop Local API response exceeds the byte limit", { status: response.status });
  }
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new DesktopContractErrorV2("Desktop Local API returned malformed JSON", { cause: error, status: response.status });
  }
}

function parseEnvelope<T>(schema: ZodType<T, z.ZodTypeDef, unknown>, payload: unknown, status: number | null, kind: string): T {
  const parsed = schema.safeParse(payload);
  if (!parsed.success) {
    throw new DesktopContractErrorV2(`Desktop Local API returned an invalid ${kind} envelope`, {
      cause: parsed.error,
      status,
    });
  }
  return parsed.data;
}

function assertRequiredFeatures(actual: readonly string[], required: readonly string[], authority: string): void {
  const features = new Set(actual);
  if (required.some((feature) => !features.has(feature))) {
    throw new DesktopContractErrorV2(`${authority} is missing required v2 release features`);
  }
}

function assertIdentity<T extends Record<K, string>, K extends keyof T>(
  value: T,
  key: K,
  expected: string,
  label: string,
): T {
  if (value[key] !== expected) throw new DesktopContractErrorV2(`${label} returned another resource identity`);
  return value;
}

function segment(value: string): string {
  return encodeURIComponent(opaqueIdV2Schema.parse(value));
}

function buildUrl(endpoint: string, path: string): string {
  return `${endpoint.replace(/\/$/, "")}${path}`;
}

function withListQuery(path: string, input?: ListRequestOptionsV2): string {
  if (input === undefined) return path;
  const options = listRequestOptionsV2Schema.parse(input);
  const query = new URLSearchParams();
  if (options.limit !== undefined) query.set("limit", String(options.limit));
  if (options.after !== undefined) query.set("after", options.after);
  return query.size === 0 ? path : `${path}?${query.toString()}`;
}

function withLifecycleLogQuery(
  path: string,
  input?: LifecycleLogRequestOptionsV2,
): string {
  if (input === undefined) return path;
  const options = lifecycleLogRequestOptionsV2Schema.parse(input);
  const query = new URLSearchParams();
  if (options.limit !== undefined) query.set("limit", String(options.limit));
  if (options.after !== undefined) query.set("after", options.after);
  if (options.afterSequence !== undefined) {
    query.set("after_sequence", String(options.afterSequence));
  }
  return query.size === 0 ? path : `${path}?${query.toString()}`;
}

function withTaskQuery(path: string, input?: TaskListRequestOptionsV2): string {
  if (input === undefined) return path;
  const options = taskListRequestOptionsV2Schema.parse(input);
  const query = new URLSearchParams();
  if (options.limit !== undefined) query.set("limit", String(options.limit));
  if (options.after !== undefined) query.set("after", options.after);
  if (options.projectId !== undefined) query.set("project_id", options.projectId);
  return query.size === 0 ? path : `${path}?${query.toString()}`;
}

function withArtifactDiffQuery(path: string, input?: ArtifactDiffRequestOptionsV2): string {
  if (input === undefined) return path;
  const options = z.object({ previousArtifactId: opaqueIdV2Schema.optional() }).strict().parse(input);
  if (options.previousArtifactId === undefined) return path;
  return `${path}?${new URLSearchParams({ previous_artifact_id: options.previousArtifactId }).toString()}`;
}

function isTransportFailure(error: unknown): boolean {
  return error instanceof TypeError
    || (error instanceof DesktopContractErrorV2 && error.message === "Desktop Local API request timed out");
}

function normalizeTransportFailure(error: unknown): unknown {
  if (error instanceof TypeError) {
    return new DesktopContractErrorV2("Desktop Local API request failed", { cause: error });
  }
  return error;
}

async function promiseResolvedTo<T>(promise: Promise<T>, expected: T): Promise<boolean> {
  try {
    return await promise === expected;
  } catch {
    return false;
  }
}
