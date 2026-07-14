import { z } from "zod";

export const MAX_PAGE_SIZE = 100;
export const MAX_JSON_DEPTH = 16;
export const MAX_JSON_NODES = 8_192;
export const MAX_JSON_COLLECTION_ITEMS = 1_024;
export const MAX_JSON_TEXT_BYTES = 262_144;
export const MAX_JSON_TOTAL_BYTES = 1_048_576;

const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f]/;
const UTC_RFC3339 = /^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,9})?Z$/;
const SHA256 = /^[0-9a-f]{64}$/;
const SENSITIVE_DYNAMIC_KEY = /(^|_)(password|passphrase|secret|access_token|refresh_token|bearer_token|authorization|private_key|credential_ref|command|stdout|stderr|host_path|remote_path|backend_url|core_url|file_uri)($|_)/i;

export const schemaVersionV1Schema = z.literal("1");
export const opaqueIdSchema = z
  .string()
  .min(1)
  .max(256)
  .refine((value) => value === value.trim() && !CONTROL_CHARACTERS.test(value), "must be trimmed text without control characters");
export const shortTextSchema = z.string().min(1).max(512).refine((value) => !value.includes("\0"));
export const longTextSchema = z.string().min(1).max(65_536).refine((value) => !value.includes("\0"));
export const utcTimestampSchema = z.string().regex(UTC_RFC3339, "must be a UTC RFC 3339 timestamp");
export const sha256DigestSchema = z.string().regex(SHA256, "must be lowercase SHA-256 hex");
export const etagSchema = z.string().regex(/^"[0-9a-f]{64}"$/);
export const executionModeV1Schema = z.enum(["codex_subscription_transcript", "self-deployed"]);

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

export const providerKindSchema = z.enum(["desktop_sidecar", "contract_simulator", "scaffold", "dry_run"]);
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
    api_name: z.literal("openevo-desktop-local-api"),
    preferred_major: z.literal(1),
    supported_majors: z.array(z.literal(1)).min(1),
    openapi_sha256: sha256DigestSchema,
    build_version: shortTextSchema,
    source_commit: z.string().regex(/^[0-9a-f]{7,40}$/),
    build_channel: z.enum(["release", "development", "test"]),
    provider_kind: providerKindSchema,
    feature_flags: z.array(featureFlagV1Schema),
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
    session_token: z.string().min(32).max(4096).refine((value) => !CONTROL_CHARACTERS.test(value)),
    negotiated_contract: negotiatedContractV1Schema,
  })
  .strict();

export const healthV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    service: z.literal("openevo-desktop-sidecar"),
    status: z.enum(["ok", "degraded", "starting"]),
    checked_at: utcTimestampSchema,
  })
  .strict();

export const apiErrorV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    request_id: opaqueIdSchema,
    code: z.string().regex(/^[a-z][a-z0-9_]{0,127}$/),
    http_status: z.number().int().min(400).max(599),
    message: shortTextSchema,
    severity: z.enum(["info", "warning", "blocking"]),
    category: z.enum(["contract", "authentication", "profile", "connection", "project", "capability", "operation", "run", "artifact", "service", "diagnostic", "maintenance"]),
    retryable: z.boolean(),
    repair_action: z.enum(["none", "openevo_can_retry", "user_input_required", "reconnect_required", "upgrade_required"]),
    next_action: shortTextSchema.nullable().default(null),
    details: safeJsonObjectSchema.default({}),
    logs_ref: opaqueIdSchema.nullable().default(null),
  })
  .strict();

export const contractNegotiationV1Schema = z
  .object({ selected_major: z.literal(1), desktop_openapi_sha256: sha256DigestSchema, core_openapi_sha256: sha256DigestSchema.nullable().default(null), compatible: z.boolean() })
  .strict();
export const coreConnectionStateV1Schema = z
  .object({
    state: z.enum(["disconnected", "connecting", "host_key_required", "bootstrapping", "tunnel_ready", "core_ready", "incompatible", "failed"]),
    profile_id: opaqueIdSchema.nullable().default(null),
    active_tunnel: z.boolean(),
    last_error_code: shortTextSchema.nullable().default(null),
  })
  .strict();
export const activeProjectStateV1Schema = z
  .object({ project_id: opaqueIdSchema, project_etag: etagSchema, profile_id: opaqueIdSchema, connection_state: z.enum(["offline", "connecting", "ready", "blocked"]) })
  .strict();
export const desktopStateV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    observed_at: utcTimestampSchema,
    contract: contractNegotiationV1Schema,
    core: coreConnectionStateV1Schema,
    active_project: activeProjectStateV1Schema.nullable().default(null),
    pending_operation_ids: z.array(opaqueIdSchema).default([]),
  })
  .strict();

export const credentialSlotStatusSchema = z
  .object({
    kind: z.enum(["ssh_password", "ssh_private_key", "ssh_private_key_passphrase", "http_proxy_password", "https_proxy_password"]),
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
const trimmedNetworkText = (maximum: number) =>
  z
    .string()
    .min(1)
    .max(maximum)
    .refine((value) => value === value.trim() && !CONTROL_CHARACTERS.test(value));
const remoteProfileFields = {
  name: shortTextSchema,
  host: trimmedNetworkText(253),
  port: z.number().int().min(1).max(65_535),
  user: trimmedNetworkText(128),
  authentication_kind: authenticationKindSchema,
  proxy: networkProxyV1Schema,
};
export const profileCreateV1Schema = z.object(remoteProfileFields).strict();
export const profilePatchV1Schema = z.object(remoteProfileFields).partial().strict().refine((value) => Object.keys(value).length > 0, "profile patch must not be empty");
export const remoteProfileV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    profile_id: opaqueIdSchema,
    ...remoteProfileFields,
    credential_slots: z.array(credentialSlotStatusSchema).default([]),
    connection_state: z.enum(["disconnected", "connecting", "host_key_required", "connected", "failed"]),
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

export const contentRefV1Schema = z.object({ content_id: opaqueIdSchema, sha256: sha256DigestSchema, byte_size: z.number().int().min(0).max(1_000_000_000_000) }).strict();
export const executionSettingsV1Schema = z
  .object({
    mode: executionModeV1Schema,
    capture_mode: z.literal("transcript"),
    token_level_metrics_available: z.literal(false),
    codex_model: shortTextSchema.nullable().default(null),
    managed_model_id: opaqueIdSchema.nullable().default(null),
  })
  .strict()
  .superRefine((value, context) => {
    const subscription = value.mode === "codex_subscription_transcript";
    if (subscription !== (value.codex_model !== null) || subscription === (value.managed_model_id !== null)) issue(context, [], "execution mode and model fields do not agree");
  });
export const projectTaskV1Schema = z.object({ title: shortTextSchema, objective: longTextSchema, task_ref: contentRefV1Schema.nullable().default(null) }).strict();
export const projectSourceV1Schema = z
  .object({ kind: z.enum(["scratch", "native_folder_snapshot", "git_snapshot", "remote_snapshot"]), display_name: shortTextSchema, source_ref: contentRefV1Schema.nullable().default(null) })
  .strict()
  .superRefine((value, context) => {
    if ((value.kind === "scratch") !== (value.source_ref === null)) issue(context, ["source_ref"], "snapshot source reference does not agree with source kind");
  });
export const evolutionTargetSelectionV1Schema = z
  .object({ enabled: z.boolean(), method: opaqueIdSchema.nullable().default(null), config: safeJsonObjectSchema.default({}) })
  .strict()
  .refine((value) => !value.enabled || value.method !== null, { path: ["method"], message: "enabled targets require a method" });
export const evolutionSelectionsV1Schema = z
  .record(opaqueIdSchema, evolutionTargetSelectionV1Schema)
  .refine((value) => Object.keys(value).length <= 128, "at most 128 evolution targets are allowed");
const projectFields = {
  name: shortTextSchema,
  profile_id: opaqueIdSchema,
  task: projectTaskV1Schema,
  source: projectSourceV1Schema,
  execution: executionSettingsV1Schema,
  evolution: evolutionSelectionsV1Schema,
};
export const projectCreateV1Schema = z.object(projectFields).strict();
export const projectPatchV1Schema = z.object(projectFields).partial().strict().refine((value) => Object.keys(value).length > 0, "project patch must not be empty");
export const projectV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    project_id: opaqueIdSchema,
    ...projectFields,
    state: z.enum(["draft", "active", "archived", "blocked"]),
    current_revision_id: opaqueIdSchema.nullable().default(null),
    etag: etagSchema,
    created_at: utcTimestampSchema,
    updated_at: utcTimestampSchema,
  })
  .strict();

export const resourceRefV1Schema = z
  .object({ resource_type: z.enum(["profile", "project", "operation", "run", "artifact", "service", "diagnostic", "maintenance"]), resource_id: opaqueIdSchema })
  .strict();
export const operationProgressV1Schema = z
  .object({ current: z.number().int().nonnegative(), total: z.number().int().positive(), label: shortTextSchema })
  .strict()
  .refine((value) => value.current <= value.total, { path: ["current"], message: "current must not exceed total" });
export const normalizedCheckV1Schema = z
  .object({
    check_id: opaqueIdSchema,
    label: shortTextSchema,
    status: z.enum(["pending", "running", "passed", "warning", "failed", "skipped"]),
    summary: shortTextSchema,
    repair_action: z.enum(["none", "openevo_can_retry", "user_input_required", "reconnect_required"]),
  })
  .strict();
export const diagnosticFindingV1Schema = z
  .object({ finding_id: opaqueIdSchema, severity: z.enum(["info", "warning", "blocking"]), category: z.enum(["desktop", "ssh", "core", "model_service", "workspace", "run", "evolution"]), summary: shortTextSchema, next_action: shortTextSchema.nullable().default(null) })
  .strict();
export const diagnosticReportV1Schema = z
  .object({ schema_version: schemaVersionV1Schema, diagnostic_id: opaqueIdSchema, status: z.enum(["healthy", "degraded", "blocked"]), generated_at: utcTimestampSchema, checks: z.array(normalizedCheckV1Schema), findings: z.array(diagnosticFindingV1Schema), etag: etagSchema })
  .strict();
const localOperationResultV1Schema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("connection"), profile_id: opaqueIdSchema, connection_state: z.enum(["connected", "disconnected", "host_key_required"]) }).strict(),
  z.object({ kind: z.literal("project"), project_id: opaqueIdSchema, project_etag: etagSchema, active: z.boolean() }).strict(),
  z.object({ kind: z.literal("diagnostic"), report: diagnosticReportV1Schema }).strict(),
]);
export const localOperationV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    operation_id: opaqueIdSchema,
    operation_kind: z.enum(["profile_connect", "profile_disconnect", "host_key_accept", "project_activate", "project_doctor", "project_repair", "bootstrap", "workspace_sync", "service_restart", "service_stop", "diagnostics", "cache_cleanup"]),
    state: z.enum(["queued", "running", "succeeded", "failed", "cancelling", "cancelled"]),
    resource: resourceRefV1Schema,
    progress: operationProgressV1Schema.nullable().default(null),
    checks: z.array(normalizedCheckV1Schema).default([]),
    result: localOperationResultV1Schema.nullable().default(null),
    error: apiErrorV1Schema.nullable().default(null),
    created_at: utcTimestampSchema,
    started_at: utcTimestampSchema.nullable().default(null),
    finished_at: utcTimestampSchema.nullable().default(null),
  })
  .strict()
  .superRefine((value, context) => {
    const terminal = ["succeeded", "failed", "cancelled"].includes(value.state);
    if (terminal !== (value.finished_at !== null)) issue(context, ["finished_at"], "terminal state and finished_at must agree");
    if ((value.state === "failed") !== (value.error !== null)) issue(context, ["error"], "error is required exactly for failed operations");
  });
export const logEntryV1Schema = z
  .object({ log_id: opaqueIdSchema, occurred_at: utcTimestampSchema, level: z.enum(["debug", "info", "warning", "error"]), source: z.enum(["desktop", "connection", "core", "run", "evolution", "service"]), message: longTextSchema, code: shortTextSchema.nullable().default(null) })
  .strict();

export const capabilitySupportAxisV1Schema = z
  .object({ supported: z.boolean(), reason_code: shortTextSchema.nullable().default(null), summary: shortTextSchema.nullable().default(null) })
  .strict()
  .superRefine((value, context) => {
    if (value.supported !== (value.reason_code === null && value.summary === null)) issue(context, [], "support axis reason does not agree with supported state");
  });
export const methodSupportV1Schema = z
  .object({ overall: z.enum(["supported", "unsupported"]), execution: capabilitySupportAxisV1Schema, capture: capabilitySupportAxisV1Schema, harness: capabilitySupportAxisV1Schema, runtime: capabilitySupportAxisV1Schema })
  .strict();
export const resolvedMethodCapabilityV1Schema = z.object({ method_id: opaqueIdSchema, identity_digest: sha256DigestSchema, support: methodSupportV1Schema }).strict();
export const methodCapabilityV1Schema = z
  .object({ method_id: opaqueIdSchema, display_name: shortTextSchema, description: shortTextSchema, maturity: z.enum(["experimental", "preview", "stable"]), identity_digest: sha256DigestSchema, config_schema: safeJsonObjectSchema, default_config: safeJsonObjectSchema, support: methodSupportV1Schema })
  .strict();
export const selectionResolverCapabilityV1Schema = z
  .object({ selection_value: opaqueIdSchema, display_name: shortTextSchema, description: shortTextSchema, resolved_methods: z.array(resolvedMethodCapabilityV1Schema) })
  .strict();
export const targetCapabilityV1Schema = z
  .object({ target_id: opaqueIdSchema, display_name: shortTextSchema, description: shortTextSchema, artifact_type: z.enum(["text_memory", "skill_bundle", "agent_system", "parametric_memory"]), release_enabled: z.boolean(), configured_default_method_id: opaqueIdSchema, effective_default_method_id: opaqueIdSchema.nullable().default(null), methods: z.array(methodCapabilityV1Schema), accepted_methods: z.array(resolvedMethodCapabilityV1Schema), selection_resolvers: z.array(selectionResolverCapabilityV1Schema).default([]) })
  .strict();
export const projectCapabilitiesV1Schema = z
  .object({ schema_version: schemaVersionV1Schema, project_id: opaqueIdSchema, execution_mode: executionModeV1Schema, source: z.literal("verified_remote_core"), registry_verified: z.literal(true), registry_digest: sha256DigestSchema, core_version: shortTextSchema, fetched_at: utcTimestampSchema, targets: z.array(targetCapabilityV1Schema) })
  .strict();

export const projectValidateRequestV1Schema = z
  .object({ project_etag: etagSchema, capability_registry_digest: sha256DigestSchema, execution: executionSettingsV1Schema, evolution: evolutionSelectionsV1Schema })
  .strict();
export const validationIssueV1Schema = z
  .object({ issue_id: opaqueIdSchema, severity: z.enum(["warning", "blocking"]), field: shortTextSchema, code: shortTextSchema, message: shortTextSchema, next_action: shortTextSchema.nullable().default(null) })
  .strict();
export const projectValidationV1Schema = z
  .object({ schema_version: schemaVersionV1Schema, project_id: opaqueIdSchema, project_etag: etagSchema, capability_registry_digest: sha256DigestSchema, valid: z.boolean(), issues: z.array(validationIssueV1Schema), validated_at: utcTimestampSchema })
  .strict()
  .refine((value) => value.valid !== value.issues.some((entry) => entry.severity === "blocking"), { path: ["valid"], message: "validity must agree with blocking issues" });

export const immutableSnapshotRefV1Schema = z.object({ snapshot_id: opaqueIdSchema, digest: sha256DigestSchema }).strict();
export const revisionRefV1Schema = z
  .object({ revision_id: opaqueIdSchema, generation: z.number().int().nonnegative(), manifest_digest: sha256DigestSchema, state: z.enum(["active", "queued", "preparing", "failed", "cancelled"]) })
  .strict();
export const runCreateV1Schema = z
  .object({ project_id: opaqueIdSchema, project_snapshot: immutableSnapshotRefV1Schema, task_snapshot: immutableSnapshotRefV1Schema, workspace_snapshot: immutableSnapshotRefV1Schema, capability_registry_digest: sha256DigestSchema, required_revision: revisionRefV1Schema })
  .strict()
  .refine((value) => value.required_revision.state === "active", { path: ["required_revision", "state"], message: "required revision must be active" });
export const runQueuedReasonV1Schema = z
  .object({ code: z.enum(["capacity_unavailable", "required_revision_uncommitted", "service_starting", "project_activation_pending"]), summary: shortTextSchema, retry_after_seconds: z.number().int().min(1).max(86_400).nullable().default(null) })
  .strict();
export const runAttemptV1Schema = z
  .object({ attempt_id: opaqueIdSchema, number: z.number().int().positive(), state: z.enum(["queued", "preparing", "running", "cancelling", "succeeded", "failed", "cancelled"]), started_at: utcTimestampSchema.nullable().default(null), finished_at: utcTimestampSchema.nullable().default(null) })
  .strict();
export const runV1Schema = z
  .object({
    schema_version: schemaVersionV1Schema,
    run_id: opaqueIdSchema,
    project_id: opaqueIdSchema,
    state: z.enum(["queued", "preparing", "running", "cancelling", "succeeded", "failed", "cancelled"]),
    queued_reason: runQueuedReasonV1Schema.nullable().default(null),
    project_snapshot: immutableSnapshotRefV1Schema,
    task_snapshot: immutableSnapshotRefV1Schema,
    workspace_snapshot: immutableSnapshotRefV1Schema,
    capability_registry_digest: sha256DigestSchema,
    pinned_revision: revisionRefV1Schema,
    successor_revision: revisionRefV1Schema.nullable().default(null),
    latest_attempt: runAttemptV1Schema,
    created_at: utcTimestampSchema,
    updated_at: utcTimestampSchema,
    etag: etagSchema,
    error: apiErrorV1Schema.nullable().default(null),
  })
  .strict()
  .superRefine((value, context) => {
    if ((value.state === "queued") !== (value.queued_reason !== null)) issue(context, ["queued_reason"], "queued reason must agree with run state");
    if ((value.state === "failed") !== (value.error !== null)) issue(context, ["error"], "error is required exactly for failed runs");
  });
export const timelineEntryV1Schema = z
  .object({ entry_id: opaqueIdSchema, occurred_at: utcTimestampSchema, stage: z.enum(["admission", "workspace", "agent", "capture", "dataset", "evolution", "materialization", "revision"]), state: z.enum(["queued", "running", "succeeded", "failed", "cancelled", "blocked"]), title: shortTextSchema, summary: shortTextSchema, progress: operationProgressV1Schema.nullable().default(null) })
  .strict();
export const contextContributionV1Schema = z
  .object({ target_id: opaqueIdSchema, artifact_id: opaqueIdSchema, artifact_type: z.enum(["text_memory", "skill_bundle", "agent_system", "parametric_memory"]), selected: z.boolean(), summary: shortTextSchema })
  .strict();
export const runContextV1Schema = z
  .object({ schema_version: schemaVersionV1Schema, run_id: opaqueIdSchema, pinned_revision: revisionRefV1Schema, successor_revision: revisionRefV1Schema.nullable().default(null), contributions: z.array(contextContributionV1Schema) })
  .strict();

export const artifactLineageV1Schema = z
  .object({ source_dataset_ids: z.array(opaqueIdSchema).default([]), parent_artifact_ids: z.array(opaqueIdSchema).default([]), producing_job_id: opaqueIdSchema.nullable().default(null) })
  .strict();
export const artifactCompatibilityV1Schema = z
  .object({ execution_modes: z.array(executionModeV1Schema), harness_ids: z.array(opaqueIdSchema).default([]), base_model_ids: z.array(opaqueIdSchema).default([]) })
  .strict();
export const artifactScoreV1Schema = z.object({ name: z.string().regex(/^[a-z][a-z0-9_]{0,127}$/), value: z.number().finite() }).strict();
const artifactBase = {
  schema_version: schemaVersionV1Schema,
  artifact_id: opaqueIdSchema,
  project_id: opaqueIdSchema,
  run_id: opaqueIdSchema,
  target_id: opaqueIdSchema,
  display_name: shortTextSchema,
  summary: shortTextSchema,
  content_digest: sha256DigestSchema,
  byte_size: z.number().int().min(0).max(1_000_000_000_000),
  lineage: artifactLineageV1Schema,
  compatibility: artifactCompatibilityV1Schema,
  scores: z.array(artifactScoreV1Schema).default([]),
  selected: z.boolean(),
  promoted: z.boolean(),
  revision_ids: z.array(opaqueIdSchema).default([]),
  created_at: utcTimestampSchema,
};
export const textMemoryArtifactV1Schema = z.object({ ...artifactBase, artifact_type: z.literal("text_memory"), format: z.enum(["markdown", "plain_text"]) }).strict();
export const skillBundleArtifactV1Schema = z.object({ ...artifactBase, artifact_type: z.literal("skill_bundle"), skill_count: z.number().int().min(1).max(1_024) }).strict();
export const agentSystemArtifactV1Schema = z.object({ ...artifactBase, artifact_type: z.literal("agent_system"), instruction_kind: z.enum(["agents", "claude", "gemini", "openhands_microagent", "generic"]) }).strict();
export const parametricMemoryArtifactV1Schema = z.object({ ...artifactBase, artifact_type: z.literal("parametric_memory"), release_enabled: z.literal(false), adapter_id: opaqueIdSchema, base_model_id: opaqueIdSchema, adapter_format: shortTextSchema }).strict();
export const artifactV1Schema = z.discriminatedUnion("artifact_type", [textMemoryArtifactV1Schema, skillBundleArtifactV1Schema, agentSystemArtifactV1Schema, parametricMemoryArtifactV1Schema]);
export const artifactDocumentV1Schema = z.object({ document_id: opaqueIdSchema, title: shortTextSchema, media_type: z.enum(["text/markdown", "text/plain"]), content: longTextSchema }).strict();
export const artifactContentV1Schema = z.object({ schema_version: schemaVersionV1Schema, artifact_id: opaqueIdSchema, content_digest: sha256DigestSchema, documents: z.array(artifactDocumentV1Schema).min(1).max(1_024) }).strict();
export const diffLineV1Schema = z.object({ kind: z.enum(["context", "added", "removed"]), old_line: z.number().int().positive().nullable().default(null), new_line: z.number().int().positive().nullable().default(null), text: longTextSchema }).strict();
export const diffHunkV1Schema = z.object({ hunk_id: opaqueIdSchema, heading: shortTextSchema, lines: z.array(diffLineV1Schema).max(10_000) }).strict();
export const artifactDiffV1Schema = z.object({ schema_version: schemaVersionV1Schema, artifact_id: opaqueIdSchema, base_artifact_id: opaqueIdSchema.nullable().default(null), hunks: z.array(diffHunkV1Schema).max(1_024), truncated: z.boolean() }).strict();
export const serviceV1Schema = z
  .object({ schema_version: schemaVersionV1Schema, service_id: opaqueIdSchema, display_name: shortTextSchema, kind: z.enum(["core", "gateway", "model", "worker", "artifact_store"]), state: z.enum(["starting", "healthy", "degraded", "stopped", "failed", "unavailable"]), health_summary: shortTextSchema, restart_supported: z.boolean(), observed_at: utcTimestampSchema })
  .strict();
export const diagnosticCreateV1Schema = z
  .object({ scope: z.enum(["active_project", "connection", "core", "run", "services"]), resource_id: opaqueIdSchema.nullable().default(null) })
  .strict()
  .superRefine((value, context) => {
    const resourceScope = value.scope === "run" || value.scope === "services";
    if (resourceScope !== (value.resource_id !== null)) issue(context, ["resource_id"], "resource ID must agree with diagnostic scope");
  });

const stateEventV1Schema = z.object({ kind: z.literal("state_changed"), state: desktopStateV1Schema }).strict();
const resourceEventV1Schema = z.object({ kind: z.enum(["profile_changed", "project_changed", "operation_changed", "run_changed", "artifact_available", "service_changed"]), resource: resourceRefV1Schema, change: z.enum(["created", "updated", "deleted"]) }).strict();
const timelineEventV1Schema = z.object({ kind: z.literal("run_timeline"), run_id: opaqueIdSchema, entry: timelineEntryV1Schema }).strict();
const logEventV1Schema = z.object({ kind: z.literal("log_appended"), resource: resourceRefV1Schema, entry: logEntryV1Schema }).strict();
const diagnosticEventV1Schema = z.object({ kind: z.literal("diagnostic_ready"), diagnostic_id: opaqueIdSchema, operation_id: opaqueIdSchema }).strict();
const heartbeatEventV1Schema = z.object({ kind: z.literal("heartbeat") }).strict();
export const eventDataV1Schema = z.discriminatedUnion("kind", [stateEventV1Schema, resourceEventV1Schema, timelineEventV1Schema, logEventV1Schema, diagnosticEventV1Schema, heartbeatEventV1Schema]);
export const eventNameV1Schema = z.enum(["desktop.v1.state.changed", "desktop.v1.profile.changed", "desktop.v1.project.changed", "desktop.v1.operation.changed", "desktop.v1.run.changed", "desktop.v1.run.timeline", "desktop.v1.log.appended", "desktop.v1.artifact.available", "desktop.v1.service.changed", "desktop.v1.diagnostic.ready", "desktop.v1.heartbeat"]);
const EVENT_BY_KIND: Record<z.infer<typeof eventDataV1Schema>["kind"], z.infer<typeof eventNameV1Schema>> = {
  state_changed: "desktop.v1.state.changed", profile_changed: "desktop.v1.profile.changed", project_changed: "desktop.v1.project.changed", operation_changed: "desktop.v1.operation.changed", run_changed: "desktop.v1.run.changed", run_timeline: "desktop.v1.run.timeline", log_appended: "desktop.v1.log.appended", artifact_available: "desktop.v1.artifact.available", service_changed: "desktop.v1.service.changed", diagnostic_ready: "desktop.v1.diagnostic.ready", heartbeat: "desktop.v1.heartbeat",
};
export const eventEnvelopeV1Schema = z
  .object({ schema_version: schemaVersionV1Schema, event_id: opaqueIdSchema, event_name: eventNameV1Schema, occurred_at: utcTimestampSchema, sequence: z.number().int().nonnegative(), data: eventDataV1Schema })
  .strict()
  .refine((value) => EVENT_BY_KIND[value.data.kind] === value.event_name, { path: ["event_name"], message: "event name must match typed event data" });
export const sseFrameV1Schema = z
  .object({ id: opaqueIdSchema, event: eventNameV1Schema, data: eventEnvelopeV1Schema })
  .strict()
  .refine((value) => value.id === value.data.event_id && value.event === value.data.event_name, {
    message: "SSE frame identity must match its event envelope",
  });

export function pageV1Schema<T extends z.ZodTypeAny>(itemSchema: T) {
  return z
    .object({ schema_version: schemaVersionV1Schema, items: z.array(itemSchema).max(MAX_PAGE_SIZE), next_cursor: opaqueIdSchema.nullable().default(null), has_more: z.boolean() })
    .strict()
    .refine((value) => value.has_more === (value.next_cursor !== null), { path: ["next_cursor"], message: "cursor must agree with has_more" });
}
export const profilePageV1Schema = pageV1Schema(remoteProfileV1Schema);
export const projectPageV1Schema = pageV1Schema(projectV1Schema);
export const runPageV1Schema = pageV1Schema(runV1Schema);
export const timelinePageV1Schema = pageV1Schema(timelineEntryV1Schema);
export const logPageV1Schema = pageV1Schema(logEntryV1Schema);
export const artifactPageV1Schema = pageV1Schema(artifactV1Schema);
export const servicePageV1Schema = pageV1Schema(serviceV1Schema);
export const emptyActionV1Schema = z.object({}).strict();

export const versionV1Schema = versionInfoV1Schema;
export const remoteProfileCreateV1Schema = profileCreateV1Schema;
export const remoteProfilePatchV1Schema = profilePatchV1Schema;
export const capabilitiesEnvelopeV1Schema = projectCapabilitiesV1Schema;
export const projectValidationRequestV1Schema = projectValidateRequestV1Schema;
export const diagnosticRequestV1Schema = diagnosticCreateV1Schema;

export type VersionInfoV1 = z.infer<typeof versionInfoV1Schema>;
export type DesktopBootstrapContextV1 = z.infer<typeof desktopBootstrapContextV1Schema>;
export type HealthV1 = z.infer<typeof healthV1Schema>;
export type ApiErrorV1 = z.infer<typeof apiErrorV1Schema>;
export type DesktopStateV1 = z.infer<typeof desktopStateV1Schema>;
export type RemoteProfileV1 = z.infer<typeof remoteProfileV1Schema>;
export type ProfileCreateV1 = z.input<typeof profileCreateV1Schema>;
export type ProfilePatchV1 = z.input<typeof profilePatchV1Schema>;
export type HostKeyAcceptV1 = z.input<typeof hostKeyAcceptV1Schema>;
export type ProjectV1 = z.infer<typeof projectV1Schema>;
export type ProjectCreateV1 = z.input<typeof projectCreateV1Schema>;
export type ProjectPatchV1 = z.input<typeof projectPatchV1Schema>;
export type LocalOperationV1 = z.infer<typeof localOperationV1Schema>;
export type RunV1 = z.infer<typeof runV1Schema>;
export type RunCreateV1 = z.input<typeof runCreateV1Schema>;
export type TimelineEntryV1 = z.infer<typeof timelineEntryV1Schema>;
export type LogEntryV1 = z.infer<typeof logEntryV1Schema>;
export type RunContextV1 = z.infer<typeof runContextV1Schema>;
export type ArtifactV1 = z.infer<typeof artifactV1Schema>;
export type ArtifactContentV1 = z.infer<typeof artifactContentV1Schema>;
export type ArtifactDiffV1 = z.infer<typeof artifactDiffV1Schema>;
export type ServiceV1 = z.infer<typeof serviceV1Schema>;
export type DiagnosticReportV1 = z.infer<typeof diagnosticReportV1Schema>;
export type DiagnosticCreateV1 = z.input<typeof diagnosticCreateV1Schema>;
export type ProjectCapabilitiesV1 = z.infer<typeof projectCapabilitiesV1Schema>;
export type ProjectValidateRequestV1 = z.input<typeof projectValidateRequestV1Schema>;
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
export type ProjectValidationRequestV1 = ProjectValidateRequestV1;
export type DiagnosticRequestV1 = DiagnosticCreateV1;

function issue(context: z.RefinementCtx, path: (string | number)[], message: string): void {
  context.addIssue({ code: z.ZodIssueCode.custom, path, message });
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
        if (SENSITIVE_DYNAMIC_KEY.test(key)) return issue(context, [...path, key], "sensitive or implementation-detail fields are forbidden");
        const size = new TextEncoder().encode(key).byteLength;
        textBytes += size;
        encodedBytes += size + 4;
        pending.push([child, depth + 1, [...path, key]]);
      }
    } else if (typeof current === "string") {
      const size = new TextEncoder().encode(current).byteLength;
      textBytes += size;
      encodedBytes += size + 2;
    } else {
      encodedBytes += typeof current === "number" ? 32 : 5;
    }
    if (textBytes > MAX_JSON_TEXT_BYTES) return issue(context, path, "JSON exceeds the text budget");
    if (encodedBytes > MAX_JSON_TOTAL_BYTES) return issue(context, path, "JSON exceeds the byte budget");
  }
}
