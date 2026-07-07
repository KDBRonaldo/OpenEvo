export type RemoteServiceState = "ready" | "running" | "planned" | "blocked";

export type EvolutionStepState = "complete" | "running" | "planned" | "blocked";

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
    mode: "codex_subscription_transcript" | "codex_managed_local_inference";
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
}

export interface OpenEvoTimelineSummary {
  readyServices: number;
  totalServices: number;
  bootstrapReady: boolean;
  completedEvolutionSteps: number;
  totalEvolutionSteps: number;
  readinessNotes: string[];
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
        detail: "Service supervisor integration is next",
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
