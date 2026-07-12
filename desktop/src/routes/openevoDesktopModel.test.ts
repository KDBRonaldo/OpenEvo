import { describe, expect, it } from "vitest";
import {
  getOpenEvoDesktopShellModel,
  getOpenEvoTimelineSummary,
  normalizeOpenEvoExecutionMode,
  toDraftPayload,
  toProjectConfigPayload,
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

  it("preserves the complete canonical evolution target map in draft payloads", () => {
    const model = getOpenEvoDesktopShellModel();
    const evolutionTargets = {
      text_memory: {
        enabled: true,
        method: "custom_memory_method",
        config: { threshold: 0.75, nested: { mode: "strict" } },
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
    model.project.evolutionTargets = evolutionTargets;

    const draft = toDraftPayload(model);
    const payload = toProjectConfigPayload(draft);

    expect(payload.evolution).toEqual({
      targets: evolutionTargets,
    });
    expect(draft.evolution.targets).not.toBe(model.project.evolutionTargets);
    expect(payload.evolution.targets).not.toBe(draft.evolution.targets);
    expect(payload.evolution.targets.text_memory?.config).not.toBe(
      draft.evolution.targets.text_memory?.config,
    );
    expect(payload).not.toHaveProperty("text_memory");
    expect(payload).not.toHaveProperty("skill_bundle");
    expect(payload).not.toHaveProperty("agent_system");
  });

  it("rejects target config numbers that JavaScript cannot preserve", () => {
    const draft = toDraftPayload(getOpenEvoDesktopShellModel());
    draft.evolution.targets.future_target = {
      enabled: false,
      method: null,
      config: { unsafe: Number.MAX_SAFE_INTEGER + 1 },
    };

    expect(() => toProjectConfigPayload(draft)).toThrow(
      "integer exceeds the safe JSON range",
    );
  });

  it("rejects non-JSON target config instead of silently deleting it", () => {
    const draft = toDraftPayload(getOpenEvoDesktopShellModel());
    draft.evolution.targets.future_target = {
      enabled: false,
      method: null,
      config: { invalid: undefined } as never,
    };

    expect(() => toProjectConfigPayload(draft)).toThrow(
      "contains a non-JSON value",
    );
  });
});
