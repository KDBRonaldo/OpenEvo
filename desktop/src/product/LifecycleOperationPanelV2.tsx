import { useEffect, useMemo, useState } from "react";
import type {
  CoreEventEnvelopeV2,
  DiagnosticV2,
  LifecycleLogEntryV2,
  LifecycleOperationV2,
  LifecycleProgressV2,
  OperationV2,
  ServiceV2,
  SuccessorTransitionV2,
  TaskV2,
} from "../api/v2/schemas";
import type { LogEntryV2 } from "../api/v2/logs";
import type { LifecycleOperationStateV2 } from "./lifecycleOperationsV2";

const COLLAPSED_LOG_LINES = 6;

export type OperationPanelLogSourceV2 = LifecycleLogEntryV2["source"]
  | "core_event"
  | "task_system"
  | "task_stdout"
  | "task_stderr"
  | "task_transcript"
  | "service_system"
  | "service_stdout"
  | "service_stderr"
  | "service_transcript";

export interface OperationPanelLogEntryV2 {
  readonly sequence: number;
  readonly occurred_at: string;
  readonly source: OperationPanelLogSourceV2;
  readonly text: string;
  readonly truncated: boolean;
}

export interface OperationPanelModelV2 {
  readonly operationId: string;
  readonly title: string;
  readonly status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  readonly phaseLabel: string;
  readonly checkpointCompleted: number | null;
  readonly checkpointTotal: number | null;
  readonly progress: LifecycleProgressV2 | null;
  readonly createdAt: string;
  readonly startedAt: string | null;
  readonly finishedAt: string | null;
  readonly elapsedMillisecondsFloor?: number;
  readonly cancellable: boolean;
  readonly failure: OperationPanelFailureV2 | null;
  readonly logs: readonly OperationPanelLogEntryV2[];
  readonly logTitle?: string;
  readonly droppedBeforeSequence: number;
  readonly hasOlderLogs: boolean;
  readonly hasNewerLogs: boolean;
  readonly unresolvedMutation: boolean;
  readonly emptyLogMessage?: string;
}

export interface OperationPanelFailureV2 {
  readonly summary: string;
  readonly retryable: boolean;
  readonly nextAction?: string;
}

export interface LifecycleOperationPanelV2Props {
  readonly model: OperationPanelModelV2;
  readonly onCancel?: () => void | Promise<void>;
  readonly onLoadOlder?: () => void | Promise<void>;
  readonly onLoadLatest?: () => void | Promise<void>;
  readonly onResume?: () => void | Promise<void>;
}

export function lifecycleOperationPanelModelV2(
  state: LifecycleOperationStateV2,
  title = lifecycleTitleV2(state.operation),
  options: { readonly unresolvedMutation?: boolean } = {},
): OperationPanelModelV2 {
  const operation = state.operation;
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
    hasOlderLogs: state.hasOlderLogs,
    hasNewerLogs: state.hasNewerLogs,
    unresolvedMutation: options.unresolvedMutation ?? false,
  });
}

export function coreOperationPanelModelV2(operation: OperationV2): OperationPanelModelV2 {
  const terminal = !["queued", "running"].includes(operation.status);
  const progress: LifecycleProgressV2 | null = operation.progress_total > 0
    ? { kind: "items", completed: operation.progress_completed, total: operation.progress_total }
    : terminal ? null : { kind: "indeterminate" };
  return Object.freeze({
    operationId: operation.operation_id,
    title: coreOperationTitleV2(operation.kind),
    status: operation.status,
    phaseLabel: `Core status: ${operation.status}`,
    checkpointCompleted: null,
    checkpointTotal: null,
    progress,
    createdAt: operation.created_at,
    startedAt: operation.created_at,
    finishedAt: terminal ? operation.updated_at : null,
    cancellable: !terminal,
    failure: operation.error === null ? null : {
      summary: operation.error.message,
      retryable: operation.error.retryable,
      nextAction: operation.error.next_action,
    },
    logs: Object.freeze([]),
    droppedBeforeSequence: 0,
    hasOlderLogs: false,
    hasNewerLogs: false,
    unresolvedMutation: false,
    emptyLogMessage: "Core operation output is available from the owning Task, transition, or service view.",
  });
}

export function taskPanelModelV2(
  task: TaskV2,
  timeline: readonly CoreEventEnvelopeV2[],
  logs: readonly LogEntryV2[] = [],
): OperationPanelModelV2 {
  const terminal = ["completed", "closed", "failed", "cancelled"].includes(task.state);
  const status: OperationPanelModelV2["status"] = task.state === "failed"
    ? "failed"
    : task.state === "cancelled"
      ? "cancelled"
      : ["completed", "closed"].includes(task.state) ? "succeeded" : "running";
  const progress: LifecycleProgressV2 | null = terminal ? null : { kind: "indeterminate" };
  return Object.freeze({
    operationId: task.task_id,
    title: "Run science Task",
    status,
    phaseLabel: `Task state: ${task.state}`,
    checkpointCompleted: null,
    checkpointTotal: null,
    progress,
    createdAt: task.created_at,
    startedAt: task.created_at,
    finishedAt: terminal ? task.updated_at : null,
    cancellable: ["admitted", "preparing", "running"].includes(task.state),
    failure: task.state === "failed" ? {
      summary: "The authoritative Task failed.",
      retryable: true,
      nextAction: "Append a new infrastructure Attempt under the same Task Admission.",
    } : null,
    logs: logs.length > 0
      ? Object.freeze(logs.slice(-200).map((entry) => ({
          sequence: entry.sequence,
          occurred_at: entry.occurred_at,
          source: `task_${entry.stream}` as OperationPanelLogSourceV2,
          text: entry.message,
          truncated: false,
        })))
      : coreTimelineLogsV2(timeline),
    logTitle: logs.length > 0 ? "Task and transcript log" : "Core timeline",
    droppedBeforeSequence: 0,
    hasOlderLogs: false,
    hasNewerLogs: false,
    unresolvedMutation: false,
    emptyLogMessage: "Waiting for authoritative Task output…",
  });
}

export function transitionPanelModelV2(
  transition: SuccessorTransitionV2,
  timeline: readonly CoreEventEnvelopeV2[],
): OperationPanelModelV2 {
  const terminal = ["committed", "failed", "cancelled", "superseded"].includes(transition.state);
  const status: OperationPanelModelV2["status"] = transition.state === "committed"
    ? "succeeded"
    : transition.state === "failed"
      ? "failed"
      : ["cancelled", "superseded"].includes(transition.state) ? "cancelled" : "running";
  const progress: LifecycleProgressV2 | null = transition.progress_total > 0
    ? {
        kind: "items",
        completed: transition.progress_completed,
        total: transition.progress_total,
      }
    : terminal ? null : { kind: "indeterminate" };
  return Object.freeze({
    operationId: transition.transition.successor_transition_id,
    title: "Build successor Project Head",
    status,
    phaseLabel: `Successor state: ${transition.state}`,
    checkpointCompleted: null,
    checkpointTotal: null,
    progress,
    createdAt: transition.created_at,
    startedAt: transition.created_at,
    finishedAt: terminal ? transition.updated_at : null,
    cancellable: false,
    failure: transition.error === null ? null : {
      summary: transition.error.message,
      retryable: transition.error.retryable,
      nextAction: transition.error.next_action,
    },
    logs: coreTimelineLogsV2(timeline),
    logTitle: "Core timeline",
    droppedBeforeSequence: 0,
    hasOlderLogs: false,
    hasNewerLogs: false,
    unresolvedMutation: false,
    emptyLogMessage: "Waiting for an authoritative successor event…",
  });
}

export function diagnosticPanelModelV2(diagnostic: DiagnosticV2): OperationPanelModelV2 {
  const terminal = ["ready", "failed"].includes(diagnostic.status);
  const progress: LifecycleProgressV2 | null = terminal ? null : { kind: "indeterminate" };
  return Object.freeze({
    operationId: diagnostic.diagnostic_id,
    title: `Collect ${diagnostic.scope} diagnostics`,
    status: diagnostic.status === "ready" ? "succeeded" : diagnostic.status,
    phaseLabel: `Diagnostic status: ${diagnostic.status}`,
    checkpointCompleted: null,
    checkpointTotal: null,
    progress,
    createdAt: diagnostic.created_at,
    startedAt: diagnostic.created_at,
    finishedAt: terminal ? diagnostic.updated_at : null,
    cancellable: false,
    failure: diagnostic.status === "failed" ? {
      summary: "Remote diagnostic collection failed.",
      retryable: true,
    } : null,
    logs: Object.freeze([]),
    logTitle: "Diagnostic output",
    droppedBeforeSequence: 0,
    hasOlderLogs: false,
    hasNewerLogs: false,
    unresolvedMutation: false,
    emptyLogMessage: diagnostic.artifact_id === null
      ? "Waiting for the diagnostic artifact…"
      : `Diagnostic artifact ${diagnostic.artifact_id} is ready.`,
  });
}

export function servicePanelModelV2(
  service: ServiceV2,
  logs: readonly LogEntryV2[],
): OperationPanelModelV2 {
  const status: OperationPanelModelV2["status"] = service.status === "ready"
    ? "succeeded"
    : ["starting", "stopping"].includes(service.status) ? "running" : "failed";
  const progress: LifecycleProgressV2 | null = ["starting", "stopping"].includes(service.status)
    ? { kind: "indeterminate" }
    : null;
  return Object.freeze({
    operationId: service.service_id,
    title: `${capitalizeV2(service.kind)} service`,
    status,
    phaseLabel: `Service status: ${service.status}`,
    checkpointCompleted: null,
    checkpointTotal: null,
    progress,
    createdAt: service.updated_at,
    startedAt: ["starting", "stopping"].includes(service.status) ? service.updated_at : null,
    finishedAt: ["starting", "stopping"].includes(service.status) ? null : service.updated_at,
    cancellable: false,
    failure: ["degraded", "unavailable"].includes(service.status) ? {
      summary: `The remote ${service.kind} service is ${service.status}.`,
      retryable: true,
    } : null,
    logs: Object.freeze(logs.slice(-200).map((entry) => ({
      sequence: entry.sequence,
      occurred_at: entry.occurred_at,
      source: `service_${entry.stream}` as OperationPanelLogSourceV2,
      text: entry.message,
      truncated: false,
    }))),
    logTitle: "Service log",
    droppedBeforeSequence: 0,
    hasOlderLogs: false,
    hasNewerLogs: false,
    unresolvedMutation: false,
    emptyLogMessage: "No retained service output is loaded.",
  });
}

export function LifecycleOperationPanelV2({
  model,
  onCancel,
  onLoadOlder,
  onLoadLatest,
  onResume,
}: LifecycleOperationPanelV2Props) {
  const [expanded, setExpanded] = useState(false);
  const elapsed = useElapsedV2(
    model.createdAt,
    model.startedAt,
    model.finishedAt,
    model.elapsedMillisecondsFloor,
  );
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
        {model.checkpointCompleted === null || model.checkpointTotal === null ? null : (
          <>
            <div className="lifecycle-progress-label">
              <span>{model.phaseLabel}</span>
              <span>Checkpoint {model.checkpointCompleted} of {model.checkpointTotal}</span>
            </div>
            <progress
              aria-label="Lifecycle checkpoints"
              max={model.checkpointTotal}
              value={model.checkpointCompleted}
            />
          </>
        )}
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
          {model.failure.nextAction ? <span>{model.failure.nextAction}</span> : null}
        </div>
      )}

      <div className="lifecycle-log-section">
        <div className="lifecycle-log-head">
          <strong>{model.logTitle ?? "Process log"}</strong>
          {model.logs.length > COLLAPSED_LOG_LINES ? (
            <button type="button" className="text-button" onClick={() => setExpanded((value) => !value)}>
              {expanded ? "Show latest logs" : "Show all logs"}
            </button>
          ) : null}
        </div>
        {model.droppedBeforeSequence > 0 ? (
          <p className="lifecycle-log-notice">Earlier log lines through sequence {model.droppedBeforeSequence} are no longer retained.</p>
        ) : null}
        <ol className="lifecycle-log-viewport" aria-label="Operation process log" tabIndex={0}>
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
        {model.hasNewerLogs && onLoadLatest !== undefined ? (
          <button type="button" className="text-button" onClick={() => void onLoadLatest()}>Show latest log tail</button>
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

function useElapsedV2(
  createdAt: string,
  startedAt: string | null,
  finishedAt: string | null,
  elapsedMillisecondsFloor = 0,
): string {
  const start = Date.parse(startedAt ?? createdAt);
  const finish = finishedAt === null ? null : Date.parse(finishedAt);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (finish !== null) return;
    const interval = globalThis.setInterval(() => setNow(Date.now()), 1_000);
    return () => globalThis.clearInterval(interval);
  }, [finish]);
  return durationLabelV2(Math.max(0, elapsedMillisecondsFloor, (finish ?? now) - start));
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

function sourceLabelV2(source: OperationPanelLogSourceV2): string {
  const labels: Record<OperationPanelLogSourceV2, string> = {
    desktop: "Desktop",
    ssh_stdout: "SSH output",
    ssh_stderr: "SSH error",
    daemon_stdout: "Daemon output",
    daemon_stderr: "Daemon error",
    core_event: "Core event",
    task_system: "Task state",
    task_stdout: "Task output",
    task_stderr: "Task error",
    task_transcript: "Transcript",
    service_system: "Service",
    service_stdout: "Service output",
    service_stderr: "Service error",
    service_transcript: "Transcript",
  };
  return labels[source];
}

function coreTimelineLogsV2(
  timeline: readonly CoreEventEnvelopeV2[],
): readonly OperationPanelLogEntryV2[] {
  return Object.freeze(timeline.slice(-200).map((event) => ({
    sequence: event.sequence,
    occurred_at: event.occurred_at,
    source: "core_event" as const,
    text: `Core event: ${event.event_type.replaceAll("_", " ")}`,
    truncated: false,
  })));
}

function capitalizeV2(value: string): string {
  return value.length === 0 ? value : `${value[0]!.toUpperCase()}${value.slice(1)}`;
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
    starting_daemon: "Starting EvoLab service",
    waiting_for_daemon: "Waiting for EvoLab service readiness",
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

function coreOperationTitleV2(kind: OperationV2["kind"]): string {
  const titles: Record<OperationV2["kind"], string> = {
    transition_retry: "Retry successor transition",
    transition_abandon: "Abandon successor transition",
    attempt_cancel: "Cancel Task attempt",
    task_close: "Close Task",
    service_restart: "Restart remote service",
    diagnostic: "Collect remote diagnostics",
    cache_cleanup: "Clean safe remote caches",
  };
  return titles[kind];
}
