import { describe, expect, it } from "vitest";
import criticalFixture from "../../../sidecar/contracts/v1/fixtures/contract-critical.json";
import { CONTRACT_FIXTURE_V1, EVENT_FIXTURE_V1, PROFILE_PAGE_FIXTURE_V1, RUN_PAGE_FIXTURE_V1 } from "./fixtures";
import {
  apiErrorV1Schema,
  artifactContentV1Schema,
  artifactDiffV1Schema,
  artifactV1Schema,
  desktopBootstrapContextV1Schema,
  desktopStateV1Schema,
  diagnosticReportV1Schema,
  eventEnvelopeV1Schema,
  executionSettingsV1Schema,
  healthV1Schema,
  localOperationV1Schema,
  logEntryV1Schema,
  profilePageV1Schema,
  profileCreateV1Schema,
  profilePatchV1Schema,
  projectCapabilitiesV1Schema,
  projectCreateV1Schema,
  projectPatchV1Schema,
  projectValidateRequestV1Schema,
  projectV1Schema,
  projectValidationV1Schema,
  remoteProfileV1Schema,
  runCreateV1Schema,
  runContextV1Schema,
  runPageV1Schema,
  runV1Schema,
  serviceV1Schema,
  timelineEntryV1Schema,
  versionInfoV1Schema,
} from "./schemas";

describe("Desktop Local API v1 schemas", () => {
  it("parses the deterministic fixture across every response family", () => {
    expect(versionInfoV1Schema.parse(CONTRACT_FIXTURE_V1.version).preferred_major).toBe(1);
    expect(desktopBootstrapContextV1Schema.parse(CONTRACT_FIXTURE_V1.bootstrap).endpoint).toContain("127.0.0.1");
    expect(healthV1Schema.parse(CONTRACT_FIXTURE_V1.health).status).toBe("ok");
    expect(apiErrorV1Schema.parse(CONTRACT_FIXTURE_V1.error).code).toBe("project_not_ready");
    expect(desktopStateV1Schema.parse(CONTRACT_FIXTURE_V1.state).contract.compatible).toBe(true);
    expect(remoteProfileV1Schema.parse(CONTRACT_FIXTURE_V1.profile).authentication_kind).toBe("native_private_key");
    expect(projectV1Schema.parse(CONTRACT_FIXTURE_V1.project).execution.mode).toBe("self-deployed");
    expect(localOperationV1Schema.parse(CONTRACT_FIXTURE_V1.operation).state).toBe("running");
    expect(runV1Schema.parse(CONTRACT_FIXTURE_V1.run).pinned_revision.generation).toBe(1);
    expect(timelineEntryV1Schema.parse(CONTRACT_FIXTURE_V1.timeline).stage).toBe("agent");
    expect(logEntryV1Schema.parse(CONTRACT_FIXTURE_V1.log).source).toBe("run");
    expect(runContextV1Schema.parse(CONTRACT_FIXTURE_V1.context).contributions).toHaveLength(1);
    expect(CONTRACT_FIXTURE_V1.artifacts.map((artifact) => artifactV1Schema.parse(artifact).artifact_type)).toEqual([
      "text_memory",
      "skill_bundle",
      "agent_system",
      "parametric_memory",
    ]);
    expect(artifactContentV1Schema.parse(CONTRACT_FIXTURE_V1.artifactContent).documents).toHaveLength(1);
    expect(artifactDiffV1Schema.parse(CONTRACT_FIXTURE_V1.artifactDiff).hunks).toHaveLength(1);
    expect(serviceV1Schema.parse(CONTRACT_FIXTURE_V1.service).state).toBe("healthy");
    expect(diagnosticReportV1Schema.parse(CONTRACT_FIXTURE_V1.diagnostic).status).toBe("healthy");
    expect(projectCapabilitiesV1Schema.parse(CONTRACT_FIXTURE_V1.capabilities).registry_digest).toHaveLength(64);
    expect(projectValidationV1Schema.parse(CONTRACT_FIXTURE_V1.validation).valid).toBe(true);
  });

  it("parses the Python-owned cross-language critical fixture", () => {
    expect(healthV1Schema.parse(criticalFixture.health).protocol).toBe("openevo-native-sidecar-v1");
    expect(desktopStateV1Schema.parse(criticalFixture.state).core.state).toBe("online");
    expect(runCreateV1Schema.parse(criticalFixture.run_create).required_revision.state).toBe("queued");
    expect(profileCreateV1Schema.parse(criticalFixture.profile_create.wire)).toEqual(criticalFixture.profile_create.normalized);
    expect(executionSettingsV1Schema.parse(criticalFixture.execution.wire)).toEqual(criticalFixture.execution.normalized);
    expect(profilePatchV1Schema.parse(criticalFixture.profile_patch.wire)).toEqual(criticalFixture.profile_patch.normalized);
    expect(localOperationV1Schema.parse(criticalFixture.operation_defaults.wire)).toEqual(criticalFixture.operation_defaults.normalized);
    expect(serviceV1Schema.parse(criticalFixture.service_defaults.wire)).toEqual(criticalFixture.service_defaults.normalized);
    expect(artifactContentV1Schema.parse(criticalFixture.artifact_content).total_documents).toBe(1);
    expect(artifactDiffV1Schema.parse(criticalFixture.artifact_diff).hunks[0]?.lines[0]?.text).toBe("");
  });

  it("rejects unknown response fields instead of stripping them", () => {
    expect(() =>
      remoteProfileV1Schema.parse({
        ...CONTRACT_FIXTURE_V1.profile,
        backend_url: "https://core.example.test",
      }),
    ).toThrow();
    expect(() =>
      runV1Schema.parse({
        ...CONTRACT_FIXTURE_V1.run,
        pid: 4242,
      }),
    ).toThrow();
  });

  it("preserves unknown method config fields without field-name heuristics", () => {
    const config = {
      password: "algorithm-owned-value",
      command: { strategy: "reflect" },
      future_plugin_field: [1, true, null],
    };
    const project = projectV1Schema.parse({
      ...CONTRACT_FIXTURE_V1.project,
      evolution: {
        targets: {
          ...CONTRACT_FIXTURE_V1.project.evolution.targets,
          text_memory: {
            ...CONTRACT_FIXTURE_V1.project.evolution.targets.text_memory,
            config,
          },
        },
      },
    });
    expect(project.evolution.targets.text_memory?.config).toEqual(config);
  });

  it("uses a dedicated reachable required-revision wire schema", () => {
    const wire = {
      project_id: "project-1",
      project_snapshot: { snapshot_id: "project-snapshot-1", digest: "a".repeat(64) },
      task_snapshot: { snapshot_id: "task-snapshot-1", digest: "b".repeat(64) },
      workspace_snapshot: { snapshot_id: "workspace-snapshot-1", digest: "c".repeat(64) },
      capability_registry_digest: "d".repeat(64),
      required_revision: {
        revision_id: "revision-2",
        generation: 2,
        manifest_digest: "e".repeat(64),
        state: "active",
      },
    } as const;

    for (const state of ["active", "queued", "preparing"] as const) {
      expect(runCreateV1Schema.parse({ ...wire, required_revision: { ...wire.required_revision, state } }).required_revision.state).toBe(state);
    }
    for (const state of ["failed", "cancelled"] as const) {
      expect(() => runCreateV1Schema.parse({ ...wire, required_revision: { ...wire.required_revision, state } })).toThrow();
    }
  });

  it("normalizes declared defaults and distinguishes omitted patch fields from null", () => {
    expect(
      profileCreateV1Schema.parse({ name: "Lab GPU", host: "gpu.example.org", user: "researcher" }),
    ).toEqual({
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
    expect(profilePatchV1Schema.parse({ proxy: { https_url: null } })).toEqual({
      proxy: { http_url: null, https_url: null, no_proxy: [] },
    });
    expect(() => profilePatchV1Schema.parse({ host: null })).toThrow();
    expect(() => profilePatchV1Schema.parse({ proxy: null })).toThrow();
    expect(() => projectPatchV1Schema.parse({ execution: null })).toThrow();
  });

  it("uses only the closed evolution.targets wrapper for projects and validation", () => {
    const target = {
      enabled: true,
      method: "reference_text_memory",
      config: { password: "algorithm-owned-value", future_field: 1 },
    } as const;
    const project = {
      name: "Protein Design",
      profile_id: "profile-1",
      task: { title: "Design", objective: "Produce a candidate." },
      source: { kind: "scratch", display_name: "New workspace" },
      execution: { mode: "self-deployed", hf_model: "open-models/model-1" },
      evolution: { targets: { text_memory: target } },
    } as const;
    expect(projectCreateV1Schema.parse(project).evolution.targets.text_memory?.config).toEqual(target.config);
    expect(
      projectValidateRequestV1Schema.parse({
        project_etag: `"${"a".repeat(64)}"`,
        capability_registry_digest: "b".repeat(64),
        execution: project.execution,
        evolution: project.evolution,
      }).evolution.targets.text_memory?.method,
    ).toBe("reference_text_memory");
    expect(() => projectCreateV1Schema.parse({ ...project, evolution: { text_memory: target } })).toThrow();
  });

  it("uses a bounded trimmed hf_model only for self-deployed execution", () => {
    expect(executionSettingsV1Schema.parse({ mode: "self-deployed", hf_model: "open-models/model-1" }).hf_model).toBe(
      "open-models/model-1",
    );
    for (const hf_model of ["", " open-models/model-1", "open-models/model-1\n"]) {
      expect(() => executionSettingsV1Schema.parse({ mode: "self-deployed", hf_model })).toThrow();
    }
    expect(() => executionSettingsV1Schema.parse({ mode: "self-deployed", managed_model_id: "legacy-model" })).toThrow();
    expect(() =>
      executionSettingsV1Schema.parse({ mode: "codex_subscription_transcript", codex_model: "gpt-5", hf_model: "open-models/model-1" }),
    ).toThrow();
  });

  it("matches Python network host and remote user validation", () => {
    for (const [host, user] of [
      ["gpu.example.org", "researcher"],
      ["gpu.example.org.", "researcher.name"],
      ["192.0.2.10", "researcher-1"],
      ["2001:db8::10", "researcher_1"],
      ["127.1", "researcher"],
    ] as const) {
      expect(profileCreateV1Schema.parse({ name: "Lab GPU", host, user }).host).toBe(host);
    }

    for (const [field, value] of [
      ["host", "gpu_name.example.org"],
      ["host", "-gpu.example.org"],
      ["host", "https://gpu.example.org"],
      ["user", "researcher name"],
      ["user", "researcher@lab"],
      ["user", "../researcher"],
    ] as const) {
      expect(() =>
        profileCreateV1Schema.parse({ name: "Lab GPU", host: "gpu.example.org", user: "researcher", [field]: value }),
      ).toThrow();
    }
  });

  it("accepts empty context lines in a bounded artifact diff", () => {
    expect(
      artifactDiffV1Schema.parse({
        schema_version: "1",
        artifact_id: "artifact-1",
        base_artifact_id: null,
        hunks: [
          {
            hunk_id: "hunk-1",
            heading: "Memory",
            lines: [{ kind: "context", old_line: 1, new_line: 1, text: "" }],
          },
        ],
        truncated: false,
      }).hunks[0]?.lines[0]?.text,
    ).toBe("");
  });

  it("accepts only the exact self-deployed execution mode spelling", () => {
    expect(() =>
      projectV1Schema.parse({
        ...CONTRACT_FIXTURE_V1.project,
        execution: { ...CONTRACT_FIXTURE_V1.project.execution, mode: "self deployed" },
      }),
    ).toThrow();
  });

  it("rejects malformed timestamps, digests, and proxy credentials", () => {
    expect(() => remoteProfileV1Schema.parse({ ...CONTRACT_FIXTURE_V1.profile, updated_at: "2026-07-14" })).toThrow();
    expect(() =>
      runV1Schema.parse({ ...CONTRACT_FIXTURE_V1.run, capability_registry_digest: "ABC" }),
    ).toThrow();
    expect(() =>
      remoteProfileV1Schema.parse({
        ...CONTRACT_FIXTURE_V1.profile,
        proxy: { ...CONTRACT_FIXTURE_V1.profile.proxy, http_url: "http://user:password@proxy.example.test" },
      }),
    ).toThrow(/user information/i);
  });

  it("parses closed pages and rejects inconsistent cursors", () => {
    expect(profilePageV1Schema.parse(PROFILE_PAGE_FIXTURE_V1).items[0].profile_id).toBe("profile-fixture-1");
    expect(runPageV1Schema.parse(RUN_PAGE_FIXTURE_V1).has_more).toBe(false);
    expect(() => runPageV1Schema.parse({ ...RUN_PAGE_FIXTURE_V1, has_more: true })).toThrow(/cursor/i);
    expect(() => runPageV1Schema.parse({ ...RUN_PAGE_FIXTURE_V1, total: 1 })).toThrow();
  });

  it("parses closed event envelopes", () => {
    const event = eventEnvelopeV1Schema.parse(EVENT_FIXTURE_V1);
    expect(event.event_name).toBe("desktop.v1.run.changed");
    expect(event.data.kind).toBe("run_changed");
    expect(() => eventEnvelopeV1Schema.parse({ ...EVENT_FIXTURE_V1, remote_path: "/srv/openevo" })).toThrow();
  });
});
