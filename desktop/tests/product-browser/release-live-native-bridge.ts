export type ReleaseLiveNativeObservationV2 = {
  commands: string[];
  stages: string[];
  rendererReady: boolean;
  unexpected: string[];
};

type ReleaseLiveBootstrapContextV2 = {
  negotiated_contract: {
    openapi_sha256: string;
    event_schema_sha256: string;
    release_version: string;
  };
};

export function installReleaseLiveNativeBridgeV2(
  context: ReleaseLiveBootstrapContextV2,
): void {
  const observation: ReleaseLiveNativeObservationV2 = {
    commands: [],
    stages: [],
    rendererReady: false,
    unexpected: [],
  };
  let mutationIntentJournal: string | null = null;
  Object.defineProperty(window, "__OPENEVO_LIVE_NATIVE_OBSERVATION__", {
    configurable: false,
    enumerable: false,
    writable: false,
    value: observation,
  });
  Object.defineProperty(window, "__TAURI_INTERNALS__", {
    configurable: false,
    enumerable: false,
    writable: false,
    value: {
      invoke: async (command: string, args: Record<string, unknown> = {}) => {
        observation.commands.push(command);
        if (command === "begin_sidecar_start") return null;
        if (command === "sidecar_bootstrap_context") return context;
        if (command === "sidecar_startup_status") {
          return {
            schema_version: "2",
            startup_epoch: 1,
            status: "succeeded",
            phase: "ready",
            phase_index: 5,
            phase_total: 6,
            elapsed_milliseconds: 0,
            cancellable: false,
            failure: null,
          };
        }
        if (command === "stop_sidecar") return null;
        if (command === "read_mutation_intent_journal_v2") return mutationIntentJournal;
        if (command === "compare_and_swap_mutation_intent_journal_v2") {
          const expectedValue = args.expectedValue;
          const newValue = args.newValue;
          if (
            (expectedValue !== null && typeof expectedValue !== "string")
            || (newValue !== null && typeof newValue !== "string")
          ) {
            observation.unexpected.push("mutation_journal_arguments");
            throw new Error("Invalid mutation journal arguments");
          }
          if (expectedValue !== mutationIntentJournal) {
            throw { code: "mutation_intent_journal_conflict" };
          }
          mutationIntentJournal = newValue;
          return null;
        }
        if (command === "renderer_bootstrap_stage") {
          const stage = typeof args.stage === "string" ? args.stage : "";
          const allowed = new Set([
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
          ]);
          if (!allowed.has(stage)) {
            observation.unexpected.push("bootstrap_stage");
            throw new Error("Unexpected renderer bootstrap stage");
          }
          observation.stages.push(stage);
          return null;
        }
        if (command === "renderer_ready") {
          if (
            args.openapiSha256 !== context.negotiated_contract.openapi_sha256
            || args.eventSchemaSha256 !== context.negotiated_contract.event_schema_sha256
            || args.releaseVersion !== context.negotiated_contract.release_version
          ) {
            observation.unexpected.push("renderer_identity");
            throw new Error("Renderer readiness identity mismatch");
          }
          observation.rendererReady = true;
          return null;
        }
        observation.unexpected.push("native_command");
        throw new Error(`Unexpected native command: ${command}`);
      },
    },
  });
}
