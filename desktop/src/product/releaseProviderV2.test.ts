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
  reportReleaseDesktopReady,
} from "./releaseProvider";

const invokeMock = vi.hoisted(() => vi.fn());
vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));

const OPENAPI = "987116bff9919930af0177567b4e2a549b3acc2e4dcf1780a1bccccc6530f672";
const EVENTS = "bc1dbc7b3bf7a68e02ba87adf35bd75f511382bf665afc33cae436110d8aea28";
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
  };
}

describe("v0.1.9 release provider", () => {
  beforeEach(() => invokeMock.mockReset());

  it("pins the exact Desktop v2 contract and event schema", () => {
    expect(DESKTOP_PRODUCT_RELEASE_CONTRACT).toMatchObject({
      releaseVersion: "0.1.9",
      acceptedOpenApiDigests: [OPENAPI],
      acceptedEventSchemaDigests: [EVENTS],
      allowedProviderKinds: ["desktop_sidecar"],
    });
    expect(DESKTOP_PRODUCT_RELEASE_CONTRACT.requiredFeatureFlags).toEqual([
      "core_control_v2",
      "daemon_bundle_v2",
      "event_replay_v2",
      "host_key_review",
      "native_askpass",
      "system_openssh_profiles",
      "task_admission_v2",
    ]);
    expect(DESKTOP_PRODUCT_RELEASE_CONTRACT).toMatchObject({
      releaseVersion: releaseManifest.v019.release_version,
      acceptedOpenApiDigests: releaseManifest.v019.accepted_desktop_openapi_digests,
      acceptedEventSchemaDigests: releaseManifest.v019.accepted_desktop_event_schema_digests,
      requiredFeatureFlags: releaseManifest.v019.required_desktop_feature_flags,
    });
    expect(CORE_PRODUCT_RELEASE_CONTRACT).toMatchObject({
      releaseVersion: releaseManifest.v019.release_version,
      acceptedOpenApiDigests: releaseManifest.v019.accepted_core_openapi_digests,
      acceptedEventSchemaDigests: releaseManifest.v019.accepted_core_event_schema_digests,
      requiredFeatureFlags: releaseManifest.v019.required_core_feature_flags,
    });
  });

  it("binds renderer readiness to OpenAPI, event schema, and release identity", async () => {
    invokeMock.mockResolvedValue(undefined);

    await reportReleaseDesktopReady();

    expect(invokeMock).toHaveBeenCalledWith("renderer_ready", {
      openapiSha256: OPENAPI,
      eventSchemaSha256: EVENTS,
      releaseVersion: "0.1.9",
    });
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

  it("recovers bootstrap context when the long native start reply never settles", async () => {
    const fetch = vi.fn<FetchLikeV2>().mockResolvedValue(new Response(JSON.stringify(version()), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
    const neverSettles = new Promise<never>(() => {});
    invokeMock.mockImplementation((command?: string) => {
      if (command === "start_sidecar") return neverSettles;
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
    expect(invokeMock).toHaveBeenCalledWith("start_sidecar");
    expect(invokeMock).toHaveBeenCalledWith("sidecar_bootstrap_context");
    expect(invokeMock.mock.calls.map(([command]) => command)).toEqual([
      "start_sidecar",
      "sidecar_bootstrap_context",
    ]);
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
