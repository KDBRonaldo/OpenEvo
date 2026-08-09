export type LiveDesktopRequest = {
  readonly staticOrigin: string;
  readonly liveOrigin: string;
  readonly requestOrigin: string;
  readonly method: string;
  readonly pathname: string;
  readonly headers: Readonly<Record<string, string>>;
  readonly sessionToken: string;
};

const PREFLIGHT_HEADERS = new Set([
  "cache-control",
  "last-event-id",
  "pragma",
  "x-openevo-desktop-session",
]);

export function liveDesktopRequestAllowed(request: LiveDesktopRequest): boolean {
  if (request.requestOrigin !== request.liveOrigin) return false;
  const discovery = request.pathname === "/version" || request.pathname === "/health";
  const v2 = request.pathname.startsWith("/desktop/v2/");
  const token = header(request.headers, "x-openevo-desktop-session");
  if (request.method === "GET") {
    if (discovery) return token === undefined;
    return v2 && token === request.sessionToken;
  }
  if (request.method !== "OPTIONS" || !v2 || token !== undefined) return false;
  if (
    header(request.headers, "origin") !== request.staticOrigin
    || header(request.headers, "access-control-request-method") !== "GET"
  ) return false;
  const requestedHeaders = (header(request.headers, "access-control-request-headers") ?? "")
    .split(",")
    .map((value) => value.trim().toLowerCase())
    .filter((value) => value.length > 0);
  return requestedHeaders.includes("x-openevo-desktop-session")
    && requestedHeaders.every((value) => PREFLIGHT_HEADERS.has(value));
}

function header(headers: Readonly<Record<string, string>>, name: string): string | undefined {
  for (const [candidate, value] of Object.entries(headers)) {
    if (candidate.toLowerCase() === name) return value;
  }
  return undefined;
}
