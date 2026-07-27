import React from "react";
import ReactDOM from "react-dom/client";
import "../styles.css";
import type { LifecycleOperationV2, OperationV2 } from "../api/v2/schemas";
import { DesktopProductAppV2 } from "./DesktopProductAppV2";
import type { LifecycleOperationStateV2 } from "./lifecycleOperationsV2";
import {
  unavailableDesktopProductProviderV2,
  type DesktopProductProviderV2,
  type DesktopProductSnapshotV2,
  type ProductSubscriptionSignalV2,
} from "./providerV2";

if (!import.meta.env.DEV) {
  throw new Error("The lifecycle preview is available only from the Vite development server.");
}

const DIGEST = "a".repeat(64);
const ETAG = `"${"b".repeat(64)}"`;
const started = new Date(Date.now() - 16_000).toISOString();

function runningOperation(): LifecycleOperationV2 {
  return {
    schema_version: "2",
    operation_id: "lifecycle-preview-connect-1",
    kind: "profile_connect",
    resource: { resource_kind: "profile", resource_id: "profile-preview-lab" },
    request_sha256: DIGEST,
    status: "running",
    phase: "transferring",
    phase_index: 6,
    phase_total: 17,
    progress: { kind: "bytes", completed: 8_388_608, total: 33_554_432 },
    cancellable: true,
    result: null,
    failure: null,
    log_sequence_high_watermark: 62,
    created_at: started,
    started_at: started,
    updated_at: started,
    finished_at: null,
    etag: ETAG,
  };
}

function logEntry(sequence: number) {
  const source = sequence % 3 === 0
    ? "daemon_stderr" as const
    : sequence % 2 === 0 ? "daemon_stdout" as const : "ssh_stdout" as const;
  const text = sequence === 62
    ? `registry-chunk-${"x".repeat(260)}`
    : sequence % 3 === 0
      ? `Daemon readiness probe ${sequence} is still warming the verified registry`
      : `SSH transfer block ${sequence} accepted by the remote workspace`;
  return {
    schema_version: "2" as const,
    operation_id: "lifecycle-preview-connect-1",
    sequence,
    occurred_at: started,
    source,
    text,
    truncated: sequence === 62,
  };
}

function lifecycleState(operation = runningOperation(), older = false): LifecycleOperationStateV2 {
  return {
    operation,
    logs: older
      ? Object.freeze(Array.from({ length: 12 }, (_, index) => logEntry(index + 1)))
      : Object.freeze(Array.from({ length: 12 }, (_, index) => logEntry(index + 51))),
    droppedBeforeSequence: 0,
    hasOlderLogs: !older,
    hasNewerLogs: older,
  };
}

const coreOperation: OperationV2 = {
  schema_version: "2",
  operation_id: "core-preview-cache-cleanup-1",
  kind: "cache_cleanup",
  status: "running",
  progress_completed: 0,
  progress_total: 0,
  error: null,
  created_at: started,
  updated_at: started,
  etag: ETAG,
};

const snapshot: DesktopProductSnapshotV2 = {
  state: {
    schema_version: "2",
    profiles: [],
    active_profile_id: null,
    active_project_id: null,
    pending_operations: [],
    last_event_id: null,
    updated_at: started,
  },
  catalog: {
    schema_version: "2",
    catalog_generation: 1,
    hosts: [],
    warnings: [],
    scanned_at: started,
  },
  profiles: [],
  projects: [],
  tasks: [],
  transitions: {},
  timelines: {},
  artifacts: [],
  services: [],
  capability: null,
  validation: null,
  activeOperation: null,
  stream: { status: "fresh", epoch: 1, lastEventId: null },
};

function createLifecyclePreviewProvider(): DesktopProductProviderV2 {
  let state = lifecycleState();
  const listeners = new Set<(signal: ProductSubscriptionSignalV2) => void>();
  const emit = () => {
    for (const listener of listeners) listener({ kind: "snapshot_changed" });
  };
  return {
    ...unavailableDesktopProductProviderV2,
    featureFlags: ["lifecycle_operations_v2", "lifecycle_process_logs_v2"],
    refresh: async () => ({ status: "fresh", snapshot }),
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    listLifecycleOperations: () => [state],
    listCoreOperations: () => [coreOperation],
    listMutationIntents: () => [{
      action_id: "lifecycle-preview-action-0001",
      mutation_kind: "profile_connect",
      resource_scope: "profile:profile-preview-lab",
      request_sha256: DIGEST,
      authority_sha256: DIGEST,
      provider_stream_instance: "lifecycle-preview-instance",
      provider_stream_epoch: 1,
      chain_step: "single",
      accepted_operation_id: state.operation.operation_id,
      completed_operation_ids: [],
      state: "accepted",
      created_at: started,
      updated_at: started,
    }],
    loadOlderLifecycleLogs: async () => {
      state = lifecycleState(state.operation, true);
      emit();
      return state;
    },
    loadLatestLifecycleLogs: async () => {
      state = lifecycleState(state.operation, false);
      emit();
      return state;
    },
    cancelLifecycleOperation: async () => {
      const cancelled: LifecycleOperationV2 = {
        ...state.operation,
        status: "cancelled",
        phase: "finalizing",
        phase_index: 16,
        progress: null,
        cancellable: false,
        updated_at: new Date().toISOString(),
        finished_at: new Date().toISOString(),
        etag: `"${"c".repeat(64)}"`,
      };
      state = lifecycleState(cancelled, false);
      emit();
      return cancelled;
    },
    resumeMutationIntent: async () => undefined,
  };
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <DesktopProductAppV2 provider={createLifecyclePreviewProvider()} />
  </React.StrictMode>,
);
