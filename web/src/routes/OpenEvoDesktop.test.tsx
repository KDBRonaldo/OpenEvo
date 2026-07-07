// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OpenEvoDesktop } from "./OpenEvoDesktop";
import {
  getOpenEvoDesktopShellModel,
  type OpenEvoDesktopShellModel,
} from "./openevoDesktopModel";

const apiMocks = vi.hoisted(() => ({
  fetchOpenEvoDesktopShellModel: vi.fn(),
  runOpenEvoBootstrap: vi.fn(),
}));

vi.mock("../api/openevo", () => ({
  fetchOpenEvoDesktopShellModel: apiMocks.fetchOpenEvoDesktopShellModel,
  runOpenEvoBootstrap: apiMocks.runOpenEvoBootstrap,
}));

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

describe("OpenEvoDesktop", () => {
  beforeEach(() => {
    apiMocks.fetchOpenEvoDesktopShellModel.mockReset();
    apiMocks.runOpenEvoBootstrap.mockReset();
    apiMocks.fetchOpenEvoDesktopShellModel.mockRejectedValue(
      new Error("sidecar unavailable"),
    );
    apiMocks.runOpenEvoBootstrap.mockRejectedValue(
      new Error("sidecar unavailable"),
    );
    document.body.innerHTML = "";
  });

  afterEach(() => {
    document.body.innerHTML = "";
  });

  it("renders fixture state when the sidecar fetch is unavailable", () => {
    const html = renderToString(<OpenEvoDesktop />);

    expect(html).toContain("Protein Folding Literature Sprint");
    expect(html).toContain("codex_subscription_transcript");
    expect(html).toContain("Remote ready");
  });

  it("runs bootstrap from the button and refreshes visible status", async () => {
    const shellModel = modelWithBootstrap({
      projectName: "Loaded Science Project",
      ready: false,
      notes: ["Remote bootstrap has not run yet."],
      bootstrapDetail: "Remote bootstrap has not run yet",
    });
    const bootstrappedModel = modelWithBootstrap({
      projectName: "Loaded Science Project",
      ready: true,
      notes: ["Remote bootstrap is ready."],
      bootstrapDetail: "Runtime image and manifests prepared",
    });
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(shellModel);
    const deferred = deferBootstrap(bootstrappedModel);
    apiMocks.runOpenEvoBootstrap.mockReturnValue(deferred.promise);

    const root = await renderClient();
    await flushEffects();

    expect(document.body.textContent).toContain("Loaded Science Project");
    expect(document.body.textContent).toContain("Setup required");

    const button = buttonByText("Bootstrap");
    expect(button.disabled).toBe(false);

    await act(async () => {
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(apiMocks.runOpenEvoBootstrap).toHaveBeenCalledTimes(1);
    expect(document.body.textContent).toContain("Bootstrapping");

    await act(async () => {
      deferred.resolve({
        bootstrap: bootstrappedModel.bootstrap,
        report: { ready: true },
        status: bootstrappedModel,
      });
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("Remote ready");
    expect(document.body.textContent).toContain("Remote bootstrap is ready.");
    await unmountClient(root);
  });

  it("shows a bootstrap error when the sidecar rejects the action", async () => {
    apiMocks.fetchOpenEvoDesktopShellModel.mockResolvedValue(
      modelWithBootstrap({
        projectName: "Loaded Science Project",
        ready: false,
        notes: ["Remote bootstrap has not run yet."],
        bootstrapDetail: "Remote bootstrap has not run yet",
      }),
    );
    apiMocks.runOpenEvoBootstrap.mockRejectedValue(
      new Error("HTTP 403 Forbidden: invalid token"),
    );

    const root = await renderClient();
    await flushEffects();

    await act(async () => {
      buttonByText("Bootstrap").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain(
      "HTTP 403 Forbidden: invalid token",
    );
    await unmountClient(root);
  });
});

async function renderClient(): Promise<Root> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(<OpenEvoDesktop />);
  });
  return root;
}

async function flushEffects() {
  await act(async () => {
    await Promise.resolve();
  });
}

async function unmountClient(root: Root) {
  await act(async () => {
    root.unmount();
  });
}

function buttonByText(text: string): HTMLButtonElement {
  const button = Array.from(document.querySelectorAll("button")).find((item) =>
    item.textContent?.includes(text),
  );
  if (!(button instanceof HTMLButtonElement)) {
    throw new Error(`Button not found: ${text}`);
  }
  return button;
}

function modelWithBootstrap({
  projectName,
  ready,
  notes,
  bootstrapDetail,
}: {
  projectName: string;
  ready: boolean;
  notes: string[];
  bootstrapDetail: string;
}): OpenEvoDesktopShellModel {
  const model = getOpenEvoDesktopShellModel();
  return {
    ...model,
    project: {
      ...model.project,
      name: projectName,
    },
    bootstrap: {
      ...model.bootstrap,
      ready,
      readinessNotes: notes,
    },
    services: model.services.map((service) =>
      service.id === "bootstrap"
        ? {
            ...service,
            state: ready ? "ready" : "planned",
            detail: bootstrapDetail,
          }
        : service,
    ),
  };
}

function deferBootstrap(status: OpenEvoDesktopShellModel) {
  let resolve!: (value: {
    bootstrap: OpenEvoDesktopShellModel["bootstrap"];
    report: Record<string, any>;
    status: OpenEvoDesktopShellModel;
  }) => void;
  const promise = new Promise<{
    bootstrap: OpenEvoDesktopShellModel["bootstrap"];
    report: Record<string, any>;
    status: OpenEvoDesktopShellModel;
  }>((next) => {
    resolve = next;
  });
  return { promise, resolve, status };
}
