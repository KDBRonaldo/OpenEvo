// @vitest-environment happy-dom

import { describe, expect, it, vi } from "vitest";

vi.mock("react-dom/client", () => ({
  default: {
    createRoot: () => ({ render: () => undefined }),
  },
}));

import { createProductPreviewProvider } from "./preview";

describe("product preview", () => {
  it("uses the same current-contract provider as the formal Desktop renderer", async () => {
    const provider = createProductPreviewProvider();
    const refreshed = await provider.refresh();

    if (refreshed.status !== "fresh") throw new Error("Preview fixture refresh was not fresh.");

    expect(refreshed.snapshot.state.schema_version).toBe("2");
    expect(refreshed.snapshot.projects).toHaveLength(1);
    expect(refreshed.snapshot.tasks).toHaveLength(1);
    expect(refreshed.snapshot.fixturePresentation?.tasks[refreshed.snapshot.tasks[0]!.task_id]).toBeDefined();
  });
});
