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
    const reportStage = vi.fn(async (stage: string) => { lifecycle.push(stage); });
    const reportReady = vi.fn().mockResolvedValue(undefined);

    root = await renderReleaseShell(
      factory,
      stop,
      reportReady,
      reportStage,
    );
    expect(document.body.textContent).toContain("暂时无法连接 OpenEvo Desktop");
    expect(document.querySelector('[data-testid="sample-research-workspace"]')).not.toBeNull();
    expect(document.body.textContent).not.toContain("secret bootstrap detail");
    expect(factory).toHaveBeenCalledTimes(1);
    expect(reportReady).not.toHaveBeenCalled();

    const retry = button("重试启动");
    await act(async () => {
      retry.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(factory).toHaveBeenCalledTimes(2);
    expect(reportStage).toHaveBeenCalledWith("provider_create_failed");
    expect(reportStage).toHaveBeenCalledWith("product_committed");
    expect(document.body.textContent).toContain("Research brief");
    const firstStart = lifecycle.indexOf("start-1");
    const secondStart = lifecycle.indexOf("start-2");
    expect(lifecycle.slice(firstStart + 1, secondStart)).toContain("stop");
  });

  it("keeps the startup fallback renderer-owned and read-only", async () => {
    const factory = vi.fn(async () => {
      throw new Error("native sidecar unavailable");
    });
    const stop = vi.fn(async () => {});
    const reportReady = vi.fn(async () => {});

    root = await renderReleaseShell(factory, stop, reportReady);

    expect(document.querySelector('[data-testid="release-startup-sample"]')).not.toBeNull();
    expect(document.querySelector('[data-testid="sample-research-workspace"]')).not.toBeNull();
    expect(button("重试启动").disabled).toBe(false);
    expect(reportReady).not.toHaveBeenCalled();
    expect(document.body.textContent).toContain("内置示例 · 只读");
    expect(document.body.textContent).toContain("不会连接本机服务或远端服务器");
    expect(document.body.textContent).not.toContain("Renderer sample");
    expect(document.body.textContent).not.toContain("provider");
    expect(
      [...document.querySelectorAll("button")].some((candidate) =>
        /Add workspace|Create project/i.test(candidate.textContent ?? "")
      ),
    ).toBe(false);

    await act(async () => {
      button("Evolution").click();
    });
    expect(document.querySelector('[data-testid="sample-evolution-workspace"]')).not.toBeNull();
    await act(async () => {
      button("System").click();
    });
    expect(document.querySelector('[data-testid="sample-about-workspace"]')).not.toBeNull();
    expect(document.body.textContent).toContain("静态、只读、不会运行");
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
    expect(document.body.textContent).toContain("正在启动 OpenEvo Desktop");

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

  it("keeps the renderer-owned read-only sample when native renderer readiness fails", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    const factory = vi.fn(async () => provider!);
    const stop = vi.fn(async () => {});
    const reportReady = vi.fn(async () => {
      throw new Error("native renderer contract mismatch");
    });

    root = await renderReleaseShell(factory, stop, reportReady);

    expect(reportReady).toHaveBeenCalledTimes(1);
    expect(stop).toHaveBeenCalledTimes(2);
    expect(document.body.textContent).toContain("暂时无法连接 OpenEvo Desktop");
    expect(document.querySelector('[data-testid="release-startup-sample"]')).not.toBeNull();
    expect(document.querySelector('[data-testid="sample-research-workspace"]')).not.toBeNull();
    expect(document.body.textContent).toContain("内置示例 · 只读");
    expect(document.body.textContent).toContain("不会连接本机服务或远端服务器");
    expect(document.body.textContent).not.toContain("native renderer contract mismatch");
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
    expect(document.body.textContent).toContain("暂时无法连接 OpenEvo Desktop");
    expect(document.querySelector('[data-testid="sample-research-workspace"]')).not.toBeNull();
    expect(document.body.textContent).not.toContain("native stop unavailable");
  });
});

async function renderReleaseShell(
  factory: () => Promise<FixtureDesktopProductProvider>,
  stopProvider?: () => Promise<void>,
  reportReady?: () => Promise<void>,
  reportStage?: (stage: string) => Promise<void> | void,
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
