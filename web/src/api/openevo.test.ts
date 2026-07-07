import { afterEach, describe, expect, it, vi } from "vitest";
import {
  fetchOpenEvoDesktopShellModel,
  runOpenEvoBootstrap,
  runOpenEvoStartRun,
  runOpenEvoWorkspaceSync,
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
        user: "alice",
        proxy: {
          https_proxy: "http://127.0.0.1:7890",
          huggingface_endpoint: "https://hf-mirror.com",
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
    });

    expect(model.remote.proxy.httpsProxy).toBe("http://127.0.0.1:7890");
    expect(model.project.taskId).toBe("folding-baseline");
    expect(model.execution.tokenMetricsAvailable).toBe(false);
    expect(model.bootstrap.readinessNotes).toEqual([
      "Codex subscription login available",
    ]);
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
          user: "alice",
          proxy: {
            https_proxy: "http://127.0.0.1:7890",
            huggingface_endpoint: null,
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

    expect(response.run.ready).toBe(true);
    expect(response.run.output_dir).toBe(
      "/home/alice/.openevo/runs/protein/folding/runs/latest",
    );
    expect(calls).toHaveLength(2);
    expect(calls[1]).toMatchObject({ path: "/openevo-api/desktop/run" });
    expect(calls[1].headers.get("X-OpenEvo-Sidecar-Token")).toBe("run-token");
  });
});

function sidecarShellPayload(mutationToken: string) {
  return {
    remote: {
      id: "lab-gpu",
      host: "gpu.example.edu",
      user: "alice",
      proxy: {
        https_proxy: "http://127.0.0.1:7890",
        huggingface_endpoint: null,
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

function sidecarRunReport() {
  return {
    ready: true,
    status: "pass",
    command:
      "openevo run /home/alice/.openevo/runs/protein/folding/experiment.json --output-dir /home/alice/.openevo/runs/protein/folding/runs/latest --json",
    return_code: 0,
    stdout: "ok",
    stderr: "",
    output_dir: "/home/alice/.openevo/runs/protein/folding/runs/latest",
    experiment_snapshot: "/home/alice/.openevo/runs/protein/folding/experiment.json",
    started_at: "2026-07-07T16:00:00+00:00",
  };
}

function jsonResponse(payload: object): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}
