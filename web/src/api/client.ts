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
  const response = await fetch(path, init);
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

export const api = {
  get: <T>(path: string, headers?: HeadersInit) =>
    request<T>("GET", path, undefined, headers),
  post: <T>(path: string, body: unknown, headers?: HeadersInit) =>
    request<T>("POST", path, body, headers),
  delete: <T>(path: string) => request<T>("DELETE", path),
};
