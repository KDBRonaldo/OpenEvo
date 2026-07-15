// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ProjectSourceV1, RunV1 } from "../api/v1/schemas";
import { DesktopProductApp } from "./DesktopProductApp";
import { createFixtureDesktopProductProvider, type FixtureDesktopProductProvider } from "./fixtureProvider";
import {
  DesktopProductUserError,
  type DesktopProductSnapshot,
  type ProductResourceMutationIntent,
} from "./provider";
import { sameSessionOutputIdentity } from "./sessionOutputIdentity";

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

  it("shows bounded session output and filters agent and evolution records", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    root = await renderProduct(provider);

    expect(screenText()).toContain("Session output");
    expect(screenText()).toContain("Evidence synthesis completed with three supported findings.");
    expect(screenText()).toContain("Memory and skills were prepared for the next session.");

    await clickButton("Evolution logs");
    expect(screenText()).not.toContain("Evidence synthesis completed with three supported findings.");
    expect(screenText()).toContain("Memory and skills were prepared for the next session.");

    await clickButton("Agent logs");
    expect(screenText()).toContain("Evidence synthesis completed with three supported findings.");
    expect(screenText()).not.toContain("Memory and skills were prepared for the next session.");
  });

  it("refreshes live session output after authoritative stream updates", async () => {
    vi.useFakeTimers();
    provider = createFixtureDesktopProductProvider({ startOnline: true, stepDelayMs: 20 });
    root = await renderProduct(provider);

    await clickButton("Start session");
    expect(screenText()).toContain("Session admitted with an immutable project snapshot.");
    expect(screenText()).not.toContain("Research execution is using the selected workspace");

    await advance(45);
    expect(screenText()).toContain("Research execution is using the selected workspace and evidence sources.");
  });

  it("polls a nonterminal run from queued through running and stops at terminal without SSE", async () => {
    vi.useFakeTimers();
    provider = createFixtureDesktopProductProvider({ startOnline: true, stepDelayMs: 1_000 });
    vi.spyOn(provider, "subscribe").mockImplementation(() => () => undefined);
    const refresh = vi.spyOn(provider, "refresh");
    root = await renderProduct(provider);

    await clickButton("Start session");
    expect(screenText()).toContain("Queued");

    await advance(1_005);
    expect(screenText()).toContain("Preparing");
    await advance(1_005);
    expect(screenText()).toContain("Running");

    await advance(1_005);
    await advance(1_005);
    await advance(1_005);
    await advance(1_005);
    expect(screenText()).toContain("Latest session complete");

    const terminalRefreshCount = refresh.mock.calls.length;
    await advance(3_000);
    expect(refresh).toHaveBeenCalledTimes(terminalRefreshCount);
  });

  it("retries run polling after a transient refresh failure", async () => {
    vi.useFakeTimers();
    provider = createFixtureDesktopProductProvider({ startOnline: true, stepDelayMs: 900 });
    vi.spyOn(provider, "subscribe").mockImplementation(() => () => undefined);
    root = await renderProduct(provider);

    await clickButton("Start session");
    provider.failNextRefresh();
    const beforeFailure = provider.refreshCount();

    await advance(1_005);
    expect(provider.refreshCount()).toBe(beforeFailure + 1);
    expect(screenText()).toContain("Queued");

    await advance(1_005);
    expect(provider.refreshCount()).toBe(beforeFailure + 2);
    expect(screenText()).toContain("Running");
  });

  it("stops run polling after an authoritative refresh observes the session offline", async () => {
    vi.useFakeTimers();
    provider = createFixtureDesktopProductProvider({ startOnline: true, stepDelayMs: 10_000 });
    vi.spyOn(provider, "subscribe").mockImplementation(() => () => undefined);
    const refresh = vi.spyOn(provider, "refresh");
    root = await renderProduct(provider);

    await clickButton("Start session");
    provider.loseActiveCoreSession();
    await advance(1_005);
    expect(screenText()).toContain("Activate this project");

    const offlineRefreshCount = refresh.mock.calls.length;
    await advance(5_000);
    expect(refresh).toHaveBeenCalledTimes(offlineRefreshCount);
  });

  it("keeps polling serial and rejects its late result after switching projects", async () => {
    vi.useFakeTimers();
    provider = createFixtureDesktopProductProvider({ startOnline: true, stepDelayMs: 10_000 });
    provider.addDraftProject({ subscription: true });
    vi.spyOn(provider, "subscribe").mockImplementation(() => () => undefined);
    const refresh = vi.spyOn(provider, "refresh");
    root = await renderProduct(provider);

    await clickButton("Start session");
    const current = await provider.refresh();
    if (current.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    const stale = {
      status: "fresh" as const,
      snapshot: {
        ...current.snapshot,
        projects: current.snapshot.projects.filter((item) => item.project_id === "project-fixture-1"),
      },
    };
    const pendingPoll = deferred<Awaited<ReturnType<FixtureDesktopProductProvider["refresh"]>>>();
    refresh.mockImplementationOnce(() => pendingPoll.promise);

    await advance(1_005);
    const inFlightRefreshCount = refresh.mock.calls.length;
    await advance(3_000);
    expect(refresh).toHaveBeenCalledTimes(inFlightRefreshCount);

    const switcher = document.querySelector<HTMLSelectElement>("#project-switcher");
    if (!switcher) throw new Error("Project switcher was not found.");
    await act(async () => {
      switcher.value = "project-fixture-2";
      switcher.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await flush();
    expect(screenText()).toContain("Second research task");

    await act(async () => {
      pendingPoll.resolve(stale);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screenText()).toContain("Second research task");

    await flush();
    expect(refresh).toHaveBeenCalledTimes(inFlightRefreshCount + 1);
    const reconciledRefreshCount = refresh.mock.calls.length;
    await advance(5_000);
    expect(refresh).toHaveBeenCalledTimes(reconciledRefreshCount);
  });

  it("cancels run polling when the renderer unmounts", async () => {
    vi.useFakeTimers();
    provider = createFixtureDesktopProductProvider({ startOnline: true, stepDelayMs: 10_000 });
    vi.spyOn(provider, "subscribe").mockImplementation(() => () => undefined);
    const refresh = vi.spyOn(provider, "refresh");
    root = await renderProduct(provider);

    await clickButton("Start session");
    const current = await provider.refresh();
    const pendingPoll = deferred<Awaited<ReturnType<FixtureDesktopProductProvider["refresh"]>>>();
    refresh.mockImplementationOnce(() => pendingPoll.promise);
    await advance(1_005);
    const inFlightRefreshCount = refresh.mock.calls.length;

    await act(async () => root?.unmount());
    root = null;
    await act(async () => {
      pendingPoll.resolve(current);
      await Promise.resolve();
      await Promise.resolve();
    });
    await advance(5_000);

    expect(refresh).toHaveBeenCalledTimes(inFlightRefreshCount);
  });

  it("never publishes a superseded run-log request into the next run", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    const initial = await provider.refresh();
    if (initial.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    const previousRun = initial.snapshot.runs[0];
    if (!previousRun) throw new Error("Expected a completed fixture run.");
    const staleLogs = (await provider.getRunLogs(previousRun.id)).map((entry, index) => ({
      ...entry,
      message: index === 0 ? "STALE PREVIOUS RUN OUTPUT" : entry.message,
    }));
    const staleRequest = deferred<typeof staleLogs>();
    const loadLogs = vi.spyOn(provider, "getRunLogs");
    loadLogs.mockImplementationOnce(() => staleRequest.promise);
    root = await renderProduct(provider);

    expect(screenText()).not.toContain("STALE PREVIOUS RUN OUTPUT");
    await clickButton("Start session");
    expect(screenText()).toContain("Session admitted with an immutable project snapshot.");

    await act(async () => {
      staleRequest.resolve(staleLogs);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screenText()).not.toContain("STALE PREVIOUS RUN OUTPUT");
  });

  it("keeps opaque run and nullable attempt identities structurally distinct", () => {
    expect(sameSessionOutputIdentity(
      { runId: "run:segment", attemptId: "attempt" },
      { runId: "run", attemptId: "segment:attempt" },
    )).toBe(false);
    expect(sameSessionOutputIdentity(
      { runId: "run", attemptId: null },
      { runId: "run", attemptId: "no-attempt" },
    )).toBe(false);
  });

  it("hides resolved output synchronously when a colliding attempt identity becomes current", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    const initial = await provider.refresh();
    if (initial.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    const baseRun = initial.snapshot.runs[0];
    if (!baseRun) throw new Error("Expected a completed fixture run.");
    const baseLogs = await provider.getRunLogs(baseRun.id);
    const oldSnapshot = withRunOutputIdentity(initial.snapshot, "run-collision", null);
    const nextSnapshot = withRunOutputIdentity(initial.snapshot, "run-collision", "no-attempt");
    const oldLogs = relabelLogs(baseLogs, "run-collision", null, "OLD NO-ATTEMPT OUTPUT");
    const nextRequest = deferred<ReturnType<typeof relabelLogs>>();
    vi.spyOn(provider, "refresh")
      .mockResolvedValueOnce({ status: "fresh", snapshot: oldSnapshot })
      .mockResolvedValueOnce({ status: "fresh", snapshot: nextSnapshot });
    vi.spyOn(provider, "getRunLogs")
      .mockResolvedValueOnce(oldLogs)
      .mockImplementationOnce(() => nextRequest.promise);
    root = await renderProduct(provider);

    expect(screenText()).toContain("OLD NO-ATTEMPT OUTPUT");
    await act(async () => provider?.emitAuthoritativeRefresh());
    await flush();

    expect(screenText()).not.toContain("OLD NO-ATTEMPT OUTPUT");
    expect(document.querySelector('[aria-label="Refreshing session output"]')).not.toBeNull();

    await act(async () => {
      nextRequest.resolve(relabelLogs(baseLogs, "run-collision", "no-attempt", "CURRENT ATTEMPT OUTPUT"));
      await Promise.resolve();
    });
    expect(screenText()).toContain("CURRENT ATTEMPT OUTPUT");
  });

  it("never publishes an old colliding request after a rapid attempt switch", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    const initial = await provider.refresh();
    if (initial.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    const baseRun = initial.snapshot.runs[0];
    if (!baseRun) throw new Error("Expected a completed fixture run.");
    const baseLogs = await provider.getRunLogs(baseRun.id);
    const oldSnapshot = withRunOutputIdentity(initial.snapshot, "run-collision", null);
    const nextSnapshot = withRunOutputIdentity(initial.snapshot, "run-collision", "no-attempt");
    const oldRequest = deferred<ReturnType<typeof relabelLogs>>();
    vi.spyOn(provider, "refresh")
      .mockResolvedValueOnce({ status: "fresh", snapshot: oldSnapshot })
      .mockResolvedValueOnce({ status: "fresh", snapshot: nextSnapshot });
    vi.spyOn(provider, "getRunLogs")
      .mockImplementationOnce(() => oldRequest.promise)
      .mockResolvedValueOnce(relabelLogs(baseLogs, "run-collision", "no-attempt", "CURRENT ATTEMPT OUTPUT"));
    root = await renderProduct(provider);

    await act(async () => provider?.emitAuthoritativeRefresh());
    await flush();
    expect(screenText()).toContain("CURRENT ATTEMPT OUTPUT");

    await act(async () => {
      oldRequest.resolve(relabelLogs(baseLogs, "run-collision", null, "STALE NO-ATTEMPT OUTPUT"));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screenText()).toContain("CURRENT ATTEMPT OUTPUT");
    expect(screenText()).not.toContain("STALE NO-ATTEMPT OUTPUT");
  });

  it("gates sessions offline and completes first-time workspace setup", async () => {
    vi.useFakeTimers();
    provider = createFixtureDesktopProductProvider({ stepDelayMs: 20 });
    root = await renderProduct(provider);

    expect(screenText()).toContain("Add a remote workspace");
    expect(optionalButton("Create project")).toBeNull();
    expect(document.querySelector<HTMLButtonElement>('button[aria-label="Create project"]')?.disabled).toBe(true);
    expect(screenText()).not.toContain("Start session");
    await clickButton("Add workspace");
    setInput("Workspace name", "Lab server");
    setInput("Server address", "lab.example.test");
    setInput("User name", "researcher");
    expect(screenText()).toContain("SSH agent");
    expect(screenText()).not.toContain("Private key");
    expect(screenText()).not.toContain("Password");
    await clickButton("Save workspace");
    expect(screenText()).toContain("Connect the remote workspace");
    expect(document.querySelector<HTMLButtonElement>('button[aria-label="Create project"]')?.disabled).toBe(true);

    await clickButton("Connect");
    await advance(25);
    expect(screenText()).toContain("Confirm server identity");
    expect(document.querySelector<HTMLButtonElement>('button[aria-label="Create project"]')?.disabled).toBe(true);
    await clickButton("Trust and continue");
    expect(screenText()).toContain("Checking environment");
    await advance(25);
    expect(screenText()).toContain("Preparing OpenEvo");
    await advance(25);
    expect(screenText()).toContain("Online");
    expect(screenText()).toContain("Create a research project");
    expect(document.querySelector<HTMLButtonElement>('button[aria-label="Create project"]')?.disabled).toBe(false);

    await clickButton("Create project");
    setInput("Project name", "Catalyst study");
    setInput("Task title", "Compare catalyst candidates");
    setInput("Objective", "Rank candidates using reproducible evidence.");
    expect(labelledControl<HTMLInputElement>("Codex model", "input").value).toBe("gpt-5.5");
    expect(screenText()).not.toContain("Hugging Face model");
    await clickButton("Prepare evolution");
    await clickButton("Save and activate");
    expect(screenText()).toContain("Compare catalyst candidates");
    expect(button("Start session").title).toBe("Start a new research session");
    expect(button("Start session").disabled).toBe(false);
    const snapshot = await provider.refresh();
    if (snapshot.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    expect(snapshot.snapshot.projects[0]?.execution).toMatchObject({
      mode: "codex_subscription_transcript",
      codex_model: "gpt-5.5",
    });

    await clickButton("Evolution");
    expect(screenText()).not.toContain("Evolution is not configured");
  });

  it("shows the authoritative retired state after editing an active project", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    root = await renderProduct(provider);

    await clickAria("Project settings");
    setInput("Objective", "Require a fresh activation after this edit.");
    await clickButton("Save");
    await flush();

    const refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    expect(refreshed.snapshot.projects[0]).toMatchObject({
      state: "draft",
      remote: null,
    });
    expect(refreshed.snapshot.state.active_project).toBeNull();
    expect(refreshed.snapshot.state.core).toMatchObject({
      state: "offline",
      active_tunnel: false,
      failure: { code: "core_not_started" },
    });
    expect(screenText()).toContain("Remote workspace is offline");
    expect(screenText()).toContain("Activate this project");
    expect(button("Start session").disabled).toBe(true);
    expect(button("Start session").title).toContain("Connect this project's remote workspace");
  });

  it("keeps new-project setup open until remote evolution defaults are saved and activated", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    const before = await provider.refresh();
    if (before.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    expect(before.snapshot.capability).toMatchObject({ status: "ready", executionMode: "self-deployed" });
    root = await renderProduct(provider);

    await clickAria("Create project");
    expect(button("Subscription").getAttribute("aria-selected")).toBe("true");
    expect(document.querySelectorAll(".target-toggle")).toHaveLength(0);
    setInput("Objective", "Keep subscription defaults scoped to this new project.");
    await clickButton("Prepare evolution");

    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    expect(screenText()).toContain("Remote evolution methods are ready");
    expect(document.querySelectorAll(".target-toggle")).toHaveLength(3);
    expect(button("Save and activate").disabled).toBe(false);

    const firstToggle = document.querySelector<HTMLInputElement>(".target-toggle input[role='switch']");
    if (!firstToggle) throw new Error("Evolution target switch was not found.");
    expect(firstToggle.type).toBe("checkbox");
    expect(firstToggle.disabled).toBe(false);
    expect(firstToggle.tabIndex).toBe(0);
    expect(firstToggle.closest("label")).not.toBeNull();
    firstToggle.focus();
    expect(document.activeElement).toBe(firstToggle);
    const firstTrack = firstToggle.nextElementSibling;
    if (!(firstTrack instanceof HTMLElement) || !firstTrack.classList.contains("switch-track")) {
      throw new Error("Evolution target switch track was not adjacent to its checkbox.");
    }
    const initiallyChecked = firstToggle.checked;
    await act(async () => firstTrack.click());
    expect(firstToggle.checked).toBe(!initiallyChecked);
    await act(async () => firstTrack.click());
    expect(firstToggle.checked).toBe(initiallyChecked);

    const prepared = await provider.refresh();
    if (prepared.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    const draft = prepared.snapshot.projects.find((project) => project.task.objective === "Keep subscription defaults scoped to this new project.");
    expect(draft?.evolution.targets).toEqual({});
    expect(draft?.evolution_configuration_state).toBe("pending");

    for (const toggle of document.querySelectorAll<HTMLInputElement>(".target-toggle input[role='switch']")) {
      await act(async () => toggle.click());
    }

    await clickButton("Save and activate");
    expect(document.querySelector('[role="dialog"]')).toBeNull();

    const activated = await provider.refresh();
    if (activated.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    const created = activated.snapshot.projects.find((project) => project.task.objective === "Keep subscription defaults scoped to this new project.");
    expect(created?.execution).toMatchObject({ mode: "codex_subscription_transcript", codex_model: "gpt-5.5" });
    expect(created?.evolution_configuration_state).toBe("configured");
    expect(created?.evolution.targets).toMatchObject({
      text_memory: { enabled: false },
      skill_bundle: { enabled: false },
      agent_system: { enabled: false },
    });
  });

  it("does not reopen or block a configured project with zero evolution targets", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    provider.clearEvolutionSelections("configured");
    root = await renderProduct(provider);

    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(button("Start session").disabled).toBe(false);
    expect(screenText()).not.toContain("Remote evolution methods are ready");
    await clickButton("Evolution");
    expect(screenText()).toContain("Evolution is off");
    expect(screenText()).not.toContain("Evolution is not configured");
  });

  it("offers cancellation while a local connection operation is active", async () => {
    vi.useFakeTimers();
    provider = createFixtureDesktopProductProvider({ newUser: false, stepDelayMs: 10_000 });
    const before = await provider.refresh();
    if (before.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    const profile = before.snapshot.profiles[0];
    if (!profile) throw new Error("Expected a remote profile fixture.");
    await provider.connectProfile(profile.profile_id, {
      actionId: "connect-cancellable-operation-0001",
      streamEpoch: before.snapshot.stream.epoch,
      etag: profile.etag,
    });
    const cancelOperation = vi.spyOn(provider, "cancelOperation");
    root = await renderProduct(provider);

    expect(screenText()).toContain("Connecting securely");
    expect(button("Cancel operation").disabled).toBe(false);
    await clickButton("Cancel operation");

    expect(cancelOperation).toHaveBeenCalledTimes(1);
    expect(screenText()).toContain("Remote workspace is offline");
  });

  it("uses release capabilities for new-project defaults and keeps unavailable modes visible", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, releaseExecutionModes: true });
    root = await renderProduct(provider);

    await clickAria("Create project");
    expect(button("Subscription").getAttribute("aria-selected")).toBe("true");
    expect(button("Subscription").disabled).toBe(false);
    expect(button("Self-deployed").disabled).toBe(true);
    expect(button("Self-deployed").title).toContain("not available in this OpenEvo Desktop release");
    expect(screenText()).not.toContain("Hugging Face model");
  });

  it("keeps a saved unavailable mode visible, blocks mutations, and lets the user switch away", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, releaseExecutionModes: true });
    const startRun = vi.spyOn(provider, "startRun");
    root = await renderProduct(provider);

    expect(button("Start session").disabled).toBe(true);
    expect(button("Start session").title).toContain("not available in this OpenEvo Desktop release");
    await clickAria("Project settings");
    expect(button("Self-deployed").getAttribute("aria-selected")).toBe("true");
    expect(screenText()).toContain("Choose Subscription to save or run this project.");
    expect(button("Save").disabled).toBe(true);

    await clickButton("Subscription");
    expect(button("Subscription").getAttribute("aria-selected")).toBe("true");
    expect(button("Save").disabled).toBe(false);
    await clickButton("Save");
    expect(startRun).not.toHaveBeenCalled();
    const refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    expect(refreshed.snapshot.projects[0]?.execution.mode).toBe("codex_subscription_transcript");
  });

  it("blocks activation for a saved release-unavailable mode", async () => {
    provider = createFixtureDesktopProductProvider({ newUser: false, releaseExecutionModes: true });
    const activateProject = vi.spyOn(provider, "activateProject");
    root = await renderProduct(provider);

    expect(button("Activate project").disabled).toBe(true);
    expect(button("Activate project").title).toContain("not available in this OpenEvo Desktop release");
    expect(activateProject).not.toHaveBeenCalled();
  });

  it("does not reuse another project's same-mode capabilities after a new-project mode switch", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    root = await renderProduct(provider);

    expect(screenText()).toContain("Self-deployed");
    expect(screenText()).not.toContain("Managed model");
    await clickAria("Create project");
    await clickButton("Self-deployed");
    expect(button("Self-deployed").getAttribute("aria-selected")).toBe("true");
    expect(labelledControl<HTMLInputElement>("Hugging Face model", "input").value).toBe("Qwen/Qwen3-8B");
    expect(document.querySelectorAll(".target-toggle")).toHaveLength(0);
    setInput("Objective", "Keep self-deployed defaults scoped to this new project.");
    await clickButton("Prepare evolution");

    const refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    const created = refreshed.snapshot.projects.find((project) => project.task.objective === "Keep self-deployed defaults scoped to this new project.");
    expect(created?.execution).toMatchObject({ mode: "self-deployed", hf_model: "Qwen/Qwen3-8B" });
    expect(created?.evolution.targets).toEqual({});
    expect(document.querySelectorAll(".target-toggle")).toHaveLength(3);
  });

  it("resets a mounted project drawer before creating and never exposes unavailable snapshot sync", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    root = await renderProduct(provider);

    await clickAria("Project settings");
    await clickButton("Folder snapshot");
    await clickButton("Save");
    await clickAria("Project settings");
    expect(optionalButton("Sync snapshot")).toBeNull();
    setInput("Project name", "Stale project A draft");
    setInput("Hugging Face model", "example/stale-a-model");

    await clickAria("Create project");
    expect(screenText()).toContain("New project");
    expect(labelledControl<HTMLInputElement>("Project name", "input").value).toBe("New research project");
    expect(button("Subscription").getAttribute("aria-selected")).toBe("true");
    expect(labelledControl<HTMLInputElement>("Codex model", "input").value).toBe("gpt-5.5");
    expect(screenText()).not.toContain("example/stale-a-model");
    expect(document.querySelectorAll(".target-toggle")).toHaveLength(0);
    expect(optionalButton("Sync snapshot")).toBeNull();

    await act(async () => provider?.emitAuthoritativeRefresh());
    await flush();
    expect(labelledControl<HTMLInputElement>("Project name", "input").value).toBe("New research project");
    expect(labelledControl<HTMLInputElement>("Codex model", "input").value).toBe("gpt-5.5");
    expect(document.querySelectorAll(".target-toggle")).toHaveLength(0);
    expect(optionalButton("Sync snapshot")).toBeNull();

    setInput("Objective", "Create without project A state.");
    await clickButton("Prepare evolution");
    const refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    const created = refreshed.snapshot.projects.find((project) => project.task.objective === "Create without project A state.");
    expect(created).toMatchObject({
      name: "New research project",
      execution: { mode: "codex_subscription_transcript", codex_model: "gpt-5.5" },
      evolution: { targets: {} },
    });
  });

  it("exposes only SSH agent authentication in the release UI", async () => {
    provider = createFixtureDesktopProductProvider();
    root = await renderProduct(provider);

    await clickAria("Remote workspace settings");
    expect(screenText()).toContain("Authentication");
    expect(screenText()).toContain("SSH agent");
    expect(screenText()).not.toContain("Server password");
    expect(screenText()).not.toContain("Private key");
    expect(document.querySelector('input[type="password"]')).toBeNull();
    expect(document.querySelector(".credential-editor")).toBeNull();
  });

  it("commits the later revision and pins it in the next session", async () => {
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
    expect(screenText()).toContain("Revision 2");
    await advance(25);
    expect(screenText()).toContain("Revision 3");
    expect(screenText()).toContain("Latest session complete");

    await clickButton("Start session");
    expect(screenText()).toContain("Pinned context");
    expect(screenText()).toContain("Revision 3");
  });

  it("keeps Desktop project identity distinct from Core run and artifact identity", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    const refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("Fixture refresh was not fresh.");
    const project = refreshed.snapshot.projects[0];
    if (!project?.remote) throw new Error("Fixture project did not have a remote identity.");

    expect(project.project_id).not.toBe(project.remote.core_project_id);
    expect(refreshed.snapshot.runs.every((run) => run.project_id === project.remote?.core_project_id)).toBe(true);
    expect(refreshed.snapshot.artifacts.every((artifact) => artifact.project_id === project.remote?.core_project_id)).toBe(true);

    root = await renderProduct(provider);
    expect(screenText()).toContain("Latest session complete");
    await clickButton("Evolution");
    expect(document.querySelectorAll(".artifact-list-item")).toHaveLength(4);
  });

  it("keeps a terminal fixture run immutable when scheduled steps fire later", async () => {
    vi.useFakeTimers();
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true, stepDelayMs: 20 });
    const initial = await provider.refresh();
    if (initial.status !== "fresh") throw new Error("Fixture refresh was not fresh.");
    const project = initial.snapshot.projects[0];
    if (!project) throw new Error("Fixture project was not found.");
    const run = await provider.startRun({ projectId: project.project_id, etag: project.etag, streamEpoch: initial.snapshot.stream.epoch, actionId: "state-machine-start" });
    await provider.cancelRun(run.id, { etag: run.etag, streamEpoch: initial.snapshot.stream.epoch, actionId: "state-machine-cancel" });

    vi.advanceTimersByTime(25);
    const transitioned = await provider.refresh();
    if (transitioned.status !== "fresh") throw new Error("Fixture refresh was not fresh.");
    const current = transitioned.snapshot.runs.find((candidate) => candidate.id === run.id);

    expect(current?.status).toBe("cancelled");
    expect(current?.finished_at).not.toBeNull();
    expect(current?.current_error).toBeNull();
    expect(current?.current_attempt?.finished_at).not.toBeNull();
    expect(current?.current_attempt?.error).toBeNull();
    expect(current?.attempts.at(-1)?.finished_at).not.toBeNull();
    expect(current?.attempts.at(-1)?.error).toBeNull();
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

  it("shows rename and empty-file document changes without line hunks", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    provider.useDocumentLevelArtifactDiff();
    root = await renderProduct(provider);

    await clickButton("Evolution");
    await clickButton("Changes");
    await flush();

    expect(screenText()).toContain("notes.md to evidence.md");
    expect(screenText()).toContain("Renamed without content changes.");
    expect(screenText()).toContain("Empty document added.");
    expect(screenText()).toContain("Empty document removed.");
    expect(document.querySelectorAll(".diff-hunk")).toHaveLength(3);
  });

  it("refuses content and changes cross-wired to another selected artifact", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    provider.useCrossWiredArtifactPayloads();
    root = await renderProduct(provider);

    await clickButton("Evolution");
    await flush();
    expect(screenText()).toContain("Artifact content identity does not match the selected artifact.");
    expect(document.querySelector(".artifact-document")).toBeNull();
    await clickButton("Changes");
    await flush();
    expect(screenText()).toContain("Artifact change identity does not match the selected artifact.");
    expect(document.querySelector(".diff-hunk")).toBeNull();
  });

  it("refuses a diff whose previous artifact identity is unrelated to the selection", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    provider.useMismatchedArtifactDiffPreviousIdentity();
    root = await renderProduct(provider);

    await clickButton("Evolution");
    await clickButton("Changes");
    await flush();
    expect(screenText()).toContain("Artifact change history does not match the selected artifact.");
    expect(document.querySelector(".diff-hunk")).toBeNull();
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
    expect(screenText()).toContain("SSH agent");
    expect(screenText()).not.toContain("Stored securely");
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

  it("resumes an incomplete evolution setup after refresh and blocks an unsupported saved method", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    provider.clearEvolutionSelections("pending");
    root = await renderProduct(provider);

    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    expect(screenText()).toContain("Remote evolution methods are ready");
    expect(document.querySelectorAll(".target-toggle")).toHaveLength(3);
    expect(screenText()).toContain("Text memory");
    await clickAria("Close settings");
    await clickButton("Discard changes");

    provider.useUnsupportedSavedMethod();
    await flush();
    expect(button("Start session").disabled).toBe(true);
    expect(screenText()).toContain("unsupported for this project and mode");
    await clickAria("Project settings");
    expect(screenText()).toContain("removed_text_memory (no longer available)");
    const staleToggle = document.querySelector<HTMLInputElement>('.target-toggle[data-target-id="text_memory"] input[role="switch"]');
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

  it("preserves explicit evolution config across mode changes without reusing old-mode capabilities", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    provider.useEditableMethodSchemaWithPartialOverride();
    const before = await provider.refresh();
    if (before.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    const explicitTargets = before.snapshot.projects[0]?.evolution.targets;
    if (!explicitTargets) throw new Error("Expected an existing project with explicit evolution targets.");
    root = await renderProduct(provider);

    await clickAria("Project settings");
    await clickButton("Subscription");
    expect(screenText()).toContain("Capabilities are unavailable for this project and mode.");
    await clickButton("Save");

    expect(screenText()).not.toContain("Research configuration");
    expect(button("Start session").disabled).toBe(true);
    const refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    expect(refreshed.snapshot.projects[0]?.evolution.targets).toEqual(explicitTargets);
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
    const retryRequest = deferred<RunV1>();
    const retryRun = vi.fn((_runId: string, _intent: ProductResourceMutationIntent) => retryRequest.promise);
    Object.assign(provider, { retryRun });
    const startRun = vi.spyOn(provider, "startRun");
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
    await act(async () => {
      button("Retry session").click();
      await Promise.resolve();
    });

    expect(retryRun).toHaveBeenCalledWith("run-failed-model", expect.objectContaining({
      actionId: expect.any(String),
      streamEpoch: expect.any(Number),
      etag: expect.any(String),
    }));
    expect(startRun).not.toHaveBeenCalled();
    expect(button("Start session").disabled).toBe(true);
    expect(Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
      .filter((item) => item.textContent?.trim() === "Retry session")
      .every((item) => item.disabled)).toBe(true);

    await act(async () => {
      retryRequest.reject(new DesktopProductUserError("The failed session could not be retried."));
      await Promise.resolve();
      await Promise.resolve();
    });
    await flush();

    expect(screenText()).toContain("Action could not be completed");
    expect(screenText()).toContain("The failed session could not be retried.");
    expect(button("Retry session").disabled).toBe(false);

    await clickButton("Retry session");
    expect(retryRun).toHaveBeenCalledTimes(2);
    expect(retryRun.mock.calls[1]?.[1].actionId).toBe(retryRun.mock.calls[0]?.[1].actionId);
    expect(startRun).not.toHaveBeenCalled();
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
    expect(artifactNames).toEqual(["Parametric memory", "Skills", "Text memory", "Text memory", "Agent guidance"]);
    const artifactSummaries = Array.from(document.querySelectorAll(".artifact-list-item small"), (item) => item.textContent);
    expect(artifactSummaries).toEqual([
      "Selected adapter state for the next session.",
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

  it("fails closed on conflicting required and transition predecessor revision identities", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    provider.useRequiredRevisionIdentityConflict();
    root = await renderProduct(provider);
    await clickButton("Evolution");
    expect(screenText()).toContain("Revision relation is unknown");

    provider.useTransitionPredecessorIdentityConflict();
    provider.emitAuthoritativeRefresh();
    await flush();
    expect(screenText()).toContain("Revision relation is unknown");
    expect(document.querySelectorAll(".artifact-list-item")).toHaveLength(0);
  });

  it("requires complete revision identity for selected artifact membership", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    provider.useArtifactMembershipIdentityConflict();
    root = await renderProduct(provider);
    await clickButton("Evolution");

    expect(screenText()).toContain("No evolved artifacts yet");
    expect(document.querySelectorAll(".artifact-list-item")).toHaveLength(0);
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
    provider.restoreOnlineActiveProject();
    await flush();

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
      repairAction: "unsupported",
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
    vi.useFakeTimers();
    provider = createFixtureDesktopProductProvider({ newUser: true, stepDelayMs: 20 });
    const selectSource = vi.spyOn(provider, "selectProjectSource");
    const settleSource = vi.spyOn(provider, "settleProjectSource");
    root = await renderProduct(provider);

    await clickButton("Add workspace");
    setInput("Server address", "lab.example.test");
    setInput("User name", "researcher");
    provider.failNextProfileCreateWithUnknownError();
    await clickButton("Save workspace");
    provider.emitAuthoritativeRefresh();
    await flush();
    await clickButton("Save workspace");
    expect(provider.profileCreateActionIds()[0]).toBe(provider.profileCreateActionIds()[1]);
    expect(provider.profileUpdateActionIds()).toHaveLength(0);

    await clickButton("Connect");
    await advance(25);
    await clickButton("Trust and continue");
    await advance(50);

    await clickAria("Create project");
    setInput("Objective", "Keep one project create identity.");
    await clickButton("Folder snapshot");
    const pendingAction = selectSource.mock.calls[0]?.[0].actionId;
    provider.failNextProjectCreateWithUnknownError();
    await clickButton("Prepare evolution");
    expect(settleSource).toHaveBeenCalledWith(pendingAction, "discard");
    await clickButton("Prepare evolution");
    expect(provider.projectCreateActionIds()[0]).toBe(provider.projectCreateActionIds()[1]);
  });

  it.each([409, 412] as const)("retries activation after HTTP %s without creating another project", async (status) => {
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    root = await renderProduct(provider);

    await clickAria("Create project");
    setInput("Objective", `Activate the authoritative project after ${status}.`);
    provider.failNextProjectActivation(status);
    await clickButton("Prepare evolution");

    expect(provider.projectCreateActionIds()).toHaveLength(1);
    expect(provider.projectActivationActionIds()).toHaveLength(1);
    await clickButton("Prepare evolution");

    expect(provider.projectCreateActionIds()).toHaveLength(1);
    expect(provider.projectActivationActionIds()).toHaveLength(2);
    expect(provider.projectActivationActionIds()[0]).not.toBe(provider.projectActivationActionIds()[1]);
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    expect(screenText()).toContain("Remote evolution methods are ready");
  });

  it("uses a single-column bounded System layout at the 760px minimum window", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    root = await renderProduct(provider);
    await clickButton("System");

    const grid = document.querySelector(".system-grid");
    expect(grid).not.toBeNull();
    expect(grid?.children).toHaveLength(1);
    expect(Array.from(grid?.children ?? []).every((child) => child.classList.contains("product-panel"))).toBe(true);
  });

  it("adopts a profile created before its response was lost without replaying it as an update", async () => {
    provider = createFixtureDesktopProductProvider({ newUser: true });
    root = await renderProduct(provider);

    await clickButton("Add workspace");
    setInput("Server address", "lab.example.test");
    setInput("User name", "researcher");
    provider.loseNextProfileCreateResponseAfterCommit();
    await clickButton("Save workspace");
    await flush();

    expect(provider.profileCreateActionIds()).toHaveLength(1);
    expect(provider.profileUpdateActionIds()).toHaveLength(0);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(screenText()).toContain("Research server");
  });

  it("supports roving keyboard selection in project and artifact tabs", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    const selectSource = vi.spyOn(provider, "selectProjectSource");
    root = await renderProduct(provider);
    await clickAria("Project settings");

    const tablists = document.querySelectorAll('[role="tablist"]');
    expect(tablists.length).toBeGreaterThanOrEqual(2);
    expect(document.querySelector('[role="tab"][aria-selected="true"]')).not.toBeNull();

    const sourceTabs = document.querySelector<HTMLElement>('[role="tablist"][aria-label="Research source"]');
    const scratch = sourceTabs?.querySelector<HTMLButtonElement>('[role="tab"][aria-selected="true"]');
    if (!scratch) throw new Error("Selected research source tab was not found.");
    scratch.focus();
    await pressKey(scratch, "ArrowDown");
    expect(document.activeElement).toBe(scratch);
    await pressKey(scratch, "ArrowRight");
    const folder = sourceTabs?.querySelector<HTMLButtonElement>('[role="tab"]:not([aria-selected="true"])');
    expect(document.activeElement).toBe(folder);
    expect(sourceTabs?.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toContain("Scratch");
    expect(selectSource).not.toHaveBeenCalled();
    if (!folder) throw new Error("Folder source tab was not found.");
    await pressKey(folder, "Enter");
    expect(sourceTabs?.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toContain("Folder snapshot");
    expect(selectSource).toHaveBeenCalledTimes(1);

    const modelTabs = document.querySelector<HTMLElement>('[role="tablist"][aria-label="Model mode"]');
    const selectedModel = modelTabs?.querySelector<HTMLButtonElement>('[role="tab"][aria-selected="true"]');
    if (!selectedModel) throw new Error("Selected model tab was not found.");
    selectedModel.focus();
    await pressKey(selectedModel, "ArrowRight");
    const nextModel = document.activeElement;
    expect(modelTabs?.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toBe(selectedModel.textContent);
    if (!(nextModel instanceof HTMLElement)) throw new Error("Next model tab was not focused.");
    await pressKey(nextModel, "Enter");
    expect(modelTabs?.querySelector('[role="tab"][aria-selected="true"]')?.textContent).not.toBe(selectedModel.textContent);

    await clickAria("Close settings");
    await clickButton("Discard changes");
    await clickButton("Evolution");
    const artifactTabs = document.querySelector<HTMLElement>('[role="tablist"][aria-label="Artifact view"]');
    const content = artifactTabs?.querySelector<HTMLButtonElement>('[role="tab"][aria-selected="true"]');
    if (!content) throw new Error("Selected artifact tab was not found.");
    content.focus();
    await pressKey(content, "ArrowRight");
    const changes = document.activeElement;
    expect(artifactTabs?.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toContain("Content");
    if (!(changes instanceof HTMLElement)) throw new Error("Changes tab was not focused.");
    await pressKey(changes, "Enter");
    expect(artifactTabs?.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toContain("Changes");
  });

  it("keeps services read-only and does not render unavailable diagnostics or restart controls", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, degraded: true });
    root = await renderProduct(provider);

    await clickButton("System");
    expect(optionalButton("Run diagnostics")).toBeNull();
    expect(document.querySelector('button[aria-label="Run diagnostics"]')).toBeNull();
    expect(document.querySelector('button[aria-label^="Restart "]')).toBeNull();
    expect(screenText()).toContain("Needs attention");
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

  it("does not expose project A services or restart actions while project B is selected", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, degraded: true });
    provider.addDraftProject();
    root = await renderProduct(provider);

    const switcher = document.querySelector<HTMLSelectElement>("#project-switcher");
    if (!switcher) throw new Error("Project switcher was not found.");
    await act(async () => {
      switcher.value = "project-fixture-2";
      switcher.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await clickButton("System");

    expect(screenText()).toContain("Services are unavailable for this project.");
    expect(screenText()).not.toContain("Model service");
    expect(document.querySelector('button[aria-label="Restart Model service"]')).toBeNull();
  });

  it("hides stale services after tunnel loss and allows the active project to reactivate", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, degraded: true, seedCompletedRun: true });
    provider.useRunStateReviewScenario();
    provider.loseActiveCoreSession();
    const activateProject = vi.spyOn(provider, "activateProject");
    root = await renderProduct(provider);

    expect(screenText()).toContain("Activate this project");
    expect(screenText()).not.toContain("Preparing the selected model.");
    await clickButton("System");
    expect(screenText()).toContain("Services are unavailable for this project.");
    expect(document.querySelector('button[aria-label^="Restart "]')).toBeNull();
    expect(button("Activate project").disabled).toBe(true);
    expect(button("Activate project").title).toContain("Reconnect");
    expect(activateProject).not.toHaveBeenCalled();
  });

  it("loads project B when selection changes while project A's drawer remains open", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    provider.addDraftProject({ subscription: true });
    const before = await provider.refresh();
    if (before.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    const projectA = before.snapshot.projects.find((project) => project.project_id === "project-fixture-1");
    if (!projectA) throw new Error("Expected project A.");
    const updateProject = vi.spyOn(provider, "updateProject");
    root = await renderProduct(provider);

    await clickAria("Project settings");
    const projectADialog = document.querySelector('[role="dialog"]');
    setInput("Project name", "Stale project A draft");
    setInput("Hugging Face model", "example/stale-a-model");
    const staleRefresh = deferred<Awaited<ReturnType<FixtureDesktopProductProvider["refresh"]>>>();
    vi.spyOn(provider, "refresh").mockImplementationOnce(() => staleRefresh.promise);
    await act(async () => provider?.emitAuthoritativeRefresh());
    const switcher = document.querySelector<HTMLSelectElement>("#project-switcher");
    if (!switcher) throw new Error("Project switcher was not found.");
    await act(async () => {
      switcher.value = "project-fixture-2";
      switcher.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await flush();

    const projectBDialog = document.querySelector('[role="dialog"]');
    expect(projectBDialog).not.toBeNull();
    expect(projectBDialog).not.toBe(projectADialog);
    expect(labelledControl<HTMLInputElement>("Project name", "input").value).toBe("Second research project");
    expect(labelledControl<HTMLInputElement>("Task title", "input").value).toBe("Second research task");
    expect(button("Subscription").getAttribute("aria-selected")).toBe("true");
    expect(labelledControl<HTMLInputElement>("Codex model", "input").value).toBe("gpt-5.5");
    expect(screenText()).not.toContain("example/stale-a-model");
    expect(document.querySelectorAll(".target-toggle")).toHaveLength(0);
    expect(screenText()).toContain("Capabilities are unavailable for this project and mode.");

    await act(async () => staleRefresh.resolve(before));
    await flush();
    expect(labelledControl<HTMLInputElement>("Project name", "input").value).toBe("Second research project");
    expect(document.querySelectorAll(".target-toggle")).toHaveLength(0);

    setInput("Objective", "Updated project B objective.");
    expect(button("Save and activate").disabled).toBe(true);
    expect(updateProject).not.toHaveBeenCalled();
    const refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    expect(refreshed.snapshot.projects.find((item) => item.project_id === "project-fixture-1")?.name).toBe(projectA.name);
  });

  it("selects a native folder through an opaque snapshot reference without fake sync", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    const selectSource = vi.spyOn(provider, "selectProjectSource");
    const settleSource = vi.spyOn(provider, "settleProjectSource");
    root = await renderProduct(provider);

    await clickAria("Project settings");
    await clickButton("Folder snapshot");
    expect(selectSource).toHaveBeenCalledWith(expect.objectContaining({ projectId: "project-fixture-1" }));
    expect(screenText()).toContain("Selected research folder");
    expect(document.querySelector('input[type="file"]')).toBeNull();
    await clickButton("Save");
    expect(settleSource).toHaveBeenCalledWith(
      selectSource.mock.calls[0]?.[0].actionId,
      "adopt",
    );
    await clickAria("Project settings");
    expect(optionalButton("Sync snapshot")).toBeNull();
    await clickAria("Close settings");

    const refreshed = await provider.refresh();
    if (refreshed.status !== "fresh") throw new Error("Fixture refresh was not fresh.");
    expect(refreshed.snapshot.projects[0]?.source).toMatchObject({
      kind: "native_folder_snapshot",
      display_name: "Selected research folder",
      import_ref: { import_id: "source-fixture-1" },
    });
  });

  it("discards pending picker imports on close, reselection, and save failure", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    const selectSource = vi.spyOn(provider, "selectProjectSource");
    const settleSource = vi.spyOn(provider, "settleProjectSource");
    root = await renderProduct(provider);

    await clickAria("Project settings");
    await clickButton("Folder snapshot");
    const closedAction = selectSource.mock.calls[0]?.[0].actionId;
    await clickAria("Close settings");
    await clickButton("Discard changes");
    await flush();
    expect(settleSource).toHaveBeenCalledWith(closedAction, "discard");

    await clickAria("Project settings");
    await clickButton("Folder snapshot");
    const replacedAction = selectSource.mock.calls[1]?.[0].actionId;
    await clickButton("Folder snapshot");
    expect(settleSource).toHaveBeenCalledWith(replacedAction, "discard");

    provider.failNextProjectSave();
    const failedAction = selectSource.mock.calls[2]?.[0].actionId;
    await clickButton("Save");
    await flush();
    expect(settleSource).toHaveBeenCalledWith(failedAction, "discard");
    expect(screenText()).toContain("New workspace");
  });

  it("keeps the source and dirty state when the native picker is cancelled", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    const selectSource = vi.spyOn(provider, "selectProjectSource");
    selectSource.mockRejectedValueOnce({
      code: "workspace_selection_cancelled",
      message: "No research folder was selected.",
    });
    root = await renderProduct(provider);

    await clickAria("Project settings");
    setInput("Objective", "Keep this dirty draft after cancellation.");
    expect(button("Save").disabled).toBe(false);
    await clickButton("Folder snapshot");

    expect(screenText()).toContain("New workspace");
    expect(screenText()).toContain("Keep this dirty draft after cancellation.");
    expect(document.querySelector(".form-error")).toBeNull();
    expect(button("Save").disabled).toBe(false);

    selectSource.mockRejectedValueOnce({
      code: "workspace_selection_invalid",
      message: "workspace_selection_cancelled",
    });
    await clickButton("Folder snapshot");
    expect(document.querySelector(".form-error")?.textContent).toBe("The request could not be completed.");
  });

  it("allows only one native picker request while selection is in flight", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    const initialSource = await provider.selectProjectSource({
      kind: "native_folder_snapshot",
      projectId: "project-fixture-1",
      actionId: "source-test-seed",
      streamEpoch: 1,
    });
    root = await renderProduct(provider);
    await clickAria("Project settings");
    await clickButton("Folder snapshot");
    await clickButton("Save");
    await clickAria("Project settings");
    setInput("Objective", "Keep controls locked during selection.");

    const pending = deferred<ProjectSourceV1>();
    const selectSource = vi.spyOn(provider, "selectProjectSource").mockImplementation(() => pending.promise);
    const folderButton = button("Folder snapshot");
    await act(async () => {
      folderButton.click();
      folderButton.click();
      await Promise.resolve();
    });

    expect(selectSource).toHaveBeenCalledTimes(1);
    expect(button("Scratch").disabled).toBe(true);
    expect(folderButton.disabled).toBe(true);
    expect(button("Save").disabled).toBe(true);
    expect(button("Undo").disabled).toBe(true);

    await act(async () => pending.resolve({ ...initialSource, display_name: "Replacement research folder" }));
    await flush();
    expect(screenText()).toContain("Replacement research folder");
    expect(button("Save").disabled).toBe(false);
  });

  it("cancels an in-flight native ingest and closes without waiting for its original promise", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    const pending = deferred<ProjectSourceV1>();
    const selectSource = vi.spyOn(provider, "selectProjectSource").mockImplementation(() => pending.promise);
    const cancelSource = vi.spyOn(provider, "cancelProjectSource").mockImplementation(async () => {
      pending.reject({
        code: "workspace_selection_cancelled",
        message: "No research folder was selected.",
      });
    });
    root = await renderProduct(provider);

    await clickAria("Project settings");
    setInput("Objective", "Cancel this in-flight snapshot.");
    await clickElement(button("Folder snapshot"));
    const actionId = selectSource.mock.calls[0]?.[0].actionId;
    await clickAria("Close settings");
    expect(cancelSource).toHaveBeenCalledWith(actionId);
    await clickButton("Discard changes");
    await flush();

    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });

  it("ignores a picker completion after a close request and a later selection wins", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true, seedCompletedRun: true });
    const first = deferred<ProjectSourceV1>();
    const selected = await provider.selectProjectSource({
      kind: "native_folder_snapshot",
      projectId: "project-fixture-1",
      actionId: "source-test-stale",
      streamEpoch: 1,
    });
    const selectSource = vi.spyOn(provider, "selectProjectSource")
      .mockImplementationOnce(() => first.promise)
      .mockResolvedValueOnce({ ...selected, display_name: "Current research folder" });
    const settleSource = vi.spyOn(provider, "settleProjectSource");
    const cancelSource = vi.spyOn(provider, "cancelProjectSource");
    root = await renderProduct(provider);

    await clickAria("Project settings");
    setInput("Objective", "Keep editing after invalidating the first picker.");
    const folderButton = button("Folder snapshot");
    await clickElement(folderButton);
    await clickAria("Close settings");
    expect(screenText()).toContain("Discard unsaved changes?");
    expect(cancelSource).toHaveBeenCalledWith(selectSource.mock.calls[0]?.[0].actionId);
    await clickButton("Keep editing");

    expect(folderButton.disabled).toBe(true);
    await clickElement(folderButton);
    expect(selectSource).toHaveBeenCalledTimes(1);
    expect(screenText()).toContain("Keep editing after invalidating the first picker.");
    expect(screenText()).toContain("New workspace");
    expect(document.querySelector(".form-error")).toBeNull();

    await act(async () => first.resolve({ ...selected, display_name: "Stale research folder" }));
    await flush();
    expect(screenText()).not.toContain("Stale research folder");
    expect(settleSource).toHaveBeenCalledWith(
      selectSource.mock.calls[0]?.[0].actionId,
      "discard",
    );

    expect(folderButton.disabled).toBe(false);
    await clickButton("Folder snapshot");
    expect(screenText()).toContain("Current research folder");
    expect(selectSource).toHaveBeenCalledTimes(2);
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
    const alertDialog = document.querySelector<HTMLElement>('[role="alertdialog"]');
    expect(alertDialog?.contains(document.activeElement)).toBe(true);
    expect(document.activeElement?.textContent).toContain("Keep editing");
    const drawerContent = document.querySelector<HTMLElement>(".drawer-content");
    expect(drawerContent?.inert).toBe(true);
    const objective = labelledControl<HTMLTextAreaElement>("Objective", "textarea");
    await act(async () => objective.focus());
    expect(alertDialog?.contains(document.activeElement)).toBe(true);
    await clickButton("Keep editing");
    expect(drawerContent?.inert).toBe(false);

    const backdrop = document.querySelector<HTMLElement>(".drawer-backdrop");
    if (!backdrop) throw new Error("Drawer backdrop was not found.");
    let firstBackdropAccepted = true;
    await act(async () => {
      firstBackdropAccepted = backdrop.dispatchEvent(
        new MouseEvent("mousedown", { bubbles: true, cancelable: true }),
      );
    });
    expect(firstBackdropAccepted).toBe(false);
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    expect(screenText()).toContain("Discard unsaved changes?");
    const keepEditing = button("Keep editing");
    expect(document.activeElement).toBe(keepEditing);
    await act(async () => backdrop.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true })));
    expect(document.activeElement).toBe(keepEditing);
    await pressKey(keepEditing, "Escape");
    expect(screenText()).not.toContain("Discard unsaved changes?");

    await act(async () => backdrop.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })));
    expect(screenText()).toContain("Discard unsaved changes?");
    await clickButton("Keep editing");

    await clickAria("Close settings");
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    expect(screenText()).toContain("A retained draft objective.");
    await clickButton("Discard changes");
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement).toBe(opener);
  });

  it("keeps first-backdrop focus inside a dirty remote workspace confirmation", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    root = await renderProduct(provider);
    await clickAria("Remote workspace settings");
    setInput("Workspace name", "Unsaved remote workspace");
    const backdrop = document.querySelector<HTMLElement>(".drawer-backdrop");
    if (!backdrop) throw new Error("Remote workspace backdrop was not found.");

    let backdropAccepted = true;
    await act(async () => {
      backdropAccepted = backdrop.dispatchEvent(
        new MouseEvent("mousedown", { bubbles: true, cancelable: true }),
      );
    });

    expect(backdropAccepted).toBe(false);
    expect(screenText()).toContain("Discard unsaved changes?");
    expect(document.activeElement).toBe(button("Keep editing"));
    await clickButton("Discard changes");
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

async function pressKey(target: HTMLElement, key: string): Promise<void> {
  await act(async () => target.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true })));
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

function withRunOutputIdentity(
  snapshot: DesktopProductSnapshot,
  runId: string,
  attemptId: string | null,
): DesktopProductSnapshot {
  const source = snapshot.runs[0];
  if (!source) throw new Error("Expected a run fixture.");
  const sourceAttempt = source.current_attempt ?? source.attempts[0];
  if (attemptId !== null && !sourceAttempt) throw new Error("Expected an attempt fixture.");
  const attempt = attemptId === null
    ? null
    : { ...sourceAttempt, id: attemptId, run_id: runId };
  const run = {
    ...source,
    id: runId,
    current_attempt_id: attemptId,
    current_attempt: attempt,
    attempt_count: attempt === null ? 0 : 1,
    attempts: attempt === null ? [] : [attempt],
  };
  return {
    ...snapshot,
    runs: [run],
    timelines: { [runId]: snapshot.timelines[source.id] ?? [] },
  };
}

function relabelLogs(
  logs: Awaited<ReturnType<FixtureDesktopProductProvider["getRunLogs"]>>,
  runId: string,
  attemptId: string | null,
  message: string,
) {
  return logs.map((entry, index) => ({
    ...entry,
    id: `${runId}-log-${index}`,
    run_id: runId,
    attempt_id: attemptId,
    message: index === 0 ? message : entry.message,
  }));
}

function deferred<T>(): {
  readonly promise: Promise<T>;
  readonly resolve: (value: T) => void;
  readonly reject: (reason?: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}
