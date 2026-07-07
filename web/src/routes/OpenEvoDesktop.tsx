import {
  Activity,
  Brain,
  CheckCircle2,
  Database,
  FileText,
  GitBranch,
  KeyRound,
  Play,
  RefreshCw,
  Server,
  Settings,
  ShieldCheck,
  Upload,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import {
  fetchOpenEvoDesktopShellModel,
  runOpenEvoBootstrap,
  runOpenEvoStartRun,
  runOpenEvoWorkspaceSync,
} from "../api/openevo";
import {
  type EvolutionStepState,
  type RemoteServiceState,
  getOpenEvoDesktopShellModel,
  getOpenEvoTimelineSummary,
} from "./openevoDesktopModel";

const serviceTone: Record<RemoteServiceState, string> = {
  ready: "border-emerald-200 bg-emerald-50 text-emerald-800",
  running: "border-blue-200 bg-blue-50 text-blue-800",
  planned: "border-slate-200 bg-slate-50 text-slate-700",
  blocked: "border-rose-200 bg-rose-50 text-rose-800",
};

const evolutionTone: Record<EvolutionStepState, string> = {
  complete: "bg-emerald-600",
  running: "bg-blue-600",
  planned: "bg-slate-300",
  blocked: "bg-rose-600",
};

export function OpenEvoDesktop() {
  const [model, setModel] = useState(() => getOpenEvoDesktopShellModel());
  const [sidecarConnected, setSidecarConnected] = useState(false);
  const [workspaceRunning, setWorkspaceRunning] = useState(false);
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const [bootstrapRunning, setBootstrapRunning] = useState(false);
  const [bootstrapError, setBootstrapError] = useState<string | null>(null);
  const [runRunning, setRunRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const summary = getOpenEvoTimelineSummary(model);

  useEffect(() => {
    let cancelled = false;

    fetchOpenEvoDesktopShellModel()
      .then((nextModel) => {
        if (!cancelled) {
          setModel(nextModel);
          setSidecarConnected(true);
        }
      })
      .catch(() => undefined);

    return () => {
      cancelled = true;
    };
  }, []);

  const handleWorkspaceSync = async () => {
    setWorkspaceRunning(true);
    setWorkspaceError(null);
    try {
      const response = await runOpenEvoWorkspaceSync();
      setModel(response.status);
      setSidecarConnected(true);
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Workspace sync failed";
      setWorkspaceError(message);
    } finally {
      setWorkspaceRunning(false);
    }
  };

  const handleBootstrap = async () => {
    setBootstrapRunning(true);
    setBootstrapError(null);
    try {
      const response = await runOpenEvoBootstrap();
      setModel(response.status);
      setSidecarConnected(true);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Bootstrap failed";
      setBootstrapError(message);
    } finally {
      setBootstrapRunning(false);
    }
  };

  const handleStartRun = async () => {
    setRunRunning(true);
    setRunError(null);
    try {
      const response = await runOpenEvoStartRun();
      setModel(response.status);
      setSidecarConnected(true);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Run launch failed";
      setRunError(message);
    } finally {
      setRunRunning(false);
    }
  };

  return (
    <div className="space-y-5">
      <section className="flex flex-col gap-3 border-b border-slate-200 pb-4 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <div className="text-xs font-medium uppercase text-emerald-700">
            OpenEvo Desktop
          </div>
          <h1 className="mt-1 text-2xl font-semibold text-slate-950">
            {model.project.name}
          </h1>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-slate-600">
            <span>{model.project.taskId}</span>
            <span className="text-slate-300">/</span>
            <span>{model.execution.mode}</span>
            <span className="text-slate-300">/</span>
            <span>transcript evolution</span>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <CommandButton
            icon={<Upload size={16} />}
            label={workspaceRunning ? "Syncing" : "Sync Workspace"}
            disabled={
              !sidecarConnected || workspaceRunning || bootstrapRunning || runRunning
            }
            onClick={handleWorkspaceSync}
          />
          <CommandButton
            icon={<RefreshCw size={16} />}
            label={bootstrapRunning ? "Bootstrapping" : "Bootstrap"}
            disabled={
              !sidecarConnected || bootstrapRunning || workspaceRunning || runRunning
            }
            onClick={handleBootstrap}
          />
          <CommandButton
            icon={<Play size={16} />}
            label={runRunning ? "Running" : "Start Run"}
            primary
            disabled={
              !sidecarConnected || runRunning || workspaceRunning || bootstrapRunning
            }
            onClick={handleStartRun}
          />
        </div>
      </section>

      <section className="grid grid-cols-1 gap-4 lg:grid-cols-4">
        <Metric
          label="Remote services"
          value={`${summary.readyServices}/${summary.totalServices}`}
          detail="ready"
        />
        <Metric
          label="Evolution steps"
          value={`${summary.completedEvolutionSteps}/${summary.totalEvolutionSteps}`}
          detail="complete"
        />
        <Metric
          label="Token metrics"
          value={model.execution.tokenMetricsAvailable ? "available" : "off"}
          detail="subscription transcript mode"
        />
        <Metric
          label="Bootstrap"
          value={summary.bootstrapReady ? "ready" : "needs setup"}
          detail="remote preflight"
        />
      </section>

      <section className="grid grid-cols-1 gap-4 xl:grid-cols-[1.15fr_0.85fr]">
        <Panel title="Science Project" icon={<FileText size={17} />}>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <Field label="Task source" value={model.project.source} />
            <Field label="Codex model" value={model.execution.model} />
            <Field label="Objective" value={model.project.objective} wide />
          </div>
        </Panel>

        <Panel title="Remote Profile" icon={<Server size={17} />}>
          <div className="grid grid-cols-1 gap-3 text-sm">
            <Field label="Profile" value={`${model.remote.id} - ${model.remote.user}`} />
            <Field label="Host" value={model.remote.host} />
            <Field label="HTTPS proxy" value={model.remote.proxy.httpsProxy} />
            <Field
              label="Hugging Face endpoint"
              value={model.remote.proxy.huggingFaceEndpoint}
            />
          </div>
        </Panel>
      </section>

      <section className="grid grid-cols-1 gap-4 xl:grid-cols-[0.95fr_1.05fr]">
        <Panel title="Bootstrap Readiness" icon={<ShieldCheck size={17} />}>
          <div className="space-y-3">
            <PathRow label="Workspace root" value={model.bootstrap.workspaceRoot} />
            <PathRow label="State root" value={model.bootstrap.stateRoot} />
            <div
              className={`flex items-center gap-2 rounded-md border px-3 py-2 text-sm ${
                summary.bootstrapReady
                  ? "border-emerald-200 bg-emerald-50 text-emerald-900"
                  : "border-amber-200 bg-amber-50 text-amber-900"
              }`}
            >
              <CheckCircle2 size={16} />
              <span>{summary.bootstrapReady ? "Remote ready" : "Setup required"}</span>
            </div>
            {summary.readinessNotes.map((note) => (
              <div
                key={note}
                className="flex items-center gap-2 rounded-md border border-emerald-200 bg-white px-3 py-2 text-sm text-emerald-900"
              >
                <KeyRound size={16} />
                <span>{note}</span>
              </div>
            ))}
            {bootstrapError ? (
              <div className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-900">
                {bootstrapError}
              </div>
            ) : null}
            {workspaceError ? (
              <div className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-900">
                {workspaceError}
              </div>
            ) : null}
            {runError ? (
              <div className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-900">
                {runError}
              </div>
            ) : null}
          </div>
        </Panel>

        <Panel title="Remote Services" icon={<Activity size={17} />}>
          <div className="divide-y divide-slate-100">
            {model.services.map((service) => (
              <div
                key={service.id}
                className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 py-3 first:pt-0 last:pb-0"
              >
                <div>
                  <div className="text-sm font-medium text-slate-900">
                    {service.label}
                  </div>
                  <div className="mt-0.5 text-xs text-slate-500">{service.detail}</div>
                </div>
                <StatusBadge state={service.state} />
              </div>
            ))}
          </div>
        </Panel>
      </section>

      <Panel title="Evolution Timeline" icon={<Brain size={17} />}>
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-4">
          {model.evolution.map((step) => (
            <div key={step.id} className="border-l border-slate-200 pl-3">
              <div className="flex items-center gap-2">
                <span
                  className={`h-2.5 w-2.5 rounded-full ${evolutionTone[step.state]}`}
                />
                <span className="text-sm font-medium text-slate-900">{step.label}</span>
              </div>
              <div className="mt-2 text-xs uppercase text-slate-500">{step.state}</div>
              <div className="mt-1 text-sm text-slate-600">{step.detail}</div>
            </div>
          ))}
        </div>
      </Panel>

      <section className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <QuickAction
          icon={<GitBranch size={16} />}
          label="Source"
          value="Git sync ready"
        />
        <QuickAction
          icon={<Database size={16} />}
          label="Artifacts"
          value="memory, skills, agent system"
        />
        <QuickAction
          icon={<Settings size={16} />}
          label="Runtime"
          value="managed science profile"
        />
      </section>
    </div>
  );
}

function CommandButton({
  icon,
  label,
  primary = false,
  disabled = false,
  onClick,
}: {
  icon: ReactNode;
  label: string;
  primary?: boolean;
  disabled?: boolean;
  onClick?: () => void;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={`inline-flex h-9 items-center gap-2 rounded-md border px-3 text-sm font-medium ${
        primary
          ? "border-slate-900 bg-slate-900 text-white"
          : "border-slate-200 bg-white text-slate-700"
      } ${
        disabled
          ? "cursor-not-allowed opacity-60"
          : "hover:border-slate-300 hover:bg-slate-50"
      }`}
    >
      {icon}
      <span>{label}</span>
    </button>
  );
}

function Panel({
  title,
  icon,
  children,
}: {
  title: string;
  icon: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white">
      <div className="flex items-center gap-2 border-b border-slate-100 px-4 py-3 text-sm font-semibold text-slate-900">
        {icon}
        <span>{title}</span>
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

function Metric({
  label,
  value,
  detail,
}: {
  label: string;
  value: string;
  detail: string;
}) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="text-xs font-medium uppercase text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-slate-950">{value}</div>
      <div className="mt-1 text-xs text-slate-500">{detail}</div>
    </div>
  );
}

function Field({
  label,
  value,
  wide = false,
}: {
  label: string;
  value: string;
  wide?: boolean;
}) {
  return (
    <div className={wide ? "md:col-span-2" : undefined}>
      <div className="text-xs font-medium uppercase text-slate-500">{label}</div>
      <div className="mt-1 text-sm leading-6 text-slate-900">{value}</div>
    </div>
  );
}

function PathRow({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs font-medium uppercase text-slate-500">{label}</div>
      <div className="mt-1 overflow-hidden text-ellipsis rounded-md bg-slate-50 px-3 py-2 font-mono text-xs text-slate-700">
        {value}
      </div>
    </div>
  );
}

function StatusBadge({ state }: { state: RemoteServiceState }) {
  return (
    <span
      className={`inline-flex h-7 items-center gap-1.5 rounded-md border px-2 text-xs font-medium ${serviceTone[state]}`}
    >
      {state === "ready" ? <CheckCircle2 size={14} /> : null}
      {state}
    </span>
  );
}

function QuickAction({
  icon,
  label,
  value,
}: {
  icon: ReactNode;
  label: string;
  value: string;
}) {
  return (
    <div className="flex items-center gap-3 rounded-lg border border-slate-200 bg-white px-4 py-3">
      <div className="flex h-9 w-9 items-center justify-center rounded-md bg-slate-100 text-slate-700">
        {icon}
      </div>
      <div className="min-w-0">
        <div className="text-xs font-medium uppercase text-slate-500">{label}</div>
        <div className="truncate text-sm text-slate-900">{value}</div>
      </div>
    </div>
  );
}
