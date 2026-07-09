import { describe, expect, it } from "vitest";
import {
  getOpenEvoDesktopShellModel,
  getOpenEvoTimelineSummary,
  normalizeOpenEvoExecutionMode,
  toDraftPayload,
} from "./openevoDesktopModel";

describe("OpenEvo Desktop shell model", () => {
  it("starts in setup-required state without demo-ready fixture data", () => {
    const model = getOpenEvoDesktopShellModel();

    expect(model.project.name).toBe("Untitled Science Project");
    expect(model.remote.id).toBe("not-configured");
    expect(model.remote.host).toBe("");
    expect(model.execution.mode).toBe("codex_subscription_transcript");
    expect(model.execution.tokenMetricsAvailable).toBe(false);
    expect(model.developerMode.enabled).toBe(false);
    expect(model.developerMode.benchmarkControlsVisible).toBe(false);
    expect(model.bootstrap.ready).toBe(false);
    expect(model.bootstrap.readinessNotes).toEqual([
      "Configure a project and remote backend to begin.",
    ]);
    expect(JSON.stringify(model)).not.toContain("Protein Folding Literature Sprint");
    expect(JSON.stringify(model)).not.toContain("gpu.example.edu");
  });

  it("summarizes setup-required readiness and evolution progress", () => {
    const model = getOpenEvoDesktopShellModel();
    const summary = getOpenEvoTimelineSummary(model);

    expect(summary.readyServices).toBe(0);
    expect(summary.totalServices).toBe(4);
    expect(summary.bootstrapReady).toBe(false);
    expect(summary.completedEvolutionSteps).toBe(0);
    expect(summary.totalEvolutionSteps).toBe(3);
    expect(summary.readinessNotes).toEqual([
      "Configure a project and remote backend to begin.",
    ]);
  });

  it("normalizes legacy managed local inference mode to self-deployed", () => {
    expect(normalizeOpenEvoExecutionMode("codex_managed_local_inference")).toBe(
      "self-deployed",
    );
    expect(normalizeOpenEvoExecutionMode("self-deployed")).toBe("self-deployed");
  });

  it("normalizes legacy execution mode in draft payloads", () => {
    const model = {
      ...getOpenEvoDesktopShellModel(),
      execution: {
        ...getOpenEvoDesktopShellModel().execution,
        mode: normalizeOpenEvoExecutionMode("codex_managed_local_inference"),
      },
    };

    expect(toDraftPayload(model).execution_mode).toBe("self-deployed");
  });
});
