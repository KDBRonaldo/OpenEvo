import { invoke } from "@tauri-apps/api/core";
import type { DesktopApiClientV2, FetchLikeV2 } from "../api/v2/client";
import {
  createDesktopApiClientV2,
  DesktopContractErrorV2,
  validateDesktopBootstrapContextV2,
} from "../api/v2/client";
import type {
  DesktopBootstrapContextV2,
  DesktopVersionV2,
} from "../api/v2/schemas";
import {
  createLocalApiDesktopProductProviderV2,
  type LocalApiNativeBridgeV2,
} from "./localApiProviderV2";
import type {
  DesktopProductProviderV2,
  NativeWorkspaceSelectionIntentV2,
} from "./providerV2";
import { DESKTOP_PRODUCT_RELEASE_CONTRACT } from "./releaseContract";

export interface ReleaseNativeBridgeV2 {
  bootstrap(): Promise<unknown>;
  stop(): Promise<unknown>;
  selectProjectSource(intent: NativeWorkspaceSelectionIntentV2): Promise<unknown>;
  cancelProjectSource(actionId: string): Promise<unknown>;
  settleProjectSource(actionId: string, outcome: "adopt" | "discard"): Promise<unknown>;
}

export interface ReleaseProviderAdapterContextV2 {
  readonly client: DesktopApiClientV2;
  readonly featureFlags: readonly string[];
  readonly native: LocalApiNativeBridgeV2;
}

export const RELEASE_DESKTOP_BOOTSTRAP_STAGES = [
  "bootstrap_context_validated",
  "bootstrap_context_failed",
  "local_api_version_verified",
  "local_api_version_failed",
  "provider_adapter_ready",
  "provider_adapter_failed",
  "provider_created",
  "provider_create_failed",
  "initial_snapshot_failed",
  "product_committed",
] as const;

export type ReleaseDesktopBootstrapStage = typeof RELEASE_DESKTOP_BOOTSTRAP_STAGES[number];

export interface ReleaseProviderFactoryDependenciesV2 {
  readonly fetch?: FetchLikeV2;
  readonly native?: ReleaseNativeBridgeV2;
  readonly adapterFactory?: (
    context: ReleaseProviderAdapterContextV2,
  ) => Promise<DesktopProductProviderV2> | DesktopProductProviderV2;
  readonly reportStage?: (stage: ReleaseDesktopBootstrapStage) => Promise<void> | void;
}

const SIDECAR_BOOTSTRAP_POLL_INTERVAL_MS = 100;
const SIDECAR_BOOTSTRAP_TIMEOUT_MS = 70_000;

type NativeBeginOutcome =
  | { readonly status: "fulfilled" }
  | { readonly status: "rejected"; readonly error: unknown };

async function bootstrapTauriSidecar(): Promise<DesktopBootstrapContextV2> {
  const beginOutcome: { current?: NativeBeginOutcome } = {};
  void invoke<void>("begin_sidecar_start").then(
    () => {
      beginOutcome.current = { status: "fulfilled" };
    },
    (error: unknown) => {
      beginOutcome.current = { status: "rejected", error };
    },
  );
  const deadline = Date.now() + SIDECAR_BOOTSTRAP_TIMEOUT_MS;
  await Promise.resolve();
  for (;;) {
    const begin = beginOutcome.current;
    if (begin?.status === "rejected") throw begin.error;
    try {
      const context = await invoke<DesktopBootstrapContextV2>("sidecar_bootstrap_context");
      const rejected = beginOutcome.current;
      if (rejected?.status === "rejected") throw rejected.error;
      return context;
    } catch (error) {
      const rejected = beginOutcome.current;
      if (rejected?.status === "rejected") throw rejected.error;
      if (!isPendingSidecarBootstrapError(error)) throw error;
    }

    if (Date.now() >= deadline) {
      throw new DesktopContractErrorV2(
        "OpenEvo Desktop timed out waiting for its verified local service bootstrap context",
      );
    }
    await new Promise<void>((resolve) => {
      setTimeout(resolve, SIDECAR_BOOTSTRAP_POLL_INTERVAL_MS);
    });
  }
}

function isPendingSidecarBootstrapError(error: unknown): boolean {
  if (typeof error !== "object" || error === null || !("code" in error)) return false;
  const code = (error as { readonly code?: unknown }).code;
  return code === "sidecar_start_in_progress" || code === "sidecar_state_unavailable";
}

const tauriNativeBridge: ReleaseNativeBridgeV2 = {
  bootstrap: bootstrapTauriSidecar,
  stop: () => invoke("stop_sidecar"),
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
    eventSchemaSha256: DESKTOP_PRODUCT_RELEASE_CONTRACT.acceptedEventSchemaDigests[0],
    releaseVersion: DESKTOP_PRODUCT_RELEASE_CONTRACT.releaseVersion,
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
  dependencies: ReleaseProviderFactoryDependenciesV2 = {},
): Promise<DesktopProductProviderV2> {
  const native = dependencies.native ?? tauriNativeBridge;
  const reportStage = dependencies.reportStage ?? reportReleaseDesktopBootstrapStage;
  let bootstrap: DesktopBootstrapContextV2;
  try {
    bootstrap = validateDesktopBootstrapContextV2(
      await native.bootstrap(),
      DESKTOP_PRODUCT_RELEASE_CONTRACT,
    );
    reportStageBestEffort(reportStage, "bootstrap_context_validated");
  } catch (error) {
    reportStageBestEffort(reportStage, "bootstrap_context_failed");
    throw error;
  }

  const fetch = dependencies.fetch ?? globalThis.fetch.bind(globalThis);
  const client = createDesktopApiClientV2({
    fetch,
    bootstrap: async () => bootstrap,
    contract: DESKTOP_PRODUCT_RELEASE_CONTRACT,
  });

  let version: DesktopVersionV2;
  try {
    version = await client.version();
    reportStageBestEffort(reportStage, "local_api_version_verified");
  } catch (error) {
    reportStageBestEffort(reportStage, "local_api_version_failed");
    throw error;
  }

  const context: ReleaseProviderAdapterContextV2 = {
    client,
    featureFlags: Object.freeze([...version.feature_flags]),
    native: {
      selectProjectSource: (intent) => native.selectProjectSource(intent),
      cancelProjectSource: async (actionId) => {
        await native.cancelProjectSource(actionId);
      },
      settleProjectSource: async (actionId, outcome) => {
        await native.settleProjectSource(actionId, outcome);
      },
    },
  };

  try {
    const provider = dependencies.adapterFactory === undefined
      ? createLocalApiDesktopProductProviderV2({
          client,
          native: context.native,
          featureFlags: context.featureFlags,
          fetch,
        })
      : await dependencies.adapterFactory(context);
    assertReleaseProviderV2(provider);
    reportStageBestEffort(reportStage, "provider_adapter_ready");
    return provider;
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

function assertReleaseProviderV2(
  provider: DesktopProductProviderV2,
): asserts provider is DesktopProductProviderV2 {
  if (provider.apiVersion !== 2 || provider.providerKind !== "desktop_sidecar") {
    throw new DesktopContractErrorV2("Release provider adapter reported a forbidden provider identity");
  }
  const requiredActions: ReadonlyArray<keyof DesktopProductProviderV2> = [
    "refresh",
    "subscribe",
    "rescanSshHosts",
    "createProfile",
    "rebindProfile",
    "connectProfile",
    "disconnectProfile",
    "reviewHostKey",
    "selectNativeWorkspace",
    "createProject",
    "updateProject",
    "activateProject",
    "loadProjectCapabilities",
    "validateProject",
    "submitTask",
    "cancelTask",
    "retryTask",
    "getProjectHead",
    "getEvolutionRevision",
    "getRuntimeContext",
    "retryTransition",
    "replaceTransition",
    "abandonTransition",
    "getArtifactContent",
    "getArtifactDiff",
    "restartService",
    "createDiagnostic",
    "getDiagnostic",
  ];
  if (requiredActions.some((action) => typeof provider[action] !== "function")) {
    throw new DesktopContractErrorV2("Release provider adapter is incomplete");
  }
}
