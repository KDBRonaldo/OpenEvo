import { z } from "zod";

export const MAX_JAVASCRIPT_SAFE_INTEGER_V2 = Number.MAX_SAFE_INTEGER;
export const MAX_PROFILE_COUNT_V2 = 100;
export const MAX_HOST_HINTS_V2 = 512;
export const MAX_CATALOG_WARNINGS_V2 = 64;
export const MAX_PAGE_SIZE_V2 = 100;
export const MAX_PROJECT_CONFIG_BYTES_V2 = 1_048_576;
export const MAX_JSON_DEPTH_V2 = 24;
export const MAX_JSON_NODES_V2 = 8_192;
export const MAX_JSON_COLLECTION_ITEMS_V2 = 1_024;
export const MAX_JSON_TEXT_BYTES_V2 = 524_288;

const UTC_RFC3339 = /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z$/;
const UTC_RFC3339_COMPONENTS = /^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?Z$/;
const OPAQUE_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const STRONG_ETAG = /^"[0-9a-f]{64}"$/;
const MIME_TYPE = /^[a-z0-9][a-z0-9!#$&^_.+-]*\/[a-z0-9][a-z0-9!#$&^_.+-]*$/;
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f]/;
const UNSAFE_MULTILINE_CONTROL_CHARACTERS = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/;

export const schemaVersionV2Schema = z.literal("2").default("2");
export const opaqueIdV2Schema = z.string().min(1).max(128).regex(OPAQUE_ID);
export const sshHostAliasV2Schema = opaqueIdV2Schema;
export const sha256DigestV2Schema = z.string().regex(SHA256);
export const etagV2Schema = z.string().length(66).regex(STRONG_ETAG);
export const utcTimestampV2Schema = z.string().regex(UTC_RFC3339)
  .refine((value) => canonicalUtcTimestampV2(value) !== null, "invalid UTC timestamp");
export const cursorV2Schema = z.string().min(1).max(512);
export const displayNameV2Schema = z.string().min(1).max(128).refine(noControlCharacters);
export const safeSummaryV2Schema = z.string().min(1).max(512).refine(noControlCharacters);
export const descriptionV2Schema = z.string().min(1).max(4_096).refine(noControlCharacters);
export const safeIntegerV2Schema = z.number().int().safe();
export const nonNegativeSafeIntegerV2Schema = safeIntegerV2Schema.min(0);
export const positiveSafeIntegerV2Schema = safeIntegerV2Schema.min(1);
export const mimeTypeV2Schema = z.string().min(3).max(127).regex(MIME_TYPE);

export type SafeJsonValueV2 = null | boolean | number | string | SafeJsonValueV2[] | {
  [key: string]: SafeJsonValueV2;
};

const safeJsonScalarV2Schema = z.union([
  z.null(),
  z.boolean(),
  z.number().finite().refine((value) => !Number.isInteger(value) || Number.isSafeInteger(value)),
  z.string(),
]);

export const safeJsonValueV2Schema: z.ZodType<SafeJsonValueV2> = z.lazy(() =>
  z.union([
    safeJsonScalarV2Schema,
    z.array(safeJsonValueV2Schema).max(MAX_JSON_COLLECTION_ITEMS_V2),
    z.record(z.string(), safeJsonValueV2Schema),
  ]),
);

export const safeJsonObjectV2Schema: z.ZodType<Record<string, SafeJsonValueV2>> = z
  .record(z.string(), safeJsonValueV2Schema)
  .superRefine(validateBoundedJsonV2);

export const desktopActionV2Schema = z.enum([
  "retry",
  "rescan",
  "review_host_key",
  "rebind",
  "reconnect",
  "install_repair_daemon",
  "administrator_action",
  "correct_project",
  "wait_for_successor",
  "none",
]);

export const desktopErrorV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  code: z.string().min(1).max(128).regex(/^[a-z][a-z0-9_]*$/),
  summary: safeSummaryV2Schema,
  retryable: z.boolean(),
  action: desktopActionV2Schema,
  affected_resource_id: opaqueIdV2Schema.nullable(),
}).strict();

export const apiErrorV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  request_id: opaqueIdV2Schema,
  code: z.string().min(1).max(128).regex(/^[a-z][a-z0-9_]*$/),
  http_status: z.number().int().min(400).max(599),
  message: descriptionV2Schema,
  category: z.enum([
    "system",
    "project",
    "task",
    "transition",
    "artifact",
    "service",
    "authentication",
    "contract",
    "internal",
  ]),
  retryable: z.boolean(),
  repair_action: z.enum(["retry", "repair", "reconfigure", "user_action_required", "unsupported"]),
  next_action: descriptionV2Schema,
}).strict();

export const contractOnlyResponseV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  code: z.literal("contract_only_not_implemented"),
  message: safeSummaryV2Schema,
}).strict();

export const executionModeV2Schema = z.enum(["codex_subscription_transcript", "self-deployed"]);
export const captureModeV2Schema = z.enum(["transcript", "proxy"]);
export const transitionKindV2Schema = z.enum([
  "run_result",
  "settings",
  "context_rebind",
  "historical_restore",
  "evolution_abandon",
]);

// Evolution capabilities retain their framework v1 schema version while the
// transport envelope is Desktop Local API v2. They are defined here directly;
// the v2 renderer never imports the legacy Desktop API models.
const frameworkExecutionModeSchema = z.enum(["subscription", "self_deployed"]);
const frameworkCaptureModeSchema = z.enum(["transcript", "token_level"]);
const supportStateSchema = z.enum(["supported", "unsupported", "unavailable"]);
const frameworkTextSchema = z.string().max(4_096).refine(noControlCharacters);
const frameworkIdSchema = z.string().min(1).max(256).refine(noControlCharacters);
const frameworkStringArraySchema = z.array(z.string().max(4_096).refine(noControlCharacters)).max(256);

export const axisSupportV2Schema = z.object({
  state: supportStateSchema,
  message: frameworkTextSchema,
  reason_code: z.string().max(256).refine(noControlCharacters).nullable().default(null),
  missing_requirements: frameworkStringArraySchema.default([]),
}).strict();

export const methodSupportV2Schema = z.object({
  overall: supportStateSchema,
  execution: axisSupportV2Schema,
  capture: axisSupportV2Schema,
  harness: axisSupportV2Schema,
  runtime: axisSupportV2Schema,
}).strict();

export const methodInputBindingV2Schema = z.object({
  binding_id: frameworkIdSchema,
  source: z.enum(["current_dataset", "history_datasets", "current_target_artifacts", "explicit_inputs"]),
  artifact_type: frameworkIdSchema,
  min_count: nonNegativeSafeIntegerV2Schema.default(0),
  max_count: positiveSafeIntegerV2Schema.nullable().default(null),
}).strict().superRefine((value, context) => {
  if (value.max_count !== null && value.min_count > value.max_count) {
    issue(context, ["max_count"], "maximum input count must not be less than minimum input count");
  }
});

const canonicalJsonObjectStringV2Schema = z.string()
  .refine((value) => utf8ByteLength(value) <= MAX_PROJECT_CONFIG_BYTES_V2)
  .refine(isCanonicalBoundedJsonObject, "must contain a canonical bounded JSON object");

export const evolutionMethodCapabilityV2Schema = z.object({
  method_id: frameworkIdSchema,
  display_name: frameworkTextSchema,
  description: frameworkTextSchema,
  exposure: z.enum(["desktop", "maintainer", "internal"]),
  maturity: z.enum(["stable", "experimental"]),
  execution_modes: z.array(frameworkExecutionModeSchema).max(2),
  capture_modes: z.array(frameworkCaptureModeSchema).max(2),
  supported_harness_ids: frameworkStringArraySchema,
  harness_requirements: frameworkStringArraySchema,
  runtime_requirements: frameworkStringArraySchema,
  input_bindings: z.array(methodInputBindingV2Schema).max(256),
  output_artifact_types: z.array(frameworkIdSchema).max(256),
  config_schema_json: canonicalJsonObjectStringV2Schema,
  default_config_json: canonicalJsonObjectStringV2Schema,
  implementation_identity_digest: sha256DigestV2Schema,
  support: methodSupportV2Schema,
}).strict();

export const evolutionResolvedMethodCapabilityV2Schema = z.object({
  method_id: frameworkIdSchema,
  implementation_identity_digest: sha256DigestV2Schema,
  support: methodSupportV2Schema,
}).strict();

export const evolutionSelectionResolverCapabilityV2Schema = z.object({
  selection_value: frameworkIdSchema,
  display_name: frameworkTextSchema,
  description: frameworkTextSchema,
  resolved_methods: z.array(evolutionResolvedMethodCapabilityV2Schema).max(256),
}).strict();

export const evolutionTargetCapabilityV2Schema = z.object({
  target_id: frameworkIdSchema,
  display_name: frameworkTextSchema,
  description: frameworkTextSchema,
  artifact_type: frameworkIdSchema,
  exposure: z.enum(["desktop", "maintainer", "internal"]),
  maturity: z.enum(["stable", "experimental"]),
  handler_id: frameworkIdSchema,
  configured_default_method_id: frameworkIdSchema,
  effective_default_method_id: frameworkIdSchema.nullable(),
  configured_default_support: methodSupportV2Schema,
  renderer_kind: z.enum(["markdown", "file_bundle", "structured_summary", "adapter"]),
  renderer_contract_version: frameworkIdSchema,
  contribution_contract_version: frameworkIdSchema,
  context_order: z.number().int().min(0).max(10_000),
  implementation_identity_digest: sha256DigestV2Schema,
  handler_identity_digest: sha256DigestV2Schema,
  accepted_methods: z.array(evolutionResolvedMethodCapabilityV2Schema).max(256),
  selection_resolvers: z.array(evolutionSelectionResolverCapabilityV2Schema).max(64),
  methods: z.array(evolutionMethodCapabilityV2Schema).max(256),
}).strict().superRefine((value, context) => {
  uniqueBy(value.accepted_methods, (item) => item.method_id, context, ["accepted_methods"]);
  uniqueBy(value.methods, (item) => item.method_id, context, ["methods"]);
  uniqueBy(value.selection_resolvers, (item) => item.selection_value, context, ["selection_resolvers"]);
  const accepted = new Map(value.accepted_methods.map((method) => [method.method_id, method]));
  for (const [resolverIndex, resolver] of value.selection_resolvers.entries()) {
    for (const [methodIndex, resolved] of resolver.resolved_methods.entries()) {
      const authority = accepted.get(resolved.method_id);
      if (authority === undefined || canonicalJsonV2(authority) !== canonicalJsonV2(resolved)) {
        issue(context, ["selection_resolvers", resolverIndex, "resolved_methods", methodIndex], "resolver method differs from accepted method authority");
      }
    }
  }
});

export const evolutionExecutionProfileV2Schema = z.object({
  execution_mode: frameworkExecutionModeSchema,
  capture_mode: frameworkCaptureModeSchema,
  harness_id: frameworkIdSchema,
  harness_capabilities: frameworkStringArraySchema.default([]),
  runtime_capabilities: frameworkStringArraySchema.default([]),
}).strict();

export const evolutionCapabilitiesV2Schema = z.object({
  schema_version: z.literal("1").default("1"),
  core_version: frameworkTextSchema,
  registry_digest: sha256DigestV2Schema,
  evaluated_profile: evolutionExecutionProfileV2Schema,
  targets: z.array(evolutionTargetCapabilityV2Schema).max(128),
}).strict().superRefine((value, context) => {
  uniqueBy(value.targets, (target) => target.target_id, context, ["targets"]);
});

export const projectEvolutionTargetSelectionV2Schema = z.object({
  enabled: z.boolean(),
  method: frameworkIdSchema.nullable().default(null),
  config: safeJsonObjectV2Schema.default({}),
}).strict();

export const scienceEvolutionConfigV2Schema = z.object({
  targets: z.record(frameworkIdSchema, projectEvolutionTargetSelectionV2Schema),
}).strict().superRefine((value, context) => {
  if (Object.keys(value.targets).length > 128) issue(context, ["targets"], "evolution target map exceeds its item limit");
});

export const scienceTaskConfigV2Schema = z.object({
  title: z.string().min(1).max(256).refine(noControlCharacters),
  objective: z.string().min(1).max(65_536).refine(noUnsafeMultilineControlCharacters),
}).strict();

export const scienceWorkspaceSourceV2Schema = z.object({
  kind: z.enum(["scratch", "native_folder_snapshot"]),
  display_name: z.string().min(1).max(256).refine(noControlCharacters),
}).strict();

export const codexSubscriptionExecutionSettingsV2Schema = z.object({
  mode: z.literal("codex_subscription_transcript"),
  capture_mode: z.literal("transcript"),
  token_level_metrics_available: z.literal(false),
  harness_id: z.literal("codex"),
  codex_model: z.string().min(1).max(256).refine(isSafeModelReference, "model must not be a path or URI"),
  reasoning_effort: z.enum(["low", "medium", "high", "xhigh"]).nullable(),
  token_limit: positiveSafeIntegerV2Schema,
  task_network_allow_internet: z.boolean(),
}).strict();

export const selfDeployedExecutionSettingsV2Schema = z.object({
  mode: z.literal("self-deployed"),
  capture_mode: z.literal("transcript"),
  token_level_metrics_available: z.literal(false),
  harness_id: z.literal("codex"),
  model_profile_id: z.literal("qwen3-0.6b-v1"),
  token_limit: positiveSafeIntegerV2Schema.max(8_192),
  task_network_allow_internet: z.boolean(),
}).strict();

export const scienceExecutionSettingsV2Schema = z.discriminatedUnion("mode", [
  codexSubscriptionExecutionSettingsV2Schema,
  selfDeployedExecutionSettingsV2Schema,
]);

export const scienceProjectConfigV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  task: scienceTaskConfigV2Schema,
  workspace: scienceWorkspaceSourceV2Schema,
  execution: scienceExecutionSettingsV2Schema,
  evolution: scienceEvolutionConfigV2Schema,
}).strict().superRefine((value, context) => {
  if (utf8ByteLength(canonicalJsonV2(value)) > MAX_PROJECT_CONFIG_BYTES_V2) {
    issue(context, [], "project config exceeds the canonical byte limit");
  }
});

export const workspaceSnapshotRefV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  workspace_snapshot_id: opaqueIdV2Schema,
  project_id: opaqueIdV2Schema,
  manifest_sha256: sha256DigestV2Schema,
  entry_count: z.number().int().safe().min(0).max(100_000),
  byte_size: z.number().int().safe().min(0).max(16 * 1024 * 1024 * 1024),
}).strict();

export const evolutionRevisionRefV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  evolution_revision_id: opaqueIdV2Schema,
  project_id: opaqueIdV2Schema,
  manifest_sha256: sha256DigestV2Schema,
  artifact_count: z.number().int().min(0).max(128),
}).strict();

export const runtimeContextSnapshotRefV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  runtime_context_snapshot_id: opaqueIdV2Schema,
  project_id: opaqueIdV2Schema,
  evolution_revision_id: opaqueIdV2Schema,
  evolution_revision_manifest_sha256: sha256DigestV2Schema,
  registry_sha256: sha256DigestV2Schema,
  runtime_contract_sha256: sha256DigestV2Schema,
  manifest_sha256: sha256DigestV2Schema,
}).strict();

export const effectiveExecutionSnapshotRefV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  effective_execution_snapshot_id: opaqueIdV2Schema,
  project_id: opaqueIdV2Schema,
  execution_mode: executionModeV2Schema,
  capture_mode: captureModeV2Schema,
  token_level_metrics_available: z.boolean(),
  producer_id: opaqueIdV2Schema,
  snapshot_sha256: sha256DigestV2Schema,
}).strict().superRefine((value, context) => {
  if (value.execution_mode === "codex_subscription_transcript" && value.capture_mode !== "transcript") {
    issue(context, ["capture_mode"], "subscription execution requires transcript capture");
  }
  if (value.capture_mode === "transcript" && value.token_level_metrics_available) {
    issue(context, ["token_level_metrics_available"], "transcript capture cannot expose token-level metrics");
  }
});

export const projectHeadRefV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  project_head_id: opaqueIdV2Schema,
  project_id: opaqueIdV2Schema,
  generation: nonNegativeSafeIntegerV2Schema,
  predecessor_project_head_id: opaqueIdV2Schema.nullable(),
  workspace_snapshot: workspaceSnapshotRefV2Schema,
  evolution_revision: evolutionRevisionRefV2Schema,
  runtime_context_snapshot: runtimeContextSnapshotRefV2Schema,
  effective_execution_snapshot: effectiveExecutionSnapshotRefV2Schema,
  registry_sha256: sha256DigestV2Schema,
  manifest_sha256: sha256DigestV2Schema,
}).strict().superRefine((value, context) => {
  if ((value.generation === 0) !== (value.predecessor_project_head_id === null)) {
    issue(context, ["predecessor_project_head_id"], "only generation zero may omit a predecessor project head");
  }
  if (value.predecessor_project_head_id === value.project_head_id) {
    issue(context, ["predecessor_project_head_id"], "a project head cannot be its own predecessor");
  }
  for (const [path, projectId] of [
    ["workspace_snapshot", value.workspace_snapshot.project_id],
    ["evolution_revision", value.evolution_revision.project_id],
    ["runtime_context_snapshot", value.runtime_context_snapshot.project_id],
    ["effective_execution_snapshot", value.effective_execution_snapshot.project_id],
  ] as const) {
    if (projectId !== value.project_id) issue(context, [path, "project_id"], `${path} belongs to another project`);
  }
  if (value.runtime_context_snapshot.evolution_revision_id !== value.evolution_revision.evolution_revision_id
    || value.runtime_context_snapshot.evolution_revision_manifest_sha256 !== value.evolution_revision.manifest_sha256) {
    issue(context, ["runtime_context_snapshot", "evolution_revision_id"], "runtime context binds another evolution revision");
  }
  if (value.runtime_context_snapshot.registry_sha256 !== value.registry_sha256) {
    issue(context, ["runtime_context_snapshot", "registry_sha256"], "runtime context and project head registry digests differ");
  }
});

export const taskAdmissionRefV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  task_admission_id: opaqueIdV2Schema,
  task_id: opaqueIdV2Schema,
  project_id: opaqueIdV2Schema,
  predecessor_project_head: projectHeadRefV2Schema,
  workspace_snapshot: workspaceSnapshotRefV2Schema,
  project_config_sha256: sha256DigestV2Schema,
  task_envelope_sha256: sha256DigestV2Schema,
  normalized_evolution_intent_sha256: sha256DigestV2Schema,
  registry_sha256: sha256DigestV2Schema,
  admission_sha256: sha256DigestV2Schema,
  admitted_at: utcTimestampV2Schema,
}).strict().superRefine((value, context) => {
  if (value.predecessor_project_head.project_id !== value.project_id) {
    issue(context, ["predecessor_project_head", "project_id"], "predecessor project head belongs to another project");
  }
  if (value.workspace_snapshot.project_id !== value.project_id) {
    issue(context, ["workspace_snapshot", "project_id"], "workspace snapshot belongs to another project");
  }
  if (value.registry_sha256 !== value.predecessor_project_head.registry_sha256) {
    issue(context, ["registry_sha256"], "admission registry digest differs from the predecessor head");
  }
  if (value.admission_sha256 !== taskAdmissionSha256ForV2(value)) {
    issue(context, ["admission_sha256"], "task admission digest does not match its immutable pins");
  }
});

export const attemptRefV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  attempt_id: opaqueIdV2Schema,
  ordinal: z.number().int().min(1).max(100),
  task_id: opaqueIdV2Schema,
  task_admission_id: opaqueIdV2Schema,
  admission_sha256: sha256DigestV2Schema,
  project_id: opaqueIdV2Schema,
  predecessor_project_head_id: opaqueIdV2Schema,
  created_at: utcTimestampV2Schema,
}).strict();

export const successorTransitionRefV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  successor_transition_id: opaqueIdV2Schema,
  project_id: opaqueIdV2Schema,
  kind: transitionKindV2Schema,
  predecessor_project_head: projectHeadRefV2Schema,
  expected_successor_generation: positiveSafeIntegerV2Schema,
  plan_sha256: sha256DigestV2Schema,
  task_admission: taskAdmissionRefV2Schema.nullable(),
  accepted_attempt: attemptRefV2Schema.nullable(),
  successor_project_head: projectHeadRefV2Schema.nullable(),
}).strict().superRefine((value, context) => {
  const predecessor = value.predecessor_project_head;
  if (predecessor.project_id !== value.project_id) {
    issue(context, ["predecessor_project_head", "project_id"], "predecessor project head belongs to another project");
  }
  if (value.expected_successor_generation !== predecessor.generation + 1) {
    issue(context, ["expected_successor_generation"], "expected successor generation must be adjacent");
  }
  const taskBound = value.kind === "run_result" || value.kind === "evolution_abandon";
  if (taskBound !== (value.task_admission !== null && value.accepted_attempt !== null)) {
    issue(context, ["task_admission"], "task-result transitions require both admission and attempt authority");
  }
  if (value.task_admission !== null && value.accepted_attempt !== null) {
    const admission = value.task_admission;
    const attempt = value.accepted_attempt;
    if (admission.project_id !== value.project_id
      || canonicalJsonV2(admission.predecessor_project_head) !== canonicalJsonV2(predecessor)) {
      issue(context, ["task_admission"], "task admission does not pin the transition predecessor");
    }
    if (attempt.project_id !== value.project_id
      || attempt.task_id !== admission.task_id
      || attempt.task_admission_id !== admission.task_admission_id
      || attempt.admission_sha256 !== admission.admission_sha256
      || attempt.predecessor_project_head_id !== predecessor.project_head_id) {
      issue(context, ["accepted_attempt"], "accepted attempt does not belong to the exact admission");
    }
  }
  if (value.successor_project_head !== null) {
    const successor = value.successor_project_head;
    if (successor.project_id !== value.project_id
      || successor.generation !== value.expected_successor_generation
      || successor.predecessor_project_head_id !== predecessor.project_head_id) {
      issue(context, ["successor_project_head"], "successor project head does not bind this transition");
    }
  }
});

export const projectCreateV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  profile_id: opaqueIdV2Schema,
  profile_connection_generation: positiveSafeIntegerV2Schema,
  display_name: displayNameV2Schema,
  config: scienceProjectConfigV2Schema,
}).strict();

export const projectPatchV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  expected_project_head_id: opaqueIdV2Schema.nullable(),
  expected_project_head_manifest_sha256: sha256DigestV2Schema.nullable(),
  expected_project_config_sha256: sha256DigestV2Schema,
  display_name: displayNameV2Schema,
  config: scienceProjectConfigV2Schema,
}).strict().superRefine((value, context) => {
  if ((value.expected_project_head_id === null) !== (value.expected_project_head_manifest_sha256 === null)) {
    issue(context, ["expected_project_head_id"], "expected project head ID and manifest must be present together");
  }
});

export const projectActionV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  expected_project_head_id: opaqueIdV2Schema,
  expected_project_head_manifest_sha256: sha256DigestV2Schema,
}).strict();

export const projectV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  project_id: opaqueIdV2Schema,
  display_name: displayNameV2Schema,
  config: scienceProjectConfigV2Schema,
  project_config_sha256: sha256DigestV2Schema,
  active_project_head: projectHeadRefV2Schema.nullable(),
  admission_etag: etagV2Schema.nullable(),
  state: z.enum(["ready", "transitioning", "not_ready", "needs_attention"]),
  created_at: utcTimestampV2Schema,
  updated_at: utcTimestampV2Schema,
  etag: etagV2Schema,
}).strict().superRefine((value, context) => {
  if (value.project_config_sha256 !== scienceProjectConfigSha256ForV2(value.config)) {
    issue(context, ["project_config_sha256"], "project config digest does not match canonical config bytes");
  }
  if (value.active_project_head !== null && value.active_project_head.project_id !== value.project_id) {
    issue(context, ["active_project_head", "project_id"], "active project head belongs to another project");
  }
  if ((value.active_project_head === null) !== (value.admission_etag === null)) {
    issue(context, ["admission_etag"], "active project head and admission ETag must appear together");
  }
});

export const projectCapabilityProjectionV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  project_id: opaqueIdV2Schema,
  execution_mode: executionModeV2Schema,
  registry_sha256: sha256DigestV2Schema,
  capabilities_sha256: sha256DigestV2Schema,
  capabilities: evolutionCapabilitiesV2Schema,
  fetched_at: utcTimestampV2Schema,
}).strict().superRefine((value, context) => {
  if (value.registry_sha256 !== value.capabilities.registry_digest) {
    issue(context, ["registry_sha256"], "capability registry digest differs from the remote envelope");
  }
  if (value.capabilities_sha256 !== evolutionCapabilitiesSha256ForV2(value.capabilities)) {
    issue(context, ["capabilities_sha256"], "capability digest differs from the remote envelope");
  }
  const expected = value.execution_mode === "codex_subscription_transcript" ? "subscription" : "self_deployed";
  if (value.capabilities.evaluated_profile.execution_mode !== expected) {
    issue(context, ["capabilities", "evaluated_profile", "execution_mode"], "capability execution mode differs from the remote envelope");
  }
});

export const projectValidationRequestV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  expected_project_head_id: opaqueIdV2Schema,
  expected_project_head_manifest_sha256: sha256DigestV2Schema,
  expected_project_config_sha256: sha256DigestV2Schema,
  capability_registry_sha256: sha256DigestV2Schema,
}).strict();

export const validationCheckV2Schema = z.object({
  check_id: opaqueIdV2Schema,
  status: z.enum(["passed", "failed", "unavailable"]),
  action: desktopActionV2Schema,
}).strict();

export const projectValidationV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  project_id: opaqueIdV2Schema,
  valid: z.boolean(),
  registry_sha256: sha256DigestV2Schema,
  checks: z.array(validationCheckV2Schema).max(256),
  validated_at: utcTimestampV2Schema,
}).strict();

export const taskSubmitRequestV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  project_id: opaqueIdV2Schema,
  expected_project_admission_etag: etagV2Schema,
  expected_project_head_id: opaqueIdV2Schema,
  expected_project_head_manifest_sha256: sha256DigestV2Schema,
  expected_project_config_sha256: sha256DigestV2Schema,
}).strict();

export const taskActionV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  task_admission_id: opaqueIdV2Schema,
  admission_sha256: sha256DigestV2Schema,
  predecessor_project_head_id: opaqueIdV2Schema,
}).strict();

export const transitionActionV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  expected_predecessor_project_head_id: opaqueIdV2Schema,
  plan_sha256: sha256DigestV2Schema,
}).strict();

export const transitionReplaceV2Schema = transitionActionV2Schema.and(z.object({
  replacement_plan_sha256: sha256DigestV2Schema,
}).strict());

export const transitionStateV2Schema = z.enum([
  "pending",
  "sealing_dataset",
  "running_methods",
  "validating",
  "materializing",
  "committing",
  "committed",
  "failed",
  "cancelled",
  "superseded",
]);

export const successorTransitionV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  transition: successorTransitionRefV2Schema,
  state: transitionStateV2Schema,
  progress_completed: z.number().int().min(0).max(10_000),
  progress_total: z.number().int().min(0).max(10_000),
  error: apiErrorV2Schema.nullable(),
  created_at: utcTimestampV2Schema,
  updated_at: utcTimestampV2Schema,
}).strict().superRefine((value, context) => {
  if (value.progress_completed > value.progress_total) issue(context, ["progress_completed"], "transition progress exceeds total");
  if ((value.state === "failed") !== (value.error !== null)) issue(context, ["error"], "error is required only for a failed transition");
});

export const taskV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  task_id: opaqueIdV2Schema,
  project_id: opaqueIdV2Schema,
  admission: taskAdmissionRefV2Schema,
  attempts: z.array(attemptRefV2Schema).min(1).max(100),
  authoritative_attempt_id: opaqueIdV2Schema.nullable(),
  successor_transition: successorTransitionRefV2Schema.nullable(),
  state: z.enum(["admitted", "preparing", "running", "cancelling", "completed", "failed", "cancelled", "closed", "waiting_for_successor"]),
  created_at: utcTimestampV2Schema,
  updated_at: utcTimestampV2Schema,
  etag: etagV2Schema,
}).strict().superRefine((value, context) => {
  if (value.admission.task_id !== value.task_id || value.admission.project_id !== value.project_id) {
    issue(context, ["admission"], "admission does not belong to this task");
  }
  const attempts = new Set<string>();
  value.attempts.forEach((attempt, index) => {
    if (attempt.task_id !== value.task_id
      || attempt.project_id !== value.project_id
      || attempt.task_admission_id !== value.admission.task_admission_id
      || attempt.admission_sha256 !== value.admission.admission_sha256
      || attempt.predecessor_project_head_id !== value.admission.predecessor_project_head.project_head_id) {
      issue(context, ["attempts", index], "attempt does not belong to this task admission");
    }
    if (attempt.ordinal !== index + 1) issue(context, ["attempts", index, "ordinal"], "attempt ordinals must be contiguous");
    if (attempts.has(attempt.attempt_id)) issue(context, ["attempts", index, "attempt_id"], "attempt identities must be unique");
    attempts.add(attempt.attempt_id);
  });
  if (value.authoritative_attempt_id !== null && !attempts.has(value.authoritative_attempt_id)) {
    issue(context, ["authoritative_attempt_id"], "authoritative attempt is not part of this task");
  }
  if (value.successor_transition !== null
    && (value.authoritative_attempt_id === null || value.successor_transition.project_id !== value.project_id)) {
    issue(context, ["successor_transition"], "task transition has another authority");
  }
});

const coreEventBaseShape = {
  schema_version: schemaVersionV2Schema,
  event_id: opaqueIdV2Schema,
  sequence: positiveSafeIntegerV2Schema,
  occurred_at: utcTimestampV2Schema,
  project_id: opaqueIdV2Schema,
};

export const taskAdmittedEventV2Schema = z.object({
  ...coreEventBaseShape,
  event_type: z.literal("task_admitted"),
  admission: taskAdmissionRefV2Schema,
}).strict().superRefine((value, context) => {
  if (value.admission.project_id !== value.project_id) issue(context, ["admission", "project_id"], "event project differs from its task admission");
});

export const attemptAppendedEventV2Schema = z.object({
  ...coreEventBaseShape,
  event_type: z.literal("attempt_appended"),
  attempt: attemptRefV2Schema,
}).strict().superRefine((value, context) => {
  if (value.attempt.project_id !== value.project_id) issue(context, ["attempt", "project_id"], "event project differs from its attempt");
});

export const datasetSealedEventV2Schema = z.object({
  ...coreEventBaseShape,
  event_type: z.literal("dataset_sealed"),
  task_id: opaqueIdV2Schema,
  task_admission_id: opaqueIdV2Schema,
  attempt_id: opaqueIdV2Schema,
  dataset_id: opaqueIdV2Schema,
  dataset_sha256: sha256DigestV2Schema,
}).strict();

export const transitionChangedEventV2Schema = z.object({
  ...coreEventBaseShape,
  event_type: z.literal("transition_changed"),
  transition: successorTransitionRefV2Schema,
  state: transitionStateV2Schema,
  progress_completed: z.number().int().min(0).max(10_000),
  progress_total: z.number().int().min(0).max(10_000),
}).strict().superRefine((value, context) => {
  if (value.transition.project_id !== value.project_id) issue(context, ["transition", "project_id"], "event project differs from its transition");
  if (value.progress_completed > value.progress_total) issue(context, ["progress_completed"], "transition progress exceeds total");
});

export const evolutionRevisionCommittedEventV2Schema = z.object({
  ...coreEventBaseShape,
  event_type: z.literal("evolution_revision_committed"),
  successor_transition_id: opaqueIdV2Schema,
  evolution_revision: evolutionRevisionRefV2Schema,
}).strict().superRefine((value, context) => {
  if (value.evolution_revision.project_id !== value.project_id) issue(context, ["evolution_revision", "project_id"], "event project differs from its evolution revision");
});

export const runtimeContextCommittedEventV2Schema = z.object({
  ...coreEventBaseShape,
  event_type: z.literal("runtime_context_committed"),
  successor_transition_id: opaqueIdV2Schema,
  runtime_context_snapshot: runtimeContextSnapshotRefV2Schema,
}).strict().superRefine((value, context) => {
  if (value.runtime_context_snapshot.project_id !== value.project_id) issue(context, ["runtime_context_snapshot", "project_id"], "event project differs from its runtime context");
});

export const projectHeadActivatedEventV2Schema = z.object({
  ...coreEventBaseShape,
  event_type: z.literal("project_head_activated"),
  successor_transition_id: opaqueIdV2Schema,
  project_head: projectHeadRefV2Schema,
}).strict().superRefine((value, context) => {
  if (value.project_head.project_id !== value.project_id) issue(context, ["project_head", "project_id"], "event project differs from its project head");
});

export const coreEventEnvelopeV2Schema = z.union([
  taskAdmittedEventV2Schema,
  attemptAppendedEventV2Schema,
  datasetSealedEventV2Schema,
  transitionChangedEventV2Schema,
  evolutionRevisionCommittedEventV2Schema,
  runtimeContextCommittedEventV2Schema,
  projectHeadActivatedEventV2Schema,
]);

export const taskContextV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  task_id: opaqueIdV2Schema,
  task_admission_id: opaqueIdV2Schema,
  project_head: projectHeadRefV2Schema,
  workspace_snapshot: workspaceSnapshotRefV2Schema,
}).strict().superRefine((value, context) => {
  if (value.project_head.project_id !== value.workspace_snapshot.project_id) {
    issue(context, ["workspace_snapshot", "project_id"], "task context workspace belongs to another project");
  }
});

export const artifactV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  artifact_id: opaqueIdV2Schema,
  project_id: opaqueIdV2Schema,
  artifact_type: z.enum(["dataset", "workspace_result", "text_memory", "skill_bundle", "agent_system", "parametric_memory", "diagnostic"]),
  manifest_sha256: sha256DigestV2Schema,
  byte_size: z.number().int().safe().min(0).max(16 * 1024 * 1024 * 1024),
  created_at: utcTimestampV2Schema,
}).strict();

export const artifactContentV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  artifact: artifactV2Schema,
  media_type: mimeTypeV2Schema,
  content_sha256: sha256DigestV2Schema,
  byte_size: z.number().int().safe().min(0).max(16 * 1024 * 1024 * 1024),
}).strict();

export const artifactDiffV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  artifact_id: opaqueIdV2Schema,
  previous_artifact_id: opaqueIdV2Schema.nullable(),
  current_manifest_sha256: sha256DigestV2Schema,
  previous_manifest_sha256: sha256DigestV2Schema.nullable(),
  status: z.enum(["available", "not_comparable", "unavailable"]),
}).strict().superRefine((value, context) => {
  if ((value.previous_artifact_id === null) !== (value.previous_manifest_sha256 === null)) {
    issue(context, ["previous_manifest_sha256"], "previous artifact identity and manifest must appear together");
  }
});

export const serviceV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  service_id: opaqueIdV2Schema,
  kind: z.enum(["daemon", "codex", "gateway", "worker", "runtime", "model"]),
  status: z.enum(["ready", "starting", "stopping", "degraded", "unavailable"]),
  updated_at: utcTimestampV2Schema,
  etag: etagV2Schema,
}).strict();

export const diagnosticV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  diagnostic_id: opaqueIdV2Schema,
  scope: z.enum(["system", "project", "task", "transition", "service"]),
  resource_id: opaqueIdV2Schema.nullable(),
  status: z.enum(["queued", "running", "ready", "failed"]),
  artifact_id: opaqueIdV2Schema.nullable(),
  created_at: utcTimestampV2Schema,
  updated_at: utcTimestampV2Schema,
  etag: etagV2Schema,
}).strict();

export const sshHostHintV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  ssh_host_alias: sshHostAliasV2Schema,
  availability: z.enum(["selectable", "manual_entry_only", "unsupported"]),
  source_kind: z.enum(["literal_host", "static_include"]),
}).strict();

export const sshCatalogWarningV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  code: z.enum([
    "dynamic_hosts_not_enumerated",
    "conditional_hosts_not_enumerated",
    "include_cycle_skipped",
    "include_unreadable",
    "catalog_budget_exhausted",
    "invalid_config_text_skipped",
  ]),
  action: z.enum(["manual_alias_available", "rescan", "administrator_action"]),
  affected_entry_count: z.number().int().min(1).max(10_000),
}).strict();

export const sshHostCatalogV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  catalog_generation: nonNegativeSafeIntegerV2Schema,
  hosts: z.array(sshHostHintV2Schema).max(MAX_HOST_HINTS_V2),
  warnings: z.array(sshCatalogWarningV2Schema).max(MAX_CATALOG_WARNINGS_V2),
  scanned_at: utcTimestampV2Schema,
}).strict().superRefine((value, context) => {
  const aliases = value.hosts.map((host) => host.ssh_host_alias);
  if (new Set(aliases).size !== aliases.length) issue(context, ["hosts"], "SSH host aliases must be unique");
  if (aliases.some((alias, index) => index > 0 && aliases[index - 1]! > alias)) issue(context, ["hosts"], "SSH host aliases must be sorted");
});

export const sshHostCatalogRescanV2Schema = z.object({ schema_version: schemaVersionV2Schema }).strict();

export const sshPromptStateV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  connection_generation: positiveSafeIntegerV2Schema,
  kind: z.enum(["password", "passphrase", "confirmation"]),
  state: z.enum(["pending", "completed", "cancelled", "expired"]),
  requested_at: utcTimestampV2Schema,
}).strict();

export const sshHostKeyFingerprintV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  algorithm: z.enum(["ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"]),
  sha256_fingerprint: z.string().regex(/^SHA256:[A-Za-z0-9+/]{43}$/),
  role: z.enum(["previous", "presented"]),
}).strict();

export const sshTrustStateV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  connection_generation: positiveSafeIntegerV2Schema,
  state: z.enum(["unverified", "trusted", "first_use_review", "changed_key_blocked", "rejected", "repairing"]),
  review_id: opaqueIdV2Schema.nullable(),
  review_sha256: sha256DigestV2Schema.nullable(),
  key_fingerprints: z.array(sshHostKeyFingerprintV2Schema).max(16),
  repair_support: z.enum(["not_needed", "first_use_acceptance_available", "automatic_replacement_available", "administrator_required"]),
}).strict().superRefine((value, context) => {
  const review = value.state === "first_use_review" || value.state === "changed_key_blocked";
  if (review && (value.review_id === null || value.review_sha256 === null || value.key_fingerprints.length === 0)) {
    issue(context, ["review_id"], "host-key review state requires a review identity and fingerprint");
  }
  if (!review && (value.review_id !== null || value.review_sha256 !== null || value.key_fingerprints.length > 0)) {
    issue(context, ["review_id"], "non-review trust state must not retain review material");
  }
});

export const connectionStateV2Schema = z.enum([
  "disconnected",
  "connecting",
  "prompt_pending",
  "host_key_review",
  "bootstrapping",
  "negotiating",
  "connected",
  "disconnecting",
  "failed",
]);

export const systemOpenSshProfileCreateV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  display_name: displayNameV2Schema,
  connection_authority: z.literal("system_openssh").default("system_openssh"),
  ssh_host_alias: sshHostAliasV2Schema,
}).strict();

export const remoteWorkspaceProfileV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  profile_kind: z.literal("system_openssh").default("system_openssh"),
  profile_id: opaqueIdV2Schema,
  display_name: displayNameV2Schema,
  connection_authority: z.literal("system_openssh").default("system_openssh"),
  ssh_host_alias: sshHostAliasV2Schema,
  catalog_generation: nonNegativeSafeIntegerV2Schema,
  connection_generation: positiveSafeIntegerV2Schema,
  connection_state: connectionStateV2Schema,
  prompt: sshPromptStateV2Schema.nullable(),
  trust: sshTrustStateV2Schema,
  failure: desktopErrorV2Schema.nullable(),
  active_project_id: opaqueIdV2Schema.nullable(),
  core_api_major: z.literal(2).nullable(),
  core_openapi_sha256: sha256DigestV2Schema.nullable(),
  core_event_schema_sha256: sha256DigestV2Schema.nullable(),
  core_registry_sha256: sha256DigestV2Schema.nullable(),
  created_at: utcTimestampV2Schema,
  updated_at: utcTimestampV2Schema,
  etag: etagV2Schema,
}).strict().superRefine((value, context) => {
  if (value.trust.connection_generation !== value.connection_generation) {
    issue(context, ["trust", "connection_generation"], "trust state has another connection generation");
  }
  if (value.prompt !== null && value.prompt.connection_generation !== value.connection_generation) {
    issue(context, ["prompt", "connection_generation"], "prompt state has another connection generation");
  }
  if ((value.connection_state === "prompt_pending") !== (value.prompt !== null)) {
    issue(context, ["prompt"], "prompt is present only while a prompt is pending");
  }
  if (value.prompt !== null && value.prompt.state !== "pending") issue(context, ["prompt", "state"], "a profile may expose only a pending prompt");
  if ((value.connection_state === "failed") !== (value.failure !== null)) {
    issue(context, ["failure"], "failure is present only for a failed connection");
  }
  if (value.connection_state === "host_key_review" && value.trust.state !== "first_use_review" && value.trust.state !== "changed_key_blocked") {
    issue(context, ["trust", "state"], "host-key review connection requires a trust review");
  }
  const coreIdentity = [
    value.core_api_major,
    value.core_openapi_sha256,
    value.core_event_schema_sha256,
    value.core_registry_sha256,
  ];
  if (value.connection_state === "connected") {
    if (coreIdentity.some((item) => item === null)) issue(context, ["core_api_major"], "connected profile requires exact Core v2 identity");
    if (value.prompt !== null || value.failure !== null) issue(context, ["connection_state"], "connected profile cannot retain prompt or failure state");
  } else if (coreIdentity.some((item) => item !== null)) {
    issue(context, ["core_api_major"], "only a connected profile may expose negotiated Core identity");
  }
});

export const legacyExplicitProfileV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  profile_kind: z.literal("legacy_explicit").default("legacy_explicit"),
  profile_id: opaqueIdV2Schema,
  display_name: displayNameV2Schema,
  connectable: z.literal(false).default(false),
  migration_state: z.enum(["rebind_required", "quarantined"]),
  created_at: utcTimestampV2Schema,
  updated_at: utcTimestampV2Schema,
  etag: etagV2Schema,
}).strict();

export const remoteProfileV2Schema = z.union([remoteWorkspaceProfileV2Schema, legacyExplicitProfileV2Schema]);

export const profileDisplayNamePatchV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  display_name: displayNameV2Schema,
}).strict();

export const profileRebindV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  connection_authority: z.literal("system_openssh").default("system_openssh"),
  ssh_host_alias: sshHostAliasV2Schema,
  catalog_generation: nonNegativeSafeIntegerV2Schema,
}).strict();

export const profileConnectionActionV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  expected_connection_generation: positiveSafeIntegerV2Schema,
}).strict();

export const hostKeyReviewRequestV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  expected_connection_generation: positiveSafeIntegerV2Schema,
  review_id: opaqueIdV2Schema,
  review_sha256: sha256DigestV2Schema,
  action: z.enum(["accept_first_use", "replace_changed_key", "reject"]),
}).strict();

export const lifecycleOperationKindV2Schema = z.enum([
  "profile_connect",
  "profile_disconnect",
  "host_key_review",
  "native_workspace_prepare",
  "project_create",
  "project_activate",
]);
export const lifecycleOperationStatusV2Schema = z.enum([
  "queued",
  "running",
  "succeeded",
  "failed",
  "cancelled",
]);
export const LIFECYCLE_PHASES_V2 = [
  "validation",
  "queued",
  "resolving_system_openssh",
  "connecting",
  "waiting_for_user",
  "remote_preflight",
  "transferring",
  "verifying",
  "starting_daemon",
  "waiting_for_daemon",
  "opening_project_tunnel",
  "negotiating_core",
  "preparing_native_workspace",
  "creating_remote_project",
  "verifying_project",
  "activating",
  "finalizing",
] as const;
export const lifecyclePhaseV2Schema = z.enum(LIFECYCLE_PHASES_V2);

export const lifecycleProgressV2Schema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("indeterminate") }).strict(),
  z.object({
    kind: z.literal("bytes"),
    completed: nonNegativeSafeIntegerV2Schema,
    total: positiveSafeIntegerV2Schema,
  }).strict(),
  z.object({
    kind: z.literal("items"),
    completed: nonNegativeSafeIntegerV2Schema,
    total: positiveSafeIntegerV2Schema,
  }).strict(),
]).superRefine((value, context) => {
  if (value.kind !== "indeterminate") validateLifecycleProgressV2(value, context);
});

export const lifecycleResourceRefV2Schema = z.discriminatedUnion("resource_kind", [
  z.object({ resource_kind: z.literal("profile"), resource_id: opaqueIdV2Schema }).strict(),
  z.object({ resource_kind: z.literal("native_workspace"), resource_id: opaqueIdV2Schema }).strict(),
  z.object({ resource_kind: z.literal("project"), resource_id: opaqueIdV2Schema }).strict(),
]);

export const lifecycleResultV2Schema = z.discriminatedUnion("result_kind", [
  z.object({
    result_kind: z.literal("profile"),
    profile_id: opaqueIdV2Schema,
    connection_generation: positiveSafeIntegerV2Schema,
  }).strict(),
  z.object({
    result_kind: z.literal("native_workspace"),
    import_id: opaqueIdV2Schema,
    content_sha256: sha256DigestV2Schema,
    byte_size: positiveSafeIntegerV2Schema,
    entry_count: nonNegativeSafeIntegerV2Schema,
    extracted_byte_size: nonNegativeSafeIntegerV2Schema,
    display_name: displayNameV2Schema,
  }).strict(),
  z.object({
    result_kind: z.literal("project"),
    project_id: opaqueIdV2Schema,
  }).strict(),
]);

const lifecycleOperationBaseV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  operation_id: opaqueIdV2Schema,
  kind: lifecycleOperationKindV2Schema,
  resource: lifecycleResourceRefV2Schema,
  request_sha256: sha256DigestV2Schema,
  status: lifecycleOperationStatusV2Schema,
  phase: lifecyclePhaseV2Schema,
  phase_index: z.number().int().min(0).max(LIFECYCLE_PHASES_V2.length - 1),
  phase_total: z.number().int().min(1).max(LIFECYCLE_PHASES_V2.length),
  log_sequence_high_watermark: nonNegativeSafeIntegerV2Schema,
  updated_at: utcTimestampV2Schema,
  etag: etagV2Schema,
}).strict();

export const lifecycleOperationRefV2Schema = lifecycleOperationBaseV2Schema
  .superRefine((value, context) => validateLifecycleIdentityAndPhaseV2(value, null, context));

export const lifecycleOperationV2Schema = lifecycleOperationBaseV2Schema.extend({
  progress: lifecycleProgressV2Schema.nullable(),
  cancellable: z.boolean(),
  result: lifecycleResultV2Schema.nullable(),
  failure: desktopErrorV2Schema.nullable(),
  created_at: utcTimestampV2Schema,
  started_at: utcTimestampV2Schema.nullable(),
  finished_at: utcTimestampV2Schema.nullable(),
}).strict().superRefine((value, context) => {
  validateLifecycleIdentityAndPhaseV2(value, value.result, context);
  if (value.status === "succeeded") {
    if (value.result === null) issue(context, ["result"], "succeeded operation requires a typed result");
    if (value.failure !== null) issue(context, ["failure"], "succeeded operation cannot retain a failure");
    if (value.phase !== "finalizing") issue(context, ["phase"], "succeeded operation requires the final phase");
  } else if (value.status === "failed") {
    if (value.failure === null) issue(context, ["failure"], "failed operation requires a typed failure");
    if (value.result !== null) issue(context, ["result"], "failed operation cannot retain a result");
  } else if (value.result !== null || value.failure !== null) {
    issue(context, ["result"], "result and failure are terminal status fields");
  }
  const terminal = ["succeeded", "failed", "cancelled"].includes(value.status);
  if (terminal !== (value.finished_at !== null)) {
    issue(context, ["finished_at"], "finished timestamp must match terminal status");
  }
  if (terminal && value.cancellable) issue(context, ["cancellable"], "terminal operation cannot remain cancellable");
  if (value.status === "queued" && value.started_at !== null) {
    issue(context, ["started_at"], "queued operation cannot have a started timestamp");
  }
  if (value.status === "running" && value.started_at === null) {
    issue(context, ["started_at"], "running operation requires a started timestamp");
  }
  const timestamps = [value.created_at, ...(value.started_at === null ? [] : [value.started_at]), value.updated_at]
    .map((timestamp) => canonicalUtcTimestampV2(timestamp));
  if (timestamps.some((timestamp) => timestamp === null)
    || timestamps.some((timestamp, index) => index > 0 && timestamp! < timestamps[index - 1]!)) {
    issue(context, ["updated_at"], "lifecycle timestamps cannot regress");
  }
  if (value.finished_at !== null
    && canonicalUtcTimestampV2(value.finished_at) !== timestamps[timestamps.length - 1]) {
    issue(context, ["finished_at"], "terminal timestamp must equal the immutable update timestamp");
  }
});

export const lifecycleLogEntryV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  operation_id: opaqueIdV2Schema,
  sequence: positiveSafeIntegerV2Schema,
  occurred_at: utcTimestampV2Schema,
  source: z.enum(["desktop", "ssh_stdout", "ssh_stderr", "daemon_stdout", "daemon_stderr"]),
  text: z.string().min(1)
    .refine((text) => utf8ByteLength(text) <= 16 * 1024, "lifecycle log text exceeds the UTF-8 byte limit")
    .refine(isSafeLifecycleLogTextV2, "lifecycle log text contains a prohibited control character"),
  truncated: z.boolean(),
}).strict();

export const lifecycleLogPageV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  operation_id: opaqueIdV2Schema,
  dropped_before_sequence: nonNegativeSafeIntegerV2Schema,
  items: z.array(lifecycleLogEntryV2Schema).max(100),
  next_cursor: cursorV2Schema.nullable().default(null),
  has_more: z.boolean(),
}).strict().superRefine((value, context) => {
  if (value.has_more !== (value.next_cursor !== null)) issue(context, ["next_cursor"], "has_more must match next_cursor presence");
  const sequences = value.items.map((entry) => entry.sequence);
  if (value.items.some((entry) => entry.operation_id !== value.operation_id)) {
    issue(context, ["items"], "lifecycle log entry belongs to another operation");
  }
  if (sequences.some((sequence, index) => index > 0 && sequence <= sequences[index - 1]!)) {
    issue(context, ["items"], "lifecycle log sequences must be unique and ascending");
  }
  if (sequences.some((sequence) => sequence <= value.dropped_before_sequence)) {
    issue(context, ["dropped_before_sequence"], "lifecycle log entry is at or before the dropped boundary");
  }
});

export const lifecycleCancelV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  expected_operation_id: opaqueIdV2Schema,
}).strict();

export const lifecycleAcknowledgeV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  expected_operation_id: opaqueIdV2Schema,
  expected_terminal_status: z.enum(["succeeded", "failed", "cancelled"]),
}).strict();

export const coreOperationV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  operation_id: opaqueIdV2Schema,
  kind: z.enum([
    "transition_retry",
    "transition_abandon",
    "attempt_cancel",
    "task_close",
    "service_restart",
    "diagnostic",
    "cache_cleanup",
  ]),
  status: z.enum(["queued", "running", "succeeded", "failed", "cancelled"]),
  progress_completed: z.number().int().min(0).max(10_000),
  progress_total: z.number().int().min(0).max(10_000),
  error: apiErrorV2Schema.nullable(),
  created_at: utcTimestampV2Schema,
  updated_at: utcTimestampV2Schema,
  etag: etagV2Schema,
}).strict();

export const cacheCleanupRequestV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  scope: z.literal("safe_unreferenced").default("safe_unreferenced"),
}).strict();

export const localOperationV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  operation_id: opaqueIdV2Schema,
  kind: z.enum([
    "ssh_catalog_rescan",
    "profile_connect",
    "profile_disconnect",
    "host_key_review",
    "project_activate",
    "task_cancel",
    "task_retry",
    "transition_retry",
    "transition_replace",
    "transition_abandon",
    "service_restart",
    "diagnostic",
  ]),
  status: z.enum(["queued", "running", "succeeded", "failed", "cancelled"]),
  failure: desktopErrorV2Schema.nullable(),
  created_at: utcTimestampV2Schema,
  updated_at: utcTimestampV2Schema,
}).strict().superRefine((value, context) => {
  if ((value.status === "failed") !== (value.failure !== null)) issue(context, ["failure"], "failure is required only for a failed operation");
});

export const desktopVersionV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  api_name: z.literal("openevo-desktop-local-api"),
  preferred_major: z.literal(2),
  supported_majors: z.array(z.literal(2)).length(1),
  mutation_major: z.literal(2),
  openapi_sha256: sha256DigestV2Schema,
  event_schema_sha256: sha256DigestV2Schema,
  release_version: safeSummaryV2Schema,
  build_id: sha256DigestV2Schema,
  source_commit: z.string().regex(/^[0-9a-f]{7,64}$/),
  build_channel: z.enum(["release", "development", "test"]),
  provider_kind: z.literal("desktop_sidecar"),
  feature_flags: z.array(opaqueIdV2Schema).min(1).max(128),
  feature_set_sha256: sha256DigestV2Schema,
  required_core_api_major: z.literal(2),
  mutation_compatible: z.boolean(),
}).strict().superRefine((value, context) => {
  if (value.supported_majors.length !== 1 || value.supported_majors[0] !== 2) issue(context, ["supported_majors"], "Desktop v2 discovery must support only major 2");
  if (!isSortedUnique(value.feature_flags)) issue(context, ["feature_flags"], "feature flags must be sorted and unique");
  if (sha256Utf8V2(canonicalJsonV2(value.feature_flags)) !== value.feature_set_sha256) {
    issue(context, ["feature_set_sha256"], "feature-set digest does not match feature flags");
  }
});

export const negotiatedContractV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  major: z.literal(2),
  mutation_major: z.literal(2),
  openapi_sha256: sha256DigestV2Schema,
  event_schema_sha256: sha256DigestV2Schema,
  release_version: safeSummaryV2Schema,
  build_id: sha256DigestV2Schema,
  source_commit: z.string().regex(/^[0-9a-f]{7,64}$/),
  build_channel: z.enum(["release", "development", "test"]),
  provider_kind: z.literal("desktop_sidecar"),
  feature_flags: z.array(opaqueIdV2Schema).min(1).max(128),
  feature_set_sha256: sha256DigestV2Schema,
  required_core_api_major: z.literal(2),
  mutation_compatible: z.boolean(),
}).strict().superRefine((value, context) => {
  if (!isSortedUnique(value.feature_flags)) issue(context, ["feature_flags"], "feature flags must be sorted and unique");
  if (sha256Utf8V2(canonicalJsonV2(value.feature_flags)) !== value.feature_set_sha256) {
    issue(context, ["feature_set_sha256"], "feature-set digest does not match feature flags");
  }
});

export const desktopBootstrapContextV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  endpoint: z.string().url().refine(isLoopbackEndpointV2, "sidecar endpoint must be an unauthenticated loopback HTTP origin"),
  session_token: z.string().min(32).max(4_096).refine(noControlCharacters),
  negotiated_contract: negotiatedContractV2Schema,
}).strict();

export const desktopHealthV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  status: z.enum(["ready", "starting", "unavailable"]),
  checked_at: utcTimestampV2Schema,
}).strict();

export const desktopStateV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  profiles: z.array(remoteProfileV2Schema).max(MAX_PROFILE_COUNT_V2),
  active_profile_id: opaqueIdV2Schema.nullable(),
  active_project_id: opaqueIdV2Schema.nullable(),
  pending_operations: z.array(lifecycleOperationRefV2Schema).max(16),
  last_event_id: opaqueIdV2Schema.nullable(),
  updated_at: utcTimestampV2Schema,
}).strict().superRefine((value, context) => {
  uniqueBy(value.profiles, (profile) => profile.profile_id, context, ["profiles"]);
  if (value.active_profile_id !== null && !value.profiles.some((profile) => profile.profile_id === value.active_profile_id)) {
    issue(context, ["active_profile_id"], "active profile is absent from state");
  }
  uniqueBy(value.pending_operations, (operation) => operation.operation_id, context, ["pending_operations"]);
});

export const serviceRestartV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  expected_service_id: opaqueIdV2Schema,
}).strict();

export const diagnosticRequestV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  profile_id: opaqueIdV2Schema,
  profile_connection_generation: positiveSafeIntegerV2Schema,
  scope: z.enum(["system", "project", "task", "transition", "service"]),
  resource_id: opaqueIdV2Schema.nullable(),
}).strict();

const hostCatalogEventPayloadV2Schema = z.object({
  payload_kind: z.literal("ssh_host_catalog_changed"),
  catalog_generation: nonNegativeSafeIntegerV2Schema,
  host_count: z.number().int().min(0).max(MAX_HOST_HINTS_V2),
  warning_count: z.number().int().min(0).max(MAX_CATALOG_WARNINGS_V2),
}).strict();

const profileEventPayloadV2Schema = z.object({
  payload_kind: z.literal("profile_connection_changed"),
  profile_id: opaqueIdV2Schema,
  connection_generation: positiveSafeIntegerV2Schema,
  connection_state: connectionStateV2Schema,
  failure: desktopErrorV2Schema.nullable(),
}).strict().superRefine((value, context) => {
  if ((value.connection_state === "failed") !== (value.failure !== null)) issue(context, ["failure"], "profile event failure must match failed state");
});

const coreAuthorityEventPayloadV2Schema = z.object({
  payload_kind: z.literal("core_authority_changed"),
  profile_id: opaqueIdV2Schema,
  project_id: opaqueIdV2Schema,
  core_event_id: opaqueIdV2Schema,
  core_event_sequence: positiveSafeIntegerV2Schema,
  core_event_type: z.enum(["task_admitted", "attempt_appended", "dataset_sealed", "transition_changed", "evolution_revision_committed", "runtime_context_committed", "project_head_activated"]),
  core_payload_sha256: sha256DigestV2Schema,
}).strict();

const diagnosticEventPayloadV2Schema = z.object({
  payload_kind: z.literal("diagnostic_changed"),
  diagnostic_id: opaqueIdV2Schema,
  status: z.enum(["queued", "running", "ready", "failed"]),
}).strict();

const lifecycleOperationEventPayloadV2Schema = z.object({
  payload_kind: z.literal("lifecycle_operation_changed"),
  operation_id: opaqueIdV2Schema,
  kind: lifecycleOperationKindV2Schema,
  status: lifecycleOperationStatusV2Schema,
  phase: lifecyclePhaseV2Schema,
  etag: etagV2Schema,
  log_sequence_high_watermark: nonNegativeSafeIntegerV2Schema,
}).strict();

export const desktopEventPayloadV2Schema = z.union([
  hostCatalogEventPayloadV2Schema,
  profileEventPayloadV2Schema,
  coreAuthorityEventPayloadV2Schema,
  diagnosticEventPayloadV2Schema,
  lifecycleOperationEventPayloadV2Schema,
]);

export const desktopEventTypeV2Schema = z.enum([
  "ssh_host_catalog_changed",
  "profile_connection_changed",
  "core_authority_changed",
  "diagnostic_changed",
  "lifecycle_operation_changed",
]);

export const desktopEventEnvelopeV2Schema = z.object({
  schema_version: schemaVersionV2Schema,
  event_id: opaqueIdV2Schema,
  sequence: positiveSafeIntegerV2Schema,
  occurred_at: utcTimestampV2Schema,
  event_type: desktopEventTypeV2Schema,
  payload_sha256: sha256DigestV2Schema,
  payload: desktopEventPayloadV2Schema,
}).strict().superRefine((value, context) => {
  if (value.event_type !== value.payload.payload_kind) issue(context, ["event_type"], "event type differs from payload kind");
  if (sha256Utf8V2(canonicalJsonV2(value.payload)) !== value.payload_sha256) {
    issue(context, ["payload_sha256"], "event payload digest mismatch");
  }
});

export const desktopSseFrameV2Schema = z.object({
  id: opaqueIdV2Schema,
  event: desktopEventTypeV2Schema,
  data: desktopEventEnvelopeV2Schema,
  retry: z.number().int().min(1_000).max(60_000).nullable().default(null),
}).strict().superRefine((value, context) => {
  if (value.id !== value.data.event_id) issue(context, ["id"], "SSE frame ID differs from event ID");
  if (value.event !== value.data.event_type) issue(context, ["event"], "SSE event name differs from event type");
});

function cursorPageV2Schema<T extends z.ZodTypeAny>(itemSchema: T, maxItems = MAX_PAGE_SIZE_V2) {
  return z.object({
    schema_version: schemaVersionV2Schema,
    items: z.array(itemSchema).max(maxItems),
    next_cursor: cursorV2Schema.nullable().default(null),
    has_more: z.boolean(),
  }).strict().superRefine((value, context) => {
    if (value.has_more !== (value.next_cursor !== null)) issue(context, ["next_cursor"], "has_more must match next_cursor presence");
  });
}

export const remoteProfilePageV2Schema = cursorPageV2Schema(remoteProfileV2Schema, MAX_PROFILE_COUNT_V2);
export const projectPageV2Schema = cursorPageV2Schema(projectV2Schema);
export const taskPageV2Schema = cursorPageV2Schema(taskV2Schema);
export const timelinePageV2Schema = cursorPageV2Schema(coreEventEnvelopeV2Schema);
export const artifactPageV2Schema = cursorPageV2Schema(artifactV2Schema);
export const servicePageV2Schema = cursorPageV2Schema(serviceV2Schema);

export function scienceProjectConfigSha256ForV2(config: z.output<typeof scienceProjectConfigV2Schema>): string {
  return sha256Utf8V2(canonicalJsonV2(config));
}

export function taskAdmissionSha256ForV2(admission: {
  readonly admission_sha256: string;
  readonly [key: string]: unknown;
}): string {
  const { admission_sha256: _ignored, ...payload } = admission;
  return sha256Utf8V2(canonicalJsonV2(payload));
}

export function evolutionCapabilitiesSha256ForV2(capabilities: z.output<typeof evolutionCapabilitiesV2Schema>): string {
  return sha256Utf8V2(canonicalJsonV2(capabilities));
}

export function canonicalJsonV2(value: unknown): string {
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") {
    if (!Number.isFinite(value) || (Number.isInteger(value) && !Number.isSafeInteger(value))) {
      throw new Error("canonical JSON requires finite JavaScript-safe numbers");
    }
    return JSON.stringify(value);
  }
  if (typeof value === "string") return asciiJsonString(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJsonV2).join(",")}]`;
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, child]) => child !== undefined)
      .sort(([left], [right]) => compareUnicodeCodePoints(left, right));
    return `{${entries.map(([key, child]) => `${asciiJsonString(key)}:${canonicalJsonV2(child)}`).join(",")}}`;
  }
  throw new Error("canonical JSON does not support this value");
}

export function sha256Utf8V2(value: string): string {
  return sha256Bytes(new TextEncoder().encode(value));
}

export type DesktopErrorV2 = z.infer<typeof desktopErrorV2Schema>;
export type ApiErrorV2 = z.infer<typeof apiErrorV2Schema>;
export type DesktopVersionV2 = z.infer<typeof desktopVersionV2Schema>;
export type NegotiatedContractV2 = z.infer<typeof negotiatedContractV2Schema>;
export type DesktopBootstrapContextV2 = z.infer<typeof desktopBootstrapContextV2Schema>;
export type DesktopHealthV2 = z.infer<typeof desktopHealthV2Schema>;
export type DesktopStateV2 = z.infer<typeof desktopStateV2Schema>;
export type SshHostCatalogV2 = z.infer<typeof sshHostCatalogV2Schema>;
export type SshHostCatalogRescanV2 = z.input<typeof sshHostCatalogRescanV2Schema>;
export type RemoteWorkspaceProfileV2 = z.infer<typeof remoteWorkspaceProfileV2Schema>;
export type LegacyExplicitProfileV2 = z.infer<typeof legacyExplicitProfileV2Schema>;
export type RemoteProfileV2 = z.infer<typeof remoteProfileV2Schema>;
export type RemoteProfilePageV2 = z.infer<typeof remoteProfilePageV2Schema>;
export type SystemOpenSshProfileCreateV2 = z.input<typeof systemOpenSshProfileCreateV2Schema>;
export type ProfileDisplayNamePatchV2 = z.input<typeof profileDisplayNamePatchV2Schema>;
export type ProfileRebindV2 = z.input<typeof profileRebindV2Schema>;
export type ProfileConnectionActionV2 = z.input<typeof profileConnectionActionV2Schema>;
export type HostKeyReviewRequestV2 = z.input<typeof hostKeyReviewRequestV2Schema>;
export type LifecycleOperationKindV2 = z.infer<typeof lifecycleOperationKindV2Schema>;
export type LifecycleOperationStatusV2 = z.infer<typeof lifecycleOperationStatusV2Schema>;
export type LifecyclePhaseV2 = z.infer<typeof lifecyclePhaseV2Schema>;
export type LifecycleProgressV2 = z.infer<typeof lifecycleProgressV2Schema>;
export type LifecycleResourceRefV2 = z.infer<typeof lifecycleResourceRefV2Schema>;
export type LifecycleResultV2 = z.infer<typeof lifecycleResultV2Schema>;
export type LifecycleOperationRefV2 = z.infer<typeof lifecycleOperationRefV2Schema>;
export type LifecycleOperationV2 = z.infer<typeof lifecycleOperationV2Schema>;
export type LifecycleLogEntryV2 = z.infer<typeof lifecycleLogEntryV2Schema>;
export type LifecycleLogPageV2 = z.infer<typeof lifecycleLogPageV2Schema>;
export type LifecycleCancelV2 = z.input<typeof lifecycleCancelV2Schema>;
export type LifecycleAcknowledgeV2 = z.input<typeof lifecycleAcknowledgeV2Schema>;
export type OperationV2 = z.infer<typeof coreOperationV2Schema>;
export type CacheCleanupRequestV2 = z.input<typeof cacheCleanupRequestV2Schema>;
export type LocalOperationV2 = z.infer<typeof localOperationV2Schema>;
export type ScienceProjectConfigV2 = z.infer<typeof scienceProjectConfigV2Schema>;
export type ProjectCreateV2 = z.input<typeof projectCreateV2Schema>;
export type ProjectPatchV2 = z.input<typeof projectPatchV2Schema>;
export type ProjectActionV2 = z.input<typeof projectActionV2Schema>;
export type ProjectV2 = z.infer<typeof projectV2Schema>;
export type ProjectPageV2 = z.infer<typeof projectPageV2Schema>;
export type ProjectCapabilityProjectionV2 = z.infer<typeof projectCapabilityProjectionV2Schema>;
export type ProjectValidationRequestV2 = z.input<typeof projectValidationRequestV2Schema>;
export type ProjectValidationV2 = z.infer<typeof projectValidationV2Schema>;
export type WorkspaceSnapshotRefV2 = z.infer<typeof workspaceSnapshotRefV2Schema>;
export type EvolutionRevisionRefV2 = z.infer<typeof evolutionRevisionRefV2Schema>;
export type RuntimeContextSnapshotRefV2 = z.infer<typeof runtimeContextSnapshotRefV2Schema>;
export type EffectiveExecutionSnapshotRefV2 = z.infer<typeof effectiveExecutionSnapshotRefV2Schema>;
export type ProjectHeadRefV2 = z.infer<typeof projectHeadRefV2Schema>;
export type TaskAdmissionRefV2 = z.infer<typeof taskAdmissionRefV2Schema>;
export type AttemptRefV2 = z.infer<typeof attemptRefV2Schema>;
export type SuccessorTransitionRefV2 = z.infer<typeof successorTransitionRefV2Schema>;
export type SuccessorTransitionV2 = z.infer<typeof successorTransitionV2Schema>;
export type TaskSubmitRequestV2 = z.input<typeof taskSubmitRequestV2Schema>;
export type TaskActionV2 = z.input<typeof taskActionV2Schema>;
export type TaskV2 = z.infer<typeof taskV2Schema>;
export type TaskPageV2 = z.infer<typeof taskPageV2Schema>;
export type CoreEventEnvelopeV2 = z.infer<typeof coreEventEnvelopeV2Schema>;
export type TimelinePageV2 = z.infer<typeof timelinePageV2Schema>;
export type TaskContextV2 = z.infer<typeof taskContextV2Schema>;
export type ArtifactV2 = z.infer<typeof artifactV2Schema>;
export type ArtifactPageV2 = z.infer<typeof artifactPageV2Schema>;
export type ArtifactContentV2 = z.infer<typeof artifactContentV2Schema>;
export type ArtifactDiffV2 = z.infer<typeof artifactDiffV2Schema>;
export type ServiceV2 = z.infer<typeof serviceV2Schema>;
export type ServicePageV2 = z.infer<typeof servicePageV2Schema>;
export type ServiceRestartV2 = z.input<typeof serviceRestartV2Schema>;
export type DiagnosticRequestV2 = z.input<typeof diagnosticRequestV2Schema>;
export type DiagnosticV2 = z.infer<typeof diagnosticV2Schema>;
export type DesktopEventEnvelopeV2 = z.infer<typeof desktopEventEnvelopeV2Schema>;
export type DesktopEventTypeV2 = z.infer<typeof desktopEventTypeV2Schema>;
export type DesktopSseFrameV2 = z.infer<typeof desktopSseFrameV2Schema>;
export type EvolutionCapabilitiesV2 = z.infer<typeof evolutionCapabilitiesV2Schema>;
export type CursorPageV2<T> = { schema_version: "2"; items: T[]; next_cursor: string | null; has_more: boolean };

function validateLifecycleProgressV2(
  value: { completed: number; total: number },
  context: z.RefinementCtx,
): void {
  if (value.completed > value.total) issue(context, ["completed"], "completed progress exceeds total");
}

function validateLifecycleIdentityAndPhaseV2(
  value: {
    kind: z.output<typeof lifecycleOperationKindV2Schema>;
    resource: z.output<typeof lifecycleResourceRefV2Schema>;
    phase: z.output<typeof lifecyclePhaseV2Schema>;
    phase_index: number;
    phase_total: number;
  },
  result: z.output<typeof lifecycleResultV2Schema> | null,
  context: z.RefinementCtx,
): void {
  const expectedResourceKind = ["profile_connect", "profile_disconnect", "host_key_review"].includes(value.kind)
    ? "profile"
    : value.kind === "native_workspace_prepare" ? "native_workspace" : "project";
  if (value.resource.resource_kind !== expectedResourceKind) {
    issue(context, ["resource", "resource_kind"], "operation kind and resource kind do not match");
  }
  if (value.phase_total !== LIFECYCLE_PHASES_V2.length) {
    issue(context, ["phase_total"], "lifecycle phase total differs from the fixed phase plan");
  }
  if (LIFECYCLE_PHASES_V2[value.phase_index] !== value.phase) {
    issue(context, ["phase_index"], "lifecycle phase index differs from the fixed phase plan");
  }
  if (result === null) return;
  if (result.result_kind !== expectedResourceKind) {
    issue(context, ["result", "result_kind"], "operation resource and result kind do not match");
    return;
  }
  if (value.kind === "project_create") return;
  const resultResourceId = result.result_kind === "profile"
    ? result.profile_id
    : result.result_kind === "native_workspace" ? result.import_id : result.project_id;
  if (resultResourceId !== value.resource.resource_id) {
    issue(context, ["result"], "operation result belongs to another resource");
  }
}

function isSafeLifecycleLogTextV2(text: string): boolean {
  return !Array.from(text).some((character) => {
    const point = character.codePointAt(0)!;
    return (point < 0x20 && character !== "\n" && character !== "\t")
      || (point >= 0x7f && point <= 0x9f);
  });
}

export function compareUtcTimestampsV2(left: string, right: string): number {
  const canonicalLeft = canonicalUtcTimestampV2(left);
  const canonicalRight = canonicalUtcTimestampV2(right);
  if (canonicalLeft === null || canonicalRight === null) {
    throw new RangeError("invalid UTC timestamp");
  }
  return canonicalLeft < canonicalRight ? -1 : canonicalLeft > canonicalRight ? 1 : 0;
}

function canonicalUtcTimestampV2(value: string): string | null {
  const match = UTC_RFC3339_COMPONENTS.exec(value);
  if (match === null) return null;
  const [year, month, day, hour, minute, second] = match.slice(1, 7).map(Number);
  if (year === undefined || month === undefined || day === undefined
    || hour === undefined || minute === undefined || second === undefined
    || year === 0 || month < 1 || month > 12 || hour > 23 || minute > 59 || second > 59) {
    return null;
  }
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const monthDays = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31] as const;
  if (day < 1 || day > monthDays[month - 1]!) return null;
  const fraction = (match[7] ?? "").padEnd(9, "0");
  return `${match[1]}-${match[2]}-${match[3]}T${match[4]}:${match[5]}:${match[6]}.${fraction}Z`;
}

function noControlCharacters(value: string): boolean {
  return !CONTROL_CHARACTERS.test(value);
}

function noUnsafeMultilineControlCharacters(value: string): boolean {
  return !UNSAFE_MULTILINE_CONTROL_CHARACTERS.test(value);
}

function isSafeModelReference(value: string): boolean {
  return value === value.trim()
    && !CONTROL_CHARACTERS.test(value)
    && !value.includes("://")
    && !/^[A-Za-z][A-Za-z0-9+.-]*:/.test(value)
    && !value.startsWith("/")
    && !value.startsWith("\\")
    && !value.startsWith(".")
    && !value.startsWith("~")
    && !value.includes("\\")
    && !value.split("/").some((part) => part === "." || part === "..");
}

function isLoopbackEndpointV2(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "http:"
      && ["127.0.0.1", "[::1]", "::1"].includes(url.hostname)
      && !url.username
      && !url.password
      && !url.search
      && !url.hash
      && (url.pathname === "" || url.pathname === "/");
  } catch {
    return false;
  }
}

function issue(context: z.RefinementCtx, path: (string | number)[], message: string): void {
  context.addIssue({ code: z.ZodIssueCode.custom, path, message });
}

function uniqueBy<T>(
  values: readonly T[],
  key: (value: T) => string,
  context: z.RefinementCtx,
  path: (string | number)[],
): void {
  const keys = values.map(key);
  if (new Set(keys).size !== keys.length) issue(context, path, "identities must be unique");
}

function isSortedUnique(values: readonly string[]): boolean {
  return new Set(values).size === values.length
    && values.every((value, index) => index === 0 || values[index - 1]! < value);
}

function isCanonicalBoundedJsonObject(value: string): boolean {
  try {
    const decoded: unknown = JSON.parse(value);
    if (decoded === null || Array.isArray(decoded) || typeof decoded !== "object") return false;
    const parsed = safeJsonObjectV2Schema.safeParse(decoded);
    return parsed.success && canonicalJsonV2(parsed.data) === value;
  } catch {
    return false;
  }
}

function validateBoundedJsonV2(value: Record<string, SafeJsonValueV2>, context: z.RefinementCtx): void {
  let nodes = 0;
  let collectionItems = 0;
  let textBytes = 0;
  const pending: Array<[SafeJsonValueV2, number, (string | number)[]]> = [[value, 1, []]];
  while (pending.length > 0) {
    const [current, depth, path] = pending.pop()!;
    nodes += 1;
    if (nodes > MAX_JSON_NODES_V2) return issue(context, path, "JSON exceeds the node budget");
    if (depth > MAX_JSON_DEPTH_V2) return issue(context, path, "JSON exceeds the depth budget");
    if (Array.isArray(current)) {
      collectionItems += current.length;
      if (current.length > MAX_JSON_COLLECTION_ITEMS_V2) return issue(context, path, "JSON array exceeds the item budget");
      current.forEach((child, index) => pending.push([child, depth + 1, [...path, index]]));
    } else if (current !== null && typeof current === "object") {
      const entries = Object.entries(current);
      collectionItems += entries.length;
      if (entries.length > MAX_JSON_COLLECTION_ITEMS_V2) return issue(context, path, "JSON object exceeds the item budget");
      for (const [key, child] of entries) {
        if (!key || key.length > 256 || key !== key.trim() || CONTROL_CHARACTERS.test(key)) {
          return issue(context, [...path, key], "JSON keys must be short trimmed strings");
        }
        textBytes += utf8ByteLength(key);
        pending.push([child, depth + 1, [...path, key]]);
      }
    } else if (typeof current === "string") {
      textBytes += utf8ByteLength(current);
    }
    if (collectionItems > 4_096) return issue(context, path, "JSON exceeds the collection budget");
    if (textBytes > MAX_JSON_TEXT_BYTES_V2) return issue(context, path, "JSON exceeds the text budget");
  }
  if (utf8ByteLength(canonicalJsonV2(value)) > MAX_PROJECT_CONFIG_BYTES_V2) issue(context, [], "JSON exceeds the byte budget");
}

function asciiJsonString(value: string): string {
  return JSON.stringify(value).replace(/[\u007f-\uffff]/g, (character) =>
    `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
}

function compareUnicodeCodePoints(left: string, right: string): number {
  const leftPoints = Array.from(left, (character) => character.codePointAt(0)!);
  const rightPoints = Array.from(right, (character) => character.codePointAt(0)!);
  const length = Math.min(leftPoints.length, rightPoints.length);
  for (let index = 0; index < length; index += 1) {
    if (leftPoints[index] !== rightPoints[index]) return leftPoints[index]! - rightPoints[index]!;
  }
  return leftPoints.length - rightPoints.length;
}

function sha256Bytes(input: Uint8Array): string {
  const constants = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ];
  const state = [
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ];
  const paddedLength = Math.ceil((input.length + 9) / 64) * 64;
  const padded = new Uint8Array(paddedLength);
  padded.set(input);
  padded[input.length] = 0x80;
  const view = new DataView(padded.buffer);
  const bitLength = input.length * 8;
  view.setUint32(paddedLength - 8, Math.floor(bitLength / 0x1_0000_0000));
  view.setUint32(paddedLength - 4, bitLength >>> 0);
  const words = new Uint32Array(64);
  for (let offset = 0; offset < paddedLength; offset += 64) {
    for (let index = 0; index < 16; index += 1) words[index] = view.getUint32(offset + index * 4);
    for (let index = 16; index < 64; index += 1) {
      const word15 = words[index - 15]!;
      const word2 = words[index - 2]!;
      const sigma0 = rotateRight(word15, 7) ^ rotateRight(word15, 18) ^ (word15 >>> 3);
      const sigma1 = rotateRight(word2, 17) ^ rotateRight(word2, 19) ^ (word2 >>> 10);
      words[index] = (words[index - 16]! + sigma0 + words[index - 7]! + sigma1) >>> 0;
    }
    let [a, b, c, d, e, f, g, h] = state;
    for (let index = 0; index < 64; index += 1) {
      const sum1 = rotateRight(e!, 6) ^ rotateRight(e!, 11) ^ rotateRight(e!, 25);
      const choice = (e! & f!) ^ (~e! & g!);
      const first = (h! + sum1 + choice + constants[index]! + words[index]!) >>> 0;
      const sum0 = rotateRight(a!, 2) ^ rotateRight(a!, 13) ^ rotateRight(a!, 22);
      const majority = (a! & b!) ^ (a! & c!) ^ (b! & c!);
      const second = (sum0 + majority) >>> 0;
      h = g;
      g = f;
      f = e;
      e = (d! + first) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (first + second) >>> 0;
    }
    state[0] = (state[0]! + a!) >>> 0;
    state[1] = (state[1]! + b!) >>> 0;
    state[2] = (state[2]! + c!) >>> 0;
    state[3] = (state[3]! + d!) >>> 0;
    state[4] = (state[4]! + e!) >>> 0;
    state[5] = (state[5]! + f!) >>> 0;
    state[6] = (state[6]! + g!) >>> 0;
    state[7] = (state[7]! + h!) >>> 0;
  }
  return state.map((word) => word.toString(16).padStart(8, "0")).join("");
}

function rotateRight(value: number, shift: number): number {
  return (value >>> shift) | (value << (32 - shift));
}

function utf8ByteLength(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}
