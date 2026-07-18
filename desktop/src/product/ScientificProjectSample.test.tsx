// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DesktopProductApp } from "./DesktopProductApp";
import {
  createFixtureDesktopProductProvider,
  type FixtureDesktopProductProvider,
} from "./fixtureProvider";
import {
  SAMPLE_SCIENTIFIC_PROJECT,
  SAMPLE_SCIENTIFIC_PROJECT_ID,
} from "./scientificProjectSampleData";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("built-in scientific project sample", () => {
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

  it("opens the read-only sample on first launch and browses its scientific progression", async () => {
    provider = createFixtureDesktopProductProvider({ newUser: true });
    const connectProfile = vi.spyOn(provider, "connectProfile");
    const createProject = vi.spyOn(provider, "createProject");
    const startRun = vi.spyOn(provider, "startRun");
    root = await renderProduct(provider);

    const switcher = document.querySelector<HTMLSelectElement>("#project-switcher");
    expect(switcher?.value).toBe(optionValueContaining(switcher, "[只读]"));
    expect(screenText()).toContain("内置示例 · 只读");
    expect(screenText()).toContain("酶动力学模型复核");
    expect(screenText()).toContain("结论未通过");
    expect(screenText()).toContain("验证通过");
    expect(screenText()).not.toContain("Start session");

    const selectedSessionTab = document.querySelector<HTMLButtonElement>('.sample-session-card[aria-selected="true"]');
    if (!selectedSessionTab) throw new Error("Selected sample session tab was not found.");
    selectedSessionTab.focus();
    await act(async () => selectedSessionTab.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true })));
    expect(screenText()).toContain("Vmax 与 Km 在重复拟合中稳定");

    await clickButtonContaining("建立 Michaelis-Menten 基线");
    expect(screenText()).toContain("线性化拟合对高浓度点产生系统性偏差");
    expect(screenText()).toContain("推理与工具调用安全摘要");
    expect(screenText()).toContain("不包含原始思维链、命令、主机位置、凭据或服务地址");

    await clickButton("Evolution");
    expect(screenText()).toContain("会话封存");
    expect(screenText()).toContain("下一 Evolution Revision");
    expect(screenText()).toContain("文本记忆");
    expect(screenText()).toContain("轨迹到技能");
    expect(screenText()).toContain("Agent 系统");

    await clickButton("可读产物");
    expect(screenText()).toContain("memory.md");
    expect(screenText()).toContain("底物浓度统一使用 mM");

    await clickButtonContaining("轨迹到技能");
    await clickButton("可读产物");
    expect(screenText()).toContain("SKILL.md");
    expect(screenText()).toContain("至少使用三个可复现起点");

    await clickButtonContaining("Agent 系统");
    await clickButton("可读产物");
    expect(screenText()).toContain("AGENTS.md");
    expect(screenText()).toContain("模型比较必须使用预先固定的留出观测");

    expect(connectProfile).not.toHaveBeenCalled();
    expect(createProject).not.toHaveBeenCalled();
    expect(startRun).not.toHaveBeenCalled();
  });

  it("keeps the sample discoverable without replacing an authoritative real project", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    root = await renderProduct(provider);
    const switcher = document.querySelector<HTMLSelectElement>("#project-switcher");
    if (!switcher) throw new Error("Project switcher was not found.");

    expect(switcher.value).toContain("project-fixture-1");
    expect(screenText()).toContain("Research brief");
    const realProjectValue = switcher.value;
    const sampleValue = optionValueContaining(switcher, "[只读]");

    await selectProject(switcher, sampleValue);
    expect(screenText()).toContain("Scientific project tour");
    expect(screenText()).not.toContain("Research brief");

    await selectProject(switcher, realProjectValue);
    expect(screenText()).toContain("Research brief");
    expect(screenText()).not.toContain("Scientific project tour");
  });

  it("keeps a real project distinct when its opaque id matches the sample data id", async () => {
    provider = createFixtureDesktopProductProvider({
      startOnline: true,
      projectId: SAMPLE_SCIENTIFIC_PROJECT_ID,
    });
    root = await renderProduct(provider);
    const switcher = document.querySelector<HTMLSelectElement>("#project-switcher");
    if (!switcher) throw new Error("Project switcher was not found.");

    const realProjectValue = switcher.value;
    const sampleValue = optionValueContaining(switcher, "[只读]");
    expect(realProjectValue).not.toBe(sampleValue);
    expect(screenText()).toContain("Research brief");

    await selectProject(switcher, sampleValue);
    expect(screenText()).toContain("Scientific project tour");

    await selectProject(switcher, realProjectValue);
    expect(screenText()).toContain("Research brief");
    expect(screenText()).not.toContain("Scientific project tour");
  });

  it("always creates a workspace from the sample without editing an existing profile", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    const createProfile = vi.spyOn(provider, "createProfile");
    const updateProfile = vi.spyOn(provider, "updateProfile");
    root = await renderProduct(provider);
    const switcher = document.querySelector<HTMLSelectElement>("#project-switcher");
    if (!switcher) throw new Error("Project switcher was not found.");

    await selectProject(switcher, optionValueContaining(switcher, "[只读]"));
    await clickButton("Add workspace");

    const host = inputForLabel("Server address");
    const user = inputForLabel("User name");
    expect(host.value).toBe("");
    expect(user.value).toBe("");
    await enterText(host, "new-research.example.org");
    await enterText(user, "scientist");
    await clickButton("Save workspace");

    await vi.waitFor(() => expect(createProfile).toHaveBeenCalledTimes(1));
    expect(updateProfile).not.toHaveBeenCalled();
    expect(switcher.selectedOptions[0]?.dataset.profileId).toBe("profile-fixture-2");
    expect(switcher.selectedOptions[0]?.textContent).toContain("Research server");
    expect(screenText()).toContain("Connect the remote workspace");
  });

  it("replays an uncertain create after the stream epoch advances without treating it as an update", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    const createProfile = vi.spyOn(provider, "createProfile");
    const updateProfile = vi.spyOn(provider, "updateProfile");
    root = await renderProduct(provider);
    const switcher = document.querySelector<HTMLSelectElement>("#project-switcher");
    if (!switcher) throw new Error("Project switcher was not found.");

    await selectProject(switcher, optionValueContaining(switcher, "[只读]"));
    await clickButton("Add workspace");
    await enterText(inputForLabel("Workspace name"), "Research GPU");
    await enterText(inputForLabel("Server address"), "gpu.example.test");
    await enterText(inputForLabel("User name"), "researcher");
    await enterText(inputForLabel("HTTP proxy"), "http://proxy.example.test:8080");
    await enterText(inputForLabel("Bypass proxy for"), "localhost");
    provider.advanceEpochOnNextRefresh();
    provider.failNextProfileCreateWithUnknownError();
    await clickButton("Save workspace");

    await vi.waitFor(() => expect(createProfile).toHaveBeenCalledTimes(1));
    expect(updateProfile).not.toHaveBeenCalled();
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    expect(switcher.value).toBe(optionValueContaining(switcher, "[只读]"));
    expect(screenText()).toContain("Action could not be completed");
    expect(screenText()).toContain("The request could not be completed.");

    await clickButton("Save workspace");
    await vi.waitFor(() => expect(document.querySelector('[role="dialog"]')).toBeNull());
    expect(createProfile).toHaveBeenCalledTimes(2);
    expect(provider.profileCreateActionIds()[1]).toBe(provider.profileCreateActionIds()[0]);
    expect(updateProfile).not.toHaveBeenCalled();
    expect(switcher.selectedOptions[0]?.dataset.profileId).toBe("profile-fixture-2");
  });

  it("waits for the exact returned profile to appear after a failed refresh", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    let publishSnapshot: (() => void) | null = null;
    vi.spyOn(provider, "subscribe").mockImplementation((listener) => {
      publishSnapshot = () => listener({ kind: "snapshot_changed" });
      return () => undefined;
    });
    root = await renderProduct(provider);
    const switcher = document.querySelector<HTMLSelectElement>("#project-switcher");
    if (!switcher) throw new Error("Project switcher was not found.");

    await selectProject(switcher, optionValueContaining(switcher, "[只读]"));
    await clickButton("Add workspace");
    await enterText(inputForLabel("Server address"), "new-research.example.org");
    await enterText(inputForLabel("User name"), "scientist");
    provider.failNextRefresh();
    await clickButton("Save workspace");

    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    expect(switcher.value).toBe(optionValueContaining(switcher, "[只读]"));
    expect(provider.profileCreateActionIds()).toHaveLength(1);

    await act(async () => publishSnapshot?.());
    await vi.waitFor(() => expect(document.querySelector('[role="dialog"]')).toBeNull());
    expect(provider.profileCreateActionIds()).toHaveLength(1);
    expect(switcher.selectedOptions[0]?.dataset.profileId).toBe("profile-fixture-2");
  });

  it("does not adopt a concurrently created matching profile after an unknown response", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    vi.spyOn(provider, "subscribe").mockImplementation(() => () => undefined);
    root = await renderProduct(provider);
    const switcher = document.querySelector<HTMLSelectElement>("#project-switcher");
    if (!switcher) throw new Error("Project switcher was not found.");
    await selectProject(switcher, optionValueContaining(switcher, "[只读]"));

    const before = await provider.refresh();
    if (before.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    const concurrentInput = {
      name: "Concurrent workspace",
      host: "concurrent.example.org",
      port: 22,
      user: "scientist",
      authentication_kind: "ssh_agent" as const,
      proxy: { http_url: null, https_url: null, no_proxy: [] },
    };
    await provider.createProfile(concurrentInput, {
      actionId: "external-profile-create",
      streamEpoch: before.snapshot.stream.epoch,
    });

    await clickButton("Add workspace");
    await enterText(inputForLabel("Workspace name"), concurrentInput.name);
    await enterText(inputForLabel("Server address"), concurrentInput.host);
    await enterText(inputForLabel("User name"), concurrentInput.user);
    provider.failNextProfileCreateWithUnknownError();
    await clickButton("Save workspace");

    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    expect(switcher.value).toBe(optionValueContaining(switcher, "[只读]"));
  });

  it("does not apply another profile's host-key review to a newly created workspace", async () => {
    provider = createFixtureDesktopProductProvider({ startOnline: true });
    const acceptHostKey = vi.spyOn(provider, "acceptHostKey");
    root = await renderProduct(provider);
    const switcher = document.querySelector<HTMLSelectElement>("#project-switcher");
    if (!switcher) throw new Error("Project switcher was not found.");

    await selectProject(switcher, optionValueContaining(switcher, "[只读]"));
    await clickButton("Add workspace");
    await enterText(inputForLabel("Server address"), "new-research.example.org");
    await enterText(inputForLabel("User name"), "scientist");
    await clickButton("Save workspace");
    await vi.waitFor(() => expect(switcher.selectedOptions[0]?.dataset.profileId).toBe("profile-fixture-2"));

    await act(async () => provider!.useForeignProfileConnectionState("host_key_review"));
    await vi.waitFor(() => expect(screenText()).toContain("Remote workspace is offline"));
    expect(screenText()).not.toContain("Confirm server identity");
    expect(buttonOrNull("Trust and continue")).toBeNull();
    expect(buttonOrNull("Connect")).not.toBeNull();
    expect(document.querySelector(".operation-cancel-bar")).toBeNull();
    expect(buttonOrNull("Cancel operation")).toBeNull();
    expect(acceptHostKey).not.toHaveBeenCalled();

    await clickButton("System");
    expect(document.querySelector(".connection-panel .state-pill")?.textContent).toBe("disconnected");
    expect(screenText()).toContain("Secure connectionNot connected");
    expect(screenText()).toContain("CompatibilityNot connected");
    expect(buttonOrNull("Reconnect")?.disabled).toBe(true);
    expect(acceptHostKey).not.toHaveBeenCalled();
  });

  it("does not let a stale operation id cancel a superseding workspace connection", async () => {
    vi.useFakeTimers();
    provider = createFixtureDesktopProductProvider({ newUser: true, stepDelayMs: 10 });
    const initial = await provider.refresh();
    if (initial.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    const profileA = await provider.createProfile({
      name: "Profile A",
      host: "profile-a.example.org",
      port: 22,
      user: "scientist",
      authentication_kind: "ssh_agent",
      proxy: { http_url: null, https_url: null, no_proxy: [] },
    }, {
      actionId: "create-profile-a",
      streamEpoch: initial.snapshot.stream.epoch,
    });
    const profileB = await provider.createProfile({
      name: "Profile B",
      host: "profile-b.example.org",
      port: 22,
      user: "scientist",
      authentication_kind: "ssh_agent",
      proxy: { http_url: null, https_url: null, no_proxy: [] },
    }, {
      actionId: "create-profile-b",
      streamEpoch: initial.snapshot.stream.epoch,
    });
    const operationA = await provider.connectProfile(profileA.profile_id, {
      actionId: "connect-profile-a",
      streamEpoch: initial.snapshot.stream.epoch,
      etag: profileA.etag,
    });
    const operationB = await provider.connectProfile(profileB.profile_id, {
      actionId: "connect-profile-b",
      streamEpoch: initial.snapshot.stream.epoch,
      etag: profileB.etag,
    });

    expect(operationA.operation_id).not.toBe(operationB.operation_id);
    await expect(provider.cancelOperation(operationA.operation_id, {
      actionId: "cancel-stale-profile-a",
      streamEpoch: initial.snapshot.stream.epoch,
      etag: operationA.etag,
    })).rejects.toThrow("no longer active");

    const current = await provider.refresh();
    if (current.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    expect(current.snapshot.activeOperation?.operation_id).toBe(operationB.operation_id);
    expect(current.snapshot.activeOperation?.resource.resource_id).toBe(profileB.profile_id);
  });

  it("does not carry profile-A project context into a connected profile-B workspace", async () => {
    vi.useFakeTimers();
    provider = createFixtureDesktopProductProvider({ startOnline: true, stepDelayMs: 10 });
    const initial = await provider.refresh();
    if (initial.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    const profileB = await provider.createProfile({
      name: "Profile B",
      host: "profile-b.example.org",
      port: 22,
      user: "scientist",
      authentication_kind: "ssh_agent",
      proxy: { http_url: null, https_url: null, no_proxy: [] },
    }, {
      actionId: "create-profile-b",
      streamEpoch: initial.snapshot.stream.epoch,
    });
    await provider.connectProfile(profileB.profile_id, {
      actionId: "connect-profile-b",
      streamEpoch: initial.snapshot.stream.epoch,
      etag: profileB.etag,
    });
    await vi.advanceTimersByTimeAsync(11);
    const reviewSnapshot = await provider.refresh();
    if (reviewSnapshot.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    const review = reviewSnapshot.snapshot.state.core.host_key_review;
    const observedProfileB = reviewSnapshot.snapshot.profiles.find(
      (candidate) => candidate.profile_id === profileB.profile_id,
    );
    if (!review || !observedProfileB) throw new Error("Expected profile B host-key review.");
    await provider.acceptHostKey(profileB.profile_id, review, {
      actionId: "accept-profile-b",
      streamEpoch: reviewSnapshot.snapshot.stream.epoch,
      etag: observedProfileB.etag,
    });
    await vi.runAllTimersAsync();

    const connected = await provider.refresh();
    if (connected.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    expect(connected.snapshot.state.core.profile_id).toBe(profileB.profile_id);
    expect(connected.snapshot.state.core.state).toBe("online");
    expect(connected.snapshot.state.active_project).toBeNull();
    expect(connected.snapshot.capability).toBeNull();
    expect(connected.snapshot.validation).toBeNull();
    expect(connected.snapshot.services).toEqual([]);
    expect(connected.snapshot.projects[0]?.profile_id).not.toBe(profileB.profile_id);
  });

  it("keeps a cancelled profile-B connection flow terminal after timers run", async () => {
    vi.useFakeTimers();
    provider = createFixtureDesktopProductProvider({ newUser: true, stepDelayMs: 10 });
    const initial = await provider.refresh();
    if (initial.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    const profile = await provider.createProfile({
      name: "Profile B",
      host: "profile-b.example.org",
      port: 22,
      user: "scientist",
      authentication_kind: "ssh_agent",
      proxy: { http_url: null, https_url: null, no_proxy: [] },
    }, {
      actionId: "create-profile-b",
      streamEpoch: initial.snapshot.stream.epoch,
    });
    const afterCreate = await provider.refresh();
    if (afterCreate.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    await provider.connectProfile(profile.profile_id, {
      actionId: "connect-profile-b",
      streamEpoch: afterCreate.snapshot.stream.epoch,
      etag: profile.etag,
    });

    await vi.advanceTimersByTimeAsync(11);
    const reviewSnapshot = await provider.refresh();
    if (reviewSnapshot.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    const review = reviewSnapshot.snapshot.state.core.host_key_review;
    const reviewedProfile = reviewSnapshot.snapshot.profiles.find(
      (candidate) => candidate.profile_id === profile.profile_id,
    );
    if (!review || !reviewedProfile) throw new Error("Expected profile B host-key review.");
    expect(reviewSnapshot.snapshot.state.pending_operation_ids).toEqual([
      reviewSnapshot.snapshot.activeOperation?.operation_id,
    ]);
    const acceptOperation = await provider.acceptHostKey(profile.profile_id, review, {
      actionId: "accept-profile-b",
      streamEpoch: reviewSnapshot.snapshot.stream.epoch,
      etag: reviewedProfile.etag,
    });
    await vi.advanceTimersByTimeAsync(11);
    const bootstrapSnapshot = await provider.refresh();
    if (bootstrapSnapshot.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    expect(bootstrapSnapshot.snapshot.activeOperation).toMatchObject({
      operation_id: acceptOperation.operation_id,
      operation_kind: "host_key_accept",
      resource: { resource_type: "profile", resource_id: profile.profile_id },
    });
    expect(bootstrapSnapshot.snapshot.state.pending_operation_ids).toEqual([
      acceptOperation.operation_id,
    ]);
    await provider.cancelOperation(acceptOperation.operation_id, {
      actionId: "cancel-profile-b",
      streamEpoch: bootstrapSnapshot.snapshot.stream.epoch,
      etag: acceptOperation.etag,
    });

    await vi.runAllTimersAsync();
    const terminal = await provider.refresh();
    if (terminal.status !== "fresh") throw new Error("Expected a fresh fixture snapshot.");
    expect(terminal.snapshot.state.core.profile_id).toBe(profile.profile_id);
    expect(terminal.snapshot.state.core.state).toBe("disconnected");
    expect(terminal.snapshot.activeOperation).toBeNull();
    expect(terminal.snapshot.profiles.find(
      (candidate) => candidate.profile_id === profile.profile_id,
    )?.connection_state).toBe("disconnected");
  });

  it("uses a closed static display model with contiguous cross-session revisions", () => {
    const project = SAMPLE_SCIENTIFIC_PROJECT;
    expect(project.sessions).toHaveLength(3);
    expect(project.evolutionTargets.map((target) => target.id)).toEqual([
      "text_memory",
      "skill_bundle",
      "agent_system",
    ]);

    project.sessions.forEach((session, index) => {
      expect(session.sequence).toBe(index + 1);
      expect(session.pinnedProjectHeadGeneration).toBe(index);
      expect(session.successorProjectHeadGeneration).toBe(index + 1);
      expect(session.pinnedEvolutionRevision).toBe(`ER-${index}`);
      expect(session.successorEvolutionRevision).toBe(`ER-${index + 1}`);
      expect(session.timeline.length).toBeGreaterThanOrEqual(4);
      expect(session.trace.some((entry) => entry.kind === "reasoning_summary")).toBe(true);
      expect(session.trace.some((entry) => entry.kind === "tool_call")).toBe(true);
      expect(session.trace.some((entry) => entry.kind === "tool_result")).toBe(true);
    });
    project.evolutionTargets.forEach((target) => {
      expect(target.steps.map((step) => step.evolutionRevision)).toEqual([
        "ER-1",
        "ER-2",
        "ER-3",
      ]);
      expect(target.artifact.content.length).toBeGreaterThan(100);
    });

    const serialized = JSON.stringify(project).toLowerCase();
    for (const forbidden of [
      "benchmark",
      "file://",
      "/home/",
      "/users/",
      "http://",
      "https://",
      "core_url",
      "host_path",
      "secret_ref",
    ]) {
      expect(serialized).not.toContain(forbidden);
    }
  });
});

async function renderProduct(provider: FixtureDesktopProductProvider): Promise<Root> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const rendered = createRoot(container);
  await act(async () => {
    rendered.render(<DesktopProductApp provider={provider} />);
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
  return rendered;
}

async function clickButton(label: string): Promise<void> {
  const match = buttonOrNull(label);
  if (!match) throw new Error(`Button not found: ${label}`);
  await act(async () => match.click());
}

function buttonOrNull(label: string): HTMLButtonElement | null {
  return Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
    .find((candidate) => candidate.textContent?.trim() === label) ?? null;
}

async function clickButtonContaining(label: string): Promise<void> {
  const match = Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
    .find((candidate) => candidate.textContent?.includes(label));
  if (!match) throw new Error(`Button not found: ${label}`);
  await act(async () => match.click());
}

async function selectProject(switcher: HTMLSelectElement, projectId: string): Promise<void> {
  await act(async () => {
    switcher.value = projectId;
    switcher.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

function screenText(): string {
  return document.body.textContent ?? "";
}

function optionValueContaining(
  switcher: HTMLSelectElement | null,
  label: string,
): string {
  if (!switcher) throw new Error("Project switcher was not found.");
  const option = Array.from(switcher.options)
    .find((candidate) => candidate.textContent?.includes(label));
  if (!option) throw new Error(`Project option not found: ${label}`);
  return option.value;
}

function inputForLabel(label: string): HTMLInputElement {
  const element = Array.from(document.querySelectorAll<HTMLLabelElement>("label"))
    .find((candidate) => candidate.textContent?.includes(label))
    ?.querySelector("input");
  if (!element) throw new Error(`Input not found: ${label}`);
  return element;
}

async function enterText(input: HTMLInputElement, value: string): Promise<void> {
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )?.set;
    if (!setter) throw new Error("Native input value setter is unavailable.");
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
