import { describe, expect, it } from "vitest";
import { toOpenEvoDesktopShellModel } from "./openevo";

describe("OpenEvo sidecar client", () => {
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
});
