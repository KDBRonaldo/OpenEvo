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
  healthV1Schema,
  localOperationV1Schema,
  logEntryV1Schema,
  profilePageV1Schema,
  projectCapabilitiesV1Schema,
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

  it("rejects secret-like fields inside explicitly dynamic config slots", () => {
    expect(() =>
      projectV1Schema.parse({
        ...CONTRACT_FIXTURE_V1.project,
        evolution: {
          ...CONTRACT_FIXTURE_V1.project.evolution,
          text_memory: {
            ...CONTRACT_FIXTURE_V1.project.evolution.text_memory,
            config: { password: "must-not-enter-react" },
          },
        },
      }),
    ).toThrow(/sensitive or implementation-detail/i);
  });

  it("accepts a follow-up run while its required revision is still queued", () => {
    const run = runCreateV1Schema.parse({
      project_id: "project-1",
      project_snapshot: { snapshot_id: "project-snapshot-1", digest: "a".repeat(64) },
      task_snapshot: { snapshot_id: "task-snapshot-1", digest: "b".repeat(64) },
      workspace_snapshot: { snapshot_id: "workspace-snapshot-1", digest: "c".repeat(64) },
      capability_registry_digest: "d".repeat(64),
      required_revision: {
        revision_id: "revision-2",
        generation: 2,
        manifest_digest: "e".repeat(64),
        state: "queued",
      },
    });
    expect(run.required_revision.state).toBe("queued");
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
