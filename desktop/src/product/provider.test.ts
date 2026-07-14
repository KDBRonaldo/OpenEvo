import { describe, expect, it, vi } from "vitest";
import type { FetchLike } from "../api/v1/client";
import {
  DesktopProductProviderUnavailableError,
  ProductRefreshOrder,
  defineDesktopProductReleaseContract,
  unavailableDesktopProductProvider,
  type DesktopProductReleaseContract,
} from "./provider";
import { DESKTOP_PRODUCT_RELEASE_CONTRACT } from "./releaseContract";
import { createReleaseDesktopProductProvider } from "./releaseProvider";
import { createFixtureDesktopProductProvider } from "./fixtureProvider";

describe("Desktop product provider boundary", () => {
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
      "5a571f32c547063677533be9b4ccae417e2037b11963b5770d245f6c5419830e",
    ]);
  });

  it("negotiates the native bootstrap, checked-in digest, provider kind, and feature set before exposing an adapter", async () => {
    const contract = DESKTOP_PRODUCT_RELEASE_CONTRACT;
    const digest = contract.acceptedOpenApiDigests[0];
    const version = releaseVersion(digest, [...contract.requiredFeatureFlags]);
    const bootstrap = releaseBootstrap(digest, [...contract.requiredFeatureFlags]);
    const native = {
      bootstrap: vi.fn().mockResolvedValue(bootstrap),
      selectProjectSource: vi.fn().mockResolvedValue({
        kind: "native_folder_snapshot",
        display_name: "Native source",
        source_ref: { content_id: "source-opaque-1", sha256: "b".repeat(64), byte_size: 12 },
      }),
    };
    const fetchMock = vi.fn<FetchLike>().mockResolvedValue(jsonResponse(version));
    const adapterFactory = vi.fn(async ({ native: bridge }) => {
      const source = await bridge.selectProjectSource({ kind: "native_folder_snapshot", actionId: "source-action-0001", streamEpoch: 7 });
      expect(source).toMatchObject({ kind: "native_folder_snapshot", source_ref: { content_id: "source-opaque-1" } });
      return unavailableDesktopProductProvider;
    });

    const provider = await createReleaseDesktopProductProvider({ fetch: fetchMock, native, adapterFactory });

    expect(provider.providerKind).toBe("desktop_sidecar");
    expect(native.bootstrap).toHaveBeenCalledTimes(1);
    expect(adapterFactory).toHaveBeenCalledTimes(1);
    expect(native.selectProjectSource).toHaveBeenCalledWith(expect.objectContaining({ kind: "native_folder_snapshot" }));
  });

  it("fails closed after successful negotiation when the real product adapter is not installed", async () => {
    const digest = DESKTOP_PRODUCT_RELEASE_CONTRACT.acceptedOpenApiDigests[0];
    const flags = [...DESKTOP_PRODUCT_RELEASE_CONTRACT.requiredFeatureFlags];
    await expect(createReleaseDesktopProductProvider({
      fetch: vi.fn<FetchLike>().mockResolvedValue(jsonResponse(releaseVersion(digest, flags))),
      native: {
        bootstrap: vi.fn().mockResolvedValue(releaseBootstrap(digest, flags)),
        selectProjectSource: vi.fn(),
      },
    })).rejects.toBeInstanceOf(DesktopProductProviderUnavailableError);
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
        selectProjectSource: vi.fn(),
      },
      adapterFactory,
    })).rejects.toThrow(/forbidden provider kind/i);

    const incompleteFlags = flags.slice(0, -1);
    await expect(createReleaseDesktopProductProvider({
      fetch: vi.fn<FetchLike>().mockResolvedValue(jsonResponse(releaseVersion(digest, incompleteFlags))),
      native: {
        bootstrap: vi.fn().mockResolvedValue(releaseBootstrap(digest, incompleteFlags)),
        selectProjectSource: vi.fn(),
      },
      adapterFactory,
    })).rejects.toThrow(/missing required release features/i);
    expect(adapterFactory).not.toHaveBeenCalled();
  });

  it("rejects native source responses that expose fields outside ProjectSourceV1", async () => {
    const digest = DESKTOP_PRODUCT_RELEASE_CONTRACT.acceptedOpenApiDigests[0];
    const flags = [...DESKTOP_PRODUCT_RELEASE_CONTRACT.requiredFeatureFlags];
    await expect(createReleaseDesktopProductProvider({
      fetch: vi.fn<FetchLike>().mockResolvedValue(jsonResponse(releaseVersion(digest, flags))),
      native: {
        bootstrap: vi.fn().mockResolvedValue(releaseBootstrap(digest, flags)),
        selectProjectSource: vi.fn().mockResolvedValue({
          kind: "native_folder_snapshot",
          display_name: "Native source",
          source_ref: { content_id: "source-opaque-1", sha256: "b".repeat(64), byte_size: 12 },
          path: "/private/source",
        }),
      },
      adapterFactory: async ({ native }) => {
        await native.selectProjectSource({ kind: "native_folder_snapshot", actionId: "source-action-0002", streamEpoch: 1 });
        return unavailableDesktopProductProvider;
      },
    })).rejects.toThrow();
  });

  it("binds fixture mutations to the observed stream epoch, ETag, and action identity", async () => {
    const provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    const refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("Fixture refresh was not fresh.");
    const project = refreshed.snapshot.projects[0]!;

    await expect(provider.updateProject(project.project_id, { name: "Changed" }, {
      actionId: "mutation-action-0001",
      streamEpoch: refreshed.snapshot.stream.epoch,
      etag: `"${"f".repeat(64)}"`,
    })).rejects.toThrow(/changed remotely/i);

    await provider.syncProjectWorkspace(project.project_id, {
      actionId: "mutation-action-0002",
      streamEpoch: refreshed.snapshot.stream.epoch,
      etag: project.etag,
    });
    await expect(provider.repairProject(project.project_id, {
      actionId: "mutation-action-0002",
      streamEpoch: refreshed.snapshot.stream.epoch,
      etag: project.etag,
    })).rejects.toThrow(/different request/i);

    provider.markStreamStale();
    await expect(provider.startRun({
      projectId: project.project_id,
      actionId: "mutation-action-0003",
      streamEpoch: refreshed.snapshot.stream.epoch,
      etag: project.etag,
    })).rejects.toThrow(/out of date/i);
    provider.dispose();
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
