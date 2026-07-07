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
    user: string;
    proxy: {
      https_proxy: string | null;
      huggingface_endpoint: string | null;
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
  codex_model: string;
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

export interface OpenEvoRunReport {
  ready: boolean;
  status: "pass" | "fail";
  command: string;
  return_code: number | null;
  stdout: string;
  stderr: string;
  output_dir: string;
  experiment_snapshot: string;
  started_at: string;
}

export interface OpenEvoRunResponsePayload {
  run: OpenEvoRunReport;
  status: OpenEvoDesktopShellStatusPayload;
}

export interface OpenEvoRunResponse {
  run: OpenEvoRunReport;
  status: OpenEvoDesktopShellModel;
}

export interface OpenEvoProjectConfigResponse {
  config: OpenEvoProjectConfigPaths;
  status: OpenEvoDesktopShellModel;
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
    run: payload.run,
    status: toOpenEvoDesktopShellModel(payload.status),
  };
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
      user: payload.remote.user,
      proxy: {
        httpsProxy: payload.remote.proxy.https_proxy ?? "not configured",
        huggingFaceEndpoint:
          payload.remote.proxy.huggingface_endpoint ?? "not configured",
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
  };
}
