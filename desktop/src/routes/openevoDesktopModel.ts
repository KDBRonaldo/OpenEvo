export type RemoteServiceState = "ready" | "running" | "planned" | "blocked";

export type EvolutionStepState = "complete" | "running" | "planned" | "blocked";

export type OpenEvoExecutionMode =
  | "codex_subscription_transcript"
  | "self-deployed";

export type OpenEvoExecutionModePayload =
  | OpenEvoExecutionMode
  | "codex_managed_local_inference";

export type OpenEvoJsonValue =
  | null
  | boolean
  | number
  | string
  | OpenEvoJsonValue[]
  | OpenEvoJsonObject;

export interface OpenEvoJsonObject {
  [key: string]: OpenEvoJsonValue;
}

export interface OpenEvoEvolutionTargetSelection {
  enabled: boolean;
  method: string | null;
  config: OpenEvoJsonObject;
}

export interface OpenEvoDesktopShellModel {
  remote: {
    id: string;
    host: string;
    port: number;
    user: string;
    auth: {
      method: "ssh_agent" | "private_key" | "password_ref";
      privateKeyPath: string | null;
      passwordRef: string | null;
      passphraseRef: string | null;
    };
    workspaceRoot: string;
    proxy: {
      httpProxy: string;
      httpsProxy: string;
      noProxy: string;
      pipIndexUrl: string;
      huggingFaceEndpoint: string;
      hfHome: string;
    };
  };
  project: {
    name: string;
    taskId: string;
    source: string;
    objective: string;
    evolutionTargets: Record<string, OpenEvoEvolutionTargetSelection>;
  };
  execution: {
    mode: OpenEvoExecutionMode;
    model: string;
    tokenMetricsAvailable: boolean;
  };
  bootstrap: {
    ready: boolean;
    stateRoot: string;
    workspaceRoot: string;
    readinessNotes: string[];
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
  developerMode: {
    enabled: boolean;
    benchmarkControlsVisible: boolean;
  };
  sidecar: {
    transport: {
      id: "dry-run" | "ssh";
      label: string;
      supportsPasswordRef: boolean;
      supportsPassphraseRef: boolean;
    };
  };
}

export interface OpenEvoTimelineSummary {
  readyServices: number;
  totalServices: number;
  bootstrapReady: boolean;
  completedEvolutionSteps: number;
  totalEvolutionSteps: number;
  readinessNotes: string[];
}

export interface OpenEvoProjectConfigDraftPayload {
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
  execution_mode: OpenEvoExecutionMode;
  codex_model?: string | null;
  hf_model?: string | null;
  evolution: {
    targets: Record<string, OpenEvoEvolutionTargetSelection>;
  };
}

export type OpenEvoProjectConfigPayload = OpenEvoProjectConfigDraftPayload;

export function normalizeOpenEvoExecutionMode(
  mode: OpenEvoExecutionModePayload,
): OpenEvoExecutionMode {
  if (mode === "codex_managed_local_inference") {
    return "self-deployed";
  }
  return mode;
}

export function getOpenEvoDesktopShellModel(): OpenEvoDesktopShellModel {
  return {
    remote: {
      id: "not-configured",
      host: "",
      port: 22,
      user: "",
      auth: {
        method: "ssh_agent",
        privateKeyPath: null,
        passwordRef: null,
        passphraseRef: null,
      },
      workspaceRoot: "~/.openevo/workspaces",
      proxy: {
        httpProxy: "not configured",
        httpsProxy: "not configured",
        noProxy: "not configured",
        pipIndexUrl: "not configured",
        huggingFaceEndpoint: "not configured",
        hfHome: "not configured",
      },
    },
    project: {
      name: "Untitled Science Project",
      taskId: "new-task",
      source: "Scratch workspace",
      objective: "",
      evolutionTargets: {},
    },
    execution: {
      mode: "codex_subscription_transcript",
      model: "codex subscription on remote server",
      tokenMetricsAvailable: false,
    },
    bootstrap: {
      ready: false,
      stateRoot: "~/.openevo/runs/untitled-science-project/new-task",
      workspaceRoot: "~/.openevo/workspaces",
      readinessNotes: ["Configure a project and remote backend to begin."],
    },
    services: [
      {
        id: "ssh",
        label: "SSH transport",
        state: "planned",
        detail: "Configure a remote GPU server profile",
      },
      {
        id: "workspace",
        label: "Workspace",
        state: "planned",
        detail: "Save project config before workspace sync",
      },
      {
        id: "bootstrap",
        label: "Bootstrap",
        state: "planned",
        detail: "Run remote bootstrap after project config is saved",
      },
      {
        id: "openevo-backend",
        label: "OpenEvo backend",
        state: "planned",
        detail: "Start backend after bootstrap is ready",
      },
    ],
    evolution: [
      {
        id: "transcript",
        label: "Transcript capture",
        state: "planned",
        detail: "Trajectory capture starts after the first run",
      },
    ],
    developerMode: {
      enabled: false,
      benchmarkControlsVisible: false,
    },
    sidecar: {
      transport: {
        id: "dry-run",
        label: "Dry-run transport",
        supportsPasswordRef: true,
        supportsPassphraseRef: true,
      },
    },
  };
}

export function getOpenEvoTimelineSummary(
  model: OpenEvoDesktopShellModel,
): OpenEvoTimelineSummary {
  return {
    readyServices: model.services.filter((service) => service.state === "ready").length,
    totalServices: model.services.length,
    bootstrapReady: model.bootstrap.ready,
    completedEvolutionSteps: model.evolution.filter((step) => step.state === "complete")
      .length,
    totalEvolutionSteps: model.evolution.length,
    readinessNotes: model.bootstrap.readinessNotes,
  };
}

export function toDraftPayload(
  model: OpenEvoDesktopShellModel,
): OpenEvoProjectConfigDraftPayload {
  const source = sourceDraftFromLabel(model.project.source);
  const executionMode = normalizeOpenEvoExecutionMode(model.execution.mode);
  return {
    project_name: model.project.name,
    task_id: model.project.taskId,
    objective: model.project.objective,
    source_type: source.source_type,
    source_path: source.source_path,
    source_url: source.source_url,
    source_branch: source.source_branch,
    remote_profile_id: model.remote.id,
    remote_host: model.remote.host,
    remote_port: model.remote.port,
    remote_user: model.remote.user,
    auth_method: model.remote.auth.method,
    private_key_path: model.remote.auth.privateKeyPath,
    password_ref: model.remote.auth.passwordRef,
    passphrase_ref: model.remote.auth.passphraseRef,
    workspace_root: model.remote.workspaceRoot,
    http_proxy: optionalConfigured(model.remote.proxy.httpProxy),
    https_proxy: optionalConfigured(model.remote.proxy.httpsProxy),
    no_proxy: optionalConfigured(model.remote.proxy.noProxy),
    pip_index_url: optionalConfigured(model.remote.proxy.pipIndexUrl),
    huggingface_endpoint: optionalConfigured(model.remote.proxy.huggingFaceEndpoint),
    hf_home: optionalConfigured(model.remote.proxy.hfHome),
    execution_mode: executionMode,
    codex_model:
      executionMode === "codex_subscription_transcript"
        ? model.execution.model || "gpt-5.1-codex-mini"
        : null,
    hf_model: executionMode === "self-deployed" ? model.execution.model : null,
    evolution: {
      targets: copyEvolutionTargets(model.project.evolutionTargets),
    },
  };
}

export function toProjectConfigPayload(
  draft: OpenEvoProjectConfigDraftPayload,
): OpenEvoProjectConfigPayload {
  return {
    ...draft,
    evolution: {
      targets: copyEvolutionTargets(draft.evolution.targets),
    },
  };
}

export function copyEvolutionTargets(
  targets: Record<string, OpenEvoEvolutionTargetSelection>,
): Record<string, OpenEvoEvolutionTargetSelection> {
  return Object.fromEntries(
    Object.entries(targets).map(([targetId, selection]) => [
      targetId,
      {
        ...selection,
        config: copyJsonObject(selection.config),
      },
    ]),
  );
}

function copyJsonObject(value: OpenEvoJsonObject): OpenEvoJsonObject {
  const copied = copyJsonValue(value, "config", new WeakSet<object>());
  if (copied === null || Array.isArray(copied) || typeof copied !== "object") {
    throw new TypeError("evolution target config must be a JSON object");
  }
  return copied;
}

function copyJsonValue(
  value: unknown,
  path: string,
  ancestors: WeakSet<object>,
): OpenEvoJsonValue {
  if (
    value === null ||
    typeof value === "string" ||
    typeof value === "boolean"
  ) {
    return value;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new TypeError(`${path} must contain only finite JSON numbers`);
    }
    if (Number.isInteger(value) && !Number.isSafeInteger(value)) {
      throw new TypeError(`${path} integer exceeds the safe JSON range`);
    }
    return value;
  }
  if (typeof value !== "object") {
    throw new TypeError(`${path} contains a non-JSON value`);
  }
  if (ancestors.has(value)) {
    throw new TypeError(`${path} contains a circular reference`);
  }

  ancestors.add(value);
  try {
    if (Array.isArray(value)) {
      return Array.from(value, (item, index) =>
        copyJsonValue(item, `${path}[${index}]`, ancestors),
      );
    }
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) {
      throw new TypeError(`${path} contains a non-JSON object`);
    }
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        key,
        copyJsonValue(item, `${path}.${key}`, ancestors),
      ]),
    );
  } finally {
    ancestors.delete(value);
  }
}

function sourceDraftFromLabel(
  label: string,
): Pick<
  OpenEvoProjectConfigDraftPayload,
  "source_type" | "source_path" | "source_url" | "source_branch"
> {
  if (label.startsWith("Remote path: ")) {
    return {
      source_type: "remote_path",
      source_path: label.slice("Remote path: ".length),
      source_url: null,
      source_branch: null,
    };
  }
  if (label.startsWith("Local folder: ")) {
    return {
      source_type: "local_folder",
      source_path: label.slice("Local folder: ".length),
      source_url: null,
      source_branch: null,
    };
  }
  if (label.startsWith("Git repository: ")) {
    const value = label.slice("Git repository: ".length);
    const match = value.match(/^(.*) \((.*)\)$/);
    return {
      source_type: "git_repository",
      source_path: null,
      source_url: match ? match[1] : value,
      source_branch: match ? match[2] : null,
    };
  }
  return {
    source_type: "scratch",
    source_path: null,
    source_url: null,
    source_branch: null,
  };
}

function optionalConfigured(value: string): string | null {
  return value === "not configured" ? null : value;
}
