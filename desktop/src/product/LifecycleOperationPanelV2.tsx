import { useEffect, useMemo, useState } from "react";
import type {
  DesktopErrorV2,
  LifecycleLogEntryV2,
  LifecycleOperationV2,
  LifecycleProgressV2,
} from "../api/v2/schemas";
import type { LifecycleOperationStateV2 } from "./lifecycleOperationsV2";

const COLLAPSED_LOG_LINES = 6;

export interface OperationPanelModelV2 {
  readonly operationId: string;
  readonly title: string;
  readonly status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  readonly phaseLabel: string;
  readonly checkpointCompleted: number;
  readonly checkpointTotal: number;
  readonly progress: LifecycleProgressV2 | null;
  readonly createdAt: string;
  readonly startedAt: string | null;
  readonly finishedAt: string | null;
  readonly cancellable: boolean;
  readonly failure: DesktopErrorV2 | null;
  readonly logs: readonly LifecycleLogEntryV2[];
  readonly droppedBeforeSequence: number;
  readonly hasOlderLogs: boolean;
  readonly unresolvedMutation: boolean;
  readonly emptyLogMessage?: string;
}

export interface LifecycleOperationPanelV2Props {
  readonly model: OperationPanelModelV2;
  readonly onCancel?: () => void | Promise<void>;
  readonly onLoadOlder?: () => void | Promise<void>;
  readonly onResume?: () => void | Promise<void>;
}

export function lifecycleOperationPanelModelV2(
  state: LifecycleOperationStateV2,
  title = lifecycleTitleV2(state.operation),
  options: { readonly unresolvedMutation?: boolean } = {},
): OperationPanelModelV2 {
  const operation = state.operation;
  const firstSequence = state.logs[0]?.sequence ?? null;
  return Object.freeze({
    operationId: operation.operation_id,
    title,
    status: operation.status,
    phaseLabel: phaseLabelV2(operation.phase),
    checkpointCompleted: Math.min(operation.phase_index + 1, operation.phase_total),
    checkpointTotal: operation.phase_total,
    progress: operation.progress,
    createdAt: operation.created_at,
    startedAt: operation.started_at,
    finishedAt: operation.finished_at,
    cancellable: operation.cancellable,
    failure: operation.failure,
    logs: state.logs,
    droppedBeforeSequence: state.droppedBeforeSequence,
    hasOlderLogs: firstSequence !== null && firstSequence > state.droppedBeforeSequence + 1,
    unresolvedMutation: options.unresolvedMutation ?? false,
  });
}

export function LifecycleOperationPanelV2({
  model,
  onCancel,
  onLoadOlder,
  onResume,
}: LifecycleOperationPanelV2Props) {
  const [expanded, setExpanded] = useState(false);
  const elapsed = useElapsedV2(model.createdAt, model.startedAt, model.finishedAt);
  const visibleLogs = useMemo(
    () => expanded ? model.logs : model.logs.slice(-COLLAPSED_LOG_LINES),
    [expanded, model.logs],
  );

  useEffect(() => {
    if (!expanded) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setExpanded(false);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [expanded]);

  return (
    <section className={`lifecycle-operation-panel status-${model.status}`} aria-labelledby={`lifecycle-title-${model.operationId}`}>
      <header className="lifecycle-operation-head">
        <div>
          <span className="panel-kicker">Operation progress</span>
          <h3 id={`lifecycle-title-${model.operationId}`}>{model.title}</h3>
        </div>
        <span className={`lifecycle-status status-${model.status}`}>{statusLabelV2(model.status)}</span>
      </header>

      <div className="lifecycle-live-status" aria-live="polite" aria-atomic="true">
        <strong>{statusLabelV2(model.status)}</strong>
        <span>{model.phaseLabel}</span>
      </div>

      <div className="lifecycle-progress-stack">
        <div className="lifecycle-progress-label">
          <span>{model.phaseLabel}</span>
          <span>Checkpoint {model.checkpointCompleted} of {model.checkpointTotal}</span>
        </div>
        <progress
          aria-label="Lifecycle checkpoints"
          max={model.checkpointTotal}
          value={model.checkpointCompleted}
        />
        {model.progress === null ? null : (
          <div className="lifecycle-subprogress">
            <progress
              aria-label="Current phase progress"
              {...(model.progress.kind === "indeterminate" ? { max: 1 } : {
                max: model.progress.total,
                value: model.progress.completed,
              })}
            />
            <span>{progressLabelV2(model.progress)}</span>
          </div>
        )}
      </div>

      <div className="lifecycle-meta">
        <span>Elapsed {elapsed}</span>
        <code>{model.operationId}</code>
      </div>

      {model.failure === null ? null : (
        <div className="lifecycle-failure" role="alert">
          <strong>{model.failure.summary}</strong>
          <span>{model.failure.retryable ? "This operation can be reconciled or retried safely." : "Review the required action before continuing."}</span>
        </div>
      )}

      <div className="lifecycle-log-section">
        <div className="lifecycle-log-head">
          <strong>Process log</strong>
          {model.logs.length > COLLAPSED_LOG_LINES ? (
            <button type="button" className="text-button" onClick={() => setExpanded((value) => !value)}>
              {expanded ? "Show latest logs" : "Show all logs"}
            </button>
          ) : null}
        </div>
        {model.droppedBeforeSequence > 0 ? (
          <p className="lifecycle-log-notice">Earlier log lines through sequence {model.droppedBeforeSequence} are no longer retained.</p>
        ) : null}
        <ol className="lifecycle-log-viewport" aria-label="Operation process log">
          {visibleLogs.length === 0 ? <li className="lifecycle-log-empty">{model.emptyLogMessage ?? "Waiting for process output…"}</li> : visibleLogs.map((entry) => (
            <li key={entry.sequence}>
              <span className="lifecycle-log-sequence">{entry.sequence}</span>
              <span className={`lifecycle-log-source source-${entry.source}`}>{sourceLabelV2(entry.source)}</span>
              <pre>{entry.text}</pre>
              {entry.truncated ? <span className="lifecycle-log-truncated">line truncated</span> : null}
            </li>
          ))}
        </ol>
      </div>

      <footer className="lifecycle-operation-actions">
        {model.hasOlderLogs && onLoadOlder !== undefined ? (
          <button type="button" className="text-button" onClick={() => void onLoadOlder()}>Load older logs</button>
        ) : null}
        {(model.unresolvedMutation || model.failure?.retryable === true) && onResume !== undefined ? (
          <button type="button" className="secondary-button" onClick={() => void onResume()}>Resume / reconcile</button>
        ) : null}
        {model.cancellable && onCancel !== undefined ? (
          <button type="button" className="danger-button" onClick={() => void onCancel()}>Cancel operation</button>
        ) : null}
      </footer>
    </section>
  );
}

function useElapsedV2(createdAt: string, startedAt: string | null, finishedAt: string | null): string {
  const start = Date.parse(startedAt ?? createdAt);
  const finish = finishedAt === null ? null : Date.parse(finishedAt);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (finish !== null) return;
    const interval = globalThis.setInterval(() => setNow(Date.now()), 1_000);
    return () => globalThis.clearInterval(interval);
  }, [finish]);
  return durationLabelV2(Math.max(0, (finish ?? now) - start));
}

function durationLabelV2(milliseconds: number): string {
  const totalSeconds = Math.floor(milliseconds / 1_000);
  const hours = Math.floor(totalSeconds / 3_600);
  const minutes = Math.floor((totalSeconds % 3_600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}h ${minutes}m ${seconds}s`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

function progressLabelV2(progress: LifecycleProgressV2): string {
  if (progress.kind === "indeterminate") return "Working — progress is not measurable for this phase";
  if (progress.kind === "bytes") return `${bytesLabelV2(progress.completed)} of ${bytesLabelV2(progress.total)}`;
  return `${progress.completed.toLocaleString()} of ${progress.total.toLocaleString()} items`;
}

function bytesLabelV2(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${trimNumberV2(bytes / 1024 ** 3)} GB`;
  if (bytes >= 1024 ** 2) return `${trimNumberV2(bytes / 1024 ** 2)} MB`;
  if (bytes >= 1024) return `${trimNumberV2(bytes / 1024)} KB`;
  return `${bytes} B`;
}

function trimNumberV2(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

function statusLabelV2(status: OperationPanelModelV2["status"]): string {
  const labels: Record<OperationPanelModelV2["status"], string> = {
    queued: "Queued",
    running: "In progress",
    succeeded: "Completed",
    failed: "Failed",
    cancelled: "Cancelled",
  };
  return labels[status];
}

function sourceLabelV2(source: LifecycleLogEntryV2["source"]): string {
  const labels: Record<LifecycleLogEntryV2["source"], string> = {
    desktop: "Desktop",
    ssh_stdout: "SSH output",
    ssh_stderr: "SSH error",
    daemon_stdout: "Daemon output",
    daemon_stderr: "Daemon error",
  };
  return labels[source];
}

function phaseLabelV2(phase: LifecycleOperationV2["phase"]): string {
  const labels: Record<LifecycleOperationV2["phase"], string> = {
    validation: "Validating request",
    queued: "Queued",
    resolving_system_openssh: "Resolving system OpenSSH configuration",
    connecting: "Connecting with system OpenSSH",
    waiting_for_user: "Waiting for authentication or host review",
    remote_preflight: "Checking remote server requirements",
    transferring: "Transferring",
    verifying: "Verifying transferred files",
    starting_daemon: "Starting OpenEvo Daemon",
    waiting_for_daemon: "Waiting for OpenEvo Daemon readiness",
    opening_project_tunnel: "Opening the project tunnel",
    negotiating_core: "Negotiating Core authority",
    preparing_native_workspace: "Preparing the local workspace snapshot",
    creating_remote_project: "Creating or loading the remote project",
    verifying_project: "Verifying project authority",
    activating: "Activating",
    finalizing: "Finalizing",
  };
  return labels[phase];
}

function lifecycleTitleV2(operation: LifecycleOperationV2): string {
  const labels: Record<LifecycleOperationV2["kind"], string> = {
    profile_connect: "Connect remote workspace",
    profile_disconnect: "Disconnect remote workspace",
    host_key_review: "Continue host-key review",
    native_workspace_prepare: "Prepare local workspace snapshot",
    project_create: "Create remote project",
    project_activate: "Activate remote project",
  };
  return labels[operation.kind];
}
