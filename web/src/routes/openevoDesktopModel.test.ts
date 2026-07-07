import { describe, expect, it } from "vitest";
import {
  getOpenEvoDesktopShellModel,
  getOpenEvoTimelineSummary,
} from "./openevoDesktopModel";

describe("OpenEvo Desktop shell model", () => {
  it("describes the science user flow without benchmark controls", () => {
    const model = getOpenEvoDesktopShellModel();

    expect(model.project.name).toBe("Protein Folding Literature Sprint");
    expect(model.execution.mode).toBe("codex_subscription_transcript");
    expect(model.execution.tokenMetricsAvailable).toBe(false);
    expect(model.developerMode.enabled).toBe(false);
    expect(model.developerMode.benchmarkControlsVisible).toBe(false);
    expect(model.bootstrap.ready).toBe(true);
    expect(model.bootstrap.readinessNotes).toEqual([
      "Codex subscription login available",
    ]);
    expect(model.remote.proxy.httpsProxy).toBe("http://127.0.0.1:7890");
  });

  it("summarizes readiness and evolution progress for the route", () => {
    const model = getOpenEvoDesktopShellModel();
    const summary = getOpenEvoTimelineSummary(model);

    expect(summary.readyServices).toBe(3);
    expect(summary.totalServices).toBe(4);
    expect(summary.bootstrapReady).toBe(true);
    expect(summary.completedEvolutionSteps).toBe(2);
    expect(summary.totalEvolutionSteps).toBe(4);
    expect(summary.readinessNotes).toEqual([
      "Codex subscription login available",
    ]);
  });
});
