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
    vi.useRealTimers();
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

  it("bootstraps a fresh endpoint after a network-level sidecar failure", async () => {
    window.__TAURI_INTERNALS__ = {};
    const replacementToken = "t".repeat(32);
    vi.mocked(invoke)
      .mockResolvedValueOnce(bootstrapContext(49152, SESSION_TOKEN))
      .mockResolvedValueOnce(bootstrapContext(49153, replacementToken));
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(jsonResponse({ status: "ok" }));

    await expect(api.get("/openevo-api/desktop/projects")).rejects.toThrow(
      "Failed to fetch",
    );
    await api.get("/openevo-api/desktop/projects");

    expect(invoke).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls.map(([input]) => String(input))).toEqual([
      "http://127.0.0.1:49152/openevo-api/desktop/projects",
      "http://127.0.0.1:49153/openevo-api/desktop/projects",
    ]);
    expect(
      new Headers(fetchMock.mock.calls[1][1]?.headers).get(
        "X-OpenEvo-Desktop-Session",
      ),
    ).toBe(replacementToken);
  });

  it("times out an ordinary request and reboots through native startup", async () => {
    vi.useFakeTimers();
    window.__TAURI_INTERNALS__ = {};
    const replacementToken = "t".repeat(32);
    vi.mocked(invoke)
      .mockResolvedValueOnce(bootstrapContext(49152, SESSION_TOKEN))
      .mockResolvedValueOnce(bootstrapContext(49153, replacementToken));
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementationOnce((_input, init) => {
        const signal = init?.signal;
        return new Promise((_resolve, reject) => {
          signal?.addEventListener("abort", () => reject(signal.reason), {
            once: true,
          });
        });
      })
      .mockResolvedValueOnce(jsonResponse({ status: "ok" }));

    const firstRequest = api.get("/openevo-api/desktop/projects");
    const timedOut = expect(firstRequest).rejects.toMatchObject({
      name: "TimeoutError",
    });
    await vi.advanceTimersByTimeAsync(15_000);
    await timedOut;
    await api.get("/openevo-api/desktop/projects");

    expect(invoke).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls.map(([input]) => String(input))).toEqual([
      "http://127.0.0.1:49152/openevo-api/desktop/projects",
      "http://127.0.0.1:49153/openevo-api/desktop/projects",
    ]);
    expect(fetchMock.mock.calls[0][1]?.signal).toBeInstanceOf(AbortSignal);
    expect(
      new Headers(fetchMock.mock.calls[1][1]?.headers).get(
        "X-OpenEvo-Desktop-Session",
      ),
    ).toBe(replacementToken);
  });

  it("does not rebootstrap for auth, contract, or other HTTP errors", async () => {
    window.__TAURI__ = {};
    vi.mocked(invoke).mockResolvedValue(bootstrapContext(49152, SESSION_TOKEN));
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse({ detail: "unauthorized" }, 401))
      .mockResolvedValueOnce(jsonResponse({ code: "contract_mismatch" }, 409))
      .mockResolvedValueOnce(jsonResponse({ detail: "unavailable" }, 503))
      .mockResolvedValueOnce(jsonResponse({ status: "ok" }));

    await expect(api.get("/openevo-api/desktop/projects")).rejects.toThrow(
      "HTTP 401",
    );
    await expect(api.get("/openevo-api/desktop/projects")).rejects.toThrow(
      "HTTP 409",
    );
    await expect(api.get("/openevo-api/desktop/projects")).rejects.toThrow(
      "HTTP 503",
    );
    await api.get("/openevo-api/desktop/projects");

    expect(invoke).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("does not rebootstrap after a successful response fails contract parsing", async () => {
    window.__TAURI_INTERNALS__ = {};
    vi.mocked(invoke).mockResolvedValue(bootstrapContext(49152, SESSION_TOKEN));
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response("{", {
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ status: "ok" }));

    await expect(api.get("/openevo-api/desktop/projects")).rejects.toThrow();
    await api.get("/openevo-api/desktop/projects");

    expect(invoke).toHaveBeenCalledTimes(1);
  });

  it("does not rebootstrap after a non-network fetch cancellation", async () => {
    window.__TAURI_INTERNALS__ = {};
    vi.mocked(invoke).mockResolvedValue(bootstrapContext(49152, SESSION_TOKEN));
    vi.spyOn(globalThis, "fetch")
      .mockRejectedValueOnce(new DOMException("Aborted", "AbortError"))
      .mockResolvedValueOnce(jsonResponse({ status: "ok" }));

    await expect(api.get("/openevo-api/desktop/projects")).rejects.toMatchObject(
      { name: "AbortError" },
    );
    await api.get("/openevo-api/desktop/projects");

    expect(invoke).toHaveBeenCalledTimes(1);
  });
});

function bootstrapContext(port: number, sessionToken: string) {
  return {
    schema_version: "1",
    endpoint: `http://127.0.0.1:${port}`,
    session_token: sessionToken,
    negotiated_contract: {
      major: 1,
      openapi_sha256:
        "07d08e2f9b354517f8caf3ca171c7bef722fefdac6b6889021e70acd86e7a861",
      provider_kind: "desktop_sidecar",
      feature_flags: ["remote_profiles"],
    },
  } as const;
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
