// @vitest-environment happy-dom

import { StrictMode, act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OpenEvoDesktop } from "./OpenEvoDesktop";
import {
  getOpenEvoDesktopShellModel,
  type OpenEvoDesktopShellModel,
} from "./openevoDesktopModel";

const apiMocks = vi.hoisted(() => ({
  activateOpenEvoProjectConfig: vi.fn(),
  fetchOpenEvoProjectConfigs: vi.fn(),
  fetchOpenEvoDesktopShellModel: vi.fn(),
  pollOpenEvoRunStatus: vi.fn(),
  runOpenEvoBootstrap: vi.fn(),
  runOpenEvoStartRun: vi.fn(),
  runOpenEvoWorkspaceSync: vi.fn(),
  saveOpenEvoProjectConfig: vi.fn(),
}));

vi.mock("../api/openevo", () => ({
  activateOpenEvoProjectConfig: apiMocks.activateOpenEvoProjectConfig,
  fetchOpenEvoProjectConfigs: apiMocks.fetchOpenEvoProjectConfigs,
  fetchOpenEvoDesktopShellModel: apiMocks.fetchOpenEvoDesktopShellModel,
  pollOpenEvoRunStatus: apiMocks.pollOpenEvoRunStatus,
  runOpenEvoBootstrap: apiMocks.runOpenEvoBootstrap,
  runOpenEvoStartRun: apiMocks.runOpenEvoStartRun,
  runOpenEvoWorkspaceSync: apiMocks.runOpenEvoWorkspaceSync,
  saveOpenEvoProjectConfig: apiMocks.saveOpenEvoProjectConfig,
}));

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

describe("OpenEvoDesktop", () => {
  beforeEach(() => {
    apiMocks.activateOpenEvoProjectConfig.mockReset();
    apiMocks.fetchOpenEvoProjectConfigs.mockReset();
    apiMocks.fetchOpenEvoDesktopShellModel.mockReset();
    apiMocks.pollOpenEvoRunStatus.mockReset();
    apiMocks.runOpenEvoBootstrap.mockReset();
    apiMocks.runOpenEvoStartRun.mockReset();
    apiMocks.runOpenEvoWorkspaceSync.mockReset();
    apiMocks.saveOpenEvoProjectConfig.mockReset();
    apiMocks.activateOpenEvoProjectConfig.mockRejectedValue(
      new Error("sidecar unavailable"),
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

  it("renders fixture state when the sidecar fetch is unavailable", () => {
    const html = renderToString(<OpenEvoDesktop />);

    expect(html).toContain("Protein Folding Literature Sprint");
    expect(html).toContain("codex_subscription_transcript");
    expect(html).toContain("Remote ready");
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
        source_type: "git_repository",
        text_memory: true,
        skill_bundle: true,
        agent_system: true,
      }),
    );
    expect(document.body.textContent).toContain("Saving");

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
    const shellModel = getOpenEvoDesktopShellModel();
    const runningModel = modelWithRun({
      projectName: "Loaded Science Project",
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
        projectName: "Loaded Science Project",
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
    expect(document.body.textContent).toContain("run_20260707170000000000");

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
    expect(document.body.textContent).not.toContain("run_20260707170000000000");
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

  it("starts a run from the button and refreshes visible status", async () => {
    const shellModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "planned",
      backendDetail: "Service supervisor integration is next",
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
      "Service supervisor integration is next",
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
    expect(document.body.textContent).toContain("run_20260707170000000000");
    expect(document.body.textContent).toContain("running");

    await act(async () => {
      poll.resolve({
        run: runStatus("succeeded"),
        status: ranModel,
      });
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("succeeded");
    expect(document.body.textContent).toContain(
      "/home/alice/.openevo/runs/protein/folding/runs/run_20260707170000000000",
    );
    expect(document.body.textContent).toContain("ok");
    expect(document.body.textContent).toContain("Last run completed");
    expect(document.body.textContent).toContain(
      "Run completed and transcript captured",
    );
    await unmountClient(root);
  });

  it("updates run state after the StrictMode mount cleanup probe", async () => {
    const shellModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "planned",
      backendDetail: "Service supervisor integration is next",
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
    expect(document.body.textContent).toContain("run_20260707170000000000");
    expect(document.body.textContent).toContain("Last run completed");
    await unmountClient(root);
  });

  it("does not continue polling after unmount", async () => {
    vi.useFakeTimers();
    const shellModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "planned",
      backendDetail: "Service supervisor integration is next",
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
    const shellModel = getOpenEvoDesktopShellModel();
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
    expect(document.body.textContent).toContain("run_20260707170000000000");

    await act(async () => {
      buttonByText("Save Config").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("Configured Science Project");
    expect(document.body.textContent).not.toContain("run_20260707170000000000");
    await unmountClient(root);
  });

  it("renders failed run status and stderr after polling", async () => {
    const shellModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "planned",
      backendDetail: "Service supervisor integration is next",
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
    const failedModel = modelWithRun({
      projectName: "Loaded Science Project",
      backendState: "blocked",
      backendDetail: "run failed",
      transcriptState: "blocked",
      transcriptDetail: "run failed",
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    apiMocks.runOpenEvoStartRun.mockResolvedValue({
      run: runStatus("running"),
      status: runningModel,
    });
    apiMocks.pollOpenEvoRunStatus.mockResolvedValue({
      run: runStatus("failed"),
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
    expect(document.body.textContent).toContain("2");
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
        backendState: "planned",
        backendDetail: "Service supervisor integration is next",
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

function inputByLabel(label: string): HTMLInputElement {
  const input = document.querySelector(`[aria-label="${label}"]`);
  if (!(input instanceof HTMLInputElement)) {
    throw new Error(`Input not found: ${label}`);
  }
  return input;
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
    },
    bootstrap: {
      ...model.bootstrap,
      ready,
      readinessNotes: notes,
    },
    services: model.services.map((service) =>
      service.id === "bootstrap"
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
    },
    services: model.services.map((service) =>
      service.id === "openevo-backend"
        ? {
            ...service,
            state: backendState,
            detail: backendDetail,
          }
        : service,
    ),
    evolution: model.evolution.map((step) =>
      step.id === "transcript"
        ? {
            ...step,
            state: transcriptState,
            detail: transcriptDetail,
          }
        : step,
    ),
  };
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
      "openevo run /home/alice/.openevo/runs/protein/folding/experiment.json --output-dir /home/alice/.openevo/runs/protein/folding/runs/run_20260707170000000000 --json",
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
