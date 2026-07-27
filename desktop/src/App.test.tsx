// @vitest-environment happy-dom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DesktopApiError } from "./api/v1/client";
import { AppShell, ReleaseDesktopProductShell } from "./App";
import { createFixtureDesktopProductProvider, type FixtureDesktopProductProvider } from "./product/fixtureProvider";
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
  it("renders the shared OpenEvo observability shell by default", () => {
    const html = renderShell("/", false);

    expect(html).toContain("OpenEvo Observability");
    expect(html).toContain('href="/tasks"');
    expect(html).toContain(">Dashboard<");
  });

  it("renders OpenEvo Desktop without shared dashboard navigation in desktop-only mode", () => {
    const html = renderShell("/openevo", true);

    expect(html).toContain("Loading your workspace");
    expect(html).toContain("Enzyme Kinetics Model Review");
    expect(html).toContain("Protein Stability Evidence Review");
    expect(html).not.toContain("OpenEvo Observability");
    expect(html).not.toContain('href="/tasks"');
    expect(html).not.toContain(">Dashboard<");
  });

  it("renders OpenEvo Desktop at the root path in desktop-only mode", () => {
    const html = renderShell("/", true);

    expect(html).toContain("Loading your workspace");
    expect(html).not.toContain("Not found");
  });
});

describe("ReleaseDesktopProductShell", () => {
  let root: Root | null = null;
  let provider: FixtureDesktopProductProvider | null = null;

  beforeEach(() => {
    document.body.innerHTML = "";
    invokeMock.mockReset();
    invokeMock.mockResolvedValue(undefined);
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
  });

  afterEach(async () => {
    provider?.dispose();
    provider = null;
    if (root) {
      await act(async () => root?.unmount());
      root = null;
    }
    vi.useRealTimers();
    document.body.innerHTML = "";
    delete (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
  });

  it("exposes both renderer-owned samples while native sidecar startup is pending", async () => {
    const pending = deferred<FixtureDesktopProductProvider>();
    provider = createFixtureDesktopProductProvider({ newUser: true });
    root = await renderReleaseShell(() => pending.promise, vi.fn(async () => {}), vi.fn(async () => {}));

    expect(document.querySelector('[data-testid="release-startup-sample"]')).not.toBeNull();
    expect(document.body.textContent).toContain("Starting OpenEvo Desktop");
    const switcher = document.querySelector<HTMLSelectElement>("#startup-sample-project");
    if (!switcher) throw new Error("Pending startup sample switcher was not found.");
    expect(switcher.disabled).toBe(false);
    expect(Array.from(switcher.options)).toHaveLength(2);
    const proteinOption = Array.from(switcher.options).find((option) =>
      option.textContent?.includes("Protein Stability Evidence Review")
    );
    if (!proteinOption) throw new Error("Pending startup protein sample was not found.");
    await act(async () => {
      switcher.value = proteinOption.value;
      switcher.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(document.body.textContent).toContain("Demo data");
    expect([...document.querySelectorAll("button")].some((item) =>
      item.textContent?.includes("Retry")
    )).toBe(false);
    expect(button("Add remote workspace").disabled).toBe(false);

    await act(async () => {
      pending.resolve(provider!);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
  });

  it("shows native startup checkpoints, elapsed time, and safe cancellation", async () => {
    const pending = deferred<FixtureDesktopProductProvider>();
    const stop = vi.fn(async () => {});
    const getStartupStatus = vi.fn(async (): Promise<NativeStartupStatusV2> => ({
      schema_version: "2",
      startup_epoch: 3,
      status: "running",
      phase: "waiting_for_local_api",
      phase_index: 3,
      phase_total: 6,
      elapsed_milliseconds: 16_000,
      cancellable: true,
      failure: null,
    }));

    root = await renderReleaseShell(
      () => pending.promise,
      stop,
      vi.fn(async () => {}),
      vi.fn(),
      getStartupStatus,
    );
    await vi.waitFor(() => expect(getStartupStatus).toHaveBeenCalled());

    expect(document.body.textContent).toContain("Waiting for the Desktop Local API");
    expect(document.body.textContent).toContain("Checkpoint 4 of 6");
    expect(document.body.textContent).toContain("Elapsed 16s");
    expect(document.body.textContent).toContain("Native startup output remains available through Diagnostics.");
    await act(async () => button("Cancel operation").click());
    expect(stop.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("keeps both renderer-owned samples interactive while the first provider snapshot is pending", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    const initial = await provider.refresh();
    const pendingRefresh = deferred<typeof initial>();
    const refresh = vi.spyOn(provider, "refresh").mockReturnValueOnce(pendingRefresh.promise);
    const factory = vi.fn(async () => provider!);
    const reportReady = vi.fn(async () => {});

    root = await renderReleaseShell(factory, vi.fn(async () => {}), reportReady);

    expect(factory).toHaveBeenCalledTimes(1);
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(reportReady).not.toHaveBeenCalled();
    expect(document.querySelector('[data-testid="initial-sync-pending"]')).not.toBeNull();
    expect(document.body.textContent).toContain("Loading your workspace");
    expect(document.body.textContent).toContain("Enzyme Kinetics Model Review");
    const switcher = document.querySelector<HTMLSelectElement>("#project-switcher");
    if (!switcher) throw new Error("Pending snapshot sample switcher was not found.");
    expect(switcher.disabled).toBe(false);
    expect(Array.from(switcher.options).filter((option) =>
      option.textContent?.includes("[Demo]")
    )).toHaveLength(2);
    const proteinOption = Array.from(switcher.options).find((option) =>
      option.textContent?.includes("Protein Stability Evidence Review")
    );
    if (!proteinOption) throw new Error("Pending snapshot protein sample was not found.");
    await act(async () => {
      switcher.value = proteinOption.value;
      switcher.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(document.body.textContent).toContain("Protein Stability Evidence Review");
    expect(document.body.textContent).toContain("Demo data");

    await act(async () => {
      button("Evolution").click();
    });
    expect(document.querySelector('[data-testid="sample-evolution-workspace"]')).not.toBeNull();
    expect(document.body.textContent).toContain("How OpenEvo improved this project");

    await act(async () => {
      pendingRefresh.resolve(initial);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(document.querySelector('[data-testid="initial-sync-pending"]')).toBeNull();
    expect(document.body.textContent).toContain("Cross-session changes");
    expect(reportReady).toHaveBeenCalledTimes(1);
  });

  it("restarts the sidecar after the first authoritative snapshot fails", async () => {
    const failedProvider = createFixtureDesktopProductProvider({ startOnline: true });
    failedProvider.failNextRefresh();
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    const factory = vi.fn()
      .mockResolvedValueOnce(failedProvider)
      .mockResolvedValueOnce(provider);
    const stop = vi.fn(async () => {});
    const reportStage = vi.fn();

    root = await renderReleaseShell(factory, stop, vi.fn(async () => {}), reportStage);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(document.querySelector('[data-testid="release-startup-sample"]')).not.toBeNull();
    expect(document.body.textContent).toContain("OpenEvo Desktop could not start");
    expect(reportStage).toHaveBeenCalledWith("initial_snapshot_failed");

    await act(async () => {
      button("Add remote workspace").click();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(factory).toHaveBeenCalledTimes(2);
    expect(stop.mock.calls.length).toBeGreaterThanOrEqual(3);
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    expect(document.body.textContent).toContain("Server connection");
    failedProvider.dispose();
  });

  it("restarts native bootstrap after a failed release startup", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    const lifecycle: string[] = [];
    const factory = vi.fn()
      .mockImplementationOnce(async () => {
        lifecycle.push("start-1");
        throw new Error("secret bootstrap detail");
      })
      .mockImplementationOnce(async () => {
        lifecycle.push("start-2");
        return provider!;
      });
    const stop = vi.fn(async () => { lifecycle.push("stop"); });
    const reportStage = vi.fn(async (stage: string) => { lifecycle.push(stage); });
    const reportReady = vi.fn().mockResolvedValue(undefined);

    root = await renderReleaseShell(
      factory,
      stop,
      reportReady,
      reportStage,
    );
    expect(document.body.textContent).toContain("OpenEvo Desktop could not start");
    expect(document.querySelector('[data-testid="sample-research-workspace"]')).not.toBeNull();
    expect(document.body.textContent).not.toContain("secret bootstrap detail");
    expect(factory).toHaveBeenCalledTimes(1);
    expect(reportReady).not.toHaveBeenCalled();

    const addRemoteWorkspace = button("Add remote workspace");
    await act(async () => {
      addRemoteWorkspace.click();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(factory).toHaveBeenCalledTimes(2);
    expect(reportStage).toHaveBeenCalledWith("provider_create_failed");
    expect(reportStage).toHaveBeenCalledWith("product_committed");
    expect(document.body.textContent).toContain("Research brief");
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    expect(document.body.textContent).toContain("Server connection");
    const firstStart = lifecycle.indexOf("start-1");
    const secondStart = lifecycle.indexOf("start-2");
    expect(lifecycle.slice(firstStart + 1, secondStart)).toContain("stop");
  });

  it("shows renderer-owned text for a typed startup failure and keeps remote workspace intent", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    const retry = deferred<FixtureDesktopProductProvider>();
    const factory = vi.fn()
      .mockRejectedValueOnce(startupApiError(
        "The local Desktop service rejected the startup request.",
        "Retry after checking the local connection.",
      ))
      .mockReturnValueOnce(retry.promise);

    root = await renderReleaseShell(factory, vi.fn(async () => {}), vi.fn(async () => {}));

    expect(document.body.textContent).toContain("The local OpenEvo Desktop service reported a startup error.");
    expect(document.body.textContent).not.toContain("The local Desktop service rejected the startup request.");
    expect(document.body.textContent).not.toContain("Retry after checking the local connection.");

    await act(async () => {
      button("Add remote workspace").click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(factory).toHaveBeenCalledTimes(2);
    expect(document.body.textContent).toContain("Retrying OpenEvo Desktop");
    expect(document.body.textContent).toContain("Your remote workspace will open when OpenEvo Desktop is ready.");
    expect(button("Retrying").disabled).toBe(true);
    button("Retrying").click();
    expect(factory).toHaveBeenCalledTimes(2);

    await act(async () => {
      retry.resolve(provider!);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    expect(document.body.textContent).toContain("Server connection");
  });

  it("acknowledges a remote workspace request while startup is still loading", async () => {
    const pending = deferred<FixtureDesktopProductProvider>();
    root = await renderReleaseShell(() => pending.promise, vi.fn(async () => {}), vi.fn(async () => {}));

    await act(async () => {
      button("Add remote workspace").click();
    });

    expect(document.body.textContent).toContain("Starting OpenEvo Desktop");
    expect(document.body.textContent).toContain("Your remote workspace will open when OpenEvo Desktop is ready.");
  });

  it("shows only closed native startup diagnostics", async () => {
    const nativeFailure = {
      code: "sidecar_exited_during_startup",
      message: "OpenEvo Desktop could not start. Startup diagnostic: python_launcher/provider_store_failed.",
    };
    root = await renderReleaseShell(
      vi.fn(async () => { throw nativeFailure; }),
      vi.fn(async () => {}),
      vi.fn(async () => {}),
    );

    expect(document.body.textContent).toContain(nativeFailure.message);

    await act(async () => {
      root?.unmount();
      root = null;
    });
    root = await renderReleaseShell(
      vi.fn(async () => {
        throw { ...nativeFailure, debug_path: "/Users/alice/private" };
      }),
      vi.fn(async () => {}),
      vi.fn(async () => {}),
    );

    expect(document.body.textContent).not.toContain("/Users/alice/private");
    expect(document.body.textContent).not.toContain(nativeFailure.message);
    expect(document.body.textContent).toContain("The local OpenEvo Desktop service could not be started.");
  });

  it("opens closed native logs after a sidecar startup failure without a provider", async () => {
    invokeMock.mockImplementation(async (command: string) => {
      if (command === "get_desktop_log_tail") {
        return {
          schema_version: "1",
          availability: "available",
          entries: [{
            schema_version: "1",
            sequence: 1,
            occurred_at: "1970-01-01T00:00:00.000Z",
            source: "sidecar",
            level: "error",
            event: "sidecar_exited_before_ready",
            code: "python_launcher_failed",
            exit_code: 255,
            signal: null,
            errno: null,
          }],
          dropped_count: 0,
        };
      }
      if (command === "reveal_desktop_log_directory") return { status: "revealed" };
      if (command === "export_desktop_diagnostics") return { status: "exported" };
      return undefined;
    });

    root = await renderReleaseShell(
      vi.fn(async () => { throw new Error("sidecar failed before provider creation"); }),
      vi.fn(async () => {}),
      vi.fn(async () => {}),
      vi.fn(),
    );

    expect(document.body.textContent).toContain("OpenEvo Desktop could not start");
    expect(document.body.textContent).not.toContain("sidecar failed before provider creation");
    await act(async () => {
      button("Diagnostics").click();
    });
    await act(async () => {
      button("View logs").click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(document.body.textContent).toContain("sidecar_exited_before_ready");
    expect(document.body.textContent).toContain("python_launcher_failed");
    expect(document.body.textContent).toContain("255");

    await act(async () => {
      button("Reveal in Finder").click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(document.body.textContent).toContain("Log folder opened.");
    await act(async () => {
      button("Export diagnostics").click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(invokeMock).toHaveBeenCalledWith("get_desktop_log_tail", { limit: 80 });
    expect(invokeMock).toHaveBeenCalledWith("reveal_desktop_log_directory");
    expect(invokeMock).toHaveBeenCalledWith("export_desktop_diagnostics");
    expect(document.body.textContent).toContain("Diagnostics exported.");
  });

  it("does not expose native log invoke errors", async () => {
    invokeMock.mockImplementation(async (command: string) => {
      if (command === "get_desktop_log_tail") {
        throw new Error("/Users/alice/private-token stderr stack trace");
      }
      return undefined;
    });
    root = await renderReleaseShell(
      vi.fn(async () => { throw new Error("sidecar failed"); }),
      vi.fn(async () => {}),
      vi.fn(async () => {}),
      vi.fn(),
    );

    await act(async () => {
      button("Diagnostics").click();
    });
    await act(async () => {
      button("View logs").click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("Native logs are unavailable.");
    expect(document.body.textContent).not.toContain("/Users/alice/private-token");
    expect(document.body.textContent).not.toContain("stderr stack trace");
  });

  it("does not request diagnostics during a normal startup", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    root = await renderReleaseShell(
      vi.fn(async () => provider!),
      vi.fn(async () => {}),
      vi.fn(async () => {}),
      vi.fn(),
      vi.fn(async (): Promise<NativeStartupStatusV2> => ({
        schema_version: "2",
        startup_epoch: 1,
        status: "succeeded",
        phase: "ready",
        phase_index: 5,
        phase_total: 6,
        elapsed_milliseconds: 25,
        cancellable: false,
        failure: null,
      })),
    );

    expect(document.body.textContent).toContain("Research brief");
    expect(invokeMock).not.toHaveBeenCalled();
  });

  it("keeps the startup fallback renderer-owned and mutation-closed", async () => {
    const factory = vi.fn(async () => {
      throw new Error("native sidecar unavailable");
    });
    const stop = vi.fn(async () => {});
    const reportReady = vi.fn(async () => {});

    root = await renderReleaseShell(factory, stop, reportReady);

    expect(document.querySelector('[data-testid="release-startup-sample"]')).not.toBeNull();
    expect(document.querySelector('[data-testid="sample-research-workspace"]')).not.toBeNull();
    expect(button("Retry").disabled).toBe(false);
    expect(reportReady).not.toHaveBeenCalled();
    expect(document.body.textContent).toContain("Demo");
    expect(document.body.textContent).not.toContain("Renderer sample");
    expect(document.body.textContent).not.toContain("provider");
    expect(button("Add remote workspace").disabled).toBe(false);
    expect(
      [...document.querySelectorAll("button")].some((candidate) =>
        /Create project/i.test(candidate.textContent ?? "")
      ),
    ).toBe(false);

    const sampleSwitcher = document.querySelector<HTMLSelectElement>("#startup-sample-project");
    if (!sampleSwitcher) throw new Error("Startup sample switcher was not found.");
    expect(Array.from(sampleSwitcher.options).filter((option) =>
      option.textContent?.includes("[Demo]")
    )).toHaveLength(2);
    const proteinOption = Array.from(sampleSwitcher.options).find((option) =>
      option.textContent?.includes("Protein Stability Evidence Review")
    );
    if (!proteinOption) throw new Error("Protein stability startup sample was not found.");
    await act(async () => {
      sampleSwitcher.value = proteinOption.value;
      sampleSwitcher.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(document.body.textContent).toContain("Demo data");
    expect(document.body.textContent).toContain("ER-PS-3");

    await act(async () => {
      button("Evolution").click();
    });
    expect(document.querySelector('[data-testid="sample-evolution-workspace"]')).not.toBeNull();
    await act(async () => button("Changes").click());
    expect(document.body.textContent).toContain("ER-PS-2 → ER-PS-3");
    await act(async () => {
      button("System").click();
    });
    expect(document.querySelector('[data-testid="sample-about-workspace"]')).not.toBeNull();
    expect(document.body.textContent).toContain("No remote workspace");
    await act(async () => {
      button("Research").click();
    });
    expect(document.querySelector('[data-testid="sample-research-workspace"]')).not.toBeNull();

    expect(factory).toHaveBeenCalledTimes(1);
    expect(reportReady).not.toHaveBeenCalled();
  });

  it("serializes StrictMode bootstrap through a fresh native lifecycle", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    const lifecycle: string[] = [];
    const factory = vi.fn(async () => {
      lifecycle.push("start");
      return provider!;
    });
    const stop = vi.fn(async () => { lifecycle.push("stop"); });
    let productCommittedWhenReported = false;
    const reportReady = vi.fn(async () => {
      productCommittedWhenReported = document.body.textContent?.includes("Research brief") ?? false;
      lifecycle.push("ready");
    });
    const reportStage = vi.fn(async (stage: string) => { lifecycle.push(stage); });
    const container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(
        <StrictMode>
          <MemoryRouter>
            <ReleaseDesktopProductShell
              createProvider={factory}
              stopProvider={stop}
              reportStage={reportStage}
              reportReady={reportReady}
            />
          </MemoryRouter>
        </StrictMode>,
      );
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(factory).toHaveBeenCalledTimes(1);
    expect(lifecycle.at(-1)).toBe("ready");
    expect(lifecycle.slice(0, -1)).toContain("stop");
    expect(reportReady).toHaveBeenCalledTimes(1);
    expect(reportStage.mock.calls.map(([stage]) => stage)).toEqual([
      "provider_created",
      "product_committed",
    ]);
    expect(productCommittedWhenReported).toBe(true);
    expect(document.body.textContent).toContain("Research brief");
  });

  it("ignores a provider resolved by a superseded factory", async () => {
    const first = deferred<FixtureDesktopProductProvider>();
    const staleProvider = createFixtureDesktopProductProvider({ newUser: true });
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    const firstFactory = vi.fn(() => first.promise);
    const secondFactory = vi.fn(async () => provider!);
    const stop = vi.fn(async () => {});
    const reportReady = vi.fn(async () => {});

    const container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root?.render(
        <MemoryRouter>
          <ReleaseDesktopProductShell
            createProvider={firstFactory}
            stopProvider={stop}
            reportReady={reportReady}
          />
        </MemoryRouter>,
      );
      await Promise.resolve();
    });
    const stopsBeforeSupersession = stop.mock.calls.length;
    await act(async () => {
      root?.render(
        <MemoryRouter>
          <ReleaseDesktopProductShell
            createProvider={secondFactory}
            stopProvider={stop}
            reportReady={reportReady}
          />
        </MemoryRouter>,
      );
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(stop.mock.calls.length).toBeGreaterThan(stopsBeforeSupersession);
    expect(document.body.textContent).toContain("Starting OpenEvo Desktop");

    await act(async () => {
      first.resolve(staleProvider);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(document.body.textContent).toContain("Research brief");
    expect(document.body.textContent).not.toContain("Create your first research project");
    expect(stop).toHaveBeenCalled();
    expect(reportReady).toHaveBeenCalledTimes(1);
    staleProvider.dispose();
  });

  it("keeps the renderer-owned demo when native renderer readiness fails", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    const factory = vi.fn(async () => provider!);
    const stop = vi.fn(async () => {});
    const reportReady = vi.fn(async () => {
      throw new Error("native renderer contract mismatch");
    });

    root = await renderReleaseShell(factory, stop, reportReady);

    expect(reportReady).toHaveBeenCalledTimes(1);
    expect(stop).toHaveBeenCalledTimes(2);
    expect(document.body.textContent).toContain("OpenEvo Desktop could not start");
    expect(document.querySelector('[data-testid="release-startup-sample"]')).not.toBeNull();
    expect(document.querySelector('[data-testid="sample-research-workspace"]')).not.toBeNull();
    expect(document.body.textContent).toContain("Demo");
    expect(document.body.textContent).not.toContain("native renderer contract mismatch");
  });

  it("shows a typed readiness failure after the product shell commits", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    const reportReady = vi.fn(async () => {
      throw startupApiError(
        "OpenEvo Desktop could not confirm the local startup state.",
        "Retry after the local service is available.",
      );
    });

    root = await renderReleaseShell(
      vi.fn(async () => provider!),
      vi.fn(async () => {}),
      reportReady,
    );

    expect(reportReady).toHaveBeenCalledTimes(1);
    expect(document.body.textContent).toContain("The local OpenEvo Desktop service reported a startup error.");
    expect(document.body.textContent).not.toContain("OpenEvo Desktop could not confirm the local startup state.");
    expect(document.body.textContent).not.toContain("Retry after the local service is available.");
    expect(button("Retry").disabled).toBe(false);
  });

  it("keeps bootstrap diagnostics outside product readiness authority", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    const factory = vi.fn(async () => provider!);
    const stop = vi.fn(async () => {});
    const reportReady = vi.fn(async () => {});
    const reportStage = vi.fn()
      .mockImplementationOnce(() => {
        throw new Error("diagnostic bridge unavailable");
      })
      .mockRejectedValueOnce(new Error("diagnostic bridge rejected"));

    root = await renderReleaseShell(factory, stop, reportReady, reportStage);

    expect(reportStage).toHaveBeenCalledTimes(2);
    expect(reportReady).toHaveBeenCalledTimes(1);
    expect(document.body.textContent).toContain("Research brief");
    expect(document.body.textContent).not.toContain("diagnostic bridge");
  });

  it("does not classify native stop failure as provider creation failure", async () => {
    const factory = vi.fn();
    const stop = vi.fn(async () => {
      throw new Error("native stop unavailable");
    });
    const reportStage = vi.fn();

    root = await renderReleaseShell(
      factory,
      stop,
      vi.fn().mockResolvedValue(undefined),
      reportStage,
    );

    expect(factory).not.toHaveBeenCalled();
    expect(reportStage).not.toHaveBeenCalledWith("provider_create_failed");
    expect(document.body.textContent).toContain("OpenEvo Desktop could not start");
    expect(document.querySelector('[data-testid="sample-research-workspace"]')).not.toBeNull();
    expect(document.body.textContent).not.toContain("native stop unavailable");
  });
});

async function renderReleaseShell(
  factory: () => Promise<FixtureDesktopProductProvider>,
  stopProvider?: () => Promise<void>,
  reportReady?: () => Promise<void>,
  reportStage?: (stage: string) => Promise<void> | void,
  getStartupStatus?: () => Promise<NativeStartupStatusV2>,
): Promise<Root> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const rendered = createRoot(container);
  await act(async () => {
    rendered.render(
      <MemoryRouter>
        <ReleaseDesktopProductShell
          createProvider={factory}
          stopProvider={stopProvider}
          reportStage={reportStage}
          reportReady={reportReady}
          getStartupStatus={getStartupStatus}
        />
      </MemoryRouter>,
    );
    await Promise.resolve();
    await Promise.resolve();
  });
  return rendered;
}

function button(label: string): HTMLButtonElement {
  const target = [...document.querySelectorAll<HTMLButtonElement>("button")]
    .find((candidate) => candidate.textContent?.trim() === label);
  if (!target) throw new Error(`Button ${label} was not found.`);
  return target;
}

function deferred<T>(): {
  readonly promise: Promise<T>;
  readonly resolve: (value: T) => void;
} {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function startupApiError(message: string, nextAction: string): DesktopApiError {
  return new DesktopApiError({
    schema_version: "1",
    request_id: "desktop-startup-request",
    code: "desktop_startup_failed",
    http_status: 503,
    message,
    severity: "blocking",
    category: "service",
    retryable: true,
    repair_action: "openevo_can_retry",
    next_action: nextAction,
    details: { field_issues: [], conflicts: [] },
    logs_ref: null,
  });
}
