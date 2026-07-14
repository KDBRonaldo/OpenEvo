import { invoke } from "@tauri-apps/api/core";

type DesktopFeatureFlagV1 =
  | "remote_profiles"
  | "project_validation"
  | "operation_events"
  | "run_observability"
  | "artifact_inspection"
  | "service_control"
  | "diagnostics"
  | "maintenance";

interface DesktopBootstrapContextV1 {
  schema_version: "1";
  endpoint: string;
  session_token: string;
  negotiated_contract: {
    major: 1;
    openapi_sha256: string;
    provider_kind: "desktop_sidecar";
    feature_flags: DesktopFeatureFlagV1[];
  };
}

declare global {
  interface Window {
    __TAURI__?: unknown;
    __TAURI_INTERNALS__?: unknown;
  }
}

let sidecarStartPromise: Promise<DesktopBootstrapContextV1> | null = null;

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  headers?: HeadersInit,
): Promise<T> {
  const requestHeaders = new Headers(headers);
  if (body) {
    requestHeaders.set("Content-Type", "application/json");
  }
  const init: RequestInit = {
    method,
    headers: requestHeaders,
    body: body ? JSON.stringify(body) : undefined,
  };
  const resolved = await resolveRequest(path);
  if (resolved.sessionToken) {
    requestHeaders.set("X-OpenEvo-Desktop-Session", resolved.sessionToken);
  }
  const response = await fetch(resolved.url, init);
  if (!response.ok) {
    let detail: any;
    try {
      detail = await response.json();
    } catch {
      detail = await response.text();
    }
    const error = new Error(
      `HTTP ${response.status} ${response.statusText}: ${
        typeof detail === "string" ? detail : JSON.stringify(detail)
      }`,
    );
    (error as any).status = response.status;
    (error as any).detail = detail;
    throw error;
  }
  const contentType = response.headers.get("Content-Type") || "";
  if (contentType.includes("application/json")) {
    return (await response.json()) as T;
  }
  return (await response.text()) as unknown as T;
}

async function resolveRequest(
  path: string,
): Promise<{ url: string; sessionToken?: string }> {
  if (!path.startsWith("/openevo-api/") || !isTauriRuntime()) {
    return { url: path };
  }
  const context = await ensureTauriSidecar();
  return {
    url: `${new URL(context.endpoint).origin}${path}`,
    sessionToken: context.session_token,
  };
}

function isTauriRuntime(): boolean {
  if (typeof window === "undefined") {
    return false;
  }
  return Boolean(window.__TAURI_INTERNALS__ || window.__TAURI__);
}

async function ensureTauriSidecar(): Promise<DesktopBootstrapContextV1> {
  if (!sidecarStartPromise) {
    sidecarStartPromise = invoke<DesktopBootstrapContextV1>("start_sidecar")
      .catch((error) => {
        sidecarStartPromise = null;
        throw error;
      });
  }
  return sidecarStartPromise;
}

export function resetOpenEvoSidecarForTests() {
  sidecarStartPromise = null;
}

export const api = {
  get: <T>(path: string, headers?: HeadersInit) =>
    request<T>("GET", path, undefined, headers),
  post: <T>(path: string, body: unknown, headers?: HeadersInit) =>
    request<T>("POST", path, body, headers),
  delete: <T>(path: string) => request<T>("DELETE", path),
};
