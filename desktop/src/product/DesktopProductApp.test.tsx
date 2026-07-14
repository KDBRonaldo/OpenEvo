// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DesktopProductApp } from "./DesktopProductApp";
import { createFixtureDesktopProductProvider, type FixtureDesktopProductProvider } from "./fixtureProvider";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("DesktopProductApp", () => {
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
    vi.useRealTimers();
    document.body.innerHTML = "";
  });

  it("navigates between Research, Evolution, and System", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    root = await renderProduct(provider);

    expect(screenText()).toContain("Research brief");
    await clickButton("Evolution");
    expect(screenText()).toContain("Cross-session changes");
    await clickButton("System");
    expect(screenText()).toContain("Remote environment");
    await clickButton("Research");
    expect(screenText()).toContain("Session history");
  });

  it("gates sessions offline and completes first-time workspace setup", async () => {
    vi.useFakeTimers();
    provider = createFixtureDesktopProductProvider({ stepDelayMs: 20 });
    root = await renderProduct(provider);

    expect(screenText()).toContain("Add a remote workspace");
    expect(screenText()).not.toContain("Start session");
    await clickButton("Add workspace");
    setInput("Workspace name", "Lab server");
    setInput("Server address", "lab.example.test");
    setInput("User name", "researcher");
    setSelect("Method", "native_password");
    await clickButton("Save workspace");

    expect(screenText()).toContain("Configure the Server password before connecting.");
    await clickButton("Configure");
    expect(screenText()).toContain("Not configured");
    const credentialButton = document.querySelector<HTMLButtonElement>(".credential-row button");
    if (!credentialButton) throw new Error("Credential configure button was not found.");
    await clickElement(credentialButton);
    expect(screenText()).toContain("Stored securely");
    await clickAria("Close connection settings");

    await clickButton("Connect");
    await advance(25);
    expect(screenText()).toContain("Confirm server identity");
    await clickButton("Trust and continue");
    expect(screenText()).toContain("Checking environment");
    await advance(25);
    expect(screenText()).toContain("Preparing OpenEvo");
    await advance(25);
    expect(screenText()).toContain("Online");
    expect(screenText()).toContain("Create a research project");

    await clickButton("Create project");
    setInput("Project name", "Catalyst study");
    setInput("Task title", "Compare catalyst candidates");
    setInput("Objective", "Rank candidates using reproducible evidence.");
    setInput("Hugging Face model", "Qwen/Qwen3-8B");
    await clickButton("Save");
    expect(screenText()).toContain("Compare catalyst candidates");
    expect(button("Start session").disabled).toBe(false);
  });

  it("renders fixture successor states and the later pinned revision", async () => {
    vi.useFakeTimers();
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true, stepDelayMs: 20 });
    root = await renderProduct(provider);

    await clickButton("Start session");
    expect(screenText()).toContain("Queued");
    expect(screenText()).toContain("Revision 2");
    await advance(45);
    expect(screenText()).toContain("Running");
    await advance(60);
    expect(screenText()).toContain("Preparing next revision");
    expect(screenText()).toContain("Revision 3");
    await advance(25);
    expect(screenText()).toContain("Revision 3 is active");

    await clickButton("Start session");
    expect(screenText()).toContain("Pinned context");
    expect(screenText()).toContain("Revision 3");
  });

  it("shows artifact content, changes, document tabs, and truncation", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true, artifactTruncated: true });
    root = await renderProduct(provider);

    await clickButton("Evolution");
    await flush();
    expect(screenText()).toContain("Preview is truncated");
    expect(screenText()).toContain("Research memory");
    await clickButton("Skills");
    await flush();
    expect(screenText()).toContain("Analysis workflow");
    expect(screenText()).toContain("Result verification");
    await clickButton("Changes");
    await flush();
    expect(screenText()).toContain("Added for Revision 2");
  });

  it("keeps implementation and sensitive operational terms out of the product surface", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    root = await renderProduct(provider);
    await clickButton("System");
    await clickAria("Remote workspace settings");

    const text = screenText().toLowerCase();
    for (const forbidden of [
      "benchmark",
      "core url",
      "host path",
      "stdout",
      "stderr",
      "method implementation",
      "process id",
      "command line",
    ]) {
      expect(text).not.toContain(forbidden);
    }
    expect(document.querySelector('input[type="password"]')).toBeNull();
    expect(screenText()).toContain("Stored securely");
  });

  it("blocks mutations while the event stream is stale and recovers from a snapshot refresh", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    root = await renderProduct(provider);

    await act(async () => provider?.markStreamStale());
    await flush();
    expect(button("Start session").disabled).toBe(true);
    expect(screenText()).toContain("Refresh this view");

    await clickButton("Refresh");
    expect(button("Start session").disabled).toBe(false);

    await act(async () => provider?.resetEventCursor());
    await flush();
    expect(button("Start session").disabled).toBe(false);

    provider.failNextRefresh();
    await act(async () => provider?.resetEventCursor());
    await flush();
    expect(button("Start session").disabled).toBe(true);
    expect(screenText()).not.toContain("internal refresh details");
  });

  it("shows remote targets for an empty project map and blocks an unsupported saved method", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    provider.clearEvolutionSelections();
    root = await renderProduct(provider);
    await clickAria("Project settings");

    expect(document.querySelectorAll(".target-toggle")).toHaveLength(3);
    expect(screenText()).toContain("Text memory");
    await clickAria("Close settings");

    provider.useUnsupportedSavedMethod();
    await flush();
    expect(button("Start session").disabled).toBe(true);
    expect(screenText()).toContain("unsupported for this project and mode");
    await clickAria("Project settings");
    expect(screenText()).toContain("removed_text_memory (no longer available)");
    const staleToggle = document.querySelector<HTMLInputElement>('.target-toggle input[role="switch"]');
    if (!staleToggle) throw new Error("Unsupported target toggle was not found.");
    await act(async () => staleToggle.click());
    expect(staleToggle.checked).toBe(false);
    await act(async () => staleToggle.click());
    const repairedMethod = document.querySelector<HTMLSelectElement>('.target-toggle select[aria-label="Text memory method"]');
    expect(staleToggle.checked).toBe(true);
    expect(repairedMethod?.value).toBe("reference_text_memory");
  });

  it("preserves accepted existing methods and offers supported Core selection resolvers", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    provider.useAcceptedSavedMethod();
    root = await renderProduct(provider);

    expect(button("Start session").disabled).toBe(false);
    await clickAria("Project settings");
    expect(screenText()).toContain("hidden_text_memory (existing selection)");
    const hiddenOption = Array.from(document.querySelectorAll<HTMLOptionElement>("option")).find((item) => item.value === "hidden_text_memory");
    expect(hiddenOption?.disabled).toBe(true);
    await clickAria("Close settings");

    provider.useResolverSavedMethod();
    await flush();
    expect(button("Start session").disabled).toBe(false);
    await clickAria("Project settings");
    const resolver = Array.from(document.querySelectorAll<HTMLOptionElement>("option")).find((item) => item.value === "auto");
    expect(resolver?.disabled).toBe(false);
    expect(resolver?.textContent).toContain("Automatic");
  });

  it("fails closed across mode changes until the saved project receives matching capabilities", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    root = await renderProduct(provider);

    await clickAria("Project settings");
    await clickButton("Subscription");
    expect(screenText()).toContain("Capabilities are unavailable for this project and mode.");
    await clickButton("Save");

    expect(screenText()).not.toContain("Research configuration");
    expect(button("Start session").disabled).toBe(false);
  });

  it("requires explicit activation after a project switch", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    provider.addDraftProject();
    root = await renderProduct(provider);

    const switcher = document.querySelector<HTMLSelectElement>("#project-switcher");
    if (!switcher) throw new Error("Project switcher was not found.");
    await act(async () => {
      switcher.value = "project-fixture-2";
      switcher.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(screenText()).toContain("Activate this project");
    expect(button("Start session").disabled).toBe(true);

    await clickButton("Activate project");
    expect(screenText()).toContain("Second research task");
    expect(button("Start session").disabled).toBe(false);
  });

  it("selects and syncs a native folder through opaque source references", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    root = await renderProduct(provider);

    await clickAria("Project settings");
    await clickButton("Folder snapshot");
    expect(screenText()).toContain("Selected research folder");
    expect(document.querySelector('input[type="file"]')).toBeNull();
    await clickButton("Save");
    await clickAria("Project settings");
    await clickButton("Sync snapshot");
    await clickAria("Close settings");

    const refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("Fixture refresh was not fresh.");
    expect(refreshed.snapshot.projects[0]?.source).toMatchObject({
      kind: "native_folder_snapshot",
      display_name: "Selected research folder",
      source_ref: { content_id: "source-fixture-1" },
    });
  });

  it("keeps drawers and drafts after save failures and restores focus on Escape", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    root = await renderProduct(provider);
    const opener = document.querySelector<HTMLButtonElement>('button[aria-label="Project settings"]');
    if (!opener) throw new Error("Project settings opener was not found.");
    opener.focus();
    await clickAria("Project settings");
    setInput("Objective", "A retained draft objective.");
    provider.failNextProjectSaveWithUnknownError();
    await clickButton("Save");

    expect(screenText()).toContain("A retained draft objective.");
    expect(screenText()).toContain("The request could not be completed.");
    expect(screenText()).not.toContain("internal host path");
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();

    await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement).toBe(opener);
  });
});

async function renderProduct(fixture: FixtureDesktopProductProvider): Promise<Root> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const rendered = createRoot(container);
  await act(async () => {
    rendered.render(<DesktopProductApp provider={fixture} />);
    await Promise.resolve();
  });
  await flush();
  return rendered;
}

async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function advance(milliseconds: number): Promise<void> {
  await act(async () => {
    vi.advanceTimersByTime(milliseconds);
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function clickButton(label: string): Promise<void> {
  const target = button(label);
  await act(async () => {
    target.click();
    await Promise.resolve();
  });
  await flush();
}

async function clickAria(label: string): Promise<void> {
  const target = document.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
  if (!target) throw new Error(`Button with aria-label ${label} was not found.`);
  await act(async () => {
    target.click();
    await Promise.resolve();
  });
  await flush();
}

async function clickElement(target: HTMLButtonElement): Promise<void> {
  await act(async () => {
    target.click();
    await Promise.resolve();
  });
  await flush();
}

function button(label: string): HTMLButtonElement {
  const all = Array.from(document.querySelectorAll<HTMLButtonElement>("button"));
  const exact = all.filter((item) => item.textContent?.trim() === label);
  const matches = exact.length ? exact : all.filter((item) => item.textContent?.trim().startsWith(label));
  const enabled = matches.find((item) => !item.disabled);
  const target = enabled ?? matches[0];
  if (!target) throw new Error(`Button ${label} was not found.`);
  return target;
}

function setInput(label: string, value: string): void {
  const control = labelledControl<HTMLInputElement>(label, "input, textarea");
  act(() => {
    const prototype = control instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value")?.set?.call(control, value);
    control.dispatchEvent(new Event("input", { bubbles: true }));
    control.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

function setSelect(label: string, value: string): void {
  const control = labelledControl<HTMLSelectElement>(label, "select");
  act(() => {
    control.value = value;
    control.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

function labelledControl<T extends HTMLElement>(text: string, selector: string): T {
  const label = Array.from(document.querySelectorAll("label")).find((item) => item.childNodes[0]?.textContent?.trim() === text);
  const control = label?.querySelector<T>(selector);
  if (!control) throw new Error(`Control ${text} was not found.`);
  return control;
}

function screenText(): string {
  return document.body.textContent ?? "";
}
