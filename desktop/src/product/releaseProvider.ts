import { invoke } from "@tauri-apps/api/core";
import type { DesktopApiClientV1, FetchLike } from "../api/v1/client";
import {
  createDesktopApiClient,
  DesktopContractError,
  validateDesktopBootstrapContext,
} from "../api/v1/client";
import {
  projectSourceV1Schema,
  type DesktopBootstrapContextV1,
  type ProjectSourceV1,
  type VersionInfoV1,
} from "../api/v1/schemas";
import {
  createLocalApiDesktopProductProvider,
  systemMaintenanceAvailableForFeatures,
} from "./localApiProvider";
import type { ProductRunRetryRecoveryStore } from "./runRetryRecovery";
import {
  type DesktopProductProvider,
  type ProjectSourceSelectionIntent,
  type ReleaseDesktopProductProvider,
  withOperationContinuationAuthority,
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
  readonly featureFlags: readonly VersionInfoV1["feature_flags"][number][];
  readonly native: {
    selectProjectSource(intent: ProjectSourceSelectionIntent): Promise<ProjectSourceV1>;
    cancelProjectSource(actionId: string): Promise<void>;
    settleProjectSource(actionId: string, outcome: "adopt" | "discard"): Promise<void>;
  };
}

export const RELEASE_DESKTOP_BOOTSTRAP_STAGES = [
  "bootstrap_context_validated",
  "bootstrap_context_failed",
  "local_api_version_verified",
  "local_api_version_failed",
  "retry_recovery_ready",
  "retry_recovery_failed",
  "provider_adapter_ready",
  "provider_adapter_failed",
  "provider_created",
  "provider_create_failed",
  "initial_snapshot_failed",
  "product_committed",
] as const;

export type ReleaseDesktopBootstrapStage = typeof RELEASE_DESKTOP_BOOTSTRAP_STAGES[number];

export interface ReleaseProviderFactoryDependencies {
  readonly fetch?: FetchLike;
  readonly native?: ReleaseNativeBridge;
  readonly adapterFactory?: (context: ReleaseProviderAdapterContext) => Promise<DesktopProductProvider> | DesktopProductProvider;
  readonly retryRecoveryStore?: ProductRunRetryRecoveryStore;
  readonly reportStage?: (stage: ReleaseDesktopBootstrapStage) => Promise<void> | void;
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

export function reportReleaseDesktopBootstrapStage(stage: ReleaseDesktopBootstrapStage): void {
  try {
    void invoke("renderer_bootstrap_stage", { stage }).catch(() => {});
  } catch {
    // Diagnostics never participate in startup or readiness authority.
  }
}

export async function createReleaseDesktopProductProvider(
  dependencies: ReleaseProviderFactoryDependencies = {},
): Promise<ReleaseDesktopProductProvider> {
  const native = dependencies.native ?? tauriNativeBridge;
  const adapterFactory = dependencies.adapterFactory;
  const reportStage = dependencies.reportStage ?? reportReleaseDesktopBootstrapStage;
  let bootstrap: DesktopBootstrapContextV1;
  try {
    bootstrap = validateDesktopBootstrapContext(await native.bootstrap(), {
      supportedMajors: [1],
      acceptedOpenApiDigests: DESKTOP_PRODUCT_RELEASE_CONTRACT.acceptedOpenApiDigests,
      allowedProviderKinds: DESKTOP_PRODUCT_RELEASE_CONTRACT.allowedProviderKinds,
    });
    reportStageBestEffort(reportStage, "bootstrap_context_validated");
  } catch (error) {
    reportStageBestEffort(reportStage, "bootstrap_context_failed");
    throw error;
  }
  const client = createDesktopApiClient({
    fetch: dependencies.fetch ?? globalThis.fetch.bind(globalThis),
    bootstrap: async () => bootstrap,
    supportedMajors: [1],
    acceptedOpenApiDigests: DESKTOP_PRODUCT_RELEASE_CONTRACT.acceptedOpenApiDigests,
    allowedProviderKinds: DESKTOP_PRODUCT_RELEASE_CONTRACT.allowedProviderKinds,
  });

  let version: VersionInfoV1;
  try {
    version = await client.version();
    const missingFeatures = DESKTOP_PRODUCT_RELEASE_CONTRACT.requiredFeatureFlags.filter(
      (feature) => !version.feature_flags.includes(feature),
    );
    if (missingFeatures.length > 0) {
      throw new DesktopContractError("Desktop Local API is missing required release features");
    }
    reportStageBestEffort(reportStage, "local_api_version_verified");
  } catch (error) {
    reportStageBestEffort(reportStage, "local_api_version_failed");
    throw error;
  }
  const featureFlags = Object.freeze([...version.feature_flags]);
  const context: ReleaseProviderAdapterContext = {
    client,
    featureFlags,
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
  let retryRecoveryStore = dependencies.retryRecoveryStore;
  if (adapterFactory === undefined && retryRecoveryStore === undefined) {
    try {
      retryRecoveryStore = await nativeRunRetryRecoveryStore(native);
      reportStageBestEffort(reportStage, "retry_recovery_ready");
    } catch (error) {
      reportStageBestEffort(reportStage, "retry_recovery_failed");
      throw error;
    }
  } else if (adapterFactory === undefined) {
    reportStageBestEffort(reportStage, "retry_recovery_ready");
  }
  try {
    let provider: DesktopProductProvider;
    if (adapterFactory !== undefined) {
      provider = await adapterFactory(context);
    } else {
      if (retryRecoveryStore === undefined) {
        throw new DesktopContractError("Native Desktop run retry recovery is unavailable");
      }
      provider = createLocalApiDesktopProductProvider({
        client,
        native: context.native,
        featureFlags,
        fetch: dependencies.fetch,
        retryRecoveryStore,
      });
    }
    assertReleaseProvider(
      provider,
      systemMaintenanceAvailableForFeatures(featureFlags),
    );
    const authoritativeProvider = withOperationContinuationAuthority(provider);
    reportStageBestEffort(reportStage, "provider_adapter_ready");
    return authoritativeProvider;
  } catch (error) {
    reportStageBestEffort(reportStage, "provider_adapter_failed");
    throw error;
  }
}

function reportStageBestEffort(
  reportStage: (stage: ReleaseDesktopBootstrapStage) => Promise<void> | void,
  stage: ReleaseDesktopBootstrapStage,
): void {
  try {
    void Promise.resolve(reportStage(stage)).catch(() => {});
  } catch {
    // Closed diagnostics cannot alter provider construction.
  }
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
        if (observed === expectedValue) throw writeError;
        poisoned = true;
        if (observed === next) {
          throw new DesktopContractError(
            "Native Desktop run retry recovery write outcome requires an application restart",
            { cause: writeError },
          );
        }
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
  systemMaintenanceAvailable: boolean,
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
  if (provider.systemMaintenanceAvailable !== systemMaintenanceAvailable) {
    throw new DesktopContractError(
      "Release provider adapter maintenance capability does not match the negotiated feature set",
    );
  }
  const requiredSystemActions: ReadonlyArray<keyof DesktopProductProvider> = [
    "getLocalOperation",
    "doctorProject",
    "repairProject",
    "restartService",
    "getCoreOperation",
    "createDiagnostic",
    "getDiagnostic",
    "cleanupCaches",
  ];
  if (requiredSystemActions.some((action) => typeof provider[action] !== "function")) {
    throw new DesktopContractError("Release provider adapter is missing System recovery capabilities");
  }
}
