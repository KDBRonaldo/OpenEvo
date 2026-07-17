// @vitest-environment happy-dom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppShell, ReleaseDesktopProductShell } from "./App";
import { createFixtureDesktopProductProvider, type FixtureDesktopProductProvider } from "./product/fixtureProvider";

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

    expect(html).toContain("Loading workspace");
    expect(html).not.toContain("OpenEvo Observability");
    expect(html).not.toContain('href="/tasks"');
    expect(html).not.toContain(">Dashboard<");
  });

  it("renders OpenEvo Desktop at the root path in desktop-only mode", () => {
    const html = renderShell("/", true);

    expect(html).toContain("Loading workspace");
    expect(html).not.toContain("Not found");
  });
});

describe("ReleaseDesktopProductShell", () => {
  let root: Root | null = null;
  let provider: FixtureDesktopProductProvider | null = null;

  beforeEach(() => {
    document.body.innerHTML = "";
  });

  afterEach(async () => {
    provider?.dispose();
    provider = null;
    if (root) {
      await act(async () => root?.unmount());
      root = null;
    }
    document.body.innerHTML = "";
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

    root = await renderReleaseShell(factory, stop, vi.fn().mockResolvedValue(undefined));
    expect(document.body.textContent).toContain("OpenEvo Desktop could not start");
    expect(document.body.textContent).not.toContain("secret bootstrap detail");
    expect(factory).toHaveBeenCalledTimes(1);

    const retry = button("Retry startup");
    await act(async () => {
      retry.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(factory).toHaveBeenCalledTimes(2);
    expect(document.body.textContent).toContain("Research brief");
    const firstStart = lifecycle.indexOf("start-1");
    const secondStart = lifecycle.indexOf("start-2");
    expect(lifecycle.slice(firstStart + 1, secondStart)).toContain("stop");
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

  it("does not expose the product shell when native renderer readiness fails", async () => {
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
    expect(document.body.textContent).not.toContain("Research brief");
    expect(document.body.textContent).not.toContain("native renderer contract mismatch");
  });
});

async function renderReleaseShell(
  factory: () => Promise<FixtureDesktopProductProvider>,
  stopProvider?: () => Promise<void>,
  reportReady?: () => Promise<void>,
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
          reportReady={reportReady}
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
