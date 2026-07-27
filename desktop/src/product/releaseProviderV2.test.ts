import { beforeEach, describe, expect, it, vi } from "vitest";
import type { FetchLikeV2 } from "../api/v2/client";
import releaseManifest from "../../release-contract.json";
import { unavailableDesktopProductProviderV2 } from "./providerV2";
import {
  CORE_PRODUCT_RELEASE_CONTRACT,
  DESKTOP_PRODUCT_RELEASE_CONTRACT,
} from "./releaseContract";
import {
  createReleaseDesktopProductProvider,
  getReleaseDesktopStartupStatus,
  reportReleaseDesktopReady,
} from "./releaseProvider";

const invokeMock = vi.hoisted(() => vi.fn());
vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));

const OPENAPI = "4cd120dab0797e223ba892b0382fd61f8e4156318df9ab6676236c201191a98a";
const EVENTS = "515b6d90e9ebdf3f5b4f7c4a57a1924dc85011536d9396b1ab3a5dc73fc48b6b";
const FEATURE_DIGEST = "67b6ad24f67de611f32c365079fcf8384c800d0855effaa64e1ff24251a7acda";

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
    feature_flags: [...DESKTOP_PRODUCT_RELEASE_CONTRACT.requiredFeatureFlags],
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
    session_token: "native-session-token-with-at-least-32-bytes",
    negotiated_contract: { major, ...negotiated },
  };
}

function native(value: unknown = bootstrap()) {
  return {
    bootstrap: vi.fn().mockResolvedValue(value),
    stop: vi.fn().mockResolvedValue(undefined),
    selectProjectSource: vi.fn(),
    cancelProjectSource: vi.fn(),
    settleProjectSource: vi.fn(),
    readMutationIntentJournalV2: vi.fn().mockResolvedValue(null),
    compareAndSwapMutationIntentJournalV2: vi.fn(),
  };
}

describe("v0.1.10 release provider", () => {
  beforeEach(() => invokeMock.mockReset());

  it("pins the exact Desktop v2 contract and event schema", () => {
    expect(DESKTOP_PRODUCT_RELEASE_CONTRACT).toMatchObject({
      releaseVersion: "0.1.10",
      acceptedOpenApiDigests: [OPENAPI],
      acceptedEventSchemaDigests: [EVENTS],
      allowedProviderKinds: ["desktop_sidecar"],
    });
    expect(DESKTOP_PRODUCT_RELEASE_CONTRACT.requiredFeatureFlags).toEqual([
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
    ]);
    expect(DESKTOP_PRODUCT_RELEASE_CONTRACT).toMatchObject({
      releaseVersion: releaseManifest.v0110.release_version,
      acceptedOpenApiDigests: releaseManifest.v0110.accepted_desktop_openapi_digests,
      acceptedEventSchemaDigests: releaseManifest.v0110.accepted_desktop_event_schema_digests,
      requiredFeatureFlags: releaseManifest.v0110.required_desktop_feature_flags,
    });
    expect(CORE_PRODUCT_RELEASE_CONTRACT).toMatchObject({
      releaseVersion: releaseManifest.v0110.release_version,
      acceptedOpenApiDigests: releaseManifest.v0110.accepted_core_openapi_digests,
      acceptedEventSchemaDigests: releaseManifest.v0110.accepted_core_event_schema_digests,
      requiredFeatureFlags: releaseManifest.v0110.required_core_feature_flags,
    });
  });

  it("binds renderer readiness to OpenAPI, event schema, and release identity", async () => {
    invokeMock.mockResolvedValue(undefined);

    await reportReleaseDesktopReady();

    expect(invokeMock).toHaveBeenCalledWith("renderer_ready", {
      openapiSha256: OPENAPI,
      eventSchemaSha256: EVENTS,
      releaseVersion: "0.1.10",
    });
  });

  it("reads only the closed native startup progress projection", async () => {
    invokeMock.mockResolvedValue({
      schema_version: "2",
      startup_epoch: 4,
      status: "running",
      phase: "waiting_for_local_api",
      phase_index: 3,
      phase_total: 6,
      elapsed_milliseconds: 16_000,
      cancellable: true,
      failure: null,
    });

    await expect(getReleaseDesktopStartupStatus()).resolves.toMatchObject({
      status: "running",
      phase: "waiting_for_local_api",
      elapsed_milliseconds: 16_000,
    });
    expect(invokeMock).toHaveBeenCalledWith("sidecar_startup_status");

    invokeMock.mockResolvedValueOnce({
      schema_version: "2",
      startup_epoch: 4,
      status: "running",
      phase: "waiting_for_local_api",
      phase_index: 3,
      phase_total: 6,
      elapsed_milliseconds: 16_000,
      cancellable: true,
      failure: null,
      stderr: "private output",
    });
    await expect(getReleaseDesktopStartupStatus()).rejects.toThrow();
  });

  it("negotiates only v2 before exposing a v2 provider", async () => {
    const fetch = vi.fn<FetchLikeV2>().mockResolvedValue(new Response(JSON.stringify(version()), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
    const adapterFactory = vi.fn(() => unavailableDesktopProductProviderV2);
    const reportStage = vi.fn();

    const provider = await createReleaseDesktopProductProvider({
      fetch,
      native: native(),
      adapterFactory,
      reportStage,
    });

    expect(provider.apiVersion).toBe(2);
    expect(adapterFactory).toHaveBeenCalledWith(expect.objectContaining({
      featureFlags: DESKTOP_PRODUCT_RELEASE_CONTRACT.requiredFeatureFlags,
    }));
    expect(reportStage.mock.calls.map(([stage]) => stage)).toEqual([
      "bootstrap_context_validated",
      "local_api_version_verified",
      "provider_adapter_ready",
    ]);
  });

  it("uses a quick native start request before observing the published bootstrap context", async () => {
    const fetch = vi.fn<FetchLikeV2>().mockResolvedValue(new Response(JSON.stringify(version()), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
    const neverSettles = new Promise<never>(() => {});
    invokeMock.mockImplementation((command?: string) => {
      if (command === "begin_sidecar_start") return neverSettles;
      if (command === "sidecar_bootstrap_context") return Promise.resolve(bootstrap());
      return Promise.resolve(undefined);
    });

    const result = await Promise.race([
      createReleaseDesktopProductProvider({ fetch, reportStage: vi.fn() }),
      new Promise<"timed_out">((resolve) => setTimeout(() => resolve("timed_out"), 1_000)),
    ]);

    expect(result).not.toBe("timed_out");
    expect(result).toMatchObject({
      apiVersion: 2,
      providerKind: "desktop_sidecar",
    });
    expect(invokeMock).not.toHaveBeenCalledWith("start_sidecar");
    expect(invokeMock).toHaveBeenCalledWith("begin_sidecar_start");
    expect(invokeMock).toHaveBeenCalledWith("sidecar_bootstrap_context");
    expect(invokeMock.mock.calls.map(([command]) => command)).toEqual([
      "begin_sidecar_start",
      "sidecar_bootstrap_context",
    ]);
  });

  it("binds mutation retry journal reads and CAS writes to the exact native commands", async () => {
    const journal = "{\"schema_version\":\"2\",\"revision\":1,\"entries\":[]}";
    invokeMock.mockImplementation((command?: string) => {
      if (command === "begin_sidecar_start") return Promise.resolve();
      if (command === "sidecar_bootstrap_context") return Promise.resolve(bootstrap());
      if (command === "read_mutation_intent_journal_v2") return Promise.resolve(journal);
      if (command === "compare_and_swap_mutation_intent_journal_v2") return Promise.resolve();
      return Promise.resolve(undefined);
    });
    const fetch = vi.fn<FetchLikeV2>().mockResolvedValue(new Response(JSON.stringify(version()), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
    const adapterFactory = vi.fn(async (context) => {
      expect(await context.native.readMutationIntentJournalV2()).toBe(journal);
      await context.native.compareAndSwapMutationIntentJournalV2(journal, null);
      return unavailableDesktopProductProviderV2;
    });

    await createReleaseDesktopProductProvider({ fetch, adapterFactory, reportStage: vi.fn() });

    expect(invokeMock).toHaveBeenCalledWith("read_mutation_intent_journal_v2");
    expect(invokeMock).toHaveBeenCalledWith("compare_and_swap_mutation_intent_journal_v2", {
      expectedValue: journal,
      newValue: null,
    });
  });

  it("fails closed when the native background start request is explicitly rejected", async () => {
    const failure = {
      code: "sidecar_start_task_failed",
      message: "OpenEvo Desktop could not schedule its local service startup task.",
    };
    const fetch = vi.fn<FetchLikeV2>();
    invokeMock.mockImplementation((command?: string) => {
      if (command === "begin_sidecar_start") return Promise.reject(failure);
      return Promise.resolve(undefined);
    });

    await expect(createReleaseDesktopProductProvider({
      fetch,
      reportStage: vi.fn(),
    })).rejects.toEqual(failure);

    expect(fetch).not.toHaveBeenCalled();
    expect(invokeMock).not.toHaveBeenCalledWith("sidecar_bootstrap_context");
  });

  it("does not accept a bootstrap context before a queued native start rejection settles", async () => {
    const failure = {
      code: "sidecar_start_task_failed",
      message: "OpenEvo Desktop could not schedule its local service startup task.",
    };
    const fetch = vi.fn<FetchLikeV2>();
    invokeMock.mockImplementation((command?: string) => {
      if (command === "begin_sidecar_start") {
        return Promise.resolve().then(() => {
          throw failure;
        });
      }
      if (command === "sidecar_bootstrap_context") return Promise.resolve(bootstrap());
      return Promise.resolve(undefined);
    });

    await expect(createReleaseDesktopProductProvider({
      fetch,
      reportStage: vi.fn(),
    })).rejects.toEqual(failure);

    expect(fetch).not.toHaveBeenCalled();
    expect(invokeMock).toHaveBeenCalledWith("sidecar_bootstrap_context");
  });

  it("fails closed on a v1 bootstrap without calling the Local API", async () => {
    const fetch = vi.fn<FetchLikeV2>();

    await expect(createReleaseDesktopProductProvider({
      fetch,
      native: native({
        schema_version: "1",
        endpoint: "http://127.0.0.1:43117",
        session_token: "native-session-token-with-at-least-32-bytes",
        negotiated_contract: { major: 1 },
      }),
      adapterFactory: () => unavailableDesktopProductProviderV2,
    })).rejects.toThrow();

    expect(fetch).not.toHaveBeenCalled();
  });

  it("constructs the strict v2 Local API provider by default", async () => {
    const provider = await createReleaseDesktopProductProvider({
      fetch: vi.fn<FetchLikeV2>().mockResolvedValue(new Response(JSON.stringify(version()), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })),
      native: native(),
    });

    expect(provider.apiVersion).toBe(2);
    expect(provider.providerKind).toBe("desktop_sidecar");
  });
});
