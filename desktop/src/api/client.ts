import { invoke } from "@tauri-apps/api/core";

interface TauriSidecarStatus {
  state: string;
  port: number | null;
  pid: number | null;
  url: string | null;
  command: string | null;
}

declare global {
  interface Window {
    __TAURI__?: unknown;
    __TAURI_INTERNALS__?: unknown;
  }
}

let sidecarStartPromise: Promise<TauriSidecarStatus> | null = null;

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
  const response = await fetch(await resolveRequestUrl(path), init);
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

async function resolveRequestUrl(path: string): Promise<string> {
  if (!path.startsWith("/openevo-api/") || !isTauriRuntime()) {
    return path;
  }
  const status = await ensureTauriSidecar();
  const baseUrl = sidecarBaseUrl(status);
  return `${baseUrl}${path}`;
}

function isTauriRuntime(): boolean {
  if (typeof window === "undefined") {
    return false;
  }
  return Boolean(window.__TAURI_INTERNALS__ || window.__TAURI__);
}

async function ensureTauriSidecar(): Promise<TauriSidecarStatus> {
  if (!sidecarStartPromise) {
    sidecarStartPromise = invoke<TauriSidecarStatus>("start_sidecar")
      .then((status) => {
        if (status.state !== "running" && status.state !== "starting") {
          throw new Error(`OpenEvo sidecar is not running: ${status.state}`);
        }
        if (status.port === null && status.url === null) {
          throw new Error("OpenEvo sidecar did not provide a local API endpoint");
        }
        return status;
      })
      .catch((error) => {
        sidecarStartPromise = null;
        throw error;
      });
  }
  return sidecarStartPromise;
}

function sidecarBaseUrl(status: TauriSidecarStatus): string {
  if (status.url) {
    return new URL(status.url).origin;
  }
  if (status.port !== null) {
    return `http://127.0.0.1:${status.port}`;
  }
  throw new Error("OpenEvo sidecar did not provide a local API endpoint");
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
