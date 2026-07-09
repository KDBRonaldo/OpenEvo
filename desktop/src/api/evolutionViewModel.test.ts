import { describe, expect, it } from "vitest";
import {
  artifactPreview,
  nextActionForError,
  timelineView,
} from "./evolutionViewModel";

describe("evolution view model", () => {
  it("renders run timeline phases and lineage without inventing algorithm fields", () => {
    const view = timelineView([
      {
        id: "event-1",
        phase: "evolution",
        title: "Memory updated",
        message: "A memory artifact was promoted.",
        artifact_ids: ["artifact-1"],
      },
    ]);

    expect(view[0].label).toBe("Memory updated");
    expect(view[0].phase).toBe("evolution");
    expect(view[0].artifactIds).toEqual(["artifact-1"]);
  });

  it("renders artifact preview and diff metadata", () => {
    const preview = artifactPreview(
      {
        id: "artifact-1",
        artifact_type: "agent_system",
        content: "Use stricter checks.",
        metadata: { lineage: { round: 2 }, target_path: "AGENTS.md" },
      },
      {
        id: "artifact-1",
        before: "Use checks.",
        after: "Use stricter checks.",
        format: "unified_text",
      },
    );

    expect(preview.kind).toBe("agent_system");
    expect(preview.targetPath).toBe("AGENTS.md");
    expect(preview.lineage).toEqual({ round: 2 });
    expect(preview.diff.after).toContain("stricter");
  });

  it("renders backend error next actions", () => {
    expect(
      nextActionForError({
        code: "docker_permission_denied",
        message: "Docker permission denied.",
        severity: "blocking",
        category: "environment",
        retryable: false,
        repair_action: "user_action_required",
        details: {},
      }),
    ).toBe("User action required");
  });
});
