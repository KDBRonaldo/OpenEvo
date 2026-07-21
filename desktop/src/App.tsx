import {
  Activity,
  AlertCircle,
  BookOpen,
  CircleDot,
  LoaderCircle,
  Plus,
  RefreshCw,
  Sparkles,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import {
  ContractVersionUnsupportedError,
  DesktopApiError,
  DesktopContractError,
} from "./api/v1/client";
import { Dashboard } from "./routes/Dashboard";
import { TasksList } from "./routes/TasksList";
import { TaskDetail } from "./routes/TaskDetail";
import { SessionDetail } from "./routes/SessionDetail";
import { Compare } from "./routes/Compare";
import { OpenEvoDesktop } from "./routes/OpenEvoDesktop";
import { subscribeOpenEvoEvents } from "./api/sse";
import { OpenEvoMark } from "./components/OpenEvoMark";
import { DesktopProductApp } from "./product/DesktopProductApp";
import { SampleScientificProjectView } from "./product/ScientificProjectSample";
import {
  SAMPLE_SCIENTIFIC_PROJECT,
  SAMPLE_SCIENTIFIC_PROJECTS,
  sampleScientificProject,
  type SampleScientificProjectId,
} from "./product/scientificProjectSampleData";
import type { DesktopProductProvider } from "./product/provider";
import {
  createReleaseDesktopProductProvider,
  reportReleaseDesktopBootstrapStage,
  reportReleaseDesktopReady,
  stopReleaseDesktopProductProvider,
  type ReleaseDesktopBootstrapStage,
} from "./product/releaseProvider";

const isOpenEvoDesktopOnlyBuild =
  import.meta.env.VITE_OPENEVO_DESKTOP_ONLY === "true";

function NavItem({ to, label }: { to: string; label: string }) {
  return (
    <NavLink
      to={to}
      end={to === "/"}
      className={({ isActive }) =>
        `rounded px-3 py-1 text-sm ${
          isActive ? "bg-slate-900 text-white" : "text-slate-700 hover:bg-slate-100"
        }`
      }
    >
      {label}
    </NavLink>
  );
}

function NavBar() {
  return (
    <nav className="border-b border-slate-200 bg-white">
      <div className="mx-auto flex max-w-7xl items-center justify-between px-4 py-2">
        <Link to="/" className="flex items-center gap-2 text-slate-900">
          <span
            className="inline-block h-3 w-3 rounded-full"
            style={{ background: "linear-gradient(135deg, #2563eb, #16a34a)" }}
          />
          <span className="font-semibold">OpenEvo Observability</span>
        </Link>
        <div className="flex items-center gap-1">
          <NavItem to="/openevo" label="OpenEvo" />
          <NavItem to="/" label="Dashboard" />
          <NavItem to="/tasks" label="Tasks" />
        </div>
      </div>
    </nav>
  );
}

export function SharedDashboardShell() {
  const location = useLocation();
  const queryClient = useQueryClient();

  // SSE → query cache invalidation
  useEffect(() => {
    const controller = new AbortController();
    subscribeOpenEvoEvents((event) => {
      const data = event.data || {};
      switch (event.type) {
        case "task.created":
        case "task.updated":
        case "task.completed":
          queryClient.invalidateQueries({ queryKey: ["tasks"] });
          if (data.task_id) {
            queryClient.invalidateQueries({ queryKey: ["task", data.task_id] });
          }
          break;
        case "session.state_changed":
          if (data.session_id) {
            queryClient.invalidateQueries({ queryKey: ["session", data.session_id] });
            queryClient.invalidateQueries({
              queryKey: ["session-completions", data.session_id],
            });
          }
          if (data.task_id) {
            queryClient.invalidateQueries({ queryKey: ["task", data.task_id] });
          }
          queryClient.invalidateQueries({ queryKey: ["topology"] });
          break;
        case "session.completion_added":
          if (data.session_id) {
            queryClient.invalidateQueries({
              queryKey: ["session-completions", data.session_id],
            });
          }
          break;
        case "ping":
        case "hello":
          break;
      }
    }, controller);
    return () => controller.abort();
  }, [queryClient]);

  return (
    <div className="flex min-h-full flex-col">
      <NavBar />
      <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-4">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/openevo" element={<OpenEvoDesktop />} />
          <Route path="/tasks" element={<TasksList />} />
          <Route path="/tasks/:taskId" element={<TaskDetail />} />
          <Route path="/sessions/:sessionId" element={<SessionDetail />} />
          <Route path="/compare" element={<Compare />} />
          <Route
            path="*"
            element={
              <div className="rounded-lg border border-slate-200 bg-white p-4 text-sm text-slate-500">
                Not found. Path: <code>{location.pathname}</code>
              </div>
            }
          />
        </Routes>
      </main>
      <footer className="border-t border-slate-200 bg-white px-4 py-2 text-center text-xs text-slate-500">
        OpenEvo Observability · local · read-only ·{" "}
        <a className="underline" href="/docs" target="_blank" rel="noreferrer">
          OpenAPI
        </a>
      </footer>
    </div>
  );
}

export function OpenEvoDesktopOnlyShell({
  provider,
  onInitialSnapshotFailed,
  onReady,
  openConnectionSettings = false,
  onConnectionSettingsOpened,
}: {
  provider?: DesktopProductProvider;
  onInitialSnapshotFailed?: (error: unknown) => void;
  onReady?: () => void;
  openConnectionSettings?: boolean;
  onConnectionSettingsOpened?: () => void;
}) {
  return (
    <Routes>
      <Route
        path="*"
        element={(
          <DesktopProductApp
            provider={provider}
            onInitialSnapshotFailed={onInitialSnapshotFailed}
            onReady={onReady}
            openConnectionSettings={openConnectionSettings}
            onConnectionSettingsOpened={onConnectionSettingsOpened}
          />
        )}
      />
    </Routes>
  );
}

export function AppShell({ desktopOnly = false, productProvider }: { desktopOnly?: boolean; productProvider?: DesktopProductProvider }) {
  return desktopOnly ? <OpenEvoDesktopOnlyShell provider={productProvider} /> : <SharedDashboardShell />;
}

type ReleaseDesktopStartupState =
  | { readonly status: "loading"; readonly retrying: boolean }
  | {
      readonly status: "committing";
      readonly provider: DesktopProductProvider;
      readonly generation: number;
    }
  | { readonly status: "ready"; readonly provider: DesktopProductProvider }
  | {
      readonly status: "failed";
      readonly stage: "bootstrap" | "readiness";
      readonly failure: ReleaseDesktopStartupFailure;
    };

type ReleaseDesktopStartupFailure = {
  readonly message: string;
  readonly nextAction: string | null;
};

function safeNativeHostFailure(error: unknown): ReleaseDesktopStartupFailure | null {
  if (typeof error !== "object" || error === null || Array.isArray(error)) return null;
  const record = error as Record<string, unknown>;
  if (
    Object.keys(record).some((key) => key !== "code" && key !== "message")
    || typeof record.code !== "string"
    || !/^[a-z][a-z0-9_]{2,63}$/.test(record.code)
    || typeof record.message !== "string"
    || record.message.length < 1
    || record.message.length > 768
    || /[^\x20-\x7e]/.test(record.message)
  ) {
    return null;
  }
  return {
    message: record.message,
    nextAction: record.code.includes("missing")
      || record.code.includes("packaged_")
      || record.code.includes("bundled_")
      ? "Reinstall OpenEvo Desktop, then try again."
      : "Retry startup. If the problem continues, restart OpenEvo Desktop.",
  };
}

function safeStartupFailure(error: unknown): ReleaseDesktopStartupFailure {
  if (error instanceof DesktopApiError) {
    return {
      message: "The local OpenEvo Desktop service reported a startup error.",
      nextAction: error.apiError.retryable
        ? "Retry startup. If the problem continues, restart OpenEvo Desktop."
        : "Restart OpenEvo Desktop. If the problem continues, install the latest version.",
    };
  }
  if (error instanceof DesktopContractError || error instanceof ContractVersionUnsupportedError) {
    return {
      message: error.message,
      nextAction: "Retry startup. If the problem continues, update OpenEvo Desktop.",
    };
  }
  const nativeFailure = safeNativeHostFailure(error);
  if (nativeFailure !== null) return nativeFailure;
  return {
    message: "The local OpenEvo Desktop service could not be started.",
    nextAction: "Retry startup. If the problem continues, restart OpenEvo Desktop.",
  };
}

type ReadonlySampleWorkspace = "research" | "evolution" | "system";

function ReleaseStartupSample({
  onRetry,
  onAddRemoteWorkspace,
  startupPending = false,
  retrying = false,
  failure = null,
  connectionRequested = false,
}: {
  onRetry: () => void;
  onAddRemoteWorkspace: () => void;
  startupPending?: boolean;
  retrying?: boolean;
  failure?: ReleaseDesktopStartupFailure | null;
  connectionRequested?: boolean;
}) {
  const [workspace, setWorkspace] = useState<ReadonlySampleWorkspace>("research");
  const [selectedSampleId, setSelectedSampleId] = useState<SampleScientificProjectId>(
    SAMPLE_SCIENTIFIC_PROJECT.id,
  );
  const selectedSample = sampleScientificProject(selectedSampleId);
  const sampleWorkspaces: ReadonlyArray<{
    readonly id: ReadonlySampleWorkspace;
    readonly label: string;
    readonly icon: typeof BookOpen;
  }> = [
    { id: "research", label: "Research", icon: BookOpen },
    { id: "evolution", label: "Evolution", icon: Sparkles },
    { id: "system", label: "System", icon: Activity },
  ];

  const handleWorkspaceKeyDown = (event: React.KeyboardEvent<HTMLElement>) => {
    if (!["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      return;
    }
    const tabs = Array.from(
      event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]'),
    );
    const current = tabs.indexOf(document.activeElement as HTMLButtonElement);
    if (current < 0 || tabs.length === 0) return;
    event.preventDefault();
    const next = event.key === "Home"
      ? 0
      : event.key === "End"
        ? tabs.length - 1
        : (
            current
            + (["ArrowRight", "ArrowDown"].includes(event.key) ? 1 : -1)
            + tabs.length
          ) % tabs.length;
    tabs[next]?.focus();
    tabs[next]?.click();
  };

  return (
    <div
      className="product-shell initial-sync-shell"
      data-testid="release-startup-sample"
      data-provider-kind="desktop_sidecar"
      data-system-maintenance-available="false"
    >
      <aside className="product-sidebar" aria-label="Primary navigation">
        <div className="product-brand" aria-label="OpenEvo Desktop">
          <span className="product-mark"><OpenEvoMark /></span>
          <span>OpenEvo</span>
        </div>
        <nav
          className="product-nav"
          role="tablist"
          aria-label="Demo views"
          aria-orientation="vertical"
          onKeyDown={handleWorkspaceKeyDown}
        >
          {sampleWorkspaces.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              type="button"
              role="tab"
              className={`product-nav-item ${workspace === id ? "active" : ""}`}
              aria-selected={workspace === id}
              tabIndex={workspace === id ? 0 : -1}
              onClick={() => setWorkspace(id)}
            >
              <Icon size={17} /> {label}
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div className="sidebar-foot-label">Demo progress</div>
          <div className="sidebar-revision">
            <CircleDot size={15} />
            <span>Generation {selectedSample.activeProjectHeadGeneration}</span>
          </div>
        </div>
      </aside>
      <div className="product-stage">
        <header className="product-topbar">
          <div className="project-switcher-wrap">
            <label htmlFor="startup-sample-project">Project</label>
            <div className="project-switcher-control">
              <select
                id="startup-sample-project"
                value={selectedSample.id}
                onChange={(event) => {
                  const selected = SAMPLE_SCIENTIFIC_PROJECTS.find(
                    (sample) => sample.id === event.target.value,
                  );
                  if (selected) setSelectedSampleId(selected.id);
                }}
              >
                {SAMPLE_SCIENTIFIC_PROJECTS.map((sample) => (
                  <option key={sample.id} value={sample.id}>
                    [Demo] {sample.name}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <button
            type="button"
            className="primary-button topbar-primary-action"
            onClick={onAddRemoteWorkspace}
          >
            <Plus size={16} /> Add remote workspace
          </button>
        </header>
        <main className="product-main">
          <div
            className="initial-sync-notice"
            role={startupPending ? "status" : "alert"}
            aria-live={startupPending ? "polite" : undefined}
          >
            <AlertCircle size={18} />
            <div>
              <strong>
                {startupPending
                  ? retrying ? "Retrying OpenEvo Desktop" : "Starting OpenEvo Desktop"
                  : "OpenEvo Desktop could not start"}
              </strong>
              {failure ? <span>{failure.message}</span> : null}
              {failure?.nextAction ? <span>{failure.nextAction}</span> : null}
              {connectionRequested ? (
                <span>Your remote workspace will open when OpenEvo Desktop is ready.</span>
              ) : null}
            </div>
            {startupPending ? (
              <button type="button" className="secondary-button" disabled>
                <LoaderCircle className="spin" size={15} /> {retrying ? "Retrying" : "Starting"}
              </button>
            ) : (
              <button type="button" className="secondary-button" onClick={onRetry}>
                <RefreshCw size={15} /> Retry
              </button>
            )}
          </div>
          <div className="initial-sync-sample">
            <SampleScientificProjectView
              workspace={workspace}
              project={selectedSample}
              onConnectRemote={onAddRemoteWorkspace}
            />
          </div>
        </main>
      </div>
    </div>
  );
}

export function ReleaseDesktopProductShell({
  createProvider = createReleaseDesktopProductProvider,
  stopProvider = stopReleaseDesktopProductProvider,
  reportStage = reportReleaseDesktopBootstrapStage,
  reportReady = reportReleaseDesktopReady,
}: {
  createProvider?: () => Promise<DesktopProductProvider>;
  stopProvider?: () => Promise<void>;
  reportStage?: (stage: ReleaseDesktopBootstrapStage) => Promise<void> | void;
  reportReady?: () => Promise<void>;
}) {
  const generation = useRef(0);
  const readinessGeneration = useRef<number | null>(null);
  const lifecycle = useRef<Promise<void>>(Promise.resolve());
  const startupInFlight = useRef(false);
  const [startup, setStartup] = useState<ReleaseDesktopStartupState>({ status: "loading", retrying: false });
  const [connectionRequested, setConnectionRequested] = useState(false);

  const reportStageBestEffort = useCallback((stage: ReleaseDesktopBootstrapStage): void => {
    try {
      void Promise.resolve(reportStage(stage)).catch(() => {});
    } catch {
      // Closed diagnostics cannot alter startup state or readiness authority.
    }
  }, [reportStage]);

  const enqueueLifecycle = useCallback((operation: () => Promise<void>): Promise<void> => {
    const next = lifecycle.current.catch(() => {}).then(operation);
    lifecycle.current = next.catch(() => {});
    return next;
  }, []);

  const cancelLifecycle = useCallback(() => {
    // Do not queue cancellation behind an in-flight bootstrap. Tauri's stop
    // command cancels and joins a native start that has not published yet.
    const cancellation = stopProvider().catch(() => {});
    lifecycle.current = Promise.all([lifecycle.current.catch(() => {}), cancellation]).then(() => {});
  }, [stopProvider]);

  const start = useCallback((retrying = false) => {
    if (startupInFlight.current) return;
    startupInFlight.current = true;
    const requestGeneration = generation.current + 1;
    generation.current = requestGeneration;
    setStartup({ status: "loading", retrying });
    void enqueueLifecycle(async () => {
      try {
        // Revoke the previous native session before requesting another
        // credential from the Tauri host.
        await stopProvider();
        if (generation.current !== requestGeneration) return;
        let provider: DesktopProductProvider;
        try {
          provider = await createProvider();
        } catch (error) {
          reportStageBestEffort("provider_create_failed");
          throw error;
        }
        reportStageBestEffort("provider_created");
        if (generation.current !== requestGeneration) {
          await stopProvider();
          return;
        }
        setStartup({ status: "committing", provider, generation: requestGeneration });
      } catch (error) {
        try {
          await stopProvider();
        } catch {
          // Native cleanup is bounded; startup remains explicitly retryable.
        }
        if (generation.current === requestGeneration) {
          setStartup({
            status: "failed",
            stage: "bootstrap",
            failure: safeStartupFailure(error),
          });
        }
      } finally {
        if (generation.current === requestGeneration) startupInFlight.current = false;
      }
    });
  }, [createProvider, enqueueLifecycle, reportStageBestEffort, stopProvider]);

  const reportCommittedProduct = useCallback((
    committingGeneration: number,
    provider: DesktopProductProvider,
  ) => {
    if (readinessGeneration.current === committingGeneration) return;
    readinessGeneration.current = committingGeneration;
    void enqueueLifecycle(async () => {
      if (generation.current !== committingGeneration) return;
      try {
        // Effects run only after React has committed the product shell. The
        // native marker therefore proves the packaged product UI, not merely
        // the bootstrap placeholder, reached the invoking main WebView.
        reportStageBestEffort("product_committed");
        await reportReady();
        if (generation.current === committingGeneration) {
          setStartup({ status: "ready", provider });
        }
      } catch (error) {
        try {
          await stopProvider();
        } catch {
          // Native cleanup is bounded; startup remains explicitly retryable.
        }
        if (generation.current === committingGeneration) {
          setStartup({
            status: "failed",
            stage: "readiness",
            failure: safeStartupFailure(error),
          });
        }
      }
    });
  }, [enqueueLifecycle, reportReady, reportStageBestEffort, stopProvider]);

  const reportInitialSnapshotFailed = useCallback((
    committingGeneration: number,
    error: unknown,
  ) => {
    if (generation.current !== committingGeneration) return;
    generation.current += 1;
    startupInFlight.current = false;
    reportStageBestEffort("initial_snapshot_failed");
    setStartup({
      status: "failed",
      stage: "readiness",
      failure: safeStartupFailure(error),
    });
    void enqueueLifecycle(async () => {
      try {
        await stopProvider();
      } catch {
        // Retry performs another bounded cleanup before creating a provider.
      }
    });
  }, [enqueueLifecycle, reportStageBestEffort, stopProvider]);

  const requestRemoteWorkspace = useCallback(() => {
    setConnectionRequested(true);
    if (startup.status === "failed") start(true);
  }, [start, startup.status]);

  const connectionSettingsOpened = useCallback(() => {
    setConnectionRequested(false);
  }, []);

  useEffect(() => {
    start();
    return () => {
      generation.current += 1;
      startupInFlight.current = false;
      cancelLifecycle();
    };
  }, [cancelLifecycle, start]);

  if (startup.status === "committing") {
    return (
      <OpenEvoDesktopOnlyShell
        provider={startup.provider}
        onInitialSnapshotFailed={(error) => reportInitialSnapshotFailed(
          startup.generation,
          error,
        )}
        onReady={() => reportCommittedProduct(startup.generation, startup.provider)}
        openConnectionSettings={connectionRequested}
        onConnectionSettingsOpened={connectionSettingsOpened}
      />
    );
  }
  if (startup.status === "ready") {
    return (
      <OpenEvoDesktopOnlyShell
        provider={startup.provider}
        openConnectionSettings={connectionRequested}
        onConnectionSettingsOpened={connectionSettingsOpened}
      />
    );
  }
  if (startup.status === "failed") {
    return (
      <ReleaseStartupSample
        onRetry={() => start(true)}
        onAddRemoteWorkspace={requestRemoteWorkspace}
        failure={startup.failure}
        connectionRequested={connectionRequested}
      />
    );
  }
  return (
    <ReleaseStartupSample
      onRetry={start}
      onAddRemoteWorkspace={requestRemoteWorkspace}
      startupPending
      retrying={startup.retrying}
      connectionRequested={connectionRequested}
    />
  );
}

// Keep this build-time branch at the entrypoint so Vite can drop shared
// dashboard code from OpenEvo-only bundles.
export default function App() {
  return isOpenEvoDesktopOnlyBuild ? (
    <ReleaseDesktopProductShell />
  ) : (
    <SharedDashboardShell />
  );
}
