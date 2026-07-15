// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from "vitest";
import { createFixtureDesktopProductProvider, type FixtureDesktopProductProvider } from "./fixtureProvider";

vi.mock("react-dom/client", () => ({
  default: {
    createRoot: () => ({ render: () => undefined }),
  },
}));

import {
  PRODUCT_PREVIEW_SCENARIOS,
  createProductPreviewProvider,
  previewScenario,
  type PreviewScenario,
} from "./preview";

const providers: FixtureDesktopProductProvider[] = [];

afterEach(() => {
  for (const provider of providers) provider.dispose();
  providers.length = 0;
});

describe("product preview scenarios", () => {
  it("keeps a closed scenario set and defaults unknown requests to completed", () => {
    expect(Object.keys(PRODUCT_PREVIEW_SCENARIOS).sort()).toEqual([
      "completed",
      "degraded",
      "failed",
      "new-user",
      "offline",
      "online",
    ]);
    expect(previewScenario("?scenario=failed")).toBe("failed");
    expect(previewScenario("?scenario=unknown")).toBe("completed");
  });

  it.each(["offline", "online", "completed", "degraded", "failed"] satisfies PreviewScenario[])(
    "%s uses the supported subscription release profile without a saved self-deployed project",
    async (scenario) => {
      const provider = createProductPreviewProvider(scenario);
      providers.push(provider);
      const refreshed = await provider.refresh();
      if (refreshed.status !== "fresh") throw new Error("Preview fixture refresh was not fresh.");
      const project = refreshed.snapshot.projects[0];

      expect(project?.execution).toEqual({
        mode: "codex_subscription_transcript",
        capture_mode: "transcript",
        token_level_metrics_available: false,
        codex_model: "gpt-5.5",
        hf_model: null,
      });
      expect(refreshed.snapshot.executionModeCapabilities.modes).toEqual(expect.arrayContaining([
        expect.objectContaining({ mode: "codex_subscription_transcript", support_state: "supported" }),
        expect.objectContaining({ mode: "self-deployed", support_state: "unavailable" }),
      ]));
      expect(refreshed.snapshot.runs.every((run) => run.execution_mode === "codex_subscription_transcript")).toBe(true);
      if (scenario !== "offline") {
        expect(refreshed.snapshot.capability).toMatchObject({
          status: "ready",
          executionMode: "codex_subscription_transcript",
          value: { capabilities: { evaluated_profile: { execution_mode: "subscription" } } },
        });
      }
      expect(refreshed.snapshot.artifacts.every((artifact) =>
        artifact.compatibility.execution_modes.length === 1
        && artifact.compatibility.execution_modes[0] === "codex_subscription_transcript"
        && artifact.compatibility.base_model_refs.length === 1
        && artifact.compatibility.base_model_refs[0] === "gpt-5.5"
      )).toBe(true);
      expect(refreshed.snapshot.artifacts.some((artifact) => artifact.artifact_type === "parametric_memory")).toBe(false);
    },
  );

  it.each(["completed", "degraded"] satisfies PreviewScenario[])(
    "%s tells a complete predecessor-to-active-successor story with three active text targets",
    async (scenario) => {
      const provider = createProductPreviewProvider(scenario);
      providers.push(provider);
      const refreshed = await provider.refresh();
      if (refreshed.status !== "fresh") throw new Error("Preview fixture refresh was not fresh.");
      const project = refreshed.snapshot.projects[0];
      const run = refreshed.snapshot.runs[0];
      const activeRevision = project?.remote?.active_revision;

      expect(run?.status).toBe("succeeded");
      expect(run?.pinned_revision?.generation).toBe(1);
      expect(run?.required_revision).toEqual({
        revision: run?.pinned_revision,
        reachable_from_revision_id: run?.pinned_revision?.id,
        relation: "active",
      });
      expect(run?.revision_transition?.state).toBe("active");
      expect(run?.revision_transition?.predecessor_revision).toEqual(run?.pinned_revision);
      expect(run?.revision_transition?.successor_revision.generation).toBe(2);
      expect(activeRevision).toEqual(run?.revision_transition?.successor_revision);

      const activeArtifacts = refreshed.snapshot.artifacts.filter((artifact) =>
        activeRevision && artifact.membership_revisions.some((revision) => revision.id === activeRevision.id),
      );
      expect(activeArtifacts.map((artifact) => artifact.artifact_type).sort()).toEqual([
        "agent_system",
        "skill_bundle",
        "text_memory",
      ]);
    },
  );

  it("keeps failed preview history failed with no fabricated successor or artifacts", async () => {
    const provider = createProductPreviewProvider("failed");
    providers.push(provider);
    const refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("Preview fixture refresh was not fresh.");
    const run = refreshed.snapshot.runs[0];

    expect(refreshed.snapshot.runs).toHaveLength(1);
    expect(run?.status).toBe("failed");
    expect(run?.pinned_revision?.generation).toBe(1);
    expect(run?.pinned_revision).toEqual(refreshed.snapshot.projects[0]?.remote?.active_revision);
    expect(run?.revision_transition).toBeNull();
    expect(refreshed.snapshot.projects[0]?.remote?.active_revision?.generation).toBe(1);
    expect(refreshed.snapshot.artifacts).toEqual([]);
  });

  it("retains an explicit generic simulator path for parametric-memory subtype coverage", async () => {
    const provider = createFixtureDesktopProductProvider({
      startOnline: true,
      seedCompletedRun: true,
      includeParametricMemory: true,
    });
    providers.push(provider);
    const refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("Fixture refresh was not fresh.");
    const parametric = refreshed.snapshot.artifacts.find((artifact) =>
      artifact.artifact_type === "parametric_memory" && artifact.produced_revision.generation === 2,
    );

    expect(parametric?.release_enabled).toBe(false);
    expect(parametric?.compatibility).toEqual({
      execution_modes: ["self-deployed"],
      harness_ids: ["codex"],
      base_model_refs: ["open-models/research-model-fixture-1"],
    });
  });
});
