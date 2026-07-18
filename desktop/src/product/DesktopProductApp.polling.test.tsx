// @vitest-environment happy-dom

import { StrictMode, act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DesktopApiClientV1, FetchLike } from "../api/v1/client";
import { CONTRACT_FIXTURE_V1 } from "../api/v1/fixtures";
import {
  desktopStateV1Schema,
  localOperationV1Schema,
  logEntryV1Schema,
  projectCapabilitiesV1Schema,
  projectValidationV1Schema,
  projectV1Schema,
  remoteProfileV1Schema,
  runV1Schema,
  serviceV1Schema,
  timelineEntryV1Schema,
  type DesktopStateV1,
  type RunV1,
} from "../api/v1/schemas";
import { DesktopProductApp } from "./DesktopProductApp";
import { LocalApiDesktopProductProvider } from "./localApiProvider";
import type { DesktopProductProvider, ProductRefreshResult } from "./provider";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const C = "c".repeat(64);
const ETAG_C = `"${C}"`;
const NOW = "2026-07-15T03:00:00Z";

describe("DesktopProductApp production run polling", () => {
  let root: Root | null = null;

  beforeEach(() => {
    document.body.innerHTML = "";
    vi.useFakeTimers();
  });

  afterEach(async () => {
    if (root) {
      await act(async () => root?.unmount());
      root = null;
    }
    vi.useRealTimers();
    document.body.innerHTML = "";
  });

  it("keeps a preexisting poll separate from the real cancel invalidation and mutation refresh", async () => {
    const harness = productionHarness();
    root = await renderProduct(harness.provider);
    expect(harness.activeEventStreams()).toBe(1);

    const pendingPoll = harness.deferNextState();
    await advance(1_005);
    expect(harness.refreshesInFlight()).toBe(1);

    await act(async () => {
      button("Cancel session").click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(harness.client.cancelRun).toHaveBeenCalledTimes(1);
    const mutationRefresh = harness.deferNextState();

    await act(async () => {
      pendingPoll.resolve(harness.state);
      await settlePromises();
    });
    expect(harness.refreshesInFlight()).toBe(1);

    await act(async () => {
      mutationRefresh.resolve(harness.state);
      await settlePromises();
    });

    expect(harness.maxRefreshesInFlight()).toBe(1);
    expect(screenText()).toContain("Latest session cancelled");
    expect(screenText()).not.toContain("Action could not be completed");
    const terminalRefreshCount = harness.refreshCount();
    await advance(3_000);
    expect(harness.refreshCount()).toBe(terminalRefreshCount);
  });

  it("publishes and resolves a mutation refresh within one batch during a continuous SSE storm", async () => {
    const harness = productionHarness();
    root = await renderProduct(harness.provider);
    const invalidationRefresh = harness.deferNextState();

    await act(async () => {
      button("Cancel session").click();
      await settlePromises();
    });
    expect(harness.client.cancelRun).toHaveBeenCalledTimes(1);
    const mutationRefresh = harness.deferNextState();

    await act(async () => {
      invalidationRefresh.resolve(harness.state);
      await settlePromises();
    });
    expect(harness.refreshesInFlight()).toBe(1);

    await harness.emitEvents(32);
    const firstStormTail = harness.deferNextState();
    await act(async () => {
      mutationRefresh.resolve(harness.state);
      await settlePromises();
    });

    expect(screenText()).toContain("Latest session cancelled");
    expect(screenText()).not.toContain("Action could not be completed");
    expect(harness.refreshesInFlight()).toBe(1);
    expect(harness.maxRefreshesInFlight()).toBe(1);

    await harness.emitEvents(32);
    const secondStormTail = harness.deferNextState();
    await act(async () => {
      firstStormTail.resolve(harness.state);
      await settlePromises();
    });
    expect(harness.refreshesInFlight()).toBe(1);

    await act(async () => {
      secondStormTail.resolve(harness.state);
      await settlePromises();
    });
    expect(harness.refreshesInFlight()).toBe(0);
    expect(harness.refreshCount()).toBe(5);
  });

  it("uses the LocalApi cancelRun invalidation without a synthetic renderer signal", async () => {
    const harness = productionHarness();
    root = await renderProduct(harness.provider);
    const refreshBeforeCancel = harness.refreshCount();
    const invalidationRefresh = harness.deferNextState();

    await act(async () => {
      button("Cancel session").click();
      await settlePromises();
    });
    expect(harness.client.cancelRun).toHaveBeenCalledTimes(1);
    expect(harness.refreshCount()).toBe(refreshBeforeCancel + 1);

    await act(async () => {
      invalidationRefresh.resolve(harness.state);
      await settlePromises();
    });
    expect(harness.refreshCount()).toBe(refreshBeforeCancel + 2);
    expect(screenText()).toContain("Latest session cancelled");
  });

  it.each(["resolve", "reject"] as const)(
    "reconciles a production provider epoch after a switched-project poll completes with %s",
    async (outcome) => {
      const harness = productionHarness({ includeSecondProject: true });
      root = await renderProduct(harness.provider);

      const pendingPoll = harness.deferNextState();
      await advance(1_005);
      await switchProject("project-fixture-2");
      expect(screenText()).toContain("Second production task");

      await act(async () => {
        if (outcome === "resolve") pendingPoll.resolve(harness.state);
        else pendingPoll.reject(new Error("late production refresh failed"));
        await settlePromises();
      });

      expect(screenText()).toContain("Second production task");
      expect(harness.maxRefreshesInFlight()).toBe(1);
      await clickButton("Activate project");
      expect(harness.client.activateProject).toHaveBeenCalledWith(
        "project-fixture-2",
        expect.objectContaining({ ifMatch: ETAG_C }),
      );
      expect(screenText()).not.toContain("Refresh this view before trying again");
    },
  );

  it.each(["resolve", "reject"] as const)(
    "does not restart an in-flight production poll after StrictMode unmount when it %s",
    async (outcome) => {
      const harness = productionHarness();
      root = await renderProduct(harness.provider, true);
      expect(harness.subscribeCount()).toBeGreaterThanOrEqual(2);
      expect(harness.eventStreamCount()).toBe(1);
      expect(harness.activeEventStreams()).toBe(1);
      const pendingPoll = harness.deferNextState();
      await advance(1_005);
      const inFlightRefreshCount = harness.refreshCount();
      expect(harness.refreshesInFlight()).toBe(1);

      await act(async () => root?.unmount());
      root = null;
      await act(async () => {
        if (outcome === "resolve") pendingPoll.resolve(harness.state);
        else pendingPoll.reject(new Error("unmounted production refresh failed"));
        await settlePromises();
      });
      await advance(5_000);

      expect(harness.maxRefreshesInFlight()).toBe(1);
      expect(harness.refreshCount()).toBe(inFlightRefreshCount);
      expect(harness.activeEventStreams()).toBe(0);
      expect(harness.abortedEventStreams()).toBe(harness.eventStreamCount());
    },
  );
});

function productionHarness(options: { readonly includeSecondProject?: boolean } = {}) {
  const profile = remoteProfileV1Schema.parse(CONTRACT_FIXTURE_V1.profile);
  const project = projectV1Schema.parse(CONTRACT_FIXTURE_V1.project);
  const secondProject = projectV1Schema.parse({
    ...CONTRACT_FIXTURE_V1.project,
    project_id: "project-fixture-2",
    name: "Second production project",
    task: { title: "Second production task", objective: "Exercise production refresh ownership." },
    execution: {
      mode: "codex_subscription_transcript",
      capture_mode: "transcript",
      token_level_metrics_available: false,
      codex_model: "gpt-5.5",
      hf_model: null,
    },
    state: "draft",
    remote: null,
    etag: ETAG_C,
  });
  const projects = options.includeSecondProject ? [project, secondProject] : [project];
  const state = desktopStateV1Schema.parse({ ...CONTRACT_FIXTURE_V1.state, pending_operation_ids: [] });
  let currentRun = runV1Schema.parse(CONTRACT_FIXTURE_V1.run);
  const stateResponses: Array<Deferred<DesktopStateV1>> = [];
  const eventStreams: ControllableEventStream[] = [];
  let eventSequence = 0;
  let abortedEventStreams = 0;

  const client = {
    state: vi.fn(() => stateResponses.shift()?.promise ?? Promise.resolve(state)),
    listProfiles: vi.fn().mockResolvedValue(page([profile])),
    listProjects: vi.fn().mockResolvedValue(page(projects)),
    listRuns: vi.fn(() => Promise.resolve(page([runSummary(currentRun)]))),
    getRun: vi.fn(() => Promise.resolve(currentRun)),
    runTimeline: vi.fn().mockResolvedValue(page([timelineEntryV1Schema.parse(CONTRACT_FIXTURE_V1.timeline)])),
    runLogs: vi.fn().mockResolvedValue(page([logEntryV1Schema.parse(CONTRACT_FIXTURE_V1.log)])),
    runArtifacts: vi.fn().mockResolvedValue(page([])),
    listServices: vi.fn().mockResolvedValue(page([serviceV1Schema.parse(CONTRACT_FIXTURE_V1.service)])),
    projectCapabilities: vi.fn().mockResolvedValue(projectCapabilitiesV1Schema.parse(CONTRACT_FIXTURE_V1.capabilities)),
    validateProject: vi.fn().mockResolvedValue(projectValidationV1Schema.parse(CONTRACT_FIXTURE_V1.validation)),
    cancelRun: vi.fn(async () => {
      currentRun = cancelledRun(currentRun);
      return currentRun;
    }),
    activateProject: vi.fn(async (projectId: string) => localOperationV1Schema.parse({
      ...CONTRACT_FIXTURE_V1.operation,
      operation_id: `activate-${projectId}`,
      operation_kind: "project_activate",
      resource: { resource_type: "project", resource_id: projectId },
    })),
    eventStreamRequest: vi.fn().mockResolvedValue({ url: "http://127.0.0.1/events", headers: {} }),
  } as unknown as DesktopApiClientV1 & Record<string, ReturnType<typeof vi.fn>>;
  const fetch = vi.fn<FetchLike>(async (_input, init) => {
    const signal = init?.signal;
    if (!signal) throw new Error("production SSE request omitted its abort signal");
    if (signal.aborted) throw new DOMException("Aborted", "AbortError");
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const observed: ControllableEventStream = { signal, enqueue: () => undefined, aborted: false };
    const stream = new ReadableStream<Uint8Array>({
      start(nextController) {
        controller = nextController;
        observed.enqueue = (value) => controller.enqueue(new TextEncoder().encode(value));
        signal.addEventListener("abort", () => {
          if (observed.aborted) return;
          observed.aborted = true;
          abortedEventStreams += 1;
          controller.error(new DOMException("Aborted", "AbortError"));
        }, { once: true });
      },
    });
    eventStreams.push(observed);
    return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
  });
  const provider = new LocalApiDesktopProductProvider({
    client,
    native: {
      selectProjectSource: vi.fn(),
      cancelProjectSource: vi.fn(),
      settleProjectSource: vi.fn(),
    },
    fetch,
    reconnectDelaysMs: [],
    retryRecoveryStore: {
      read: () => null,
      write: () => undefined,
    },
  });
  const subscribe = vi.spyOn(provider, "subscribe");
  const originalRefresh = provider.refresh.bind(provider);
  let refreshCount = 0;
  let refreshesInFlight = 0;
  let maxRefreshesInFlight = 0;
  vi.spyOn(provider, "refresh").mockImplementation(async (): Promise<ProductRefreshResult> => {
    refreshCount += 1;
    refreshesInFlight += 1;
    maxRefreshesInFlight = Math.max(maxRefreshesInFlight, refreshesInFlight);
    try {
      return await originalRefresh();
    } finally {
      refreshesInFlight -= 1;
    }
  });

  return {
    provider,
    client,
    state,
    deferNextState: () => {
      const pending = deferred<DesktopStateV1>();
      stateResponses.push(pending);
      return pending;
    },
    emitEvents: async (count: number) => {
      let active: ControllableEventStream | undefined;
      for (let index = eventStreams.length - 1; index >= 0; index -= 1) {
        if (!eventStreams[index]!.aborted) {
          active = eventStreams[index];
          break;
        }
      }
      if (!active) throw new Error("production SSE stream is not active");
      let frames = "";
      for (let index = 0; index < count; index += 1) {
        eventSequence += 1;
        frames += eventFrame(eventSequence);
      }
      await act(async () => {
        active.enqueue(frames);
        await settlePromises();
      });
    },
    refreshCount: () => refreshCount,
    refreshesInFlight: () => refreshesInFlight,
    maxRefreshesInFlight: () => maxRefreshesInFlight,
    eventStreamCount: () => eventStreams.length,
    activeEventStreams: () => eventStreams.filter((stream) => !stream.aborted).length,
    abortedEventStreams: () => abortedEventStreams,
    subscribeCount: () => subscribe.mock.calls.length,
  };
}

function eventFrame(sequence: number): string {
  const eventId = `event-production-${sequence}`;
  return `id: ${eventId}\nevent: desktop.v1.resource.changed\ndata: ${JSON.stringify({
    schema_version: "1",
    event_id: eventId,
    event_name: "desktop.v1.resource.changed",
    occurred_at: NOW,
    sequence,
    data: {
      kind: "resource_changed",
      authority: "core",
      resource: { resource_type: "run", resource_id: CONTRACT_FIXTURE_V1.run.id },
      change: "updated",
      change_id: `change-production-${sequence}`,
      resource_etag: ETAG_C,
      content_sha256: null,
    },
  })}\n\n`;
}

function cancelledRun(run: RunV1): RunV1 {
  const attempt = run.current_attempt ? {
    ...run.current_attempt,
    status: "cancelled" as const,
    finished_at: NOW,
  } : null;
  return runV1Schema.parse({
    ...run,
    status: "cancelled",
    current_attempt: attempt,
    attempts: attempt ? [attempt] : [],
    finished_at: NOW,
    updated_at: NOW,
    etag: ETAG_C,
  });
}

function runSummary(run: RunV1) {
  const { attempts: _attempts, ...summary } = run;
  return summary;
}

function page<T>(items: readonly T[]) {
  return { schema_version: "1" as const, items: [...items], next_cursor: null, has_more: false };
}

async function renderProduct(provider: DesktopProductProvider, strict = false): Promise<Root> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const rendered = createRoot(container);
  await act(async () => {
    rendered.render(strict
      ? <StrictMode><DesktopProductApp provider={provider} /></StrictMode>
      : <DesktopProductApp provider={provider} />);
    await settlePromises();
  });
  return rendered;
}

async function switchProject(projectId: string): Promise<void> {
  const switcher = document.querySelector<HTMLSelectElement>("#project-switcher");
  if (!switcher) throw new Error("Project switcher was not found.");
  await act(async () => {
    switcher.value = projectOptionValue(switcher, projectId);
    switcher.dispatchEvent(new Event("change", { bubbles: true }));
    await settlePromises();
  });
}

function projectOptionValue(switcher: HTMLSelectElement, projectId: string): string {
  const option = Array.from(switcher.options).find(
    (candidate) => candidate.dataset.projectId === projectId,
  );
  if (!option) throw new Error(`Project option ${projectId} was not found.`);
  return option.value;
}

async function clickButton(label: string): Promise<void> {
  await act(async () => {
    button(label).click();
    await settlePromises();
  });
}

async function advance(milliseconds: number): Promise<void> {
  await act(async () => {
    vi.advanceTimersByTime(milliseconds);
    await settlePromises();
  });
}

async function settlePromises(): Promise<void> {
  for (let index = 0; index < 20; index += 1) await Promise.resolve();
}

function button(label: string): HTMLButtonElement {
  const target = [...document.querySelectorAll("button")].find((candidate) => candidate.textContent?.trim().includes(label));
  if (!(target instanceof HTMLButtonElement)) throw new Error(`Button ${label} was not found.`);
  return target;
}

function screenText(): string {
  return document.body.textContent ?? "";
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

type Deferred<T> = {
  readonly promise: Promise<T>;
  readonly resolve: (value: T) => void;
  readonly reject: (reason?: unknown) => void;
};

type ControllableEventStream = {
  readonly signal: AbortSignal;
  enqueue: (value: string) => void;
  aborted: boolean;
};
