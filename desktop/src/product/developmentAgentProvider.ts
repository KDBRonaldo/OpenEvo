import { z } from "zod";
import { evolutionCapabilitiesV2Schema, scienceProjectConfigV2Schema } from "../api/v2/schemas";
import {
  createDevelopmentAgentDesktopProductProvider,
  type DevelopmentAgentBackend,
  type DevelopmentAgentTurnRequest,
} from "./fixtureProvider";
import type { DesktopProductProviderV2 } from "./providerV2";

const artifactSchema = z.object({
  artifact_id: z.string().min(1),
  project_id: z.string().min(1),
  session_id: z.string().min(1),
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
  created_at: z.string().min(1),
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

const turnResponseSchema = z.object({
  schema_version: z.literal("1"),
  session_id: z.string().min(1),
  response: z.string().min(1),
  model: z.string().min(1).nullable(),
  duration_ms: z.number().int().nonnegative(),
  logs: z.array(z.string()),
  evolution_artifacts: z.array(artifactSchema).optional(),
  evolution_errors: z.array(z.object({
    target_id: z.string().min(1),
    method: z.string().min(1),
    message: z.string().min(1),
  }).strict()).optional(),
  workspace_changes: z.array(workspaceChangeSchema).default([]),
  workspace: workspaceSnapshotSchema.optional(),
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
  state: z.enum(["running", "completed", "failed"]),
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
  error: z.string().nullable(),
  created_at: z.string().min(1),
  updated_at: z.string().min(1),
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
    target_id: z.string().min(1),
    method_id: z.string().min(1),
    config: z.record(z.string(), z.unknown()),
    state: z.enum(["queued", "running", "completed", "failed"]),
    artifact_ids: z.array(z.string().min(1)),
    error: z.string().nullable(),
    created_at: z.string().min(1),
    updated_at: z.string().min(1),
  }).strict()),
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

  const requestJson = async (path: string, init?: RequestInit): Promise<unknown> => {
    const controller = new AbortController();
    const timeoutMs = path === "/sessions" ? 20 * 60_000 : 15_000;
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
          path === "/sessions"
            ? "The remote Session timed out. Check the daemon log before retrying."
            : "The SSH development tunnel stopped responding. Restart the remote development launcher.",
        );
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
          workspaceChanges: session.workspace_changes.map(toWorkspaceChange),
          error: session.error,
          createdAt: session.created_at,
          updatedAt: session.updated_at,
        })),
        artifacts: payload.artifacts.map(toPersistedArtifact),
        evolutionJobs: payload.evolution_jobs.map((job) => ({
          jobId: job.job_id,
          sessionId: job.session_id,
          targetId: job.target_id,
          methodId: job.method_id,
          config: job.config,
          state: job.state,
          artifactIds: job.artifact_ids,
          error: job.error,
        })),
        workspaces: payload.workspaces.map(toWorkspaceSnapshot),
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
    runAgentTurn: async (request) => {
      const payload = turnResponseSchema.parse(await requestJson(
        "/sessions",
        jsonRequest("POST", toTurnRequestBody(request)),
      ));
      return {
        sessionId: payload.session_id,
        responseText: payload.response,
        model: payload.model,
        durationMs: payload.duration_ms,
        logMessages: payload.logs,
        evolutionArtifacts: (payload.evolution_artifacts ?? []).map(toPersistedArtifact),
        evolutionErrors: (payload.evolution_errors ?? []).map((error) => ({
          targetId: error.target_id,
          method: error.method,
          message: error.message,
        })),
        workspaceChanges: payload.workspace_changes.map(toWorkspaceChange),
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
