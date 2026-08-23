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
    const evolutionRuns: Record<string, unknown>[] = [];
    const workspaces: Record<string, unknown>[] = [];
    let activeProjectId: string | null = null;
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const body = typeof init?.body === "string"
        ? JSON.parse(init.body) as Record<string, unknown>
        : null;
      if (url.endsWith("/state")) {
        return jsonResponse({
          schema_version: "1",
          active_project_id: activeProjectId,
          projects,
          sessions,
          artifacts,
          evolution_jobs: evolutionJobs,
          evolution_runs: evolutionRuns,
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
          evolution_evidence_ready: true,
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
      if (url.endsWith("/evolution-runs") && init?.method === "POST") {
        evolutionRuns.push({
          run_id: "evolution-run-1",
          project_id: body!.project_id,
          source_session_ids: body!.session_ids,
          selections: body!.selections,
          state: "candidate_ready",
          artifact_ids: [],
          error: null,
          created_at: "2026-08-14T10:04:00Z",
          updated_at: "2026-08-14T10:04:01Z",
        });
        return jsonResponse({ schema_version: "1", run: evolutionRuns[0] }, 202);
      }
      if (url.endsWith("/evolution-runs/evolution-run-1/apply") && init?.method === "POST") {
        Object.assign(evolutionRuns[0]!, {
          state: "applied",
          updated_at: "2026-08-14T10:05:00Z",
        });
        return jsonResponse({ schema_version: "1", run: evolutionRuns[0] });
      }
      if (url.includes("/workspace/files?") && init?.method === "PUT") {
        const requestUrl = new URL(url, "http://localhost");
        const path = requestUrl.searchParams.get("path")!;
        const data = init.body instanceof Blob ? await init.body.text() : "";
        const workspace = workspaces[0] as { entries: Record<string, unknown>[] };
        workspace.entries = [
          ...workspace.entries.filter((entry) => entry.path !== path),
          {
            path,
            kind: "file",
            byte_size: new TextEncoder().encode(data).byteLength,
            content_sha256: "e".repeat(64),
            media_type: "text/plain",
            content: data,
            modified_at: "2026-08-14T10:03:00Z",
          },
        ];
        return jsonResponse({ schema_version: "1", project_id: activeProjectId, entry: workspace.entries.at(-1) }, 201);
      }
      if (url.includes("/workspace/files?") && !init?.method) {
        return new Response("uploaded evidence\n", {
          status: 200,
          headers: { "Content-Type": "text/plain" },
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
    expect(completed.snapshot.runtimePresentation?.tasks[task.task_id]?.evolutionEvidenceReady).toBe(true);
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
    await provider.startEvolutionRun?.(
      project.project_id,
      [task.task_id],
      [{ targetId: "text_memory", method: "text_memory_reflector", config: {} }],
      { actionId: "evolve-evidence", streamEpoch: retried.snapshot.stream.epoch },
    );
    let afterEvolution = await provider.refresh();
    if (afterEvolution.status !== "fresh") throw new Error("Evolution provider was not fresh");
    expect(afterEvolution.snapshot.runtimePresentation?.evolutionRuns?.[0]).toMatchObject({
      runId: "evolution-run-1",
      sourceTaskIds: [task.task_id],
      state: "candidate_ready",
    });
    await provider.applyEvolutionRun?.(
      "evolution-run-1",
      { actionId: "apply-evolution", streamEpoch: afterEvolution.snapshot.stream.epoch },
    );
    afterEvolution = await provider.refresh();
    if (afterEvolution.status !== "fresh") throw new Error("Applied Evolution provider was not fresh");
    expect(afterEvolution.snapshot.runtimePresentation?.evolutionRuns?.[0]?.state).toBe("applied");
    await provider.uploadWorkspaceFile?.(
      project.project_id,
      {
        path: "evidence.txt",
        data: new Blob(["uploaded evidence\n"], { type: "text/plain" }),
        mediaType: "text/plain",
        overwrite: false,
      },
      { actionId: "upload-evidence", streamEpoch: retried.snapshot.stream.epoch },
    );
    const afterUpload = await provider.refresh();
    if (afterUpload.status !== "fresh") throw new Error("uploaded provider was not fresh");
    expect(afterUpload.snapshot.runtimePresentation?.workspaces?.[project.project_id]?.entries).toEqual(
      expect.arrayContaining([expect.objectContaining({ path: "evidence.txt", content: "uploaded evidence\n" })]),
    );
    const downloaded = await provider.downloadWorkspaceFile?.(project.project_id, "evidence.txt");
    expect(downloaded?.fileName).toBe("evidence.txt");
    expect(await downloaded?.data.text()).toBe("uploaded evidence\n");
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

  it("uses authenticated daemon v2 workspace inventory and verified file transfer", async () => {
    const projectId = "project-workspace-v2";
    const workspaceConfig: ScienceProjectConfigV2 = {
      ...config,
      evolution: { targets: {} },
    };
    const sha256 = async (bytes: ArrayBuffer) => Array.from(
      new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
      (value) => value.toString(16).padStart(2, "0"),
    ).join("");
    const downloadedBytes = new TextEncoder().encode("verified download\n");
    const downloadedDigest = await sha256(downloadedBytes.buffer);
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/state")) {
        return jsonResponse({
          schema_version: "1",
          active_project_id: projectId,
          projects: [{
            project_id: projectId,
            display_name: "Workspace v2",
            config: workspaceConfig,
            created_at: "2026-08-23T00:00:00Z",
            updated_at: "2026-08-23T00:00:00Z",
          }],
          sessions: [], artifacts: [], evolution_jobs: [], evolution_runs: [],
          workspaces: [{ project_id: projectId, entries: [], truncated: false }],
        });
      }
      if (url.endsWith("/capabilities")) {
        return jsonResponse({
          schema_version: "1",
          authority: "development_catalog_unverified",
          capabilities: {
            schema_version: "1",
            core_version: "development",
            registry_digest: "a".repeat(64),
            evaluated_profile: {
              execution_mode: "subscription", capture_mode: "transcript", harness_id: "codex",
              harness_capabilities: [], runtime_capabilities: [],
            },
            targets: [],
          },
        });
      }
      if (url.includes(`/desktop/v2/development/projects/${projectId}/workspace?`)) {
        expect(new Headers(init?.headers).get("X-OpenEvo-Desktop-Session")).toBe("session-secret");
        return jsonResponse({
          schema_version: "2",
          project_id: projectId,
          manifest_sha256: "b".repeat(64),
          items: [{
            schema_version: "2",
            path: "existing.txt",
            kind: "file",
            byte_size: 9,
            content_sha256: "c".repeat(64),
            media_type: "text/plain",
            content: "existing\n",
            modified_at: "2026-08-23T00:00:00Z",
          }],
          next_cursor: null,
          has_more: false,
          truncated: false,
        });
      }
      if (url.includes("/workspace/files?") && init?.method === "PUT") {
        const bytes = init.body as ArrayBuffer;
        const digest = await sha256(bytes);
        expect(new Headers(init.headers).get("X-OpenEvo-Content-SHA256")).toBe(digest);
        return jsonResponse({
          schema_version: "2", project_id: projectId, manifest_sha256: "d".repeat(64),
          entry: {
            schema_version: "2", path: "upload.txt", kind: "file",
            byte_size: bytes.byteLength, content_sha256: digest, media_type: "text/plain",
            content: "upload\n", modified_at: "2026-08-23T00:00:01Z",
          },
        }, 201);
      }
      if (url.includes("/workspace/files?") && init?.method === undefined) {
        return new Response(downloadedBytes, {
          headers: {
            "Content-Type": "text/plain",
            "X-OpenEvo-Content-SHA256": downloadedDigest,
          },
        });
      }
      throw new Error(`Unexpected workspace v2 request: ${init?.method ?? "GET"} ${url}`);
    });
    const provider = createDevelopmentAgentProvider({
      fetchImpl,
      workspaceV2BaseUrl: "/desktop/v2/development/projects",
      desktopSessionToken: "session-secret",
    });

    const refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("workspace v2 provider was not fresh");
    expect(refreshed.snapshot.runtimePresentation?.workspaces?.[projectId]?.entries[0]?.path)
      .toBe("existing.txt");
    await provider.uploadWorkspaceFile?.(
      projectId,
      { path: "upload.txt", data: new Blob(["upload\n"]), mediaType: "text/plain", overwrite: false },
      { actionId: "upload-v2", streamEpoch: refreshed.snapshot.stream.epoch },
    );
    const downloaded = await provider.downloadWorkspaceFile?.(projectId, "download.txt");
    expect(await downloaded?.data.text()).toBe("verified download\n");
  });

  it("loads rich artifact presentation through the authenticated daemon v2 bridge", async () => {
    const projectId = "project-artifact-v2";
    const artifactId = "artifact-skill-v2";
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/state")) {
        return jsonResponse({
          schema_version: "1",
          active_project_id: projectId,
          projects: [{
            project_id: projectId,
            display_name: "Artifact v2",
            config,
            created_at: "2026-08-23T00:00:00Z",
            updated_at: "2026-08-23T00:00:00Z",
          }],
          sessions: [],
          artifacts: [],
          evolution_jobs: [],
          evolution_runs: [],
          workspaces: [],
        });
      }
      if (url.endsWith("/capabilities")) {
        return jsonResponse({
          schema_version: "1",
          authority: "development_catalog_unverified",
          capabilities: {
            schema_version: "1",
            core_version: "development",
            registry_digest: "a".repeat(64),
            evaluated_profile: {
              execution_mode: "subscription",
              capture_mode: "transcript",
              harness_id: "codex",
              harness_capabilities: [],
              runtime_capabilities: [],
            },
            targets: [],
          },
        });
      }
      if (url.includes("/desktop/v2/development/artifacts?")) {
        expect(url).toContain(`project_id=${projectId}`);
        expect(new Headers(init?.headers).get("X-OpenEvo-Desktop-Session")).toBe("session-secret");
        return jsonResponse({
          schema_version: "2",
          items: [{
            schema_version: "2",
            artifact_id: artifactId,
            project_id: projectId,
            session_id: "task-artifact-v2",
            run_id: null,
            target_id: "skill_bundle",
            artifact_type: "skill_bundle",
            method: "skill_bundle_reflector",
            renderer_kind: "file_bundle",
            documents: [{
              schema_version: "2",
              path: "SKILL.md",
              media_type: "text/markdown",
              content: "# Real daemon v2 skill\n",
            }],
            manifest: { content_path: "SKILL.md" },
            content_path: "SKILL.md",
            content: "# Real daemon v2 skill\n",
            content_sha256: "d".repeat(64),
            byte_size: 23,
            previous_artifact_id: null,
            promoted: false,
            created_at: "2026-08-23T00:00:01Z",
          }],
          next_cursor: null,
          has_more: false,
        });
      }
      throw new Error(`Unexpected Artifact v2 request: ${init?.method ?? "GET"} ${url}`);
    });
    const provider = createDevelopmentAgentProvider({
      fetchImpl,
      artifactV2BaseUrl: "/desktop/v2/development/artifacts",
      desktopSessionToken: "session-secret",
    });

    const refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("artifact v2 provider was not fresh");
    expect(refreshed.snapshot.runtimePresentation?.artifacts[artifactId]?.documents[0]?.content)
      .toBe("# Real daemon v2 skill\n");
  });

  it("loads, creates, and applies Evolution Runs through the authenticated daemon v2 bridge", async () => {
    const projectId = "project-evolution-v2";
    const runs: Record<string, unknown>[] = [{
      schema_version: "2",
      run_id: "evolution-run-v2-1",
      action_id: "action-evolution-v2-1",
      project_id: projectId,
      source_task_ids: ["task-evolution-v2"],
      selections: [{
        schema_version: "2",
        target_id: "text_memory",
        method: "text_memory_reflector",
        config: {},
      }],
      state: "candidate_ready",
      artifact_ids: ["artifact-memory-v2"],
      error: null,
      created_at: "2026-08-23T00:00:00Z",
      updated_at: "2026-08-23T00:00:01Z",
    }];
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/state")) {
        return jsonResponse({
          schema_version: "1",
          active_project_id: projectId,
          projects: [{
            project_id: projectId,
            display_name: "Evolution v2",
            config,
            created_at: "2026-08-23T00:00:00Z",
            updated_at: "2026-08-23T00:00:00Z",
          }],
          sessions: [],
          artifacts: [],
          evolution_jobs: [],
          evolution_runs: [{
            run_id: "legacy-run-that-must-be-ignored",
            project_id: projectId,
            source_session_ids: ["legacy-task"],
            selections: [{ target_id: "text_memory", method: "text_memory_reflector", config: {} }],
            state: "failed",
            artifact_ids: [],
            error: "legacy",
            created_at: "2026-08-22T00:00:00Z",
            updated_at: "2026-08-22T00:00:00Z",
          }],
          workspaces: [],
        });
      }
      if (url.endsWith("/capabilities")) {
        return jsonResponse({
          schema_version: "1",
          authority: "development_catalog_unverified",
          capabilities: {
            schema_version: "1",
            core_version: "development",
            registry_digest: "a".repeat(64),
            evaluated_profile: {
              execution_mode: "subscription",
              capture_mode: "transcript",
              harness_id: "codex",
              harness_capabilities: [],
              runtime_capabilities: [],
            },
            targets: [],
          },
        });
      }
      if (url.includes("/desktop/v2/development/evolution-runs?") && init?.method === undefined) {
        expect(new Headers(init?.headers).get("X-OpenEvo-Desktop-Session")).toBe("session-secret");
        return jsonResponse({ schema_version: "2", items: runs, next_cursor: null, has_more: false });
      }
      if (url.endsWith("/desktop/v2/development/evolution-runs") && init?.method === "POST") {
        const creation = JSON.parse(String(init.body)) as Record<string, unknown>;
        const created = {
          schema_version: "2",
          run_id: "evolution-run-v2-2",
          action_id: creation.action_id,
          project_id: creation.project_id,
          source_task_ids: creation.source_task_ids,
          selections: creation.selections,
          state: "running",
          artifact_ids: [],
          error: null,
          created_at: "2026-08-23T00:00:02Z",
          updated_at: "2026-08-23T00:00:02Z",
        };
        runs.push(created);
        return jsonResponse(created, 202);
      }
      if (url.endsWith("/desktop/v2/development/evolution-runs/evolution-run-v2-1/apply")) {
        const applied = { ...runs[0]!, state: "applied", updated_at: "2026-08-23T00:00:03Z" };
        runs[0] = applied;
        return jsonResponse(applied);
      }
      throw new Error(`Unexpected Evolution v2 request: ${init?.method ?? "GET"} ${url}`);
    });
    const provider = createDevelopmentAgentProvider({
      fetchImpl,
      evolutionV2BaseUrl: "/desktop/v2/development/evolution-runs",
      desktopSessionToken: "session-secret",
    });

    let refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("Evolution v2 provider was not fresh");
    expect(refreshed.snapshot.runtimePresentation?.evolutionRuns?.map((run) => run.runId))
      .toEqual(["evolution-run-v2-1"]);

    await provider.applyEvolutionRun?.("evolution-run-v2-1", {
      actionId: "apply-evolution-v2",
      streamEpoch: refreshed.snapshot.stream.epoch,
    });
    refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("Applied Evolution v2 provider was not fresh");
    expect(refreshed.snapshot.runtimePresentation?.evolutionRuns?.[0]?.state).toBe("applied");

    await provider.startEvolutionRun?.(
      projectId,
      ["task-evolution-v2"],
      [{ targetId: "text_memory", method: "text_memory_reflector", config: {} }],
      { actionId: "start-evolution-v2", streamEpoch: refreshed.snapshot.stream.epoch },
    );
    refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("Created Evolution v2 provider was not fresh");
    expect(refreshed.snapshot.runtimePresentation?.evolutionRuns?.[1]?.state).toBe("running");
    expect(fetchImpl.mock.calls.some(([input]) => String(input).includes("/openevo-dev-agent/v1/evolution-runs")))
      .toBe(false);
  });

  it("loads attempts and retries Evolution Jobs through the authenticated daemon v2 bridge", async () => {
    const projectId = "project-job-v2";
    const taskId = "task-job-v2";
    const jobId = "job-text-memory-v2";
    const jobs: Record<string, unknown>[] = [{
      schema_version: "2",
      job_id: jobId,
      project_id: projectId,
      task_id: taskId,
      run_id: null,
      target_id: "text_memory",
      method_id: "text_memory_reflector",
      requested_method_id: "text_memory_reflector",
      resolver_input_artifact_ids: [],
      previous_artifact_id: null,
      config: {},
      state: "failed",
      artifact_ids: [],
      error: "temporary failure",
      attempts: [{
        schema_version: "2",
        attempt_id: `${jobId}-attempt-1`,
        action_id: null,
        job_id: jobId,
        ordinal: 1,
        state: "failed",
        stage: "method_execution",
        artifact_ids: [],
        error_code: "method_execution_failed",
        error_message: "temporary failure",
        logs: ["Evolution attempt failed."],
        created_at: "2026-08-23T00:00:00Z",
        started_at: "2026-08-23T00:00:00Z",
        completed_at: "2026-08-23T00:00:01Z",
        updated_at: "2026-08-23T00:00:01Z",
      }],
      created_at: "2026-08-23T00:00:00Z",
      updated_at: "2026-08-23T00:00:01Z",
    }];
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/state")) {
        return jsonResponse({
          schema_version: "1",
          active_project_id: projectId,
          projects: [{
            project_id: projectId,
            display_name: "Evolution Job v2",
            config,
            created_at: "2026-08-23T00:00:00Z",
            updated_at: "2026-08-23T00:00:00Z",
          }],
          sessions: [{
            session_id: taskId,
            project_id: projectId,
            task_title: "Retry a method",
            instruction: "Produce reusable context.",
            response: "Captured evidence.",
            model: "test",
            state: "completed",
            duration_ms: 1,
            logs: [],
            selected_evolution: [],
            evolution_errors: [],
            workspace_changes: [],
            context_artifact_ids: [],
            runtime_activation: null,
            evolution_evidence_ready: true,
            error: null,
            created_at: "2026-08-23T00:00:00Z",
            updated_at: "2026-08-23T00:00:01Z",
          }],
          artifacts: [],
          evolution_jobs: [{
            job_id: "legacy-job-that-must-be-ignored",
            session_id: taskId,
            run_id: null,
            target_id: "text_memory",
            method_id: "text_memory_reflector",
            requested_method_id: "text_memory_reflector",
            resolver_input_artifact_ids: [],
            previous_artifact_id: null,
            config: {},
            state: "failed",
            artifact_ids: [],
            error: "legacy",
            attempts: [],
            created_at: "2026-08-22T00:00:00Z",
            updated_at: "2026-08-22T00:00:00Z",
          }],
          evolution_runs: [],
          workspaces: [],
        });
      }
      if (url.endsWith("/capabilities")) {
        return jsonResponse({
          schema_version: "1",
          authority: "development_catalog_unverified",
          capabilities: {
            schema_version: "1",
            core_version: "development",
            registry_digest: "a".repeat(64),
            evaluated_profile: {
              execution_mode: "subscription",
              capture_mode: "transcript",
              harness_id: "codex",
              harness_capabilities: [],
              runtime_capabilities: [],
            },
            targets: [],
          },
        });
      }
      if (url.includes("/desktop/v2/development/evolution-jobs?") && init?.method === undefined) {
        expect(new Headers(init?.headers).get("X-OpenEvo-Desktop-Session")).toBe("session-secret");
        return jsonResponse({ schema_version: "2", items: jobs, next_cursor: null, has_more: false });
      }
      if (url.endsWith(`/desktop/v2/development/evolution-jobs/${jobId}/retry`)) {
        const request = JSON.parse(String(init?.body)) as { action_id: string };
        const current = jobs[0]!;
        current.state = "running";
        current.error = null;
        current.attempts = [
          ...(current.attempts as Record<string, unknown>[]),
          {
            schema_version: "2",
            attempt_id: `${jobId}-attempt-2`,
            action_id: request.action_id,
            job_id: jobId,
            ordinal: 2,
            state: "running",
            stage: "input_resolution",
            artifact_ids: [],
            error_code: null,
            error_message: null,
            logs: ["Retry admitted with the original fixed inputs."],
            created_at: "2026-08-23T00:00:02Z",
            started_at: "2026-08-23T00:00:02Z",
            completed_at: null,
            updated_at: "2026-08-23T00:00:02Z",
          },
        ];
        return jsonResponse(current, 202);
      }
      throw new Error(`Unexpected Evolution Job v2 request: ${init?.method ?? "GET"} ${url}`);
    });
    const provider = createDevelopmentAgentProvider({
      fetchImpl,
      evolutionJobV2BaseUrl: "/desktop/v2/development/evolution-jobs",
      desktopSessionToken: "session-secret",
    });

    let refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("Evolution Job v2 provider was not fresh");
    expect(refreshed.snapshot.runtimePresentation?.tasks[taskId]?.evolutionJobs?.[0]).toMatchObject({
      jobId,
      state: "failed",
      attempts: [{ ordinal: 1, errorCode: "method_execution_failed" }],
    });

    await provider.retryEvolutionJob?.(jobId, {
      actionId: "retry-job-v2",
      streamEpoch: refreshed.snapshot.stream.epoch,
    });
    refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("Retried Evolution Job v2 provider was not fresh");
    expect(refreshed.snapshot.runtimePresentation?.tasks[taskId]?.evolutionJobs?.[0]).toMatchObject({
      jobId,
      state: "running",
      attempts: [{ ordinal: 1 }, { ordinal: 2, state: "running" }],
    });
    expect(fetchImpl.mock.calls.some(([input]) => String(input).includes("/openevo-dev-agent/v1/evolution-jobs")))
      .toBe(false);
  });
});

function jsonResponse(body: object, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
