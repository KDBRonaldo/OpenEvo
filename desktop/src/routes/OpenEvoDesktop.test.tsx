// @vitest-environment happy-dom

import { StrictMode, act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OpenEvoDesktop } from "./OpenEvoDesktop";
import { parseEvolutionConfigSchema } from "../api/evolutionConfigSchema";
import {
  getOpenEvoDesktopShellModel as getEmptyOpenEvoDesktopShellModel,
  type OpenEvoDesktopShellModel,
} from "./openevoDesktopModel";

const apiMocks = vi.hoisted(() => ({
  activateOpenEvoProjectConfig: vi.fn(),
  fetchOpenEvoBackendArtifactPreview: vi.fn(),
  fetchOpenEvoBackendRunArtifacts: vi.fn(),
  fetchOpenEvoBackendRunTimeline: vi.fn(),
  fetchOpenEvoDesktopCapabilities: vi.fn(),
  fetchOpenEvoProjectConfigs: vi.fn(),
  fetchOpenEvoDesktopShellModel: vi.fn(),
  pollOpenEvoRunStatus: vi.fn(),
  runOpenEvoBootstrap: vi.fn(),
  runOpenEvoServices: vi.fn(),
  runOpenEvoStartRun: vi.fn(),
  runOpenEvoWorkspaceSync: vi.fn(),
  saveOpenEvoProjectConfig: vi.fn(),
}));

vi.mock("../api/openevo", () => ({
  activateOpenEvoProjectConfig: apiMocks.activateOpenEvoProjectConfig,
  fetchOpenEvoBackendArtifactPreview: apiMocks.fetchOpenEvoBackendArtifactPreview,
  fetchOpenEvoBackendRunArtifacts: apiMocks.fetchOpenEvoBackendRunArtifacts,
  fetchOpenEvoBackendRunTimeline: apiMocks.fetchOpenEvoBackendRunTimeline,
  fetchOpenEvoDesktopCapabilities: apiMocks.fetchOpenEvoDesktopCapabilities,
  fetchOpenEvoProjectConfigs: apiMocks.fetchOpenEvoProjectConfigs,
  fetchOpenEvoDesktopShellModel: apiMocks.fetchOpenEvoDesktopShellModel,
  pollOpenEvoRunStatus: apiMocks.pollOpenEvoRunStatus,
  runOpenEvoBootstrap: apiMocks.runOpenEvoBootstrap,
  runOpenEvoServices: apiMocks.runOpenEvoServices,
  runOpenEvoStartRun: apiMocks.runOpenEvoStartRun,
  runOpenEvoWorkspaceSync: apiMocks.runOpenEvoWorkspaceSync,
  saveOpenEvoProjectConfig: apiMocks.saveOpenEvoProjectConfig,
}));

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

function getOpenEvoDesktopShellModel(): OpenEvoDesktopShellModel {
  const model = getEmptyOpenEvoDesktopShellModel();
  return {
    ...model,
    project: {
      ...model.project,
      evolutionTargets: {
        text_memory: {
          enabled: true,
          method: "text_memory_reflector",
          config: {},
        },
        parametric_memory: {
          enabled: false,
          method: "parametric_memory_register",
          config: {},
        },
        skill_bundle: {
          enabled: true,
          method: "skill_bundle_reflector",
          config: {},
        },
        agent_system: {
          enabled: true,
          method: "auto",
          config: { target_path: "AGENTS.md" },
        },
      },
    },
    evolution: [
      {
        id: "text-memory",
        label: "Text memory",
        state: "planned",
        detail: "Memory updates appear after a run produces trajectories",
      },
      {
        id: "skill-bundle",
        label: "Skill bundle",
        state: "planned",
        detail: "Learned skills appear after evolution jobs complete",
      },
      {
        id: "agent-system",
        label: "Agent system",
        state: "planned",
        detail: "Instruction diffs appear after promoted artifacts exist",
      },
    ],
  };
}

describe("OpenEvoDesktop", () => {
  beforeEach(() => {
    apiMocks.activateOpenEvoProjectConfig.mockReset();
    apiMocks.fetchOpenEvoBackendArtifactPreview.mockReset();
    apiMocks.fetchOpenEvoBackendRunArtifacts.mockReset();
    apiMocks.fetchOpenEvoBackendRunTimeline.mockReset();
    apiMocks.fetchOpenEvoDesktopCapabilities.mockReset();
    apiMocks.fetchOpenEvoProjectConfigs.mockReset();
    apiMocks.fetchOpenEvoDesktopShellModel.mockReset();
    apiMocks.pollOpenEvoRunStatus.mockReset();
    apiMocks.runOpenEvoBootstrap.mockReset();
    apiMocks.runOpenEvoServices.mockReset();
    apiMocks.runOpenEvoStartRun.mockReset();
    apiMocks.runOpenEvoWorkspaceSync.mockReset();
    apiMocks.saveOpenEvoProjectConfig.mockReset();
    apiMocks.activateOpenEvoProjectConfig.mockRejectedValue(
      new Error("sidecar unavailable"),
    );
    apiMocks.fetchOpenEvoBackendArtifactPreview.mockRejectedValue(
      new Error("sidecar unavailable"),
    );
    apiMocks.fetchOpenEvoBackendRunArtifacts.mockResolvedValue(
      emptyRunArtifacts(),
    );
    apiMocks.fetchOpenEvoBackendRunTimeline.mockResolvedValue(emptyRunTimeline());
    apiMocks.fetchOpenEvoDesktopCapabilities.mockResolvedValue(
      desktopCapabilities(),
    );
    apiMocks.fetchOpenEvoProjectConfigs.mockRejectedValue(
      new Error("sidecar unavailable"),
    );
    apiMocks.fetchOpenEvoDesktopShellModel.mockRejectedValue(
      new Error("sidecar unavailable"),
    );
    apiMocks.pollOpenEvoRunStatus.mockRejectedValue(
      new Error("sidecar unavailable"),
    );
    apiMocks.runOpenEvoBootstrap.mockRejectedValue(
      new Error("sidecar unavailable"),
    );
    apiMocks.runOpenEvoServices.mockRejectedValue(
      new Error("sidecar unavailable"),
    );
    apiMocks.runOpenEvoStartRun.mockRejectedValue(
      new Error("sidecar unavailable"),
    );
    apiMocks.runOpenEvoWorkspaceSync.mockRejectedValue(
      new Error("sidecar unavailable"),
    );
    apiMocks.saveOpenEvoProjectConfig.mockRejectedValue(
      new Error("sidecar unavailable"),
    );
    document.body.innerHTML = "";
  });

  afterEach(() => {
    vi.useRealTimers();
    document.body.innerHTML = "";
  });

  it("renders setup-required state when the sidecar fetch is unavailable", () => {
    const html = renderToString(<OpenEvoDesktop />);

    expect(html).toContain("Untitled Science Project");
    expect(html).toContain("codex_subscription_transcript");
    expect(html).toContain("Setup required");
    expect(html).not.toContain("Protein Folding Literature Sprint");
    expect(html).not.toContain("gpu.example.edu");
  });

  it("runs bootstrap from the button and refreshes visible status", async () => {
    const shellModel = modelWithBootstrap({
      projectName: "Loaded Science Project",
      ready: false,
      notes: ["Remote bootstrap has not run yet."],
      bootstrapDetail: "Remote bootstrap has not run yet",
    });
    const bootstrappedModel = modelWithBootstrap({
      projectName: "Loaded Science Project",
      ready: true,
      notes: ["Remote bootstrap is ready."],
      bootstrapDetail: "Runtime image and manifests prepared",
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    const deferred = deferBootstrap(bootstrappedModel);
    apiMocks.runOpenEvoBootstrap.mockReturnValue(deferred.promise);

    const root = await renderClient();
    await flushEffects();

    expect(document.body.textContent).toContain("Loaded Science Project");
    expect(document.body.textContent).toContain("Setup required");

    const button = buttonByText("Bootstrap");
    expect(button.disabled).toBe(false);

    await act(async () => {
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(apiMocks.runOpenEvoBootstrap).toHaveBeenCalledTimes(1);
    expect(document.body.textContent).toContain("Bootstrapping");

    await act(async () => {
      deferred.resolve({
        bootstrap: bootstrappedModel.bootstrap,
        report: { ready: true },
        status: bootstrappedModel,
      });
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("Remote ready");
    expect(document.body.textContent).toContain("Remote bootstrap is ready.");
    await unmountClient(root);
  });

  it("renders bootstrap report next actions and failed preflight checks", async () => {
    const shellModel = modelWithBootstrap({
      projectName: "Loaded Science Project",
      ready: false,
      notes: ["Remote bootstrap has not run yet."],
      bootstrapDetail: "Remote bootstrap has not run yet",
    });
    const failedModel = modelWithBootstrap({
      projectName: "Loaded Science Project",
      ready: false,
      notes: ["Fix remote preflight failures and rerun bootstrap."],
      bootstrapDetail: "Fix remote preflight failures and rerun bootstrap.",
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    const deferred = deferBootstrap(failedModel);
    apiMocks.runOpenEvoBootstrap.mockReturnValue(deferred.promise);

    const root = await renderClient();
    await flushEffects();

    await act(async () => {
      buttonByText("Bootstrap").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
    });
    await act(async () => {
      deferred.resolve({
        bootstrap: failedModel.bootstrap,
        report: {
          ready: false,
          next_actions: ["Fix remote preflight failures and rerun bootstrap."],
          preflight: {
            checks: [
              {
                name: "ssh",
                status: "pass",
                message: "Remote command execution is available.",
                remediation_kind: "none",
                command: "true",
              },
              {
                name: "docker",
                status: "fail",
                message: "Docker is not available.",
                remediation_kind: "openevo_install",
                command: "docker info",
              },
            ],
          },
        },
        status: failedModel,
      });
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("Bootstrap Report");
    expect(document.body.textContent).toContain(
      "Fix remote preflight failures and rerun bootstrap.",
    );
    expect(document.body.textContent).toContain("docker");
    expect(document.body.textContent).toContain("openevo_install");
    expect(document.body.textContent).toContain("Docker is not available.");
    expect(document.body.textContent).not.toContain(
      "Remote command execution is available.",
    );
    await unmountClient(root);
  });

  it("renders failed bootstrap steps from the bootstrap report", async () => {
    const shellModel = modelWithBootstrap({
      projectName: "Loaded Science Project",
      ready: false,
      notes: ["Remote bootstrap has not run yet."],
      bootstrapDetail: "Remote bootstrap has not run yet",
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.runOpenEvoBootstrap.mockResolvedValue({
      bootstrap: shellModel.bootstrap,
      report: {
        ready: false,
        steps: [
          {
            id: "docker_pull_runtime",
            status: "fail",
            message: "Runtime image pull failed.",
            command: "docker pull openevo/science-runtime:0.1.1",
            stderr: "network timeout",
          },
        ],
      },
      status: shellModel,
    });

    const root = await renderClient();
    await flushEffects();

    await act(async () => {
      buttonByText("Bootstrap").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("Bootstrap Report");
    expect(document.body.textContent).toContain("docker_pull_runtime");
    expect(document.body.textContent).toContain("Runtime image pull failed.");
    expect(document.body.textContent).toContain(
      "docker pull openevo/science-runtime:0.1.1",
    );
    expect(document.body.textContent).toContain("network timeout");
    await unmountClient(root);
  });

  it("starts remote services and enables run after services are ready", async () => {
    const shellModel = withBackendService(
      modelWithBootstrap({
        projectName: "Loaded Science Project",
        ready: true,
        notes: ["Remote bootstrap is ready."],
        bootstrapDetail: "Runtime image and manifests prepared",
      }),
      {
        state: "planned",
        detail: "Remote runtime services have not started",
      },
    );
    const servicesModel = withBackendService(shellModel, {
      state: "ready",
      detail: "Remote runtime services are ready",
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    const deferred = deferServices(servicesModel);
    apiMocks.runOpenEvoServices.mockReturnValue(deferred.promise);

    const root = await renderClient();
    await flushEffects();

    expect(document.body.textContent).toContain(
      "Remote runtime services have not started",
    );
    expect(buttonByText("Start Run").disabled).toBe(true);
    expect(apiMocks.fetchOpenEvoDesktopCapabilities).not.toHaveBeenCalled();

    await act(async () => {
      buttonByText("Start Services").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
    });

    expect(apiMocks.runOpenEvoServices).toHaveBeenCalledTimes(1);
    expect(document.body.textContent).toContain("Starting Services");

    await act(async () => {
      deferred.resolve({
        services: {
          ready: true,
          state_root: "/home/alice/.openevo/runs/protein/folding",
          topology_path:
            "/home/alice/.openevo/runs/protein/folding/services/topology.yaml",
        },
        report: {
          ready: true,
          steps: [
            {
              id: "rollout",
              status: "pass",
              message: "Rollout server is ready.",
              command: "python -m openevo.rollout.server --config topology.yaml",
            },
          ],
        },
        status: servicesModel,
      });
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("Remote runtime services are ready");
    expect(buttonByText("Start Run").disabled).toBe(false);
    expect(apiMocks.fetchOpenEvoDesktopCapabilities).toHaveBeenCalledTimes(1);
    expect(apiMocks.fetchOpenEvoDesktopCapabilities).toHaveBeenLastCalledWith(
      "codex_subscription_transcript",
    );
    await unmountClient(root);
  });

  it("disables service start until bootstrap is ready", async () => {
    const shellModel = withBackendService(
      modelWithBootstrap({
        projectName: "Loaded Science Project",
        ready: false,
        notes: ["Remote bootstrap has not run yet."],
        bootstrapDetail: "Remote bootstrap has not run yet",
      }),
      {
        state: "planned",
        detail: "Remote runtime services have not started",
      },
    );
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);

    const root = await renderClient();
    await flushEffects();

    expect(buttonByText("Start Services").disabled).toBe(true);
    await unmountClient(root);
  });

  it("renders failed service steps from the services report", async () => {
    const shellModel = withBackendService(
      modelWithBootstrap({
        projectName: "Loaded Science Project",
        ready: true,
        notes: ["Remote bootstrap is ready."],
        bootstrapDetail: "Runtime image and manifests prepared",
      }),
      {
        state: "planned",
        detail: "Remote runtime services have not started",
      },
    );
    const failedModel = withBackendService(shellModel, {
      state: "blocked",
      detail: "rollout failed",
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.runOpenEvoServices.mockResolvedValue({
      services: {
        ready: false,
        state_root: "/home/alice/.openevo/runs/protein/folding",
        topology_path:
          "/home/alice/.openevo/runs/protein/folding/services/topology.yaml",
      },
      report: {
        ready: false,
        next_actions: ["Fix remote service failure and restart services."],
        steps: [
          {
            id: "rollout",
            status: "fail",
            message: "Rollout server failed to start.",
            command: "python -m openevo.rollout.server --config topology.yaml",
            stderr: "rollout failed",
          },
        ],
      },
      status: failedModel,
    });

    const root = await renderClient();
    await flushEffects();

    await act(async () => {
      buttonByText("Start Services").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("Services Report");
    expect(document.body.textContent).toContain(
      "Fix remote service failure and restart services.",
    );
    expect(document.body.textContent).toContain("rollout");
    expect(document.body.textContent).toContain("python -m openevo.rollout.server --config topology.yaml");
    expect(document.body.textContent).toContain("rollout failed");
    expect(buttonByText("Start Run").disabled).toBe(true);
    await unmountClient(root);
  });

  it("keeps the bootstrap report when workspace sync runs", async () => {
    const shellModel = modelWithBootstrap({
      projectName: "Loaded Science Project",
      ready: false,
      notes: ["Remote bootstrap has not run yet."],
      bootstrapDetail: "Remote bootstrap has not run yet",
    });
    const syncedModel = modelWithWorkspace({
      projectName: "Loaded Science Project",
      state: "ready",
      detail: "Workspace prepared",
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.runOpenEvoBootstrap.mockResolvedValue({
      bootstrap: shellModel.bootstrap,
      report: {
        ready: false,
        next_actions: ["Resolve failed bootstrap steps and rerun."],
      },
      status: shellModel,
    });
    apiMocks.runOpenEvoWorkspaceSync.mockResolvedValue({
      workspace: { ready: true, actions: [] },
      report: { ready: true },
      status: syncedModel,
    });

    const root = await renderClient();
    await flushEffects();

    await act(async () => {
      buttonByText("Bootstrap").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });
    expect(document.body.textContent).toContain("Bootstrap Report");

    await act(async () => {
      buttonByText("Sync Workspace").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("Workspace prepared");
    expect(document.body.textContent).toContain("Bootstrap Report");
    expect(document.body.textContent).toContain(
      "Resolve failed bootstrap steps and rerun.",
    );
    await unmountClient(root);
  });

  it("clears lifecycle reports when a new project config is saved", async () => {
    const shellModel = modelWithBootstrap({
      projectName: "Loaded Science Project",
      ready: false,
      notes: ["Remote bootstrap has not run yet."],
      bootstrapDetail: "Remote bootstrap has not run yet",
    });
    const savedModel = {
      ...shellModel,
      project: {
        ...shellModel.project,
        name: "Configured Science Project",
      },
    };
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.runOpenEvoBootstrap.mockResolvedValue({
      bootstrap: shellModel.bootstrap,
      report: {
        ready: false,
        next_actions: ["Fix remote preflight failures and rerun bootstrap."],
      },
      status: shellModel,
    });
    apiMocks.saveOpenEvoProjectConfig.mockResolvedValue({
      config: {
        science_config_path:
          "/home/alice/.openevo/desktop/projects/configured/science.yaml",
        remote_profile_path:
          "/home/alice/.openevo/desktop/profiles/science-team.yaml",
      },
      status: savedModel,
    });

    const root = await renderClient();
    await flushEffects();

    await act(async () => {
      buttonByText("Bootstrap").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });
    expect(document.body.textContent).toContain("Bootstrap Report");

    await act(async () => {
      buttonByText("Save Config").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("Configured Science Project");
    expect(document.body.textContent).not.toContain("Bootstrap Report");
    expect(document.body.textContent).not.toContain(
      "Fix remote preflight failures and rerun bootstrap.",
    );
    await unmountClient(root);
  });

  it("saves project config from the setup form and refreshes visible status", async () => {
    const shellModel = getOpenEvoDesktopShellModel();
    const savedModel = {
      ...shellModel,
      project: {
        ...shellModel.project,
        name: "Configured Science Project",
      },
      remote: {
        ...shellModel.remote,
        host: "configured.gpu.example.edu",
        port: 2222,
      },
    };
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    const deferred = deferProjectConfig(savedModel);
    apiMocks.saveOpenEvoProjectConfig.mockReturnValue(deferred.promise);

    const root = await renderClient();
    await flushEffects();

    await changeInput("Project name", "Configured Science Project");
    await changeInput("Remote host", "configured.gpu.example.edu");
    await changeInput("Remote port", "2222");

    const button = buttonByText("Save Config");
    expect(button.disabled).toBe(false);

    await act(async () => {
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(apiMocks.saveOpenEvoProjectConfig).toHaveBeenCalledWith(
      expect.objectContaining({
        project_name: "Configured Science Project",
        remote_host: "configured.gpu.example.edu",
        remote_port: 2222,
        source_type: "scratch",
        evolution: {
          targets: shellModel.project.evolutionTargets,
        },
      }),
    );
    expect(document.body.textContent).toContain("Saving");
    expect(projectConfigFieldset().disabled).toBe(true);

    await act(async () => {
      deferred.resolve({
        config: {
          science_config_path:
            "/home/alice/.openevo/desktop/projects/configured/science.yaml",
          remote_profile_path:
            "/home/alice/.openevo/desktop/profiles/science-team.yaml",
        },
        status: savedModel,
      });
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("Configured Science Project");
    expect(document.body.textContent).toContain("configured.gpu.example.edu");
    expect(inputByLabel("Remote port").value).toBe("2222");
    expect(projectConfigFieldset().disabled).toBe(false);
    await unmountClient(root);
  });

  it("adopts the canonical sidecar draft after save normalization", async () => {
    const shellModel = withBackendService(getOpenEvoDesktopShellModel(), {
      state: "ready",
      detail: "Remote runtime services are ready",
    });
    const savedModel = {
      ...shellModel,
      project: {
        ...shellModel.project,
        name: "Normalized Science Project",
      },
    };
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.saveOpenEvoProjectConfig.mockResolvedValue({
      config: {
        science_config_path: "/tmp/normalized/science.yaml",
        remote_profile_path: "/tmp/normalized/remote.yaml",
      },
      status: savedModel,
    });

    const root = await renderClient();
    await flushEffects();
    await changeInput("Project name", "Normalized Science Project  ");
    await act(async () => {
      buttonByText("Save Config").click();
      await Promise.resolve();
    });
    await flushEffects();

    expect(inputByLabel("Project name").value).toBe(
      "Normalized Science Project",
    );
    expect(document.body.textContent).not.toContain(
      "Save configuration changes before starting a run.",
    );
    expect(buttonByText("Start Run").disabled).toBe(false);
    await unmountClient(root);
  });

  it("round-trips complete remote setup fields through the setup form", async () => {
    const shellModel = modelWithRemoteSetup();
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.saveOpenEvoProjectConfig.mockResolvedValue({
      config: {
        science_config_path:
          "/home/alice/.openevo/desktop/projects/protein-design/science.yaml",
        remote_profile_path:
          "/home/alice/.openevo/desktop/profiles/science-team.yaml",
      },
      status: shellModel,
    });

    const root = await renderClient();
    await flushEffects();

    expect(inputByLabel("Remote profile ID").value).toBe("science-team");
    expect(inputByLabel("Remote port").value).toBe("2222");
    expect(selectByLabel("Auth method").value).toBe("private_key");
    expect(inputByLabel("Private key path").value).toBe(
      "/home/alice/.ssh/openevo",
    );
    expect(inputByLabel("Passphrase ref").value).toBe(
      "keyring://openevo/science-team",
    );
    expect(inputByLabel("Workspace root").value).toBe(
      "/data/openevo/workspaces",
    );
    expect(inputByLabel("HTTP proxy").value).toBe("http://127.0.0.1:7890");
    expect(inputByLabel("HTTPS proxy").value).toBe("http://127.0.0.1:7891");
    expect(inputByLabel("NO_PROXY").value).toBe("localhost,127.0.0.1");
    expect(inputByLabel("PIP index URL").value).toBe(
      "https://pypi.tuna.tsinghua.edu.cn/simple",
    );
    expect(document.body.textContent).not.toContain("HF home");

    await changeInput("HTTP proxy", "http://127.0.0.1:1080");

    await act(async () => {
      buttonByText("Save Config").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });

    expect(apiMocks.saveOpenEvoProjectConfig).toHaveBeenCalledWith(
      expect.objectContaining({
        remote_profile_id: "science-team",
        remote_port: 2222,
        auth_method: "private_key",
        private_key_path: "/home/alice/.ssh/openevo",
        passphrase_ref: "keyring://openevo/science-team",
        workspace_root: "/data/openevo/workspaces",
        http_proxy: "http://127.0.0.1:1080",
        https_proxy: "http://127.0.0.1:7891",
        no_proxy: "localhost,127.0.0.1",
        pip_index_url: "https://pypi.tuna.tsinghua.edu.cn/simple",
        huggingface_endpoint: "https://hf-mirror.com",
        hf_home: "/data/hf-cache",
      }),
    );
    await unmountClient(root);
  });

  it("round-trips self-deployed setup through the setup form", async () => {
    const shellModel = modelWithSelfDeployedInference();
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.saveOpenEvoProjectConfig.mockResolvedValue({
      config: {
        science_config_path:
          "/home/alice/.openevo/desktop/projects/protein-design/science.yaml",
        remote_profile_path:
          "/home/alice/.openevo/desktop/profiles/science-team.yaml",
      },
      status: shellModel,
    });

    const root = await renderClient();
    await flushEffects();

    expect(selectByLabel("Execution mode").value).toBe("self-deployed");
    expect(inputByLabel("HF model").value).toBe(
      "Qwen/Qwen3-Coder-30B-A3B-Instruct",
    );
    expect(inputByLabel("Hugging Face endpoint").value).toBe("https://hf-mirror.com");
    expect(inputByLabel("HF home").value).toBe("");
    expect(document.body.textContent).toContain("HF model");
    expect(document.body.textContent).toContain("transcript evolution");

    await changeInput("HF model", "Qwen/Qwen2.5-7B-Instruct");
    await changeInput("HF home", "/mnt/models/hf-cache");
    await act(async () => {
      buttonByText("Save Config").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });

    expect(apiMocks.saveOpenEvoProjectConfig).toHaveBeenCalledWith(
      expect.objectContaining({
        execution_mode: "self-deployed",
        codex_model: null,
        hf_model: "Qwen/Qwen2.5-7B-Instruct",
        hf_home: "/mnt/models/hf-cache",
      }),
    );
    await unmountClient(root);
  });

  it("renders evolution target controls from desktop capabilities", async () => {
    const shellModel = withBackendService(getOpenEvoDesktopShellModel(), {
      state: "ready",
      detail: "Remote runtime services are ready",
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.fetchOpenEvoDesktopCapabilities.mockResolvedValue(
      desktopCapabilities(),
    );

    const root = await renderClient();
    await flushEffects();

    const targetLabels = Array.from(
      document.querySelectorAll('[data-testid="evolution-target"]'),
    ).map((item) => item.querySelector("input")?.getAttribute("aria-label"));
    expect(targetLabels).toEqual([
      "Text memory",
      "Skill bundle",
      "Agent system",
    ]);
    expect(document.body.textContent).not.toContain("Parametric memory");
    expect(apiMocks.fetchOpenEvoDesktopCapabilities).toHaveBeenCalledWith(
      "codex_subscription_transcript",
    );
    expect(
      apiMocks.fetchOpenEvoDesktopShellModel.mock.invocationCallOrder[0],
    ).toBeLessThan(
      apiMocks.fetchOpenEvoDesktopCapabilities.mock.invocationCallOrder[0],
    );
    await unmountClient(root);
  });

  it("shows unavailable method reasons and exposes config disclosure semantics", async () => {
    const shellModel = withBackendService(getOpenEvoDesktopShellModel(), {
      state: "ready",
      detail: "Remote runtime services are ready",
    });
    const capabilities = desktopCapabilities();
    capabilities.targets[0].methods.push({
      ...capabilities.targets[0].methods[0],
      methodId: "gpu_only_memory",
      displayName: "GPU-only memory",
      description: "Uses the accelerated synthesis path.",
      support: unsupportedSupport(
        "missing_runtime_requirement",
        "GPU synthesis runtime is unavailable.",
      ),
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.fetchOpenEvoDesktopCapabilities.mockResolvedValue(capabilities);

    const root = await renderClient();
    await flushEffects();

    expect(document.body.textContent).toContain(
      "GPU synthesis runtime is unavailable.",
    );
    const configure = buttonByLabel("Configure Text memory");
    expect(configure.getAttribute("aria-expanded")).toBe("false");
    expect(configure.getAttribute("aria-controls")).toBe(
      "evolution-config-text_memory",
    );
    await act(async () => configure.click());
    expect(configure.getAttribute("aria-expanded")).toBe("true");
    expect(
      document.getElementById("evolution-config-text_memory"),
    ).not.toBeNull();
    await unmountClient(root);
  });

  it("selects a remote evolution method and saves its declared default config", async () => {
    const shellModel = withBackendService(getOpenEvoDesktopShellModel(), {
      state: "ready",
      detail: "Remote runtime services are ready",
    });
    const capabilities = desktopCapabilities();
    const target = capabilities.targets[0];
    target.methods.push({
      ...target.methods[0],
      methodId: "evidence_distillation",
      displayName: "Evidence distillation",
      description: "Distill reusable evidence from prior trajectories.",
      configSchemaJson:
        '{"additionalProperties":false,"properties":{"record_limit":{"maximum":20,"minimum":1,"title":"Record limit","type":"integer"}},"type":"object"}',
      defaultConfigJson: '{"record_limit":8}',
      configSchema: parseEvolutionConfigSchema({
        type: "object",
        additionalProperties: false,
        properties: {
          record_limit: {
            type: "integer",
            title: "Record limit",
            minimum: 1,
            maximum: 20,
          },
        },
      }),
      defaultConfig: { record_limit: 8 },
    });
    target.acceptedMethods.push({
      methodId: "evidence_distillation",
      implementationIdentityDigest: "f".repeat(64),
      support: supportedSupport(),
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.fetchOpenEvoDesktopCapabilities.mockResolvedValue(capabilities);
    apiMocks.saveOpenEvoProjectConfig.mockResolvedValue({
      config: {
        science_config_path: "/tmp/science.yaml",
        remote_profile_path: "/tmp/remote.yaml",
      },
      status: shellModel,
    });

    const root = await renderClient();
    await flushEffects();

    await changeSelect("Text memory method", "evidence_distillation");
    expect(inputByLabel("Record limit").value).toBe("8");
    await act(async () => {
      buttonByText("Save Config").click();
      await Promise.resolve();
    });

    expect(
      apiMocks.saveOpenEvoProjectConfig.mock.calls[0]?.[0].evolution.targets
        .text_memory,
    ).toEqual({
      enabled: true,
      method: "evidence_distillation",
      config: { record_limit: 8 },
    });
    await unmountClient(root);
  });

  it("blocks save for an invalid method config and saves the repaired value", async () => {
    const shellModel = withBackendService(getOpenEvoDesktopShellModel(), {
      state: "ready",
      detail: "Remote runtime services are ready",
    });
    shellModel.project.evolutionTargets.text_memory = {
      enabled: true,
      method: "text_memory_reflector",
      config: { record_limit: 4 },
    };
    const capabilities = desktopCapabilities();
    capabilities.targets[0].methods[0].configSchema =
      parseEvolutionConfigSchema({
        type: "object",
        additionalProperties: false,
        properties: {
          record_limit: {
            type: "integer",
            title: "Record limit",
            minimum: 1,
            maximum: 10,
          },
        },
      });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.fetchOpenEvoDesktopCapabilities.mockResolvedValue(capabilities);
    apiMocks.saveOpenEvoProjectConfig.mockResolvedValue({
      config: {
        science_config_path: "/tmp/science.yaml",
        remote_profile_path: "/tmp/remote.yaml",
      },
      status: shellModel,
    });

    const root = await renderClient();
    await flushEffects();

    await act(async () => buttonByLabel("Configure Text memory").click());
    await changeInput("Record limit", "12");
    expect(document.body.textContent).toContain("must be at most 10");
    expect(buttonByText("Save Config").disabled).toBe(true);

    await act(async () => inputByLabel("Text memory").click());
    expect(buttonByText("Save Config").disabled).toBe(false);
    await act(async () => inputByLabel("Text memory").click());
    expect(buttonByText("Save Config").disabled).toBe(true);

    await changeInput("Record limit", "6");
    expect(buttonByText("Save Config").disabled).toBe(false);
    await act(async () => {
      buttonByText("Save Config").click();
      await Promise.resolve();
    });
    expect(
      apiMocks.saveOpenEvoProjectConfig.mock.calls[0]?.[0].evolution.targets
        .text_memory.config,
    ).toEqual({ record_limit: 6 });
    await unmountClient(root);
  });

  it("resets a stale unknown method override after the remote schema shrinks", async () => {
    const shellModel = withBackendService(getOpenEvoDesktopShellModel(), {
      state: "ready",
      detail: "Remote runtime services are ready",
    });
    shellModel.project.evolutionTargets.text_memory = {
      enabled: true,
      method: "text_memory_reflector",
      config: { removed_option: "legacy" },
    };
    const capabilities = desktopCapabilities();
    capabilities.targets[0].methods[0].configSchema =
      parseEvolutionConfigSchema({
        type: "object",
        additionalProperties: false,
        properties: {
          threshold: { type: "number", title: "Threshold" },
        },
      });
    capabilities.targets[0].methods[0].defaultConfig = { threshold: 0.5 };
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.fetchOpenEvoDesktopCapabilities.mockResolvedValue(capabilities);
    apiMocks.saveOpenEvoProjectConfig.mockResolvedValue({
      config: {
        science_config_path: "/tmp/science.yaml",
        remote_profile_path: "/tmp/remote.yaml",
      },
      status: shellModel,
    });

    const root = await renderClient();
    await flushEffects();

    expect(document.body.textContent).toContain("unknown property");
    await act(async () => inputByLabel("Skill bundle").click());
    expect(buttonByText("Save Config").disabled).toBe(true);
    const reset = buttonByLabel("Reset Text memory configuration");
    expect(reset.title).toBe("Reset Text memory configuration");
    await act(async () => reset.click());

    expect(document.body.textContent).not.toContain("unknown property");
    expect(buttonByText("Save Config").disabled).toBe(false);
    await act(async () => {
      buttonByText("Save Config").click();
      await Promise.resolve();
    });
    expect(
      apiMocks.saveOpenEvoProjectConfig.mock.calls[0]?.[0].evolution.targets
        .text_memory.config,
    ).toEqual({ threshold: 0.5 });
    await unmountClient(root);
  });

  it("requires the final merged method config before saving a new selection", async () => {
    const shellModel = withBackendService(getOpenEvoDesktopShellModel(), {
      state: "ready",
      detail: "Remote runtime services are ready",
    });
    const capabilities = desktopCapabilities();
    const target = capabilities.targets[0];
    target.methods.push({
      ...target.methods[0],
      methodId: "operator_memory",
      displayName: "Operator memory",
      configSchema: parseEvolutionConfigSchema({
        type: "object",
        additionalProperties: false,
        properties: {
          operator: { type: "string", title: "Operator", minLength: 1 },
        },
        required: ["operator"],
      }),
      defaultConfig: {},
    });
    target.acceptedMethods.push({
      methodId: "operator_memory",
      implementationIdentityDigest: "f".repeat(64),
      support: supportedSupport(),
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.fetchOpenEvoDesktopCapabilities.mockResolvedValue(capabilities);
    apiMocks.saveOpenEvoProjectConfig.mockResolvedValue({
      config: {
        science_config_path: "/tmp/science.yaml",
        remote_profile_path: "/tmp/remote.yaml",
      },
      status: shellModel,
    });

    const root = await renderClient();
    await flushEffects();
    await changeSelect("Text memory method", "operator_memory");

    expect(document.body.textContent).toContain("required property is missing");
    expect(buttonByText("Save Config").disabled).toBe(true);
    await changeInput("Operator", "reflect");
    expect(buttonByText("Save Config").disabled).toBe(false);

    await act(async () => {
      buttonByText("Save Config").click();
      await Promise.resolve();
    });
    expect(
      apiMocks.saveOpenEvoProjectConfig.mock.calls[0]?.[0].evolution.targets
        .text_memory.config,
    ).toEqual({ operator: "reflect" });
    await unmountClient(root);
  });

  it("preserves a supported Core-owned automatic method resolver", async () => {
    const shellModel = withBackendService(getOpenEvoDesktopShellModel(), {
      state: "ready",
      detail: "Remote runtime services are ready",
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.saveOpenEvoProjectConfig.mockResolvedValue({
      config: {
        science_config_path: "/tmp/science.yaml",
        remote_profile_path: "/tmp/remote.yaml",
      },
      status: shellModel,
    });

    const root = await renderClient();
    await flushEffects();

    expect(document.body.textContent).not.toContain(
      'Selected method "auto" is no longer available',
    );
    expect(buttonByText("Start Run").disabled).toBe(false);
    await act(async () => inputByLabel("Agent system").click());
    await act(async () => inputByLabel("Agent system").click());
    await act(async () => {
      buttonByText("Save Config").click();
      await Promise.resolve();
    });

    expect(
      apiMocks.saveOpenEvoProjectConfig.mock.calls[0]?.[0].evolution.targets
        .agent_system,
    ).toEqual({
      enabled: true,
      method: "auto",
      config: { target_path: "AGENTS.md" },
    });
    await unmountClient(root);
  });

  it("allows an enabled unsupported target to be disabled but not re-enabled", async () => {
    const shellModel = withBackendService(getOpenEvoDesktopShellModel(), {
      state: "ready",
      detail: "Remote runtime services are ready",
    });
    shellModel.project.evolutionTargets.text_memory = {
      enabled: true,
      method: "text_memory_reflector",
      config: {},
    };
    const capabilities = desktopCapabilities();
    capabilities.targets[0].effectiveDefaultMethodId = null;
    capabilities.targets[0].configuredDefaultSupport = unsupportedSupport(
      "unsupported_execution_mode",
      "Text memory is unavailable for this execution profile.",
    );
    capabilities.targets[0].methods[0].support =
      capabilities.targets[0].configuredDefaultSupport;
    capabilities.targets[0].acceptedMethods[0].support =
      capabilities.targets[0].configuredDefaultSupport;
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.fetchOpenEvoDesktopCapabilities.mockResolvedValue(capabilities);

    const root = await renderClient();
    await flushEffects();

    expect(inputByLabel("Text memory").checked).toBe(true);
    expect(inputByLabel("Text memory").disabled).toBe(false);
    expect(document.body.textContent).toContain(
      "Text memory is unavailable for this execution profile.",
    );
    await act(async () => inputByLabel("Text memory").click());
    expect(inputByLabel("Text memory").checked).toBe(false);
    expect(inputByLabel("Text memory").disabled).toBe(true);
    await unmountClient(root);
  });

  it("shows an enabled target missing from the remote registry and allows disabling it", async () => {
    const shellModel = withBackendService(getOpenEvoDesktopShellModel(), {
      state: "ready",
      detail: "Remote runtime services are ready",
    });
    shellModel.project.evolutionTargets.removed_target = {
      enabled: true,
      method: "removed_method",
      config: { retained: true },
    };
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.saveOpenEvoProjectConfig.mockResolvedValue({
      config: {
        science_config_path: "/tmp/science.yaml",
        remote_profile_path: "/tmp/remote.yaml",
      },
      status: shellModel,
    });

    const root = await renderClient();
    await flushEffects();

    expect(inputByLabel("Removed target").checked).toBe(true);
    expect(document.body.textContent).toContain(
      'Target "removed_target" is no longer available in the remote registry.',
    );
    expect(buttonByText("Start Run").disabled).toBe(true);

    await act(async () => inputByLabel("Removed target").click());
    expect(buttonByText("Start Run").disabled).toBe(true);
    expect(document.body.textContent).toContain(
      "Save configuration changes before starting a run.",
    );
    await act(async () => {
      buttonByText("Save Config").click();
      await Promise.resolve();
    });
    expect(
      apiMocks.saveOpenEvoProjectConfig.mock.calls[0]?.[0].evolution.targets
        .removed_target,
    ).toEqual({
      enabled: false,
      method: "removed_method",
      config: { retained: true },
    });
    await unmountClient(root);
  });

  it("blocks a missing selected method and resets it to the supported remote default", async () => {
    const shellModel = withBackendService(getOpenEvoDesktopShellModel(), {
      state: "ready",
      detail: "Remote runtime services are ready",
    });
    shellModel.project.evolutionTargets.text_memory = {
      enabled: true,
      method: "removed_memory_method",
      config: { stale: true },
    };
    const capabilities = desktopCapabilities();
    capabilities.targets[0].methods[0].defaultConfig = { threshold: 0.5 };
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.fetchOpenEvoDesktopCapabilities.mockResolvedValue(capabilities);
    apiMocks.saveOpenEvoProjectConfig.mockResolvedValue({
      config: {
        science_config_path: "/tmp/science.yaml",
        remote_profile_path: "/tmp/remote.yaml",
      },
      status: shellModel,
    });

    const root = await renderClient();
    await flushEffects();

    expect(document.body.textContent).toContain(
      'Selected method "removed_memory_method" is no longer available',
    );
    expect(buttonByText("Start Run").disabled).toBe(true);
    await act(async () => inputByLabel("Text memory").click());
    expect(inputByLabel("Text memory").disabled).toBe(false);
    await act(async () => inputByLabel("Text memory").click());
    await act(async () => {
      buttonByText("Save Config").click();
      await Promise.resolve();
    });

    expect(
      apiMocks.saveOpenEvoProjectConfig.mock.calls[0]?.[0].evolution.targets
        .text_memory,
    ).toEqual({
      enabled: true,
      method: "text_memory_reflector",
      config: { threshold: 0.5 },
    });
    await unmountClient(root);
  });

  it("allows an explicitly selected supported method when the default is unavailable", async () => {
    const shellModel = withBackendService(getOpenEvoDesktopShellModel(), {
      state: "ready",
      detail: "Remote runtime services are ready",
    });
    shellModel.project.evolutionTargets.text_memory = {
      enabled: false,
      method: "alternate_memory",
      config: { retained: true },
    };
    const capabilities = desktopCapabilities();
    capabilities.targets[0].effectiveDefaultMethodId = null;
    capabilities.targets[0].configuredDefaultSupport = unsupportedSupport(
      "missing_runtime_requirement",
      "The default method is unavailable.",
    );
    capabilities.targets[0].methods[0].support =
      capabilities.targets[0].configuredDefaultSupport;
    capabilities.targets[0].methods.push({
      ...capabilities.targets[0].methods[0],
      methodId: "alternate_memory",
      support: supportedSupport(),
    });
    capabilities.targets[0].acceptedMethods.push({
      methodId: "alternate_memory",
      implementationIdentityDigest: "f".repeat(64),
      support: supportedSupport(),
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.fetchOpenEvoDesktopCapabilities.mockResolvedValue(capabilities);
    apiMocks.saveOpenEvoProjectConfig.mockResolvedValue({
      config: {
        science_config_path: "/tmp/science.yaml",
        remote_profile_path: "/tmp/remote.yaml",
      },
      status: shellModel,
    });

    const root = await renderClient();
    await flushEffects();

    expect(inputByLabel("Text memory").disabled).toBe(false);
    expect(document.body.textContent).toContain(
      "The default method is unavailable.",
    );
    await act(async () => inputByLabel("Text memory").click());
    await act(async () => {
      buttonByText("Save Config").click();
      await Promise.resolve();
    });
    expect(
      apiMocks.saveOpenEvoProjectConfig.mock.calls[0]?.[0].evolution.targets
        .text_memory,
    ).toEqual({
      enabled: true,
      method: "alternate_memory",
      config: { retained: true },
    });
    await unmountClient(root);
  });

  it("uses the remote default config when enabling a null method selection", async () => {
    const shellModel = withBackendService(getOpenEvoDesktopShellModel(), {
      state: "ready",
      detail: "Remote runtime services are ready",
    });
    shellModel.project.evolutionTargets.text_memory = {
      enabled: false,
      method: null,
      config: { stale: true },
    };
    const capabilities = desktopCapabilities();
    capabilities.targets[0].methods[0].defaultConfig = { threshold: 0.25 };
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.fetchOpenEvoDesktopCapabilities.mockResolvedValue(capabilities);
    apiMocks.saveOpenEvoProjectConfig.mockResolvedValue({
      config: {
        science_config_path: "/tmp/science.yaml",
        remote_profile_path: "/tmp/remote.yaml",
      },
      status: shellModel,
    });

    const root = await renderClient();
    await flushEffects();
    await act(async () => inputByLabel("Text memory").click());
    await act(async () => {
      buttonByText("Save Config").click();
      await Promise.resolve();
    });

    expect(
      apiMocks.saveOpenEvoProjectConfig.mock.calls[0]?.[0].evolution.targets
        .text_memory,
    ).toEqual({
      enabled: true,
      method: "text_memory_reflector",
      config: { threshold: 0.25 },
    });
    await unmountClient(root);
  });

  it("blocks an enabled method unsupported by the current remote profile", async () => {
    const shellModel = withBackendService(getOpenEvoDesktopShellModel(), {
      state: "ready",
      detail: "Remote runtime services are ready",
    });
    shellModel.project.evolutionTargets.text_memory = {
      enabled: true,
      method: "experimental_memory",
      config: {},
    };
    const capabilities = desktopCapabilities();
    capabilities.targets[0].methods.push({
      ...capabilities.targets[0].methods[0],
      methodId: "experimental_memory",
      support: unsupportedSupport(
        "missing_runtime_requirement",
        "A required runtime is unavailable.",
      ),
    });
    capabilities.targets[0].acceptedMethods.push({
      methodId: "experimental_memory",
      implementationIdentityDigest: "f".repeat(64),
      support: unsupportedSupport(
        "missing_runtime_requirement",
        "A required runtime is unavailable.",
      ),
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.fetchOpenEvoDesktopCapabilities.mockResolvedValue(capabilities);

    const root = await renderClient();
    await flushEffects();

    expect(document.body.textContent).toContain(
      'Selected method "experimental_memory" is unsupported by the current remote profile.',
    );
    expect(document.body.textContent).toContain("A required runtime is unavailable.");
    expect(buttonByText("Start Run").disabled).toBe(true);
    expect(inputByLabel("Text memory").disabled).toBe(false);
    await unmountClient(root);
  });

  it("retries failed capabilities with the current execution mode", async () => {
    const shellModel = withBackendService(getOpenEvoDesktopShellModel(), {
      state: "ready",
      detail: "Remote runtime services are ready",
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.fetchOpenEvoDesktopCapabilities
      .mockRejectedValueOnce(new Error("remote registry unavailable"))
      .mockResolvedValueOnce(desktopCapabilities());

    const root = await renderClient();
    await flushEffects();

    expect(buttonByText("Start Run").disabled).toBe(true);
    expect(document.body.textContent).toContain("remote registry unavailable");
    await act(async () => buttonByText("Retry Capabilities").click());
    await flushEffects();

    expect(apiMocks.fetchOpenEvoDesktopCapabilities).toHaveBeenCalledTimes(2);
    expect(apiMocks.fetchOpenEvoDesktopCapabilities).toHaveBeenLastCalledWith(
      "codex_subscription_transcript",
    );
    expect(buttonByText("Start Run").disabled).toBe(false);
    await unmountClient(root);
  });

  it("round-trips a generic remote target through save, reload, and activation", async () => {
    const shellModel = withBackendService(getOpenEvoDesktopShellModel(), {
      state: "ready",
      detail: "Remote runtime services are ready",
    });
    const target = desktopCapabilityTarget(
      "quality_notes_external",
      "Quality notes",
      "synthesize_notes",
    );
    target.artifactType = "text_memory";
    target.methods[0].configSchema = parseEvolutionConfigSchema({
      type: "object",
      additionalProperties: false,
      properties: {
        style: { type: "string", title: "Style" },
        limits: {
          type: "object",
          title: "Limits",
          additionalProperties: false,
          properties: {
            records: {
              type: "integer",
              title: "Records",
              minimum: 1,
              maximum: 32,
            },
          },
          required: ["records"],
        },
      },
      required: ["style", "limits"],
    });
    target.methods[0].configSchemaJson =
      '{"additionalProperties":false,"properties":{"limits":{"additionalProperties":false,"properties":{"records":{"maximum":32,"minimum":1,"title":"Records","type":"integer"}},"required":["records"],"title":"Limits","type":"object"},"style":{"title":"Style","type":"string"}},"required":["style","limits"],"type":"object"}';
    target.methods[0].defaultConfig = {
      style: "concise",
      limits: { records: 8 },
    };
    target.methods[0].defaultConfigJson =
      '{"limits":{"records":8},"style":"concise"}';
    const savedSelection = {
      enabled: true,
      method: "synthesize_notes",
      config: { style: "concise", limits: { records: 12 } },
    };
    const savedModel = {
      ...shellModel,
      project: {
        ...shellModel.project,
        evolutionTargets: {
          ...shellModel.project.evolutionTargets,
          quality_notes_external: savedSelection,
        },
      },
    };
    const activatedModel = {
      ...savedModel,
      project: {
        ...savedModel.project,
        name: "Activated External Notes",
      },
    };
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.fetchOpenEvoDesktopCapabilities.mockResolvedValue({
      ...desktopCapabilities(),
      targets: [target],
    });
    apiMocks.saveOpenEvoProjectConfig.mockResolvedValue({
      config: {
        science_config_path: "/tmp/science.yaml",
        remote_profile_path: "/tmp/remote.yaml",
      },
      status: savedModel,
    });

    let root = await renderClient();
    await flushEffects();
    await act(async () => inputByLabel("Quality notes").click());
    await act(async () => buttonByLabel("Configure Quality notes").click());
    expect(inputByLabel("Records").value).toBe("8");
    await changeInput("Records", "12");
    await act(async () => {
      buttonByText("Save Config").click();
      await Promise.resolve();
    });

    expect(
      apiMocks.saveOpenEvoProjectConfig.mock.calls[0]?.[0].evolution.targets
        .quality_notes_external,
    ).toEqual(savedSelection);
    await unmountClient(root);

    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(savedModel);
    apiMocks.fetchOpenEvoProjectConfigs.mockResolvedValue(savedProjectConfigs());
    apiMocks.activateOpenEvoProjectConfig.mockResolvedValue({
      config: {
        science_config_path: "/tmp/science.yaml",
        remote_profile_path: "/tmp/remote.yaml",
      },
      status: activatedModel,
    });
    root = await renderClient();
    await flushEffects();

    expect(inputByLabel("Quality notes").checked).toBe(true);
    expect(selectByLabel("Quality notes method").value).toBe("synthesize_notes");
    await act(async () => buttonByLabel("Configure Quality notes").click());
    expect(inputByLabel("Style").value).toBe("concise");
    expect(inputByLabel("Records").value).toBe("12");

    await changeInput("Project name", "Unsaved local edit");
    expect(buttonByLabel("Activate Protein Design").disabled).toBe(true);
    expect(document.body.textContent).toContain(
      "Save or discard current changes before activating another config.",
    );
    await act(async () => buttonByText("Discard Changes").click());
    expect(inputByLabel("Project name").value).toBe(savedModel.project.name);
    expect(buttonByLabel("Activate Protein Design").disabled).toBe(false);
    await act(async () => {
      buttonByLabel("Activate Protein Design").click();
      await Promise.resolve();
    });
    expect(document.body.textContent).toContain("Activated External Notes");
    expect(inputByLabel("Quality notes").checked).toBe(true);
    await unmountClient(root);
  });

  it("shows remote capabilities as unavailable before the backend connects", async () => {
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(
      getOpenEvoDesktopShellModel(),
    );
    const root = await renderClient();
    await flushEffects();

    expect(apiMocks.fetchOpenEvoDesktopCapabilities).not.toHaveBeenCalled();
    expect(document.body.textContent).toContain("Remote capabilities unavailable");
    expect(document.querySelectorAll('[data-testid="evolution-target"]')).toHaveLength(3);
    expect(document.body.textContent).not.toContain(
      "is no longer available in the remote registry",
    );
    await unmountClient(root);
  });

  it("clears capabilities when a saved config changes the remote context", async () => {
    const connectedModel = withBackendService(getOpenEvoDesktopShellModel(), {
      state: "ready",
      detail: "Remote runtime services are ready",
    });
    const disconnectedModel = getOpenEvoDesktopShellModel();
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(connectedModel);
    apiMocks.saveOpenEvoProjectConfig.mockResolvedValue({
      config: {
        science_config_path: "/tmp/science.yaml",
        remote_profile_path: "/tmp/remote.yaml",
      },
      status: disconnectedModel,
    });

    const root = await renderClient();
    await flushEffects();
    expect(document.querySelectorAll('[data-testid="evolution-target"]')).toHaveLength(3);

    await act(async () => {
      buttonByText("Save Config").click();
      await Promise.resolve();
    });

    expect(document.querySelectorAll('[data-testid="evolution-target"]')).toHaveLength(3);
    expect(document.body.textContent).toContain("Remote capabilities unavailable");
    expect(apiMocks.fetchOpenEvoDesktopCapabilities).toHaveBeenCalledTimes(1);
    await unmountClient(root);
  });

  it("toggles only enabled while preserving complete evolution selections", async () => {
    const shellModel = withBackendService(getOpenEvoDesktopShellModel(), {
      state: "ready",
      detail: "Remote runtime services are ready",
    });
    shellModel.project.evolutionTargets = {
      text_memory: {
        enabled: true,
        method: "custom_memory_method",
        config: { threshold: 0.75 },
      },
      skill_bundle: {
        enabled: false,
        method: null,
        config: { draft_prompt: "retain me" },
      },
      future_target: {
        enabled: false,
        method: "future_method",
        config: { opaque: [1, 2, 3] },
      },
    };
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.saveOpenEvoProjectConfig.mockResolvedValue({
      config: {
        science_config_path: "/tmp/science.yaml",
        remote_profile_path: "/tmp/remote.yaml",
      },
      status: shellModel,
    });

    const root = await renderClient();
    await flushEffects();

    expect(inputByLabel("Text memory").checked).toBe(true);
    expect(inputByLabel("Skill bundle").checked).toBe(false);
    expect(inputByLabel("Agent system").checked).toBe(false);
    await act(async () => {
      inputByLabel("Text memory").click();
      inputByLabel("Skill bundle").click();
      inputByLabel("Agent system").click();
    });
    await act(async () => {
      buttonByText("Save Config").click();
      await Promise.resolve();
    });

    const submitted = apiMocks.saveOpenEvoProjectConfig.mock.calls[0]?.[0];
    expect(submitted.evolution.targets).toEqual({
      ...shellModel.project.evolutionTargets,
      text_memory: {
        ...shellModel.project.evolutionTargets.text_memory,
        enabled: false,
      },
      skill_bundle: {
        ...shellModel.project.evolutionTargets.skill_bundle,
        enabled: true,
        method: "skill_bundle_reflector",
        config: {},
      },
      agent_system: {
        enabled: true,
        method: "agent_system_reflector",
        config: {},
      },
    });
    expect(submitted).not.toHaveProperty("text_memory");
    expect(submitted).not.toHaveProperty("skill_bundle");
    expect(submitted).not.toHaveProperty("agent_system");
    await unmountClient(root);
  });

  it("switches subscription setup to self-deployed", async () => {
    const shellModel = getOpenEvoDesktopShellModel();
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.saveOpenEvoProjectConfig.mockResolvedValue({
      config: {
        science_config_path:
          "/home/alice/.openevo/desktop/projects/protein-design/science.yaml",
        remote_profile_path:
          "/home/alice/.openevo/desktop/profiles/science-team.yaml",
      },
      status: modelWithSelfDeployedInference(),
    });

    const root = await renderClient();
    await flushEffects();

    expect(selectByLabel("Execution mode").value).toBe(
      "codex_subscription_transcript",
    );
    expect(document.body.textContent).not.toContain("Codex model");

    await changeSelect("Execution mode", "self-deployed");
    expect(inputByLabel("HF model").value).toBe("");
    await changeInput("HF model", "Qwen/Qwen3-Coder-30B-A3B-Instruct");
    await act(async () => {
      buttonByText("Save Config").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });

    expect(apiMocks.saveOpenEvoProjectConfig).toHaveBeenCalledWith(
      expect.objectContaining({
        execution_mode: "self-deployed",
        codex_model: null,
        hf_model: "Qwen/Qwen3-Coder-30B-A3B-Instruct",
      }),
    );
    await unmountClient(root);
  });

  it("refreshes remote capabilities when execution mode changes on a ready backend", async () => {
    const shellModel = withBackendService(getOpenEvoDesktopShellModel(), {
      state: "ready",
      detail: "Remote runtime services are ready",
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);

    const root = await renderClient();
    await flushEffects();
    expect(apiMocks.fetchOpenEvoDesktopCapabilities).toHaveBeenCalledTimes(1);

    await changeSelect("Execution mode", "self-deployed");
    await flushEffects();

    expect(apiMocks.fetchOpenEvoDesktopCapabilities).toHaveBeenCalledTimes(2);
    expect(apiMocks.fetchOpenEvoDesktopCapabilities).toHaveBeenLastCalledWith(
      "self-deployed",
    );
    await unmountClient(root);
  });

  it("blocks evolution saves while matching remote capabilities are unavailable", async () => {
    const shellModel = withBackendService(getOpenEvoDesktopShellModel(), {
      state: "ready",
      detail: "Remote runtime services are ready",
    });
    const reload = deferCapabilities();
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.fetchOpenEvoDesktopCapabilities
      .mockResolvedValueOnce(desktopCapabilities())
      .mockReturnValueOnce(reload.promise);

    const root = await renderClient();
    await flushEffects();
    await act(async () => inputByLabel("Text memory").click());
    expect(buttonByText("Save Config").disabled).toBe(false);

    await changeSelect("Execution mode", "self-deployed");
    expect(buttonByText("Save Config").disabled).toBe(true);
    expect(document.body.textContent).toContain(
      "Wait for remote capabilities before saving evolution changes.",
    );

    await act(async () => {
      reload.reject(new Error("new backend registry unavailable"));
      await Promise.resolve();
    });
    expect(buttonByText("Save Config").disabled).toBe(true);
    expect(document.body.textContent).toContain("new backend registry unavailable");
    expect(apiMocks.saveOpenEvoProjectConfig).not.toHaveBeenCalled();
    await unmountClient(root);
  });

  it("refetches a previous mode after another mode fails", async () => {
    const shellModel = withBackendService(getOpenEvoDesktopShellModel(), {
      state: "ready",
      detail: "Remote runtime services are ready",
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.fetchOpenEvoDesktopCapabilities
      .mockResolvedValueOnce(desktopCapabilities())
      .mockRejectedValueOnce(new Error("self-deployed registry unavailable"))
      .mockResolvedValueOnce(desktopCapabilities());

    const root = await renderClient();
    await flushEffects();
    await changeSelect("Execution mode", "self-deployed");
    await flushEffects();
    expect(document.body.textContent).toContain(
      "self-deployed registry unavailable",
    );

    await changeSelect("Execution mode", "codex_subscription_transcript");
    await flushEffects();

    expect(apiMocks.fetchOpenEvoDesktopCapabilities).toHaveBeenCalledTimes(3);
    expect(apiMocks.fetchOpenEvoDesktopCapabilities).toHaveBeenLastCalledWith(
      "codex_subscription_transcript",
    );
    expect(document.body.textContent).not.toContain(
      "self-deployed registry unavailable",
    );
    expect(buttonByText("Start Run").disabled).toBe(true);
    expect(document.body.textContent).toContain(
      "Save configuration changes before starting a run.",
    );
    await unmountClient(root);
  });

  it("saves password reference auth from the setup form", async () => {
    const shellModel = getOpenEvoDesktopShellModel();
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.saveOpenEvoProjectConfig.mockResolvedValue({
      config: {
        science_config_path:
          "/home/alice/.openevo/desktop/projects/protein-design/science.yaml",
        remote_profile_path:
          "/home/alice/.openevo/desktop/profiles/science-team.yaml",
      },
      status: shellModel,
    });

    const root = await renderClient();
    await flushEffects();

    await changeSelect("Auth method", "password_ref");
    await changeInput("Password ref", "keyring://openevo/science-team");
    await act(async () => {
      buttonByText("Save Config").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });

    expect(apiMocks.saveOpenEvoProjectConfig).toHaveBeenCalledWith(
      expect.objectContaining({
        auth_method: "password_ref",
        private_key_path: null,
        password_ref: "keyring://openevo/science-team",
        passphrase_ref: null,
      }),
    );
    await unmountClient(root);
  });

  it("loads saved project configs and marks invalid configs read-only", async () => {
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(
      getOpenEvoDesktopShellModel(),
    );
    apiMocks.fetchOpenEvoProjectConfigs.mockResolvedValue(savedProjectConfigs());

    const root = await renderClient();
    await flushEffects();

    expect(document.body.textContent).toContain("Saved Configs");
    expect(document.body.textContent).toContain("Protein Design");
    expect(document.body.textContent).toContain("gpu.example.edu");
    expect(document.body.textContent).toContain("Broken Project");
    expect(document.body.textContent).toContain("profiles/broken.yaml: not found");
    expect(buttonByLabel("Activate Protein Design").disabled).toBe(false);
    expect(buttonByLabel("Activate Broken Project").disabled).toBe(true);
    await unmountClient(root);
  });

  it("activates a saved config and clears stale run state", async () => {
    const shellModel = withBackendService(getOpenEvoDesktopShellModel(), {
      state: "ready",
      detail: "Remote runtime services are ready",
    });
    const runningModel = modelWithRun({
      projectName: shellModel.project.name,
      backendState: "running",
      backendDetail: "OpenEvo run is running",
      transcriptState: "running",
      transcriptDetail: "Capturing transcript trajectory",
    });
    const activatedModel = {
      ...shellModel,
      project: {
        ...shellModel.project,
        name: "Activated Project",
      },
      remote: {
        ...shellModel.remote,
        host: "activated.gpu.example.edu",
      },
    };
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.fetchOpenEvoProjectConfigs.mockResolvedValue(savedProjectConfigs());
    apiMocks.runOpenEvoStartRun.mockResolvedValue({
      run: runStatus("running"),
      status: runningModel,
    });
    apiMocks.pollOpenEvoRunStatus.mockResolvedValue({
      run: runStatus("failed"),
      status: modelWithRun({
        projectName: shellModel.project.name,
        backendState: "blocked",
        backendDetail: "run failed",
        transcriptState: "blocked",
        transcriptDetail: "run failed",
      }),
    });
    apiMocks.activateOpenEvoProjectConfig.mockResolvedValue({
      config: {
        science_config_path:
          "/home/alice/.openevo/desktop/projects/protein-design/science.yaml",
        remote_profile_path:
          "/home/alice/.openevo/desktop/profiles/science-team.yaml",
      },
      status: activatedModel,
    });

    const root = await renderClient();
    await flushEffects();

    await act(async () => {
      buttonByText("Start Run").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });
    expect(document.body.textContent).toContain("Run Status");
    expect(document.body.textContent).toContain("failed");

    await act(async () => {
      buttonByLabel("Activate Protein Design").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });

    expect(apiMocks.activateOpenEvoProjectConfig).toHaveBeenCalledWith(
      "protein-design",
    );
    expect(document.body.textContent).toContain("Activated Project");
    expect(document.body.textContent).toContain("activated.gpu.example.edu");
    expect(document.body.textContent).not.toContain("Run Status");
    await unmountClient(root);
  });

  it("disables save while activating a saved config", async () => {
    const shellModel = getOpenEvoDesktopShellModel();
    const activatedModel = {
      ...shellModel,
      project: {
        ...shellModel.project,
        name: "Activated Project",
      },
    };
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.fetchOpenEvoProjectConfigs.mockResolvedValue(savedProjectConfigs());
    const activation = deferProjectConfig(activatedModel);
    apiMocks.activateOpenEvoProjectConfig.mockReturnValue(activation.promise);

    const root = await renderClient();
    await flushEffects();

    await act(async () => {
      buttonByLabel("Activate Protein Design").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });

    expect(buttonByText("Save Config").disabled).toBe(true);
    expect(projectConfigFieldset().disabled).toBe(true);

    await act(async () => {
      activation.resolve({
        config: {
          science_config_path:
            "/home/alice/.openevo/desktop/projects/protein-design/science.yaml",
          remote_profile_path:
            "/home/alice/.openevo/desktop/profiles/science-team.yaml",
        },
        status: activatedModel,
      });
      await Promise.resolve();
    });

    expect(buttonByText("Save Config").disabled).toBe(false);
    expect(projectConfigFieldset().disabled).toBe(false);
    await unmountClient(root);
  });

  it("refreshes saved configs after saving a project config", async () => {
    const shellModel = getOpenEvoDesktopShellModel();
    const savedModel = {
      ...shellModel,
      project: {
        ...shellModel.project,
        name: "Configured Science Project",
      },
    };
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.fetchOpenEvoProjectConfigs
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([
        {
          ...savedProjectConfigs()[0],
          projectSlug: "configured-science-project",
          projectName: "Configured Science Project",
        },
      ]);
    apiMocks.saveOpenEvoProjectConfig.mockResolvedValue({
      config: {
        science_config_path:
          "/home/alice/.openevo/desktop/projects/configured/science.yaml",
        remote_profile_path:
          "/home/alice/.openevo/desktop/profiles/science-team.yaml",
      },
      status: savedModel,
    });

    const root = await renderClient();
    await flushEffects();

    expect(document.body.textContent).not.toContain("Saved Configs");
    await act(async () => {
      buttonByText("Save Config").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });

    expect(apiMocks.fetchOpenEvoProjectConfigs).toHaveBeenCalledTimes(2);
    expect(document.body.textContent).toContain("Configured Science Project");
    await unmountClient(root);
  });

  it("keeps the newest saved config refresh when requests resolve out of order", async () => {
    const shellModel = getOpenEvoDesktopShellModel();
    const savedModel = {
      ...shellModel,
      project: {
        ...shellModel.project,
        name: "Configured Science Project",
      },
    };
    const initialCatalog = deferCatalog();
    const postSaveCatalog = deferCatalog();
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.fetchOpenEvoProjectConfigs
      .mockReturnValueOnce(initialCatalog.promise)
      .mockReturnValueOnce(postSaveCatalog.promise);
    apiMocks.saveOpenEvoProjectConfig.mockResolvedValue({
      config: {
        science_config_path:
          "/home/alice/.openevo/desktop/projects/configured/science.yaml",
        remote_profile_path:
          "/home/alice/.openevo/desktop/profiles/science-team.yaml",
      },
      status: savedModel,
    });

    const root = await renderClient();
    await flushEffects();

    await act(async () => {
      buttonByText("Save Config").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });

    await act(async () => {
      postSaveCatalog.resolve([
        {
          ...savedProjectConfigs()[0],
          projectSlug: "configured-science-project",
          projectName: "Configured Science Project",
        },
      ]);
      await Promise.resolve();
    });
    expect(document.body.textContent).toContain("Configured Science Project");

    await act(async () => {
      initialCatalog.resolve([]);
      await Promise.resolve();
    });
    expect(document.body.textContent).toContain("Configured Science Project");
    await unmountClient(root);
  });

  it("runs workspace sync from the button and refreshes visible status", async () => {
    const shellModel = modelWithWorkspace({
      projectName: "Loaded Science Project",
      state: "planned",
      detail: "Workspace preparation has not run yet",
    });
    const syncedModel = modelWithWorkspace({
      projectName: "Loaded Science Project",
      state: "ready",
      detail: "Workspace prepared",
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    const deferred = deferWorkspace(syncedModel);
    apiMocks.runOpenEvoWorkspaceSync.mockReturnValue(deferred.promise);

    const root = await renderClient();
    await flushEffects();

    expect(document.body.textContent).toContain("Loaded Science Project");
    expect(document.body.textContent).toContain(
      "Workspace preparation has not run yet",
    );

    const button = buttonByText("Sync Workspace");
    expect(button.disabled).toBe(false);

    await act(async () => {
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(apiMocks.runOpenEvoWorkspaceSync).toHaveBeenCalledTimes(1);
    expect(document.body.textContent).toContain("Syncing");

    await act(async () => {
      deferred.resolve({
        workspace: { ready: true, actions: [] },
        report: { ready: true },
        status: syncedModel,
      });
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("Workspace prepared");
    await unmountClient(root);
  });

  it("blocks lifecycle actions when SSH password refs are unsupported", async () => {
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(
      modelWithUnsupportedSshAuth({
        auth: {
          method: "password_ref",
          privateKeyPath: null,
          passwordRef: "keyring://openevo/science-team",
          passphraseRef: null,
        },
      }),
    );

    const root = await renderClient();
    await flushEffects();

    expect(document.body.textContent).toContain(
      "SSH transport cannot resolve password_ref yet",
    );
    expect(buttonByText("Sync Workspace").disabled).toBe(true);
    expect(buttonByText("Bootstrap").disabled).toBe(true);
    expect(buttonByText("Start Run").disabled).toBe(true);

    await act(async () => {
      buttonByText("Sync Workspace").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      buttonByText("Bootstrap").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      buttonByText("Start Run").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });

    expect(apiMocks.runOpenEvoWorkspaceSync).not.toHaveBeenCalled();
    expect(apiMocks.runOpenEvoBootstrap).not.toHaveBeenCalled();
    expect(apiMocks.runOpenEvoStartRun).not.toHaveBeenCalled();
    await unmountClient(root);
  });

  it("blocks lifecycle actions when SSH passphrase refs are unsupported", async () => {
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(
      modelWithUnsupportedSshAuth({
        auth: {
          method: "private_key",
          privateKeyPath: "/home/alice/.ssh/openevo",
          passwordRef: null,
          passphraseRef: "keyring://openevo/science-team",
        },
      }),
    );

    const root = await renderClient();
    await flushEffects();

    expect(document.body.textContent).toContain(
      "SSH transport cannot resolve passphrase_ref yet",
    );
    expect(buttonByText("Sync Workspace").disabled).toBe(true);
    expect(buttonByText("Bootstrap").disabled).toBe(true);
    expect(buttonByText("Start Run").disabled).toBe(true);
    await unmountClient(root);
  });

  it("renders failed workspace actions from the workspace report", async () => {
    const shellModel = modelWithWorkspace({
      projectName: "Loaded Science Project",
      state: "planned",
      detail: "Workspace preparation has not run yet",
    });
    const failedModel = modelWithWorkspace({
      projectName: "Loaded Science Project",
      state: "blocked",
      detail: "Workspace preparation failed",
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    const deferred = deferWorkspace(failedModel);
    apiMocks.runOpenEvoWorkspaceSync.mockReturnValue(deferred.promise);

    const root = await renderClient();
    await flushEffects();

    await act(async () => {
      buttonByText("Sync Workspace").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
    });
    await act(async () => {
      deferred.resolve({
        workspace: { ready: false, actions: [] },
        report: {
          ready: false,
          workspace: {
            actions: [
              {
                type: "upload_dir",
                status: "fail",
                message: "Local folder upload failed.",
                target:
                  "/home/alice/.openevo/workspaces/protein/folding/abcdef",
                stderr: "upload failed",
              },
            ],
          },
        },
        status: failedModel,
      });
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("Workspace Report");
    expect(document.body.textContent).toContain("upload_dir");
    expect(document.body.textContent).toContain("Local folder upload failed.");
    expect(document.body.textContent).toContain("upload failed");
    await unmountClient(root);
  });

  it("starts a run from the button and refreshes visible status", async () => {
    const shellModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "ready",
      backendDetail: "Remote runtime services are ready",
      transcriptState: "planned",
      transcriptDetail: "Trajectory capture will start after the first run",
    });
    const runningModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "running",
      backendDetail: "OpenEvo run is running",
      transcriptState: "running",
      transcriptDetail: "Capturing transcript trajectory",
    });
    const ranModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "ready",
      backendDetail: "Last run completed",
      transcriptState: "complete",
      transcriptDetail: "Run completed and transcript captured",
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    const launch = deferRun(runningModel, runStatus("running"));
    const poll = deferRun(ranModel, runStatus("succeeded"));
    apiMocks.runOpenEvoStartRun.mockReturnValue(launch.promise);
    apiMocks.pollOpenEvoRunStatus.mockReturnValue(poll.promise);

    const root = await renderClient();
    await flushEffects();

    expect(document.body.textContent).toContain("Loaded Science Project");
    expect(document.body.textContent).toContain(
      "Remote runtime services are ready",
    );

    const button = buttonByText("Start Run");
    expect(button.disabled).toBe(false);

    await act(async () => {
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(apiMocks.runOpenEvoStartRun).toHaveBeenCalledTimes(1);
    expect(document.body.textContent).toContain("Running");

    await act(async () => {
      launch.resolve({
        run: runStatus("running"),
        status: runningModel,
      });
      await Promise.resolve();
    });

    expect(apiMocks.pollOpenEvoRunStatus).toHaveBeenCalledTimes(1);
    expect(document.body.textContent).toContain("running");

    await act(async () => {
      poll.resolve({
        run: runStatus("succeeded"),
        status: ranModel,
      });
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("succeeded");
    expect(document.body.textContent).toContain("Last run completed");
    expect(document.body.textContent).toContain(
      "Run completed and transcript captured",
    );
    await unmountClient(root);
  });

  it("loads and renders the artifact timeline after a terminal run", async () => {
    const shellModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "ready",
      backendDetail: "Remote runtime services are ready",
      transcriptState: "planned",
      transcriptDetail: "Trajectory capture will start after the first run",
    });
    const runningModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "running",
      backendDetail: "OpenEvo run is running",
      transcriptState: "running",
      transcriptDetail: "Capturing transcript trajectory",
    });
    const ranModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "ready",
      backendDetail: "Last run completed",
      transcriptState: "complete",
      transcriptDetail: "Run completed and transcript captured",
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    const launch = deferRun(runningModel, runStatus("running"));
    const poll = deferRun(ranModel, runStatus("succeeded"));
    apiMocks.runOpenEvoStartRun.mockReturnValue(launch.promise);
    apiMocks.pollOpenEvoRunStatus.mockReturnValue(poll.promise);
    apiMocks.fetchOpenEvoBackendRunTimeline.mockResolvedValue(runTimeline());
    apiMocks.fetchOpenEvoBackendRunArtifacts.mockResolvedValue(runArtifacts());
    apiMocks.fetchOpenEvoBackendArtifactPreview.mockResolvedValue(
      artifactPreview(),
    );

    const root = await renderClient();
    await flushEffects();

    await act(async () => {
      buttonByText("Start Run").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
    });
    await act(async () => {
      launch.resolve({
        run: runStatus("running"),
        status: runningModel,
      });
      await Promise.resolve();
    });
    await act(async () => {
      poll.resolve({
        run: runStatus("succeeded"),
        status: ranModel,
      });
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(apiMocks.fetchOpenEvoBackendRunTimeline).toHaveBeenCalledWith(
      "run_20260707170000000000",
    );
    expect(apiMocks.fetchOpenEvoBackendRunArtifacts).toHaveBeenCalledWith(
      "run_20260707170000000000",
    );
    expect(apiMocks.fetchOpenEvoBackendArtifactPreview).toHaveBeenCalledWith(
      "artifact-text-memory",
    );
    expect(document.body.textContent).toContain("Run Artifact Timeline");
    expect(document.body.textContent).toContain("Memory updated");
    expect(document.body.textContent).toContain("Initial memory draft");
    expect(document.body.textContent).toContain("Text memory");
    expect(document.body.textContent).toContain("Learned Memory");
    expect(document.body.textContent).toContain("Promoted");
    await unmountClient(root);
  });

  it("clears the previous backend timeline while launching another run", async () => {
    const shellModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "ready",
      backendDetail: "Remote runtime services are ready",
      transcriptState: "planned",
      transcriptDetail: "Trajectory capture will start after the first run",
    });
    const runningModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "running",
      backendDetail: "OpenEvo run is running",
      transcriptState: "running",
      transcriptDetail: "Capturing transcript trajectory",
    });
    const ranModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "ready",
      backendDetail: "Last run completed",
      transcriptState: "complete",
      transcriptDetail: "Run completed and transcript captured",
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    const secondLaunch = deferRun(runningModel, runStatus("running"));
    apiMocks.runOpenEvoStartRun
      .mockResolvedValueOnce({
        run: runStatus("succeeded"),
        status: ranModel,
      })
      .mockReturnValueOnce(secondLaunch.promise);
    apiMocks.fetchOpenEvoBackendRunTimeline.mockResolvedValue(runTimeline());
    apiMocks.fetchOpenEvoBackendRunArtifacts.mockResolvedValue(runArtifacts());
    apiMocks.fetchOpenEvoBackendArtifactPreview.mockResolvedValue(artifactPreview());

    const root = await renderClient();
    await flushEffects();

    await act(async () => {
      buttonByText("Start Run").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(document.body.textContent).toContain("Memory updated");

    await act(async () => {
      buttonByText("Start Run").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });

    expect(document.body.textContent).not.toContain("Memory updated");
    await unmountClient(root);
  });

  it("does not expose raw diagnostics for ordinary users", async () => {
    const shellModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "ready",
      backendDetail: "Remote runtime services are ready",
      transcriptState: "planned",
      transcriptDetail: "Trajectory capture will start after the first run",
    });
    const ranModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "ready",
      backendDetail: "Last run completed",
      transcriptState: "complete",
      transcriptDetail: "Run completed and transcript captured",
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    const diagnosticRun = {
      ...runStatus("succeeded"),
      stdout: "stdout secret payload",
      stderr: "stderr secret payload",
    };
    apiMocks.runOpenEvoStartRun.mockResolvedValue({
      run: diagnosticRun,
      status: ranModel,
    });
    apiMocks.fetchOpenEvoBackendRunTimeline.mockResolvedValue(runTimeline());
    apiMocks.fetchOpenEvoBackendRunArtifacts.mockResolvedValue(runArtifacts());
    apiMocks.fetchOpenEvoBackendArtifactPreview.mockResolvedValue(
      artifactPreview({
        body: "# Learned Memory\n",
      }),
    );

    const root = await renderClient();
    await flushEffects();

    await act(async () => {
      buttonByText("Start Run").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(document.body.textContent).not.toContain("Diagnostics");
    expect(document.body.textContent).not.toContain("run_20260707170000000000");
    expect(document.body.textContent).not.toContain(
      "/home/alice/.openevo/runs/protein/folding/runs/run_20260707170000000000",
    );
    expect(document.body.textContent).not.toContain("stdout secret payload");
    expect(document.body.textContent).not.toContain("stderr secret payload");
    expect(document.body.textContent).not.toContain("text_memory_reflector");
    expect(document.body.textContent).not.toContain("artifact-text-memory");
    await unmountClient(root);
  });

  it("hides raw run and artifact diagnostics until developer Diagnostics is opened", async () => {
    const shellModel = withDeveloperMode(
      modelWithRun({
        projectName: "Loaded Science Project",
        backendState: "ready",
        backendDetail: "Remote runtime services are ready",
        transcriptState: "planned",
        transcriptDetail: "Trajectory capture will start after the first run",
      }),
    );
    const ranModel = withDeveloperMode(
      modelWithRun({
        projectName: "Loaded Science Project",
        backendState: "ready",
        backendDetail: "Last run completed",
        transcriptState: "complete",
        transcriptDetail: "Run completed and transcript captured",
      }),
    );
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    const diagnosticRun = {
      ...runStatus("succeeded"),
      stdout: "stdout secret payload",
      stderr: "stderr secret payload",
    };
    apiMocks.runOpenEvoStartRun.mockResolvedValue({
      run: diagnosticRun,
      status: ranModel,
    });
    apiMocks.fetchOpenEvoBackendRunTimeline.mockResolvedValue(runTimeline());
    apiMocks.fetchOpenEvoBackendRunArtifacts.mockResolvedValue(runArtifacts());
    apiMocks.fetchOpenEvoBackendArtifactPreview.mockResolvedValue(
      artifactPreview({
        body: "# Learned Memory\n",
      }),
    );

    const root = await renderClient();
    await flushEffects();

    await act(async () => {
      buttonByText("Start Run").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("Diagnostics");
    expect(document.body.textContent).not.toContain("run_20260707170000000000");
    expect(document.body.textContent).not.toContain(
      "/home/alice/.openevo/runs/protein/folding/runs/run_20260707170000000000",
    );
    expect(document.body.textContent).not.toContain("stdout secret payload");
    expect(document.body.textContent).not.toContain("stderr secret payload");
    expect(document.body.textContent).not.toContain("text_memory_reflector");
    expect(document.body.textContent).not.toContain("artifact-text-memory");

    await act(async () => {
      const details = document.querySelector("details");
      if (!(details instanceof HTMLDetailsElement)) {
        throw new Error("Diagnostics details not found");
      }
      details.open = true;
      details.dispatchEvent(new Event("toggle", { bubbles: true }));
    });

    expect(document.body.textContent).toContain("run_20260707170000000000");
    expect(document.body.textContent).toContain(
      "/home/alice/.openevo/runs/protein/folding/runs/run_20260707170000000000",
    );
    expect(document.body.textContent).toContain("stdout secret payload");
    expect(document.body.textContent).toContain("stderr secret payload");
    expect(document.body.textContent).toContain("text_memory_reflector");
    expect(document.body.textContent).toContain("artifact-text-memory");
    await unmountClient(root);
  });

  it("does not preview draft artifacts without approved artifact IDs", async () => {
    const shellModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "ready",
      backendDetail: "Remote runtime services are ready",
      transcriptState: "planned",
      transcriptDetail: "Trajectory capture will start after the first run",
    });
    const ranModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "ready",
      backendDetail: "Last run completed",
      transcriptState: "complete",
      transcriptDetail: "Run completed and transcript captured",
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.runOpenEvoStartRun.mockResolvedValue({
      run: runStatus("succeeded"),
      status: ranModel,
    });
    apiMocks.fetchOpenEvoBackendRunTimeline.mockResolvedValue(runTimeline());
    apiMocks.fetchOpenEvoBackendRunArtifacts.mockResolvedValue(
      draftOnlyRunArtifacts(),
    );
    apiMocks.fetchOpenEvoBackendArtifactPreview.mockResolvedValue(
      artifactPreview({
        id: "artifact-draft-memory",
        body: "# Draft Memory\n\n- This content is not approved.\n",
      }),
    );

    const root = await renderClient();
    await flushEffects();

    await act(async () => {
      buttonByText("Start Run").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(apiMocks.fetchOpenEvoBackendArtifactPreview).not.toHaveBeenCalled();
    expect(document.body.textContent).toContain(
      "No promoted artifact content available yet.",
    );
    expect(document.body.textContent).not.toContain("Draft Memory");
    expect(document.body.textContent).not.toContain("artifact-draft-memory");
    await unmountClient(root);
  });

  it("shows artifact timeline load failures without hiding terminal run status", async () => {
    const shellModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "ready",
      backendDetail: "Remote runtime services are ready",
      transcriptState: "planned",
      transcriptDetail: "Trajectory capture will start after the first run",
    });
    const runningModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "running",
      backendDetail: "OpenEvo run is running",
      transcriptState: "running",
      transcriptDetail: "Capturing transcript trajectory",
    });
    const ranModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "ready",
      backendDetail: "Last run completed",
      transcriptState: "complete",
      transcriptDetail: "Run completed and transcript captured",
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.runOpenEvoStartRun.mockResolvedValue({
      run: runStatus("running"),
      status: runningModel,
    });
    apiMocks.pollOpenEvoRunStatus.mockResolvedValue({
      run: runStatus("succeeded"),
      status: ranModel,
    });
    apiMocks.fetchOpenEvoBackendRunArtifacts.mockRejectedValue(
      new Error("OpenEvo backend artifacts not found."),
    );

    const root = await renderClient();
    await flushEffects();

    await act(async () => {
      buttonByText("Start Run").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("succeeded");
    expect(document.body.textContent).toContain(
      "OpenEvo backend artifacts not found.",
    );
    await unmountClient(root);
  });

  it("updates run state after the StrictMode mount cleanup probe", async () => {
    const shellModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "ready",
      backendDetail: "Remote runtime services are ready",
      transcriptState: "planned",
      transcriptDetail: "Trajectory capture will start after the first run",
    });
    const runningModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "running",
      backendDetail: "OpenEvo run is running",
      transcriptState: "running",
      transcriptDetail: "Capturing transcript trajectory",
    });
    const ranModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "ready",
      backendDetail: "Last run completed",
      transcriptState: "complete",
      transcriptDetail: "Run completed and transcript captured",
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.runOpenEvoStartRun.mockResolvedValue({
      run: runStatus("running"),
      status: runningModel,
    });
    apiMocks.pollOpenEvoRunStatus.mockResolvedValue({
      run: runStatus("succeeded"),
      status: ranModel,
    });

    const root = await renderClient({ strict: true });
    await flushEffects();

    await act(async () => {
      buttonByText("Start Run").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("succeeded");
    expect(document.body.textContent).toContain("Last run completed");
    await unmountClient(root);
  });

  it("does not continue polling after unmount", async () => {
    vi.useFakeTimers();
    const shellModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "ready",
      backendDetail: "Remote runtime services are ready",
      transcriptState: "planned",
      transcriptDetail: "Trajectory capture will start after the first run",
    });
    const runningModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "running",
      backendDetail: "OpenEvo run is running",
      transcriptState: "running",
      transcriptDetail: "Capturing transcript trajectory",
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    const launch = deferRun(runningModel, runStatus("running"));
    const poll = deferRun(runningModel, runStatus("running"));
    apiMocks.runOpenEvoStartRun.mockReturnValue(launch.promise);
    apiMocks.pollOpenEvoRunStatus.mockReturnValue(poll.promise);

    const root = await renderClient();
    await flushEffects();

    await act(async () => {
      buttonByText("Start Run").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
    });
    await act(async () => {
      launch.resolve({
        run: runStatus("running"),
        status: runningModel,
      });
      await Promise.resolve();
    });
    expect(apiMocks.pollOpenEvoRunStatus).toHaveBeenCalledTimes(1);

    await unmountClient(root);
    await act(async () => {
      poll.resolve({
        run: runStatus("running"),
        status: runningModel,
      });
      await Promise.resolve();
      vi.advanceTimersByTime(1000);
      await Promise.resolve();
    });

    expect(apiMocks.pollOpenEvoRunStatus).toHaveBeenCalledTimes(1);
  });

  it("clears the latest run when a new project config is saved", async () => {
    const shellModel = withBackendService(getOpenEvoDesktopShellModel(), {
      state: "ready",
      detail: "Remote runtime services are ready",
    });
    const runningModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "running",
      backendDetail: "OpenEvo run is running",
      transcriptState: "running",
      transcriptDetail: "Capturing transcript trajectory",
    });
    const savedModel = {
      ...shellModel,
      project: {
        ...shellModel.project,
        name: "Configured Science Project",
      },
    };
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.runOpenEvoStartRun.mockResolvedValue({
      run: runStatus("running"),
      status: runningModel,
    });
    apiMocks.pollOpenEvoRunStatus.mockResolvedValue({
      run: runStatus("failed"),
      status: modelWithRun({
        projectName: "Loaded Science Project",
        backendState: "blocked",
        backendDetail: "run failed",
        transcriptState: "blocked",
        transcriptDetail: "run failed",
      }),
    });
    apiMocks.saveOpenEvoProjectConfig.mockResolvedValue({
      config: {
        science_config_path:
          "/home/alice/.openevo/desktop/projects/configured/science.yaml",
        remote_profile_path:
          "/home/alice/.openevo/desktop/profiles/science-team.yaml",
      },
      status: savedModel,
    });

    const root = await renderClient();
    await flushEffects();

    await act(async () => {
      buttonByText("Start Run").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });
    expect(document.body.textContent).toContain("Run Status");
    expect(document.body.textContent).toContain("failed");

    await act(async () => {
      buttonByText("Save Config").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("Configured Science Project");
    expect(document.body.textContent).not.toContain("Run Status");
    await unmountClient(root);
  });

  it("renders failed run status and stderr after polling", async () => {
    const shellModel = withDeveloperMode(
      modelWithRun({
        projectName: "Loaded Science Project",
        backendState: "ready",
        backendDetail: "Remote runtime services are ready",
        transcriptState: "planned",
        transcriptDetail: "Trajectory capture will start after the first run",
      }),
    );
    const runningModel = withDeveloperMode(
      modelWithRun({
        projectName: "Loaded Science Project",
        backendState: "running",
        backendDetail: "OpenEvo run is running",
        transcriptState: "running",
        transcriptDetail: "Capturing transcript trajectory",
      }),
    );
    const failedModel = withDeveloperMode(
      modelWithRun({
        projectName: "Loaded Science Project",
        backendState: "blocked",
        backendDetail: "run failed",
        transcriptState: "blocked",
        transcriptDetail: "run failed",
      }),
    );
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.runOpenEvoStartRun.mockResolvedValue({
      run: runStatus("running"),
      status: runningModel,
    });
    apiMocks.pollOpenEvoRunStatus.mockResolvedValue({
      run: {
        ...runStatus("failed"),
        stderr: "stderr secret payload",
      },
      status: failedModel,
    });

    const root = await renderClient();
    await flushEffects();

    await act(async () => {
      buttonByText("Start Run").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("failed");
    expect(document.body.textContent).toContain("run failed");
    expect(document.body.textContent).not.toContain("stderr secret payload");
    expect(document.body.textContent).toContain("2");
    await act(async () => {
      const details = document.querySelector("details");
      if (!(details instanceof HTMLDetailsElement)) {
        throw new Error("Diagnostics details not found");
      }
      details.open = true;
      details.dispatchEvent(new Event("toggle", { bubbles: true }));
    });
    expect(document.body.textContent).toContain("stderr secret payload");
    await unmountClient(root);
  });

  it("shows a bootstrap error when the sidecar rejects the action", async () => {
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(
      modelWithBootstrap({
        projectName: "Loaded Science Project",
        ready: false,
        notes: ["Remote bootstrap has not run yet."],
        bootstrapDetail: "Remote bootstrap has not run yet",
      }),
    );
    apiMocks.runOpenEvoBootstrap.mockRejectedValue(
      new Error("HTTP 403 Forbidden: invalid token"),
    );

    const root = await renderClient();
    await flushEffects();

    await act(async () => {
      buttonByText("Bootstrap").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain(
      "HTTP 403 Forbidden: invalid token",
    );
    await unmountClient(root);
  });

  it("shows a workspace sync error when the sidecar rejects the action", async () => {
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(
      modelWithWorkspace({
        projectName: "Loaded Science Project",
        state: "planned",
        detail: "Workspace preparation has not run yet",
      }),
    );
    apiMocks.runOpenEvoWorkspaceSync.mockRejectedValue(
      new Error("HTTP 403 Forbidden: invalid token"),
    );

    const root = await renderClient();
    await flushEffects();

    await act(async () => {
      buttonByText("Sync Workspace").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain(
      "HTTP 403 Forbidden: invalid token",
    );
    await unmountClient(root);
  });

  it("shows a run error when the sidecar rejects the action", async () => {
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(
      modelWithRun({
        projectName: "Loaded Science Project",
        backendState: "ready",
        backendDetail: "Remote runtime services are ready",
        transcriptState: "planned",
        transcriptDetail: "Trajectory capture will start after the first run",
      }),
    );
    apiMocks.runOpenEvoStartRun.mockRejectedValue(
      new Error("HTTP 403 Forbidden: invalid token"),
    );

    const root = await renderClient();
    await flushEffects();

    await act(async () => {
      buttonByText("Start Run").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain(
      "HTTP 403 Forbidden: invalid token",
    );
    await unmountClient(root);
  });

  it("shows a project config error when the sidecar rejects the draft", async () => {
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(
      getOpenEvoDesktopShellModel(),
    );
    apiMocks.saveOpenEvoProjectConfig.mockRejectedValue(
      new Error("HTTP 422 Unprocessable Entity: invalid draft"),
    );

    const root = await renderClient();
    await flushEffects();

    await act(async () => {
      buttonByText("Save Config").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain(
      "HTTP 422 Unprocessable Entity: invalid draft",
    );
    await unmountClient(root);
  });
});

async function renderClient({ strict = false }: { strict?: boolean } = {}): Promise<Root> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      strict ? (
        <StrictMode>
          <OpenEvoDesktop />
        </StrictMode>
      ) : (
        <OpenEvoDesktop />
      ),
    );
  });
  return root;
}

async function flushEffects() {
  await act(async () => {
    await Promise.resolve();
  });
}

async function unmountClient(root: Root) {
  await act(async () => {
    root.unmount();
  });
}

function buttonByText(text: string): HTMLButtonElement {
  const button = Array.from(document.querySelectorAll("button")).find((item) =>
    item.textContent?.includes(text),
  );
  if (!(button instanceof HTMLButtonElement)) {
    throw new Error(`Button not found: ${text}`);
  }
  return button;
}

function buttonByLabel(label: string): HTMLButtonElement {
  const button = document.querySelector(`[aria-label="${label}"]`);
  if (!(button instanceof HTMLButtonElement)) {
    throw new Error(`Button not found: ${label}`);
  }
  return button;
}

async function changeInput(label: string, value: string) {
  const input = inputByLabel(label);
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )?.set;
    setter?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

async function changeSelect(label: string, value: string) {
  const select = selectByLabel(label);
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(
      HTMLSelectElement.prototype,
      "value",
    )?.set;
    setter?.call(select, value);
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

function inputByLabel(label: string): HTMLInputElement {
  const input = document.querySelector(`[aria-label="${label}"]`);
  if (!(input instanceof HTMLInputElement)) {
    throw new Error(`Input not found: ${label}`);
  }
  return input;
}

function selectByLabel(label: string): HTMLSelectElement {
  const select = document.querySelector(`[aria-label="${label}"]`);
  if (!(select instanceof HTMLSelectElement)) {
    throw new Error(`Select not found: ${label}`);
  }
  return select;
}

function modelWithRemoteSetup(): OpenEvoDesktopShellModel {
  const model = getOpenEvoDesktopShellModel();
  return {
    ...model,
    remote: {
      ...model.remote,
      id: "science-team",
      port: 2222,
      auth: {
        method: "private_key",
        privateKeyPath: "/home/alice/.ssh/openevo",
        passwordRef: null,
        passphraseRef: "keyring://openevo/science-team",
      },
      workspaceRoot: "/data/openevo/workspaces",
      proxy: {
        ...model.remote.proxy,
        httpProxy: "http://127.0.0.1:7890",
        httpsProxy: "http://127.0.0.1:7891",
        noProxy: "localhost,127.0.0.1",
        pipIndexUrl: "https://pypi.tuna.tsinghua.edu.cn/simple",
        huggingFaceEndpoint: "https://hf-mirror.com",
        hfHome: "/data/hf-cache",
      },
    },
    bootstrap: {
      ...model.bootstrap,
      workspaceRoot: "/data/openevo/workspaces",
    },
  };
}

function modelWithSelfDeployedInference(): OpenEvoDesktopShellModel {
  const model = modelWithRemoteSetup();
  return {
    ...model,
    remote: {
      ...model.remote,
      proxy: {
        ...model.remote.proxy,
        hfHome: "not configured",
      },
    },
    execution: {
      mode: "self-deployed",
      model: "Qwen/Qwen3-Coder-30B-A3B-Instruct",
      tokenMetricsAvailable: false,
    },
  };
}

function modelWithBootstrap({
  projectName,
  ready,
  notes,
  bootstrapDetail,
}: {
  projectName: string;
  ready: boolean;
  notes: string[];
  bootstrapDetail: string;
}): OpenEvoDesktopShellModel {
  const model = getOpenEvoDesktopShellModel();
  return {
    ...model,
    project: {
      ...model.project,
      name: projectName,
      evolutionTargets: concreteEvolutionTargets(model),
    },
    bootstrap: {
      ...model.bootstrap,
      ready,
      readinessNotes: notes,
    },
    services: model.services.map((service) =>
      service.id === "workspace" && ready
        ? {
            ...service,
            state: "ready",
            detail: "Workspace prepared",
          }
        : service.id === "bootstrap"
        ? {
            ...service,
            state: ready ? "ready" : "planned",
            detail: bootstrapDetail,
          }
        : service,
    ),
  };
}

function modelWithWorkspace({
  projectName,
  state,
  detail,
}: {
  projectName: string;
  state: OpenEvoDesktopShellModel["services"][number]["state"];
  detail: string;
}): OpenEvoDesktopShellModel {
  const model = getOpenEvoDesktopShellModel();
  return {
    ...model,
    project: {
      ...model.project,
      name: projectName,
    },
    services: model.services.map((service) =>
      service.id === "workspace"
        ? {
            ...service,
            state,
            detail,
          }
        : service,
    ),
  };
}

function modelWithUnsupportedSshAuth({
  auth,
}: {
  auth: OpenEvoDesktopShellModel["remote"]["auth"];
}): OpenEvoDesktopShellModel {
  const model = modelWithWorkspace({
    projectName: "Loaded Science Project",
    state: "planned",
    detail: "Workspace preparation has not run yet",
  });
  return {
    ...model,
    remote: {
      ...model.remote,
      auth,
    },
    sidecar: {
      transport: {
        id: "ssh",
        label: "SSH transport",
        supportsPasswordRef: false,
        supportsPassphraseRef: false,
      },
    },
  };
}

function modelWithRun({
  projectName,
  backendState,
  backendDetail,
  transcriptState,
  transcriptDetail,
}: {
  projectName: string;
  backendState: OpenEvoDesktopShellModel["services"][number]["state"];
  backendDetail: string;
  transcriptState: OpenEvoDesktopShellModel["evolution"][number]["state"];
  transcriptDetail: string;
}): OpenEvoDesktopShellModel {
  const model = getOpenEvoDesktopShellModel();
  return {
    ...model,
    project: {
      ...model.project,
      name: projectName,
      evolutionTargets: concreteEvolutionTargets(model),
    },
    services: model.services.map((service) =>
      service.id === "workspace"
        ? {
            ...service,
            state: "ready",
            detail: "Workspace prepared",
          }
        : service.id === "bootstrap"
        ? {
            ...service,
            state: "ready",
            detail: "Runtime image and manifests prepared",
          }
        : service.id === "openevo-backend"
        ? {
            ...service,
            state: backendState,
            detail: backendDetail,
          }
        : service,
    ),
    bootstrap: {
      ...model.bootstrap,
      ready: true,
      readinessNotes: ["Remote bootstrap is ready."],
    },
    evolution: [
      {
        id: "transcript",
        label: "Transcript capture",
        state: transcriptState,
        detail: transcriptDetail,
      },
      ...model.evolution,
    ],
  };
}

function withDeveloperMode(
  model: OpenEvoDesktopShellModel,
): OpenEvoDesktopShellModel {
  return {
    ...model,
    developerMode: {
      ...model.developerMode,
      enabled: true,
    },
  };
}

function withBackendService(
  model: OpenEvoDesktopShellModel,
  {
    state,
    detail,
  }: {
    state: OpenEvoDesktopShellModel["services"][number]["state"];
    detail: string;
  },
): OpenEvoDesktopShellModel {
  return {
    ...model,
    project: {
      ...model.project,
      evolutionTargets: concreteEvolutionTargets(model),
    },
    services: model.services.map((service) =>
      service.id === "openevo-backend"
        ? {
            ...service,
            state,
            detail,
          }
        : service,
    ),
  };
}

function concreteEvolutionTargets(model: OpenEvoDesktopShellModel) {
  return Object.fromEntries(
    Object.entries(model.project.evolutionTargets).map(([targetId, selection]) => [
      targetId,
      selection,
    ]),
  );
}

function deferBootstrap(status: OpenEvoDesktopShellModel) {
  let resolve!: (value: {
    bootstrap: OpenEvoDesktopShellModel["bootstrap"];
    report: Record<string, any>;
    status: OpenEvoDesktopShellModel;
  }) => void;
  const promise = new Promise<{
    bootstrap: OpenEvoDesktopShellModel["bootstrap"];
    report: Record<string, any>;
    status: OpenEvoDesktopShellModel;
  }>((next) => {
    resolve = next;
  });
  return { promise, resolve, status };
}

function deferServices(status: OpenEvoDesktopShellModel) {
  let resolve!: (value: {
    services: Record<string, any>;
    report: Record<string, any>;
    status: OpenEvoDesktopShellModel;
  }) => void;
  const promise = new Promise<{
    services: Record<string, any>;
    report: Record<string, any>;
    status: OpenEvoDesktopShellModel;
  }>((next) => {
    resolve = next;
  });
  return { promise, resolve, status };
}

function deferWorkspace(status: OpenEvoDesktopShellModel) {
  let resolve!: (value: {
    workspace: Record<string, any>;
    report: Record<string, any>;
    status: OpenEvoDesktopShellModel;
  }) => void;
  const promise = new Promise<{
    workspace: Record<string, any>;
    report: Record<string, any>;
    status: OpenEvoDesktopShellModel;
  }>((next) => {
    resolve = next;
  });
  return { promise, resolve, status };
}

function deferRun(status: OpenEvoDesktopShellModel, run: Record<string, any>) {
  let resolve!: (value: {
    run: Record<string, any>;
    status: OpenEvoDesktopShellModel;
  }) => void;
  const promise = new Promise<{
    run: Record<string, any>;
    status: OpenEvoDesktopShellModel;
  }>((next) => {
    resolve = next;
  });
  return { promise, resolve, run, status };
}

function runStatus(state: "running" | "succeeded" | "failed") {
  return {
    id: "run_20260707170000000000",
    state,
    ready: state === "succeeded",
    command:
      "openevo-backend run /home/alice/.openevo/runs/protein/folding/experiment.json --output-dir /home/alice/.openevo/runs/protein/folding/runs/run_20260707170000000000 --artifact-root /home/alice/.openevo/runs/protein/folding/evolution/artifacts --json",
    returnCode: state === "running" ? null : state === "succeeded" ? 0 : 2,
    stdout: state === "succeeded" ? "ok" : "",
    stderr: state === "failed" ? "run failed" : "",
    outputDir:
      "/home/alice/.openevo/runs/protein/folding/runs/run_20260707170000000000",
    experimentSnapshot: "/home/alice/.openevo/runs/protein/folding/experiment.json",
    startedAt: "2026-07-07T16:00:00+00:00",
    finishedAt: state === "running" ? null : "2026-07-07T17:01:00+00:00",
  };
}

function emptyRunArtifacts() {
  return [];
}

function emptyRunTimeline() {
  return [];
}

function runTimeline() {
  return [
    {
      id: "event-memory",
      phase: "evolution",
      label: "Memory updated",
      message: "Text memory worker promoted one artifact.",
      artifactIds: ["artifact-text-memory"],
    },
  ];
}

function runArtifacts() {
  return [
    {
      id: "artifact-text-memory",
      runId: "run_20260707170000000000",
      artifactType: "text_memory",
      title: "Initial memory draft",
      promoted: true,
      lineage: {
        method: "text_memory_reflector",
        dataset_id: "dataset-artifact-1",
      },
    },
  ];
}

function draftOnlyRunArtifacts() {
  return [
    {
      id: "artifact-draft-memory",
      runId: "run_20260707170000000000",
      artifactType: "text_memory",
      title: "Rejected memory draft",
      promoted: false,
      lineage: {
        method: "text_memory_reflector",
      },
    },
  ];
}

function artifactPreview({
  id = "artifact-text-memory",
  body = "# Learned Memory\n\n- Prefer stable folds.\n",
}: {
  id?: string;
  body?: string;
} = {}) {
  return {
    id,
    kind: "text_memory",
    body,
    targetPath: "memory.md",
    lineage: {
      method: "text_memory_reflector",
    },
    diff: {
      id,
      before: "",
      after: body,
      format: "unified_text" as const,
    },
  };
}

function desktopCapabilities() {
  return {
    schemaVersion: "1",
    coreVersion: "0.1.0",
    registryDigest: "a".repeat(64),
    evaluatedProfile: {
      executionMode: "subscription",
      captureMode: "transcript",
      harnessId: "codex",
      harnessCapabilities: ["stable_transcript"],
      runtimeCapabilities: [],
    },
    targets: [
      desktopCapabilityTarget("text_memory", "Text memory", "text_memory_reflector"),
      desktopCapabilityTarget("skill_bundle", "Skill bundle", "skill_bundle_reflector"),
      desktopCapabilityTarget("agent_system", "Agent system", "agent_system_reflector"),
    ],
  };
}

function desktopCapabilityTarget(
  targetId: string,
  displayName: string,
  methodId: string,
): any {
  const support = supportedSupport();
  return {
    targetId,
    displayName,
    description: `${displayName} target`,
    artifactType: targetId,
    exposure: "desktop",
    maturity: "stable",
    handlerId: `${targetId}_handler`,
    configuredDefaultMethodId: methodId,
    effectiveDefaultMethodId: methodId,
    configuredDefaultSupport: support,
    rendererKind: "markdown",
    rendererContractVersion: "1",
    contributionContractVersion: "1",
    contextOrder: 10,
    implementationIdentityDigest: "b".repeat(64),
    handlerIdentityDigest: "c".repeat(64),
    acceptedMethods: [
      {
        methodId,
        implementationIdentityDigest: "d".repeat(64),
        support,
      },
      ...(targetId === "agent_system"
        ? [
            {
              methodId: "agent_system_history_reflector",
              implementationIdentityDigest: "e".repeat(64),
              support,
            },
          ]
        : []),
    ],
    selectionResolvers:
      targetId === "agent_system"
        ? [
            {
              selectionValue: "auto",
              displayName: "Automatic",
              description: "Resolve from prior datasets.",
              resolvedMethods: [
                {
                  methodId: "agent_system_reflector",
                  implementationIdentityDigest: "d".repeat(64),
                  support,
                },
                {
                  methodId: "agent_system_history_reflector",
                  implementationIdentityDigest: "e".repeat(64),
                  support,
                },
              ],
            },
          ]
        : [],
    methods: [
      {
        methodId,
        displayName,
        description: `${displayName} method`,
        exposure: "desktop",
        maturity: "stable",
        executionModes: ["subscription", "self_deployed"],
        captureModes: ["transcript"],
        supportedHarnessIds: ["codex"],
        harnessRequirements: ["stable_transcript"],
        runtimeRequirements: [],
        inputBindings: [],
        outputArtifactTypes: [targetId],
        configSchemaJson:
          '{"additionalProperties":false,"properties":{"retained":{"type":"boolean"},"threshold":{"type":"number"}},"type":"object"}',
        defaultConfigJson: "{}",
        configSchema: parseEvolutionConfigSchema({
          additionalProperties: false,
          properties: {
            retained: { type: "boolean" },
            threshold: { type: "number" },
          },
          type: "object",
        }),
        defaultConfig: {},
        implementationIdentityDigest: "d".repeat(64),
        support,
      },
    ],
  };
}

function supportedSupport(): any {
  const axis = {
    state: "supported",
    reasonCode: null,
    message: "Supported.",
    missingRequirements: [],
  };
  return {
    overall: "supported",
    execution: { ...axis },
    capture: { ...axis },
    harness: { ...axis },
    runtime: { ...axis },
  };
}

function unsupportedSupport(reasonCode: string, message: string): any {
  return {
    ...supportedSupport(),
    overall: "unsupported",
    execution: {
      state: "unsupported",
      reasonCode,
      message,
      missingRequirements: [],
    },
  };
}

function deferProjectConfig(status: OpenEvoDesktopShellModel) {
  let resolve!: (value: {
    config: {
      science_config_path: string;
      remote_profile_path: string;
    };
    status: OpenEvoDesktopShellModel;
  }) => void;
  const promise = new Promise<{
    config: {
      science_config_path: string;
      remote_profile_path: string;
    };
    status: OpenEvoDesktopShellModel;
  }>((next) => {
    resolve = next;
  });
  return { promise, resolve, status };
}

function deferCapabilities() {
  let resolve!: (value: ReturnType<typeof desktopCapabilities>) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<ReturnType<typeof desktopCapabilities>>(
    (next, fail) => {
      resolve = next;
      reject = fail;
    },
  );
  return { promise, resolve, reject };
}

function projectConfigFieldset(): HTMLFieldSetElement {
  const fieldset = document.querySelector<HTMLFieldSetElement>(
    '[data-testid="project-config-fields"]',
  );
  if (!fieldset) {
    throw new Error("project config fieldset not found");
  }
  return fieldset;
}

function deferCatalog() {
  let resolve!: (value: ReturnType<typeof savedProjectConfigs>) => void;
  const promise = new Promise<ReturnType<typeof savedProjectConfigs>>((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

function savedProjectConfigs() {
  return [
    {
      projectSlug: "protein-design",
      valid: true,
      error: null,
      projectName: "Protein Design",
      taskId: "folding-baseline",
      objective: "Improve the folding baseline.",
      sourceType: "remote_path",
      sourceLabel: "/datasets/folding-baseline",
      remoteProfileId: "science-team",
      remoteHost: "gpu.example.edu",
      remoteUser: "alice",
      scienceConfigPath:
        "/home/alice/.openevo/desktop/projects/protein-design/science.yaml",
      remoteProfilePath:
        "/home/alice/.openevo/desktop/profiles/science-team.yaml",
    },
    {
      projectSlug: "broken-project",
      valid: false,
      error: "profiles/broken.yaml: not found",
      projectName: "Broken Project",
      taskId: "broken-task",
      objective: "Repair the config.",
      sourceType: "remote_path",
      sourceLabel: "/datasets/broken",
      remoteProfileId: "broken",
      remoteHost: null,
      remoteUser: null,
      scienceConfigPath:
        "/home/alice/.openevo/desktop/projects/broken-project/science.yaml",
      remoteProfilePath:
        "/home/alice/.openevo/desktop/profiles/broken.yaml",
    },
  ];
}
