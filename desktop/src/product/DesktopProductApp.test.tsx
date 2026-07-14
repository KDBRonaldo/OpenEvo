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
    await clickButton("Text memory");
    await flush();
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

  it("retries unavailable capabilities for the same project and mode without losing the draft", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    root = await renderProduct(provider);
    await act(async () => provider?.setCapabilitiesUnavailableUntilRefresh());
    await flush();

    await clickAria("Project settings");
    setInput("Objective", "Keep this capability retry draft.");
    expect(screenText()).toContain("Capabilities are unavailable for this project and mode.");
    await clickButton("Retry capabilities");

    expect(screenText()).toContain("Keep this capability retry draft.");
    expect(screenText()).not.toContain("Capabilities are unavailable for this project and mode.");
  });

  it("edits every ordinary field in a closed remote method config schema", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    provider.useEditableMethodSchema();
    root = await renderProduct(provider);
    await clickAria("Project settings");

    setInput("Reflection prompt", "Retain only verified findings.");
    setInput("Iterations", "7");
    setInput("Temperature", "0.25");
    setSelect("Strategy", "strict");
    setCheckbox("Include failures", true);
    setInput("Minimum score", "0.8");
    setInput("Tags", '["evidence","review"]');
    await clickButton("Save");

    const refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("Fixture refresh was not fresh.");
    expect(refreshed.snapshot.projects[0]?.evolution.targets.text_memory?.config).toEqual({
      prompt: "Retain only verified findings.",
      iterations: 7,
      temperature: 0.25,
      strategy: "strict",
      include_failures: true,
      advanced: { minimum_score: 0.8 },
      tags: ["evidence", "review"],
    });
  });

  it("deep-merges method defaults for display while saving only the partial user override", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    provider.useEditableMethodSchemaWithPartialOverride();
    root = await renderProduct(provider);
    await clickAria("Project settings");

    expect(labelledControl<HTMLInputElement>("Reflection prompt", "input").value).toBe("Keep durable findings.");
    expect(labelledControl<HTMLInputElement>("Iterations", "input").value).toBe("5");
    expect(labelledControl<HTMLInputElement>("Minimum score", "input").value).toBe("0.5");
    setInput("Minimum score", "0.8");
    await clickButton("Save");

    const refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("Fixture refresh was not fresh.");
    expect(refreshed.snapshot.projects[0]?.evolution.targets.text_memory?.config).toEqual({
      iterations: 5,
      advanced: { minimum_score: 0.8 },
    });
  });

  it("re-enables a supported saved method without an effective default", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    provider.useNullEffectiveDefault();
    root = await renderProduct(provider);
    await clickAria("Project settings");

    const toggle = document.querySelector<HTMLInputElement>('.target-toggle[data-target-id="text_memory"] input[role="switch"]');
    expect(toggle?.checked).toBe(false);
    expect(toggle?.disabled).toBe(false);
    await act(async () => toggle?.click());
    expect(toggle?.checked).toBe(true);
    expect(document.querySelector<HTMLSelectElement>('select[aria-label="Text memory method"]')?.value).toBe("reference_text_memory");
  });

  it("requires an effective default when a disabled target has no saved method", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    provider.useNullEffectiveDefaultWithoutSavedMethod();
    root = await renderProduct(provider);
    await clickAria("Project settings");

    const toggle = document.querySelector<HTMLInputElement>('.target-toggle[data-target-id="text_memory"] input[role="switch"]');
    expect(toggle?.checked).toBe(false);
    expect(toggle?.disabled).toBe(true);
    expect(screenText()).toContain("No supported default is available from the remote registry.");
  });

  it("renders typed queued, succeeded, failed, and cancelled run outcomes with recovery", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    provider.useRunStateReviewScenario();
    root = await renderProduct(provider);

    expect(screenText()).toContain("Model preparation");
    expect(screenText()).toContain("The selected model is being prepared.");
    expect(screenText()).toContain("The model worker could not load the selected model.");
    expect(screenText()).toContain("Complete");
    expect(screenText()).toContain("Failed");
    expect(screenText()).toContain("Cancelled");
    expect(document.querySelectorAll('[role="columnheader"]')).toHaveLength(6);
    expect(document.querySelectorAll('[role="cell"]').length).toBeGreaterThanOrEqual(24);

    await clickButton("Cancel session");
    expect(button("Retry session").disabled).toBe(false);
    await clickButton("Retry session");
    expect(screenText()).toContain("Preparing the remote workspace.");
  });

  it("shows every selected revision member, including multiple artifacts for one target, in stable order", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    provider.useAuthoritativeArtifactOrderingScenario();
    root = await renderProduct(provider);
    await clickButton("Evolution");
    await flush();

    expect(screenText()).toContain("Revision 4");
    expect(screenText()).not.toContain("Unselected newer artifact");
    const artifactNames = Array.from(document.querySelectorAll(".artifact-list-item strong"), (item) => item.textContent);
    expect(artifactNames).toEqual(["Skills", "Text memory", "Text memory", "Agent guidance"]);
    const artifactSummaries = Array.from(document.querySelectorAll(".artifact-list-item small"), (item) => item.textContent);
    expect(artifactSummaries).toEqual([
      "Reusable analysis and validation routines.",
      "Additional selected memory",
      "Durable findings and constraints from this session.",
      "Updated operating guidance for the next session.",
    ]);

    provider.makeRevisionEvidenceUnknown();
    await flush();
    expect(screenText()).toContain("Revision unknown");
    expect(button("Refetch revision").disabled).toBe(false);
  });

  it("does not present a partial paginated artifact collection as complete revision membership", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    provider.useAuthoritativeArtifactOrderingScenario();
    provider.markArtifactCollectionIncomplete();
    root = await renderProduct(provider);
    await clickButton("Evolution");

    expect(screenText()).toContain("Artifact collection is incomplete");
    expect(document.querySelectorAll(".artifact-list-item")).toHaveLength(0);
    expect(button("Refetch artifacts").disabled).toBe(false);
  });

  it("reloads typed 409/410/412 failures without replaying stale mutations", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    root = await renderProduct(provider);

    await clickAria("Project settings");
    setInput("Objective", "Draft retained across an etag refresh.");
    provider.failNextProjectSaveWithStatus(412);
    await clickButton("Save");
    expect(screenText()).toContain("Draft retained across an etag refresh.");
    expect(screenText()).toContain("The project changed remotely.");
    expect(provider.projectUpdateAttempts()).toBe(1);
    await clickButton("Save");
    expect(provider.projectUpdateAttempts()).toBe(2);

    provider.failNextRunStartWithStatus(409);
    await clickButton("Start session");
    expect(provider.runStartAttempts()).toBe(1);
    expect(button("Re-admit session").disabled).toBe(false);
    await clickButton("Re-admit session");
    expect(provider.runStartAttempts()).toBe(2);
    await clickButton("Cancel session");

    provider.failNextRunStartWithStatus(410);
    await clickButton("Start session");
    expect(provider.runStartAttempts()).toBe(3);
    expect(provider.refreshCount()).toBeGreaterThanOrEqual(4);
    expect(screenText()).toContain("The event cursor expired.");
  });

  it("offers re-admission only for an explicitly retryable admission conflict with no equivalent run", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    root = await renderProduct(provider);

    provider.failNextRunStartWithConflict({
      code: "idempotency_key_reused",
      retryable: false,
      repairAction: "none",
    });
    await clickButton("Start session");
    expect(screenText()).toContain("That action identity belongs to another request.");
    expect(optionalButton("Re-admit session")).toBeNull();

    provider.failNextRunStartWithConflict({
      code: "run_admission_conflict",
      retryable: true,
      repairAction: "openevo_can_retry",
      addEquivalentRun: true,
    });
    await clickButton("Start session");
    expect(screenText()).toContain("The original session is already queued.");
    expect(screenText()).toContain("Active session");
    expect(optionalButton("Re-admit session")).toBeNull();
  });

  it("keeps update action identities across uncertain responses and replaces them after a changed precondition", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    root = await renderProduct(provider);

    await clickAria("Project settings");
    setInput("Objective", "Keep one project update identity.");
    provider.failNextProjectSaveWithUnknownError();
    await clickButton("Save");
    await clickButton("Save");
    const uncertainIds = provider.projectUpdateActionIds();
    expect(uncertainIds[0]).toBe(uncertainIds[1]);

    await clickAria("Project settings");
    setInput("Objective", "Use a new identity after editing.");
    provider.failNextProjectSaveWithStatus(412);
    await clickButton("Save");
    await clickButton("Save");
    const allIds = provider.projectUpdateActionIds();
    expect(allIds[2]).not.toBe(allIds[1]);
    expect(allIds[3]).not.toBe(allIds[2]);

    await clickAria("Remote workspace settings");
    setInput("Workspace name", "Keep one profile update identity.");
    provider.failNextProfileSaveWithUnknownError();
    await clickButton("Save workspace");
    await clickButton("Save workspace");
    expect(provider.profileUpdateActionIds()[0]).toBe(provider.profileUpdateActionIds()[1]);
  });

  it("keeps create action identities when profile and project responses are uncertain", async () => {
    provider = createFixtureDesktopProductProvider({ newUser: true });
    root = await renderProduct(provider);

    await clickButton("Add workspace");
    setInput("Server address", "lab.example.test");
    setInput("User name", "researcher");
    provider.failNextProfileCreateWithUnknownError();
    await clickButton("Save workspace");
    await clickButton("Save workspace");
    expect(provider.profileCreateActionIds()[0]).toBe(provider.profileCreateActionIds()[1]);

    await clickAria("Create project");
    setInput("Objective", "Keep one project create identity.");
    provider.failNextProjectCreateWithUnknownError();
    await clickButton("Save");
    await clickButton("Save");
    expect(provider.projectCreateActionIds()[0]).toBe(provider.projectCreateActionIds()[1]);
  });

  it("marks segmented controls as tabs with an explicit selected state", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    root = await renderProduct(provider);
    await clickAria("Project settings");

    const tablists = document.querySelectorAll('[role="tablist"]');
    expect(tablists.length).toBeGreaterThanOrEqual(2);
    expect(document.querySelector('[role="tab"][aria-selected="true"]')).not.toBeNull();
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

  it("keeps dirty drawer drafts until Escape, overlay, or close is confirmed", async () => {
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

    await pressEscape();
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    expect(screenText()).toContain("Discard unsaved changes?");
    await clickButton("Keep editing");

    const backdrop = document.querySelector<HTMLElement>(".drawer-backdrop");
    if (!backdrop) throw new Error("Drawer backdrop was not found.");
    await act(async () => backdrop.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })));
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    expect(screenText()).toContain("Discard unsaved changes?");
    await clickButton("Keep editing");

    await clickAria("Close settings");
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    expect(screenText()).toContain("A retained draft objective.");
    await clickButton("Discard changes");
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

function optionalButton(label: string): HTMLButtonElement | null {
  return Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
    .find((item) => item.textContent?.trim() === label) ?? null;
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

function setCheckbox(label: string, checked: boolean): void {
  const control = labelledControl<HTMLInputElement>(label, 'input[type="checkbox"]');
  act(() => {
    if (control.checked !== checked) control.click();
  });
}

async function pressEscape(): Promise<void> {
  await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
  await flush();
}

function labelledControl<T extends HTMLElement>(text: string, selector: string): T {
  const label = Array.from(document.querySelectorAll("label")).find((item) => item.textContent?.trim().startsWith(text));
  const control = label?.querySelector<T>(selector);
  if (!control) throw new Error(`Control ${text} was not found.`);
  return control;
}

function screenText(): string {
  return document.body.textContent ?? "";
}
