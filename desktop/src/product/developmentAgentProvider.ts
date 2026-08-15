import { z } from "zod";
import { scienceProjectConfigV2Schema } from "../api/v2/schemas";
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
  artifact_type: z.enum(["text_memory", "skill_bundle", "agent_system"]),
  method: z.enum(["text_memory_reflector", "skill_bundle_reflector", "agent_system_reflector"]),
  content_path: z.enum(["memory.md", "SKILL.md", "AGENTS.md"]),
  content: z.string().min(1),
  content_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  byte_size: z.number().int().nonnegative(),
  previous_artifact_id: z.string().min(1).nullable(),
  created_at: z.string().min(1),
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
    target_id: z.enum(["text_memory", "skill_bundle", "agent_system"]),
    method: z.enum(["text_memory_reflector", "skill_bundle_reflector", "agent_system_reflector"]),
    message: z.string().min(1),
  }).strict()).optional(),
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
    target_id: z.enum(["text_memory", "skill_bundle", "agent_system"]),
    method: z.enum(["text_memory_reflector", "skill_bundle_reflector", "agent_system_reflector"]),
  }).strict()),
  evolution_errors: z.array(z.object({
    target_id: z.enum(["text_memory", "skill_bundle", "agent_system"]),
    method: z.enum(["text_memory_reflector", "skill_bundle_reflector", "agent_system_reflector"]),
    message: z.string().min(1),
  }).strict()),
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
    const response = await fetchImpl(`${baseUrl}${path}`, init);
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(`Remote development daemon failed (${response.status}): ${detail || response.statusText}`);
    }
    return response.json();
  };

  const backend: DevelopmentAgentBackend = {
    loadState: async () => {
      const payload = stateSchema.parse(await requestJson("/state"));
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
          })),
          evolutionErrors: session.evolution_errors.map((error) => ({
            targetId: error.target_id,
            method: error.method,
            message: error.message,
          })),
          error: session.error,
          createdAt: session.created_at,
          updatedAt: session.updated_at,
        })),
        artifacts: payload.artifacts.map(toPersistedArtifact),
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
      };
    },
  };

  return createDevelopmentAgentDesktopProductProvider(backend);
}

function toPersistedArtifact(artifact: z.infer<typeof artifactSchema>) {
  return {
    artifactId: artifact.artifact_id,
    projectId: artifact.project_id,
    sessionId: artifact.session_id,
    artifactType: artifact.artifact_type,
    method: artifact.method,
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
