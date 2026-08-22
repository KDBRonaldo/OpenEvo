import type { DesktopReleaseContractV2 } from "../api/v2/client";

function nonEmpty<T>(values: readonly T[], label: string): readonly [T, ...T[]] {
  if (values.length === 0) throw new Error(`Desktop release contract requires ${label}.`);
  return Object.freeze([values[0]!, ...values.slice(1)]);
}

// Updated only with reviewed, checked-in v0.1.10 Desktop/Core contract snapshots.
export const DESKTOP_PRODUCT_RELEASE_CONTRACT: DesktopReleaseContractV2 = Object.freeze({
  releaseVersion: "0.1.10",
  acceptedOpenApiDigests: nonEmpty([
    "fe4ac8415f20e584bf0f9b3240d52ec98bc61366d587a09b91d14b4ae29541af",
  ], "a Desktop v2 OpenAPI digest"),
  acceptedEventSchemaDigests: nonEmpty([
    "515b6d90e9ebdf3f5b4f7c4a57a1924dc85011536d9396b1ab3a5dc73fc48b6b",
  ], "a Desktop v2 event schema digest"),
  allowedProviderKinds: Object.freeze(["desktop_sidecar"] as const),
  requiredFeatureFlags: Object.freeze([
    "core_control_v2",
    "daemon_bundle_v2",
    "event_replay_v2",
    "host_key_review",
    "lifecycle_operations_v2",
    "lifecycle_process_logs_v2",
    "mutation_idempotency_v2",
    "native_askpass",
    "system_openssh_profiles",
    "task_admission_v2",
  ]),
});

export const CORE_PRODUCT_RELEASE_CONTRACT = Object.freeze({
  releaseVersion: "0.1.10",
  mutationMajor: 2,
  acceptedOpenApiDigests: nonEmpty([
    "ae677842c735e4b7be673d3e308b76737929c60ac5857d2c4a578d9b372c68fd",
  ], "a Core v2 OpenAPI digest"),
  acceptedEventSchemaDigests: nonEmpty([
    "0e5465c110fd42964f53f076a4bbeed55e899b27f235b5e26c3d478356c5b7d2",
  ], "a Core v2 event schema digest"),
  requiredFeatureFlags: Object.freeze([
    "atomic_successor_v2",
    "event_replay_v2",
    "project_genesis_v2",
    "project_heads_v2",
    "task_admission_v2",
    "task_execution_v2",
    "verified_capabilities",
    "verified_registry",
    "workspace_snapshots_v2",
  ]),
  transport: "active_project_ssh_tunnel",
  requireRegistryIdentity: true,
});
