import { api } from "./client";
import type {
  EvolutionStepState,
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

export interface EvolutionMethodCapability {
  method_id: string;
  display_name: string;
  artifact_type:
    | "text_memory"
    | "skill_bundle"
    | "agent_system"
    | "parametric_memory";
  supported_execution_modes: OpenEvoExecutionMode[];
  visible_in_desktop: boolean;
  stability_level: "stable" | "experimental" | "internal";
}

export interface OpenEvoArtifactTargetCapability {
  artifact_type: EvolutionMethodCapability["artifact_type"];
  display_name: string;
  visible_in_desktop: boolean;
  stability_level: "stable" | "experimental" | "internal";
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
  artifact_targets: OpenEvoArtifactTargetCapability[];
  evolution_methods: EvolutionMethodCapability[];
}

export interface OpenEvoDesktopCapabilities {
  artifactTargets: Array<{
    artifactType: OpenEvoArtifactTargetCapability["artifact_type"];
    displayName: string;
    visibleInDesktop: boolean;
    stabilityLevel: OpenEvoArtifactTargetCapability["stability_level"];
  }>;
  evolutionMethods: Array<{
    methodId: string;
    displayName: string;
    artifactType: EvolutionMethodCapability["artifact_type"];
    supportedExecutionModes: OpenEvoExecutionMode[];
    visibleInDesktop: boolean;
    stabilityLevel: EvolutionMethodCapability["stability_level"];
  }>;
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

export async function fetchOpenEvoDesktopCapabilities(): Promise<OpenEvoDesktopCapabilities> {
  const payload = await api.get<OpenEvoDesktopCapabilitiesPayload>(
    "/openevo-api/desktop/capabilities",
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
    artifactTargets: payload.artifact_targets.map((target) => ({
      artifactType: target.artifact_type,
      displayName: target.display_name,
      visibleInDesktop: target.visible_in_desktop,
      stabilityLevel: target.stability_level,
    })),
    evolutionMethods: payload.evolution_methods.map((method) => ({
      methodId: method.method_id,
      displayName: method.display_name,
      artifactType: method.artifact_type,
      supportedExecutionModes: method.supported_execution_modes,
      visibleInDesktop: method.visible_in_desktop,
      stabilityLevel: method.stability_level,
    })),
  };
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
