import { describe, expect, it } from "vitest";
import { evolutionCapabilitiesV2Schema, projectV2Schema, taskV2Schema } from "../api/v2/schemas";
import { createFixtureDesktopProductProvider } from "./fixtureProvider";

describe("Desktop fixture provider", () => {
  it("takes a newly created project through ready admission and a simulated session", async () => {
    const provider = createFixtureDesktopProductProvider();
    const initial = await provider.refresh();
    if (initial.status !== "fresh") throw new Error("fixture did not publish a fresh snapshot");
    const source = initial.snapshot.projects[0]!;

    await provider.createProject({
      profileId: initial.snapshot.state.active_profile_id!,
      displayName: "New fixture project",
      config: { ...source.config, task: { title: "Fixture task", objective: "Exercise the simulated session flow." } },
    }, { actionId: "create-project-test", streamEpoch: initial.snapshot.stream.epoch });

    const created = await provider.refresh();
    if (created.status !== "fresh") throw new Error("fixture project refresh failed");
    const project = created.snapshot.projects.find((candidate) => candidate.display_name === "New fixture project")!;
    expect(created.snapshot.projects).toHaveLength(initial.snapshot.projects.length + 1);
    expect(created.snapshot.projects.some((candidate) => candidate.project_id === source.project_id)).toBe(true);
    expect(project.state).toBe("ready");
    expect(project.active_project_head?.generation).toBe(0);
    expect(created.snapshot.state.active_project_id).toBe(project.project_id);
    expect(created.snapshot.capability?.capabilities.targets.map((target) => target.target_id)).toEqual([
      "text_memory",
      "skill_bundle",
      "agent_system",
    ]);
    expect(Object.values(project.config.evolution.targets).every((target) => target.enabled)).toBe(true);
    evolutionCapabilitiesV2Schema.parse(created.snapshot.capability?.capabilities);

    const task = await provider.submitTask(project.project_id, {
      actionId: "submit-task-test",
      streamEpoch: created.snapshot.stream.epoch,
    });
    expect(task.project_id).toBe(project.project_id);
    expect(task.state).toBe("closed");
    taskV2Schema.parse(task);

    const afterRun = await provider.refresh();
    if (afterRun.status !== "fresh") throw new Error("fixture task refresh failed");
    expect(afterRun.snapshot.tasks.some((candidate) => candidate.task_id === task.task_id)).toBe(true);
    expect(afterRun.snapshot.fixturePresentation?.tasks[task.task_id]?.transcript).toHaveLength(2);
    expect(afterRun.snapshot.fixturePresentation?.tasks[task.task_id]?.usedArtifactIds).toEqual([]);
    expect(afterRun.snapshot.fixturePresentation?.tasks[task.task_id]?.producedArtifactIds).toHaveLength(3);
    expect(afterRun.snapshot.projects.find((candidate) => candidate.project_id === project.project_id)?.active_project_head?.generation).toBe(1);
    for (const artifactId of afterRun.snapshot.fixturePresentation?.tasks[task.task_id]?.producedArtifactIds ?? []) {
      expect(afterRun.snapshot.fixturePresentation?.artifacts[artifactId]?.documents[0]?.content).toContain("Session 1:");
    }

    const secondTask = await provider.submitTask(project.project_id, {
      actionId: "submit-second-task-test",
      streamEpoch: afterRun.snapshot.stream.epoch,
    });
    const afterSecondRun = await provider.refresh();
    if (afterSecondRun.status !== "fresh") throw new Error("second fixture task refresh failed");
    const secondPresentation = afterSecondRun.snapshot.fixturePresentation?.tasks[secondTask.task_id];
    expect(secondPresentation?.usedArtifactIds).toEqual(
      afterRun.snapshot.fixturePresentation?.tasks[task.task_id]?.producedArtifactIds,
    );
    expect(secondPresentation?.producedArtifactIds).toHaveLength(3);
    const secondMemoryId = secondPresentation?.producedArtifactIds.find((artifactId) => artifactId.includes("text_memory"));
    expect(secondMemoryId).toBeTruthy();
    expect(afterSecondRun.snapshot.fixturePresentation?.artifacts[secondMemoryId!]?.documents[0]?.content).toContain("Session 1:");
    expect(afterSecondRun.snapshot.fixturePresentation?.artifacts[secondMemoryId!]?.documents[0]?.content).toContain("Session 2:");
    expect((await provider.getArtifactDiff(secondMemoryId!)).status).toBe("available");
    const evolvedProject = afterSecondRun.snapshot.projects.find((candidate) => candidate.project_id === project.project_id)!;
    expect(evolvedProject.active_project_head?.generation).toBe(2);
    expect(evolvedProject.active_project_head?.evolution_revision.artifact_count).toBe(3);
    projectV2Schema.parse(evolvedProject);

    await provider.activateProject(source.project_id, {
      actionId: "activate-original-project-test",
      streamEpoch: afterSecondRun.snapshot.stream.epoch,
    });
    const switchedBack = await provider.refresh();
    if (switchedBack.status !== "fresh") throw new Error("fixture project activation failed");
    expect(switchedBack.snapshot.state.active_project_id).toBe(source.project_id);
    expect(switchedBack.snapshot.projects).toHaveLength(2);
    expect(switchedBack.snapshot.tasks.filter((candidate) => candidate.project_id === project.project_id)).toHaveLength(2);
    expect(switchedBack.snapshot.tasks.filter((candidate) => candidate.project_id === source.project_id)).toHaveLength(1);
  });
});
