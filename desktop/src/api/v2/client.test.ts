import { describe, expect, it, vi } from "vitest";
import {
  DESKTOP_API_V2_PREFIX,
  DESKTOP_RESOURCE_GENERATION_HEADER,
  DESKTOP_SESSION_HEADER,
  DesktopApiErrorV2,
  DesktopContractErrorV2,
  createDesktopApiClientV2,
  validateDesktopBootstrapContextV2,
  type FetchLikeV2,
} from "./client";

const OPENAPI = "987116bff9919930af0177567b4e2a549b3acc2e4dcf1780a1bccccc6530f672";
const EVENTS = "bc1dbc7b3bf7a68e02ba87adf35bd75f511382bf665afc33cae436110d8aea28";
const FEATURES = [
  "core_control_v2",
  "daemon_bundle_v2",
  "event_replay_v2",
  "host_key_review",
  "native_askpass",
  "system_openssh_profiles",
  "task_admission_v2",
] as const;
const FEATURE_DIGEST = "026eb1f1eecd219a6bf282f6e0063bf2e19d018619a934487eec3f151b66af9b";

function version() {
  return {
    schema_version: "2",
    api_name: "openevo-desktop-local-api",
    preferred_major: 2,
    supported_majors: [2],
    mutation_major: 2,
    openapi_sha256: OPENAPI,
    event_schema_sha256: EVENTS,
    release_version: "0.1.9",
    build_id: "a".repeat(64),
    source_commit: "abcdef1",
    build_channel: "release",
    provider_kind: "desktop_sidecar",
    feature_flags: [...FEATURES],
    feature_set_sha256: FEATURE_DIGEST,
    required_core_api_major: 2,
    mutation_compatible: true,
  };
}

function bootstrap() {
  const { api_name: _apiName, preferred_major: major, supported_majors: _supported, ...negotiated } = version();
  return {
    schema_version: "2",
    endpoint: "http://127.0.0.1:43117",
    session_token: "session-token-that-never-enters-discovery",
    negotiated_contract: { major, ...negotiated },
  };
}

const contract = {
  releaseVersion: "0.1.9",
  acceptedOpenApiDigests: [OPENAPI] as const,
  acceptedEventSchemaDigests: [EVENTS] as const,
  allowedProviderKinds: ["desktop_sidecar"] as const,
  requiredFeatureFlags: FEATURES,
};

function fixtureClient(fetch: FetchLikeV2) {
  return createDesktopApiClientV2({ fetch, bootstrap: async () => bootstrap(), contract });
}

describe("Desktop Local API v2 client", () => {
  it("requires exact native bootstrap authority and rejects v1 or identity drift", () => {
    expect(validateDesktopBootstrapContextV2(bootstrap(), contract).negotiated_contract.major).toBe(2);
    expect(() => validateDesktopBootstrapContextV2({ ...bootstrap(), schema_version: "1" }, contract)).toThrow();
    expect(() => validateDesktopBootstrapContextV2({
      ...bootstrap(),
      negotiated_contract: { ...bootstrap().negotiated_contract, event_schema_sha256: "f".repeat(64) },
    }, contract)).toThrow(/event schema/i);
  });

  it("uses only /desktop/v2 and injects session plus generation mutation authority", async () => {
    const catalog = {
      schema_version: "2",
      catalog_generation: 3,
      hosts: [],
      warnings: [],
      scanned_at: "2026-07-23T06:00:00Z",
    };
    const fetch = vi.fn<FetchLikeV2>()
      .mockResolvedValueOnce(jsonResponse(version()))
      .mockResolvedValueOnce(jsonResponse(catalog, 202));
    const client = fixtureClient(fetch);

    await client.version();
    await client.rescanSshHosts({ schema_version: "2" }, {
      resourceGeneration: 3,
      idempotencyKey: "rescan-host-catalog-0001",
    });

    expect(DESKTOP_API_V2_PREFIX).toBe("/desktop/v2");
    expect(String(fetch.mock.calls[0][0])).toBe("http://127.0.0.1:43117/version");
    expect(new Headers(fetch.mock.calls[0][1]?.headers).has(DESKTOP_SESSION_HEADER)).toBe(false);
    expect(String(fetch.mock.calls[1][0])).toBe("http://127.0.0.1:43117/desktop/v2/ssh-hosts/rescan");
    const headers = new Headers(fetch.mock.calls[1][1]?.headers);
    expect(headers.get(DESKTOP_SESSION_HEADER)).toBe(bootstrap().session_token);
    expect(headers.get(DESKTOP_RESOURCE_GENERATION_HEADER)).toBe("3");
    expect(headers.get("Idempotency-Key")).toBe("rescan-host-catalog-0001");
    expect(JSON.parse(String(fetch.mock.calls[1][1]?.body))).toEqual({ schema_version: "2" });
  });

  it("sends profile actions with the exact alias-native body and CAS headers", async () => {
    const operation = {
      schema_version: "2",
      operation_id: "operation-connect-1",
      kind: "profile_connect",
      status: "queued",
      failure: null,
      created_at: "2026-07-23T06:00:00Z",
      updated_at: "2026-07-23T06:00:00Z",
    };
    const fetch = vi.fn<FetchLikeV2>().mockResolvedValue(jsonResponse(operation, 202));
    const client = fixtureClient(fetch);

    await client.connectProfile("profile-lab", {
      schema_version: "2",
      expected_connection_generation: 4,
    }, {
      resourceGeneration: 4,
      ifMatch: `"${"b".repeat(64)}"`,
      idempotencyKey: "connect-profile-lab-0001",
    });

    const [url, init] = fetch.mock.calls[0];
    expect(String(url)).not.toContain("/desktop/v1");
    expect(String(url)).toContain("/desktop/v2/profiles/profile-lab/connect");
    expect(JSON.stringify(JSON.parse(String(init?.body)))).not.toMatch(/password|username|identity|host_path/i);
    expect(new Headers(init?.headers).get("If-Match")).toBe(`"${"b".repeat(64)}"`);
  });

  it("does not apply the short Local API deadline to first-connect bootstrap", async () => {
    vi.useFakeTimers();
    try {
      const operation = {
        schema_version: "2",
        operation_id: "operation-connect-long-bootstrap",
        kind: "profile_connect",
        status: "succeeded",
        failure: null,
        created_at: "2026-07-23T06:00:00Z",
        updated_at: "2026-07-23T06:00:40Z",
      };
      let resolveFetch: ((response: Response) => void) | undefined;
      let observedSignal: AbortSignal | undefined;
      const fetch = vi.fn<FetchLikeV2>((_input, init) => {
        observedSignal = init?.signal ?? undefined;
        return new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        });
      });
      const client = createDesktopApiClientV2({
        fetch,
        bootstrap: async () => bootstrap(),
        contract,
        requestTimeoutMs: 25,
      });

      const pending = client.connectProfile("profile-lab", {
        schema_version: "2",
        expected_connection_generation: 4,
      }, {
        resourceGeneration: 4,
        ifMatch: `"${"b".repeat(64)}"`,
        idempotencyKey: "connect-profile-long-bootstrap-0001",
      });
      await vi.advanceTimersByTimeAsync(30);

      expect(observedSignal?.aborted).toBe(false);
      resolveFetch?.(jsonResponse(operation, 202));
      await expect(pending).resolves.toMatchObject({ status: "succeeded" });
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps first-connect bootstrap alive for the managed runtime install window", async () => {
    vi.useFakeTimers();
    try {
      let observedSignal: AbortSignal | undefined;
      const fetch = vi.fn<FetchLikeV2>((_input, init) => {
        observedSignal = init?.signal ?? undefined;
        return new Promise<Response>(() => undefined);
      });
      const client = createDesktopApiClientV2({
        fetch,
        bootstrap: async () => bootstrap(),
        contract,
        requestTimeoutMs: 25,
      });

      const pending = client.connectProfile("profile-lab", {
        schema_version: "2",
        expected_connection_generation: 4,
      }, {
        resourceGeneration: 4,
        ifMatch: `"${"b".repeat(64)}"`,
        idempotencyKey: "connect-profile-runtime-window-0001",
      });
      void pending.catch(() => undefined);
      await vi.advanceTimersByTimeAsync(900_000);

      expect(observedSignal?.aborted).toBe(false);
      await vi.advanceTimersByTimeAsync(30_000);
      expect(observedSignal?.aborted).toBe(true);
      await expect(pending).rejects.toThrow("Desktop Local API request timed out");
    } finally {
      vi.useRealTimers();
    }
  });

  it("retains the short bounded deadline for ordinary Local API reads", async () => {
    vi.useFakeTimers();
    try {
      let observedSignal: AbortSignal | undefined;
      const fetch = vi.fn<FetchLikeV2>((_input, init) => {
        observedSignal = init?.signal ?? undefined;
        return new Promise<Response>(() => undefined);
      });
      const client = createDesktopApiClientV2({
        fetch,
        bootstrap: async () => bootstrap(),
        contract,
        requestTimeoutMs: 25,
      });

      const pending = client.state();
      const rejected = expect(pending).rejects.toThrow(/timed out/i);
      await vi.advanceTimersByTimeAsync(30);

      await rejected;
      expect(observedSignal?.aborted).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("rejects strict error envelopes carrying secret/path canaries", async () => {
    const error = {
      schema_version: "2",
      code: "ssh_authentication_failed",
      summary: "Authentication was not accepted.",
      retryable: true,
      action: "retry",
      affected_resource_id: "profile-lab",
    };
    await expect(fixtureClient(async () => jsonResponse(error, 503)).state()).rejects.toBeInstanceOf(DesktopApiErrorV2);
    await expect(fixtureClient(async () => jsonResponse({ ...error, password: "canary" }, 503)).state())
      .rejects.toBeInstanceOf(DesktopContractErrorV2);
  });
});

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
