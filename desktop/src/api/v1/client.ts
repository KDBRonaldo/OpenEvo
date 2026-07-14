import { z, type ZodType } from "zod";
import {
  apiErrorV1Schema,
  artifactContentV1Schema,
  artifactDiffV1Schema,
  artifactPageV1Schema,
  artifactV1Schema,
  desktopBootstrapContextV1Schema,
  desktopStateV1Schema,
  diagnosticCreateV1Schema,
  diagnosticReportV1Schema,
  etagSchema,
  healthV1Schema,
  hostKeyAcceptV1Schema,
  localOperationV1Schema,
  logPageV1Schema,
  opaqueIdSchema,
  profileCreateV1Schema,
  profilePageV1Schema,
  profilePatchV1Schema,
  projectCapabilitiesV1Schema,
  projectCreateV1Schema,
  projectPageV1Schema,
  projectPatchV1Schema,
  projectV1Schema,
  projectValidateRequestV1Schema,
  projectValidationV1Schema,
  remoteProfileV1Schema,
  runContextV1Schema,
  runCreateV1Schema,
  runPageV1Schema,
  runV1Schema,
  servicePageV1Schema,
  serviceV1Schema,
  timelinePageV1Schema,
  versionInfoV1Schema,
  type ApiErrorV1,
  type ArtifactContentV1,
  type ArtifactDiffV1,
  type ArtifactV1,
  type DesktopBootstrapContextV1,
  type DesktopStateV1,
  type DiagnosticCreateV1,
  type DiagnosticReportV1,
  type HealthV1,
  type HostKeyAcceptV1,
  type LocalOperationV1,
  type LogEntryV1,
  type PageV1,
  type ProfileCreateV1,
  type ProfilePatchV1,
  type ProjectCapabilitiesV1,
  type ProjectCreateV1,
  type ProjectPatchV1,
  type ProjectV1,
  type ProjectValidationV1,
  type ProjectValidateRequestV1,
  type RemoteProfileV1,
  type RunContextV1,
  type RunCreateV1,
  type RunV1,
  type ServiceV1,
  type TimelineEntryV1,
  type VersionInfoV1,
} from "./schemas";

export const DESKTOP_API_V1_PREFIX = "/desktop/v1";
export const DESKTOP_SESSION_HEADER = "X-OpenEvo-Desktop-Session";

export type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
export type BootstrapContextProvider = () => Promise<unknown>;

export interface DesktopClientOptions {
  readonly fetch: FetchLike;
  readonly bootstrap: BootstrapContextProvider;
  readonly supportedMajors?: readonly number[];
  readonly acceptedOpenApiDigests?: readonly string[];
  readonly allowedProviderKinds?: readonly DesktopBootstrapContextV1["negotiated_contract"]["provider_kind"][];
}

export interface CreateRequestOptions {
  readonly idempotencyKey: string;
}

export interface ActionRequestOptions extends CreateRequestOptions {
  readonly ifMatch: string;
}

export interface IfMatchRequestOptions {
  readonly ifMatch: string;
}

export interface ListRequestOptions {
  readonly limit?: number;
  readonly after?: string;
  readonly sort?: string;
  readonly direction?: "asc" | "desc";
}

export interface EventStreamRequest {
  readonly url: string;
  readonly headers: Readonly<Record<string, string>>;
}

const listRequestOptionsSchema = z
  .object({
    limit: z.number().int().min(1).max(100).optional(),
    after: z.string().min(1).max(2048).optional(),
    sort: z.string().regex(/^[a-z][a-z0-9_]{0,63}$/).optional(),
    direction: z.enum(["asc", "desc"]).optional(),
  })
  .strict();

const idempotencyKeySchema = z
  .string()
  .trim()
  .min(16)
  .max(256)
  .refine((value) => !/[\u0000-\u001f\u007f]/.test(value));
const ifMatchSchema = etagSchema;

export class DesktopApiError extends Error {
  readonly apiError: ApiErrorV1;
  readonly status: number;

  constructor(apiError: ApiErrorV1) {
    super(apiError.message);
    this.name = "DesktopApiError";
    this.apiError = apiError;
    this.status = apiError.http_status;
  }
}

export class DesktopContractError extends Error {
  readonly cause: unknown;
  readonly status: number | null;

  constructor(message: string, options: { cause?: unknown; status?: number | null } = {}) {
    super(message);
    this.name = "DesktopContractError";
    this.cause = options.cause;
    this.status = options.status ?? null;
  }
}

export class ContractVersionUnsupportedError extends Error {
  readonly status = 426;
  readonly clientSupportedMajors: readonly number[];
  readonly serverSupportedMajors: readonly number[];

  constructor(clientSupportedMajors: readonly number[], serverSupportedMajors: readonly number[]) {
    super("Desktop Local API contract version is unsupported");
    this.name = "ContractVersionUnsupportedError";
    this.clientSupportedMajors = clientSupportedMajors;
    this.serverSupportedMajors = serverSupportedMajors;
  }
}

export interface NegotiatedVersion {
  readonly major: number;
  readonly openapiSha256: string;
  readonly server: VersionInfoV1;
}

export function negotiateVersion(
  serverInput: unknown,
  clientSupportedMajors: readonly number[] = [1],
  acceptedOpenApiDigests?: readonly string[],
): NegotiatedVersion {
  const discovery = z
    .object({
      preferred_major: z.number().int().positive(),
      supported_majors: z.array(z.number().int().positive()).min(1),
    })
    .passthrough()
    .parse(serverInput);
  const common = discovery.supported_majors
    .filter((major) => clientSupportedMajors.includes(major))
    .sort((left, right) => right - left);
  const major = common[0];
  if (major === undefined) {
    throw new ContractVersionUnsupportedError(clientSupportedMajors, discovery.supported_majors);
  }
  const server = versionInfoV1Schema.parse(serverInput);
  if (acceptedOpenApiDigests && !acceptedOpenApiDigests.includes(server.openapi_sha256)) {
    throw new DesktopContractError("Desktop Local API reported an unknown OpenAPI digest");
  }
  return { major, openapiSha256: server.openapi_sha256, server };
}

export interface DesktopApiClientV1 {
  version(): Promise<VersionInfoV1>;
  health(): Promise<HealthV1>;
  state(): Promise<DesktopStateV1>;
  listProfiles(options?: ListRequestOptions): Promise<PageV1<RemoteProfileV1>>;
  createProfile(input: ProfileCreateV1, options: CreateRequestOptions): Promise<RemoteProfileV1>;
  getProfile(profileId: string): Promise<RemoteProfileV1>;
  updateProfile(profileId: string, input: ProfilePatchV1, options: IfMatchRequestOptions): Promise<RemoteProfileV1>;
  deleteProfile(profileId: string, options: IfMatchRequestOptions): Promise<void>;
  connectProfile(profileId: string, options: ActionRequestOptions): Promise<LocalOperationV1>;
  disconnectProfile(profileId: string, options: ActionRequestOptions): Promise<LocalOperationV1>;
  acceptProfileHostKey(profileId: string, input: HostKeyAcceptV1, options: ActionRequestOptions): Promise<LocalOperationV1>;
  listProjects(options?: ListRequestOptions): Promise<PageV1<ProjectV1>>;
  createProject(input: ProjectCreateV1, options: CreateRequestOptions): Promise<ProjectV1>;
  getProject(projectId: string): Promise<ProjectV1>;
  updateProject(projectId: string, input: ProjectPatchV1, options: IfMatchRequestOptions): Promise<ProjectV1>;
  deleteProject(projectId: string, options: IfMatchRequestOptions): Promise<void>;
  activateProject(projectId: string, options: ActionRequestOptions): Promise<LocalOperationV1>;
  doctorProject(projectId: string, options: ActionRequestOptions): Promise<LocalOperationV1>;
  repairProject(projectId: string, options: ActionRequestOptions): Promise<LocalOperationV1>;
  bootstrapProject(projectId: string, options: ActionRequestOptions): Promise<LocalOperationV1>;
  syncProjectWorkspace(projectId: string, options: ActionRequestOptions): Promise<LocalOperationV1>;
  projectCapabilities(projectId: string): Promise<ProjectCapabilitiesV1>;
  validateProject(projectId: string, input: ProjectValidateRequestV1, options: ActionRequestOptions): Promise<ProjectValidationV1>;
  getOperation(operationId: string): Promise<LocalOperationV1>;
  operationLogs(operationId: string, options?: ListRequestOptions): Promise<PageV1<LogEntryV1>>;
  cancelOperation(operationId: string, options: ActionRequestOptions): Promise<LocalOperationV1>;
  listRuns(options?: ListRequestOptions): Promise<PageV1<RunV1>>;
  createRun(input: RunCreateV1, options: CreateRequestOptions): Promise<RunV1>;
  getRun(runId: string): Promise<RunV1>;
  deleteRun(runId: string, options: IfMatchRequestOptions): Promise<void>;
  cancelRun(runId: string, options: ActionRequestOptions): Promise<RunV1>;
  retryRun(runId: string, options: ActionRequestOptions): Promise<RunV1>;
  runTimeline(runId: string, options?: ListRequestOptions): Promise<PageV1<TimelineEntryV1>>;
  runLogs(runId: string, options?: ListRequestOptions): Promise<PageV1<LogEntryV1>>;
  runContext(runId: string): Promise<RunContextV1>;
  runArtifacts(runId: string, options?: ListRequestOptions): Promise<PageV1<ArtifactV1>>;
  getArtifact(artifactId: string): Promise<ArtifactV1>;
  artifactContent(artifactId: string): Promise<ArtifactContentV1>;
  artifactDiff(artifactId: string): Promise<ArtifactDiffV1>;
  listServices(options?: ListRequestOptions): Promise<PageV1<ServiceV1>>;
  restartService(serviceId: string, options: ActionRequestOptions): Promise<LocalOperationV1>;
  stopService(serviceId: string, options: ActionRequestOptions): Promise<LocalOperationV1>;
  serviceLogs(serviceId: string, options?: ListRequestOptions): Promise<PageV1<LogEntryV1>>;
  createDiagnostic(input: DiagnosticCreateV1, options: CreateRequestOptions): Promise<LocalOperationV1>;
  getDiagnostic(diagnosticId: string): Promise<DiagnosticReportV1>;
  deleteDiagnostic(diagnosticId: string, options: IfMatchRequestOptions): Promise<void>;
  cleanupMaintenanceCache(options: CreateRequestOptions): Promise<LocalOperationV1>;
  eventStreamRequest(lastEventId?: string): Promise<EventStreamRequest>;
}

export function createDesktopApiClient(options: DesktopClientOptions): DesktopApiClientV1 {
  const supportedMajors = options.supportedMajors ?? [1];
  const allowedProviderKinds = options.allowedProviderKinds ?? ["desktop_sidecar"];
  let bootstrapPromise: Promise<DesktopBootstrapContextV1> | null = null;

  async function context(): Promise<DesktopBootstrapContextV1> {
    if (!bootstrapPromise) {
      bootstrapPromise = options
        .bootstrap()
        .then((input) => desktopBootstrapContextV1Schema.parse(input))
        .then((bootstrap) => {
          if (!supportedMajors.includes(bootstrap.negotiated_contract.major)) {
            throw new ContractVersionUnsupportedError(supportedMajors, [bootstrap.negotiated_contract.major]);
          }
          if (
            options.acceptedOpenApiDigests &&
            !options.acceptedOpenApiDigests.includes(bootstrap.negotiated_contract.openapi_sha256)
          ) {
            throw new DesktopContractError("Tauri bootstrap reported an unknown OpenAPI digest");
          }
          if (!allowedProviderKinds.includes(bootstrap.negotiated_contract.provider_kind)) {
            throw new DesktopContractError("Tauri bootstrap reported a forbidden provider kind");
          }
          return bootstrap;
        })
        .catch((error) => {
          bootstrapPromise = null;
          throw error;
        });
    }
    return bootstrapPromise;
  }

  async function request<S extends z.ZodTypeAny>(
    method: string,
    path: string,
    responseSchema: S,
    requestOptions: {
      body?: unknown;
      bodySchema?: ZodType;
      idempotencyKey?: string;
      ifMatch?: string;
      authenticated?: boolean;
    } = {},
  ): Promise<z.output<S>> {
    const bootstrap = await context();
    const headers = new Headers({ Accept: "application/json" });
    if (requestOptions.authenticated !== false) {
      headers.set(DESKTOP_SESSION_HEADER, bootstrap.session_token);
    }
    if (requestOptions.idempotencyKey !== undefined) {
      headers.set("Idempotency-Key", idempotencyKeySchema.parse(requestOptions.idempotencyKey));
    }
    if (requestOptions.ifMatch !== undefined) {
      headers.set("If-Match", ifMatchSchema.parse(requestOptions.ifMatch));
    }
    let body: string | undefined;
    if (requestOptions.body !== undefined) {
      const parsedBody = requestOptions.bodySchema?.parse(requestOptions.body) ?? requestOptions.body;
      body = JSON.stringify(parsedBody);
      headers.set("Content-Type", "application/json");
    }

    const response = await options.fetch(buildUrl(bootstrap.endpoint, path), {
      method,
      headers,
      body,
      credentials: "omit",
      cache: "no-store",
    });
    if (!response.ok) {
      const payload = await readJson(response);
      const parsedError = parseResponse(apiErrorV1Schema, payload, response.status, "error");
      if (parsedError.http_status !== response.status) {
        throw new DesktopContractError("ApiError HTTP status does not match the response", {
          status: response.status,
        });
      }
      throw new DesktopApiError(parsedError);
    }
    const payload = await readJson(response);
    return parseResponse(responseSchema, payload, response.status, "success");
  }

  async function requestNoContent(
    method: string,
    path: string,
    requestOptions: { ifMatch: string },
  ): Promise<void> {
    const bootstrap = await context();
    const headers = new Headers({ Accept: "application/json" });
    headers.set(DESKTOP_SESSION_HEADER, bootstrap.session_token);
    headers.set("If-Match", ifMatchSchema.parse(requestOptions.ifMatch));
    const response = await options.fetch(buildUrl(bootstrap.endpoint, path), {
      method,
      headers,
      credentials: "omit",
      cache: "no-store",
    });
    if (!response.ok) {
      const payload = await readJson(response);
      const parsedError = parseResponse(apiErrorV1Schema, payload, response.status, "error");
      if (parsedError.http_status !== response.status) {
        throw new DesktopContractError("ApiError HTTP status does not match the response", { status: response.status });
      }
      throw new DesktopApiError(parsedError);
    }
    if (response.status !== 204) {
      throw new DesktopContractError("Desktop Local API delete did not return HTTP 204", { status: response.status });
    }
  }

  const action = (
    path: string,
    responseSchema: z.ZodType<LocalOperationV1, z.ZodTypeDef, any>,
    actionOptions: ActionRequestOptions,
  ) =>
      request("POST", path, responseSchema, {
      idempotencyKey: actionOptions.idempotencyKey,
      ifMatch: actionOptions.ifMatch,
    });

  return {
    version: () => request("GET", "/version", versionInfoV1Schema, { authenticated: false }),
    health: () => request("GET", "/health", healthV1Schema, { authenticated: false }),
    state: () => request("GET", `${DESKTOP_API_V1_PREFIX}/state`, desktopStateV1Schema),
    listProfiles: (listOptions) =>
      request("GET", withQuery(`${DESKTOP_API_V1_PREFIX}/profiles`, listOptions), profilePageV1Schema),
    createProfile: (input, createOptions) =>
      request("POST", `${DESKTOP_API_V1_PREFIX}/profiles`, remoteProfileV1Schema, {
        body: input,
        bodySchema: profileCreateV1Schema,
        idempotencyKey: createOptions.idempotencyKey,
      }),
    getProfile: (profileId) =>
      request("GET", `${DESKTOP_API_V1_PREFIX}/profiles/${segment(profileId)}`, remoteProfileV1Schema),
    updateProfile: (profileId, input, actionOptions) =>
      request("PATCH", `${DESKTOP_API_V1_PREFIX}/profiles/${segment(profileId)}`, remoteProfileV1Schema, {
        body: input,
        bodySchema: profilePatchV1Schema,
        ifMatch: actionOptions.ifMatch,
      }),
    deleteProfile: (profileId, actionOptions) =>
      requestNoContent("DELETE", `${DESKTOP_API_V1_PREFIX}/profiles/${segment(profileId)}`, actionOptions),
    connectProfile: (profileId, actionOptions) =>
      action(`${DESKTOP_API_V1_PREFIX}/profiles/${segment(profileId)}/connect`, localOperationV1Schema, actionOptions),
    disconnectProfile: (profileId, actionOptions) =>
      action(`${DESKTOP_API_V1_PREFIX}/profiles/${segment(profileId)}/disconnect`, localOperationV1Schema, actionOptions),
    acceptProfileHostKey: (profileId, input, actionOptions) =>
      request("POST", `${DESKTOP_API_V1_PREFIX}/profiles/${segment(profileId)}/host-key/accept`, localOperationV1Schema, {
        body: input,
        bodySchema: hostKeyAcceptV1Schema,
        idempotencyKey: actionOptions.idempotencyKey,
        ifMatch: actionOptions.ifMatch,
      }),
    listProjects: (listOptions) =>
      request("GET", withQuery(`${DESKTOP_API_V1_PREFIX}/projects`, listOptions), projectPageV1Schema),
    createProject: (input, createOptions) =>
      request("POST", `${DESKTOP_API_V1_PREFIX}/projects`, projectV1Schema, {
        body: input,
        bodySchema: projectCreateV1Schema,
        idempotencyKey: createOptions.idempotencyKey,
      }),
    getProject: (projectId) =>
      request("GET", `${DESKTOP_API_V1_PREFIX}/projects/${segment(projectId)}`, projectV1Schema),
    updateProject: (projectId, input, actionOptions) =>
      request("PATCH", `${DESKTOP_API_V1_PREFIX}/projects/${segment(projectId)}`, projectV1Schema, {
        body: input,
        bodySchema: projectPatchV1Schema,
        ifMatch: actionOptions.ifMatch,
      }),
    deleteProject: (projectId, actionOptions) =>
      requestNoContent("DELETE", `${DESKTOP_API_V1_PREFIX}/projects/${segment(projectId)}`, actionOptions),
    activateProject: (projectId, actionOptions) =>
      action(`${DESKTOP_API_V1_PREFIX}/projects/${segment(projectId)}/activate`, localOperationV1Schema, actionOptions),
    doctorProject: (projectId, actionOptions) =>
      action(`${DESKTOP_API_V1_PREFIX}/projects/${segment(projectId)}/doctor`, localOperationV1Schema, actionOptions),
    repairProject: (projectId, actionOptions) =>
      action(`${DESKTOP_API_V1_PREFIX}/projects/${segment(projectId)}/repair`, localOperationV1Schema, actionOptions),
    bootstrapProject: (projectId, actionOptions) =>
      action(`${DESKTOP_API_V1_PREFIX}/projects/${segment(projectId)}/bootstrap`, localOperationV1Schema, actionOptions),
    syncProjectWorkspace: (projectId, actionOptions) =>
      action(`${DESKTOP_API_V1_PREFIX}/projects/${segment(projectId)}/workspace-sync`, localOperationV1Schema, actionOptions),
    projectCapabilities: (projectId) =>
      request("GET", `${DESKTOP_API_V1_PREFIX}/projects/${segment(projectId)}/capabilities`, projectCapabilitiesV1Schema),
    validateProject: (projectId, input, actionOptions) =>
      request("POST", `${DESKTOP_API_V1_PREFIX}/projects/${segment(projectId)}/validate`, projectValidationV1Schema, {
        body: input,
        bodySchema: projectValidateRequestV1Schema,
        idempotencyKey: actionOptions.idempotencyKey,
        ifMatch: actionOptions.ifMatch,
      }),
    getOperation: (operationId) =>
      request("GET", `${DESKTOP_API_V1_PREFIX}/operations/${segment(operationId)}`, localOperationV1Schema),
    operationLogs: (operationId, listOptions) =>
      request("GET", withQuery(`${DESKTOP_API_V1_PREFIX}/operations/${segment(operationId)}/logs`, listOptions), logPageV1Schema),
    cancelOperation: (operationId, actionOptions) =>
      action(`${DESKTOP_API_V1_PREFIX}/operations/${segment(operationId)}/cancel`, localOperationV1Schema, actionOptions),
    listRuns: (listOptions) =>
      request("GET", withQuery(`${DESKTOP_API_V1_PREFIX}/runs`, listOptions), runPageV1Schema),
    createRun: (input, createOptions) =>
      request("POST", `${DESKTOP_API_V1_PREFIX}/runs`, runV1Schema, {
        body: input,
        bodySchema: runCreateV1Schema,
        idempotencyKey: createOptions.idempotencyKey,
      }),
    getRun: (runId) => request("GET", `${DESKTOP_API_V1_PREFIX}/runs/${segment(runId)}`, runV1Schema),
    deleteRun: (runId, actionOptions) =>
      requestNoContent("DELETE", `${DESKTOP_API_V1_PREFIX}/runs/${segment(runId)}`, actionOptions),
    cancelRun: (runId, actionOptions) =>
      request("POST", `${DESKTOP_API_V1_PREFIX}/runs/${segment(runId)}/cancel`, runV1Schema, {
        idempotencyKey: actionOptions.idempotencyKey,
        ifMatch: actionOptions.ifMatch,
      }),
    retryRun: (runId, actionOptions) =>
      request("POST", `${DESKTOP_API_V1_PREFIX}/runs/${segment(runId)}/retry`, runV1Schema, {
        idempotencyKey: actionOptions.idempotencyKey,
        ifMatch: actionOptions.ifMatch,
      }),
    runTimeline: (runId, listOptions) =>
      request("GET", withQuery(`${DESKTOP_API_V1_PREFIX}/runs/${segment(runId)}/timeline`, listOptions), timelinePageV1Schema),
    runLogs: (runId, listOptions) =>
      request("GET", withQuery(`${DESKTOP_API_V1_PREFIX}/runs/${segment(runId)}/logs`, listOptions), logPageV1Schema),
    runContext: (runId) =>
      request("GET", `${DESKTOP_API_V1_PREFIX}/runs/${segment(runId)}/context`, runContextV1Schema),
    runArtifacts: (runId, listOptions) =>
      request("GET", withQuery(`${DESKTOP_API_V1_PREFIX}/runs/${segment(runId)}/artifacts`, listOptions), artifactPageV1Schema),
    getArtifact: (artifactId) =>
      request("GET", `${DESKTOP_API_V1_PREFIX}/artifacts/${segment(artifactId)}`, artifactV1Schema),
    artifactContent: (artifactId) =>
      request("GET", `${DESKTOP_API_V1_PREFIX}/artifacts/${segment(artifactId)}/content`, artifactContentV1Schema),
    artifactDiff: (artifactId) =>
      request("GET", `${DESKTOP_API_V1_PREFIX}/artifacts/${segment(artifactId)}/diff`, artifactDiffV1Schema),
    listServices: (listOptions) =>
      request("GET", withQuery(`${DESKTOP_API_V1_PREFIX}/services`, listOptions), servicePageV1Schema),
    restartService: (serviceId, actionOptions) =>
      action(`${DESKTOP_API_V1_PREFIX}/services/${segment(serviceId)}/restart`, localOperationV1Schema, actionOptions),
    stopService: (serviceId, actionOptions) =>
      action(`${DESKTOP_API_V1_PREFIX}/services/${segment(serviceId)}/stop`, localOperationV1Schema, actionOptions),
    serviceLogs: (serviceId, listOptions) =>
      request("GET", withQuery(`${DESKTOP_API_V1_PREFIX}/services/${segment(serviceId)}/logs`, listOptions), logPageV1Schema),
    createDiagnostic: (input, createOptions) =>
      request("POST", `${DESKTOP_API_V1_PREFIX}/diagnostics`, localOperationV1Schema, {
        body: input,
        bodySchema: diagnosticCreateV1Schema,
        idempotencyKey: createOptions.idempotencyKey,
      }),
    getDiagnostic: (diagnosticId) =>
      request("GET", `${DESKTOP_API_V1_PREFIX}/diagnostics/${segment(diagnosticId)}`, diagnosticReportV1Schema),
    deleteDiagnostic: (diagnosticId, actionOptions) =>
      requestNoContent("DELETE", `${DESKTOP_API_V1_PREFIX}/diagnostics/${segment(diagnosticId)}`, actionOptions),
    cleanupMaintenanceCache: (createOptions) =>
      request("POST", `${DESKTOP_API_V1_PREFIX}/maintenance/cache-cleanup`, localOperationV1Schema, {
        idempotencyKey: createOptions.idempotencyKey,
      }),
    eventStreamRequest: async (lastEventId) => {
      const bootstrap = await context();
      const headers: Record<string, string> = {
        Accept: "text/event-stream",
        [DESKTOP_SESSION_HEADER]: bootstrap.session_token,
      };
      if (lastEventId !== undefined) {
        headers["Last-Event-ID"] = opaqueIdSchema.parse(lastEventId);
      }
      return {
        url: buildUrl(bootstrap.endpoint, `${DESKTOP_API_V1_PREFIX}/events`),
        headers,
      };
    },
  };
}

async function readJson(response: Response): Promise<unknown> {
  const contentType = response.headers.get("Content-Type") ?? "";
  if (!contentType.toLowerCase().includes("application/json")) {
    throw new DesktopContractError("Desktop Local API response is not JSON", { status: response.status });
  }
  try {
    return await response.json();
  } catch (error) {
    throw new DesktopContractError("Desktop Local API returned malformed JSON", {
      cause: error,
      status: response.status,
    });
  }
}

function parseResponse<T>(schema: ZodType<T, z.ZodTypeDef, any>, payload: unknown, status: number, kind: string): T {
  const parsed = schema.safeParse(payload);
  if (!parsed.success) {
    throw new DesktopContractError(`Desktop Local API returned an invalid ${kind} envelope`, {
      cause: parsed.error,
      status,
    });
  }
  return parsed.data;
}

function segment(value: string): string {
  const parsed = z.string().min(1).max(256).refine((item) => !/[\u0000-\u001f\u007f]/.test(item)).parse(value);
  return encodeURIComponent(parsed);
}

function buildUrl(endpoint: string, path: string): string {
  return `${endpoint.replace(/\/$/, "")}${path}`;
}

function withQuery(path: string, input?: ListRequestOptions): string {
  if (!input) return path;
  const options = listRequestOptionsSchema.parse(input);
  const query = new URLSearchParams();
  if (options.limit !== undefined) query.set("limit", String(options.limit));
  if (options.after !== undefined) query.set("after", options.after);
  if (options.sort !== undefined) query.set("sort", options.sort);
  if (options.direction !== undefined) query.set("direction", options.direction);
  const serialized = query.toString();
  return serialized ? `${path}?${serialized}` : path;
}
