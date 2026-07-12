import { api } from "./client";
import type {
  EvolutionStepState,
  OpenEvoJsonObject,
  OpenEvoExecutionModePayload,
  OpenEvoDesktopShellModel,
  OpenEvoProjectConfigDraftPayload,
  OpenEvoProjectConfigPayload,
  RemoteServiceState,
} from "../routes/openevoDesktopModel";
import {
  copyEvolutionTargets,
  normalizeOpenEvoExecutionMode,
  toProjectConfigPayload,
} from "../routes/openevoDesktopModel";
import { artifactPreview, timelineView } from "./evolutionViewModel";

export type OpenEvoExecutionMode =
  | "codex_subscription_transcript"
  | "self-deployed";

export type OpenEvoCapabilitySupportState =
  | "supported"
  | "unsupported"
  | "unavailable";

export interface OpenEvoCapabilityAxisSupportPayload {
  state: OpenEvoCapabilitySupportState;
  reason_code: string | null;
  message: string;
  missing_requirements: string[];
}

export interface OpenEvoCapabilityMethodSupportPayload {
  overall: OpenEvoCapabilitySupportState;
  execution: OpenEvoCapabilityAxisSupportPayload;
  capture: OpenEvoCapabilityAxisSupportPayload;
  harness: OpenEvoCapabilityAxisSupportPayload;
  runtime: OpenEvoCapabilityAxisSupportPayload;
}

export interface OpenEvoCapabilityInputBindingPayload {
  binding_id: string;
  source:
    | "current_dataset"
    | "history_datasets"
    | "current_target_artifacts"
    | "explicit_inputs";
  artifact_type: string;
  min_count: number;
  max_count: number | null;
}

export interface EvolutionMethodCapabilityPayload {
  method_id: string;
  display_name: string;
  description: string;
  exposure: "desktop" | "maintainer" | "internal";
  maturity: "stable" | "experimental";
  execution_modes: Array<"subscription" | "self_deployed">;
  capture_modes: Array<"transcript" | "token_level">;
  supported_harness_ids: string[];
  harness_requirements: string[];
  runtime_requirements: string[];
  input_bindings: OpenEvoCapabilityInputBindingPayload[];
  output_artifact_types: string[];
  config_schema_json: string;
  default_config_json: string;
  implementation_identity_digest: string;
  support: OpenEvoCapabilityMethodSupportPayload;
}

export interface OpenEvoResolvedMethodCapabilityPayload {
  method_id: string;
  implementation_identity_digest: string;
  support: OpenEvoCapabilityMethodSupportPayload;
}

export interface OpenEvoSelectionResolverCapabilityPayload {
  selection_value: string;
  display_name: string;
  description: string;
  resolved_methods: OpenEvoResolvedMethodCapabilityPayload[];
}

export interface OpenEvoArtifactTargetCapabilityPayload {
  target_id: string;
  display_name: string;
  description: string;
  artifact_type: string;
  exposure: "desktop" | "maintainer" | "internal";
  maturity: "stable" | "experimental";
  handler_id: string;
  configured_default_method_id: string;
  effective_default_method_id: string | null;
  configured_default_support: OpenEvoCapabilityMethodSupportPayload;
  renderer_kind: "markdown" | "file_bundle" | "structured_summary" | "adapter";
  renderer_contract_version: string;
  contribution_contract_version: string;
  context_order: number;
  implementation_identity_digest: string;
  handler_identity_digest: string;
  accepted_methods: OpenEvoResolvedMethodCapabilityPayload[];
  selection_resolvers: OpenEvoSelectionResolverCapabilityPayload[];
  methods: EvolutionMethodCapabilityPayload[];
}

export interface OpenEvoDesktopShellStatusPayload {
  remote: {
    id: string;
    host: string;
    port: number;
    user: string;
    auth: {
      method: OpenEvoProjectConfigDraft["auth_method"];
      private_key_path: string | null;
      password_ref: string | null;
      passphrase_ref: string | null;
    };
    workspace_root: string;
    proxy: {
      http_proxy: string | null;
      https_proxy: string | null;
      no_proxy: string | null;
      pip_index_url: string | null;
      huggingface_endpoint: string | null;
      hf_home: string | null;
    };
  };
  project: {
    name: string;
    task_id: string;
    source: string;
    objective: string;
    evolution_targets: OpenEvoProjectConfigPayload["evolution"]["targets"];
  };
  execution: {
    mode: OpenEvoExecutionModePayload;
    model: string;
    token_metrics_available: boolean;
  };
  bootstrap: {
    ready: boolean;
    state_root: string;
    workspace_root: string;
    readiness_notes: string[];
  };
  services: Array<{
    id: string;
    label: string;
    state: RemoteServiceState;
    detail: string;
  }>;
  evolution: Array<{
    id: string;
    label: string;
    state: EvolutionStepState;
    detail: string;
  }>;
  developer_mode: {
    enabled: boolean;
    benchmark_controls_visible: boolean;
  };
  sidecar?: {
    mutation_token: string | null;
    transport?: {
      id: OpenEvoDesktopShellModel["sidecar"]["transport"]["id"];
      label: string;
      supports_password_ref: boolean;
      supports_passphrase_ref: boolean;
    };
  };
}

export interface OpenEvoBootstrapResponsePayload {
  bootstrap: OpenEvoDesktopShellStatusPayload["bootstrap"];
  report: Record<string, any>;
  status: OpenEvoDesktopShellStatusPayload;
}

export interface OpenEvoWorkspaceResponsePayload {
  workspace: Record<string, any>;
  report: Record<string, any>;
  status: OpenEvoDesktopShellStatusPayload;
}

export interface OpenEvoServicesResponsePayload {
  services: Record<string, any>;
  report: Record<string, any>;
  status: OpenEvoDesktopShellStatusPayload;
}

export type OpenEvoProjectConfigDraft = OpenEvoProjectConfigDraftPayload;

export type { OpenEvoProjectConfigPayload };

export interface OpenEvoProjectConfigPaths {
  science_config_path: string;
  remote_profile_path: string;
}

export interface OpenEvoProjectConfigResponsePayload {
  config: OpenEvoProjectConfigPaths;
  status: OpenEvoDesktopShellStatusPayload;
}

export interface OpenEvoSavedProjectConfigPayload {
  project_slug: string;
  valid: boolean;
  error: string | null;
  project_name: string | null;
  task_id: string | null;
  objective: string | null;
  source_type: string | null;
  source_label: string | null;
  remote_profile_id: string | null;
  remote_host: string | null;
  remote_user: string | null;
  science_config_path: string;
  remote_profile_path: string | null;
}

export interface OpenEvoProjectConfigsResponsePayload {
  configs: OpenEvoSavedProjectConfigPayload[];
}

export interface OpenEvoBootstrapResponse {
  bootstrap: OpenEvoDesktopShellModel["bootstrap"];
  report: Record<string, any>;
  status: OpenEvoDesktopShellModel;
}

export interface OpenEvoWorkspaceResponse {
  workspace: Record<string, any>;
  report: Record<string, any>;
  status: OpenEvoDesktopShellModel;
}

export interface OpenEvoServicesResponse {
  services: Record<string, any>;
  report: Record<string, any>;
  status: OpenEvoDesktopShellModel;
}

export interface OpenEvoRunStatusPayload {
  id: string;
  state: "running" | "succeeded" | "failed";
  ready: boolean;
  command: string;
  return_code: number | null;
  stdout: string;
  stderr: string;
  output_dir: string;
  experiment_snapshot: string;
  started_at: string;
  finished_at: string | null;
}

export interface OpenEvoRunStatus {
  id: string;
  state: "running" | "succeeded" | "failed";
  ready: boolean;
  command: string;
  returnCode: number | null;
  stdout: string;
  stderr: string;
  outputDir: string;
  experimentSnapshot: string;
  startedAt: string;
  finishedAt: string | null;
}

export interface OpenEvoRunResponsePayload {
  run: OpenEvoRunStatusPayload;
  status: OpenEvoDesktopShellStatusPayload;
}

export interface OpenEvoRunResponse {
  run: OpenEvoRunStatus;
  status: OpenEvoDesktopShellModel;
}

export interface OpenEvoRunArtifactJobPayload {
  artifact_type: string;
  method: string;
  worker_status: string;
  artifact_ids: string[];
  approved_artifact_ids: string[];
  promotion_status: string;
}

export interface OpenEvoRunArtifactRoundPayload {
  round_index: number;
  policy_version: string | null;
  rollout_status: string | null;
  dataset_status: string | null;
  artifact_ids: Record<string, string[]>;
  jobs: OpenEvoRunArtifactJobPayload[];
}

export interface OpenEvoRunArtifactTaskPayload {
  task_id: string;
  rounds: OpenEvoRunArtifactRoundPayload[];
}

export interface OpenEvoRunArtifactsPayload {
  run_id: string;
  output_dir: string;
  summary_status: string | null;
  experiment_id: string | null;
  experiment_name: string | null;
  round_count: number | null;
  tasks: OpenEvoRunArtifactTaskPayload[];
}

export interface OpenEvoDesktopCapabilitiesPayload {
  schema_version: "1";
  core_version: string;
  registry_digest: string;
  evaluated_profile: {
    execution_mode: "subscription" | "self_deployed";
    capture_mode: "transcript" | "token_level";
    harness_id: string;
    harness_capabilities: string[];
    runtime_capabilities: string[];
  };
  targets: OpenEvoArtifactTargetCapabilityPayload[];
}

export interface OpenEvoCapabilityAxisSupport {
  state: OpenEvoCapabilitySupportState;
  reasonCode: string | null;
  message: string;
  missingRequirements: string[];
}

export interface OpenEvoCapabilityMethodSupport {
  overall: OpenEvoCapabilitySupportState;
  execution: OpenEvoCapabilityAxisSupport;
  capture: OpenEvoCapabilityAxisSupport;
  harness: OpenEvoCapabilityAxisSupport;
  runtime: OpenEvoCapabilityAxisSupport;
}

export interface OpenEvoEvolutionMethodCapability {
  methodId: string;
  displayName: string;
  description: string;
  exposure: EvolutionMethodCapabilityPayload["exposure"];
  maturity: EvolutionMethodCapabilityPayload["maturity"];
  executionModes: EvolutionMethodCapabilityPayload["execution_modes"];
  captureModes: EvolutionMethodCapabilityPayload["capture_modes"];
  supportedHarnessIds: string[];
  harnessRequirements: string[];
  runtimeRequirements: string[];
  inputBindings: Array<{
    bindingId: string;
    source: OpenEvoCapabilityInputBindingPayload["source"];
    artifactType: string;
    minCount: number;
    maxCount: number | null;
  }>;
  outputArtifactTypes: string[];
  configSchemaJson: string;
  defaultConfigJson: string;
  configSchema: OpenEvoJsonObject;
  defaultConfig: OpenEvoJsonObject;
  implementationIdentityDigest: string;
  support: OpenEvoCapabilityMethodSupport;
}

export interface OpenEvoEvolutionTargetCapability {
  targetId: string;
  displayName: string;
  description: string;
  artifactType: string;
  exposure: OpenEvoArtifactTargetCapabilityPayload["exposure"];
  maturity: OpenEvoArtifactTargetCapabilityPayload["maturity"];
  handlerId: string;
  configuredDefaultMethodId: string;
  effectiveDefaultMethodId: string | null;
  configuredDefaultSupport: OpenEvoCapabilityMethodSupport;
  rendererKind: OpenEvoArtifactTargetCapabilityPayload["renderer_kind"];
  rendererContractVersion: string;
  contributionContractVersion: string;
  contextOrder: number;
  implementationIdentityDigest: string;
  handlerIdentityDigest: string;
  acceptedMethods: Array<{
    methodId: string;
    implementationIdentityDigest: string;
    support: OpenEvoCapabilityMethodSupport;
  }>;
  selectionResolvers: Array<{
    selectionValue: string;
    displayName: string;
    description: string;
    resolvedMethods: Array<{
      methodId: string;
      implementationIdentityDigest: string;
      support: OpenEvoCapabilityMethodSupport;
    }>;
  }>;
  methods: OpenEvoEvolutionMethodCapability[];
}

export interface OpenEvoDesktopCapabilities {
  schemaVersion: "1";
  coreVersion: string;
  registryDigest: string;
  evaluatedProfile: {
    executionMode: OpenEvoDesktopCapabilitiesPayload["evaluated_profile"]["execution_mode"];
    captureMode: OpenEvoDesktopCapabilitiesPayload["evaluated_profile"]["capture_mode"];
    harnessId: string;
    harnessCapabilities: string[];
    runtimeCapabilities: string[];
  };
  targets: OpenEvoEvolutionTargetCapability[];
}

export interface OpenEvoArtifactContentPayload {
  artifact_id: string;
  artifact_type: string;
  filename: string;
  content: string;
  mime_type: string;
}

export interface OpenEvoArtifactContent {
  artifactId: string;
  artifactType: string;
  filename: string;
  content: string;
  mimeType: string;
}

export interface OpenEvoBackendTimelineEventPayload {
  id: string;
  phase: string;
  title: string;
  message: string;
  artifact_ids: string[];
}

export interface OpenEvoBackendTimelineEvent {
  id: string;
  phase: string;
  label: string;
  message: string;
  artifactIds: string[];
}

export interface OpenEvoBackendArtifactSummaryPayload {
  id: string;
  run_id: string;
  artifact_type: string;
  title: string;
  promoted: boolean;
  lineage: Record<string, unknown>;
}

export interface OpenEvoBackendArtifactSummary {
  id: string;
  runId: string;
  artifactType: string;
  title: string;
  promoted: boolean;
  lineage: Record<string, unknown>;
}

export interface OpenEvoBackendArtifactContentPayload {
  id: string;
  artifact_type: string;
  content: string;
  metadata: Record<string, unknown>;
}

export interface OpenEvoBackendArtifactDiffPayload {
  id: string;
  before: string;
  after: string;
  format: "unified_text";
}

export interface OpenEvoBackendArtifactPreview {
  id: string;
  kind: string;
  body: string;
  targetPath?: string;
  lineage: Record<string, unknown>;
  diff: OpenEvoBackendArtifactDiffPayload;
}

export interface OpenEvoRunArtifactJob {
  artifactType: string;
  method: string;
  workerStatus: string;
  artifactIds: string[];
  approvedArtifactIds: string[];
  promotionStatus: string;
}

export interface OpenEvoRunArtifactRound {
  roundIndex: number;
  policyVersion: string;
  rolloutStatus: string;
  datasetStatus: string;
  artifactIds: Record<string, string[]>;
  jobs: OpenEvoRunArtifactJob[];
}

export interface OpenEvoRunArtifactTask {
  taskId: string;
  rounds: OpenEvoRunArtifactRound[];
}

export interface OpenEvoRunArtifacts {
  runId: string;
  outputDir: string;
  summaryStatus: string;
  experimentId: string;
  experimentName: string;
  roundCount: number;
  tasks: OpenEvoRunArtifactTask[];
}

export interface OpenEvoProjectConfigResponse {
  config: OpenEvoProjectConfigPaths;
  status: OpenEvoDesktopShellModel;
}

export interface OpenEvoSavedProjectConfig {
  projectSlug: string;
  valid: boolean;
  error: string | null;
  projectName: string | null;
  taskId: string | null;
  objective: string | null;
  sourceType: string | null;
  sourceLabel: string | null;
  remoteProfileId: string | null;
  remoteHost: string | null;
  remoteUser: string | null;
  scienceConfigPath: string;
  remoteProfilePath: string | null;
}

const sidecarMutationTokenHeader = "X-OpenEvo-Sidecar-Token";
let sidecarMutationToken: string | null = null;

export async function fetchOpenEvoDesktopShellModel(): Promise<OpenEvoDesktopShellModel> {
  const payload = await api.get<OpenEvoDesktopShellStatusPayload>(
    "/openevo-api/desktop/shell",
  );
  rememberOpenEvoSidecarMutationToken(payload);
  return toOpenEvoDesktopShellModel(payload);
}

export async function runOpenEvoBootstrap(): Promise<OpenEvoBootstrapResponse> {
  const headers = sidecarMutationToken
    ? { [sidecarMutationTokenHeader]: sidecarMutationToken }
    : undefined;
  const payload = await api.post<OpenEvoBootstrapResponsePayload>(
    "/openevo-api/desktop/bootstrap",
    {},
    headers,
  );
  rememberOpenEvoSidecarMutationToken(payload.status);
  return toOpenEvoBootstrapResponse(payload);
}

export async function runOpenEvoWorkspaceSync(): Promise<OpenEvoWorkspaceResponse> {
  const headers = sidecarMutationToken
    ? { [sidecarMutationTokenHeader]: sidecarMutationToken }
    : undefined;
  const payload = await api.post<OpenEvoWorkspaceResponsePayload>(
    "/openevo-api/desktop/workspace",
    {},
    headers,
  );
  rememberOpenEvoSidecarMutationToken(payload.status);
  return {
    workspace: payload.workspace,
    report: payload.report,
    status: toOpenEvoDesktopShellModel(payload.status),
  };
}

export async function runOpenEvoServices(): Promise<OpenEvoServicesResponse> {
  const headers = sidecarMutationToken
    ? { [sidecarMutationTokenHeader]: sidecarMutationToken }
    : undefined;
  const payload = await api.post<OpenEvoServicesResponsePayload>(
    "/openevo-api/desktop/services",
    {},
    headers,
  );
  rememberOpenEvoSidecarMutationToken(payload.status);
  return {
    services: payload.services,
    report: payload.report,
    status: toOpenEvoDesktopShellModel(payload.status),
  };
}

export async function saveOpenEvoProjectConfig(
  draft: OpenEvoProjectConfigDraft,
): Promise<OpenEvoProjectConfigResponse> {
  const headers = sidecarMutationToken
    ? { [sidecarMutationTokenHeader]: sidecarMutationToken }
    : undefined;
  const payload = await api.post<OpenEvoProjectConfigResponsePayload>(
    "/openevo-api/desktop/project-config",
    toProjectConfigPayload(draft),
    headers,
  );
  rememberOpenEvoSidecarMutationToken(payload.status);
  return {
    config: payload.config,
    status: toOpenEvoDesktopShellModel(payload.status),
  };
}

export async function fetchOpenEvoProjectConfigs(): Promise<
  OpenEvoSavedProjectConfig[]
> {
  const payload = await api.get<OpenEvoProjectConfigsResponsePayload>(
    "/openevo-api/desktop/project-configs",
  );
  return payload.configs.map(toOpenEvoSavedProjectConfig);
}

export async function activateOpenEvoProjectConfig(
  projectSlug: string,
): Promise<OpenEvoProjectConfigResponse> {
  const headers = sidecarMutationToken
    ? { [sidecarMutationTokenHeader]: sidecarMutationToken }
    : undefined;
  const payload = await api.post<OpenEvoProjectConfigResponsePayload>(
    `/openevo-api/desktop/project-configs/${encodeURIComponent(projectSlug)}/activate`,
    {},
    headers,
  );
  rememberOpenEvoSidecarMutationToken(payload.status);
  return {
    config: payload.config,
    status: toOpenEvoDesktopShellModel(payload.status),
  };
}

export async function runOpenEvoStartRun(): Promise<OpenEvoRunResponse> {
  const headers = sidecarMutationToken
    ? { [sidecarMutationTokenHeader]: sidecarMutationToken }
    : undefined;
  const payload = await api.post<OpenEvoRunResponsePayload>(
    "/openevo-api/desktop/run",
    {},
    headers,
  );
  rememberOpenEvoSidecarMutationToken(payload.status);
  return {
    run: toOpenEvoRunStatus(payload.run),
    status: toOpenEvoDesktopShellModel(payload.status),
  };
}

export async function pollOpenEvoRunStatus(): Promise<OpenEvoRunResponse> {
  const headers = sidecarMutationToken
    ? { [sidecarMutationTokenHeader]: sidecarMutationToken }
    : undefined;
  const payload = await api.get<OpenEvoRunResponsePayload>(
    "/openevo-api/desktop/run",
    headers,
  );
  rememberOpenEvoSidecarMutationToken(payload.status);
  return {
    run: toOpenEvoRunStatus(payload.run),
    status: toOpenEvoDesktopShellModel(payload.status),
  };
}

export async function fetchOpenEvoBackendRunTimeline(
  runId: string,
): Promise<OpenEvoBackendTimelineEvent[]> {
  const headers = sidecarMutationToken
    ? { [sidecarMutationTokenHeader]: sidecarMutationToken }
    : undefined;
  const payload = await api.get<OpenEvoBackendTimelineEventPayload[]>(
    `/openevo-api/backend/runs/${encodeURIComponent(runId)}/timeline`,
    headers,
  );
  return timelineView(payload);
}

export async function fetchOpenEvoBackendRunArtifacts(
  runId: string,
): Promise<OpenEvoBackendArtifactSummary[]> {
  const headers = sidecarMutationToken
    ? { [sidecarMutationTokenHeader]: sidecarMutationToken }
    : undefined;
  const payload = await api.get<OpenEvoBackendArtifactSummaryPayload[]>(
    `/openevo-api/backend/runs/${encodeURIComponent(runId)}/artifacts`,
    headers,
  );
  return payload.map(toOpenEvoBackendArtifactSummary);
}

export async function fetchOpenEvoDesktopCapabilities(
  executionMode: OpenEvoExecutionMode,
): Promise<OpenEvoDesktopCapabilities> {
  const headers = sidecarMutationToken
    ? { [sidecarMutationTokenHeader]: sidecarMutationToken }
    : undefined;
  const payload = await api.get<OpenEvoDesktopCapabilitiesPayload>(
    `/openevo-api/desktop/capabilities?execution_mode=${encodeURIComponent(executionMode)}`,
    headers,
  );
  return toOpenEvoDesktopCapabilities(payload);
}

export async function fetchOpenEvoBackendArtifactPreview(
  artifactId: string,
): Promise<OpenEvoBackendArtifactPreview> {
  const headers = sidecarMutationToken
    ? { [sidecarMutationTokenHeader]: sidecarMutationToken }
    : undefined;
  const [content, diff] = await Promise.all([
    api.get<OpenEvoBackendArtifactContentPayload>(
      `/openevo-api/backend/artifacts/${encodeURIComponent(artifactId)}/content`,
      headers,
    ),
    api.get<OpenEvoBackendArtifactDiffPayload>(
      `/openevo-api/backend/artifacts/${encodeURIComponent(artifactId)}/diff`,
      headers,
    ),
  ]);
  return artifactPreview(content, diff);
}

export function toOpenEvoBootstrapResponse(
  payload: OpenEvoBootstrapResponsePayload,
): OpenEvoBootstrapResponse {
  return {
    bootstrap: {
      ready: payload.bootstrap.ready,
      stateRoot: payload.bootstrap.state_root,
      workspaceRoot: payload.bootstrap.workspace_root,
      readinessNotes: payload.bootstrap.readiness_notes,
    },
    report: payload.report,
    status: toOpenEvoDesktopShellModel(payload.status),
  };
}

export function toOpenEvoRunStatus(
  payload: OpenEvoRunStatusPayload,
): OpenEvoRunStatus {
  return {
    id: payload.id,
    state: payload.state,
    ready: payload.ready,
    command: payload.command,
    returnCode: payload.return_code,
    stdout: payload.stdout,
    stderr: payload.stderr,
    outputDir: payload.output_dir,
    experimentSnapshot: payload.experiment_snapshot,
    startedAt: payload.started_at,
    finishedAt: payload.finished_at,
  };
}

export function toOpenEvoRunArtifacts(
  payload: OpenEvoRunArtifactsPayload,
): OpenEvoRunArtifacts {
  return {
    runId: payload.run_id,
    outputDir: payload.output_dir,
    summaryStatus: payload.summary_status ?? "unknown",
    experimentId: payload.experiment_id ?? "unknown",
    experimentName:
      payload.experiment_name ?? payload.experiment_id ?? "unknown",
    roundCount: payload.round_count ?? 0,
    tasks: payload.tasks.map((task) => ({
      taskId: task.task_id,
      rounds: task.rounds.map((round) => ({
        roundIndex: round.round_index,
        policyVersion: round.policy_version ?? "",
        rolloutStatus: round.rollout_status ?? "unknown",
        datasetStatus: round.dataset_status ?? "unknown",
        artifactIds: round.artifact_ids,
        jobs: round.jobs.map((job) => ({
          artifactType: job.artifact_type,
          method: job.method,
          workerStatus: job.worker_status,
          artifactIds: job.artifact_ids,
          approvedArtifactIds: job.approved_artifact_ids,
          promotionStatus: job.promotion_status,
        })),
      })),
    })),
  };
}

export function toOpenEvoDesktopCapabilities(
  payload: OpenEvoDesktopCapabilitiesPayload,
): OpenEvoDesktopCapabilities {
  return {
    schemaVersion: payload.schema_version,
    coreVersion: payload.core_version,
    registryDigest: payload.registry_digest,
    evaluatedProfile: {
      executionMode: payload.evaluated_profile.execution_mode,
      captureMode: payload.evaluated_profile.capture_mode,
      harnessId: payload.evaluated_profile.harness_id,
      harnessCapabilities: payload.evaluated_profile.harness_capabilities,
      runtimeCapabilities: payload.evaluated_profile.runtime_capabilities,
    },
    targets: payload.targets.map((target) => ({
      targetId: target.target_id,
      artifactType: target.artifact_type,
      displayName: target.display_name,
      description: target.description,
      exposure: target.exposure,
      maturity: target.maturity,
      handlerId: target.handler_id,
      configuredDefaultMethodId: target.configured_default_method_id,
      effectiveDefaultMethodId: target.effective_default_method_id,
      configuredDefaultSupport: toCapabilitySupport(
        target.configured_default_support,
      ),
      rendererKind: target.renderer_kind,
      rendererContractVersion: target.renderer_contract_version,
      contributionContractVersion: target.contribution_contract_version,
      contextOrder: target.context_order,
      implementationIdentityDigest: target.implementation_identity_digest,
      handlerIdentityDigest: target.handler_identity_digest,
      acceptedMethods: target.accepted_methods.map((method) => ({
        methodId: method.method_id,
        implementationIdentityDigest: method.implementation_identity_digest,
        support: toCapabilitySupport(method.support),
      })),
      selectionResolvers: target.selection_resolvers.map((resolver) => ({
        selectionValue: resolver.selection_value,
        displayName: resolver.display_name,
        description: resolver.description,
        resolvedMethods: resolver.resolved_methods.map((method) => ({
          methodId: method.method_id,
          implementationIdentityDigest: method.implementation_identity_digest,
          support: toCapabilitySupport(method.support),
        })),
      })),
      methods: target.methods.map((method) => ({
        methodId: method.method_id,
        displayName: method.display_name,
        description: method.description,
        exposure: method.exposure,
        maturity: method.maturity,
        executionModes: method.execution_modes,
        captureModes: method.capture_modes,
        supportedHarnessIds: method.supported_harness_ids,
        harnessRequirements: method.harness_requirements,
        runtimeRequirements: method.runtime_requirements,
        inputBindings: method.input_bindings.map((binding) => ({
          bindingId: binding.binding_id,
          source: binding.source,
          artifactType: binding.artifact_type,
          minCount: binding.min_count,
          maxCount: binding.max_count,
        })),
        outputArtifactTypes: method.output_artifact_types,
        configSchemaJson: method.config_schema_json,
        defaultConfigJson: method.default_config_json,
        configSchema: parseCanonicalJsonObject(
          method.config_schema_json,
          "config_schema_json",
        ),
        defaultConfig: parseCanonicalJsonObject(
          method.default_config_json,
          "default_config_json",
        ),
        implementationIdentityDigest: method.implementation_identity_digest,
        support: toCapabilitySupport(method.support),
      })),
    })),
  };
}

function toCapabilitySupport(
  support: OpenEvoCapabilityMethodSupportPayload,
): OpenEvoCapabilityMethodSupport {
  const axis = (value: OpenEvoCapabilityAxisSupportPayload) => ({
    state: value.state,
    reasonCode: value.reason_code,
    message: value.message,
    missingRequirements: value.missing_requirements,
  });
  return {
    overall: support.overall,
    execution: axis(support.execution),
    capture: axis(support.capture),
    harness: axis(support.harness),
    runtime: axis(support.runtime),
  };
}

function parseCanonicalJsonObject(
  encoded: string,
  field: string,
): OpenEvoJsonObject {
  let value: unknown;
  try {
    value = JSON.parse(encoded);
  } catch {
    throw new Error(`${field} must contain canonical JSON`);
  }
  if (!isJsonObject(value)) {
    throw new Error(`${field} must contain canonical JSON`);
  }
  return value;
}

function isJsonObject(value: unknown): value is OpenEvoJsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function toOpenEvoArtifactContent(
  payload: OpenEvoArtifactContentPayload,
): OpenEvoArtifactContent {
  return {
    artifactId: payload.artifact_id,
    artifactType: payload.artifact_type,
    filename: payload.filename,
    content: payload.content,
    mimeType: payload.mime_type,
  };
}

export function toOpenEvoBackendArtifactSummary(
  payload: OpenEvoBackendArtifactSummaryPayload,
): OpenEvoBackendArtifactSummary {
  return {
    id: payload.id,
    runId: payload.run_id,
    artifactType: payload.artifact_type,
    title: payload.title,
    promoted: payload.promoted,
    lineage: payload.lineage,
  };
}

export function toOpenEvoSavedProjectConfig(
  payload: OpenEvoSavedProjectConfigPayload,
): OpenEvoSavedProjectConfig {
  return {
    projectSlug: payload.project_slug,
    valid: payload.valid,
    error: payload.error,
    projectName: payload.project_name,
    taskId: payload.task_id,
    objective: payload.objective,
    sourceType: payload.source_type,
    sourceLabel: payload.source_label,
    remoteProfileId: payload.remote_profile_id,
    remoteHost: payload.remote_host,
    remoteUser: payload.remote_user,
    scienceConfigPath: payload.science_config_path,
    remoteProfilePath: payload.remote_profile_path,
  };
}

function rememberOpenEvoSidecarMutationToken(
  payload: OpenEvoDesktopShellStatusPayload,
) {
  const token = payload.sidecar?.mutation_token;
  if (token) {
    sidecarMutationToken = token;
  }
}

export function toOpenEvoDesktopShellModel(
  payload: OpenEvoDesktopShellStatusPayload,
): OpenEvoDesktopShellModel {
  return {
    remote: {
      id: payload.remote.id,
      host: payload.remote.host,
      port: payload.remote.port,
      user: payload.remote.user,
      auth: {
        method: payload.remote.auth.method,
        privateKeyPath: payload.remote.auth.private_key_path,
        passwordRef: payload.remote.auth.password_ref,
        passphraseRef: payload.remote.auth.passphrase_ref,
      },
      workspaceRoot: payload.remote.workspace_root,
      proxy: {
        httpProxy: payload.remote.proxy.http_proxy ?? "not configured",
        httpsProxy: payload.remote.proxy.https_proxy ?? "not configured",
        noProxy: payload.remote.proxy.no_proxy ?? "not configured",
        pipIndexUrl: payload.remote.proxy.pip_index_url ?? "not configured",
        huggingFaceEndpoint:
          payload.remote.proxy.huggingface_endpoint ?? "not configured",
        hfHome: payload.remote.proxy.hf_home ?? "not configured",
      },
    },
    project: {
      name: payload.project.name,
      taskId: payload.project.task_id,
      source: payload.project.source,
      objective: payload.project.objective,
      evolutionTargets: copyEvolutionTargets(
        payload.project.evolution_targets,
      ),
    },
    execution: {
      mode: normalizeOpenEvoExecutionMode(payload.execution.mode),
      model: payload.execution.model,
      tokenMetricsAvailable: payload.execution.token_metrics_available,
    },
    bootstrap: {
      ready: payload.bootstrap.ready,
      stateRoot: payload.bootstrap.state_root,
      workspaceRoot: payload.bootstrap.workspace_root,
      readinessNotes: payload.bootstrap.readiness_notes,
    },
    services: payload.services,
    evolution: payload.evolution,
    developerMode: {
      enabled: payload.developer_mode.enabled,
      benchmarkControlsVisible:
        payload.developer_mode.benchmark_controls_visible,
    },
    sidecar: {
      transport: {
        id: payload.sidecar?.transport?.id ?? "dry-run",
        label: payload.sidecar?.transport?.label ?? "Dry-run transport",
        supportsPasswordRef:
          payload.sidecar?.transport?.supports_password_ref ?? true,
        supportsPassphraseRef:
          payload.sidecar?.transport?.supports_passphrase_ref ?? true,
      },
    },
  };
}
