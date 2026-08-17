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
      text_memory: { enabled: true, method: "text_memory_reflector", config: {} },
    },
  },
};

describe("development agent provider", () => {
  it("restores persisted projects and real transcripts after creating a new provider", async () => {
    const methodIds = ["text_memory_reflector", "skill_bundle_reflector", "agent_system_reflector"];
    const supportedAxis = { state: "supported", message: "Supported", reason_code: null, missing_requirements: [] } as const;
    const support = { overall: "supported", execution: supportedAxis, capture: supportedAxis, harness: supportedAxis, runtime: supportedAxis } as const;
    const capabilities = {
      schema_version: "1",
      core_version: "development-catalog-unverified",
      registry_digest: "a".repeat(64),
      evaluated_profile: { execution_mode: "subscription", capture_mode: "transcript", harness_id: "codex", harness_capabilities: [], runtime_capabilities: [] },
      targets: methodIds.map((methodId, index) => {
        const targetId = ["text_memory", "skill_bundle", "agent_system"][index]!;
        const digest = (index + 1).toString().repeat(64);
        const method = {
          method_id: methodId, display_name: methodId, description: "Development method",
          exposure: "desktop", maturity: "experimental", execution_modes: ["subscription"],
          capture_modes: ["transcript"], supported_harness_ids: ["codex"], harness_requirements: [],
          runtime_requirements: [], input_bindings: [], output_artifact_types: [targetId],
          config_schema_json: '{"additionalProperties":false,"properties":{},"type":"object"}',
          default_config_json: "{}", implementation_identity_digest: digest, support,
        } as const;
        return {
          target_id: targetId, display_name: targetId, description: "Development target",
          artifact_type: targetId, exposure: "desktop", maturity: "experimental",
          handler_id: `${targetId}_handler`, configured_default_method_id: methodId,
          effective_default_method_id: methodId, configured_default_support: support,
          renderer_kind: targetId === "skill_bundle" ? "file_bundle" : "markdown",
          renderer_contract_version: "1", contribution_contract_version: "2", context_order: index,
          implementation_identity_digest: digest, handler_identity_digest: digest,
          accepted_methods: [{ method_id: methodId, implementation_identity_digest: method.implementation_identity_digest, support: method.support }],
          selection_resolvers: [],
          methods: [method],
        };
      }),
    };
    const projects: Record<string, unknown>[] = [];
    const sessions: Record<string, unknown>[] = [];
    const artifacts: Record<string, unknown>[] = [];
    const evolutionJobs: Record<string, unknown>[] = [];
    const workspaces: Record<string, unknown>[] = [];
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
          evolution_jobs: evolutionJobs,
          workspaces,
        });
      }
      if (url.endsWith("/capabilities")) {
        return jsonResponse({
          schema_version: "1",
          authority: "development_catalog_unverified",
          capabilities,
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
        workspaces.push({ project_id: body!.project_id, entries: [], truncated: false });
        return jsonResponse({ schema_version: "1" }, 201);
      }
      if (url.endsWith("/sessions") && init?.method === "POST") {
        const workspaceChanges = [{
          path: "src/answer.py",
          change_type: "created",
          byte_size: 9,
          media_type: "text/x-python",
          content: "print(4)\n",
          previous_path: null,
          diff_lines: [{ kind: "added", text: "print(4)" }],
        }];
        workspaces.splice(0, workspaces.length, {
          project_id: body!.project_id,
          entries: [{
            path: "src/answer.py",
            kind: "file",
            byte_size: 9,
            content_sha256: "d".repeat(64),
            media_type: "text/x-python",
            content: "print(4)\n",
            modified_at: "2026-08-14T10:01:01Z",
          }],
          truncated: false,
        });
        const evolved = {
          artifact_id: "dev-text-memory-1",
          project_id: body!.project_id,
          session_id: "dev-session-1",
          artifact_type: "text_memory",
          target_id: "text_memory",
          method: "text_memory_reflector",
          renderer_kind: "markdown",
          documents: [{ path: "memory.md", media_type: "text/markdown", content: "# Evolved memory\n\n- Verify arithmetic before answering.\n" }],
          manifest: { content_path: "memory.md" },
          content_path: "memory.md",
          content: "# Evolved memory\n\n- Verify arithmetic before answering.\n",
          content_sha256: "c".repeat(64),
          byte_size: 54,
          previous_artifact_id: null,
          created_at: "2026-08-14T10:01:02Z",
        };
        artifacts.push(evolved);
        evolutionJobs.push({
          job_id: "job-text-memory-dev-session-1",
          session_id: "dev-session-1",
          target_id: "text_memory",
          method_id: "text_memory_reflector",
          requested_method_id: "text_memory_reflector",
          resolver_input_artifact_ids: [],
          previous_artifact_id: null,
          config: {},
          state: "failed",
          artifact_ids: [],
          error: "temporary reflector failure",
          attempts: [{
            attempt_id: "job-text-memory-dev-session-1-attempt-1",
            job_id: "job-text-memory-dev-session-1",
            ordinal: 1,
            state: "failed",
            stage: "method_execution",
            artifact_ids: [],
            error_code: "method_execution_failed",
            error_message: "temporary reflector failure",
            logs: ["Running text_memory_reflector.", "Evolution attempt failed."],
            created_at: "2026-08-14T10:01:01Z",
            started_at: "2026-08-14T10:01:01Z",
            completed_at: "2026-08-14T10:01:02Z",
            updated_at: "2026-08-14T10:01:02Z",
          }],
          created_at: "2026-08-14T10:01:01Z",
          updated_at: "2026-08-14T10:01:02Z",
        });
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
          // Old development databases persisted these two fields before generic method config
          // became part of the session contract. The provider must keep those sessions readable.
          selected_evolution: [{ target_id: "text_memory", method: "text_memory_reflector" }],
          evolution_errors: [],
          workspace_changes: workspaceChanges,
          error: null,
          created_at: "2026-08-14T10:01:00Z",
          updated_at: "2026-08-14T10:01:01Z",
        });
        return jsonResponse({
          schema_version: "1",
          session_id: "dev-session-1",
          state: "running",
          status_url: "/openevo-dev-agent/v1/sessions/dev-session-1",
        }, 202);
      }
      if (url.endsWith("/evolution-jobs/job-text-memory-dev-session-1/retry") && init?.method === "POST") {
        Object.assign(evolutionJobs[0]!, {
          state: "completed",
          artifact_ids: ["dev-text-memory-1"],
          error: null,
          attempts: [
            ...(evolutionJobs[0]!.attempts as Record<string, unknown>[]),
            {
              attempt_id: "job-text-memory-dev-session-1-attempt-2",
              job_id: "job-text-memory-dev-session-1",
              ordinal: 2,
              state: "completed",
              stage: "completed",
              artifact_ids: ["dev-text-memory-1"],
              error_code: null,
              error_message: null,
              logs: ["Retry admitted with the original fixed inputs.", "Evolution attempt completed and published its outputs."],
              created_at: "2026-08-14T10:02:00Z",
              started_at: "2026-08-14T10:02:00Z",
              completed_at: "2026-08-14T10:02:01Z",
              updated_at: "2026-08-14T10:02:01Z",
            },
          ],
          updated_at: "2026-08-14T10:02:01Z",
        });
        return jsonResponse({ schema_version: "1", job: evolutionJobs[0] }, 202);
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
    expect(completed.snapshot.runtimePresentation?.tasks[task.task_id]?.transcript).toEqual([
      { speaker: "user", text: "What is two plus two?" },
      { speaker: "agent", text: "Two plus two is four." },
    ]);
    expect(completed.snapshot.runtimePresentation?.tasks[task.task_id]?.producedArtifactIds).toEqual(["dev-text-memory-1"]);
    expect(completed.snapshot.runtimePresentation?.tasks[task.task_id]?.outputFiles[0]).toMatchObject({
      name: "src/answer.py",
      content: "print(4)\n",
    });
    expect(completed.snapshot.runtimePresentation?.workspaces?.[project.project_id]?.entries[0]).toMatchObject({
      path: "src/answer.py",
      content: "print(4)\n",
    });
    expect(completed.snapshot.runtimePresentation?.tasks[task.task_id]?.selectedEvolution).toEqual([
      { targetId: "text_memory", method: "text_memory_reflector", config: {} },
    ]);
    expect(completed.snapshot.runtimePresentation?.tasks[task.task_id]?.evolutionJobs?.[0]).toMatchObject({
      jobId: "job-text-memory-dev-session-1",
      state: "failed",
      attempts: [{ ordinal: 1, stage: "method_execution", errorCode: "method_execution_failed" }],
    });
    await provider.retryEvolutionJob?.("job-text-memory-dev-session-1", {
      actionId: "retry-text-memory",
      streamEpoch: completed.snapshot.stream.epoch,
    });
    const retried = await provider.refresh();
    if (retried.status !== "fresh") throw new Error("retried provider was not fresh");
    expect(retried.snapshot.runtimePresentation?.tasks[task.task_id]?.evolutionJobs?.[0]).toMatchObject({
      state: "completed",
      attempts: [{ ordinal: 1, state: "failed" }, { ordinal: 2, state: "completed" }],
    });
    expect(completed.snapshot.artifacts.map((artifact) => artifact.artifact_id)).toEqual(["dev-text-memory-1"]);
    expect(completed.snapshot.runtimePresentation?.artifacts["dev-text-memory-1"]?.documents[0]?.content).toContain("Verify arithmetic");
    expect(fetchImpl).toHaveBeenCalledWith("/openevo-dev-agent/v1/sessions", expect.objectContaining({
      method: "POST",
      signal: expect.any(AbortSignal),
    }));

    const providerAfterPageReload = createDevelopmentAgentProvider({ fetchImpl });
    const restored = await providerAfterPageReload.refresh();
    if (restored.status !== "fresh") throw new Error("restored provider was not fresh");
    expect(restored.snapshot.projects.map((candidate) => candidate.display_name)).toEqual(["Live project"]);
    expect(restored.snapshot.tasks.map((candidate) => candidate.task_id)).toEqual(["dev-session-1"]);
    expect(restored.snapshot.runtimePresentation?.tasks["dev-session-1"]?.transcript).toEqual([
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
