// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from "vitest";
import { invoke } from "@tauri-apps/api/core";
import { api, resetOpenEvoSidecarForTests } from "./client";

vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(),
}));

const SESSION_TOKEN = "s".repeat(32);

describe("OpenEvo API client host routing", () => {
  afterEach(() => {
    vi.clearAllMocks();
    vi.restoreAllMocks();
    resetOpenEvoSidecarForTests();
    delete window.__TAURI_INTERNALS__;
    delete window.__TAURI__;
  });

  it("uses relative API paths in browser development mode", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(() => Promise.resolve(jsonResponse({ status: "ok" })));

    await api.get("/openevo-api/desktop/shell");

    expect(invoke).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledWith(
      "/openevo-api/desktop/shell",
      expect.any(Object),
    );
  });

  it("starts the native sidecar once and routes Tauri API calls to localhost", async () => {
    window.__TAURI_INTERNALS__ = {};
    vi.mocked(invoke).mockResolvedValue(bootstrapContext(49152, SESSION_TOKEN));
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(() => Promise.resolve(jsonResponse({ status: "ok" })));

    await api.get("/openevo-api/desktop/shell");
    await api.post("/openevo-api/desktop/bootstrap", {});

    expect(invoke).toHaveBeenCalledTimes(1);
    expect(invoke).toHaveBeenCalledWith("start_sidecar");
    expect(fetchMock.mock.calls.map(([input]) => String(input))).toEqual([
      "http://127.0.0.1:49152/openevo-api/desktop/shell",
      "http://127.0.0.1:49152/openevo-api/desktop/bootstrap",
    ]);
    for (const [, init] of fetchMock.mock.calls) {
      expect(new Headers(init?.headers).get("X-OpenEvo-Desktop-Session")).toBe(
        SESSION_TOKEN,
      );
    }
  });

  it("retries sidecar startup after a failed native invoke", async () => {
    window.__TAURI__ = {};
    vi.mocked(invoke)
      .mockRejectedValueOnce(new Error("sidecar missing"))
      .mockResolvedValueOnce(bootstrapContext(3766, "t".repeat(32)));
    vi.spyOn(globalThis, "fetch").mockImplementation(() =>
      Promise.resolve(jsonResponse({ status: "ok" })),
    );

    await expect(api.get("/openevo-api/desktop/shell")).rejects.toThrow(
      "sidecar missing",
    );
    await api.get("/openevo-api/desktop/shell");

    expect(invoke).toHaveBeenCalledTimes(2);
  });
});

function bootstrapContext(port: number, sessionToken: string) {
  return {
    schema_version: "1",
    endpoint: `http://127.0.0.1:${port}`,
    session_token: sessionToken,
    negotiated_contract: {
      major: 1,
      openapi_sha256: "a".repeat(64),
      provider_kind: "desktop_sidecar",
      feature_flags: ["remote_profiles"],
    },
  } as const;
}

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    headers: { "Content-Type": "application/json" },
  });
}
