import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { OpenEvoDesktop } from "./OpenEvoDesktop";

vi.mock("../api/openevo", () => ({
  fetchOpenEvoDesktopShellModel: vi.fn(() =>
    Promise.reject(new Error("sidecar unavailable")),
  ),
}));

describe("OpenEvoDesktop", () => {
  it("renders fixture state when the sidecar fetch is unavailable", () => {
    const html = renderToString(<OpenEvoDesktop />);

    expect(html).toContain("Protein Folding Literature Sprint");
    expect(html).toContain("codex_subscription_transcript");
    expect(html).toContain("Remote ready");
  });
});
