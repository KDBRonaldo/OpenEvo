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
