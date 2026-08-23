import {
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
  const refresh = async (): Promise<ProductRefreshResultV2> => {
    const [formalResult, presentationResult] = await Promise.all([
      formal.refresh(),
      presentation.refresh(),
    ]);
    if (formalResult.status !== "fresh") return formalResult;
    if (presentationResult.status !== "fresh") return presentationResult;
    const compatibilityPresentation = presentationResult.snapshot.runtimePresentation;
    const formalTaskIds = new Set(formalResult.snapshot.tasks.map((task) => task.task_id));
    const tasks = Object.fromEntries(Object.entries(compatibilityPresentation?.tasks ?? {}).map(
      ([taskId, task]) => [taskId, formalTaskIds.has(taskId) ? { ...task, transcript: [] } : task],
    ));
    const snapshot: DesktopProductSnapshotV2 = {
      ...formalResult.snapshot,
      runtimePresentation: compatibilityPresentation === undefined ? undefined : {
        ...compatibilityPresentation,
        tasks,
      },
    };
    return { status: "fresh", snapshot };
  };

  const subscribe = (listener: (signal: ProductSubscriptionSignalV2) => void) => {
    const stopFormal = formal.subscribe(listener);
    const stopPresentation = presentation.subscribe(listener);
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
      if (property === "retryEvolutionJob") return presentation.retryEvolutionJob?.bind(presentation);
      if (property === "startEvolutionRun") return presentation.startEvolutionRun?.bind(presentation);
      if (property === "applyEvolutionRun") return presentation.applyEvolutionRun?.bind(presentation);
      if (property === "uploadWorkspaceFile") return presentation.uploadWorkspaceFile?.bind(presentation);
      if (property === "downloadWorkspaceFile") return presentation.downloadWorkspaceFile?.bind(presentation);
      const value = Reflect.get(target, property, receiver);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}
