// @vitest-environment happy-dom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppShell, ReleaseDesktopProductShell } from "./App";
import type { NativeStartupStatusV2 } from "./product/releaseProvider";

const invokeMock = vi.hoisted(() => vi.fn());

vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function renderShell(path: string, desktopOnly: boolean) {
  const queryClient = new QueryClient();
  return renderToString(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <AppShell desktopOnly={desktopOnly} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("AppShell", () => {
  it("renders the shared observability shell by default", () => {
    const html = renderShell("/", false);

    expect(html).toContain("OpenEvo Observability");
    expect(html).toContain('href="/tasks"');
    expect(html).toContain(">Dashboard<");
  });

  it("renders a fail-closed Desktop startup state without sample projects", () => {
    const html = renderShell("/openevo", true);

    expect(html).toContain("Loading your workspace");
    expect(html).toContain("No authoritative workspace loaded");
    expect(html).not.toContain("Enzyme Kinetics Model Review");
    expect(html).not.toContain("Protein Stability Evidence Review");
    expect(html).not.toContain("OpenEvo Observability");
  });
});

describe("ReleaseDesktopProductShell", () => {
  let root: Root | null = null;

  beforeEach(() => {
    document.body.innerHTML = "";
    invokeMock.mockReset();
    invokeMock.mockResolvedValue(undefined);
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
  });

  afterEach(async () => {
    if (root) {
      await act(async () => root?.unmount());
      root = null;
    }
    document.body.innerHTML = "";
    delete (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
  });

  it("shows no fabricated project while native startup is pending", async () => {
    const pendingProvider = new Promise<never>(() => {});
    root = await renderReleaseShell(() => pendingProvider);

    expect(document.body.textContent).toContain("Starting OpenEvo Desktop");
    expect(document.body.textContent).toContain("Waiting for authoritative Desktop state");
    expect(document.body.textContent).toContain(
      "No project, Session, artifact, or workspace data is shown",
    );
    expect(document.body.textContent).not.toContain("Demo data");
    expect(document.body.textContent).not.toContain("[Demo]");
  });

  it("shows native startup checkpoints without manufacturing domain data", async () => {
    const pendingProvider = new Promise<never>(() => {});
    const status: NativeStartupStatusV2 = {
      schema_version: "2",
      startup_epoch: 3,
      status: "running",
      phase: "waiting_for_local_api",
      phase_index: 3,
      phase_total: 6,
      elapsed_milliseconds: 16_000,
      cancellable: true,
      failure: null,
    };
    const getStartupStatus = vi.fn(async () => status);

    root = await renderReleaseShell(
      () => pendingProvider,
      vi.fn(async () => {}),
      vi.fn(async () => {}),
      vi.fn(),
      getStartupStatus,
    );
    await vi.waitFor(() => expect(getStartupStatus).toHaveBeenCalled());

    expect(document.body.textContent).toContain("Waiting for the Desktop Local API");
    expect(document.body.textContent).toContain("Checkpoint 4 of 6");
    expect(document.body.textContent).toContain("Elapsed 16s");
    expect(document.body.textContent).not.toContain("Enzyme Kinetics Model Review");
  });

  it("reports a bounded startup failure instead of falling back to sample state", async () => {
    const factory = vi.fn(async () => {
      throw new Error("secret bootstrap detail");
    });
    const reportStage = vi.fn();

    root = await renderReleaseShell(
      factory,
      vi.fn(async () => {}),
      vi.fn(async () => {}),
      reportStage,
    );
    await vi.waitFor(() =>
      expect(document.body.textContent).toContain("OpenEvo Desktop could not start"),
    );

    expect(reportStage).toHaveBeenCalledWith("provider_create_failed");
    expect(document.body.textContent).not.toContain("secret bootstrap detail");
    expect(document.body.textContent).toContain("Add remote workspace");
    expect(document.body.textContent).not.toContain("Demo data");
  });
});

async function renderReleaseShell(
  factory: React.ComponentProps<typeof ReleaseDesktopProductShell>["createProvider"],
  stop = vi.fn(async () => {}),
  reportReady = vi.fn(async () => {}),
  reportStage = vi.fn(),
  getStartupStatus = vi.fn(async (): Promise<NativeStartupStatusV2> => ({
    schema_version: "2",
    startup_epoch: 0,
    status: "idle",
    phase: "validating_bundle",
    phase_index: 0,
    phase_total: 6,
    elapsed_milliseconds: 0,
    cancellable: false,
    failure: null,
  })),
): Promise<Root> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const rendered = createRoot(container);
  await act(async () => {
    rendered.render(
      <ReleaseDesktopProductShell
        createProvider={factory}
        stopProvider={stop}
        reportReady={reportReady}
        reportStage={reportStage}
        getStartupStatus={getStartupStatus}
      />,
    );
    await Promise.resolve();
  });
  return rendered;
}
