import { describe, expect, it } from "vitest";
import { liveDesktopRequestAllowed } from "./release-live-network-boundary";

const STATIC_ORIGIN = "http://tauri.localhost";
const LIVE_ORIGIN = "http://127.0.0.1:41235";
const SESSION_TOKEN = "release-session-token";

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
});
