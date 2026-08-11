import { describe, expect, it } from "vitest";
import { liveDesktopRequestAllowed } from "./release-live-network-boundary";

const STATIC_ORIGIN = "http://tauri.localhost";
const LIVE_ORIGIN = "http://127.0.0.1:41235";
const SESSION_TOKEN = "release-session-token";
const ACKNOWLEDGE_PATH = `/desktop/v2/operations/operation-${"a".repeat(64)}/acknowledge`;
const ACKNOWLEDGE_HEADERS = {
  origin: STATIC_ORIGIN,
  "content-type": "application/json",
  "idempotency-key": "lifecycle-ack-operation-1",
  "if-match": `"${"b".repeat(64)}"`,
  "x-openevo-desktop-session": SESSION_TOKEN,
  "x-openevo-resource-generation": "0",
};

function request(overrides: Partial<Parameters<typeof liveDesktopRequestAllowed>[0]> = {}) {
  return {
    staticOrigin: STATIC_ORIGIN,
    liveOrigin: LIVE_ORIGIN,
    requestOrigin: LIVE_ORIGIN,
    method: "OPTIONS",
    pathname: "/desktop/v2/state",
    headers: {
      origin: STATIC_ORIGIN,
      "access-control-request-method": "GET",
      "access-control-request-headers": "cache-control, x-openevo-desktop-session",
    },
    sessionToken: SESSION_TOKEN,
    ...overrides,
  };
}

describe("live renderer Desktop network boundary", () => {
  it("admits the closed authenticated GET preflight", () => {
    expect(liveDesktopRequestAllowed(request())).toBe(true);
    expect(liveDesktopRequestAllowed(request({
      headers: {
        origin: STATIC_ORIGIN,
        "access-control-request-method": "GET",
        "access-control-request-headers": (
          "Cache-Control, Last-Event-ID, Pragma, X-OpenEvo-Desktop-Session"
        ),
      },
    }))).toBe(true);
  });

  it.each([
    ["wrong request origin", { requestOrigin: "http://127.0.0.1:41236" }],
    ["wrong renderer origin", {
      headers: {
        origin: "https://tauri.localhost.example",
        "access-control-request-method": "GET",
        "access-control-request-headers": "x-openevo-desktop-session",
      },
    }],
    ["wrong requested method", {
      headers: {
        origin: STATIC_ORIGIN,
        "access-control-request-method": "POST",
        "access-control-request-headers": "x-openevo-desktop-session",
      },
    }],
    ["discovery path", { pathname: "/version" }],
    ["non-v2 path", { pathname: "/desktop/v1/state" }],
    ["actual session token", {
      headers: {
        origin: STATIC_ORIGIN,
        "access-control-request-method": "GET",
        "access-control-request-headers": "x-openevo-desktop-session",
        "x-openevo-desktop-session": SESSION_TOKEN,
      },
    }],
    ["missing requested session header", {
      headers: {
        origin: STATIC_ORIGIN,
        "access-control-request-method": "GET",
        "access-control-request-headers": "cache-control",
      },
    }],
    ["unknown requested header", {
      headers: {
        origin: STATIC_ORIGIN,
        "access-control-request-method": "GET",
        "access-control-request-headers": "x-not-allowed, x-openevo-desktop-session",
      },
    }],
  ])("rejects preflight with %s", (_label, overrides) => {
    expect(liveDesktopRequestAllowed(request(overrides))).toBe(false);
  });

  it("enforces ordinary GET discovery and authenticated v2 rules", () => {
    expect(liveDesktopRequestAllowed(request({
      method: "GET",
      pathname: "/version",
      headers: {},
    }))).toBe(true);
    expect(liveDesktopRequestAllowed(request({
      method: "GET",
      pathname: "/desktop/v2/state",
      headers: { "x-openevo-desktop-session": SESSION_TOKEN },
    }))).toBe(true);
    expect(liveDesktopRequestAllowed(request({
      method: "GET",
      pathname: "/version",
      headers: { "x-openevo-desktop-session": SESSION_TOKEN },
    }))).toBe(false);
    expect(liveDesktopRequestAllowed(request({
      method: "GET",
      pathname: "/desktop/v2/state",
      headers: {},
    }))).toBe(false);
    expect(liveDesktopRequestAllowed(request({
      method: "POST",
      pathname: "/desktop/v2/state",
      headers: { "x-openevo-desktop-session": SESSION_TOKEN },
    }))).toBe(false);
  });

  it("admits only the closed terminal-operation acknowledgement mutation", () => {
    expect(liveDesktopRequestAllowed(request({
      method: "POST",
      pathname: ACKNOWLEDGE_PATH,
      headers: ACKNOWLEDGE_HEADERS,
    }))).toBe(true);
    expect(liveDesktopRequestAllowed(request({
      method: "POST",
      pathname: "/desktop/v2/tasks/task-1/cancel",
      headers: ACKNOWLEDGE_HEADERS,
    }))).toBe(false);
    expect(liveDesktopRequestAllowed(request({
      method: "POST",
      pathname: "/desktop/v2/operations/operation-1/acknowledge/extra",
      headers: ACKNOWLEDGE_HEADERS,
    }))).toBe(false);
    for (const requiredHeader of [
      "content-type",
      "idempotency-key",
      "if-match",
      "x-openevo-desktop-session",
      "x-openevo-resource-generation",
    ]) {
      const headers = { ...ACKNOWLEDGE_HEADERS };
      delete headers[requiredHeader as keyof typeof headers];
      expect(liveDesktopRequestAllowed(request({
        method: "POST",
        pathname: ACKNOWLEDGE_PATH,
        headers,
      })), requiredHeader).toBe(false);
    }
  });

  it("admits only the closed terminal-operation acknowledgement preflight", () => {
    const headers = {
      origin: STATIC_ORIGIN,
      "access-control-request-method": "POST",
      "access-control-request-headers": (
        "Content-Type, Idempotency-Key, If-Match, X-OpenEvo-Desktop-Session, "
        + "X-OpenEvo-Resource-Generation"
      ),
    };
    expect(liveDesktopRequestAllowed(request({
      pathname: ACKNOWLEDGE_PATH,
      headers,
    }))).toBe(true);
    expect(liveDesktopRequestAllowed(request({
      pathname: "/desktop/v2/tasks/task-1/cancel",
      headers,
    }))).toBe(false);
    expect(liveDesktopRequestAllowed(request({
      pathname: ACKNOWLEDGE_PATH,
      headers: {
        ...headers,
        "access-control-request-headers": `${headers["access-control-request-headers"]}, X-Not-Allowed`,
      },
    }))).toBe(false);
    expect(liveDesktopRequestAllowed(request({
      pathname: ACKNOWLEDGE_PATH,
      headers: {
        ...headers,
        "access-control-request-headers": `${headers["access-control-request-headers"]}, Last-Event-ID`,
      },
    }))).toBe(false);
  });
});
