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
  it("restores persisted projects and real transcripts after creating a new provider", async () => {
    const projects: Record<string, unknown>[] = [];
    const sessions: Record<string, unknown>[] = [];
    const artifacts: Record<string, unknown>[] = [];
    let activeProjectId: string | null = null;
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : null;
      if (url.endsWith("/state")) {
        return jsonResponse({
          schema_version: "1",
          active_project_id: activeProjectId,
          projects,
          sessions,
          artifacts,
        });
      }
      if (url.endsWith("/projects") && init?.method === "POST") {
        activeProjectId = String(body!.project_id);
        projects.push({
          project_id: body!.project_id,
          display_name: body!.display_name,
          config: body!.config,
          created_at: "2026-08-14T10:00:00Z",
          updated_at: "2026-08-14T10:00:00Z",
        });
        return jsonResponse({ schema_version: "1" }, 201);
      }
      if (url.endsWith("/sessions") && init?.method === "POST") {
        const evolved = {
          artifact_id: "dev-text-memory-1",
          project_id: body!.project_id,
          session_id: "dev-session-1",
          artifact_type: "text_memory",
          method: "text_memory_reflector",
          content_path: "memory.md",
          content: "# Evolved memory\n\n- Verify arithmetic before answering.\n",
          content_sha256: "c".repeat(64),
          byte_size: 54,
          previous_artifact_id: null,
          created_at: "2026-08-14T10:01:02Z",
        };
        artifacts.push(evolved);
        sessions.push({
          session_id: "dev-session-1",
          project_id: body!.project_id,
          task_title: body!.task_title,
          instruction: body!.instruction,
          response: "Two plus two is four.",
          model: null,
          state: "completed",
          duration_ms: 42,
          logs: ["Remote development daemon admitted the session.", "Codex completed the session."],
          selected_evolution: [{ target_id: "text_memory", method: "text_memory_reflector" }],
          evolution_errors: [],
          error: null,
          created_at: "2026-08-14T10:01:00Z",
          updated_at: "2026-08-14T10:01:01Z",
        });
        return jsonResponse({
          schema_version: "1",
          session_id: "dev-session-1",
          response: "Two plus two is four.",
          model: null,
          duration_ms: 42,
          logs: ["Remote development daemon admitted the session.", "Codex completed the session."],
          evolution_artifacts: [evolved],
          evolution_errors: [],
        });
      }
      throw new Error(`Unexpected development request: ${init?.method ?? "GET"} ${url}`);
    });
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
    expect(project.config.evolution.targets.text_memory).toEqual({
      enabled: true,
      method: "text_memory_reflector",
      config: {},
    });
    expect(created.snapshot.capability?.capabilities.targets[0]?.methods[0]?.method_id).toBe("text_memory_reflector");
    expect(created.snapshot.capability?.capabilities.targets.map((target) => target.methods[0]?.method_id)).toEqual([
      "text_memory_reflector",
      "skill_bundle_reflector",
      "agent_system_reflector",
    ]);

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
    expect(completed.snapshot.fixturePresentation?.tasks[task.task_id]?.producedArtifactIds).toEqual(["dev-text-memory-1"]);
    expect(completed.snapshot.artifacts.map((artifact) => artifact.artifact_id)).toEqual(["dev-text-memory-1"]);
    expect(completed.snapshot.fixturePresentation?.artifacts["dev-text-memory-1"]?.documents[0]?.content).toContain("Verify arithmetic");
    expect(fetchImpl).toHaveBeenCalledWith("/openevo-dev-agent/v1/sessions", expect.objectContaining({
      method: "POST",
    }));

    const providerAfterPageReload = createDevelopmentAgentProvider({ fetchImpl });
    const restored = await providerAfterPageReload.refresh();
    if (restored.status !== "fresh") throw new Error("restored provider was not fresh");
    expect(restored.snapshot.projects.map((candidate) => candidate.display_name)).toEqual(["Live project"]);
    expect(restored.snapshot.tasks.map((candidate) => candidate.task_id)).toEqual(["dev-session-1"]);
    expect(restored.snapshot.fixturePresentation?.tasks["dev-session-1"]?.transcript).toEqual([
      { speaker: "user", text: "What is two plus two?" },
      { speaker: "agent", text: "Two plus two is four." },
    ]);
  });
});

function jsonResponse(body: object, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
