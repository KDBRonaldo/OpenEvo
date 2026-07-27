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

const OPENAPI = "f0996184595992a22ec6abd257d9040342c9d2f7a31a9882b4a0597061594760";
const EVENTS = "515b6d90e9ebdf3f5b4f7c4a57a1924dc85011536d9396b1ab3a5dc73fc48b6b";
const FEATURES = [
  "core_control_v2",
  "daemon_bundle_v2",
  "event_replay_v2",
  "host_key_review",
  "lifecycle_operations_v2",
  "lifecycle_process_logs_v2",
  "mutation_idempotency_v2",
  "native_askpass",
  "system_openssh_profiles",
  "task_admission_v2",
] as const;
const FEATURE_DIGEST = "67b6ad24f67de611f32c365079fcf8384c800d0855effaa64e1ff24251a7acda";
const NOW = "2026-07-27T08:00:00Z";
const ETAG = `"${"b".repeat(64)}"`;

function version() {
  return {
    schema_version: "2",
    api_name: "openevo-desktop-local-api",
    preferred_major: 2,
    supported_majors: [2],
    mutation_major: 2,
    openapi_sha256: OPENAPI,
    event_schema_sha256: EVENTS,
    release_version: "0.1.10",
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
  releaseVersion: "0.1.10",
  acceptedOpenApiDigests: [OPENAPI] as const,
  acceptedEventSchemaDigests: [EVENTS] as const,
  allowedProviderKinds: ["desktop_sidecar"] as const,
  requiredFeatureFlags: FEATURES,
};

function fixtureClient(fetch: FetchLikeV2) {
  return createDesktopApiClientV2({ fetch, bootstrap: async () => bootstrap(), contract });
}

function lifecycleOperation(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: "2",
    operation_id: "lifecycle-profile-connect-1",
    kind: "profile_connect",
    resource: { resource_kind: "profile", resource_id: "profile-lab" },
    request_sha256: "a".repeat(64),
    status: "queued",
    phase: "queued",
    phase_index: 1,
    phase_total: 17,
    progress: null,
    cancellable: true,
    result: null,
    failure: null,
    log_sequence_high_watermark: 0,
    created_at: NOW,
    started_at: null,
    updated_at: NOW,
    finished_at: null,
    etag: ETAG,
    ...overrides,
  };
}

function coreOperation() {
  return {
    schema_version: "2",
    operation_id: "core-operation-1",
    kind: "cache_cleanup",
    status: "queued",
    progress_completed: 0,
    progress_total: 0,
    error: null,
    created_at: NOW,
    updated_at: NOW,
    etag: ETAG,
  };
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
    const operation = lifecycleOperation();
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

  it("uses the normal bounded deadline for lifecycle reservation instead of waiting for remote work", async () => {
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
        idempotencyKey: "connect-profile-long-bootstrap-0001",
      });
      void pending.catch(() => undefined);
      await vi.advanceTimersByTimeAsync(0);
      await vi.advanceTimersByTimeAsync(30);

      expect(observedSignal?.aborted).toBe(true);
      await expect(pending).rejects.toThrow("Desktop Local API request timed out");
    } finally {
      vi.useRealTimers();
    }
  });

  it("returns project creation as a lifecycle operation without accepting an inline project", async () => {
    const operation = lifecycleOperation({
      operation_id: "lifecycle-project-create-1",
      kind: "project_create",
      resource: { resource_kind: "project", resource_id: "project-bridge-1" },
    });
    const fetch = vi.fn<FetchLikeV2>().mockResolvedValue(jsonResponse(operation, 202));
    const client = fixtureClient(fetch);
    const config = {
      schema_version: "2" as const,
      task: { title: "Study", objective: "Analyze the evidence." },
      workspace: { kind: "scratch" as const, display_name: "Study" },
      execution: {
        mode: "codex_subscription_transcript" as const,
        capture_mode: "transcript" as const,
        token_level_metrics_available: false as const,
        harness_id: "codex" as const,
        codex_model: "gpt-5.6-codex",
        reasoning_effort: "high" as const,
        token_limit: 8192,
        task_network_allow_internet: true,
      },
      evolution: { targets: {} },
    };

    await expect(client.createProject({
      schema_version: "2",
      profile_id: "profile-lab",
      profile_connection_generation: 4,
      display_name: "Study",
      config,
    }, {
      resourceGeneration: 4,
      idempotencyKey: "create-project-study-0001",
    })).resolves.toMatchObject({ kind: "project_create", status: "queued" });
    expect(fetch.mock.calls[0]?.[1]?.method).toBe("POST");
  });

  it("observes, cancels, acknowledges, and pages Desktop lifecycle authority", async () => {
    const operation = lifecycleOperation();
    const logPage = {
      schema_version: "2",
      operation_id: operation.operation_id,
      dropped_before_sequence: 0,
      items: [],
      next_cursor: null,
      has_more: false,
    };
    const fetch = vi.fn<FetchLikeV2>()
      .mockResolvedValueOnce(jsonResponse(operation))
      .mockResolvedValueOnce(jsonResponse(operation))
      .mockResolvedValueOnce(jsonResponse(logPage))
      .mockResolvedValueOnce(jsonResponse(operation, 202))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    const client = fixtureClient(fetch);

    await client.getLifecycleOperationByAction("connect-profile-action-0001", "profile_connect");
    await client.getLifecycleOperation(operation.operation_id);
    await client.lifecycleOperationLogs(operation.operation_id, { limit: 25, after: "cursor/value+1" });
    await client.cancelLifecycleOperation(operation.operation_id, {
      schema_version: "2",
      expected_operation_id: operation.operation_id,
    }, { resourceGeneration: 7, ifMatch: ETAG, idempotencyKey: "cancel-lifecycle-operation-0001" });
    await client.acknowledgeLifecycleOperation(operation.operation_id, {
      schema_version: "2",
      expected_operation_id: operation.operation_id,
      expected_terminal_status: "cancelled",
    }, { resourceGeneration: 7, ifMatch: ETAG, idempotencyKey: "ack-lifecycle-operation-0001" });

    expect(String(fetch.mock.calls[0]?.[0])).toContain("operations/by-action?action_id=connect-profile-action-0001&kind=profile_connect");
    expect(String(fetch.mock.calls[2]?.[0])).toContain("after=cursor%2Fvalue%2B1");
    expect(new Headers(fetch.mock.calls[3]?.[1]?.headers).get("If-Match")).toBe(ETAG);
    expect(fetch.mock.calls[4]?.[1]?.method).toBe("POST");
  });

  it("uses tunnel-only Core operation, service log, and cache cleanup routes", async () => {
    const operation = coreOperation();
    const logs = { schema_version: "2", items: [], next_cursor: null, has_more: false };
    const fetch = vi.fn<FetchLikeV2>()
      .mockResolvedValueOnce(jsonResponse(operation))
      .mockResolvedValueOnce(jsonResponse(operation, 202))
      .mockResolvedValueOnce(jsonResponse(logs))
      .mockResolvedValueOnce(jsonResponse(operation, 202));
    const client = fixtureClient(fetch);

    await client.getCoreOperation(operation.operation_id);
    await client.cancelCoreOperation(operation.operation_id, {
      resourceGeneration: 8,
      ifMatch: ETAG,
      idempotencyKey: "cancel-core-operation-0001",
    });
    await client.serviceLogs("daemon-service-1", { after: "service/cursor" });
    await client.cleanupCaches({ schema_version: "2", scope: "safe_unreferenced" }, {
      resourceGeneration: 8,
      idempotencyKey: "cleanup-safe-caches-0001",
    });

    expect(fetch.mock.calls.map(([url]) => String(url))).toEqual([
      "http://127.0.0.1:43117/desktop/v2/core-operations/core-operation-1",
      "http://127.0.0.1:43117/desktop/v2/core-operations/core-operation-1/cancel",
      "http://127.0.0.1:43117/desktop/v2/services/daemon-service-1/logs?after=service%2Fcursor",
      "http://127.0.0.1:43117/desktop/v2/maintenance/cache-cleanup",
    ]);
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
