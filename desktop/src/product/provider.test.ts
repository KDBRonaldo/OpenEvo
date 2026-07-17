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
  nativeRunRetryRecoveryStore,
  reportReleaseDesktopBootstrapStage,
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
      "60cd51f9ab1e7b1140747b9cc5d3760fad32204e4e5c399b608bb5d406172777",
    ]);
  });

  it("binds renderer readiness to the frozen Local API digest", async () => {
    invokeMock.mockResolvedValue(undefined);

    await reportReleaseDesktopReady();

    expect(invokeMock).toHaveBeenCalledWith("renderer_ready", {
      openapiSha256: DESKTOP_PRODUCT_RELEASE_CONTRACT.acceptedOpenApiDigests[0],
    });
  });

  it("reports only a closed renderer bootstrap stage through native IPC", async () => {
    invokeMock.mockResolvedValue(undefined);

    reportReleaseDesktopBootstrapStage("provider_created");

    expect(invokeMock).toHaveBeenCalledWith("renderer_bootstrap_stage", {
      stage: "provider_created",
    });
  });

  it("fails closed when run retry is unavailable", async () => {
    await expect(unavailableDesktopProductProvider.retryRun!("run-fixture-1", {
      actionId: "renderer-action-retry-0001",
      streamEpoch: 7,
      etag: '"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"',
    })).rejects.toThrow("local service");
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
    };
    const fetchMock = vi.fn<FetchLike>().mockResolvedValue(jsonResponse(version));
    const reportStage = vi.fn();
    const adapterFactory = vi.fn(async ({ native: bridge }) => {
      const source = await bridge.selectProjectSource({ kind: "native_folder_snapshot", actionId: "source-action-0001", streamEpoch: 7 });
      expect(source).toMatchObject({ kind: "native_folder_snapshot", import_ref: { import_id: "source-opaque-1" } });
      return unavailableDesktopProductProvider;
    });

    const provider = await createReleaseDesktopProductProvider({
      fetch: fetchMock,
      native,
      adapterFactory,
      reportStage,
    });

    expect(provider.providerKind).toBe("desktop_sidecar");
    expect(native.bootstrap).toHaveBeenCalledTimes(1);
    expect(adapterFactory).toHaveBeenCalledTimes(1);
    expect(native.selectProjectSource).toHaveBeenCalledWith(expect.objectContaining({ kind: "native_folder_snapshot" }));
    expect(reportStage.mock.calls.map(([stage]) => stage)).toEqual([
      "bootstrap_context_validated",
      "local_api_version_verified",
      "provider_adapter_ready",
    ]);
  });

  it("constructs the real Local API provider by default", async () => {
    const digest = DESKTOP_PRODUCT_RELEASE_CONTRACT.acceptedOpenApiDigests[0];
    const flags = [...DESKTOP_PRODUCT_RELEASE_CONTRACT.requiredFeatureFlags];
    const provider = await createReleaseDesktopProductProvider({
      fetch: vi.fn<FetchLike>().mockResolvedValue(jsonResponse(releaseVersion(digest, flags))),
      retryRecoveryStore: memoryRetryRecoveryStore(),
      native: {
        bootstrap: vi.fn().mockResolvedValue(releaseBootstrap(digest, flags)),
        stop: vi.fn().mockResolvedValue(undefined),
        selectProjectSource: vi.fn(),
        cancelProjectSource: vi.fn(),
        settleProjectSource: vi.fn(),
      },
    });
    expect(provider.providerKind).toBe("desktop_sidecar");
  });

  it("requires and restores the native retry journal for the release provider", async () => {
    const digest = DESKTOP_PRODUCT_RELEASE_CONTRACT.acceptedOpenApiDigests[0];
    const flags = [...DESKTOP_PRODUCT_RELEASE_CONTRACT.requiredFeatureFlags];
    const native = {
      bootstrap: vi.fn().mockResolvedValue(releaseBootstrap(digest, flags)),
      stop: vi.fn().mockResolvedValue(undefined),
      readRunRetryRecovery: vi.fn().mockResolvedValue(null),
      writeRunRetryRecovery: vi.fn().mockResolvedValue(undefined),
      selectProjectSource: vi.fn(),
      cancelProjectSource: vi.fn(),
      settleProjectSource: vi.fn(),
    };
    const reportStage = vi.fn();

    const provider = await createReleaseDesktopProductProvider({
      fetch: vi.fn<FetchLike>().mockResolvedValue(jsonResponse(releaseVersion(digest, flags))),
      native,
      reportStage,
    });

    expect(provider.providerKind).toBe("desktop_sidecar");
    expect(native.readRunRetryRecovery).toHaveBeenCalledTimes(1);
    expect(reportStage.mock.calls.map(([stage]) => stage)).toEqual([
      "bootstrap_context_validated",
      "local_api_version_verified",
      "retry_recovery_ready",
      "provider_adapter_ready",
    ]);
  });

  it("reports the exact closed provider bootstrap failure boundary", async () => {
    const digest = DESKTOP_PRODUCT_RELEASE_CONTRACT.acceptedOpenApiDigests[0];
    const flags = [...DESKTOP_PRODUCT_RELEASE_CONTRACT.requiredFeatureFlags];
    const baseNative = {
      bootstrap: vi.fn().mockResolvedValue(releaseBootstrap(digest, flags)),
      stop: vi.fn().mockResolvedValue(undefined),
      readRunRetryRecovery: vi.fn().mockResolvedValue(null),
      writeRunRetryRecovery: vi.fn(),
      selectProjectSource: vi.fn(),
      cancelProjectSource: vi.fn(),
      settleProjectSource: vi.fn(),
    };

    const bootstrapStages = vi.fn();
    await expect(createReleaseDesktopProductProvider({
      fetch: vi.fn<FetchLike>(),
      native: {
        ...baseNative,
        bootstrap: vi.fn().mockResolvedValue({ invalid: true }),
      },
      adapterFactory: () => unavailableDesktopProductProvider,
      reportStage: bootstrapStages,
    })).rejects.toThrow();
    expect(bootstrapStages.mock.calls.map(([stage]) => stage)).toEqual([
      "bootstrap_context_failed",
    ]);

    const versionStages = vi.fn();
    await expect(createReleaseDesktopProductProvider({
      fetch: vi.fn<FetchLike>().mockRejectedValue(new TypeError("blocked transport")),
      native: baseNative,
      adapterFactory: () => unavailableDesktopProductProvider,
      reportStage: versionStages,
    })).rejects.toThrow("blocked transport");
    expect(versionStages.mock.calls.map(([stage]) => stage)).toEqual([
      "bootstrap_context_validated",
      "local_api_version_failed",
    ]);

    const recoveryStages = vi.fn();
    await expect(createReleaseDesktopProductProvider({
      fetch: vi.fn<FetchLike>().mockResolvedValue(jsonResponse(releaseVersion(digest, flags))),
      native: {
        ...baseNative,
        readRunRetryRecovery: vi.fn().mockRejectedValue(new Error("native recovery failed")),
      },
      reportStage: recoveryStages,
    })).rejects.toThrow("native recovery failed");
    expect(recoveryStages.mock.calls.map(([stage]) => stage)).toEqual([
      "bootstrap_context_validated",
      "local_api_version_verified",
      "retry_recovery_failed",
    ]);

    const adapterStages = vi.fn();
    await expect(createReleaseDesktopProductProvider({
      fetch: vi.fn<FetchLike>().mockResolvedValue(jsonResponse(releaseVersion(digest, flags))),
      native: baseNative,
      adapterFactory: () => {
        throw new Error("adapter failed");
      },
      reportStage: adapterStages,
    })).rejects.toThrow("adapter failed");
    expect(adapterStages.mock.calls.map(([stage]) => stage)).toEqual([
      "bootstrap_context_validated",
      "local_api_version_verified",
      "provider_adapter_failed",
    ]);
  });

  it("uses native compare-and-swap authority and fails closed after a lost write response", async () => {
    let durable: string | null = null;
    const writeRunRetryRecovery = vi.fn(async (next: string | null, expected: string | null) => {
      expect(durable).toBe(expected);
      durable = next;
    });
    const native = {
      readRunRetryRecovery: vi.fn(async () => durable),
      writeRunRetryRecovery,
    };
    const store = await nativeRunRetryRecoveryStore(native);

    await store.write("first");
    await store.write("second");

    expect(writeRunRetryRecovery).toHaveBeenNthCalledWith(1, "first", null);
    expect(writeRunRetryRecovery).toHaveBeenNthCalledWith(2, "second", "first");
    expect(store.read()).toBe("second");

    writeRunRetryRecovery.mockImplementationOnce(async (next: string | null, expected: string | null) => {
      expect(durable).toBe(expected);
      durable = next;
      throw new Error("native response lost after commit");
    });
    await expect(store.write("third")).rejects.toThrow(/restart/i);
    expect(() => store.read()).toThrow(/restart/i);
    await expect(store.write("fourth")).rejects.toThrow(/restart/i);
    expect(durable).toBe("third");
  });

  it("fails closed when another Desktop process wins the native retry journal", async () => {
    let durable: string | null = null;
    const conflict = new Error("run_retry_recovery_conflict");
    const native = {
      readRunRetryRecovery: vi.fn(async () => durable),
      writeRunRetryRecovery: vi.fn(async () => {
        durable = "other-process-authority";
        throw conflict;
      }),
    };
    const store = await nativeRunRetryRecoveryStore(native);

    await expect(store.write("this-process-authority")).rejects.toThrow(/another process/i);
    expect(() => store.read()).toThrow(/restart/i);
    await expect(store.write("replacement")).rejects.toThrow(/restart/i);
    expect(native.writeRunRetryRecovery).toHaveBeenCalledTimes(1);
    expect(durable).toBe("other-process-authority");
  });

  it("keeps a deterministic unchanged native write failure retryable", async () => {
    const failure = new Error("native lock temporarily unavailable");
    const native = {
      readRunRetryRecovery: vi.fn().mockResolvedValue(null),
      writeRunRetryRecovery: vi.fn().mockRejectedValueOnce(failure).mockResolvedValueOnce(undefined),
    };
    const store = await nativeRunRetryRecoveryStore(native);

    await expect(store.write("authority")).rejects.toBe(failure);
    await expect(store.write("authority")).resolves.toBeUndefined();
    expect(native.writeRunRetryRecovery).toHaveBeenNthCalledWith(2, "authority", null);
  });

  it("fails release startup when the native retry journal is unavailable or malformed", async () => {
    const digest = DESKTOP_PRODUCT_RELEASE_CONTRACT.acceptedOpenApiDigests[0];
    const flags = [...DESKTOP_PRODUCT_RELEASE_CONTRACT.requiredFeatureFlags];
    const baseNative = {
      bootstrap: vi.fn().mockResolvedValue(releaseBootstrap(digest, flags)),
      stop: vi.fn().mockResolvedValue(undefined),
      selectProjectSource: vi.fn(),
      cancelProjectSource: vi.fn(),
      settleProjectSource: vi.fn(),
    };
    const fetch = () => vi.fn<FetchLike>().mockResolvedValue(jsonResponse(releaseVersion(digest, flags)));

    await expect(createReleaseDesktopProductProvider({ fetch: fetch(), native: baseNative }))
      .rejects.toThrow(/native.*retry recovery.*unavailable/i);
    await expect(createReleaseDesktopProductProvider({
      fetch: fetch(),
      native: {
        ...baseNative,
        readRunRetryRecovery: vi.fn().mockResolvedValue({ corrupted: true }),
        writeRunRetryRecovery: vi.fn(),
      },
    })).rejects.toThrow(/invalid record/i);
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
    const simulatorStages = vi.fn();
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
      },
      adapterFactory,
      reportStage: simulatorStages,
    })).rejects.toThrow(/forbidden provider kind/i);
    expect(simulatorStages.mock.calls.map(([stage]) => stage)).toEqual([
      "bootstrap_context_failed",
    ]);

    const incompleteFlags = flags.slice(0, -1);
    const incompleteStages = vi.fn();
    await expect(createReleaseDesktopProductProvider({
      fetch: vi.fn<FetchLike>().mockResolvedValue(jsonResponse(releaseVersion(digest, incompleteFlags))),
      native: {
        bootstrap: vi.fn().mockResolvedValue(releaseBootstrap(digest, incompleteFlags)),
        stop: vi.fn().mockResolvedValue(undefined),
        selectProjectSource: vi.fn(),
        cancelProjectSource: vi.fn(),
        settleProjectSource: vi.fn(),
      },
      adapterFactory,
      reportStage: incompleteStages,
    })).rejects.toThrow(/missing required release features/i);
    expect(incompleteStages.mock.calls.map(([stage]) => stage)).toEqual([
      "bootstrap_context_validated",
      "local_api_version_failed",
    ]);
    expect(adapterFactory).not.toHaveBeenCalled();
  });

  it("rejects a release adapter without the run retry contract", async () => {
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
      },
      adapterFactory: () => ({ ...unavailableDesktopProductProvider, retryRun: undefined }),
    })).rejects.toThrow(/run retry contract/i);

    await expect(createReleaseDesktopProductProvider({
      fetch: vi.fn<FetchLike>().mockResolvedValue(jsonResponse(releaseVersion(digest, flags))),
      native: {
        bootstrap: vi.fn().mockResolvedValue(releaseBootstrap(digest, flags)),
        stop: vi.fn().mockResolvedValue(undefined),
        selectProjectSource: vi.fn(),
        cancelProjectSource: vi.fn(),
        settleProjectSource: vi.fn(),
      },
      adapterFactory: () => ({ ...unavailableDesktopProductProvider, getRunRetryRecovery: undefined }),
    })).rejects.toThrow(/durable run retry recovery/i);
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
      },
      adapterFactory: async ({ native }) => {
        await native.selectProjectSource({ kind: "native_folder_snapshot", actionId: "source-action-0003", streamEpoch: 1 });
        return unavailableDesktopProductProvider;
      },
    })).rejects.toThrow(/requested kind/i);
  });

  it("does not expose a native credential command to the release adapter", async () => {
    const digest = DESKTOP_PRODUCT_RELEASE_CONTRACT.acceptedOpenApiDigests[0];
    const flags = [...DESKTOP_PRODUCT_RELEASE_CONTRACT.requiredFeatureFlags];
    await createReleaseDesktopProductProvider({
      fetch: vi.fn<FetchLike>().mockResolvedValue(jsonResponse(releaseVersion(digest, flags))),
      native: {
        bootstrap: vi.fn().mockResolvedValue(releaseBootstrap(digest, flags)),
        stop: vi.fn().mockResolvedValue(undefined),
        selectProjectSource: vi.fn(),
        cancelProjectSource: vi.fn(),
        settleProjectSource: vi.fn(),
      },
      adapterFactory: async ({ native }) => {
        expect("configureCredential" in native).toBe(false);
        return unavailableDesktopProductProvider;
      },
    });
  });

  it("never invokes a Tauri credential command", async () => {
    const digest = DESKTOP_PRODUCT_RELEASE_CONTRACT.acceptedOpenApiDigests[0];
    const flags = [...DESKTOP_PRODUCT_RELEASE_CONTRACT.requiredFeatureFlags];
    invokeMock.mockImplementation(async (command: string) => {
      if (command === "start_sidecar") return releaseBootstrap(digest, flags);
      throw new Error(`Unexpected Tauri command: ${command}`);
    });

    await createReleaseDesktopProductProvider({
      fetch: vi.fn<FetchLike>().mockResolvedValue(jsonResponse(releaseVersion(digest, flags))),
      adapterFactory: async () => unavailableDesktopProductProvider,
    });
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

function memoryRetryRecoveryStore() {
  let value: string | null = null;
  return {
    read: () => value,
    write: (next: string | null) => { value = next; },
  };
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });
}
