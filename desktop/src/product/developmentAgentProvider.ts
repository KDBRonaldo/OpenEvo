import { z } from "zod";
import {
  createDevelopmentAgentDesktopProductProvider,
  type DevelopmentAgentTurnRequest,
} from "./fixtureProvider";
import type { DesktopProductProviderV2 } from "./providerV2";

const responseSchema = z.object({
  schema_version: z.literal("1"),
  session_id: z.string().min(1),
  response: z.string().min(1),
  model: z.string().min(1).nullable(),
  duration_ms: z.number().int().nonnegative(),
  logs: z.array(z.string()),
}).strict();

export interface DevelopmentAgentProviderOptions {
  readonly endpoint?: string;
  readonly fetchImpl?: typeof fetch;
}

/**
 * Browser-only development bridge. The Vite proxy owns the bearer credential and forwards this
 * same-origin request through an SSH tunnel; no remote token or SSH detail reaches renderer code.
 */
export function createDevelopmentAgentProvider(
  options: DevelopmentAgentProviderOptions = {},
): DesktopProductProviderV2 {
  const endpoint = options.endpoint ?? "/openevo-dev-agent/v1/sessions";
  const fetchImpl = options.fetchImpl ?? fetch;
  return createDevelopmentAgentDesktopProductProvider(async (request) => {
    const response = await fetchImpl(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(toRequestBody(request)),
    });
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(`Remote development agent failed (${response.status}): ${detail || response.statusText}`);
    }
    const payload = responseSchema.parse(await response.json());
    return {
      sessionId: payload.session_id,
      responseText: payload.response,
      model: payload.model,
      durationMs: payload.duration_ms,
      logMessages: payload.logs,
    };
  });
}

function toRequestBody(request: DevelopmentAgentTurnRequest) {
  return {
    schema_version: "1",
    project_id: request.projectId,
    project_name: request.projectName,
    task_title: request.taskTitle,
    instruction: request.instruction,
  };
}
