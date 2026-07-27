// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { LifecycleOperationV2 } from "../api/v2/schemas";
import type { LifecycleOperationStateV2 } from "./lifecycleOperationsV2";
import {
  LifecycleOperationPanelV2,
  lifecycleOperationPanelModelV2,
} from "./LifecycleOperationPanelV2";

const NOW = "2026-07-27T08:00:00Z";
const DIGEST = "a".repeat(64);
const ETAG = `"${"b".repeat(64)}"`;

function operation(overrides: Partial<LifecycleOperationV2> = {}): LifecycleOperationV2 {
  return {
    schema_version: "2",
    operation_id: "operation-panel-1",
    kind: "profile_connect",
    resource: { resource_kind: "profile", resource_id: "profile-lab" },
    request_sha256: DIGEST,
    status: "running",
    phase: "transferring",
    phase_index: 6,
    phase_total: 17,
    progress: { kind: "bytes", completed: 512, total: 1_024 },
    cancellable: true,
    result: null,
    failure: null,
    log_sequence_high_watermark: 3,
    created_at: NOW,
    started_at: NOW,
    updated_at: NOW,
    finished_at: null,
    etag: ETAG,
    ...overrides,
  };
}

function state(overrides: Partial<LifecycleOperationStateV2> = {}): LifecycleOperationStateV2 {
  const current = operation();
  return {
    operation: current,
    droppedBeforeSequence: 0,
    logs: [{
      schema_version: "2",
      operation_id: current.operation_id,
      sequence: 1,
      occurred_at: NOW,
      source: "ssh_stdout",
      text: "Uploading daemon bundle",
      truncated: false,
    }, {
      schema_version: "2",
      operation_id: current.operation_id,
      sequence: 2,
      occurred_at: NOW,
      source: "daemon_stderr",
      text: "Waiting for readiness probe",
      truncated: true,
    }],
    ...overrides,
  };
}

describe("LifecycleOperationPanelV2", () => {
  let root: Root | null = null;

  beforeEach(() => {
    document.body.innerHTML = "<div id=\"root\"></div>";
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  });

  afterEach(async () => {
    if (root) await act(async () => root?.unmount());
    root = null;
    vi.useRealTimers();
    document.body.innerHTML = "";
  });

  it("shows authoritative checkpoints, byte progress, and actual SSH/Daemon lines", async () => {
    root = await render(lifecycleOperationPanelModelV2(state(), "Connect Lab GPU"));

    expect(document.body.textContent).toContain("Connect Lab GPU");
    expect(document.body.textContent).toContain("Transferring");
    expect(document.body.textContent).toContain("Checkpoint 7 of 17");
    expect(document.body.textContent).toContain("512 B of 1 KB");
    expect(document.body.textContent).toContain("SSH output");
    expect(document.body.textContent).toContain("Uploading daemon bundle");
    expect(document.body.textContent).toContain("Daemon error");
    expect(document.body.textContent).toContain("line truncated");
    expect(document.querySelector<HTMLProgressElement>('progress[aria-label="Current phase progress"]')?.value).toBe(512);
  });

  it("renders indeterminate work and updates elapsed time without an ETA or fabricated percentage", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-27T08:00:05Z"));
    root = await render(lifecycleOperationPanelModelV2(state({
      operation: operation({ progress: { kind: "indeterminate" } }),
    })));

    expect(document.body.textContent).toContain("Elapsed 5s");
    const progress = document.querySelector<HTMLProgressElement>('progress[aria-label="Current phase progress"]');
    expect(progress?.hasAttribute("value")).toBe(false);
    expect(document.body.textContent).not.toMatch(/ETA|%/);

    await act(async () => vi.advanceTimersByTime(2_000));
    expect(document.body.textContent).toContain("Elapsed 7s");
  });

  it("supports log expansion, older-page loading, safe cancellation, and reconciliation", async () => {
    const onCancel = vi.fn();
    const onLoadOlder = vi.fn();
    const onResume = vi.fn();
    const manyLogs = Array.from({ length: 12 }, (_, index) => ({
      schema_version: "2" as const,
      operation_id: "operation-panel-1",
      sequence: index + 51,
      occurred_at: NOW,
      source: "desktop" as const,
      text: `checkpoint line ${index + 1}`,
      truncated: false,
    }));
    root = await render(lifecycleOperationPanelModelV2(state({ logs: manyLogs }), undefined, {
      unresolvedMutation: true,
    }), { onCancel, onLoadOlder, onResume });

    expect(logTexts()).not.toContain("checkpoint line 1");
    await click("Show all logs");
    expect(logTexts()).toContain("checkpoint line 1");
    await click("Load older logs");
    await click("Cancel operation");
    await click("Resume / reconcile");
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onResume).toHaveBeenCalledTimes(1);

    await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(logTexts()).not.toContain("checkpoint line 1");
  });

  it("announces typed terminal failure without making the log viewport live", async () => {
    const failed = operation({
      status: "failed",
      progress: null,
      cancellable: false,
      failure: {
        schema_version: "2",
        code: "daemon_readiness_failed",
        summary: "OpenEvo Daemon did not become ready.",
        retryable: true,
        action: "install_repair_daemon",
        affected_resource_id: "profile-lab",
      },
      finished_at: NOW,
    });
    root = await render(lifecycleOperationPanelModelV2(state({ operation: failed })));

    expect(document.querySelector('[aria-live="polite"]')?.textContent).toContain("Failed");
    expect(document.body.textContent).toContain("OpenEvo Daemon did not become ready.");
    expect(document.querySelector(".lifecycle-log-viewport")?.hasAttribute("aria-live")).toBe(false);
    expect(button("Cancel operation")).toBeNull();
  });

  async function render(
    model: ReturnType<typeof lifecycleOperationPanelModelV2>,
    actions: {
      readonly onCancel?: () => void;
      readonly onLoadOlder?: () => void;
      readonly onResume?: () => void;
    } = {},
  ): Promise<Root> {
    const container = document.querySelector("#root");
    if (!container) throw new Error("test root missing");
    const next = createRoot(container);
    await act(async () => next.render(<LifecycleOperationPanelV2 model={model} {...actions} />));
    return next;
  }
});

function button(label: string): HTMLButtonElement | null {
  return [...document.querySelectorAll<HTMLButtonElement>("button")]
    .find((candidate) => candidate.textContent?.trim() === label) ?? null;
}

async function click(label: string): Promise<void> {
  const target = button(label);
  if (!target) throw new Error(`Button not found: ${label}`);
  await act(async () => target.click());
}

function logTexts(): string[] {
  return [...document.querySelectorAll<HTMLPreElement>(".lifecycle-log-viewport pre")]
    .map((entry) => entry.textContent ?? "");
}
