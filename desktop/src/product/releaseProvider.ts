import { invoke } from "@tauri-apps/api/core";
import type { DesktopApiClientV1, FetchLike } from "../api/v1/client";
import { createDesktopApiClient, DesktopContractError } from "../api/v1/client";
import {
  projectSourceV1Schema,
  type DesktopBootstrapContextV1,
  type ProjectSourceV1,
} from "../api/v1/schemas";
import { createLocalApiDesktopProductProvider } from "./localApiProvider";
import type { ProductRunRetryRecoveryStore } from "./runRetryRecovery";
import {
  type DesktopProductProvider,
  type ProjectSourceSelectionIntent,
  type ReleaseDesktopProductProvider,
} from "./provider";
import { DESKTOP_PRODUCT_RELEASE_CONTRACT } from "./releaseContract";

export interface ReleaseNativeBridge {
  bootstrap(): Promise<unknown>;
  stop(): Promise<unknown>;
  readRunRetryRecovery?(): Promise<unknown>;
  writeRunRetryRecovery?(value: string | null, expectedValue: string | null): Promise<unknown>;
  selectProjectSource(intent: ProjectSourceSelectionIntent): Promise<unknown>;
  cancelProjectSource(actionId: string): Promise<unknown>;
  settleProjectSource(actionId: string, outcome: "adopt" | "discard"): Promise<unknown>;
}

export interface ReleaseProviderAdapterContext {
  readonly client: DesktopApiClientV1;
  readonly native: {
    selectProjectSource(intent: ProjectSourceSelectionIntent): Promise<ProjectSourceV1>;
    cancelProjectSource(actionId: string): Promise<void>;
    settleProjectSource(actionId: string, outcome: "adopt" | "discard"): Promise<void>;
  };
}

export interface ReleaseProviderFactoryDependencies {
  readonly fetch?: FetchLike;
  readonly native?: ReleaseNativeBridge;
  readonly adapterFactory?: (context: ReleaseProviderAdapterContext) => Promise<DesktopProductProvider> | DesktopProductProvider;
  readonly retryRecoveryStore?: ProductRunRetryRecoveryStore;
}

const tauriNativeBridge: ReleaseNativeBridge = {
  bootstrap: () => invoke<DesktopBootstrapContextV1>("start_sidecar"),
  stop: () => invoke("stop_sidecar"),
  readRunRetryRecovery: () => invoke("read_run_retry_recovery"),
  writeRunRetryRecovery: (value, expectedValue) => invoke("write_run_retry_recovery", {
    value,
    expectedValue,
  }),
  selectProjectSource: (intent) => invoke("select_project_source", {
    kind: intent.kind,
    actionId: intent.actionId,
    ...(intent.projectId === undefined ? {} : { projectId: intent.projectId }),
  }),
  cancelProjectSource: (actionId) => invoke("cancel_project_source", { actionId }),
  settleProjectSource: (actionId, outcome) => invoke("settle_project_source", {
    actionId,
    outcome,
  }),
};

export async function stopReleaseDesktopProductProvider(): Promise<void> {
  await tauriNativeBridge.stop();
}

export async function reportReleaseDesktopReady(): Promise<void> {
  await invoke("renderer_ready", {
    openapiSha256: DESKTOP_PRODUCT_RELEASE_CONTRACT.acceptedOpenApiDigests[0],
  });
}

export async function createReleaseDesktopProductProvider(
  dependencies: ReleaseProviderFactoryDependencies = {},
): Promise<ReleaseDesktopProductProvider> {
  const native = dependencies.native ?? tauriNativeBridge;
  const client = createDesktopApiClient({
    fetch: dependencies.fetch ?? globalThis.fetch.bind(globalThis),
    bootstrap: () => native.bootstrap(),
    supportedMajors: [1],
    acceptedOpenApiDigests: DESKTOP_PRODUCT_RELEASE_CONTRACT.acceptedOpenApiDigests,
    allowedProviderKinds: DESKTOP_PRODUCT_RELEASE_CONTRACT.allowedProviderKinds,
  });

  const version = await client.version();
  const missingFeatures = DESKTOP_PRODUCT_RELEASE_CONTRACT.requiredFeatureFlags.filter(
    (feature) => !version.feature_flags.includes(feature),
  );
  if (missingFeatures.length > 0) {
    throw new DesktopContractError("Desktop Local API is missing required release features");
  }
  const context: ReleaseProviderAdapterContext = {
    client,
    native: {
      selectProjectSource: async (intent) => {
        const source = projectSourceV1Schema.parse(await native.selectProjectSource(intent));
        if (source.kind !== intent.kind || source.import_ref === null) {
          throw new DesktopContractError("Native project source does not match the requested kind");
        }
        return source;
      },
      cancelProjectSource: async (actionId) => {
        await native.cancelProjectSource(actionId);
      },
      settleProjectSource: async (actionId, outcome) => {
        await native.settleProjectSource(actionId, outcome);
      },
    },
  };
  const provider = dependencies.adapterFactory
    ? await dependencies.adapterFactory(context)
    : createLocalApiDesktopProductProvider({
        client,
        native: context.native,
        fetch: dependencies.fetch,
        retryRecoveryStore: dependencies.retryRecoveryStore
          ?? await nativeRunRetryRecoveryStore(native),
      });
  assertReleaseProvider(provider);
  return provider;
}

export async function nativeRunRetryRecoveryStore(
  native: Pick<ReleaseNativeBridge, "readRunRetryRecovery" | "writeRunRetryRecovery">,
): Promise<ProductRunRetryRecoveryStore> {
  const read = native.readRunRetryRecovery;
  const write = native.writeRunRetryRecovery;
  if (!read || !write) {
    throw new DesktopContractError("Native Desktop run retry recovery is unavailable");
  }
  let value = parseNativeRunRetryRecovery(await read());
  let poisoned = false;
  return {
    read: () => {
      if (poisoned) {
        throw new DesktopContractError("Native Desktop run retry recovery requires an application restart");
      }
      return value;
    },
    write: async (next) => {
      if (poisoned) {
        throw new DesktopContractError("Native Desktop run retry recovery requires an application restart");
      }
      const expectedValue = value;
      try {
        await write(next, expectedValue);
        value = next;
      } catch (writeError) {
        let observed: string | null;
        try {
          observed = parseNativeRunRetryRecovery(await read());
        } catch (readError) {
          poisoned = true;
          throw new DesktopContractError(
            "Native Desktop run retry recovery could not reconcile a failed write",
            { cause: readError },
          );
        }
        if (observed === next) {
          value = next;
          return;
        }
        if (observed === expectedValue) throw writeError;
        poisoned = true;
        throw new DesktopContractError(
          "Native Desktop run retry recovery changed in another process",
          { cause: writeError },
        );
      }
    },
  };
}

function parseNativeRunRetryRecovery(value: unknown): string | null {
  if (value !== null && typeof value !== "string") {
    throw new DesktopContractError("Native Desktop run retry recovery returned an invalid record");
  }
  return value;
}

function assertReleaseProvider(
  provider: DesktopProductProvider,
): asserts provider is ReleaseDesktopProductProvider {
  if (provider.providerKind !== "desktop_sidecar") {
    throw new DesktopContractError("Release provider adapter reported a forbidden provider kind");
  }
  if (typeof provider.retryRun !== "function") {
    throw new DesktopContractError("Release provider adapter is missing the run retry contract");
  }
  if (typeof provider.getRunRetryRecovery !== "function") {
    throw new DesktopContractError("Release provider adapter is missing durable run retry recovery");
  }
}
