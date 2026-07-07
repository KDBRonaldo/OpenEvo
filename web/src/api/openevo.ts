import { api } from "./client";
import type {
  EvolutionStepState,
  OpenEvoDesktopShellModel,
  RemoteServiceState,
} from "../routes/openevoDesktopModel";

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
  };
  execution: {
    mode: OpenEvoDesktopShellModel["execution"]["mode"];
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

export interface OpenEvoProjectConfigDraft {
  project_name: string;
  task_id: string;
  objective: string;
  source_type: "local_folder" | "git_repository" | "remote_path" | "scratch";
  source_path?: string | null;
  source_url?: string | null;
  source_branch?: string | null;
  remote_profile_id: string;
  remote_host: string;
  remote_port: number;
  remote_user: string;
  auth_method: "ssh_agent" | "private_key" | "password_ref";
  private_key_path?: string | null;
  password_ref?: string | null;
  passphrase_ref?: string | null;
  workspace_root?: string | null;
  http_proxy?: string | null;
  https_proxy?: string | null;
  no_proxy?: string | null;
  pip_index_url?: string | null;
  huggingface_endpoint?: string | null;
  hf_home?: string | null;
  execution_mode:
    | "codex_subscription_transcript"
    | "codex_managed_local_inference";
  codex_model?: string | null;
  hf_model?: string | null;
  text_memory: boolean;
  skill_bundle: boolean;
  agent_system: boolean;
}

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
    draft,
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

export async function fetchOpenEvoRunArtifacts(): Promise<OpenEvoRunArtifacts> {
  const headers = sidecarMutationToken
    ? { [sidecarMutationTokenHeader]: sidecarMutationToken }
    : undefined;
  const payload = await api.get<OpenEvoRunArtifactsPayload>(
    "/openevo-api/desktop/run/artifacts",
    headers,
  );
  return toOpenEvoRunArtifacts(payload);
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
    },
    execution: {
      mode: payload.execution.mode,
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
