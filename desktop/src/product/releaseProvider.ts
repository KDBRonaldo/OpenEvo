import { invoke } from "@tauri-apps/api/core";
import type { DesktopApiClientV1, FetchLike } from "../api/v1/client";
import { createDesktopApiClient, DesktopContractError } from "../api/v1/client";
import { projectSourceV1Schema, type DesktopBootstrapContextV1, type ProjectSourceV1 } from "../api/v1/schemas";
import type { DesktopProductProvider, ProjectSourceSelectionIntent } from "./provider";
import { DesktopProductProviderUnavailableError } from "./provider";
import { DESKTOP_PRODUCT_RELEASE_CONTRACT } from "./releaseContract";

export interface ReleaseNativeBridge {
  bootstrap(): Promise<unknown>;
  selectProjectSource(intent: ProjectSourceSelectionIntent): Promise<unknown>;
}

export interface ReleaseProviderAdapterContext {
  readonly client: DesktopApiClientV1;
  readonly native: {
    selectProjectSource(intent: ProjectSourceSelectionIntent): Promise<ProjectSourceV1>;
  };
}

export interface ReleaseProviderFactoryDependencies {
  readonly fetch?: FetchLike;
  readonly native?: ReleaseNativeBridge;
  readonly adapterFactory?: (context: ReleaseProviderAdapterContext) => Promise<DesktopProductProvider> | DesktopProductProvider;
}

const tauriNativeBridge: ReleaseNativeBridge = {
  bootstrap: () => invoke<DesktopBootstrapContextV1>("start_sidecar"),
  selectProjectSource: (intent) => invoke("select_project_source", { kind: intent.kind, actionId: intent.actionId }),
};

export async function createReleaseDesktopProductProvider(
  dependencies: ReleaseProviderFactoryDependencies = {},
): Promise<DesktopProductProvider> {
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
  if (!dependencies.adapterFactory) {
    throw new DesktopProductProviderUnavailableError();
  }

  const provider = await dependencies.adapterFactory({
    client,
    native: {
      selectProjectSource: async (intent) => projectSourceV1Schema.parse(await native.selectProjectSource(intent)),
    },
  });
  if (provider.providerKind !== "desktop_sidecar") {
    throw new DesktopContractError("Release provider adapter reported a forbidden provider kind");
  }
  return provider;
}
