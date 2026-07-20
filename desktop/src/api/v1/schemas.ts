import { z } from "zod";
import { providerKindSchema } from "./providerKinds";

export { providerKindSchema } from "./providerKinds";

export const MAX_PAGE_SIZE = 100;
export const MAX_JSON_DEPTH = 16;
export const MAX_JSON_NODES = 8_192;
export const MAX_JSON_COLLECTION_ITEMS = 1_024;
export const MAX_JSON_TEXT_BYTES = 262_144;
export const MAX_JSON_TOTAL_BYTES = 1_048_576;
export const MAX_SAFE_INTEGER = Number.MAX_SAFE_INTEGER;

const MAX_ARTIFACT_PREVIEW_BYTES = 2 * 1024 * 1024;
const MAX_ARTIFACT_PREVIEW_DOCUMENTS = 128;
const MAX_ARTIFACT_DIFF_HUNKS = 128;
const MAX_ARTIFACT_DIFF_LINES = 8_192;
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f]/;
const UTC_RFC3339 = /^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,9})?Z$/;
const CORE_UTC_RFC3339 = /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z$/;
const SHA256 = /^[0-9a-f]{64}$/;
const STABLE_ID = /^[A-Za-z][A-Za-z0-9_.-]{0,127}$/;
const CODEX_PROVIDER_PREFIXES = ["gcp/google/", "openai/", "anthropic/", "google/"] as const;

function codexCliModelName(model: string): string {
  const prefix = CODEX_PROVIDER_PREFIXES.find((candidate) => model.startsWith(candidate));
  return prefix ? model.slice(prefix.length) : model;
}

function isValidCodexCliModel(model: string): boolean {
  const cliModel = codexCliModelName(model);
  return cliModel.length > 0
    && cliModel.length <= 128
    && /^[\x21-\x7e]+$/.test(cliModel)
    && cliModel !== "gpt-5";
}

export const schemaVersionV1Schema = z.literal("1").default("1");
export const opaqueIdSchema = z
  .string()
  .min(1)
  .max(256)
  .refine((value) => value === value.trim() && !CONTROL_CHARACTERS.test(value), "must be trimmed text without control characters");
export const shortTextSchema = z.string().min(1).max(512).refine((value) => !value.includes("\0"));
const projectDisplayNameSchema = z.string().min(1).max(128).refine((value) => !value.includes("\0"));
const localCoreShortTextSchema = z.string().min(1).max(256).refine((value) => !value.includes("\0"));
export const longTextSchema = z.string().min(1).max(65_536).refine((value) => !value.includes("\0"));
export const utcTimestampSchema = z.string().regex(UTC_RFC3339, "must be a UTC RFC 3339 timestamp");
export const sha256DigestSchema = z.string().regex(SHA256, "must be lowercase SHA-256 hex");
export const etagSchema = z.string().regex(/^"[0-9a-f]{64}"$/);
export const executionModeV1Schema = z.enum(["codex_subscription_transcript", "self-deployed"]);
export const codexReasoningEffortV1Schema = z.enum(["low", "medium", "high", "xhigh"]);
const executionModeReasonCodeV1Schema = z.enum([
  "self_deployed_release_unavailable",
  "execution_mode_release_unsupported",
]);
export const executionModeCapabilityV1Schema = z
  .object({
    mode: executionModeV1Schema,
    display_name: shortTextSchema,
    support_state: z.enum(["supported", "unavailable", "unsupported"]),
    reason_code: executionModeReasonCodeV1Schema.nullable().default(null),
    message: shortTextSchema,
  })
  .strict()
  .superRefine((value, context) => {
    if (value.support_state === "supported" && value.reason_code !== null) {
      issue(context, ["reason_code"], "supported execution modes cannot include a reason code");
    }
    if (value.support_state !== "supported" && value.reason_code === null) {
      issue(context, ["reason_code"], "unavailable and unsupported execution modes require a reason code");
    }
  });
export const executionModeCapabilitiesV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    modes: z.array(executionModeCapabilityV1Schema).length(2),
  })
  .strict()
  .superRefine((value, context) => {
    const modes = value.modes.map((capability) => capability.mode);
    if (new Set(modes).size !== modes.length) {
      issue(context, ["modes"], "execution mode capabilities must not contain duplicates");
    }
    if (!["codex_subscription_transcript", "self-deployed"].every((mode) => modes.includes(mode as typeof modes[number]))) {
      issue(context, ["modes"], "execution mode capabilities must contain every known mode exactly once");
    }
  });

const coreOpaqueIdSchema = z
  .string()
  .min(1)
  .max(128)
  .regex(/^[^\u0000-\u0020\u007f](?:[^\u0000-\u001f\u007f]*[^\u0000-\u0020\u007f])?$/);
const coreShortTextSchema = z.string().min(1).max(256);
const displayNameSchema = z.string().min(1).max(128);
const descriptionSchema = z.string().min(1).max(4_096);
const coreLogTextSchema = z.string().max(16_384);
const contentTextSchema = z.string().max(2 * 1024 * 1024);
const coreUtcTimestampSchema = z.string().regex(CORE_UTC_RFC3339);
const agentModelRefSchema = z
  .string()
  .min(1)
  .max(256)
  .regex(/^[^\u0000-\u0020\u007f](?:[^\u0000-\u001f\u007f]*[^\u0000-\u0020\u007f])?$/);
const stableIdSchema = z.string().regex(STABLE_ID);
const safeIntegerSchema = z.number().int().safe();
const nonNegativeSafeIntegerSchema = safeIntegerSchema.min(0);
const mimeTypeSchema = z.string().max(127).regex(/^[a-z0-9][a-z0-9!#$&^_.+-]*\/[a-z0-9][a-z0-9!#$&^_.+-]*$/);
const huggingFaceModelSchema = z
  .string()
  .min(1)
  .max(256)
  .refine((value) => value === value.trim() && !CONTROL_CHARACTERS.test(value), "hf_model must be trimmed text without control characters");

export type SafeJsonValue = null | boolean | number | string | SafeJsonValue[] | { [key: string]: SafeJsonValue };

const safeJsonScalarSchema = z.union([
  z.null(),
  z.boolean(),
  z.number().finite().refine((value) => !Number.isInteger(value) || Number.isSafeInteger(value)),
  z.string(),
]);
export const safeJsonValueSchema: z.ZodType<SafeJsonValue> = z.lazy(() =>
  z.union([
    safeJsonScalarSchema,
    z.array(safeJsonValueSchema).max(MAX_JSON_COLLECTION_ITEMS),
    z.record(z.string(), safeJsonValueSchema),
  ]),
);
export const safeJsonObjectSchema: z.ZodType<Record<string, SafeJsonValue>> = z
  .record(z.string(), safeJsonValueSchema)
  .superRefine((value, context) => validateBoundedJson(value, context));

export const featureFlagV1Schema = z.enum([
  "remote_profiles",
  "project_validation",
  "operation_events",
  "run_observability",
  "artifact_inspection",
  "service_control",
  "diagnostics",
  "maintenance",
]);

export const versionInfoV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    api_name: z.literal("openevo-desktop-local-api").default("openevo-desktop-local-api"),
    preferred_major: z.literal(1).default(1),
    supported_majors: z.array(z.literal(1)).min(1).default([1]),
    openapi_sha256: sha256DigestSchema,
    build_version: shortTextSchema,
    source_commit: z.string().regex(/^[0-9a-f]{7,40}$/),
    build_channel: z.enum(["release", "development", "test"]),
    provider_kind: providerKindSchema,
    feature_flags: z.array(featureFlagV1Schema).default([]),
  })
  .strict()
  .superRefine((value, context) => {
    if (!value.supported_majors.includes(value.preferred_major)) issue(context, ["preferred_major"], "preferred major must be supported");
    if (new Set(value.feature_flags).size !== value.feature_flags.length) issue(context, ["feature_flags"], "feature flags must be unique");
    if (value.build_channel === "release" && value.provider_kind !== "desktop_sidecar") issue(context, ["provider_kind"], "release builds require the real sidecar");
  });

export const negotiatedContractV1Schema = z
  .object({
    major: z.literal(1),
    openapi_sha256: sha256DigestSchema,
    provider_kind: providerKindSchema,
    feature_flags: z.array(featureFlagV1Schema),
  })
  .strict();
export const desktopBootstrapContextV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    endpoint: z.string().url().refine(isLoopbackEndpoint, "sidecar endpoint must be an unauthenticated loopback HTTP URL"),
    session_token: z.string().min(32).max(4_096).refine((value) => !CONTROL_CHARACTERS.test(value)),
    negotiated_contract: negotiatedContractV1Schema,
  })
  .strict();

export const healthV1Schema = z
  .object({
    service: z.literal("openevo-sidecar").default("openevo-sidecar"),
    status: z.enum(["ok", "degraded", "starting"]),
    protocol: z.literal("openevo-native-sidecar-v1").nullable().default(null),
    instance_id: z.string().regex(/^[0-9a-f]{32}$/).nullable().default(null),
    instance_proof: sha256DigestSchema.nullable().default(null),
  })
  .strict()
  .superRefine((value, context) => {
    const proof = [value.protocol, value.instance_id, value.instance_proof];
    const present = proof.filter((entry) => entry !== null).length;
    if (present !== 0 && present !== proof.length) issue(context, ["protocol"], "native health proof fields must be atomic");
    if (present === proof.length && value.status !== "ok") issue(context, ["status"], "native readiness proof requires ok status");
  });

const errorSeveritySchema = z.enum(["info", "warning", "blocking"]);
const errorCategorySchema = z.enum(["environment", "project", "run", "artifact", "service", "authentication", "contract", "internal"]);
const repairActionSchema = z.enum([
  "openevo_can_retry",
  "openevo_can_install",
  "openevo_can_reconfigure",
  "user_action_required",
  "unsupported",
]);
const errorFieldIssueV1Schema = z.object({ field: z.string().min(1).max(256), issue: coreShortTextSchema }).strict();
const errorConflictV1Schema = z.object({ resource_type: z.string().min(1).max(64), resource_id: coreOpaqueIdSchema }).strict();
const apiErrorDetailsV1Schema = z
  .object({
    field_issues: z.array(errorFieldIssueV1Schema).max(64).default([]),
    conflicts: z.array(errorConflictV1Schema).max(32).default([]),
  })
  .strict();
export const apiErrorV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    request_id: coreOpaqueIdSchema,
    code: z.string().min(1).max(128).regex(/^[a-z][a-z0-9_]*$/),
    http_status: z.number().int().min(400).max(599),
    message: descriptionSchema,
    severity: errorSeveritySchema,
    category: errorCategorySchema,
    retryable: z.boolean(),
    repair_action: repairActionSchema,
    next_action: descriptionSchema,
    details: apiErrorDetailsV1Schema.default({}),
    logs_ref: coreOpaqueIdSchema.nullable().default(null),
  })
  .strict();

export const contractNegotiationV1Schema = z
  .object({ selected_major: z.literal(1), desktop_openapi_sha256: sha256DigestSchema, core_openapi_sha256: sha256DigestSchema.nullable().default(null), compatible: z.boolean() })
  .strict();
export const hostKeyReviewV1Schema = z
  .object({ algorithm: z.enum(["ssh-ed25519", "ecdsa-sha2-nistp256", "rsa-sha2-512"]), fingerprint: z.string().regex(/^SHA256:[A-Za-z0-9+/]{20,88}={0,2}$/) })
  .strict();
export const coreCompatibilityV1Schema = z
  .object({ contract_version: schemaVersionV1Schema, contract_digest: sha256DigestSchema, core_version: shortTextSchema })
  .strict();
export const connectionFailureV1Schema = z
  .object({ code: z.string().regex(/^[a-z][a-z0-9_]{0,127}$/), message: shortTextSchema, retryable: z.boolean(), next_action: shortTextSchema.nullable().default(null) })
  .strict();
export const coreConnectionStateV1Schema = z
  .object({
    state: z.enum(["disconnected", "connecting", "host_key_review", "checking", "bootstrapping", "core_starting", "online", "degraded", "reconnecting", "offline"]),
    profile_id: opaqueIdSchema.nullable().default(null),
    active_tunnel: z.boolean(),
    operation_id: opaqueIdSchema.nullable().default(null),
    host_key_review: hostKeyReviewV1Schema.nullable().default(null),
    core: coreCompatibilityV1Schema.nullable().default(null),
    failure: connectionFailureV1Schema.nullable().default(null),
  })
  .strict()
  .superRefine((value, context) => {
    const operationStates = new Set(["connecting", "host_key_review", "checking", "bootstrapping", "core_starting", "reconnecting"]);
    const activeStates = new Set([...operationStates, "online", "degraded", "offline"]);
    if (operationStates.has(value.state) && value.operation_id === null) issue(context, ["operation_id"], `${value.state} requires an operation ID`);
    if (activeStates.has(value.state) && value.profile_id === null) issue(context, ["profile_id"], `${value.state} requires a profile`);
    if ((value.state === "host_key_review") !== (value.host_key_review !== null)) issue(context, ["host_key_review"], "host-key review data must agree with state");
    if (value.state === "online" && (!value.active_tunnel || value.core === null)) issue(context, ["core"], "online requires an active tunnel and compatible Core");
    if (!["online", "degraded", "reconnecting"].includes(value.state) && value.core !== null) issue(context, ["core"], "Core metadata is invalid before compatibility succeeds");
    if (["degraded", "offline"].includes(value.state) !== (value.failure !== null)) issue(context, ["failure"], "typed failure must agree with connection state");
    if (["disconnected", "offline"].includes(value.state) && value.active_tunnel) issue(context, ["active_tunnel"], "offline state cannot have an active tunnel");
  });
export const activeProjectStateV1Schema = z
  .object({ project_id: opaqueIdSchema, project_etag: etagSchema, profile_id: opaqueIdSchema, connection_state: z.enum(["offline", "connecting", "ready", "blocked"]) })
  .strict();
export const desktopStateV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    observed_at: utcTimestampSchema,
    contract: contractNegotiationV1Schema,
    execution_mode_capabilities: executionModeCapabilitiesV1Schema,
    core: coreConnectionStateV1Schema,
    active_project: activeProjectStateV1Schema.nullable().default(null),
    pending_operation_ids: z.array(opaqueIdSchema).default([]),
  })
  .strict();

export const credentialSlotStatusSchema = z
  .object({
    kind: z.enum(["ssh_password", "ssh_private_key", "ssh_private_key_passphrase", "http_proxy_password", "https_proxy_password", "hugging_face_token"]),
    status: z.enum(["empty", "stored", "unavailable"]),
    updated_at: utcTimestampSchema.nullable().default(null),
  })
  .strict();
export const networkProxyV1Schema = z
  .object({ http_url: z.string().nullable().default(null), https_url: z.string().nullable().default(null), no_proxy: z.array(shortTextSchema).default([]) })
  .strict()
  .superRefine((value, context) => {
    for (const key of ["http_url", "https_url"] as const) {
      const url = value[key];
      if (url !== null && !isSafeProxyUrl(url)) issue(context, [key], "proxy URL must contain only an HTTP(S) origin without user information");
    }
  });
const authenticationKindSchema = z.enum(["ssh_agent", "native_private_key", "native_password"]);
const networkHostSchema = z.string().min(1).max(253).refine(isNetworkHost, "host must be a valid hostname or IP address");
const remoteUserSchema = z.string().min(1).max(128).regex(/^[A-Za-z0-9._-]+$/, "user must be a remote account name, not a path");
const remoteProfileFields = {
  name: shortTextSchema,
  host: networkHostSchema,
  port: z.number().int().min(1).max(65_535).default(22),
  user: remoteUserSchema,
  authentication_kind: authenticationKindSchema.default("ssh_agent"),
  proxy: networkProxyV1Schema.default({}),
};
export const profileCreateV1Schema = z.object(remoteProfileFields).strict();
export const profilePatchV1Schema = z.object(remoteProfileFields).partial().strict().refine((value) => Object.keys(value).length > 0, "profile patch must not be empty");
export const remoteProfileV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    profile_id: opaqueIdSchema,
    ...remoteProfileFields,
    credential_slots: z.array(credentialSlotStatusSchema).default([]),
    connection_state: z.enum(["disconnected", "connecting", "host_key_required", "connected", "failed"]).default("disconnected"),
    host_key_fingerprint: shortTextSchema.nullable().default(null),
    etag: etagSchema,
    created_at: utcTimestampSchema,
    updated_at: utcTimestampSchema,
  })
  .strict()
  .superRefine((value, context) => {
    const kinds = value.credential_slots.map((slot) => slot.kind);
    if (new Set(kinds).size !== kinds.length) issue(context, ["credential_slots"], "credential slot kinds must be unique");
  });
export const hostKeyAcceptV1Schema = z
  .object({ algorithm: z.enum(["ssh-ed25519", "ecdsa-sha2-nistp256", "rsa-sha2-512"]), fingerprint: z.string().regex(/^SHA256:[A-Za-z0-9+/]{20,88}={0,2}$/) })
  .strict();

export const executionSettingsV1Schema = z
  .object({
    mode: executionModeV1Schema,
    capture_mode: z.literal("transcript").default("transcript"),
    token_level_metrics_available: z.literal(false).default(false),
    codex_model: localCoreShortTextSchema.nullable().default(null),
    reasoning_effort: codexReasoningEffortV1Schema.nullable().default(null),
    hf_model: huggingFaceModelSchema.nullable().default(null),
  })
  .strict()
  .superRefine((value, context) => {
    const subscription = value.mode === "codex_subscription_transcript";
    if (subscription !== (value.codex_model !== null) || subscription === (value.hf_model !== null)) issue(context, [], "execution mode and model fields do not agree");
    if (subscription && value.codex_model !== null && !isValidCodexCliModel(value.codex_model)) {
      issue(context, ["codex_model"], "Codex model is not executable after provider normalization");
    }
    if (!subscription && value.reasoning_effort !== null) issue(context, ["reasoning_effort"], "reasoning effort is only valid for Codex subscription mode");
  });
export const projectTaskV1Schema = z.object({ title: localCoreShortTextSchema, objective: longTextSchema }).strict();
export const workspaceImportRefV1Schema = z
  .object({
    import_id: opaqueIdSchema,
    content_sha256: sha256DigestSchema,
    byte_size: safeIntegerSchema.min(1_024).max(16 * 1024 * 1024 * 1024),
    entry_count: safeIntegerSchema.min(0).max(100_000),
    extracted_byte_size: safeIntegerSchema.min(0).max(16 * 1024 * 1024 * 1024),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.byte_size % 512 !== 0) issue(context, ["byte_size"], "workspace import size must align to a tar block");
    if (value.entry_count === 0 && value.extracted_byte_size !== 0) issue(context, ["extracted_byte_size"], "empty import cannot declare extracted bytes");
  });
export const projectSourceV1Schema = z
  .object({
    kind: z.enum(["scratch", "native_folder_snapshot"]),
    display_name: localCoreShortTextSchema,
    import_ref: workspaceImportRefV1Schema.nullable().default(null),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.kind === "scratch" && value.import_ref !== null) issue(context, ["import_ref"], "scratch sources must not include import_ref");
    if (value.kind === "native_folder_snapshot" && value.import_ref === null) issue(context, ["import_ref"], "native folder sources require an opaque import_ref");
  });
export const evolutionTargetSelectionV1Schema = z
  .object({ enabled: z.boolean(), method: stableIdSchema.nullable().default(null), config: safeJsonObjectSchema.default({}) })
  .strict()
  .refine((value) => !value.enabled || value.method !== null, { path: ["method"], message: "enabled targets require a method" });
export const evolutionSelectionsV1Schema = z
  .record(stableIdSchema, evolutionTargetSelectionV1Schema)
  .refine((value) => Object.keys(value).length <= 128, "at most 128 evolution targets are allowed");
export const evolutionConfigV1Schema = z
  .object({ targets: evolutionSelectionsV1Schema })
  .strict()
  .refine((value) => utf8ByteLength(JSON.stringify(value)) <= 1_048_576, "evolution config exceeds the aggregate byte budget");
export const evolutionConfigurationStateV1Schema = z.enum(["pending", "configured"]);
const projectFields = {
  name: projectDisplayNameSchema,
  profile_id: opaqueIdSchema,
  task: projectTaskV1Schema,
  source: projectSourceV1Schema,
  execution: executionSettingsV1Schema,
  evolution: evolutionConfigV1Schema,
  evolution_configuration_state: evolutionConfigurationStateV1Schema,
};
export const projectCreateV1Schema = z.object({
  ...projectFields,
  evolution_configuration_state: evolutionConfigurationStateV1Schema.default("configured"),
}).strict();
export const projectPatchV1Schema = z.object(projectFields).partial().strict().refine((value) => Object.keys(value).length > 0, "project patch must not be empty");

export const revisionRefV1Schema = z
  .object({
    id: coreOpaqueIdSchema,
    project_id: coreOpaqueIdSchema,
    generation: nonNegativeSafeIntegerSchema,
    manifest_sha256: sha256DigestSchema,
  })
  .strict();
export const modelPreparationV1Schema = z
  .object({
    model_ref: agentModelRefSchema,
    status: z.enum(["unresolved", "downloading", "ready", "failed"]),
    downloaded_bytes: nonNegativeSafeIntegerSchema.nullable().default(null),
    total_bytes: nonNegativeSafeIntegerSchema.nullable().default(null),
    error: apiErrorV1Schema.nullable().default(null),
    updated_at: coreUtcTimestampSchema,
  })
  .strict()
  .superRefine((value, context) => {
    const progressKnown = value.downloaded_bytes !== null || value.total_bytes !== null;
    if (progressKnown && (value.downloaded_bytes === null || value.total_bytes === null)) {
      issue(context, ["downloaded_bytes"], "downloaded_bytes and total_bytes must appear together");
    }
    if (value.downloaded_bytes !== null && value.total_bytes !== null && value.downloaded_bytes > value.total_bytes) issue(context, ["downloaded_bytes"], "downloaded_bytes exceeds total_bytes");
    if ((value.status === "failed") !== (value.error !== null)) issue(context, ["error"], "error is required only for failed model preparation");
    if (value.status === "unresolved" && progressKnown) issue(context, ["downloaded_bytes"], "unresolved model preparation cannot report progress");
    if (value.status === "downloading") {
      if (!progressKnown) issue(context, ["downloaded_bytes"], "downloading model preparation requires progress");
      if (value.downloaded_bytes !== null && value.downloaded_bytes === value.total_bytes) issue(context, ["status"], "completed progress must use ready status");
    }
    if (value.status === "ready" && progressKnown && value.downloaded_bytes !== value.total_bytes) {
      issue(context, ["downloaded_bytes"], "ready model preparation requires complete progress");
    }
  });
export const remoteProjectStateV1Schema = z
  .object({
    core_project_id: opaqueIdSchema,
    status: z.enum(["draft", "ready", "blocked", "archived"]),
    active_revision: revisionRefV1Schema.nullable().default(null),
    registry_digest: sha256DigestSchema.nullable().default(null),
    model_preparation: modelPreparationV1Schema,
    observed_at: utcTimestampSchema,
    etag: etagSchema,
  })
  .strict()
  .superRefine((value, context) => {
    if (value.status === "ready" && (value.active_revision === null || value.registry_digest === null || value.model_preparation.status !== "ready")) issue(context, ["status"], "ready remote projects require a revision, registry, and prepared model");
    if (value.active_revision !== null && value.active_revision.project_id !== value.core_project_id) issue(context, ["active_revision"], "remote project revision belongs to another project");
  });
export const projectV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    project_id: opaqueIdSchema,
    ...projectFields,
    state: z.enum(["draft", "active", "archived", "blocked"]),
    remote: remoteProjectStateV1Schema.nullable().default(null),
    etag: etagSchema,
    created_at: utcTimestampSchema,
    updated_at: utcTimestampSchema,
  })
  .strict();

export const resourceRefV1Schema = z
  .object({ resource_type: z.enum(["profile", "project", "operation", "run", "artifact", "service", "diagnostic", "maintenance"]), resource_id: opaqueIdSchema })
  .strict();
export const operationProgressV1Schema = z
  .object({ current: nonNegativeSafeIntegerSchema, total: safeIntegerSchema.min(1), label: shortTextSchema })
  .strict()
  .refine((value) => value.current <= value.total, { path: ["current"], message: "current must not exceed total" });
export const normalizedCheckV1Schema = z
  .object({
    check_id: opaqueIdSchema,
    label: shortTextSchema,
    status: z.enum(["pending", "running", "passed", "warning", "failed", "skipped"]),
    summary: shortTextSchema,
    repair_action: z.enum(["none", "openevo_can_retry", "user_input_required", "reconnect_required"]).default("none"),
  })
  .strict();
export const diagnosticFindingV1Schema = z
  .object({ finding_id: opaqueIdSchema, severity: z.enum(["info", "warning", "blocking"]), category: z.enum(["desktop", "ssh", "core", "model_service", "workspace", "run", "evolution"]), summary: shortTextSchema, next_action: shortTextSchema.nullable().default(null) })
  .strict();
export const desktopDiagnosticReportV1Schema = z
  .object({ schema_version: schemaVersionV1Schema, diagnostic_id: opaqueIdSchema, status: z.enum(["healthy", "degraded", "blocked"]), generated_at: utcTimestampSchema, checks: z.array(normalizedCheckV1Schema), findings: z.array(diagnosticFindingV1Schema), etag: etagSchema })
  .strict();
const localOperationResultV1Schema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("connection"), profile_id: opaqueIdSchema, connection_state: z.enum(["connected", "disconnected", "host_key_required"]) }).strict(),
  z.object({ kind: z.literal("project"), project_id: opaqueIdSchema, project_etag: etagSchema, active: z.boolean() }).strict(),
  z.object({ kind: z.literal("diagnostic"), report: desktopDiagnosticReportV1Schema }).strict(),
]);
export const localOperationV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    operation_id: opaqueIdSchema,
    operation_kind: z.enum(["profile_connect", "profile_disconnect", "host_key_accept", "project_activate", "project_doctor", "project_repair", "bootstrap", "workspace_sync"]),
    state: z.enum(["queued", "running", "succeeded", "failed", "cancelling", "cancelled"]),
    resource: resourceRefV1Schema,
    progress: operationProgressV1Schema.nullable().default(null),
    checks: z.array(normalizedCheckV1Schema).default([]),
    result: localOperationResultV1Schema.nullable().default(null),
    error: apiErrorV1Schema.nullable().default(null),
    created_at: utcTimestampSchema,
    started_at: utcTimestampSchema.nullable().default(null),
    finished_at: utcTimestampSchema.nullable().default(null),
    etag: etagSchema,
  })
  .strict()
  .superRefine((value, context) => {
    const terminal = ["succeeded", "failed", "cancelled"].includes(value.state);
    if (terminal !== (value.finished_at !== null)) issue(context, ["finished_at"], "terminal state and finished_at must agree");
    if ((value.state === "failed") !== (value.error !== null)) issue(context, ["error"], "error is required exactly for failed operations");
  });
export const localLogEntryV1Schema = z
  .object({
    log_id: opaqueIdSchema,
    occurred_at: utcTimestampSchema,
    level: z.enum(["debug", "info", "warning", "error"]),
    source: z.enum(["desktop", "connection", "core", "run", "evolution", "service"]),
    message: longTextSchema,
    code: shortTextSchema.nullable().default(null),
  })
  .strict();

const axisSupportV1Schema = z
  .object({
    state: z.enum(["supported", "unsupported", "unavailable"]),
    reason_code: stableIdSchema.nullable().default(null),
    message: z.string().max(4_096).refine((value) => value.trim().length > 0),
    missing_requirements: z.array(stableIdSchema).max(256).default([]),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.state === "supported" && (value.reason_code !== null || value.missing_requirements.length > 0)) issue(context, ["state"], "supported axis cannot include a failure reason");
    if (value.state !== "supported" && value.reason_code === null) issue(context, ["reason_code"], "unsupported and unavailable axes require a reason code");
    if (new Set(value.missing_requirements).size !== value.missing_requirements.length) issue(context, ["missing_requirements"], "missing requirements must be unique");
  });
export const methodSupportV1Schema = z
  .object({
    overall: z.enum(["supported", "unsupported", "unavailable"]),
    execution: axisSupportV1Schema,
    capture: axisSupportV1Schema,
    harness: axisSupportV1Schema,
    runtime: axisSupportV1Schema,
  })
  .strict()
  .superRefine((value, context) => {
    const states = [value.execution.state, value.capture.state, value.harness.state, value.runtime.state];
    const expected = states.includes("unsupported") ? "unsupported" : states.includes("unavailable") ? "unavailable" : "supported";
    if (value.overall !== expected) issue(context, ["overall"], "overall support must match the four axes");
  });
export const methodInputBindingV1Schema = z
  .object({
    binding_id: stableIdSchema,
    source: z.enum(["current_dataset", "history_datasets", "current_target_artifacts", "explicit_inputs"]),
    artifact_type: stableIdSchema,
    min_count: nonNegativeSafeIntegerSchema.default(0),
    max_count: safeIntegerSchema.min(1).nullable().default(null),
  })
  .strict()
  .refine((value) => value.max_count === null || value.min_count <= value.max_count, { path: ["max_count"], message: "min_count must not exceed max_count" });
export const evolutionExecutionProfileV1Schema = z
  .object({
    execution_mode: z.enum(["subscription", "self_deployed"]),
    capture_mode: z.enum(["transcript", "token_level"]),
    harness_id: stableIdSchema,
    harness_capabilities: z.array(stableIdSchema).max(256).default([]),
    runtime_capabilities: z.array(stableIdSchema).max(256).default([]),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.execution_mode === "subscription" && value.capture_mode !== "transcript") issue(context, ["capture_mode"], "subscription execution requires transcript capture");
    for (const key of ["harness_capabilities", "runtime_capabilities"] as const) {
      if (new Set(value[key]).size !== value[key].length) issue(context, [key], `${key} must be unique`);
    }
  });
const canonicalJsonObjectStringSchema = z
  .string()
  .min(2)
  .refine((value) => utf8ByteLength(value) <= 262_144, "canonical JSON exceeds maximum bytes")
  .refine(isCanonicalJsonObject, "must contain a canonical JSON object with sorted keys");
export const evolutionResolvedMethodCapabilityV1Schema = z
  .object({ method_id: stableIdSchema, implementation_identity_digest: sha256DigestSchema, support: methodSupportV1Schema })
  .strict();
export const evolutionMethodCapabilityV1Schema = z
  .object({
    method_id: stableIdSchema,
    display_name: z.string().max(4_096).refine((value) => value.trim().length > 0),
    description: z.string().max(4_096).refine((value) => value.trim().length > 0),
    exposure: z.enum(["desktop", "maintainer", "internal"]),
    maturity: z.enum(["stable", "experimental"]),
    execution_modes: z.array(z.enum(["subscription", "self_deployed"])).max(2),
    capture_modes: z.array(z.enum(["transcript", "token_level"])).max(2),
    supported_harness_ids: z.array(z.string()).max(256),
    harness_requirements: z.array(z.string()).max(256),
    runtime_requirements: z.array(z.string()).max(256),
    input_bindings: z.array(methodInputBindingV1Schema).max(256),
    output_artifact_types: z.array(z.string()).max(256),
    config_schema_json: canonicalJsonObjectStringSchema,
    default_config_json: canonicalJsonObjectStringSchema,
    implementation_identity_digest: sha256DigestSchema,
    support: methodSupportV1Schema,
  })
  .strict();
export const evolutionSelectionResolverCapabilityV1Schema = z
  .object({
    selection_value: stableIdSchema,
    display_name: z.string().max(4_096).refine((value) => value.trim().length > 0),
    description: z.string().max(4_096).refine((value) => value.trim().length > 0),
    resolved_methods: z.array(evolutionResolvedMethodCapabilityV1Schema).min(1).max(256),
  })
  .strict()
  .superRefine((value, context) => uniqueSortedBy(value.resolved_methods, "method_id", context, ["resolved_methods"]));
export const evolutionTargetCapabilityV1Schema = z
  .object({
    target_id: stableIdSchema,
    display_name: z.string().max(4_096).refine((value) => value.trim().length > 0),
    description: z.string().max(4_096).refine((value) => value.trim().length > 0),
    artifact_type: stableIdSchema,
    exposure: z.enum(["desktop", "maintainer", "internal"]),
    maturity: z.enum(["stable", "experimental"]),
    handler_id: stableIdSchema,
    configured_default_method_id: stableIdSchema,
    effective_default_method_id: stableIdSchema.nullable(),
    configured_default_support: methodSupportV1Schema,
    renderer_kind: z.enum(["markdown", "file_bundle", "structured_summary", "adapter"]),
    renderer_contract_version: z.string().max(4_096).refine((value) => value.trim().length > 0),
    contribution_contract_version: z.string().max(4_096).refine((value) => value.trim().length > 0),
    context_order: z.number().int().min(0).max(10_000),
    implementation_identity_digest: sha256DigestSchema,
    handler_identity_digest: sha256DigestSchema,
    accepted_methods: z.array(evolutionResolvedMethodCapabilityV1Schema).min(1).max(256),
    selection_resolvers: z.array(evolutionSelectionResolverCapabilityV1Schema).max(64),
    methods: z.array(evolutionMethodCapabilityV1Schema).max(256),
  })
  .strict()
  .superRefine((value, context) => {
    uniqueSortedBy(value.methods, "method_id", context, ["methods"]);
    uniqueSortedBy(value.accepted_methods, "method_id", context, ["accepted_methods"]);
    uniqueSortedBy(value.selection_resolvers, "selection_value", context, ["selection_resolvers"]);
    const visible = new Map(value.methods.map((method) => [method.method_id, method]));
    const accepted = new Map(value.accepted_methods.map((method) => [method.method_id, method]));
    const configured = visible.get(value.configured_default_method_id);
    if (!configured) issue(context, ["configured_default_method_id"], "configured default must be visible");
    else if (!sameValue(configured.support, value.configured_default_support)) issue(context, ["configured_default_support"], "configured default support must match the method");
    for (const method of value.methods) {
      const acceptedMethod = accepted.get(method.method_id);
      if (!acceptedMethod || acceptedMethod.implementation_identity_digest !== method.implementation_identity_digest || !sameValue(acceptedMethod.support, method.support)) {
        issue(context, ["accepted_methods"], "visible and accepted method metadata must match");
      }
    }
    for (const resolver of value.selection_resolvers) {
      for (const method of resolver.resolved_methods) {
        if (!sameValue(accepted.get(method.method_id), method)) issue(context, ["selection_resolvers"], "resolver methods must exactly match accepted methods");
      }
    }
    if (value.effective_default_method_id !== null) {
      if (value.effective_default_method_id !== value.configured_default_method_id) issue(context, ["effective_default_method_id"], "effective default cannot replace the configured default");
      if (visible.get(value.effective_default_method_id)?.support.overall !== "supported") issue(context, ["effective_default_method_id"], "effective default must be supported");
    }
  });
export const evolutionCapabilitiesV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    core_version: z.string().refine((value) => value.trim().length > 0),
    registry_digest: sha256DigestSchema,
    evaluated_profile: evolutionExecutionProfileV1Schema,
    targets: z.array(evolutionTargetCapabilityV1Schema).max(128),
  })
  .strict()
  .superRefine((value, context) => uniqueSortedBy(value.targets, "target_id", context, ["targets"]));
export const capabilitiesEnvelopeV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    project_id: opaqueIdSchema,
    project_etag: etagSchema,
    source: z.literal("verified_remote_core").default("verified_remote_core"),
    registry_verified: z.literal(true).default(true),
    fetched_at: utcTimestampSchema,
    capabilities: evolutionCapabilitiesV1Schema,
  })
  .strict();
export const projectCapabilitiesV1Schema = capabilitiesEnvelopeV1Schema;

const checkStatusSchema = z.enum(["ok", "warning", "blocking", "unavailable"]);
export const validationCheckV1Schema = z
  .object({
    id: coreOpaqueIdSchema,
    status: checkStatusSchema,
    message: descriptionSchema,
    target_id: coreOpaqueIdSchema.nullable().default(null),
    method_id: coreOpaqueIdSchema.nullable().default(null),
  })
  .strict();
export const projectValidationV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    project_id: opaqueIdSchema,
    project_etag: etagSchema,
    registry_digest: sha256DigestSchema,
    valid: z.boolean(),
    checks: z.array(validationCheckV1Schema).max(256),
    validated_at: utcTimestampSchema,
  })
  .strict();

export const immutableSnapshotRefV1Schema = z
  .object({
    id: coreOpaqueIdSchema,
    kind: z.enum(["project", "task", "workspace"]),
    content_sha256: sha256DigestSchema,
    created_at: coreUtcTimestampSchema,
  })
  .strict();
export const reachableRequiredRevisionRefV1Schema = z
  .object({
    revision: revisionRefV1Schema,
    reachable_from_revision_id: coreOpaqueIdSchema,
    relation: z.enum(["active", "successor"]),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.relation === "active" && value.revision.id !== value.reachable_from_revision_id) issue(context, ["reachable_from_revision_id"], "active required revision must be the reachable head");
    if (value.relation === "successor" && value.revision.id === value.reachable_from_revision_id) issue(context, ["reachable_from_revision_id"], "successor must differ from its predecessor");
  });
export const runCreateV1Schema = z.object({ project_id: opaqueIdSchema }).strict();
export const runRetryV1Schema = z.object({ terminal_attempt_id: coreOpaqueIdSchema }).strict();
export const queuedReasonV1Schema = z
  .object({
    code: z.enum(["admission_pending", "capacity", "service_starting", "required_revision_uncommitted"]),
    summary: coreShortTextSchema,
    retry_after_seconds: z.number().int().min(0).max(86_400).nullable().default(null),
  })
  .strict();
const runStatusSchema = z.enum(["queued", "preparing", "running", "cancelling", "succeeded", "failed", "cancelled"]);
export const attemptV1Schema = z
  .object({
    id: coreOpaqueIdSchema,
    run_id: coreOpaqueIdSchema,
    number: z.number().int().min(1).max(100),
    status: runStatusSchema,
    queued_reason: queuedReasonV1Schema.nullable().default(null),
    created_at: coreUtcTimestampSchema,
    updated_at: coreUtcTimestampSchema,
    started_at: coreUtcTimestampSchema.nullable().default(null),
    finished_at: coreUtcTimestampSchema.nullable().default(null),
    error: apiErrorV1Schema.nullable().default(null),
  })
  .strict()
  .superRefine(validateAttempt);
export const revisionTransitionV1Schema = z
  .object({
    state: z.enum(["not_started", "sealing_dataset", "running_methods", "validating", "materializing", "preparing_serving", "committing", "active", "failed", "cancelled", "unavailable"]),
    predecessor_revision: revisionRefV1Schema,
    successor_revision: revisionRefV1Schema,
    progress_completed: z.number().int().min(0).max(10_000),
    progress_total: z.number().int().min(0).max(10_000),
    message: descriptionSchema,
    error: apiErrorV1Schema.nullable().default(null),
    updated_at: coreUtcTimestampSchema,
  })
  .strict()
  .superRefine((value, context) => {
    if (value.progress_completed > value.progress_total) issue(context, ["progress_completed"], "transition progress exceeds total");
    if ((value.state === "failed") !== (value.error !== null)) issue(context, ["error"], "error is required only for failed transitions");
    if (value.successor_revision.project_id !== value.predecessor_revision.project_id) issue(context, ["successor_revision"], "transition cannot cross projects");
    if (value.successor_revision.generation !== value.predecessor_revision.generation + 1) issue(context, ["successor_revision", "generation"], "successor generation must follow predecessor");
  });

const runSummaryFields = {
  id: coreOpaqueIdSchema,
  project_id: coreOpaqueIdSchema,
  project_snapshot: immutableSnapshotRefV1Schema,
  task_snapshot: immutableSnapshotRefV1Schema,
  workspace_snapshot: immutableSnapshotRefV1Schema,
  registry_digest: sha256DigestSchema,
  execution_mode: executionModeV1Schema,
  capture_mode: z.enum(["transcript", "token_level"]),
  status: runStatusSchema,
  queued_reason: queuedReasonV1Schema.nullable().default(null),
  current_attempt_id: coreOpaqueIdSchema.nullable().default(null),
  current_attempt: attemptV1Schema.nullable().default(null),
  attempt_count: z.number().int().min(0).max(100),
  current_error: apiErrorV1Schema.nullable().default(null),
  pinned_revision: revisionRefV1Schema.nullable().default(null),
  required_revision: reachableRequiredRevisionRefV1Schema,
  revision_transition: revisionTransitionV1Schema.nullable().default(null),
  created_at: coreUtcTimestampSchema,
  updated_at: coreUtcTimestampSchema,
  admitted_at: coreUtcTimestampSchema.nullable().default(null),
  started_at: coreUtcTimestampSchema.nullable().default(null),
  finished_at: coreUtcTimestampSchema.nullable().default(null),
  etag: etagSchema,
};
export const runSummaryV1Schema = z.object(runSummaryFields).strict().superRefine(validateRunSummary);
export const runV1Schema = z
  .object({ ...runSummaryFields, attempts: z.array(attemptV1Schema).max(100) })
  .strict()
  .superRefine((value, context) => {
    validateRunSummary(value, context);
    if (value.attempts.length !== value.attempt_count) issue(context, ["attempts"], "attempt_count must match attempts");
    if (new Set(value.attempts.map((attempt) => attempt.id)).size !== value.attempts.length) issue(context, ["attempts"], "attempt IDs must be unique");
    if (value.attempts.some((attempt) => attempt.run_id !== value.id)) issue(context, ["attempts"], "attempt belongs to another run");
    if (value.attempts.some((attempt, index) => attempt.number !== index + 1)) issue(context, ["attempts"], "attempt numbers must be contiguous and ordered");
    if (value.attempts.length > 0 && value.attempts.at(-1)?.id !== value.current_attempt_id) issue(context, ["current_attempt_id"], "current attempt must be the last attempt");
    if (value.attempts.length > 0 && !sameValue(value.attempts.at(-1), value.current_attempt)) issue(context, ["current_attempt"], "current attempt must equal the last attempt");
    if (value.attempts.slice(0, -1).some((attempt) => !["succeeded", "failed", "cancelled"].includes(attempt.status))) issue(context, ["attempts"], "superseded attempts must be terminal");
  });
export const timelineEntryV1Schema = z
  .object({
    id: coreOpaqueIdSchema,
    run_id: coreOpaqueIdSchema,
    attempt_id: coreOpaqueIdSchema.nullable().default(null),
    sequence: nonNegativeSafeIntegerSchema,
    service_id: coreOpaqueIdSchema,
    phase: z.enum(["admission", "preparation", "execution", "capture", "dataset", "evolution", "materialization", "revision", "terminal"]),
    status: z.enum(["pending", "running", "succeeded", "failed", "cancelled", "unavailable"]),
    title: displayNameSchema,
    message: descriptionSchema,
    occurred_at: coreUtcTimestampSchema,
    artifact_ids: z.array(coreOpaqueIdSchema).max(128).default([]),
    content_sha256: sha256DigestSchema,
    error: apiErrorV1Schema.nullable().default(null),
  })
  .strict()
  .refine((value) => (value.status === "failed") === (value.error !== null), { path: ["error"], message: "error is required only for failed timeline entries" });
export const logEntryV1Schema = z
  .object({
    id: coreOpaqueIdSchema,
    sequence: nonNegativeSafeIntegerSchema,
    occurred_at: coreUtcTimestampSchema,
    stream: z.enum(["core", "agent", "evolution", "service"]),
    level: z.enum(["debug", "info", "warning", "error"]),
    message: coreLogTextSchema,
    run_id: coreOpaqueIdSchema.nullable().default(null),
    attempt_id: coreOpaqueIdSchema.nullable().default(null),
    service_id: coreOpaqueIdSchema,
    content_sha256: sha256DigestSchema,
  })
  .strict()
  .superRefine((value, context) => {
    if (value.attempt_id !== null && value.run_id === null) issue(context, ["attempt_id"], "attempt ID requires run ID");
    if (["agent", "evolution"].includes(value.stream) && value.run_id === null) issue(context, ["run_id"], "agent and evolution logs require run identity");
  });
export const contextArtifactRefV1Schema = z
  .object({ artifact_id: coreOpaqueIdSchema, artifact_type: z.enum(["text_memory", "skill_bundle", "agent_system", "parametric_memory"]), target_id: coreOpaqueIdSchema, revision: revisionRefV1Schema })
  .strict();
export const adapterRefV1Schema = z
  .object({ artifact_id: coreOpaqueIdSchema, adapter_id: coreOpaqueIdSchema, base_model_ref: agentModelRefSchema, revision: revisionRefV1Schema })
  .strict();
export const runContextV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    run_id: coreOpaqueIdSchema,
    project_id: coreOpaqueIdSchema,
    project_snapshot: immutableSnapshotRefV1Schema,
    task_snapshot: immutableSnapshotRefV1Schema,
    workspace_snapshot: immutableSnapshotRefV1Schema,
    status: runStatusSchema,
    queued_reason: queuedReasonV1Schema.nullable().default(null),
    current_attempt_id: coreOpaqueIdSchema.nullable().default(null),
    current_attempt: attemptV1Schema.nullable().default(null),
    attempt_count: z.number().int().min(0).max(100),
    current_error: apiErrorV1Schema.nullable().default(null),
    pinned_revision: revisionRefV1Schema.nullable().default(null),
    required_revision: reachableRequiredRevisionRefV1Schema,
    revision_transition: revisionTransitionV1Schema.nullable().default(null),
    registry_digest: sha256DigestSchema,
    execution_mode: executionModeV1Schema,
    capture_mode: z.enum(["transcript", "token_level"]),
    created_at: coreUtcTimestampSchema,
    updated_at: coreUtcTimestampSchema,
    admitted_at: coreUtcTimestampSchema.nullable().default(null),
    started_at: coreUtcTimestampSchema.nullable().default(null),
    finished_at: coreUtcTimestampSchema.nullable().default(null),
    etag: etagSchema,
    token_level_metrics_available: z.boolean(),
    artifacts: z.array(contextArtifactRefV1Schema).max(256),
    adapters: z.array(adapterRefV1Schema).max(64),
  })
  .strict()
  .superRefine((value, context) => {
    validateRunSummary({ ...value, id: value.run_id }, context);
    if (value.capture_mode === "transcript" && value.token_level_metrics_available) issue(context, ["token_level_metrics_available"], "transcript capture has no token-level metrics");
  });

const artifactCompatibilityV1Schema = z
  .object({ execution_modes: z.array(executionModeV1Schema).max(2), harness_ids: z.array(coreOpaqueIdSchema).max(64), base_model_refs: z.array(agentModelRefSchema).max(64) })
  .strict();
const artifactLineageV1Schema = z
  .object({ method_id: coreOpaqueIdSchema, job_id: coreOpaqueIdSchema, source_dataset_ids: z.array(coreOpaqueIdSchema).max(128), source_artifact_ids: z.array(coreOpaqueIdSchema).max(128) })
  .strict();
const artifactScoreV1Schema = z.object({ name: z.string().min(1).max(64).regex(/^[a-z][a-z0-9_]*$/), value: z.number().finite().min(-1_000_000).max(1_000_000) }).strict();
const artifactBaseFields = {
  id: coreOpaqueIdSchema,
  project_id: coreOpaqueIdSchema,
  run_id: coreOpaqueIdSchema.nullable().default(null),
  target_id: coreOpaqueIdSchema,
  display_name: displayNameSchema,
  summary: descriptionSchema,
  byte_size: nonNegativeSafeIntegerSchema,
  produced_revision: revisionRefV1Schema,
  membership_revisions: z.array(revisionRefV1Schema).max(128),
  content_sha256: sha256DigestSchema,
  selected: z.boolean(),
  promoted: z.boolean(),
  release_enabled: z.boolean(),
  compatibility: artifactCompatibilityV1Schema,
  lineage: artifactLineageV1Schema,
  scores: z.array(artifactScoreV1Schema).max(64),
  created_at: coreUtcTimestampSchema,
};
const textMemoryArtifactV1Schema = z.object({ ...artifactBaseFields, artifact_type: z.literal("text_memory"), metadata: z.object({ record_count: nonNegativeSafeIntegerSchema, source_dataset_ids: z.array(coreOpaqueIdSchema).max(128) }).strict() }).strict();
const skillBundleArtifactV1Schema = z.object({ ...artifactBaseFields, artifact_type: z.literal("skill_bundle"), metadata: z.object({ document_count: z.number().int().min(1).max(128), root_document: z.literal("SKILL.md").default("SKILL.md") }).strict() }).strict();
const agentSystemArtifactV1Schema = z.object({ ...artifactBaseFields, artifact_type: z.literal("agent_system"), metadata: z.object({ target_path: z.string().min(1).max(256).refine(isAllowedAgentTargetPath) }).strict() }).strict();
const parametricMemoryArtifactV1Schema = z.object({ ...artifactBaseFields, artifact_type: z.literal("parametric_memory"), release_enabled: z.literal(false), metadata: z.object({ adapter_id: coreOpaqueIdSchema, base_model_ref: agentModelRefSchema, adapter_format: z.literal("lora") }).strict() }).strict();
export const artifactV1Schema = z
  .discriminatedUnion("artifact_type", [textMemoryArtifactV1Schema, skillBundleArtifactV1Schema, agentSystemArtifactV1Schema, parametricMemoryArtifactV1Schema])
  .superRefine((value, context) => {
    const revisions = [value.produced_revision, ...value.membership_revisions];
    if (revisions.some((revision) => revision.project_id !== value.project_id)) issue(context, ["produced_revision"], "artifact revision belongs to another project");
    const ids = value.membership_revisions.map((revision) => revision.id);
    if (new Set(ids).size !== ids.length) issue(context, ["membership_revisions"], "membership revisions must be unique");
  });
export const artifactDocumentV1Schema = z
  .object({
    document_id: coreOpaqueIdSchema,
    display_name: displayNameSchema,
    relative_path: z.string().max(256).regex(/^[^/\u0000-\u001f\u007f][^\u0000-\u001f\u007f]*$/).nullable().default(null),
    mime_type: mimeTypeSchema,
    content: contentTextSchema,
    content_sha256: sha256DigestSchema,
    byte_size: nonNegativeSafeIntegerSchema,
    truncated: z.boolean(),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.relative_path !== null && value.relative_path.split("/").some((segment) => ["", ".", ".."].includes(segment))) issue(context, ["relative_path"], "relative path contains an unsafe segment");
    const returned = utf8ByteLength(value.content);
    if (returned > value.byte_size) issue(context, ["content"], "document preview exceeds authoritative byte size");
    if (!value.truncated && returned !== value.byte_size) issue(context, ["byte_size"], "complete document byte size must match content");
  });
export const artifactContentV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    artifact_id: coreOpaqueIdSchema,
    artifact_type: z.enum(["text_memory", "skill_bundle", "agent_system", "parametric_memory"]),
    documents: z.array(artifactDocumentV1Schema).max(MAX_ARTIFACT_PREVIEW_DOCUMENTS),
    total_documents: nonNegativeSafeIntegerSchema,
    total_utf8_bytes: nonNegativeSafeIntegerSchema,
    returned_utf8_bytes: nonNegativeSafeIntegerSchema.max(MAX_ARTIFACT_PREVIEW_BYTES),
    truncated: z.boolean(),
  })
  .strict()
  .superRefine((value, context) => {
    const returned = value.documents.reduce((total, document) => total + utf8ByteLength(document.content), 0);
    if (returned > MAX_ARTIFACT_PREVIEW_BYTES) issue(context, ["documents"], "artifact preview exceeds the aggregate UTF-8 byte budget");
    if (returned !== value.returned_utf8_bytes) issue(context, ["returned_utf8_bytes"], "returned bytes must match document previews");
    if (value.documents.length > value.total_documents) issue(context, ["total_documents"], "returned documents exceed total documents");
    if (value.returned_utf8_bytes > value.total_utf8_bytes) issue(context, ["total_utf8_bytes"], "returned bytes exceed total bytes");
    const actuallyTruncated = value.documents.length < value.total_documents || value.returned_utf8_bytes < value.total_utf8_bytes || value.documents.some((document) => document.truncated);
    if (value.truncated !== actuallyTruncated) issue(context, ["truncated"], "truncated must match preview totals");
  });
export const diffLineV1Schema = z
  .object({
    kind: z.enum(["context", "added", "removed"]),
    old_line_number: safeIntegerSchema.min(1).nullable().default(null),
    new_line_number: safeIntegerSchema.min(1).nullable().default(null),
    text: coreLogTextSchema,
  })
  .strict()
  .superRefine((value, context) => {
    const valid = value.kind === "context"
      ? value.old_line_number !== null && value.new_line_number !== null
      : value.kind === "added"
        ? value.old_line_number === null && value.new_line_number !== null
        : value.old_line_number !== null && value.new_line_number === null;
    if (!valid) issue(context, [], "line numbers must match diff line kind");
  });
export const artifactDiffDocumentIdentityV1Schema = z
  .object({
    artifact_id: coreOpaqueIdSchema,
    artifact_content_sha256: sha256DigestSchema,
    document_id: coreOpaqueIdSchema,
    relative_path: z.string().min(1).max(256).regex(/^[^/\\\u0000-\u001f\u007f][^\\\u0000-\u001f\u007f]*$/),
    content_sha256: sha256DigestSchema,
  })
  .strict()
  .refine((value) => !value.relative_path.split("/").some((segment) => ["", ".", ".."].includes(segment)), {
    path: ["relative_path"],
    message: "relative path contains an unsafe segment",
  });
export const diffHunkV1Schema = z
  .object({
    old_document: artifactDiffDocumentIdentityV1Schema.nullable().default(null),
    new_document: artifactDiffDocumentIdentityV1Schema.nullable().default(null),
    old_start: nonNegativeSafeIntegerSchema,
    old_count: nonNegativeSafeIntegerSchema,
    new_start: nonNegativeSafeIntegerSchema,
    new_count: nonNegativeSafeIntegerSchema,
    lines: z.array(diffLineV1Schema).max(512),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.old_document === null && value.new_document === null) issue(context, [], "a diff hunk requires an old or new document");
    if (value.old_document === null && (value.old_start !== 0 || value.old_count !== 0)) issue(context, ["old_start"], "an added-document hunk has no old range");
    if (value.new_document === null && (value.new_start !== 0 || value.new_count !== 0)) issue(context, ["new_start"], "a removed-document hunk has no new range");
    const oldLines = value.lines.filter((line) => line.kind === "context" || line.kind === "removed").length;
    const newLines = value.lines.filter((line) => line.kind === "context" || line.kind === "added").length;
    if (oldLines !== value.old_count || newLines !== value.new_count) issue(context, ["lines"], "diff hunk ranges must match its lines");
  });

function validateDocumentChangeHunks(
  value: { old_document?: z.infer<typeof artifactDiffDocumentIdentityV1Schema>; new_document?: z.infer<typeof artifactDiffDocumentIdentityV1Schema>; hunks: z.infer<typeof diffHunkV1Schema>[] },
  context: z.RefinementCtx,
): void {
  const oldDocument = value.old_document ?? null;
  const newDocument = value.new_document ?? null;
  if (value.hunks.some((hunk) => !sameValue(hunk.old_document, oldDocument) || !sameValue(hunk.new_document, newDocument))) {
    issue(context, ["hunks"], "diff hunk document identity must match its document change");
  }
}

const addedArtifactDocumentChangeV1Schema = z
  .object({ kind: z.literal("added"), new_document: artifactDiffDocumentIdentityV1Schema, hunks: z.array(diffHunkV1Schema).max(MAX_ARTIFACT_DIFF_HUNKS) })
  .strict()
  .superRefine(validateDocumentChangeHunks);
const removedArtifactDocumentChangeV1Schema = z
  .object({ kind: z.literal("removed"), old_document: artifactDiffDocumentIdentityV1Schema, hunks: z.array(diffHunkV1Schema).max(MAX_ARTIFACT_DIFF_HUNKS) })
  .strict()
  .superRefine(validateDocumentChangeHunks);
const modifiedArtifactDocumentChangeV1Schema = z
  .object({ kind: z.literal("modified"), old_document: artifactDiffDocumentIdentityV1Schema, new_document: artifactDiffDocumentIdentityV1Schema, hunks: z.array(diffHunkV1Schema).max(MAX_ARTIFACT_DIFF_HUNKS) })
  .strict()
  .superRefine((value, context) => {
    if (value.old_document.relative_path !== value.new_document.relative_path) issue(context, ["new_document", "relative_path"], "modified document must retain its relative path");
    validateDocumentChangeHunks(value, context);
  });
const renamedArtifactDocumentChangeV1Schema = z
  .object({ kind: z.literal("renamed"), old_document: artifactDiffDocumentIdentityV1Schema, new_document: artifactDiffDocumentIdentityV1Schema, hunks: z.array(diffHunkV1Schema).max(MAX_ARTIFACT_DIFF_HUNKS) })
  .strict()
  .superRefine((value, context) => {
    if (value.old_document.relative_path === value.new_document.relative_path) issue(context, ["new_document", "relative_path"], "renamed document must change its relative path");
    validateDocumentChangeHunks(value, context);
  });
export const artifactDocumentChangeV1Schema = z.union([
  addedArtifactDocumentChangeV1Schema,
  removedArtifactDocumentChangeV1Schema,
  modifiedArtifactDocumentChangeV1Schema,
  renamedArtifactDocumentChangeV1Schema,
]);
export const artifactDiffV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    artifact_id: coreOpaqueIdSchema,
    artifact_content_sha256: sha256DigestSchema,
    previous_artifact_id: coreOpaqueIdSchema,
    previous_artifact_content_sha256: sha256DigestSchema,
    document_changes: z.array(artifactDocumentChangeV1Schema).max(MAX_ARTIFACT_PREVIEW_DOCUMENTS),
    total_document_changes: nonNegativeSafeIntegerSchema,
    total_hunks: nonNegativeSafeIntegerSchema,
    total_lines: nonNegativeSafeIntegerSchema,
    truncated: z.boolean(),
  })
  .strict()
  .superRefine((value, context) => {
    const hunks = value.document_changes.flatMap((change) => change.hunks);
    for (const [index, change] of value.document_changes.entries()) {
      const oldDocument = "old_document" in change ? change.old_document : null;
      const newDocument = "new_document" in change ? change.new_document : null;
      if (oldDocument !== null && (oldDocument.artifact_id !== value.previous_artifact_id
        || oldDocument.artifact_content_sha256 !== value.previous_artifact_content_sha256)) {
        issue(context, ["document_changes", index, "old_document"], "old document identity must match the previous artifact");
      }
      if (newDocument !== null && (newDocument.artifact_id !== value.artifact_id
        || newDocument.artifact_content_sha256 !== value.artifact_content_sha256)) {
        issue(context, ["document_changes", index, "new_document"], "new document identity must match the current artifact");
      }
    }
    const lines = hunks.reduce((total, hunk) => total + hunk.lines.length, 0);
    const bytes = hunks.reduce((total, hunk) => total + hunk.lines.reduce((subtotal, line) => subtotal + utf8ByteLength(line.text), 0), 0);
    if (hunks.length > MAX_ARTIFACT_DIFF_HUNKS) issue(context, ["document_changes"], "artifact diff exceeds the hunk budget");
    if (lines > MAX_ARTIFACT_DIFF_LINES) issue(context, ["document_changes"], "artifact diff exceeds the line budget");
    if (bytes > MAX_ARTIFACT_PREVIEW_BYTES) issue(context, ["document_changes"], "artifact diff exceeds the UTF-8 byte budget");
    if (value.document_changes.length > value.total_document_changes || hunks.length > value.total_hunks || lines > value.total_lines) issue(context, [], "returned diff exceeds authoritative totals");
    const actuallyTruncated = value.document_changes.length < value.total_document_changes || hunks.length < value.total_hunks || lines < value.total_lines;
    if (value.truncated !== actuallyTruncated) issue(context, ["truncated"], "truncated must match diff totals");
  });

export const serviceV1Schema = z
  .object({
    id: coreOpaqueIdSchema,
    display_name: displayNameSchema,
    kind: z.enum(["control", "gateway", "inference", "evolution_worker", "artifact_store"]),
    status: z.enum(["stopped", "starting", "running", "degraded", "failed", "unavailable"]),
    restartable: z.boolean(),
    status_message: coreShortTextSchema.nullable().default(null),
    error: apiErrorV1Schema.nullable().default(null),
    model_preparation: modelPreparationV1Schema.nullable().default(null),
    updated_at: coreUtcTimestampSchema,
    observed_at: coreUtcTimestampSchema,
    etag: etagSchema,
  })
  .strict()
  .superRefine((value, context) => {
    if ((value.status === "failed") !== (value.error !== null)) issue(context, ["error"], "error is required only for failed services");
    if ((value.kind === "inference") !== (value.model_preparation !== null)) issue(context, ["model_preparation"], "model preparation is required only for inference services");
  });
const diagnosticScopeSchema = z.enum(["environment", "project", "run", "services", "registry", "storage"]);
const diagnosticTargetV1Schema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("global") }).strict(),
  z.object({ kind: z.literal("project"), project_id: coreOpaqueIdSchema }).strict(),
  z.object({ kind: z.literal("run"), project_id: coreOpaqueIdSchema, run_id: coreOpaqueIdSchema }).strict(),
]);
export const diagnosticCreateV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    scopes: z.array(diagnosticScopeSchema).min(1).max(16),
    target: diagnosticTargetV1Schema,
  })
  .strict()
  .superRefine((value, context) => {
    if (new Set(value.scopes).size !== value.scopes.length) issue(context, ["scopes"], "diagnostic scopes must be unique");
    const scopes = new Set(value.scopes);
    const global = new Set(["environment", "services", "registry", "storage"]);
    if (value.target.kind === "global" && value.scopes.some((scope) => !global.has(scope))) issue(context, ["scopes"], "global diagnostics accept only global scopes");
    if (value.target.kind === "project" && !(scopes.size === 1 && scopes.has("project"))) issue(context, ["scopes"], "project diagnostics require exactly the project scope");
    if (value.target.kind === "run" && !(scopes.size === 1 && scopes.has("run"))) issue(context, ["scopes"], "run diagnostics require exactly the run scope");
  });
const diagnosticCheckV1Schema = z
  .object({ id: coreOpaqueIdSchema, scope: diagnosticScopeSchema, status: checkStatusSchema, message: descriptionSchema, repair_action: repairActionSchema, logs_ref: coreOpaqueIdSchema.nullable().default(null) })
  .strict();
export const diagnosticReportV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    id: coreOpaqueIdSchema,
    status: z.enum(["queued", "running", "succeeded", "failed"]),
    scopes: z.array(diagnosticScopeSchema).min(1).max(16),
    target: diagnosticTargetV1Schema,
    checks: z.array(diagnosticCheckV1Schema).max(256),
    created_at: coreUtcTimestampSchema,
    updated_at: coreUtcTimestampSchema,
    observed_at: coreUtcTimestampSchema,
    finished_at: coreUtcTimestampSchema.nullable().default(null),
    error: apiErrorV1Schema.nullable().default(null),
    etag: etagSchema,
  })
  .strict()
  .superRefine((value, context) => {
    validateAsyncStatus(value, context);
    const request = diagnosticCreateV1Schema.safeParse({ scopes: value.scopes, target: value.target });
    if (!request.success) issue(context, ["target"], "diagnostic target and scopes are inconsistent");
    if (value.checks.some((check) => !value.scopes.includes(check.scope))) issue(context, ["checks"], "diagnostic check has an unrequested scope");
  });
const cacheScopeSchema = z.enum(["model_downloads", "build_artifacts", "completed_runs", "completed_diagnostics"]);
export const cacheCleanupRequestV1Schema = z
  .object({ schema_version: schemaVersionV1Schema, scopes: z.array(cacheScopeSchema).min(1).max(8), older_than_days: z.number().int().min(1).max(3_650) })
  .strict()
  .refine((value) => new Set(value.scopes).size === value.scopes.length, { path: ["scopes"], message: "cache scopes must be unique" });
export const cacheCleanupResultV1Schema = z
  .object({
    scopes: z.array(cacheScopeSchema).min(1).max(8),
    removed_entries: z.number().int().min(0).max(100_000_000),
    reclaimed_bytes: nonNegativeSafeIntegerSchema,
  })
  .strict();
const environmentRepairActionSchema = z.enum([
  "retry_network",
  "restart_container_runtime",
  "restart_model_service",
  "repair_registry_install",
  "reconcile_managed_state",
]);
const environmentRepairRequestV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    execution_mode: executionModeV1Schema,
    actions: z.array(environmentRepairActionSchema).min(1).max(16),
  })
  .strict()
  .refine((value) => new Set(value.actions).size === value.actions.length, { path: ["actions"], message: "repair actions must be unique" });
const repairActionResultV1Schema = z
  .object({ action: environmentRepairActionSchema, status: checkStatusSchema, message: descriptionSchema })
  .strict();
const environmentRepairResponseV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    status: z.enum(["ok", "degraded", "needs_user_action"]),
    results: z.array(repairActionResultV1Schema).min(1).max(16),
    checked_at: coreUtcTimestampSchema,
  })
  .strict();
const serviceRestartRequestV1Schema = z
  .object({ schema_version: schemaVersionV1Schema, reason: z.string().min(1).max(512) })
  .strict();
const operationKindSchema = z.enum(["environment_repair", "service_restart", "cache_cleanup"]);
const operationDescriptorV1Schema = z
  .object({ kind: operationKindSchema, cancellable: z.boolean() })
  .strict()
  .superRefine((value, context) => {
    if (value.cancellable !== (value.kind === "environment_repair")) issue(context, ["cancellable"], "operation cancellation policy must match its kind");
  });
const operationRequestV1Schema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("environment_repair"), request: environmentRepairRequestV1Schema }).strict(),
  z.object({ kind: z.literal("service_restart"), service_id: coreOpaqueIdSchema, request: serviceRestartRequestV1Schema }).strict(),
  z.object({ kind: z.literal("cache_cleanup"), request: cacheCleanupRequestV1Schema }).strict(),
]);
const operationResultV1Schema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("environment_repair"), response: environmentRepairResponseV1Schema }).strict(),
  z.object({ kind: z.literal("service_restart"), service: serviceV1Schema }).strict(),
  z.object({ kind: z.literal("cache_cleanup"), result: cacheCleanupResultV1Schema }).strict(),
]);
const operationCancellationV1Schema = z
  .object({ reason: z.literal("user_requested"), requested_at: coreUtcTimestampSchema })
  .strict();
export const operationV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    id: coreOpaqueIdSchema,
    kind: operationKindSchema,
    descriptor: operationDescriptorV1Schema,
    status: z.enum(["queued", "running", "cancelling", "succeeded", "failed", "cancelled"]),
    request: operationRequestV1Schema,
    result: operationResultV1Schema.nullable().default(null),
    cancellation: operationCancellationV1Schema.nullable().default(null),
    logs_ref: coreOpaqueIdSchema,
    created_at: coreUtcTimestampSchema,
    updated_at: coreUtcTimestampSchema,
    observed_at: coreUtcTimestampSchema,
    finished_at: coreUtcTimestampSchema.nullable().default(null),
    error: apiErrorV1Schema.nullable().default(null),
    etag: etagSchema,
  })
  .strict()
  .superRefine((value, context) => {
    const terminal = ["succeeded", "failed", "cancelled"].includes(value.status);
    if (terminal !== (value.finished_at !== null)) issue(context, ["finished_at"], "finished_at is required only for terminal operations");
    if ((value.status === "failed") !== (value.error !== null)) issue(context, ["error"], "error is required only for failed operations");
    const cancelling = value.status === "cancelling" || value.status === "cancelled";
    if (cancelling !== (value.cancellation !== null)) issue(context, ["cancellation"], "cancellation is required only for cancelling operations");
    if (cancelling && !value.descriptor.cancellable) issue(context, ["descriptor", "cancellable"], "non-cancellable operation cannot enter cancellation states");
    if (value.descriptor.kind !== value.kind || value.request.kind !== value.kind) issue(context, ["kind"], "operation descriptor and request must match its kind");
    const succeeded = value.status === "succeeded";
    if (succeeded !== (value.result !== null)) issue(context, ["result"], "only successful operations carry a typed result");
    if (value.result !== null && value.result.kind !== value.kind) issue(context, ["result", "kind"], "operation result must match its kind");
    if (value.result?.kind === "environment_repair" && value.request.kind === "environment_repair") {
      const resultActions = value.result.response.results.map((item) => item.action);
      if (!sameValue(resultActions, value.request.request.actions)) issue(context, ["result", "response", "results"], "environment repair results must match requested actions");
    }
    if (value.result?.kind === "service_restart" && value.request.kind === "service_restart") {
      if (value.result.service.id !== value.request.service_id) issue(context, ["result", "service", "id"], "service restart result has the wrong service ID");
    }
    if (value.result?.kind === "cache_cleanup" && value.request.kind === "cache_cleanup") {
      if (!sameValue(value.result.result.scopes, value.request.request.scopes)) issue(context, ["result", "result", "scopes"], "cache cleanup result scopes must match its request");
    }
  });

const stateEventV1Schema = z.object({ kind: z.literal("state_changed"), state: desktopStateV1Schema }).strict();
const resourceEventV1Schema = z
  .object({
    kind: z.literal("resource_changed"),
    authority: z.enum(["desktop", "core"]),
    resource: resourceRefV1Schema,
    change: z.enum(["created", "updated", "deleted", "appended"]),
    change_id: opaqueIdSchema,
    resource_etag: etagSchema.nullable().default(null),
    content_sha256: sha256DigestSchema.nullable().default(null),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.resource_etag === null && value.content_sha256 === null) issue(context, ["resource_etag"], "resource events require an authoritative ETag or digest");
    const desktopResources = new Set(["profile", "project", "operation", "maintenance"]);
    if (value.authority === "desktop" && !desktopResources.has(value.resource.resource_type)) issue(context, ["authority"], "Desktop authority cannot identify a Core-owned resource");
    if (value.authority === "core" && ["profile", "project", "maintenance"].includes(value.resource.resource_type)) issue(context, ["authority"], "Core changes must use a mapped Desktop project resource");
  });
const heartbeatEventV1Schema = z.object({ kind: z.literal("heartbeat") }).strict();
export const eventDataV1Schema = z.union([stateEventV1Schema, resourceEventV1Schema, heartbeatEventV1Schema]);
export const eventNameV1Schema = z.enum(["desktop.v1.state.changed", "desktop.v1.resource.changed", "desktop.v1.heartbeat"]);
export const eventEnvelopeV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    event_id: opaqueIdSchema,
    event_name: eventNameV1Schema,
    occurred_at: utcTimestampSchema,
    sequence: nonNegativeSafeIntegerSchema,
    data: eventDataV1Schema,
  })
  .strict()
  .refine((value) => {
    const expected = value.data.kind === "state_changed"
      ? "desktop.v1.state.changed"
      : value.data.kind === "resource_changed"
        ? "desktop.v1.resource.changed"
        : "desktop.v1.heartbeat";
    return expected === value.event_name;
  }, { path: ["event_name"], message: "event name must match typed event data" });
export const sseFrameV1Schema = z
  .object({ id: opaqueIdSchema, event: eventNameV1Schema, data: eventEnvelopeV1Schema })
  .strict()
  .refine((value) => value.id === value.data.event_id && value.event === value.data.event_name, { message: "SSE frame identity must match its event envelope" });

export function pageV1Schema<T extends z.ZodTypeAny>(itemSchema: T) {
  return z
    .object({ schema_version: schemaVersionV1Schema, items: z.array(itemSchema).max(MAX_PAGE_SIZE), next_cursor: opaqueIdSchema.nullable().default(null), has_more: z.boolean() })
    .strict()
    .refine((value) => value.has_more === (value.next_cursor !== null), { path: ["next_cursor"], message: "cursor must agree with has_more" });
}
function corePageV1Schema<T extends z.ZodTypeAny>(itemSchema: T, maxItems = 100) {
  return z
    .object({ schema_version: schemaVersionV1Schema, items: z.array(itemSchema).max(maxItems), next_cursor: z.string().min(1).max(512).nullable().default(null), has_more: z.boolean() })
    .strict()
    .refine((value) => value.has_more === (value.next_cursor !== null), { path: ["next_cursor"], message: "cursor must agree with has_more" });
}
export const profilePageV1Schema = pageV1Schema(remoteProfileV1Schema);
export const projectPageV1Schema = pageV1Schema(projectV1Schema);
export const localLogPageV1Schema = pageV1Schema(localLogEntryV1Schema);
export const runPageV1Schema = corePageV1Schema(runSummaryV1Schema);
export const timelinePageV1Schema = corePageV1Schema(timelineEntryV1Schema);
export const logPageV1Schema = corePageV1Schema(logEntryV1Schema);
export const referencedLogPageV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    items: z.array(logEntryV1Schema).max(100),
    next_cursor: z.string().min(1).max(512).nullable().default(null),
    has_more: z.boolean(),
    logs_ref: coreOpaqueIdSchema,
  })
  .strict()
  .refine((value) => value.has_more === (value.next_cursor !== null), { path: ["next_cursor"], message: "cursor must agree with has_more" });
export const artifactPageV1Schema = corePageV1Schema(artifactV1Schema);
export const servicePageV1Schema = corePageV1Schema(serviceV1Schema, 64);

export const versionV1Schema = versionInfoV1Schema;
export const remoteProfileCreateV1Schema = profileCreateV1Schema;
export const remoteProfilePatchV1Schema = profilePatchV1Schema;
export const diagnosticRequestV1Schema = diagnosticCreateV1Schema;

export type VersionInfoV1 = z.infer<typeof versionInfoV1Schema>;
export type DesktopBootstrapContextV1 = z.infer<typeof desktopBootstrapContextV1Schema>;
export type HealthV1 = z.infer<typeof healthV1Schema>;
export type ApiErrorV1 = z.infer<typeof apiErrorV1Schema>;
export type DesktopStateV1 = z.infer<typeof desktopStateV1Schema>;
export type ExecutionModeCapabilityV1 = z.infer<typeof executionModeCapabilityV1Schema>;
export type ExecutionModeCapabilitiesV1 = z.infer<typeof executionModeCapabilitiesV1Schema>;
export type CoreConnectionStateV1 = z.infer<typeof coreConnectionStateV1Schema>;
export type RemoteProfileV1 = z.infer<typeof remoteProfileV1Schema>;
export type ProfileCreateV1 = z.input<typeof profileCreateV1Schema>;
export type ProfilePatchV1 = z.input<typeof profilePatchV1Schema>;
export type HostKeyAcceptV1 = z.input<typeof hostKeyAcceptV1Schema>;
export type ProjectV1 = z.infer<typeof projectV1Schema>;
export type ProjectCreateV1 = z.input<typeof projectCreateV1Schema>;
export type ProjectPatchV1 = z.input<typeof projectPatchV1Schema>;
export type ProjectSourceV1 = z.infer<typeof projectSourceV1Schema>;
export type LocalOperationV1 = z.infer<typeof localOperationV1Schema>;
export type LocalLogEntryV1 = z.infer<typeof localLogEntryV1Schema>;
export type RunSummaryV1 = z.infer<typeof runSummaryV1Schema>;
export type RunV1 = z.infer<typeof runV1Schema>;
export type RunCreateV1 = z.input<typeof runCreateV1Schema>;
export type RunRetryV1 = z.input<typeof runRetryV1Schema>;
export type TimelineEntryV1 = z.infer<typeof timelineEntryV1Schema>;
export type LogEntryV1 = z.infer<typeof logEntryV1Schema>;
export type RunContextV1 = z.infer<typeof runContextV1Schema>;
export type ArtifactV1 = z.infer<typeof artifactV1Schema>;
export type ArtifactContentV1 = z.infer<typeof artifactContentV1Schema>;
export type ArtifactDiffV1 = z.infer<typeof artifactDiffV1Schema>;
export type ServiceV1 = z.infer<typeof serviceV1Schema>;
export type OperationV1 = z.infer<typeof operationV1Schema>;
export type ReferencedLogPageV1 = z.infer<typeof referencedLogPageV1Schema>;
export type DiagnosticReportV1 = z.infer<typeof diagnosticReportV1Schema>;
export type DiagnosticCreateV1 = z.input<typeof diagnosticCreateV1Schema>;
export type CacheCleanupRequestV1 = z.input<typeof cacheCleanupRequestV1Schema>;
export type ProjectCapabilitiesV1 = z.infer<typeof capabilitiesEnvelopeV1Schema>;
export type ProjectValidationV1 = z.infer<typeof projectValidationV1Schema>;
export type EventEnvelopeV1 = z.infer<typeof eventEnvelopeV1Schema>;
export type EventDataV1 = z.infer<typeof eventDataV1Schema>;
export type EventNameV1 = z.infer<typeof eventNameV1Schema>;
export type SseFrameV1 = z.infer<typeof sseFrameV1Schema>;
export type PageV1<T> = { schema_version: "1"; items: T[]; next_cursor: string | null; has_more: boolean };
export type VersionV1 = VersionInfoV1;
export type RemoteProfileCreateV1 = ProfileCreateV1;
export type RemoteProfilePatchV1 = ProfilePatchV1;
export type CapabilitiesEnvelopeV1 = ProjectCapabilitiesV1;
export type DiagnosticRequestV1 = DiagnosticCreateV1;

function validateAttempt(
  value: {
    status: "queued" | "preparing" | "running" | "cancelling" | "succeeded" | "failed" | "cancelled";
    queued_reason: unknown | null;
    started_at: string | null;
    finished_at: string | null;
    error: unknown | null;
  },
  context: z.RefinementCtx,
): void {
  const terminal = ["succeeded", "failed", "cancelled"].includes(value.status);
  const started = ["running", "cancelling", "succeeded", "failed"].includes(value.status);
  if ((value.status === "queued") !== (value.queued_reason !== null)) issue(context, ["queued_reason"], "queued reason is required only for queued attempts");
  if (terminal !== (value.finished_at !== null)) issue(context, ["finished_at"], "finished_at is required only for terminal attempts");
  if (started && value.started_at === null) issue(context, ["started_at"], "started_at is required after an attempt starts");
  if ((value.status === "failed") !== (value.error !== null)) issue(context, ["error"], "error is required only for failed attempts");
}

type RunSummaryShape = z.infer<z.ZodObject<typeof runSummaryFields>>;

function validateRunSummary(value: RunSummaryShape, context: z.RefinementCtx): void {
  const terminal = ["succeeded", "failed", "cancelled"].includes(value.status);
  const started = ["running", "cancelling", "succeeded", "failed"].includes(value.status);
  if ((value.status === "queued") !== (value.queued_reason !== null)) issue(context, ["queued_reason"], "queued reason is required only for queued runs");
  if (terminal !== (value.finished_at !== null)) issue(context, ["finished_at"], "finished_at is required only for terminal runs");
  if (started && value.started_at === null) issue(context, ["started_at"], "started_at is required after a run starts");
  if ((value.attempt_count === 0) !== (value.current_attempt_id === null)) issue(context, ["current_attempt_id"], "current attempt ID must match attempt count");
  if ((value.current_attempt_id === null) !== (value.current_attempt === null)) issue(context, ["current_attempt"], "current attempt must match its ID");
  if (value.current_attempt !== null) {
    if (value.current_attempt.id !== value.current_attempt_id || value.current_attempt.run_id !== value.id) issue(context, ["current_attempt"], "current attempt identity is invalid");
    if (!sameValue(value.current_attempt.error, value.current_error)) issue(context, ["current_error"], "run and current attempt errors must match");
    if (value.current_attempt.number !== value.attempt_count) issue(context, ["current_attempt", "number"], "current attempt number must match attempt count");
    if (value.current_attempt.status !== value.status) issue(context, ["current_attempt", "status"], "run and current attempt statuses must match");
  }
  if (value.pinned_revision !== null && !sameValue(value.pinned_revision, value.required_revision.revision)) issue(context, ["pinned_revision"], "run may pin only its required revision");
  if ((value.admitted_at === null) !== (value.pinned_revision === null)) issue(context, ["admitted_at"], "admitted_at and pinned_revision must appear together");
  const admissionRequired = ["preparing", "running", "cancelling", "succeeded", "failed"].includes(value.status);
  if (admissionRequired && value.admitted_at === null) issue(context, ["admitted_at"], "admitted run requires its exact revision pin");
  if (value.status === "queued" && value.queued_reason?.code === "required_revision_uncommitted" && value.admitted_at !== null) {
    issue(context, ["admitted_at"], "run waiting for its required revision is not admitted");
  }
  if (value.project_snapshot.kind !== "project") issue(context, ["project_snapshot", "kind"], "project snapshot has the wrong kind");
  if (value.task_snapshot.kind !== "task") issue(context, ["task_snapshot", "kind"], "task snapshot has the wrong kind");
  if (value.workspace_snapshot.kind !== "workspace") issue(context, ["workspace_snapshot", "kind"], "workspace snapshot has the wrong kind");
  if (value.required_revision.revision.project_id !== value.project_id) issue(context, ["required_revision"], "required revision belongs to another project");
  if (value.required_revision.relation === "active") {
    if (value.revision_transition !== null) issue(context, ["revision_transition"], "active required revision has no successor transition");
  } else if (value.revision_transition === null) {
    issue(context, ["revision_transition"], "successor required revision requires its transition");
  } else {
    if (value.revision_transition.predecessor_revision.id !== value.required_revision.reachable_from_revision_id
      || !sameValue(value.revision_transition.successor_revision, value.required_revision.revision)) {
      issue(context, ["revision_transition"], "successor transition must prove the required revision");
    }
    if (value.pinned_revision !== null && value.revision_transition.state !== "active") issue(context, ["revision_transition", "state"], "admitted successor transition must be active");
  }
  if ((value.status === "failed") !== (value.current_error !== null)) issue(context, ["current_error"], "current error is required only for failed runs");
  if (value.execution_mode === "codex_subscription_transcript" && value.capture_mode !== "transcript") issue(context, ["capture_mode"], "subscription execution requires transcript capture");
}

function validateAsyncStatus(
  value: { status: "queued" | "running" | "succeeded" | "failed"; finished_at: string | null; error: ApiErrorV1 | null },
  context: z.RefinementCtx,
): void {
  const terminal = value.status === "succeeded" || value.status === "failed";
  if (terminal !== (value.finished_at !== null)) issue(context, ["finished_at"], "finished_at is required only for terminal state");
  if ((value.status === "failed") !== (value.error !== null)) issue(context, ["error"], "error is required only for failed state");
}

function issue(context: z.RefinementCtx, path: (string | number)[], message: string): void {
  context.addIssue({ code: z.ZodIssueCode.custom, path, message });
}

function uniqueSortedBy<T extends Record<K, string>, K extends keyof T>(
  values: T[],
  key: K,
  context: z.RefinementCtx,
  path: (string | number)[],
): void {
  const ids = values.map((value) => value[key]);
  if (new Set(ids).size !== ids.length) issue(context, path, `${String(key)} values must be unique`);
  if (ids.some((id, index) => index > 0 && ids[index - 1]! > id)) issue(context, path, `${String(key)} values must be sorted`);
}

function isCanonicalJsonObject(value: string): boolean {
  try {
    const decoded: unknown = JSON.parse(value);
    if (decoded === null || Array.isArray(decoded) || typeof decoded !== "object") return false;
    const validation = validateCapabilityJson(decoded);
    return validation && JSON.stringify(sortJson(decoded as Record<string, unknown>)) === value;
  } catch {
    return false;
  }
}

function validateCapabilityJson(value: unknown): boolean {
  let nodes = 0;
  let collectionItems = 0;
  const pending: Array<[unknown, number]> = [[value, 1]];
  while (pending.length > 0) {
    const [current, depth] = pending.pop()!;
    nodes += 1;
    if (nodes > 8_192 || depth > 16) return false;
    if (typeof current === "number" && (!Number.isFinite(current) || (Number.isInteger(current) && !Number.isSafeInteger(current)))) return false;
    if (Array.isArray(current)) {
      collectionItems += current.length;
      current.forEach((child) => pending.push([child, depth + 1]));
    } else if (current !== null && typeof current === "object") {
      const children = Object.values(current);
      collectionItems += children.length;
      children.forEach((child) => pending.push([child, depth + 1]));
    }
    if (collectionItems > 4_096) return false;
  }
  return true;
}

function sortJson(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortJson);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0).map(([key, child]) => [key, sortJson(child)]));
  }
  return value;
}

function sameValue(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function isAllowedAgentTargetPath(value: string): boolean {
  if (["AGENTS.md", "agents.md", "CLAUDE.md", "GEMINI.md"].includes(value)) return true;
  return value.startsWith(".openhands/microagents/") && value.endsWith(".md") && !value.slice(".openhands/microagents/".length).includes("/") && value !== ".openhands/microagents/.md" && !value.includes("..");
}

function isLoopbackEndpoint(value: string): boolean {
  const url = new URL(value);
  return url.protocol === "http:" && ["127.0.0.1", "[::1]", "::1"].includes(url.hostname) && !url.username && !url.password;
}

function isSafeProxyUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password && (url.pathname === "" || url.pathname === "/") && !url.search && !url.hash;
  } catch {
    return false;
  }
}

function validateBoundedJson(value: Record<string, SafeJsonValue>, context: z.RefinementCtx): void {
  let nodes = 0;
  let textBytes = 0;
  let encodedBytes = 2;
  const pending: Array<[SafeJsonValue, number, (string | number)[]]> = [[value, 1, []]];
  while (pending.length > 0) {
    const [current, depth, path] = pending.pop()!;
    nodes += 1;
    if (nodes > MAX_JSON_NODES) return issue(context, path, "JSON exceeds the node budget");
    if (depth > MAX_JSON_DEPTH) return issue(context, path, "JSON exceeds the depth budget");
    if (Array.isArray(current)) {
      if (current.length > MAX_JSON_COLLECTION_ITEMS) return issue(context, path, "JSON array exceeds the item budget");
      current.forEach((child, index) => pending.push([child, depth + 1, [...path, index]]));
    } else if (current !== null && typeof current === "object") {
      const entries = Object.entries(current);
      if (entries.length > MAX_JSON_COLLECTION_ITEMS) return issue(context, path, "JSON object exceeds the item budget");
      for (const [key, child] of entries) {
        if (!key || key.length > 256 || key !== key.trim()) return issue(context, [...path, key], "JSON keys must be short trimmed strings");
        const size = utf8ByteLength(key);
        textBytes += size;
        encodedBytes += size + 4;
        pending.push([child, depth + 1, [...path, key]]);
      }
    } else if (typeof current === "string") {
      const size = utf8ByteLength(current);
      textBytes += size;
      encodedBytes += size + 2;
    } else if (typeof current === "number") {
      encodedBytes += Number.isInteger(current) ? String(current).length : 32;
    } else {
      encodedBytes += 5;
    }
    if (textBytes > MAX_JSON_TEXT_BYTES) return issue(context, path, "JSON exceeds the text budget");
    if (encodedBytes > MAX_JSON_TOTAL_BYTES) return issue(context, path, "JSON exceeds the byte budget");
  }
}

function utf8ByteLength(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function isNetworkHost(value: string): boolean {
  if (value !== value.trim() || CONTROL_CHARACTERS.test(value) || ["/", "\\", "://", "@"].some((marker) => value.includes(marker))) return false;
  if (isIpv6Address(value)) return true;
  const hostname = value.endsWith(".") ? value.slice(0, -1) : value;
  return hostname.length > 0 && hostname.split(".").every((label) => /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/.test(label));
}

function isIpv6Address(value: string): boolean {
  const scopeSeparator = value.indexOf("%");
  const address = scopeSeparator === -1 ? value : value.slice(0, scopeSeparator);
  const scope = scopeSeparator === -1 ? null : value.slice(scopeSeparator + 1);
  if (scope === "" || (scope !== null && scope.includes("%")) || !address.includes(":")) return false;
  const compressed = address.split("::");
  if (compressed.length > 2) return false;
  const groups = compressed.flatMap((side) => (side === "" ? [] : side.split(":")));
  let units = 0;
  for (const [index, group] of groups.entries()) {
    if (group.includes(".")) {
      if (index !== groups.length - 1 || !isIpv4Address(group)) return false;
      units += 2;
    } else {
      if (!/^[0-9A-Fa-f]{1,4}$/.test(group)) return false;
      units += 1;
    }
  }
  return compressed.length === 2 ? units < 8 : units === 8;
}

function isIpv4Address(value: string): boolean {
  const octets = value.split(".");
  return octets.length === 4 && octets.every((octet) => /^(?:0|[1-9]\d{0,2})$/.test(octet) && Number(octet) <= 255);
}
