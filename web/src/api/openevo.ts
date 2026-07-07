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
}

export async function fetchOpenEvoDesktopShellModel(): Promise<OpenEvoDesktopShellModel> {
  const payload = await api.get<OpenEvoDesktopShellStatusPayload>(
    "/openevo-api/desktop/shell",
  );
  return toOpenEvoDesktopShellModel(payload);
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
