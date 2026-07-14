import { describe, expect, it, vi } from "vitest";
import {
  ContractVersionUnsupportedError,
  DESKTOP_SESSION_HEADER,
  DesktopApiError,
  DesktopContractError,
  createDesktopApiClient,
  negotiateVersion,
  type FetchLike,
} from "./client";
import { CONTRACT_FIXTURE_V1, PROFILE_PAGE_FIXTURE_V1 } from "./fixtures";

const OTHER_OPENAPI_DIGEST = "d".repeat(64);

describe("Desktop Local API v1 client", () => {
  it("injects the bootstrap session header and parses page responses", async () => {
    const fetchMock = vi.fn<FetchLike>().mockResolvedValue(jsonResponse(PROFILE_PAGE_FIXTURE_V1));
    const bootstrap = vi.fn().mockResolvedValue(CONTRACT_FIXTURE_V1.bootstrap);
    const client = fixtureClient(fetchMock, bootstrap);

    const page = await client.listProfiles({ limit: 25, sort: "updated_at", direction: "desc" });

    expect(page.items[0].profile_id).toBe("profile-fixture-1");
    expect(bootstrap).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe(
      "http://127.0.0.1:43117/desktop/v1/profiles?limit=25&sort=updated_at&direction=desc",
    );
    expect(new Headers(init?.headers).get(DESKTOP_SESSION_HEADER)).toBe(
      CONTRACT_FIXTURE_V1.bootstrap.session_token,
    );
  });

  it("requires explicit action metadata and sends Idempotency-Key plus If-Match", async () => {
    const fetchMock = vi.fn<FetchLike>().mockResolvedValue(jsonResponse(CONTRACT_FIXTURE_V1.operation, 202));
    const client = fixtureClient(fetchMock);

    await client.connectProfile("profile-fixture-1", {
      idempotencyKey: "connect-fixture-1",
      ifMatch: CONTRACT_FIXTURE_V1.profile.etag,
    });

    const [, init] = fetchMock.mock.calls[0];
    const headers = new Headers(init?.headers);
    expect(headers.get("Idempotency-Key")).toBe("connect-fixture-1");
    expect(headers.get("If-Match")).toBe(CONTRACT_FIXTURE_V1.profile.etag);
    expect(headers.get(DESKTOP_SESSION_HEADER)).toBe(CONTRACT_FIXTURE_V1.bootstrap.session_token);
    expect(init?.body).toBeUndefined();
  });

  it("keeps discovery unauthenticated while still using the injected endpoint", async () => {
    const fetchMock = vi.fn<FetchLike>().mockResolvedValue(jsonResponse(CONTRACT_FIXTURE_V1.version));
    const client = fixtureClient(fetchMock);

    await client.version();

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("http://127.0.0.1:43117/version");
    expect(new Headers(init?.headers).has(DESKTOP_SESSION_HEADER)).toBe(false);
  });

  it("throws a typed ApiError for a strict HTTP 426 envelope", async () => {
    const fetchMock = vi
      .fn<FetchLike>()
      .mockResolvedValue(jsonResponse(CONTRACT_FIXTURE_V1.unsupportedVersionError, 426));
    const client = fixtureClient(fetchMock);

    const error = await client.state().catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(DesktopApiError);
    expect(error).toMatchObject({
      status: 426,
      apiError: { code: "contract_version_unsupported", category: "contract" },
    });
  });

  it("rejects error envelopes whose status does not match the HTTP response", async () => {
    const fetchMock = vi.fn<FetchLike>().mockResolvedValue(
      jsonResponse({ ...CONTRACT_FIXTURE_V1.error, http_status: 400 }, 409),
    );
    const client = fixtureClient(fetchMock);

    await expect(client.state()).rejects.toMatchObject({
      name: "DesktopContractError",
      status: 409,
    });
  });

  it("rejects malformed error envelopes and extra success fields", async () => {
    const badErrorClient = fixtureClient(
      vi.fn<FetchLike>().mockResolvedValue(
        jsonResponse({ ...CONTRACT_FIXTURE_V1.error, http_status: 400, ssh_password: "leak" }, 400),
      ),
    );
    await expect(badErrorClient.state()).rejects.toBeInstanceOf(DesktopContractError);

    const badSuccessClient = fixtureClient(
      vi.fn<FetchLike>().mockResolvedValue(
        jsonResponse({ ...CONTRACT_FIXTURE_V1.state, command: "ssh gpu.example.test" }),
      ),
    );
    await expect(badSuccessClient.state()).rejects.toBeInstanceOf(DesktopContractError);
  });

  it("builds an SSE request without opening an EventSource", async () => {
    const fetchMock = vi.fn<FetchLike>();
    const client = fixtureClient(fetchMock);

    const request = await client.eventStreamRequest("event-fixture-7");

    expect(request.url).toBe("http://127.0.0.1:43117/desktop/v1/events");
    expect(request.headers).toMatchObject({
      Accept: "text/event-stream",
      "Last-Event-ID": "event-fixture-7",
      [DESKTOP_SESSION_HEADER]: CONTRACT_FIXTURE_V1.bootstrap.session_token,
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("covers maintenance and completed diagnostic routes with strict headers and parsing", async () => {
    const fetchMock = vi
      .fn<FetchLike>()
      .mockResolvedValueOnce(jsonResponse(CONTRACT_FIXTURE_V1.operation, 202))
      .mockResolvedValueOnce(jsonResponse(CONTRACT_FIXTURE_V1.diagnostic))
      .mockResolvedValueOnce(noContentResponse());
    const client = fixtureClient(fetchMock);

    await client.cleanupMaintenanceCache({ idempotencyKey: "cleanup-fixture-1" });
    const diagnostic = await client.getDiagnostic("diagnostic-fixture-1");
    await client.deleteRun("run-fixture-1", { ifMatch: CONTRACT_FIXTURE_V1.run.etag });

    expect(diagnostic.status).toBe("healthy");
    expect(String(fetchMock.mock.calls[0][0]).endsWith("/desktop/v1/maintenance/cache-cleanup")).toBe(true);
    expect(new Headers(fetchMock.mock.calls[0][1]?.headers).get("Idempotency-Key")).toBe(
      "cleanup-fixture-1",
    );
    expect(new Headers(fetchMock.mock.calls[0][1]?.headers).has("If-Match")).toBe(false);
    expect(String(fetchMock.mock.calls[1][0]).endsWith("/desktop/v1/diagnostics/diagnostic-fixture-1")).toBe(true);
    expect(new Headers(fetchMock.mock.calls[2][1]?.headers).get("If-Match")).toBe(
      CONTRACT_FIXTURE_V1.run.etag,
    );
  });

  it("requires a non-empty digest allowlist at client creation at runtime", () => {
    expect(() =>
      createDesktopApiClient({
        fetch: vi.fn<FetchLike>(),
        bootstrap: vi.fn().mockResolvedValue(CONTRACT_FIXTURE_V1.bootstrap),
      } as unknown as Parameters<typeof createDesktopApiClient>[0]),
    ).toThrow(DesktopContractError);
    expect(() =>
      createDesktopApiClient({
        fetch: vi.fn<FetchLike>(),
        bootstrap: vi.fn().mockResolvedValue(CONTRACT_FIXTURE_V1.bootstrap),
        acceptedOpenApiDigests: [] as unknown as readonly [string, ...string[]],
      }),
    ).toThrow(DesktopContractError);
  });

  it("rejects an unknown bootstrap digest before making a request", async () => {
    const fetchMock = vi.fn<FetchLike>();
    const client = createDesktopApiClient({
      fetch: fetchMock,
      bootstrap: vi.fn().mockResolvedValue(CONTRACT_FIXTURE_V1.bootstrap),
      acceptedOpenApiDigests: [OTHER_OPENAPI_DIGEST],
      allowedProviderKinds: ["contract_simulator"],
    });

    await expect(client.state()).rejects.toThrow(/unknown OpenAPI digest/i);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("accepts the exact create and asynchronous run statuses", async () => {
    const fetchMock = vi
      .fn<FetchLike>()
      .mockResolvedValueOnce(jsonResponse(CONTRACT_FIXTURE_V1.profile, 201))
      .mockResolvedValueOnce(jsonResponse(CONTRACT_FIXTURE_V1.run, 202));
    const client = fixtureClient(fetchMock);

    await expect(
      client.createProfile(profileCreateInput(), { idempotencyKey: "profile-create-fixture" }),
    ).resolves.toMatchObject({ profile_id: CONTRACT_FIXTURE_V1.profile.profile_id });
    await expect(
      client.createRun(runCreateInput(), { idempotencyKey: "run-create-fixture" }),
    ).resolves.toMatchObject({ run_id: CONTRACT_FIXTURE_V1.run.run_id });
  });

  it.each([
    ["digest", { openapi_sha256: OTHER_OPENAPI_DIGEST }],
    ["provider", { provider_kind: "desktop_sidecar" as const }],
    ["features", { feature_flags: [...CONTRACT_FIXTURE_V1.version.feature_flags, "diagnostics" as const] }],
  ])("rejects bootstrap/version %s inconsistency", async (_field, versionOverride) => {
    const version = { ...CONTRACT_FIXTURE_V1.version, ...versionOverride };
    const fetchMock = vi.fn<FetchLike>().mockResolvedValue(jsonResponse(version));
    const client = createDesktopApiClient({
      fetch: fetchMock,
      bootstrap: vi.fn().mockResolvedValue(CONTRACT_FIXTURE_V1.bootstrap),
      acceptedOpenApiDigests: [CONTRACT_FIXTURE_V1.version.openapi_sha256, OTHER_OPENAPI_DIGEST],
      allowedProviderKinds: ["desktop_sidecar", "contract_simulator"],
    });

    await expect(client.version()).rejects.toBeInstanceOf(DesktopContractError);
  });

  it.each([
    ["GET", 201, CONTRACT_FIXTURE_V1.state, (client: ReturnType<typeof fixtureClient>) => client.state()],
    [
      "PATCH",
      201,
      CONTRACT_FIXTURE_V1.profile,
      (client: ReturnType<typeof fixtureClient>) =>
        client.updateProfile(
          "profile-fixture-1",
          { name: "Updated profile" },
          { ifMatch: CONTRACT_FIXTURE_V1.profile.etag },
        ),
    ],
    [
      "create",
      200,
      CONTRACT_FIXTURE_V1.profile,
      (client: ReturnType<typeof fixtureClient>) =>
        client.createProfile(profileCreateInput(), { idempotencyKey: "profile-create-fixture" }),
    ],
    [
      "action",
      200,
      CONTRACT_FIXTURE_V1.operation,
      (client: ReturnType<typeof fixtureClient>) =>
        client.connectProfile("profile-fixture-1", {
          idempotencyKey: "profile-connect-fixture",
          ifMatch: CONTRACT_FIXTURE_V1.profile.etag,
        }),
    ],
    [
      "run",
      201,
      CONTRACT_FIXTURE_V1.run,
      (client: ReturnType<typeof fixtureClient>) =>
        client.createRun(runCreateInput(), { idempotencyKey: "run-create-fixture" }),
    ],
    [
      "DELETE",
      200,
      CONTRACT_FIXTURE_V1.profile,
      (client: ReturnType<typeof fixtureClient>) =>
        client.deleteProfile("profile-fixture-1", { ifMatch: CONTRACT_FIXTURE_V1.profile.etag }),
    ],
  ])("rejects an unexpected successful status for %s endpoints", async (_kind, status, payload, invoke) => {
    const fetchMock = vi.fn<FetchLike>().mockResolvedValue(jsonResponse(payload, status));
    const client = fixtureClient(fetchMock);

    await expect(invoke(client)).rejects.toMatchObject({
      name: "DesktopContractError",
      status,
    });
  });
});

describe("Desktop contract version negotiation", () => {
  it("selects the supported v1 major and verifies the digest", () => {
    const result = negotiateVersion(
      CONTRACT_FIXTURE_V1.version,
      [1, 2],
      [CONTRACT_FIXTURE_V1.version.openapi_sha256],
    );
    expect(result.major).toBe(1);
  });

  it("requires a non-empty digest allowlist at runtime", () => {
    expect(() =>
      negotiateVersion(
        CONTRACT_FIXTURE_V1.version,
        [1],
        undefined as unknown as readonly [string, ...string[]],
      ),
    ).toThrow(DesktopContractError);
    expect(() =>
      negotiateVersion(
        CONTRACT_FIXTURE_V1.version,
        [1],
        [] as unknown as readonly [string, ...string[]],
      ),
    ).toThrow(DesktopContractError);
  });

  it("rejects an unknown discovery digest", () => {
    expect(() => negotiateVersion(CONTRACT_FIXTURE_V1.version, [1], [OTHER_OPENAPI_DIGEST])).toThrow(
      /unknown OpenAPI digest/i,
    );
  });

  it("signals HTTP 426 semantics when there is no common major", () => {
    expect(() =>
      negotiateVersion(
        { ...CONTRACT_FIXTURE_V1.version, preferred_major: 2, supported_majors: [2] },
        [1],
        [CONTRACT_FIXTURE_V1.version.openapi_sha256],
      ),
    ).toThrow(ContractVersionUnsupportedError);
    try {
      negotiateVersion(
        { ...CONTRACT_FIXTURE_V1.version, preferred_major: 2, supported_majors: [2] },
        [1],
        [CONTRACT_FIXTURE_V1.version.openapi_sha256],
      );
    } catch (error) {
      expect(error).toMatchObject({ status: 426, clientSupportedMajors: [1], serverSupportedMajors: [2] });
    }
  });
});

function fixtureClient(fetch: FetchLike, bootstrap = vi.fn().mockResolvedValue(CONTRACT_FIXTURE_V1.bootstrap)) {
  return createDesktopApiClient({
    fetch,
    bootstrap,
    acceptedOpenApiDigests: [CONTRACT_FIXTURE_V1.version.openapi_sha256],
    allowedProviderKinds: ["contract_simulator"],
  });
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function noContentResponse(): Response {
  return new Response(null, { status: 204 });
}

function profileCreateInput() {
  const profile = CONTRACT_FIXTURE_V1.profile;
  return {
    name: profile.name,
    host: profile.host,
    port: profile.port,
    user: profile.user,
    authentication_kind: profile.authentication_kind,
    proxy: { ...profile.proxy, no_proxy: [...profile.proxy.no_proxy] },
  };
}

function runCreateInput() {
  const run = CONTRACT_FIXTURE_V1.run;
  return {
    project_id: run.project_id,
    project_snapshot: run.project_snapshot,
    task_snapshot: run.task_snapshot,
    workspace_snapshot: run.workspace_snapshot,
    capability_registry_digest: run.capability_registry_digest,
    required_revision: run.pinned_revision,
  };
}

if (false) {
  const fetch = vi.fn<FetchLike>();
  const bootstrap = vi.fn().mockResolvedValue(CONTRACT_FIXTURE_V1.bootstrap);

  // @ts-expect-error The accepted digest allowlist is required.
  createDesktopApiClient({ fetch, bootstrap });
  // @ts-expect-error The accepted digest allowlist is statically non-empty.
  createDesktopApiClient({ fetch, bootstrap, acceptedOpenApiDigests: [] });
  // @ts-expect-error Negotiation always requires an accepted digest allowlist.
  negotiateVersion(CONTRACT_FIXTURE_V1.version, [1]);
  // @ts-expect-error The negotiation digest allowlist is statically non-empty.
  negotiateVersion(CONTRACT_FIXTURE_V1.version, [1], []);
}
