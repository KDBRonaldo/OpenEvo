import { invoke } from "@tauri-apps/api/core";
import type { DesktopApiClientV1, FetchLike } from "../api/v1/client";
import { createDesktopApiClient, DesktopContractError } from "../api/v1/client";
import {
  projectSourceV1Schema,
  remoteProfileV1Schema,
  type DesktopBootstrapContextV1,
  type ProjectSourceV1,
  type RemoteProfileV1,
} from "../api/v1/schemas";
import { createLocalApiDesktopProductProvider } from "./localApiProvider";
import type { DesktopProductProvider, ProjectSourceSelectionIntent } from "./provider";
import { DESKTOP_PRODUCT_RELEASE_CONTRACT } from "./releaseContract";

export interface ReleaseNativeBridge {
  bootstrap(): Promise<unknown>;
  selectProjectSource(intent: ProjectSourceSelectionIntent): Promise<unknown>;
  settleProjectSource(actionId: string, outcome: "adopt" | "discard"): Promise<unknown>;
  configureCredential(
    profileId: string,
    slotKind: RemoteProfileV1["credential_slots"][number]["kind"],
    etag: string,
    actionId: string,
  ): Promise<unknown>;
}

export interface ReleaseProviderAdapterContext {
  readonly client: DesktopApiClientV1;
  readonly native: {
    selectProjectSource(intent: ProjectSourceSelectionIntent): Promise<ProjectSourceV1>;
    settleProjectSource(actionId: string, outcome: "adopt" | "discard"): Promise<void>;
    configureCredential(
      profileId: string,
      slotKind: RemoteProfileV1["credential_slots"][number]["kind"],
      etag: string,
      actionId: string,
    ): Promise<RemoteProfileV1>;
  };
}

export interface ReleaseProviderFactoryDependencies {
  readonly fetch?: FetchLike;
  readonly native?: ReleaseNativeBridge;
  readonly adapterFactory?: (context: ReleaseProviderAdapterContext) => Promise<DesktopProductProvider> | DesktopProductProvider;
}

const tauriNativeBridge: ReleaseNativeBridge = {
  bootstrap: () => invoke<DesktopBootstrapContextV1>("start_sidecar"),
  selectProjectSource: (intent) => invoke("select_project_source", {
    kind: intent.kind,
    actionId: intent.actionId,
    ...(intent.projectId === undefined ? {} : { projectId: intent.projectId }),
  }),
  settleProjectSource: (actionId, outcome) => invoke("settle_project_source", {
    actionId,
    outcome,
  }),
  configureCredential: (profileId, slotKind, etag, actionId) => invoke("configure_credential", {
    profileId,
    slotKind,
    etag,
    actionId,
  }),
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
      settleProjectSource: async (actionId, outcome) => {
        await native.settleProjectSource(actionId, outcome);
      },
      configureCredential: async (profileId, slotKind, etag, actionId) => {
        const profile = remoteProfileV1Schema.parse(
          await native.configureCredential(profileId, slotKind, etag, actionId),
        );
        if (profile.profile_id !== profileId
          || !profile.credential_slots.some((slot) => slot.kind === slotKind)) {
          throw new DesktopContractError("Native credential response does not match the requested profile slot");
        }
        return profile;
      },
    },
  };
  const provider = dependencies.adapterFactory
    ? await dependencies.adapterFactory(context)
    : createLocalApiDesktopProductProvider({ client, native: context.native, fetch: dependencies.fetch });
  if (provider.providerKind !== "desktop_sidecar") {
    throw new DesktopContractError("Release provider adapter reported a forbidden provider kind");
  }
  return provider;
}
