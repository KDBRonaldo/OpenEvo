import { describe, expect, it, vi } from "vitest";
import type { ScienceProjectConfigV2 } from "../api/v2/schemas";
import { createDevelopmentAgentProvider } from "./developmentAgentProvider";

const config: ScienceProjectConfigV2 = {
  schema_version: "2",
  task: { title: "Real question", objective: "What is two plus two?" },
  workspace: { kind: "scratch", display_name: "Development workspace" },
  execution: {
    mode: "codex_subscription_transcript",
    capture_mode: "transcript",
    token_level_metrics_available: false,
    harness_id: "codex",
    codex_model: "gpt-5.3-codex-spark",
    reasoning_effort: "high",
    token_limit: 32_000,
    task_network_allow_internet: true,
  },
  evolution: {
    targets: {
      text_memory: { enabled: true, method: "ignored-in-live-agent-mode", config: {} },
    },
  },
};

describe("development agent provider", () => {
  it("shows the real bridge response and does not fabricate evolution output", async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({
      schema_version: "1",
      session_id: "dev-session-1",
      response: "Two plus two is four.",
      model: null,
      duration_ms: 42,
      logs: ["Remote development daemon admitted the session.", "Codex completed the session."],
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    const provider = createDevelopmentAgentProvider({ fetchImpl });
    const initial = await provider.refresh();
    if (initial.status !== "fresh") throw new Error("development provider was not fresh");
    expect(initial.snapshot.projects).toEqual([]);

    await provider.createProject({
      profileId: initial.snapshot.state.active_profile_id!,
      displayName: "Live project",
      config,
    }, { actionId: "create-live-project", streamEpoch: initial.snapshot.stream.epoch });
    const created = await provider.refresh();
    if (created.status !== "fresh") throw new Error("created provider was not fresh");
    const project = created.snapshot.projects[0]!;
    expect(Object.values(project.config.evolution.targets).every((target) => !target.enabled)).toBe(true);
    expect(created.snapshot.capability?.capabilities.targets).toEqual([]);

    const task = await provider.submitTask(project.project_id, {
      actionId: "submit-live-task",
      streamEpoch: created.snapshot.stream.epoch,
    });
    const completed = await provider.refresh();
    if (completed.status !== "fresh") throw new Error("completed provider was not fresh");
    expect(completed.snapshot.fixturePresentation?.tasks[task.task_id]?.transcript).toEqual([
      { speaker: "user", text: "What is two plus two?" },
      { speaker: "agent", text: "Two plus two is four." },
    ]);
    expect(completed.snapshot.fixturePresentation?.tasks[task.task_id]?.producedArtifactIds).toEqual([]);
    expect(completed.snapshot.artifacts).toEqual([]);
    expect(fetchImpl).toHaveBeenCalledWith("/openevo-dev-agent/v1/sessions", expect.objectContaining({
      method: "POST",
    }));
  });
});
