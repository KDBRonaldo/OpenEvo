import { AlertCircle, LoaderCircle, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { Dashboard } from "./routes/Dashboard";
import { TasksList } from "./routes/TasksList";
import { TaskDetail } from "./routes/TaskDetail";
import { SessionDetail } from "./routes/SessionDetail";
import { Compare } from "./routes/Compare";
import { OpenEvoDesktop } from "./routes/OpenEvoDesktop";
import { subscribeOpenEvoEvents } from "./api/sse";
import { DesktopProductApp } from "./product/DesktopProductApp";
import type { DesktopProductProvider } from "./product/provider";
import {
  createReleaseDesktopProductProvider,
  reportReleaseDesktopReady,
  stopReleaseDesktopProductProvider,
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
  onReady,
}: {
  provider?: DesktopProductProvider;
  onReady?: () => void;
}) {
  return (
    <Routes>
      <Route path="*" element={<DesktopProductApp provider={provider} onReady={onReady} />} />
    </Routes>
  );
}

export function AppShell({ desktopOnly = false, productProvider }: { desktopOnly?: boolean; productProvider?: DesktopProductProvider }) {
  return desktopOnly ? <OpenEvoDesktopOnlyShell provider={productProvider} /> : <SharedDashboardShell />;
}

type ReleaseDesktopStartupState =
  | { readonly status: "loading" }
  | {
      readonly status: "committing";
      readonly provider: DesktopProductProvider;
      readonly generation: number;
    }
  | { readonly status: "ready"; readonly provider: DesktopProductProvider }
  | { readonly status: "failed" };

export function ReleaseDesktopProductShell({
  createProvider = createReleaseDesktopProductProvider,
  stopProvider = stopReleaseDesktopProductProvider,
  reportReady = reportReleaseDesktopReady,
}: {
  createProvider?: () => Promise<DesktopProductProvider>;
  stopProvider?: () => Promise<void>;
  reportReady?: () => Promise<void>;
}) {
  const generation = useRef(0);
  const readinessGeneration = useRef<number | null>(null);
  const lifecycle = useRef<Promise<void>>(Promise.resolve());
  const [startup, setStartup] = useState<ReleaseDesktopStartupState>({ status: "loading" });

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

  const start = useCallback(() => {
    const requestGeneration = generation.current + 1;
    generation.current = requestGeneration;
    setStartup({ status: "loading" });
    void enqueueLifecycle(async () => {
      try {
        // Revoke the previous native session before requesting another
        // credential from the Tauri host.
        await stopProvider();
        if (generation.current !== requestGeneration) return;
        const provider = await createProvider();
        if (generation.current !== requestGeneration) {
          await stopProvider();
          return;
        }
        setStartup({ status: "committing", provider, generation: requestGeneration });
      } catch {
        try {
          await stopProvider();
        } catch {
          // Native cleanup is bounded; startup remains explicitly retryable.
        }
        if (generation.current === requestGeneration) {
          setStartup({ status: "failed" });
        }
      }
    });
  }, [createProvider, enqueueLifecycle, stopProvider]);

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
        await reportReady();
        if (generation.current === committingGeneration) {
          setStartup({ status: "ready", provider });
        }
      } catch {
        try {
          await stopProvider();
        } catch {
          // Native cleanup is bounded; startup remains explicitly retryable.
        }
        if (generation.current === committingGeneration) {
          setStartup({ status: "failed" });
        }
      }
    });
  }, [enqueueLifecycle, reportReady, stopProvider]);

  useEffect(() => {
    start();
    return () => {
      generation.current += 1;
      cancelLifecycle();
    };
  }, [cancelLifecycle, start]);

  if (startup.status === "committing") {
    return (
      <OpenEvoDesktopOnlyShell
        provider={startup.provider}
        onReady={() => reportCommittedProduct(startup.generation, startup.provider)}
      />
    );
  }
  if (startup.status === "ready") {
    return <OpenEvoDesktopOnlyShell provider={startup.provider} />;
  }
  return (
    <div className="product-boot">
      {startup.status === "loading" ? (
        <div className="product-loading-row" role="status" aria-live="polite">
          <LoaderCircle className="spin" size={18} /> Starting OpenEvo Desktop...
        </div>
      ) : (
        <div className="blocking-state" role="alert">
          <span className="product-mark large"><AlertCircle size={22} /></span>
          <h1>OpenEvo Desktop could not start</h1>
          <p>The local service did not become ready. Retry startup to create a new secure session.</p>
          <button type="button" className="primary-button" onClick={start}>
            <RefreshCw size={16} /> Retry startup
          </button>
        </div>
      )}
    </div>
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
