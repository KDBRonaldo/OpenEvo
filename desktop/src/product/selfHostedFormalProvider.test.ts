import { describe, expect, it, vi } from "vitest";
import {
  combineSelfHostedProviders,
} from "./selfHostedFormalProvider";
import {
  unavailableDesktopProductProviderV2,
  type DesktopProductProviderV2,
  type DesktopProductSnapshotV2,
} from "./providerV2";

function providerWith(
  overrides: Partial<DesktopProductProviderV2>,
): DesktopProductProviderV2 {
  return { ...unavailableDesktopProductProviderV2, ...overrides };
}

describe("self-hosted formal provider", () => {
  it("keeps formal authority while merging readable development presentation", async () => {
    const formalSnapshot = { state: { active_project_id: "formal-project" } } as unknown as DesktopProductSnapshotV2;
    const runtimePresentation = {
      tasks: {
        "task-1": {
          instruction: null,
          transcript: [{ speaker: "agent" as const, text: "legacy answer" }],
          outputFiles: [],
          usedArtifactIds: [],
          producedArtifactIds: [],
        },
      },
      artifacts: {},
    } as const;
    const formalTask = { task_id: "task-1" } as DesktopProductSnapshotV2["tasks"][number];
    const formal = providerWith({
      refresh: vi.fn(async () => ({
        status: "fresh" as const,
        snapshot: { ...formalSnapshot, tasks: [formalTask] },
      })),
    });
    const presentation = providerWith({
      refresh: vi.fn(async () => ({
        status: "fresh" as const,
        snapshot: { runtimePresentation } as unknown as DesktopProductSnapshotV2,
      })),
    });

    const result = await combineSelfHostedProviders(formal, presentation).refresh();

    expect(result.status).toBe("fresh");
    if (result.status !== "fresh") throw new Error("expected a fresh snapshot");
    expect(result.snapshot.state.active_project_id).toBe("formal-project");
    expect(result.snapshot.runtimePresentation).toEqual(runtimePresentation);
  });

  it("reuses the all-Project presentation model when only the active Project changes", async () => {
    let activeProjectId = "project-1";
    let taskUpdatedAt = "2026-08-26T10:00:00Z";
    const formal = providerWith({
      refresh: vi.fn(async () => ({
        status: "fresh" as const,
        snapshot: {
          state: { active_project_id: activeProjectId },
          projects: [{ project_id: "project-1" }, { project_id: "project-2" }],
          tasks: [{ task_id: "task-1", updated_at: taskUpdatedAt }],
          artifacts: [],
        } as unknown as DesktopProductSnapshotV2,
      })),
    });
    const presentationRefresh = vi.fn(async () => ({
      status: "fresh" as const,
      snapshot: {
        runtimePresentation: { tasks: {}, artifacts: {} },
      } as unknown as DesktopProductSnapshotV2,
    }));
    const combined = combineSelfHostedProviders(
      formal,
      providerWith({ refresh: presentationRefresh }),
    );

    await combined.refresh();
    activeProjectId = "project-2";
    await combined.refresh();

    expect(presentationRefresh).toHaveBeenCalledTimes(1);

    taskUpdatedAt = "2026-08-26T10:00:01Z";
    await combined.refresh();
    expect(presentationRefresh).toHaveBeenCalledTimes(2);
  });

  it("routes project and task mutations formally but standalone Evolution explicitly to the proven bridge", async () => {
    const formalCreate = vi.fn();
    const formalSubmit = vi.fn();
    const formalEvolution = vi.fn();
    const presentationEvolution = vi.fn(async () => {});
    const formal = providerWith({
      createProject: formalCreate,
      submitTask: formalSubmit,
      startEvolutionRun: formalEvolution,
    });
    const presentation = providerWith({ startEvolutionRun: presentationEvolution });
    const combined = combineSelfHostedProviders(formal, presentation);

    expect(combined.createProject).not.toBe(presentation.createProject);
    expect(combined.submitTask).not.toBe(presentation.submitTask);
    await combined.startEvolutionRun?.("project-1", ["task-1"], [{
      targetId: "text_memory",
      method: "text_memory_expel_reflector",
      config: {},
    }], { actionId: "action-evolution-1", streamEpoch: 1 });

    expect(presentationEvolution).toHaveBeenCalledOnce();
    expect(formalEvolution).not.toHaveBeenCalled();
  });

  it("preserves the existing development product presentation flag", () => {
    const formal = providerWith({ featureFlags: ["development_agent_bridge_v2"] });
    const presentation = providerWith({ featureFlags: ["development_agent_bridge"] });

    expect(combineSelfHostedProviders(formal, presentation).featureFlags).toEqual([
      "development_agent_bridge_v2",
      "development_agent_bridge",
    ]);
  });
});
