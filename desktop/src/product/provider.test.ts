import { beforeEach, describe, expect, it, vi } from "vitest";
import type { FetchLike } from "../api/v1/client";
import { CONTRACT_FIXTURE_V1 } from "../api/v1/fixtures";
import {
  ProductRefreshOrder,
  defineDesktopProductReleaseContract,
  unavailableDesktopProductProvider,
  type DesktopProductReleaseContract,
} from "./provider";
import { DESKTOP_PRODUCT_RELEASE_CONTRACT } from "./releaseContract";
import {
  createReleaseDesktopProductProvider,
  reportReleaseDesktopReady,
} from "./releaseProvider";

const invokeMock = vi.hoisted(() => vi.fn());

vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));

describe("Desktop product provider boundary", () => {
  beforeEach(() => {
    invokeMock.mockReset();
  });

  it("requires checked-in release digests and the native provider", () => {
    expect(() => defineDesktopProductReleaseContract({
      acceptedOpenApiDigests: [],
      allowedProviderKinds: ["desktop_sidecar"],
      requiredFeatureFlags: [],
    } as unknown as DesktopProductReleaseContract)).toThrow("requires a checked-in OpenAPI digest");

    const contract = defineDesktopProductReleaseContract({
      acceptedOpenApiDigests: ["a".repeat(64)],
      allowedProviderKinds: ["desktop_sidecar"],
      requiredFeatureFlags: ["remote_profiles", "run_observability"],
    });
    expect(contract.allowedProviderKinds).toEqual(["desktop_sidecar"]);
    expect(Object.isFrozen(contract.acceptedOpenApiDigests)).toBe(true);
    expect(DESKTOP_PRODUCT_RELEASE_CONTRACT.acceptedOpenApiDigests).toEqual([
      "07d08e2f9b354517f8caf3ca171c7bef722fefdac6b6889021e70acd86e7a861",
    ]);
  });

  it("binds renderer readiness to the frozen Local API digest", async () => {
    invokeMock.mockResolvedValue(undefined);

    await reportReleaseDesktopReady();

    expect(invokeMock).toHaveBeenCalledWith("renderer_ready", {
      openapiSha256: DESKTOP_PRODUCT_RELEASE_CONTRACT.acceptedOpenApiDigests[0],
    });
  });

  it("negotiates the native bootstrap, checked-in digest, provider kind, and feature set before exposing an adapter", async () => {
    const contract = DESKTOP_PRODUCT_RELEASE_CONTRACT;
    const digest = contract.acceptedOpenApiDigests[0];
    const version = releaseVersion(digest, [...contract.requiredFeatureFlags]);
    const bootstrap = releaseBootstrap(digest, [...contract.requiredFeatureFlags]);
    const native = {
      bootstrap: vi.fn().mockResolvedValue(bootstrap),
      stop: vi.fn().mockResolvedValue(undefined),
      selectProjectSource: vi.fn().mockResolvedValue({
        kind: "native_folder_snapshot",
        display_name: "Native source",
        import_ref: {
          import_id: "source-opaque-1",
          content_sha256: "b".repeat(64),
          byte_size: 1024,
          entry_count: 1,
          extracted_byte_size: 12,
        },
      }),
      cancelProjectSource: vi.fn(),
      settleProjectSource: vi.fn(),
      configureCredential: vi.fn(),
    };
    const fetchMock = vi.fn<FetchLike>().mockResolvedValue(jsonResponse(version));
    const adapterFactory = vi.fn(async ({ native: bridge }) => {
      const source = await bridge.selectProjectSource({ kind: "native_folder_snapshot", actionId: "source-action-0001", streamEpoch: 7 });
      expect(source).toMatchObject({ kind: "native_folder_snapshot", import_ref: { import_id: "source-opaque-1" } });
      return unavailableDesktopProductProvider;
    });

    const provider = await createReleaseDesktopProductProvider({ fetch: fetchMock, native, adapterFactory });

    expect(provider.providerKind).toBe("desktop_sidecar");
    expect(native.bootstrap).toHaveBeenCalledTimes(1);
    expect(adapterFactory).toHaveBeenCalledTimes(1);
    expect(native.selectProjectSource).toHaveBeenCalledWith(expect.objectContaining({ kind: "native_folder_snapshot" }));
  });

  it("constructs the real Local API provider by default", async () => {
    const digest = DESKTOP_PRODUCT_RELEASE_CONTRACT.acceptedOpenApiDigests[0];
    const flags = [...DESKTOP_PRODUCT_RELEASE_CONTRACT.requiredFeatureFlags];
    const provider = await createReleaseDesktopProductProvider({
      fetch: vi.fn<FetchLike>().mockResolvedValue(jsonResponse(releaseVersion(digest, flags))),
      native: {
        bootstrap: vi.fn().mockResolvedValue(releaseBootstrap(digest, flags)),
        stop: vi.fn().mockResolvedValue(undefined),
        selectProjectSource: vi.fn(),
        cancelProjectSource: vi.fn(),
        settleProjectSource: vi.fn(),
        configureCredential: vi.fn(),
      },
    });
    expect(provider.providerKind).toBe("desktop_sidecar");
  });

  it("passes an existing project ID to the Tauri picker and omits it for a new project", async () => {
    const digest = DESKTOP_PRODUCT_RELEASE_CONTRACT.acceptedOpenApiDigests[0];
    const flags = [...DESKTOP_PRODUCT_RELEASE_CONTRACT.requiredFeatureFlags];
    const source = {
      kind: "native_folder_snapshot",
      display_name: "Native source",
      import_ref: {
        import_id: "source-opaque-bridge",
        content_sha256: "b".repeat(64),
        byte_size: 1024,
        entry_count: 1,
        extracted_byte_size: 12,
      },
    };
    invokeMock.mockImplementation(async (command: string) => {
      if (command === "start_sidecar") return releaseBootstrap(digest, flags);
      if (command === "select_project_source") return source;
      if (command === "cancel_project_source") return undefined;
      if (command === "settle_project_source") return undefined;
      throw new Error(`Unexpected Tauri command: ${command}`);
    });

    await createReleaseDesktopProductProvider({
      fetch: vi.fn<FetchLike>().mockResolvedValue(jsonResponse(releaseVersion(digest, flags))),
      adapterFactory: async ({ native }) => {
        await native.selectProjectSource({
          kind: "native_folder_snapshot",
          projectId: "project-existing-1",
          actionId: "source-action-existing",
          streamEpoch: 7,
        });
        await native.cancelProjectSource("source-action-existing");
        await native.settleProjectSource("source-action-new", "discard");
        await native.selectProjectSource({
          kind: "native_folder_snapshot",
          actionId: "source-action-new",
          streamEpoch: 7,
        });
        return unavailableDesktopProductProvider;
      },
    });

    const selectionCalls = invokeMock.mock.calls.filter(([command]) => command === "select_project_source");
    expect(selectionCalls).toEqual([
      ["select_project_source", {
        kind: "native_folder_snapshot",
        actionId: "source-action-existing",
        projectId: "project-existing-1",
      }],
      ["select_project_source", {
        kind: "native_folder_snapshot",
        actionId: "source-action-new",
      }],
    ]);
    expect(selectionCalls[1]?.[1]).not.toHaveProperty("projectId");
    expect(invokeMock).toHaveBeenCalledWith("cancel_project_source", {
      actionId: "source-action-existing",
    });
    expect(invokeMock).toHaveBeenCalledWith("settle_project_source", {
      actionId: "source-action-new",
      outcome: "discard",
    });
  });

  it("rejects simulator bootstrap and missing release features without constructing an adapter", async () => {
    const digest = DESKTOP_PRODUCT_RELEASE_CONTRACT.acceptedOpenApiDigests[0];
    const flags = [...DESKTOP_PRODUCT_RELEASE_CONTRACT.requiredFeatureFlags];
    const adapterFactory = vi.fn(() => unavailableDesktopProductProvider);
    await expect(createReleaseDesktopProductProvider({
      fetch: vi.fn<FetchLike>(),
      native: {
        bootstrap: vi.fn().mockResolvedValue({
          ...releaseBootstrap(digest, flags),
          negotiated_contract: { ...releaseBootstrap(digest, flags).negotiated_contract, provider_kind: "contract_simulator" },
        }),
        stop: vi.fn().mockResolvedValue(undefined),
        selectProjectSource: vi.fn(),
        cancelProjectSource: vi.fn(),
        settleProjectSource: vi.fn(),
        configureCredential: vi.fn(),
      },
      adapterFactory,
    })).rejects.toThrow(/forbidden provider kind/i);

    const incompleteFlags = flags.slice(0, -1);
    await expect(createReleaseDesktopProductProvider({
      fetch: vi.fn<FetchLike>().mockResolvedValue(jsonResponse(releaseVersion(digest, incompleteFlags))),
      native: {
        bootstrap: vi.fn().mockResolvedValue(releaseBootstrap(digest, incompleteFlags)),
        stop: vi.fn().mockResolvedValue(undefined),
        selectProjectSource: vi.fn(),
        cancelProjectSource: vi.fn(),
        settleProjectSource: vi.fn(),
        configureCredential: vi.fn(),
      },
      adapterFactory,
    })).rejects.toThrow(/missing required release features/i);
    expect(adapterFactory).not.toHaveBeenCalled();
  });

  it("rejects native source responses outside ProjectSourceV1 or cross-wired to another source kind", async () => {
    const digest = DESKTOP_PRODUCT_RELEASE_CONTRACT.acceptedOpenApiDigests[0];
    const flags = [...DESKTOP_PRODUCT_RELEASE_CONTRACT.requiredFeatureFlags];
    await expect(createReleaseDesktopProductProvider({
      fetch: vi.fn<FetchLike>().mockResolvedValue(jsonResponse(releaseVersion(digest, flags))),
      native: {
        bootstrap: vi.fn().mockResolvedValue(releaseBootstrap(digest, flags)),
        stop: vi.fn().mockResolvedValue(undefined),
        selectProjectSource: vi.fn().mockResolvedValue({
          kind: "native_folder_snapshot",
          display_name: "Native source",
          import_ref: {
            import_id: "source-opaque-1",
            content_sha256: "b".repeat(64),
            byte_size: 1024,
            entry_count: 1,
            extracted_byte_size: 12,
          },
          path: "/private/source",
        }),
        cancelProjectSource: vi.fn(),
        settleProjectSource: vi.fn(),
        configureCredential: vi.fn(),
      },
      adapterFactory: async ({ native }) => {
        await native.selectProjectSource({ kind: "native_folder_snapshot", actionId: "source-action-0002", streamEpoch: 1 });
        return unavailableDesktopProductProvider;
      },
    })).rejects.toThrow();

    await expect(createReleaseDesktopProductProvider({
      fetch: vi.fn<FetchLike>().mockResolvedValue(jsonResponse(releaseVersion(digest, flags))),
      native: {
        bootstrap: vi.fn().mockResolvedValue(releaseBootstrap(digest, flags)),
        stop: vi.fn().mockResolvedValue(undefined),
        selectProjectSource: vi.fn().mockResolvedValue({
          kind: "scratch",
          display_name: "Cross-wired scratch source",
          import_ref: null,
        }),
        cancelProjectSource: vi.fn(),
        settleProjectSource: vi.fn(),
        configureCredential: vi.fn(),
      },
      adapterFactory: async ({ native }) => {
        await native.selectProjectSource({ kind: "native_folder_snapshot", actionId: "source-action-0003", streamEpoch: 1 });
        return unavailableDesktopProductProvider;
      },
    })).rejects.toThrow(/requested kind/i);
  });

  it("rejects a native credential response cross-wired to another profile or slot", async () => {
    const digest = DESKTOP_PRODUCT_RELEASE_CONTRACT.acceptedOpenApiDigests[0];
    const flags = [...DESKTOP_PRODUCT_RELEASE_CONTRACT.requiredFeatureFlags];
    await expect(createReleaseDesktopProductProvider({
      fetch: vi.fn<FetchLike>().mockResolvedValue(jsonResponse(releaseVersion(digest, flags))),
      native: {
        bootstrap: vi.fn().mockResolvedValue(releaseBootstrap(digest, flags)),
        stop: vi.fn().mockResolvedValue(undefined),
        selectProjectSource: vi.fn(),
        cancelProjectSource: vi.fn(),
        settleProjectSource: vi.fn(),
        configureCredential: vi.fn().mockResolvedValue({
          ...CONTRACT_FIXTURE_V1.profile,
          profile_id: "profile-cross-wired",
          credential_slots: [{ kind: "ssh_password", status: "stored", updated_at: "2026-07-14T12:00:00Z" }],
        }),
      },
      adapterFactory: async ({ native }) => {
        await native.configureCredential(
          "profile-fixture-1",
          "ssh_private_key",
          `"${"a".repeat(64)}"`,
          "credential-action-0001",
        );
        return unavailableDesktopProductProvider;
      },
    })).rejects.toThrow(/profile slot/i);
  });

  it("fails closed without invoking an unavailable native credential command", async () => {
    const digest = DESKTOP_PRODUCT_RELEASE_CONTRACT.acceptedOpenApiDigests[0];
    const flags = [...DESKTOP_PRODUCT_RELEASE_CONTRACT.requiredFeatureFlags];
    invokeMock.mockImplementation(async (command: string) => {
      if (command === "start_sidecar") return releaseBootstrap(digest, flags);
      throw new Error(`Unexpected Tauri command: ${command}`);
    });

    await expect(createReleaseDesktopProductProvider({
      fetch: vi.fn<FetchLike>().mockResolvedValue(jsonResponse(releaseVersion(digest, flags))),
      adapterFactory: async ({ native }) => {
        await native.configureCredential(
          "profile-fixture-1",
          "ssh_private_key",
          `"${"a".repeat(64)}"`,
          "credential-action-0002",
        );
        return unavailableDesktopProductProvider;
      },
    })).rejects.toThrow(/SSH agent is the only supported authentication method/i);
    expect(invokeMock.mock.calls.some(([command]) => command === "configure_credential")).toBe(false);
  });

  it("rejects an older refresh result after a newer refresh has started", () => {
    const order = new ProductRefreshOrder();
    const older = order.begin();
    const newer = order.begin();

    expect(order.isCurrent(newer)).toBe(true);
    expect(order.isCurrent(older)).toBe(false);
  });
});

function releaseVersion(openapiSha256: string, featureFlags: string[]) {
  return {
    schema_version: "1",
    api_name: "openevo-desktop-local-api",
    preferred_major: 1,
    supported_majors: [1],
    openapi_sha256: openapiSha256,
    build_version: "1.0.0",
    source_commit: "abcdef12",
    build_channel: "release",
    provider_kind: "desktop_sidecar",
    feature_flags: featureFlags,
  };
}

function releaseBootstrap(openapiSha256: string, featureFlags: string[]) {
  return {
    schema_version: "1",
    endpoint: "http://127.0.0.1:43117",
    session_token: "release-desktop-session-token-0000000000001",
    negotiated_contract: {
      major: 1,
      openapi_sha256: openapiSha256,
      provider_kind: "desktop_sidecar",
      feature_flags: featureFlags,
    },
  } as const;
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });
}
