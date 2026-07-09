// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from "vitest";
import { invoke } from "@tauri-apps/api/core";
import { api, resetOpenEvoSidecarForTests } from "./client";

vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(),
}));

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
    vi.mocked(invoke).mockResolvedValue({
      state: "running",
      port: 49152,
      pid: 42,
      url: "http://127.0.0.1:49152/openevo",
      command: "python3 -m desktop.server.launcher --port 49152",
    });
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
  });

  it("retries sidecar startup after a failed native invoke", async () => {
    window.__TAURI__ = {};
    vi.mocked(invoke)
      .mockRejectedValueOnce(new Error("sidecar missing"))
      .mockResolvedValueOnce({
        state: "running",
        port: 3766,
        pid: 51,
        url: null,
        command: "python3 -m desktop.server.launcher --port 3766",
      });
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

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    headers: { "Content-Type": "application/json" },
  });
}
