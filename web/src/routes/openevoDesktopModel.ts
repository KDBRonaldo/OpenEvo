export type RemoteServiceState = "ready" | "running" | "planned" | "blocked";

export type EvolutionStepState = "complete" | "running" | "planned" | "blocked";

export type OpenEvoExecutionMode =
  | "codex_subscription_transcript"
  | "self-deployed";

export type OpenEvoExecutionModePayload =
  | OpenEvoExecutionMode
  | "codex_managed_local_inference";

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
  text_memory: boolean;
  skill_bundle: boolean;
  agent_system: boolean;
}

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
      id: "lab-gpu",
      host: "gpu.example.edu",
      port: 22,
      user: "alice",
      auth: {
        method: "ssh_agent",
        privateKeyPath: null,
        passwordRef: null,
        passphraseRef: null,
      },
      workspaceRoot: "/home/alice/.openevo/workspaces",
      proxy: {
        httpProxy: "not configured",
        httpsProxy: "http://127.0.0.1:7890",
        noProxy: "not configured",
        pipIndexUrl: "not configured",
        huggingFaceEndpoint: "https://hf-mirror.com",
        hfHome: "not configured",
      },
    },
    project: {
      name: "Protein Folding Literature Sprint",
      taskId: "folding-baseline",
      source: "Git repository: github.com/example/protein-workflows",
      objective:
        "Survey recent folding papers, extract benchmark tables, and run the baseline analysis notebook.",
    },
    execution: {
      mode: "codex_subscription_transcript",
      model: "gpt-5.1-codex-mini",
      tokenMetricsAvailable: false,
    },
    bootstrap: {
      ready: true,
      stateRoot:
        "/home/alice/.openevo/runs/protein-folding-literature-sprint/folding-baseline",
      workspaceRoot: "/home/alice/.openevo/workspaces",
      readinessNotes: ["Codex subscription login available"],
    },
    services: [
      {
        id: "ssh",
        label: "SSH transport",
        state: "ready",
        detail: "Remote command execution available",
      },
      {
        id: "workspace",
        label: "Workspace",
        state: "ready",
        detail: "Repository materialized in managed workspace",
      },
      {
        id: "bootstrap",
        label: "Bootstrap",
        state: "ready",
        detail: "Runtime image and manifests prepared",
      },
      {
        id: "openevo-backend",
        label: "OpenEvo backend",
        state: "planned",
        detail: "Remote runtime services have not started",
      },
    ],
    evolution: [
      {
        id: "transcript",
        label: "Transcript capture",
        state: "complete",
        detail: "Codex subscription mode uses transcript trajectory data",
      },
      {
        id: "memory",
        label: "Text memory",
        state: "complete",
        detail: "Two durable research notes promoted",
      },
      {
        id: "skills",
        label: "Skill bundle",
        state: "running",
        detail: "Extracting reusable literature-review workflow",
      },
      {
        id: "agent-system",
        label: "Agent system",
        state: "planned",
        detail: "Instruction diff will be reviewed after this round",
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
    text_memory: model.evolution.some((step) =>
      ["text-memory", "memory"].includes(step.id),
    ),
    skill_bundle: model.evolution.some((step) =>
      ["skill-bundle", "skills"].includes(step.id),
    ),
    agent_system: model.evolution.some((step) => step.id === "agent-system"),
  };
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
