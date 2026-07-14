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

  it("runs a session through successor activation and pins it on the next session", async () => {
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
