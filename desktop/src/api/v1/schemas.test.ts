import { describe, expect, it } from "vitest";
import criticalFixture from "../../../sidecar/contracts/v1/fixtures/contract-critical.json";
import { CONTRACT_FIXTURE_V1, EVENT_FIXTURE_V1, PROFILE_PAGE_FIXTURE_V1, RUN_PAGE_FIXTURE_V1 } from "./fixtures";
import {
  apiErrorV1Schema,
  artifactContentV1Schema,
  artifactDiffV1Schema,
  artifactV1Schema,
  cacheCleanupRequestV1Schema,
  capabilitiesEnvelopeV1Schema,
  desktopBootstrapContextV1Schema,
  desktopStateV1Schema,
  diagnosticCreateV1Schema,
  diagnosticReportV1Schema,
  eventEnvelopeV1Schema,
  executionSettingsV1Schema,
  executionModeCapabilitiesV1Schema,
  healthV1Schema,
  localLogEntryV1Schema,
  localOperationV1Schema,
  logEntryV1Schema,
  operationV1Schema,
  profileCreateV1Schema,
  profilePageV1Schema,
  profilePatchV1Schema,
  projectCreateV1Schema,
  projectPatchV1Schema,
  projectV1Schema,
  projectValidationV1Schema,
  remoteProfileV1Schema,
  runContextV1Schema,
  runCreateV1Schema,
  runPageV1Schema,
  runSummaryV1Schema,
  runV1Schema,
  serviceV1Schema,
  timelineEntryV1Schema,
  versionInfoV1Schema,
  workspaceImportRefV1Schema,
} from "./schemas";

describe("Desktop Local API v1 schemas", () => {
  it("parses deterministic fixtures across local and Core response families", () => {
    expect(versionInfoV1Schema.parse(CONTRACT_FIXTURE_V1.version).preferred_major).toBe(1);
    expect(desktopBootstrapContextV1Schema.parse(CONTRACT_FIXTURE_V1.bootstrap).endpoint).toContain("127.0.0.1");
    expect(healthV1Schema.parse(CONTRACT_FIXTURE_V1.health).status).toBe("ok");
    expect(apiErrorV1Schema.parse(CONTRACT_FIXTURE_V1.error).code).toBe("project_not_ready");
    expect(desktopStateV1Schema.parse(CONTRACT_FIXTURE_V1.state).contract.compatible).toBe(true);
    expect(remoteProfileV1Schema.parse(CONTRACT_FIXTURE_V1.profile).authentication_kind).toBe("ssh_agent");
    expect(remoteProfileV1Schema.parse(CONTRACT_FIXTURE_V1.profile).credential_slots).toEqual([]);
    expect(projectV1Schema.parse(CONTRACT_FIXTURE_V1.project).remote?.model_preparation.status).toBe("ready");
    expect(localOperationV1Schema.parse(CONTRACT_FIXTURE_V1.operation).state).toBe("running");
    expect(localLogEntryV1Schema.parse(CONTRACT_FIXTURE_V1.operationLog).source).toBe("connection");
    expect(runSummaryV1Schema.parse(CONTRACT_FIXTURE_V1.runSummary).pinned_revision?.generation).toBe(1);
    expect(runV1Schema.parse(CONTRACT_FIXTURE_V1.run).attempts).toHaveLength(1);
    expect(timelineEntryV1Schema.parse(CONTRACT_FIXTURE_V1.timeline).phase).toBe("execution");
    expect(logEntryV1Schema.parse(CONTRACT_FIXTURE_V1.log).stream).toBe("agent");
    expect(runContextV1Schema.parse(CONTRACT_FIXTURE_V1.context).artifacts).toHaveLength(1);
    expect(CONTRACT_FIXTURE_V1.artifacts.map((artifact) => artifactV1Schema.parse(artifact).artifact_type)).toEqual([
      "text_memory",
      "skill_bundle",
      "agent_system",
      "parametric_memory",
    ]);
    expect(artifactContentV1Schema.parse(CONTRACT_FIXTURE_V1.artifactContent).returned_utf8_bytes).toBe(29);
    expect(artifactDiffV1Schema.parse(CONTRACT_FIXTURE_V1.artifactDiff).total_lines).toBe(1);
    expect(serviceV1Schema.parse(CONTRACT_FIXTURE_V1.service).status).toBe("running");
    expect(operationV1Schema.parse(CONTRACT_FIXTURE_V1.serviceOperation).kind).toBe("service_restart");
    expect(diagnosticCreateV1Schema.parse(CONTRACT_FIXTURE_V1.diagnosticRequest).scopes).toHaveLength(1);
    expect(diagnosticReportV1Schema.parse(CONTRACT_FIXTURE_V1.diagnostic).status).toBe("running");
    expect(cacheCleanupRequestV1Schema.parse(CONTRACT_FIXTURE_V1.cacheCleanupRequest).older_than_days).toBe(30);
    expect(operationV1Schema.parse(CONTRACT_FIXTURE_V1.cacheOperation).status).toBe("running");
    expect(capabilitiesEnvelopeV1Schema.parse(CONTRACT_FIXTURE_V1.capabilities).capabilities.registry_digest).toHaveLength(64);
    expect(projectValidationV1Schema.parse(CONTRACT_FIXTURE_V1.validation).checks[0]?.status).toBe("ok");
  });

  it("parses the Python-owned cross-language critical fixture", () => {
    expect(healthV1Schema.parse(criticalFixture.health).protocol).toBe("openevo-native-sidecar-v1");
    expect(desktopStateV1Schema.parse(criticalFixture.state).core.state).toBe("online");
    expect(runCreateV1Schema.parse(criticalFixture.run_create)).toEqual({ project_id: "project-fixture-1" });
    expect(profileCreateV1Schema.parse(criticalFixture.profile_create.wire)).toEqual(criticalFixture.profile_create.normalized);
    expect(executionSettingsV1Schema.parse(criticalFixture.execution.wire)).toEqual(criticalFixture.execution.normalized);
    expect(profilePatchV1Schema.parse(criticalFixture.profile_patch.wire)).toEqual(criticalFixture.profile_patch.normalized);
    expect(workspaceImportRefV1Schema.parse(criticalFixture.workspace_import)).toEqual(criticalFixture.workspace_import);
    expect(localOperationV1Schema.parse(criticalFixture.operation_defaults.wire)).toEqual(criticalFixture.operation_defaults.normalized);
    expect(serviceV1Schema.parse(criticalFixture.service)).toEqual(criticalFixture.service);
    expect(artifactContentV1Schema.parse(criticalFixture.artifact_content).returned_utf8_bytes).toBe(29);
    expect(artifactDiffV1Schema.parse(criticalFixture.artifact_diff).document_changes[0]?.hunks[0]?.lines[0]?.text).toBe("");
  });

  it("rejects missing, duplicate, and unknown execution-mode capabilities", () => {
    const capabilities = CONTRACT_FIXTURE_V1.state.execution_mode_capabilities;
    expect(executionModeCapabilitiesV1Schema.parse(capabilities).modes.map((item) => item.support_state)).toEqual([
      "supported",
      "unavailable",
    ]);
    expect(() => desktopStateV1Schema.parse(({ ...CONTRACT_FIXTURE_V1.state, execution_mode_capabilities: undefined }))).toThrow();
    expect(() => executionModeCapabilitiesV1Schema.parse({ ...capabilities, modes: capabilities.modes.slice(0, 1) })).toThrow();
    expect(() => executionModeCapabilitiesV1Schema.parse({ ...capabilities, modes: [capabilities.modes[0], capabilities.modes[0]] })).toThrow(/duplicate/i);
    expect(() => executionModeCapabilitiesV1Schema.parse({
      ...capabilities,
      modes: [capabilities.modes[0], { ...capabilities.modes[1], mode: "future-mode" }],
    })).toThrow();
    expect(() => executionModeCapabilitiesV1Schema.parse({
      ...capabilities,
      modes: [capabilities.modes[0], { ...capabilities.modes[1], reason_code: "future_reason" }],
    })).toThrow();
  });

  it("mirrors Core admission, operation, diff, and pagination closure", () => {
    expect(() => runV1Schema.parse({ ...CONTRACT_FIXTURE_V1.run, admitted_at: null })).toThrow(/admitted/i);
    expect(() => operationV1Schema.parse({
      ...CONTRACT_FIXTURE_V1.serviceOperation,
      status: "cancelled",
      cancellation: { reason: "user_requested", requested_at: CONTRACT_FIXTURE_V1.serviceOperation.updated_at },
      finished_at: CONTRACT_FIXTURE_V1.serviceOperation.updated_at,
    })).toThrow(/cancell/i);

    const change = CONTRACT_FIXTURE_V1.artifactDiff.document_changes[0];
    expect(() => artifactDiffV1Schema.parse({
      ...CONTRACT_FIXTURE_V1.artifactDiff,
      document_changes: [{ ...change, old_document: { ...change.old_document, artifact_id: "wrong-artifact" } }],
    })).toThrow(/previous artifact/i);
    expect(() => runPageV1Schema.parse({ ...RUN_PAGE_FIXTURE_V1, has_more: true })).toThrow(/cursor/i);
  });

  it("keeps ProjectTask closed and accepts only opaque native workspace imports", () => {
    const imported = {
      ...CONTRACT_FIXTURE_V1.project,
      source: {
        kind: "native_folder_snapshot",
        display_name: "Imported workspace",
        import_ref: CONTRACT_FIXTURE_V1.workspaceImport,
      },
    } as const;
    expect(projectV1Schema.parse(imported).source.import_ref?.byte_size).toBe(1_024);

    expect(() => projectV1Schema.parse({ ...CONTRACT_FIXTURE_V1.project, task: { ...CONTRACT_FIXTURE_V1.project.task, task_ref: { content_id: "raw" } } })).toThrow();
    expect(() => projectV1Schema.parse({ ...CONTRACT_FIXTURE_V1.project, source: { ...imported.source, source_ref: "/Users/researcher/data" } })).toThrow();
    expect(() => projectV1Schema.parse({ ...CONTRACT_FIXTURE_V1.project, source: { ...imported.source, workspace_path: "/Users/researcher/data" } })).toThrow();
    expect(() => projectV1Schema.parse({ ...CONTRACT_FIXTURE_V1.project, source: { kind: "git_snapshot", display_name: "Git" } })).toThrow();
  });

  it("enforces lossless local-to-Core text and evolution ID boundaries", () => {
    const base = {
      name: "P",
      profile_id: "profile-1",
      task: { title: "T", objective: "Run the task." },
      source: { kind: "scratch", display_name: "S" },
      execution: { mode: "self-deployed", hf_model: "m" },
      evolution: { targets: { "Text_memory.v1": { enabled: true, method: "Method.v1", config: {} } } },
    } as const;
    const configured = projectCreateV1Schema.parse(base);
    expect(configured.evolution.targets["Text_memory.v1"]?.method).toBe("Method.v1");
    expect(configured.evolution_configuration_state).toBe("configured");
    expect(projectCreateV1Schema.parse({
      ...base,
      evolution: { targets: {} },
      evolution_configuration_state: "pending",
    }).evolution_configuration_state).toBe("pending");
    const missingResponseState: Record<string, unknown> = { ...CONTRACT_FIXTURE_V1.project };
    delete missingResponseState.evolution_configuration_state;
    expect(() => projectV1Schema.parse(missingResponseState)).toThrow();
    expect(projectCreateV1Schema.parse({ ...base, name: "n".repeat(128) }).name).toHaveLength(128);
    expect(projectCreateV1Schema.parse({ ...base, task: { ...base.task, title: "t".repeat(256) } }).task.title).toHaveLength(256);
    expect(projectCreateV1Schema.parse({ ...base, source: { ...base.source, display_name: "s".repeat(256) } }).source.display_name).toHaveLength(256);
    expect(projectCreateV1Schema.parse({ ...base, execution: { mode: "self-deployed", hf_model: "m".repeat(256) } }).execution.hf_model).toHaveLength(256);
    expect(projectCreateV1Schema.parse({ ...base, execution: { mode: "codex_subscription_transcript", codex_model: "c".repeat(256) } }).execution.codex_model).toHaveLength(256);

    for (const invalid of [
      { ...base, name: "n".repeat(129) },
      { ...base, task: { ...base.task, title: "t".repeat(257) } },
      { ...base, source: { ...base.source, display_name: "s".repeat(257) } },
      { ...base, execution: { mode: "self-deployed", hf_model: "m".repeat(257) } },
      { ...base, execution: { mode: "codex_subscription_transcript", codex_model: "c".repeat(257) } },
      { ...base, evolution: { targets: { "1invalid": { enabled: true, method: "Method.v1", config: {} } } } },
      { ...base, evolution: { targets: { valid: { enabled: true, method: "method/invalid", config: {} } } } },
      { ...base, evolution: { targets: { ["t".repeat(129)]: { enabled: true, method: "Method.v1", config: {} } } } },
    ]) {
      expect(() => projectCreateV1Schema.parse(invalid)).toThrow();
    }
  });

  it("matches workspace import tar bounds, alignment, and empty archive semantics", () => {
    expect(workspaceImportRefV1Schema.parse(CONTRACT_FIXTURE_V1.workspaceImport).entry_count).toBe(1);
    expect(
      workspaceImportRefV1Schema.parse({
        ...CONTRACT_FIXTURE_V1.workspaceImport,
        entry_count: 0,
        extracted_byte_size: 0,
      }).extracted_byte_size,
    ).toBe(0);

    for (const invalid of [
      { ...CONTRACT_FIXTURE_V1.workspaceImport, byte_size: 512 },
      { ...CONTRACT_FIXTURE_V1.workspaceImport, byte_size: 1_025 },
      { ...CONTRACT_FIXTURE_V1.workspaceImport, byte_size: 16 * 1024 * 1024 * 1024 + 512 },
      { ...CONTRACT_FIXTURE_V1.workspaceImport, entry_count: 100_001 },
      { ...CONTRACT_FIXTURE_V1.workspaceImport, entry_count: 0, extracted_byte_size: 1 },
      { ...CONTRACT_FIXTURE_V1.workspaceImport, extracted_byte_size: 16 * 1024 * 1024 * 1024 + 1 },
    ]) {
      expect(() => workspaceImportRefV1Schema.parse(invalid)).toThrow();
    }
  });

  it("preserves complete Core revision and model-preparation state", () => {
    const remote = projectV1Schema.parse(CONTRACT_FIXTURE_V1.project).remote!;
    expect(remote.active_revision).toEqual({
      id: "revision-fixture-1",
      project_id: "project-fixture-1",
      generation: 1,
      manifest_sha256: "c".repeat(64),
    });
    expect(remote.model_preparation).toMatchObject({
      model_ref: "open-models/research-model-fixture-1",
      downloaded_bytes: 1_024,
      total_bytes: 1_024,
    });
    expect(() =>
      projectV1Schema.parse({
        ...CONTRACT_FIXTURE_V1.project,
        remote: { ...CONTRACT_FIXTURE_V1.project.remote, active_revision: { id: "lossy-revision" } },
      }),
    ).toThrow();
    expect(() =>
      projectV1Schema.parse({
        ...CONTRACT_FIXTURE_V1.project,
        remote: {
          ...CONTRACT_FIXTURE_V1.project.remote,
          model_preparation: { ...CONTRACT_FIXTURE_V1.project.remote.model_preparation, status: "downloading" },
        },
      }),
    ).toThrow();
    expect(() =>
      projectV1Schema.parse({
        ...CONTRACT_FIXTURE_V1.project,
        remote: { ...CONTRACT_FIXTURE_V1.project.remote, core_project_id: "different-project" },
      }),
    ).toThrow();
  });

  it("accepts queued runs only without a pin and keeps required revision and transition", () => {
    const queued = runSummaryV1Schema.parse(CONTRACT_FIXTURE_V1.queuedRunSummary);
    expect(queued.pinned_revision).toBeNull();
    expect(queued.required_revision.relation).toBe("successor");
    expect(queued.revision_transition?.successor_revision.generation).toBe(2);

    expect(() => runSummaryV1Schema.parse({ ...CONTRACT_FIXTURE_V1.queuedRunSummary, pinned_revision: CONTRACT_FIXTURE_V1.runSummary.pinned_revision })).toThrow();
    const { required_revision: _required, ...withoutRequired } = CONTRACT_FIXTURE_V1.queuedRunSummary;
    expect(() => runSummaryV1Schema.parse(withoutRequired)).toThrow();
    const { revision_transition: _transition, ...withoutTransition } = CONTRACT_FIXTURE_V1.queuedRunSummary;
    expect(() => runSummaryV1Schema.parse(withoutTransition)).toThrow();
  });

  it("makes RunCreate renderer-owned only by project identity", () => {
    expect(runCreateV1Schema.parse({ project_id: "project-fixture-1" })).toEqual({ project_id: "project-fixture-1" });
    for (const field of ["project_snapshot", "task_snapshot", "workspace_snapshot", "capability_registry_digest", "required_revision"]) {
      expect(() => runCreateV1Schema.parse({ project_id: "project-fixture-1", [field]: "forbidden" })).toThrow();
    }
  });

  it("keeps capabilities lossless, including unavailable support and canonical config JSON", () => {
    const parsed = capabilitiesEnvelopeV1Schema.parse(CONTRACT_FIXTURE_V1.capabilities);
    const method = parsed.capabilities.targets[0]?.methods[0];
    expect(method).toMatchObject({
      exposure: "desktop",
      execution_modes: ["self_deployed"],
      config_schema_json: '{"additionalProperties":false,"properties":{},"type":"object"}',
    });

    const unavailableAxis = {
      state: "unavailable",
      reason_code: "runtime_missing",
      message: "Required runtime is unavailable.",
      missing_requirements: ["gpu"],
    } as const;
    const unavailableSupport = {
      overall: "unavailable",
      execution: unavailableAxis,
      capture: { ...unavailableAxis, state: "supported", reason_code: null, message: "Supported.", missing_requirements: [] },
      harness: { ...unavailableAxis, state: "supported", reason_code: null, message: "Supported.", missing_requirements: [] },
      runtime: unavailableAxis,
    } as const;
    const capability = CONTRACT_FIXTURE_V1.capabilities.capabilities.targets[0];
    expect(
      capabilitiesEnvelopeV1Schema.parse({
        ...CONTRACT_FIXTURE_V1.capabilities,
        capabilities: {
          ...CONTRACT_FIXTURE_V1.capabilities.capabilities,
          targets: [
            {
              ...capability,
              configured_default_support: unavailableSupport,
              effective_default_method_id: null,
              accepted_methods: capability.accepted_methods.map((entry) => ({ ...entry, support: unavailableSupport })),
              methods: capability.methods.map((entry) => ({ ...entry, support: unavailableSupport })),
            },
          ],
        },
      }).capabilities.targets[0]?.configured_default_support.overall,
    ).toBe("unavailable");

    expect(() => capabilitiesEnvelopeV1Schema.parse({ ...CONTRACT_FIXTURE_V1.capabilities, methods: [] })).toThrow();
    expect(() =>
      capabilitiesEnvelopeV1Schema.parse({
        ...CONTRACT_FIXTURE_V1.capabilities,
        capabilities: {
          ...CONTRACT_FIXTURE_V1.capabilities.capabilities,
          targets: [{ ...capability, methods: [{ ...capability.methods[0], config_schema_json: '{"type":"object","additionalProperties":false,"properties":{}}' }] }],
        },
      }),
    ).toThrow(/canonical/i);
  });

  it("separates local operation logs from Core run and service logs", () => {
    expect(localLogEntryV1Schema.parse(CONTRACT_FIXTURE_V1.operationLog).log_id).toBe("local-log-fixture-1");
    expect(logEntryV1Schema.parse(CONTRACT_FIXTURE_V1.log).id).toBe("log-fixture-1");
    expect(() => logEntryV1Schema.parse(CONTRACT_FIXTURE_V1.operationLog)).toThrow();
    expect(() => localLogEntryV1Schema.parse(CONTRACT_FIXTURE_V1.log)).toThrow();
  });

  it("rejects unknown fields from Core-owned response DTOs", () => {
    expect(() => runV1Schema.parse({ ...CONTRACT_FIXTURE_V1.run, pid: 4242 })).toThrow();
    expect(() => serviceV1Schema.parse({ ...CONTRACT_FIXTURE_V1.service, stop_supported: true })).toThrow();
    expect(() => diagnosticReportV1Schema.parse({ ...CONTRACT_FIXTURE_V1.diagnostic, local_report: {} })).toThrow();
    expect(() => artifactV1Schema.parse({ ...CONTRACT_FIXTURE_V1.artifacts[0], uri: "file:///secret" })).toThrow();
  });

  it("normalizes declared defaults and distinguishes omitted patch fields from null", () => {
    expect(profileCreateV1Schema.parse({ name: "Lab GPU", host: "gpu.example.org", user: "researcher" })).toEqual({
      name: "Lab GPU",
      host: "gpu.example.org",
      port: 22,
      user: "researcher",
      authentication_kind: "ssh_agent",
      proxy: { http_url: null, https_url: null, no_proxy: [] },
    });
    expect(executionSettingsV1Schema.parse({ mode: "self-deployed", hf_model: "open-models/model-1" })).toEqual({
      mode: "self-deployed",
      capture_mode: "transcript",
      token_level_metrics_available: false,
      codex_model: null,
      hf_model: "open-models/model-1",
    });
    expect(profilePatchV1Schema.parse({ name: "Renamed" })).toEqual({ name: "Renamed" });
    expect(() => profilePatchV1Schema.parse({ host: null })).toThrow();
    expect(() => projectPatchV1Schema.parse({ execution: null })).toThrow();
  });

  it("preserves unknown method config while enforcing JavaScript-safe integers", () => {
    const config = { password: "algorithm-owned-value", command: { strategy: "reflect" }, future_plugin_field: [1, true, null] };
    const project = projectV1Schema.parse({
      ...CONTRACT_FIXTURE_V1.project,
      evolution: {
        targets: {
          ...CONTRACT_FIXTURE_V1.project.evolution.targets,
          text_memory: { ...CONTRACT_FIXTURE_V1.project.evolution.targets.text_memory, config },
        },
      },
    });
    expect(project.evolution.targets.text_memory?.config).toEqual(config);
    expect(() => projectCreateV1Schema.parse({ ...project, evolution: { targets: { text_memory: { enabled: true, method: "reference_text_memory", config: { unsafe: Number.MAX_SAFE_INTEGER + 1 } } } } })).toThrow();
  });

  it("parses closed pages and keeps run list items as summaries", () => {
    expect(profilePageV1Schema.parse(PROFILE_PAGE_FIXTURE_V1).items[0]?.profile_id).toBe("profile-fixture-1");
    expect(runPageV1Schema.parse(RUN_PAGE_FIXTURE_V1).items[0]?.id).toBe("run-fixture-1");
    expect(() => runPageV1Schema.parse({ ...RUN_PAGE_FIXTURE_V1, items: [CONTRACT_FIXTURE_V1.run] })).toThrow();
    expect(() => profilePageV1Schema.parse({ ...PROFILE_PAGE_FIXTURE_V1, total: 1 })).toThrow();
  });

  it("accepts only invalidation-style local SSE events", () => {
    const event = eventEnvelopeV1Schema.parse(EVENT_FIXTURE_V1);
    expect(event.event_name).toBe("desktop.v1.resource.changed");
    expect(event.data.kind).toBe("resource_changed");
    expect(() => eventEnvelopeV1Schema.parse({ ...EVENT_FIXTURE_V1, data: { ...EVENT_FIXTURE_V1.data, change_id: undefined } })).toThrow();
    expect(() => eventEnvelopeV1Schema.parse({ ...EVENT_FIXTURE_V1, data: { ...EVENT_FIXTURE_V1.data, resource_etag: null, content_sha256: null } })).toThrow();
    expect(() => eventEnvelopeV1Schema.parse({ ...EVENT_FIXTURE_V1, data: { ...EVENT_FIXTURE_V1.data, authority: "desktop" } })).toThrow();
    expect(() =>
      eventEnvelopeV1Schema.parse({
        ...EVENT_FIXTURE_V1,
        data: {
          ...EVENT_FIXTURE_V1.data,
          authority: "core",
          resource: { resource_type: "project", resource_id: "project-fixture-1" },
        },
      }),
    ).toThrow();
    expect(
      eventEnvelopeV1Schema.parse({
        ...EVENT_FIXTURE_V1,
        data: {
          ...EVENT_FIXTURE_V1.data,
          authority: "desktop",
          resource: { resource_type: "project", resource_id: "project-fixture-1" },
        },
      }).data.kind,
    ).toBe("resource_changed");
    expect(() =>
      eventEnvelopeV1Schema.parse({
        ...EVENT_FIXTURE_V1,
        event_name: "desktop.v1.run.timeline",
        data: { kind: "run_timeline", run_id: "run-fixture-1", entry: CONTRACT_FIXTURE_V1.timeline },
      }),
    ).toThrow();
  });
});
