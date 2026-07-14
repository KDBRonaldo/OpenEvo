// @vitest-environment happy-dom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
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
    const factory = vi.fn()
      .mockRejectedValueOnce(new Error("secret bootstrap detail"))
      .mockResolvedValueOnce(provider);

    root = await renderReleaseShell(factory);
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
  });

  it("ignores a provider resolved by a superseded factory", async () => {
    const first = deferred<FixtureDesktopProductProvider>();
    const staleProvider = createFixtureDesktopProductProvider({ newUser: true });
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    const firstFactory = vi.fn(() => first.promise);
    const secondFactory = vi.fn(async () => provider!);

    const container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root?.render(
        <MemoryRouter>
          <ReleaseDesktopProductShell createProvider={firstFactory} />
        </MemoryRouter>,
      );
      await Promise.resolve();
    });
    await act(async () => {
      root?.render(
        <MemoryRouter>
          <ReleaseDesktopProductShell createProvider={secondFactory} />
        </MemoryRouter>,
      );
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(document.body.textContent).toContain("Research brief");

    await act(async () => {
      first.resolve(staleProvider);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(document.body.textContent).toContain("Research brief");
    expect(document.body.textContent).not.toContain("Create your first research project");
    staleProvider.dispose();
  });
});

async function renderReleaseShell(
  factory: () => Promise<FixtureDesktopProductProvider>,
): Promise<Root> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const rendered = createRoot(container);
  await act(async () => {
    rendered.render(
      <MemoryRouter>
        <ReleaseDesktopProductShell createProvider={factory} />
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
