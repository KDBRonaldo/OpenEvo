import { z } from "zod";
import { evolutionCapabilitiesV2Schema, scienceProjectConfigV2Schema } from "../api/v2/schemas";
import {
  createDevelopmentAgentDesktopProductProvider,
  type DevelopmentAgentBackend,
  type DevelopmentAgentTurnRequest,
} from "./developmentAgentDesktopProvider";
import type { DesktopProductProviderV2 } from "./providerV2";

const artifactSchema = z.object({
  artifact_id: z.string().min(1),
  project_id: z.string().min(1),
  session_id: z.string().min(1),
  run_id: z.string().min(1).nullable().default(null),
  target_id: z.string().min(1),
  artifact_type: z.enum(["text_memory", "skill_bundle", "agent_system", "parametric_memory", "report"]),
  method: z.string().min(1),
  renderer_kind: z.enum(["markdown", "file_bundle", "structured_summary", "adapter"]),
  documents: z.array(z.object({
    path: z.string().min(1),
    media_type: z.string().min(1),
    content: z.string(),
  }).strict()),
  manifest: z.record(z.string(), z.unknown()),
  content_path: z.string().min(1).nullable(),
  content: z.string().nullable(),
  content_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  byte_size: z.number().int().nonnegative(),
  previous_artifact_id: z.string().min(1).nullable(),
  promoted: z.boolean().default(true),
  created_at: z.string().min(1),
}).strict();

const artifactV2Schema = artifactSchema.extend({
  schema_version: z.literal("2"),
  documents: z.array(z.object({
    schema_version: z.literal("2"),
    path: z.string().min(1),
    media_type: z.string().min(1),
    content: z.string(),
  }).strict()).max(128),
}).strict();

const artifactPageV2Schema = z.object({
  schema_version: z.literal("2"),
  items: z.array(artifactV2Schema).max(5),
  next_cursor: z.string().min(1).nullable(),
  has_more: z.boolean(),
}).strict();

const workspaceEntrySchema = z.object({
  path: z.string().min(1),
  kind: z.enum(["file", "directory", "symlink", "unreadable"]),
  byte_size: z.number().int().nonnegative(),
  content_sha256: z.string().regex(/^[0-9a-f]{64}$/).nullable(),
  media_type: z.string().min(1).nullable(),
  content: z.string().nullable(),
  modified_at: z.string().min(1),
}).strict();

const workspaceSnapshotSchema = z.object({
  project_id: z.string().min(1),
  entries: z.array(workspaceEntrySchema),
  truncated: z.boolean(),
}).strict();

const workspaceEntryV2Schema = workspaceEntrySchema.extend({
  schema_version: z.literal("2"),
}).strict();

const workspacePageV2Schema = z.object({
  schema_version: z.literal("2"),
  project_id: z.string().min(1),
  manifest_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  items: z.array(workspaceEntryV2Schema).max(100),
  next_cursor: z.string().min(1).max(512).nullable(),
  has_more: z.boolean(),
  truncated: z.boolean(),
}).strict();

const workspaceMutationV2Schema = z.object({
  schema_version: z.literal("2"),
  project_id: z.string().min(1),
  manifest_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  entry: workspaceEntryV2Schema,
}).strict();

const workspaceChangeSchema = z.object({
  path: z.string().min(1),
  change_type: z.enum(["created", "modified", "deleted"]),
  byte_size: z.number().int().nonnegative(),
  media_type: z.string().min(1).nullable(),
  content: z.string().nullable(),
  previous_path: z.string().min(1).nullable(),
  diff_lines: z.array(z.object({
    kind: z.enum(["added", "removed", "context"]),
    text: z.string(),
  }).strict()),
}).strict();

const runtimeActivationSchema = z.object({
  schema_version: z.literal("1"),
  adapter_id: z.string().min(1),
  fully_supported: z.boolean(),
  decisions: z.array(z.object({
    intent_id: z.string().min(1),
    feature_id: z.string().min(1),
    source_kind: z.string().min(1),
    source_contract_version: z.string().min(1),
    parameters: z.record(z.string(), z.unknown()),
    status: z.enum(["active", "delegated", "unsupported"]),
    owner: z.string().min(1),
    message: z.string().min(1),
  }).strict()),
}).strict();

const turnSubmissionSchema = z.object({
  schema_version: z.literal("1"),
  session_id: z.string().min(1),
  state: z.literal("running"),
  status_url: z.string().min(1),
}).strict();

const projectSchema = z.object({
  project_id: z.string().min(1),
  display_name: z.string().min(1),
  config: scienceProjectConfigV2Schema,
  created_at: z.string().min(1),
  updated_at: z.string().min(1),
}).strict();

const sessionSchema = z.object({
  session_id: z.string().min(1),
  project_id: z.string().min(1),
  task_title: z.string().min(1),
  instruction: z.string().min(1),
  response: z.string().nullable(),
  model: z.string().nullable(),
  state: z.enum(["running", "cancelling", "completed", "failed", "cancelled"]),
  duration_ms: z.number().int().nonnegative().nullable(),
  logs: z.array(z.string()),
  selected_evolution: z.array(z.object({
    target_id: z.string().min(1),
    method: z.string().min(1),
    config: z.record(z.string(), z.unknown()).default({}),
  }).strict()),
  evolution_errors: z.array(z.object({
    target_id: z.string().min(1),
    method: z.string().min(1),
    message: z.string().min(1),
  }).strict()),
  workspace_changes: z.array(workspaceChangeSchema).default([]),
  context_artifact_ids: z.array(z.string().min(1)).default([]),
  runtime_activation: runtimeActivationSchema.nullable().default(null),
  evolution_evidence_ready: z.boolean().default(false),
  error: z.string().nullable(),
  created_at: z.string().min(1),
  updated_at: z.string().min(1),
}).strict();

const evolutionRunSchema = z.object({
  run_id: z.string().min(1),
  project_id: z.string().min(1),
  source_session_ids: z.array(z.string().min(1)).min(1),
  selections: z.array(z.object({
    target_id: z.string().min(1),
    method: z.string().min(1),
    config: z.record(z.string(), z.unknown()).default({}),
  }).strict()).min(1),
  state: z.enum(["running", "candidate_ready", "applied", "failed"]),
  artifact_ids: z.array(z.string().min(1)),
  error: z.string().nullable(),
  created_at: z.string().min(1),
  updated_at: z.string().min(1),
}).strict();

const evolutionSelectionV2Schema = z.object({
  schema_version: z.literal("2"),
  target_id: z.string().min(1),
  method: z.string().min(1),
  config: z.record(z.string(), z.unknown()),
}).strict();

const evolutionRunV2Schema = z.object({
  schema_version: z.literal("2"),
  run_id: z.string().min(1),
  action_id: z.string().min(1),
  project_id: z.string().min(1),
  source_task_ids: z.array(z.string().min(1)).min(1).max(128),
  selections: z.array(evolutionSelectionV2Schema).min(1).max(64),
  state: z.enum(["running", "candidate_ready", "applied", "failed"]),
  artifact_ids: z.array(z.string().min(1)).max(256),
  error: z.string().max(32_000).nullable(),
  created_at: z.string().min(1),
  updated_at: z.string().min(1),
}).strict();

const evolutionRunPageV2Schema = z.object({
  schema_version: z.literal("2"),
  items: z.array(evolutionRunV2Schema).max(25),
  next_cursor: z.string().min(1).nullable(),
  has_more: z.boolean(),
}).strict();

const stateSchema = z.object({
  schema_version: z.literal("1"),
  active_project_id: z.string().min(1).nullable(),
  projects: z.array(projectSchema),
  sessions: z.array(sessionSchema),
  artifacts: z.array(artifactSchema),
  evolution_jobs: z.array(z.object({
    job_id: z.string().min(1),
    session_id: z.string().min(1),
    run_id: z.string().min(1).nullable().default(null),
    target_id: z.string().min(1),
    method_id: z.string().min(1),
    requested_method_id: z.string().min(1).optional(),
    resolver_input_artifact_ids: z.array(z.string().min(1)).default([]),
    previous_artifact_id: z.string().min(1).nullable().default(null),
    config: z.record(z.string(), z.unknown()),
    state: z.enum(["queued", "running", "completed", "failed"]),
    artifact_ids: z.array(z.string().min(1)),
    error: z.string().nullable(),
    attempts: z.array(z.object({
      attempt_id: z.string().min(1),
      job_id: z.string().min(1),
      ordinal: z.number().int().positive(),
      state: z.enum(["queued", "running", "completed", "failed", "cancelled"]),
      stage: z.string().min(1),
      artifact_ids: z.array(z.string().min(1)),
      error_code: z.string().min(1).nullable(),
      error_message: z.string().min(1).nullable(),
      logs: z.array(z.string()),
      created_at: z.string().min(1),
      started_at: z.string().min(1).nullable(),
      completed_at: z.string().min(1).nullable(),
      updated_at: z.string().min(1),
    }).strict()).default([]),
    created_at: z.string().min(1),
    updated_at: z.string().min(1),
  }).strict()),
  evolution_runs: z.array(evolutionRunSchema).default([]),
  workspaces: z.array(workspaceSnapshotSchema).default([]),
}).strict();

const capabilityResponseSchema = z.object({
  schema_version: z.literal("1"),
  authority: z.literal("development_catalog_unverified"),
  capabilities: evolutionCapabilitiesV2Schema,
}).strict();

export interface DevelopmentAgentProviderOptions {
  readonly baseUrl?: string;
  readonly fetchImpl?: typeof fetch;
  readonly workspaceV2BaseUrl?: string;
  readonly artifactV2BaseUrl?: string;
  readonly evolutionV2BaseUrl?: string;
  readonly desktopSessionToken?: string;
}

/**
 * Browser-only development bridge. The Vite proxy owns the bearer credential and forwards these
 * same-origin requests through an SSH tunnel; Project and Session authority live in remote SQLite.
 */
export function createDevelopmentAgentProvider(
  options: DevelopmentAgentProviderOptions = {},
): DesktopProductProviderV2 {
  const baseUrl = (options.baseUrl ?? "/openevo-dev-agent/v1").replace(/\/$/, "");
  const fetchImpl = options.fetchImpl ?? fetch;
  const workspaceV2BaseUrl = options.workspaceV2BaseUrl?.replace(/\/$/, "");
  const artifactV2BaseUrl = options.artifactV2BaseUrl?.replace(/\/$/, "");
  const evolutionV2BaseUrl = options.evolutionV2BaseUrl?.replace(/\/$/, "");

  const digestHex = async (payload: ArrayBuffer): Promise<string> => Array.from(
    new Uint8Array(await globalThis.crypto.subtle.digest("SHA-256", payload)),
    (value) => value.toString(16).padStart(2, "0"),
  ).join("");

  const workspaceV2Fetch = async (
    projectId: string,
    suffix: string,
    init: RequestInit = {},
    timeoutMs = 60_000,
  ): Promise<Response> => {
    if (workspaceV2BaseUrl === undefined) throw new Error("Workspace v2 is not configured.");
    const controller = new AbortController();
    const timeout = globalThis.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const headers = new Headers(init.headers);
      if (options.desktopSessionToken !== undefined) {
        headers.set("X-OpenEvo-Desktop-Session", options.desktopSessionToken);
      }
      const response = await fetchImpl(
        `${workspaceV2BaseUrl}/${encodeURIComponent(projectId)}/workspace${suffix}`,
        {
          ...init,
          headers,
          signal: controller.signal,
        },
      );
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Remote development daemon failed (${response.status}): ${detail || response.statusText}`);
      }
      return response;
    } catch (error) {
      if (controller.signal.aborted) {
        throw new Error("The workspace request timed out. Check the SSH development tunnel.");
      }
      throw error;
    } finally {
      globalThis.clearTimeout(timeout);
    }
  };

  const loadWorkspaceV2 = async (projectId: string) => {
    const entries: z.infer<typeof workspaceEntrySchema>[] = [];
    let cursor: string | null = null;
    let manifest: string | null = null;
    let truncated = false;
    for (let pageNumber = 0; pageNumber < 10; pageNumber += 1) {
      const parameters = new URLSearchParams({ limit: "100" });
      if (cursor !== null) parameters.set("after", cursor);
      if (manifest !== null) parameters.set("manifest_sha256", manifest);
      const response = await workspaceV2Fetch(projectId, `?${parameters.toString()}`);
      const page = workspacePageV2Schema.parse(await response.json());
      if (page.project_id !== projectId || (manifest !== null && page.manifest_sha256 !== manifest)) {
        throw new Error("The workspace authority changed across one refresh.");
      }
      manifest = page.manifest_sha256;
      truncated = page.truncated;
      entries.push(...page.items.map(({ schema_version: _schemaVersion, ...entry }) => entry));
      if (!page.has_more) {
        if (page.next_cursor !== null) throw new Error("Workspace v2 returned an invalid terminal cursor.");
        return { project_id: projectId, entries, truncated };
      }
      if (page.next_cursor === null || page.next_cursor === cursor) {
        throw new Error("Workspace v2 returned a non-advancing cursor.");
      }
      cursor = page.next_cursor;
    }
    throw new Error("Workspace v2 exceeded the bounded 1000-entry inventory.");
  };

  const loadArtifactsV2 = async (projectId: string) => {
    if (artifactV2BaseUrl === undefined) throw new Error("Artifact v2 is not configured.");
    const artifacts: z.infer<typeof artifactSchema>[] = [];
    let cursor: string | null = null;
    for (let pageNumber = 0; pageNumber < 200; pageNumber += 1) {
      const parameters = new URLSearchParams({ project_id: projectId, limit: "5" });
      if (cursor !== null) parameters.set("after", cursor);
      const controller = new AbortController();
      const timeout = globalThis.setTimeout(() => controller.abort(), 60_000);
      try {
        const headers = new Headers();
        if (options.desktopSessionToken !== undefined) {
          headers.set("X-OpenEvo-Desktop-Session", options.desktopSessionToken);
        }
        const response = await fetchImpl(`${artifactV2BaseUrl}?${parameters.toString()}`, {
          headers,
          signal: controller.signal,
        });
        if (!response.ok) {
          const detail = await response.text();
          throw new Error(`Remote development daemon failed (${response.status}): ${detail || response.statusText}`);
        }
        const page = artifactPageV2Schema.parse(await response.json());
        for (const item of page.items) {
          if (item.project_id !== projectId) {
            throw new Error("Artifact v2 crossed project authority.");
          }
          const { schema_version: _schemaVersion, documents, ...artifact } = item;
          artifacts.push({
            ...artifact,
            documents: documents.map(({ schema_version: _documentSchemaVersion, ...document }) => document),
          });
        }
        if (!page.has_more) {
          if (page.next_cursor !== null) throw new Error("Artifact v2 returned an invalid terminal cursor.");
          return artifacts;
        }
        if (page.next_cursor === null || page.next_cursor === cursor) {
          throw new Error("Artifact v2 returned an invalid continuation cursor.");
        }
        cursor = page.next_cursor;
      } catch (error) {
        if (controller.signal.aborted) {
          throw new Error("The artifact request timed out. Check the SSH development tunnel.");
        }
        throw error;
      } finally {
        globalThis.clearTimeout(timeout);
      }
    }
    throw new Error("Artifact v2 exceeded the bounded pagination limit.");
  };

  const requestEvolutionV2 = async (
    suffix: string,
    init: RequestInit = {},
    timeoutMs = 60_000,
  ): Promise<unknown> => {
    if (evolutionV2BaseUrl === undefined) throw new Error("Evolution Run v2 is not configured.");
    const controller = new AbortController();
    const timeout = globalThis.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const headers = new Headers(init.headers);
      if (options.desktopSessionToken !== undefined) {
        headers.set("X-OpenEvo-Desktop-Session", options.desktopSessionToken);
      }
      const response = await fetchImpl(`${evolutionV2BaseUrl}${suffix}`, {
        ...init,
        headers,
        signal: controller.signal,
      });
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Remote development daemon failed (${response.status}): ${detail || response.statusText}`);
      }
      return response.json();
    } catch (error) {
      if (controller.signal.aborted) {
        throw new Error("The Evolution Run request timed out. Check the SSH development tunnel.");
      }
      throw error;
    } finally {
      globalThis.clearTimeout(timeout);
    }
  };

  const loadEvolutionRunsV2 = async (projectId: string) => {
    const runs: z.infer<typeof evolutionRunSchema>[] = [];
    let cursor: string | null = null;
    for (let pageNumber = 0; pageNumber < 40; pageNumber += 1) {
      const parameters = new URLSearchParams({ project_id: projectId, limit: "25" });
      if (cursor !== null) parameters.set("after", cursor);
      const page = evolutionRunPageV2Schema.parse(
        await requestEvolutionV2(`?${parameters.toString()}`),
      );
      for (const item of page.items) {
        if (item.project_id !== projectId) {
          throw new Error("Evolution Run v2 crossed project authority.");
        }
        runs.push({
          run_id: item.run_id,
          project_id: item.project_id,
          source_session_ids: item.source_task_ids,
          selections: item.selections.map((selection) => ({
            target_id: selection.target_id,
            method: selection.method,
            config: selection.config,
          })),
          state: item.state,
          artifact_ids: item.artifact_ids,
          error: item.error,
          created_at: item.created_at,
          updated_at: item.updated_at,
        });
      }
      if (!page.has_more) {
        if (page.next_cursor !== null) {
          throw new Error("Evolution Run v2 returned an invalid terminal cursor.");
        }
        return runs;
      }
      if (page.next_cursor === null || page.next_cursor === cursor) {
        throw new Error("Evolution Run v2 returned an invalid continuation cursor.");
      }
      cursor = page.next_cursor;
    }
    throw new Error("Evolution Run v2 exceeded the bounded pagination limit.");
  };

  const requestJson = async (
    path: string,
    init?: RequestInit,
    timeoutMs = 15_000,
  ): Promise<unknown> => {
    const controller = new AbortController();
    const timeout = globalThis.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetchImpl(`${baseUrl}${path}`, {
        ...init,
        signal: controller.signal,
      });
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Remote development daemon failed (${response.status}): ${detail || response.statusText}`);
      }
      return response.json();
    } catch (error) {
      if (controller.signal.aborted) {
        throw new Error(
          "The SSH development tunnel stopped responding. Restart the remote development launcher.",
        );
      }
      throw error;
    } finally {
      globalThis.clearTimeout(timeout);
    }
  };

  const requestBlob = async (path: string): Promise<{ data: Blob; mediaType: string }> => {
    const controller = new AbortController();
    const timeout = globalThis.setTimeout(() => controller.abort(), 60_000);
    try {
      const response = await fetchImpl(`${baseUrl}${path}`, { signal: controller.signal });
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Remote development daemon failed (${response.status}): ${detail || response.statusText}`);
      }
      return {
        data: await response.blob(),
        mediaType: response.headers.get("Content-Type") ?? "application/octet-stream",
      };
    } catch (error) {
      if (controller.signal.aborted) {
        throw new Error("The workspace download timed out. Check the SSH development tunnel.");
      }
      throw error;
    } finally {
      globalThis.clearTimeout(timeout);
    }
  };

  const backend: DevelopmentAgentBackend = {
    loadState: async () => {
      const [payload, capabilityPayload] = await Promise.all([
        requestJson("/state").then((value) => stateSchema.parse(value)),
        requestJson("/capabilities").then((value) => capabilityResponseSchema.parse(value)),
      ]);
      const [workspaces, artifacts, evolutionRuns] = await Promise.all([
        workspaceV2BaseUrl === undefined
          ? payload.workspaces
          : Promise.all(payload.projects.map((project) => loadWorkspaceV2(project.project_id))),
        artifactV2BaseUrl === undefined
          ? payload.artifacts
          : Promise.all(payload.projects.map((project) => loadArtifactsV2(project.project_id)))
            .then((pages) => pages.flat()),
        evolutionV2BaseUrl === undefined
          ? payload.evolution_runs
          : Promise.all(payload.projects.map((project) => loadEvolutionRunsV2(project.project_id)))
            .then((pages) => pages.flat()),
      ]);
      return {
        activeProjectId: payload.active_project_id,
        projects: payload.projects.map((project) => ({
          projectId: project.project_id,
          displayName: project.display_name,
          config: project.config,
          createdAt: project.created_at,
          updatedAt: project.updated_at,
        })),
        sessions: payload.sessions.map((session) => ({
          sessionId: session.session_id,
          projectId: session.project_id,
          taskTitle: session.task_title,
          instruction: session.instruction,
          response: session.response,
          model: session.model,
          state: session.state,
          durationMs: session.duration_ms,
          logMessages: session.logs,
          selectedEvolution: session.selected_evolution.map((selection) => ({
            targetId: selection.target_id,
            method: selection.method,
            config: selection.config,
          })),
          evolutionErrors: session.evolution_errors.map((error) => ({
            targetId: error.target_id,
            method: error.method,
            message: error.message,
          })),
          evolutionEvidenceReady: session.evolution_evidence_ready,
          workspaceChanges: session.workspace_changes.map(toWorkspaceChange),
          contextArtifactIds: session.context_artifact_ids,
          error: session.error,
          createdAt: session.created_at,
          updatedAt: session.updated_at,
        })),
        artifacts: artifacts.map(toPersistedArtifact),
        evolutionJobs: payload.evolution_jobs.map((job) => ({
          jobId: job.job_id,
          sessionId: job.session_id,
          runId: job.run_id,
          targetId: job.target_id,
          methodId: job.method_id,
          requestedMethodId: job.requested_method_id ?? job.method_id,
          resolverInputArtifactIds: job.resolver_input_artifact_ids,
          previousArtifactId: job.previous_artifact_id,
          config: job.config,
          state: job.state,
          artifactIds: job.artifact_ids,
          error: job.error,
          attempts: job.attempts.map((attempt) => ({
            attemptId: attempt.attempt_id,
            jobId: attempt.job_id,
            ordinal: attempt.ordinal,
            state: attempt.state,
            stage: attempt.stage,
            artifactIds: attempt.artifact_ids,
            errorCode: attempt.error_code,
            errorMessage: attempt.error_message,
            logs: attempt.logs,
            createdAt: attempt.created_at,
            startedAt: attempt.started_at,
            completedAt: attempt.completed_at,
            updatedAt: attempt.updated_at,
          })),
          createdAt: job.created_at,
          updatedAt: job.updated_at,
        })),
        evolutionRuns: evolutionRuns.map((run) => ({
          runId: run.run_id,
          projectId: run.project_id,
          sourceSessionIds: run.source_session_ids,
          selections: run.selections.map((selection) => ({
            targetId: selection.target_id,
            method: selection.method,
            config: selection.config,
          })),
          state: run.state,
          artifactIds: run.artifact_ids,
          error: run.error,
          createdAt: run.created_at,
          updatedAt: run.updated_at,
        })),
        workspaces: workspaces.map(toWorkspaceSnapshot),
        capabilities: capabilityPayload.capabilities,
      };
    },
    createProject: async (project) => {
      await requestJson("/projects", jsonRequest("POST", {
        schema_version: "1",
        project_id: project.projectId,
        display_name: project.displayName,
        config: project.config,
      }));
    },
    updateProject: async (project) => {
      await requestJson(`/projects/${encodeURIComponent(project.projectId)}`, jsonRequest("PUT", {
        schema_version: "1",
        display_name: project.displayName,
        config: project.config,
      }));
    },
    activateProject: async (projectId) => {
      await requestJson(`/projects/${encodeURIComponent(projectId)}/activate`, jsonRequest("POST", {
        schema_version: "1",
      }));
    },
    submitAgentTurn: async (request) => {
      const payload = turnSubmissionSchema.parse(await requestJson(
        "/sessions",
        jsonRequest("POST", toTurnRequestBody(request)),
      ));
      return {
        sessionId: payload.session_id,
        state: payload.state,
      };
    },
    cancelAgentTurn: async (sessionId) => {
      await requestJson(
        `/sessions/${encodeURIComponent(sessionId)}/cancel`,
        jsonRequest("POST", { schema_version: "1" }),
      );
    },
    retryEvolutionJob: async (jobId) => {
      await requestJson(
        `/evolution-jobs/${encodeURIComponent(jobId)}/retry`,
        jsonRequest("POST", { schema_version: "1" }),
      );
    },
    startEvolutionRun: async (projectId, sourceSessionIds, selections) => {
      const serializedSelections = selections.map((selection) => ({
        target_id: selection.targetId,
        method: selection.method,
        config: selection.config,
      }));
      if (evolutionV2BaseUrl === undefined) {
        await requestJson(
          "/evolution-runs",
          jsonRequest("POST", {
            schema_version: "1",
            project_id: projectId,
            session_ids: sourceSessionIds,
            selections: serializedSelections,
          }),
        );
        return;
      }
      const actionId = globalThis.crypto.randomUUID();
      const created = evolutionRunV2Schema.parse(await requestEvolutionV2(
        "",
        jsonRequest("POST", {
          schema_version: "2",
          action_id: actionId,
          project_id: projectId,
          source_task_ids: sourceSessionIds,
          selections: serializedSelections.map((selection) => ({
            schema_version: "2",
            ...selection,
          })),
        }),
      ));
      if (created.action_id !== actionId || created.project_id !== projectId) {
        throw new Error("Evolution Run v2 returned inconsistent creation authority.");
      }
    },
    applyEvolutionRun: async (runId) => {
      if (evolutionV2BaseUrl === undefined) {
        await requestJson(
          `/evolution-runs/${encodeURIComponent(runId)}/apply`,
          jsonRequest("POST", { schema_version: "1" }),
        );
        return;
      }
      const applied = evolutionRunV2Schema.parse(await requestEvolutionV2(
        `/${encodeURIComponent(runId)}/apply`,
        jsonRequest("POST", { schema_version: "2" }),
      ));
      if (applied.run_id !== runId || applied.state !== "applied") {
        throw new Error("Evolution Run v2 returned inconsistent apply authority.");
      }
    },
    uploadWorkspaceFile: async (projectId, path, data, mediaType, overwrite) => {
      if (workspaceV2BaseUrl !== undefined) {
        const bytes = await data.arrayBuffer();
        const contentSha256 = await digestHex(bytes);
        const response = await workspaceV2Fetch(
          projectId,
          `/files?path=${encodeURIComponent(path)}&overwrite=${overwrite}`,
          {
            method: "PUT",
            headers: {
              "Content-Type": mediaType || "application/octet-stream",
              "X-OpenEvo-Content-SHA256": contentSha256,
            },
            body: bytes,
          },
        );
        const mutation = workspaceMutationV2Schema.parse(await response.json());
        if (
          mutation.project_id !== projectId
          || mutation.entry.path !== path
          || mutation.entry.content_sha256 !== contentSha256
        ) {
          throw new Error("Workspace v2 upload receipt did not match the submitted file.");
        }
        return;
      }
      await requestJson(
        `/projects/${encodeURIComponent(projectId)}/workspace/files?path=${encodeURIComponent(path)}&overwrite=${overwrite}`,
        {
          method: "PUT",
          headers: { "Content-Type": mediaType || "application/octet-stream" },
          body: data,
        },
        60_000,
      );
    },
    downloadWorkspaceFile: async (projectId, path) => {
      const result = workspaceV2BaseUrl === undefined
        ? await requestBlob(
          `/projects/${encodeURIComponent(projectId)}/workspace/files?path=${encodeURIComponent(path)}`,
        )
        : await (async () => {
          const response = await workspaceV2Fetch(
            projectId,
            `/files?path=${encodeURIComponent(path)}`,
          );
          const expectedDigest = response.headers.get("X-OpenEvo-Content-SHA256");
          if (expectedDigest === null || !/^[0-9a-f]{64}$/.test(expectedDigest)) {
            throw new Error("Workspace v2 download omitted its content digest.");
          }
          const bytes = await response.arrayBuffer();
          if (await digestHex(bytes) !== expectedDigest) {
            throw new Error("Workspace v2 download failed content verification.");
          }
          return {
            data: new Blob([bytes], {
              type: response.headers.get("Content-Type") ?? "application/octet-stream",
            }),
            mediaType: response.headers.get("Content-Type") ?? "application/octet-stream",
          };
        })();
      return {
        ...result,
        fileName: path.split("/").at(-1) ?? "download",
      };
    },
  };

  return createDevelopmentAgentDesktopProductProvider(backend);
}

function toWorkspaceSnapshot(workspace: z.infer<typeof workspaceSnapshotSchema>) {
  return {
    projectId: workspace.project_id,
    entries: workspace.entries.map((entry) => ({
      path: entry.path,
      kind: entry.kind,
      byteSize: entry.byte_size,
      contentSha256: entry.content_sha256,
      mediaType: entry.media_type,
      content: entry.content,
      modifiedAt: entry.modified_at,
    })),
    truncated: workspace.truncated,
  };
}

function toWorkspaceChange(change: z.infer<typeof workspaceChangeSchema>) {
  return {
    path: change.path,
    changeType: change.change_type,
    byteSize: change.byte_size,
    mediaType: change.media_type,
    content: change.content,
    previousPath: change.previous_path,
    diffLines: change.diff_lines,
  };
}

function toPersistedArtifact(artifact: z.infer<typeof artifactSchema>) {
  return {
    artifactId: artifact.artifact_id,
    projectId: artifact.project_id,
    sessionId: artifact.session_id,
    runId: artifact.run_id,
    targetId: artifact.target_id,
    artifactType: artifact.artifact_type,
    method: artifact.method,
    rendererKind: artifact.renderer_kind,
    documents: artifact.documents.map((document) => ({
      path: document.path,
      mediaType: document.media_type,
      content: document.content,
    })),
    manifest: artifact.manifest,
    contentPath: artifact.content_path,
    content: artifact.content,
    contentSha256: artifact.content_sha256,
    byteSize: artifact.byte_size,
    previousArtifactId: artifact.previous_artifact_id,
    promoted: artifact.promoted,
    createdAt: artifact.created_at,
  };
}

function jsonRequest(method: "POST" | "PUT", body: object): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

function toTurnRequestBody(request: DevelopmentAgentTurnRequest) {
  return {
    schema_version: "1",
    project_id: request.projectId,
    project_name: request.projectName,
    task_title: request.taskTitle,
    instruction: request.instruction,
  };
}
