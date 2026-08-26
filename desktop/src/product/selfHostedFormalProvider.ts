import {
  canonicalJsonV2,
  desktopBootstrapContextV2Schema,
  type DesktopBootstrapContextV2,
} from "../api/v2/schemas";
import {
  createDesktopApiClientV2,
  validateDesktopBootstrapContextV2,
  type DesktopReleaseContractV2,
} from "../api/v2/client";
import { createDevelopmentAgentProvider } from "./developmentAgentProvider";
import {
  createLocalApiDesktopProductProviderV2,
  type LocalApiNativeBridgeV2,
} from "./localApiProviderV2";
import type {
  DesktopProductProviderV2,
  DesktopProductSnapshotV2,
  ProductRefreshResultV2,
  ProductSubscriptionSignalV2,
} from "./providerV2";

const MUTATION_JOURNAL_KEY = "openevo.desktop.self-hosted.mutation-journal.v2";

function developmentContractOf(
  bootstrap: DesktopBootstrapContextV2,
): DesktopReleaseContractV2 {
  const negotiated = bootstrap.negotiated_contract;
  if (negotiated.feature_flags.length === 0) {
    throw new Error("The self-hosted Desktop service reported no formal v2 capabilities.");
  }
  return {
    releaseVersion: negotiated.release_version,
    acceptedOpenApiDigests: [negotiated.openapi_sha256],
    acceptedEventSchemaDigests: [negotiated.event_schema_sha256],
    allowedProviderKinds: ["desktop_sidecar"],
    requiredFeatureFlags: [...negotiated.feature_flags],
  };
}

const browserNativeBridge: LocalApiNativeBridgeV2 = {
  selectProjectSource: async () => {
    throw new Error("Use the project file upload control in the self-hosted browser.");
  },
  cancelProjectSource: async () => {},
  settleProjectSource: async () => {},
  readMutationIntentJournalV2: async () => window.localStorage.getItem(MUTATION_JOURNAL_KEY),
  compareAndSwapMutationIntentJournalV2: async (expectedValue, newValue) => {
    const current = window.localStorage.getItem(MUTATION_JOURNAL_KEY);
    if (current !== expectedValue) {
      throw new Error("The self-hosted Desktop mutation journal changed concurrently.");
    }
    if (newValue === null) window.localStorage.removeItem(MUTATION_JOURNAL_KEY);
    else window.localStorage.setItem(MUTATION_JOURNAL_KEY, newValue);
  },
};

/**
 * Build the first formal walking-skeleton provider.
 *
 * Project/Task authority and mutations use the strict Desktop v2 provider.  The readable
 * transcript projections and standalone Evolution actions stay on the proven development
 * provider until those product contracts exist in Desktop v2. Workspace inventory and file
 * transfer use the authenticated development-only Desktop v2 bridge. This keeps the
 * browser product usable while making the migration boundary explicit and testable.
 */
export async function createSelfHostedFormalProvider(
  bootstrapInput: unknown,
): Promise<DesktopProductProviderV2> {
  const untrusted = desktopBootstrapContextV2Schema.parse(bootstrapInput);
  const contract = developmentContractOf(untrusted);
  const bootstrap = validateDesktopBootstrapContextV2(bootstrapInput, contract);
  const client = createDesktopApiClientV2({
    fetch: globalThis.fetch.bind(globalThis),
    bootstrap: async () => bootstrap,
    contract,
  });
  const version = await client.version();
  const formal = createLocalApiDesktopProductProviderV2({
    client,
    native: browserNativeBridge,
    featureFlags: version.feature_flags,
    providerStreamInstance: version.build_id,
    fetch: globalThis.fetch.bind(globalThis),
  });
  const presentation = createDevelopmentAgentProvider({
    workspaceV2BaseUrl: "/desktop/v2/development/projects",
    artifactV2BaseUrl: "/desktop/v2/development/artifacts",
    evolutionV2BaseUrl: "/desktop/v2/development/evolution-runs",
    evolutionJobV2BaseUrl: "/desktop/v2/development/evolution-jobs",
    taskPresentationV2BaseUrl: "/desktop/v2/development/task-presentations",
    presentationV2BaseUrl: "/desktop/v2/development",
    desktopSessionToken: bootstrap.session_token,
  });
  return combineSelfHostedProviders(formal, presentation);
}

/** Exported for a focused routing test; production calls createSelfHostedFormalProvider. */
export function combineSelfHostedProviders(
  formal: DesktopProductProviderV2,
  presentation: DesktopProductProviderV2,
): DesktopProductProviderV2 {
  const featureFlags = Object.freeze([
    ...new Set([...formal.featureFlags, ...presentation.featureFlags]),
  ]);
  let presentationAuthorityKey: string | null = null;
  let cachedPresentation: DesktopProductSnapshotV2["runtimePresentation"];
  let presentationLoaded = false;
  let presentationDirty = true;
  const refresh = async (): Promise<ProductRefreshResultV2> => {
    // Preserve the original parallel first load; only later refreshes can make
    // the cache decision from the newly observed formal authority.
    const initialResults = !presentationLoaded
      ? await Promise.all([formal.refresh(), presentation.refresh()] as const)
      : null;
    const formalResult = initialResults?.[0] ?? await formal.refresh();
    if (formalResult.status !== "fresh") return formalResult;
    // Active-Project selection is deliberately absent from this key. The
    // formal snapshot already contains every Project's Task/artifact authority,
    // so changing only active_project_id can reuse the complete presentation
    // read model loaded by the previous refresh.
    const nextPresentationAuthorityKey = canonicalJsonV2({
      projects: formalResult.snapshot.projects,
      tasks: formalResult.snapshot.tasks,
      artifacts: formalResult.snapshot.artifacts,
    });
    if (
      presentationDirty
      || !presentationLoaded
      || presentationAuthorityKey !== nextPresentationAuthorityKey
    ) {
      const presentationResult = initialResults?.[1] ?? await presentation.refresh();
      if (presentationResult.status !== "fresh") return presentationResult;
      cachedPresentation = presentationResult.snapshot.runtimePresentation;
      presentationAuthorityKey = nextPresentationAuthorityKey;
      presentationLoaded = true;
      presentationDirty = false;
    }
    const compatibilityPresentation = cachedPresentation;
    const snapshot: DesktopProductSnapshotV2 = {
      ...formalResult.snapshot,
      runtimePresentation: compatibilityPresentation === undefined ? undefined : {
        ...compatibilityPresentation,
      },
    };
    return { status: "fresh", snapshot };
  };

  const subscribe = (listener: (signal: ProductSubscriptionSignalV2) => void) => {
    const stopFormal = formal.subscribe(listener);
    const stopPresentation = presentation.subscribe((signal) => {
      presentationDirty = true;
      listener(signal);
    });
    return () => {
      stopFormal();
      stopPresentation();
    };
  };

  return new Proxy(formal, {
    get(target, property, receiver) {
      if (property === "featureFlags") return featureFlags;
      if (property === "refresh") return refresh;
      if (property === "subscribe") return subscribe;
      if (["retryEvolutionJob", "startEvolutionRun", "applyEvolutionRun", "uploadWorkspaceFile"].includes(String(property))) {
        const mutation = Reflect.get(presentation, property);
        if (typeof mutation !== "function") return mutation;
        return async (...args: unknown[]) => {
          const result = await mutation.apply(presentation, args);
          presentationDirty = true;
          return result;
        };
      }
      if (property === "downloadWorkspaceFile") return presentation.downloadWorkspaceFile?.bind(presentation);
      const value = Reflect.get(target, property, receiver);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}
