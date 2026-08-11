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
const ACKNOWLEDGE_REQUIRED_HEADERS = [
  "content-type",
  "idempotency-key",
  "if-match",
  "x-openevo-desktop-session",
  "x-openevo-resource-generation",
] as const;
const ACKNOWLEDGE_PREFLIGHT_HEADERS = new Set(ACKNOWLEDGE_REQUIRED_HEADERS);
const OPAQUE_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

export function liveDesktopRequestAllowed(request: LiveDesktopRequest): boolean {
  if (request.requestOrigin !== request.liveOrigin) return false;
  const discovery = request.pathname === "/version" || request.pathname === "/health";
  const v2 = request.pathname.startsWith("/desktop/v2/");
  const token = header(request.headers, "x-openevo-desktop-session");
  if (request.method === "GET") {
    if (discovery) return token === undefined;
    return v2 && token === request.sessionToken;
  }
  const acknowledgement = acknowledgementPath(request.pathname);
  if (request.method === "POST") {
    return acknowledgement
      && token === request.sessionToken
      && header(request.headers, "origin") === request.staticOrigin
      && header(request.headers, "content-type") === "application/json"
      && ACKNOWLEDGE_REQUIRED_HEADERS.every((name) => {
        const value = header(request.headers, name);
        return value !== undefined && value.length > 0;
      });
  }
  if (request.method !== "OPTIONS" || !v2 || token !== undefined) return false;
  const requestedMethod = header(request.headers, "access-control-request-method");
  if (
    header(request.headers, "origin") !== request.staticOrigin
    || (requestedMethod !== "GET" && !(requestedMethod === "POST" && acknowledgement))
  ) return false;
  const requestedHeaders = (header(request.headers, "access-control-request-headers") ?? "")
    .split(",")
    .map((value) => value.trim().toLowerCase())
    .filter((value) => value.length > 0);
  if (requestedMethod === "POST") {
    return ACKNOWLEDGE_REQUIRED_HEADERS.every((name) => requestedHeaders.includes(name))
      && requestedHeaders.every((value) => ACKNOWLEDGE_PREFLIGHT_HEADERS.has(value));
  }
  return requestedHeaders.includes("x-openevo-desktop-session")
    && requestedHeaders.every((value) => PREFLIGHT_HEADERS.has(value));
}

function acknowledgementPath(pathname: string): boolean {
  const prefix = "/desktop/v2/operations/";
  const suffix = "/acknowledge";
  if (!pathname.startsWith(prefix) || !pathname.endsWith(suffix)) return false;
  return OPAQUE_ID.test(pathname.slice(prefix.length, -suffix.length));
}

function header(headers: Readonly<Record<string, string>>, name: string): string | undefined {
  for (const [candidate, value] of Object.entries(headers)) {
    if (candidate.toLowerCase() === name) return value;
  }
  return undefined;
}
