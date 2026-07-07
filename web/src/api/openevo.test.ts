import { afterEach, describe, expect, it, vi } from "vitest";
import {
  activateOpenEvoProjectConfig,
  fetchOpenEvoProjectConfigs,
  fetchOpenEvoDesktopShellModel,
  pollOpenEvoRunStatus,
  runOpenEvoBootstrap,
  runOpenEvoStartRun,
  runOpenEvoWorkspaceSync,
  saveOpenEvoProjectConfig,
  toOpenEvoBootstrapResponse,
  toOpenEvoDesktopShellModel,
} from "./openevo";

describe("OpenEvo sidecar client", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("maps sidecar shell status to the route model", () => {
    const model = toOpenEvoDesktopShellModel({
      remote: {
        id: "lab-gpu",
        host: "gpu.example.edu",
        port: 2222,
        user: "alice",
        auth: {
          method: "private_key",
          private_key_path: "/home/alice/.ssh/openevo",
          password_ref: null,
          passphrase_ref: "keyring://openevo/lab-gpu",
        },
        workspace_root: "/data/openevo/workspaces",
        proxy: {
          http_proxy: "http://127.0.0.1:7890",
          https_proxy: "http://127.0.0.1:7890",
          no_proxy: "localhost,127.0.0.1",
          pip_index_url: "https://pypi.tuna.tsinghua.edu.cn/simple",
          huggingface_endpoint: "https://hf-mirror.com",
          hf_home: "/data/hf-cache",
        },
      },
      project: {
        name: "Protein Folding Literature Sprint",
        task_id: "folding-baseline",
        source: "Git repository: github.com/example/protein-workflows",
        objective: "Survey papers.",
      },
      execution: {
        mode: "codex_subscription_transcript",
        model: "gpt-5.1-codex-mini",
        token_metrics_available: false,
      },
      bootstrap: {
        ready: true,
        state_root: "/home/alice/.openevo/runs/protein/folding",
        workspace_root: "/home/alice/.openevo/workspaces",
        readiness_notes: ["Codex subscription login available"],
      },
      services: [
        {
          id: "ssh",
          label: "SSH transport",
          state: "ready",
          detail: "Remote command execution available",
        },
      ],
      evolution: [
        {
          id: "transcript",
          label: "Transcript capture",
          state: "complete",
          detail: "Transcript captured.",
        },
      ],
      developer_mode: {
        enabled: false,
        benchmark_controls_visible: false,
      },
      sidecar: {
        mutation_token: "token-123",
        transport: {
          id: "ssh",
          label: "SSH transport",
          supports_password_ref: false,
          supports_passphrase_ref: false,
        },
      },
    });

    expect(model.remote.proxy.httpsProxy).toBe("http://127.0.0.1:7890");
    expect(model.remote.port).toBe(2222);
    expect(model.remote.auth.privateKeyPath).toBe("/home/alice/.ssh/openevo");
    expect(model.remote.auth.passphraseRef).toBe("keyring://openevo/lab-gpu");
    expect(model.remote.workspaceRoot).toBe("/data/openevo/workspaces");
    expect(model.remote.proxy.noProxy).toBe("localhost,127.0.0.1");
    expect(model.remote.proxy.pipIndexUrl).toBe(
      "https://pypi.tuna.tsinghua.edu.cn/simple",
    );
    expect(model.remote.proxy.hfHome).toBe("/data/hf-cache");
    expect(model.project.taskId).toBe("folding-baseline");
    expect(model.execution.tokenMetricsAvailable).toBe(false);
    expect(model.bootstrap.readinessNotes).toEqual([
      "Codex subscription login available",
    ]);
    expect(model.sidecar.transport).toEqual({
      id: "ssh",
      label: "SSH transport",
      supportsPasswordRef: false,
      supportsPassphraseRef: false,
    });
    expect(model.developerMode.benchmarkControlsVisible).toBe(false);
  });

  it("maps bootstrap response status to the route model", () => {
    const response = toOpenEvoBootstrapResponse({
      bootstrap: {
        ready: true,
        state_root: "/home/alice/.openevo/runs/protein/folding",
        workspace_root: "/home/alice/.openevo/workspaces",
        readiness_notes: ["Remote bootstrap is ready."],
      },
      report: {
        ready: true,
        prepared_paths: {
          bootstrap_manifest: "/home/alice/.openevo/runs/protein/folding/bootstrap.json",
        },
      },
      status: {
        remote: {
          id: "lab-gpu",
          host: "gpu.example.edu",
          port: 22,
          user: "alice",
          auth: {
            method: "ssh_agent",
            private_key_path: null,
            password_ref: null,
            passphrase_ref: null,
          },
          workspace_root: "/home/alice/.openevo/workspaces",
          proxy: {
            http_proxy: null,
            https_proxy: "http://127.0.0.1:7890",
            no_proxy: null,
            pip_index_url: null,
            huggingface_endpoint: null,
            hf_home: null,
          },
        },
        project: {
          name: "Protein Folding Literature Sprint",
          task_id: "folding-baseline",
          source: "Remote path: /datasets/folding-baseline",
          objective: "Improve folding.",
        },
        execution: {
          mode: "codex_subscription_transcript",
          model: "gpt-5.1-codex-mini",
          token_metrics_available: false,
        },
        bootstrap: {
          ready: true,
          state_root: "/home/alice/.openevo/runs/protein/folding",
          workspace_root: "/home/alice/.openevo/workspaces",
          readiness_notes: ["Remote bootstrap is ready."],
        },
        services: [],
        evolution: [],
        developer_mode: {
          enabled: false,
          benchmark_controls_visible: false,
        },
      },
    });

    expect(response.bootstrap.ready).toBe(true);
    expect(response.status.bootstrap.readinessNotes).toEqual([
      "Remote bootstrap is ready.",
    ]);
    expect(response.report.prepared_paths).toEqual({
      bootstrap_manifest: "/home/alice/.openevo/runs/protein/folding/bootstrap.json",
    });
  });

  it("sends the sidecar mutation token on bootstrap requests", async () => {
    const calls: Array<{ path: string; headers: Headers }> = [];
    const shellPayload = sidecarShellPayload("token-123");
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input, init) => {
        const path = String(input);
        calls.push({
          path,
          headers: new Headers(init?.headers),
        });
        if (path === "/openevo-api/desktop/shell") {
          return jsonResponse(shellPayload);
        }
        if (path === "/openevo-api/desktop/bootstrap") {
          return jsonResponse({
            bootstrap: shellPayload.bootstrap,
            report: { ready: true },
            status: shellPayload,
          });
        }
        return new Response("not found", { status: 404 });
      },
    );

    await fetchOpenEvoDesktopShellModel();
    await runOpenEvoBootstrap();

    expect(calls).toHaveLength(2);
    expect(calls[1]).toMatchObject({ path: "/openevo-api/desktop/bootstrap" });
    expect(calls[1].headers.get("X-OpenEvo-Sidecar-Token")).toBe("token-123");
  });

  it("sends the sidecar mutation token on workspace sync requests", async () => {
    const calls: Array<{ path: string; headers: Headers }> = [];
    const shellPayload = sidecarShellPayload("workspace-token");
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input, init) => {
        const path = String(input);
        calls.push({
          path,
          headers: new Headers(init?.headers),
        });
        if (path === "/openevo-api/desktop/shell") {
          return jsonResponse(shellPayload);
        }
        if (path === "/openevo-api/desktop/workspace") {
          return jsonResponse({
            workspace: { ready: true, actions: [] },
            report: { ready: true },
            status: shellPayload,
          });
        }
        return new Response("not found", { status: 404 });
      },
    );

    await fetchOpenEvoDesktopShellModel();
    const response = await runOpenEvoWorkspaceSync();

    expect(response.workspace.ready).toBe(true);
    expect(calls).toHaveLength(2);
    expect(calls[1]).toMatchObject({ path: "/openevo-api/desktop/workspace" });
    expect(calls[1].headers.get("X-OpenEvo-Sidecar-Token")).toBe(
      "workspace-token",
    );
  });

  it("sends the sidecar mutation token on start run requests", async () => {
    const calls: Array<{ path: string; headers: Headers }> = [];
    const shellPayload = sidecarShellPayload("run-token");
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input, init) => {
        const path = String(input);
        calls.push({
          path,
          headers: new Headers(init?.headers),
        });
        if (path === "/openevo-api/desktop/shell") {
          return jsonResponse(shellPayload);
        }
        if (path === "/openevo-api/desktop/run") {
          return jsonResponse({
            run: sidecarRunReport(),
            status: shellPayload,
          });
        }
        return new Response("not found", { status: 404 });
      },
    );

    await fetchOpenEvoDesktopShellModel();
    const response = await runOpenEvoStartRun();

    expect(response.run.id).toBe("run_20260707170000000000");
    expect(response.run.state).toBe("running");
    expect(response.run.ready).toBe(false);
    expect(response.run.outputDir).toBe(
      "/home/alice/.openevo/runs/protein/folding/runs/run_20260707170000000000",
    );
    expect(response.run.finishedAt).toBeNull();
    expect(calls).toHaveLength(2);
    expect(calls[1]).toMatchObject({ path: "/openevo-api/desktop/run" });
    expect(calls[1].headers.get("X-OpenEvo-Sidecar-Token")).toBe("run-token");
  });

  it("sends the sidecar mutation token on run status polling", async () => {
    const calls: Array<{ path: string; headers: Headers; method: string }> = [];
    const shellPayload = sidecarShellPayload("poll-token");
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input, init) => {
        const path = String(input);
        calls.push({
          path,
          headers: new Headers(init?.headers),
          method: init?.method ?? "GET",
        });
        if (path === "/openevo-api/desktop/shell") {
          return jsonResponse(shellPayload);
        }
        if (path === "/openevo-api/desktop/run") {
          return jsonResponse({
            run: sidecarRunReport({ state: "succeeded" }),
            status: shellPayload,
          });
        }
        return new Response("not found", { status: 404 });
      },
    );

    await fetchOpenEvoDesktopShellModel();
    const response = await pollOpenEvoRunStatus();

    expect(response.run.state).toBe("succeeded");
    expect(response.run.ready).toBe(true);
    expect(response.run.returnCode).toBe(0);
    expect(response.run.finishedAt).toBe("2026-07-07T17:01:00+00:00");
    expect(calls).toHaveLength(2);
    expect(calls[1]).toMatchObject({
      path: "/openevo-api/desktop/run",
      method: "GET",
    });
    expect(calls[1].headers.get("X-OpenEvo-Sidecar-Token")).toBe("poll-token");
  });

  it("sends the sidecar mutation token on project config requests", async () => {
    const calls: Array<{ path: string; headers: Headers; body: any }> = [];
    const shellPayload = sidecarShellPayload("config-token");
    const savedPayload = {
      ...shellPayload,
      project: {
        ...shellPayload.project,
        name: "Configured Project",
      },
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input, init) => {
        const path = String(input);
        calls.push({
          path,
          headers: new Headers(init?.headers),
          body: init?.body ? JSON.parse(String(init.body)) : undefined,
        });
        if (path === "/openevo-api/desktop/shell") {
          return jsonResponse(shellPayload);
        }
        if (path === "/openevo-api/desktop/project-config") {
          return jsonResponse({
            config: {
              science_config_path:
                "/home/alice/.openevo/desktop/projects/configured/science.yaml",
              remote_profile_path:
                "/home/alice/.openevo/desktop/profiles/science-team.yaml",
            },
            status: savedPayload,
          });
        }
        return new Response("not found", { status: 404 });
      },
    );

    await fetchOpenEvoDesktopShellModel();
    const response = await saveOpenEvoProjectConfig(projectConfigDraft());

    expect(response.config.science_config_path).toContain("science.yaml");
    expect(response.status.project.name).toBe("Configured Project");
    expect(calls).toHaveLength(2);
    expect(calls[1]).toMatchObject({
      path: "/openevo-api/desktop/project-config",
    });
    expect(calls[1].headers.get("X-OpenEvo-Sidecar-Token")).toBe(
      "config-token",
    );
    expect(calls[1].body.remote_host).toBe("gpu.example.edu");
  });

  it("fetches saved project config summaries", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = String(input);
      if (path === "/openevo-api/desktop/project-configs") {
        return jsonResponse({
          configs: [
            {
              project_slug: "protein-design",
              valid: true,
              error: null,
              project_name: "Protein Design",
              task_id: "folding-baseline",
              objective: "Improve the folding baseline.",
              source_type: "git_repository",
              source_label: "https://example.com/repo.git@main",
              remote_profile_id: "science-team",
              remote_host: "gpu.example.edu",
              remote_user: "alice",
              science_config_path:
                "/home/alice/.openevo/desktop/projects/protein-design/science.yaml",
              remote_profile_path:
                "/home/alice/.openevo/desktop/profiles/science-team.yaml",
            },
            {
              project_slug: "broken-project",
              valid: false,
              error: "profiles/broken.yaml: not found",
              project_name: "Broken Project",
              task_id: "broken-task",
              objective: "Repair the config.",
              source_type: "remote_path",
              source_label: "/datasets/broken",
              remote_profile_id: "broken",
              remote_host: null,
              remote_user: null,
              science_config_path:
                "/home/alice/.openevo/desktop/projects/broken-project/science.yaml",
              remote_profile_path:
                "/home/alice/.openevo/desktop/profiles/broken.yaml",
            },
          ],
        });
      }
      return new Response("not found", { status: 404 });
    });

    const configs = await fetchOpenEvoProjectConfigs();

    expect(configs).toHaveLength(2);
    expect(configs[0]).toMatchObject({
      projectSlug: "protein-design",
      valid: true,
      projectName: "Protein Design",
      sourceLabel: "https://example.com/repo.git@main",
      remoteHost: "gpu.example.edu",
      scienceConfigPath:
        "/home/alice/.openevo/desktop/projects/protein-design/science.yaml",
    });
    expect(configs[1]).toMatchObject({
      projectSlug: "broken-project",
      valid: false,
      error: "profiles/broken.yaml: not found",
      remoteHost: null,
      remoteUser: null,
    });
  });

  it("activates a saved project config with the sidecar mutation token", async () => {
    const calls: Array<{ path: string; headers: Headers; method: string }> = [];
    const shellPayload = sidecarShellPayload("activate-token");
    const activatedPayload = {
      ...shellPayload,
      project: {
        ...shellPayload.project,
        name: "Activated Science Project",
      },
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input, init) => {
        const path = String(input);
        calls.push({
          path,
          headers: new Headers(init?.headers),
          method: init?.method ?? "GET",
        });
        if (path === "/openevo-api/desktop/shell") {
          return jsonResponse(shellPayload);
        }
        if (
          path ===
          "/openevo-api/desktop/project-configs/protein-design/activate"
        ) {
          return jsonResponse({
            config: {
              science_config_path:
                "/home/alice/.openevo/desktop/projects/protein-design/science.yaml",
              remote_profile_path:
                "/home/alice/.openevo/desktop/profiles/science-team.yaml",
            },
            status: activatedPayload,
          });
        }
        return new Response("not found", { status: 404 });
      },
    );

    await fetchOpenEvoDesktopShellModel();
    const response = await activateOpenEvoProjectConfig("protein-design");

    expect(response.status.project.name).toBe("Activated Science Project");
    expect(calls).toHaveLength(2);
    expect(calls[1]).toMatchObject({
      path: "/openevo-api/desktop/project-configs/protein-design/activate",
      method: "POST",
    });
    expect(calls[1].headers.get("X-OpenEvo-Sidecar-Token")).toBe(
      "activate-token",
    );
  });
});

function sidecarShellPayload(mutationToken: string) {
  return {
    remote: {
      id: "lab-gpu",
      host: "gpu.example.edu",
      port: 22,
      user: "alice",
      auth: {
        method: "ssh_agent" as const,
        private_key_path: null,
        password_ref: null,
        passphrase_ref: null,
      },
      workspace_root: "/home/alice/.openevo/workspaces",
      proxy: {
        http_proxy: null,
        https_proxy: "http://127.0.0.1:7890",
        no_proxy: null,
        pip_index_url: null,
        huggingface_endpoint: null,
        hf_home: null,
      },
    },
    project: {
      name: "Protein Folding Literature Sprint",
      task_id: "folding-baseline",
      source: "Remote path: /datasets/folding-baseline",
      objective: "Improve folding.",
    },
    execution: {
      mode: "codex_subscription_transcript" as const,
      model: "gpt-5.1-codex-mini",
      token_metrics_available: false,
    },
    bootstrap: {
      ready: true,
      state_root: "/home/alice/.openevo/runs/protein/folding",
      workspace_root: "/home/alice/.openevo/workspaces",
      readiness_notes: ["Remote bootstrap is ready."],
    },
    services: [],
    evolution: [],
    developer_mode: {
      enabled: false,
      benchmark_controls_visible: false,
    },
    sidecar: {
      mutation_token: mutationToken,
    },
  };
}

function sidecarRunReport({
  state = "running",
}: {
  state?: "running" | "succeeded" | "failed";
} = {}) {
  const succeeded = state === "succeeded";
  const failed = state === "failed";
  return {
    id: "run_20260707170000000000",
    state,
    ready: succeeded,
    command:
      "openevo run /home/alice/.openevo/runs/protein/folding/experiment.json --output-dir /home/alice/.openevo/runs/protein/folding/runs/run_20260707170000000000 --json",
    return_code: state === "running" ? null : succeeded ? 0 : 2,
    stdout: succeeded ? "ok" : "",
    stderr: failed ? "run failed" : "",
    output_dir:
      "/home/alice/.openevo/runs/protein/folding/runs/run_20260707170000000000",
    experiment_snapshot: "/home/alice/.openevo/runs/protein/folding/experiment.json",
    started_at: "2026-07-07T16:00:00+00:00",
    finished_at: state === "running" ? null : "2026-07-07T17:01:00+00:00",
  };
}

function projectConfigDraft() {
  return {
    project_name: "Protein Design",
    task_id: "folding-baseline",
    objective: "Improve the folding baseline.",
    source_type: "remote_path" as const,
    source_path: "/datasets/folding-baseline",
    remote_profile_id: "science-team",
    remote_host: "gpu.example.edu",
    remote_port: 22,
    remote_user: "alice",
    auth_method: "ssh_agent" as const,
    workspace_root: "/home/alice/.openevo/workspaces",
    https_proxy: "http://127.0.0.1:7890",
    huggingface_endpoint: "https://hf-mirror.com",
    codex_model: "gpt-5.1-codex-mini",
    text_memory: true,
    skill_bundle: true,
    agent_system: true,
  };
}

function jsonResponse(payload: object): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}
