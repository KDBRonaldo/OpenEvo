import {
  Activity,
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  BookOpen,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleDot,
  FolderOpen,
  FileText,
  History,
  LoaderCircle,
  PanelLeft,
  Play,
  Plus,
  RefreshCw,
  Server,
  Settings,
  ShieldCheck,
  Sparkles,
  X,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { DesktopApiErrorV2 } from "../api/v2/client";
import type { LogEntryV2 } from "../api/v2/logs";
import type {
  ProjectV2,
  RemoteProfileV2,
  RemoteWorkspaceProfileV2,
  ScienceProjectConfigV2,
  SuccessorTransitionV2,
  TaskV2,
} from "../api/v2/schemas";
import { OpenEvoMark } from "../components/OpenEvoMark";
import {
  LifecycleOperationPanelV2,
  coreOperationPanelModelV2,
  diagnosticPanelModelV2,
  lifecycleOperationPanelModelV2,
  servicePanelModelV2,
  taskPanelModelV2,
  transitionPanelModelV2,
} from "./LifecycleOperationPanelV2";
import {
  unavailableDesktopProductProviderV2,
  type DesktopProductProviderV2,
  type DesktopProductSnapshotV2,
  type ProductMutationIntentV2,
} from "./providerV2";

type Workspace = "research" | "evolution" | "system";

function withSessionDocumentEvolution(
  config: ScienceProjectConfigV2,
  task: ScienceProjectConfigV2["task"],
  targets: ScienceProjectConfigV2["evolution"]["targets"],
): ScienceProjectConfigV2 {
  return {
    ...config,
    task,
    evolution: { targets },
  } as ScienceProjectConfigV2;
}

export interface DesktopProductAppProps {
  readonly provider?: DesktopProductProviderV2;
  readonly onInitialSnapshotFailed?: (error: unknown) => void;
  readonly onReady?: () => void;
  readonly openConnectionSettings?: boolean;
  readonly onConnectionSettingsOpened?: () => void;
}

export function DesktopProductApp({
  provider = unavailableDesktopProductProviderV2,
  onInitialSnapshotFailed,
  onReady,
  openConnectionSettings = false,
  onConnectionSettingsOpened,
}: DesktopProductAppProps) {
  const [snapshot, setSnapshot] = useState<DesktopProductSnapshotV2 | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionStatus, setActionStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [workspace, setWorkspace] = useState<Workspace>("research");
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [connectionOpen, setConnectionOpen] = useState(false);
  const [projectOpen, setProjectOpen] = useState(false);
  const [projectEditing, setProjectEditing] = useState(false);
  const [taskLogs, setTaskLogs] = useState<Readonly<Record<string, readonly LogEntryV2[]>>>({});
  const [serviceLogs, setServiceLogs] = useState<Readonly<Record<string, readonly LogEntryV2[]>>>({});
  const readyReported = useRef(false);
  const initialFailureReported = useRef(false);
  const refreshSequence = useRef(0);
  const snapshotRef = useRef<DesktopProductSnapshotV2 | null>(null);

  const refresh = useCallback(async (): Promise<DesktopProductSnapshotV2 | null> => {
    const sequence = refreshSequence.current + 1;
    refreshSequence.current = sequence;
    try {
      const result = await provider.refresh();
      if (sequence !== refreshSequence.current) return null;
      if (result.status !== "fresh") {
        const error = new Error("OpenEvo Desktop state is not currently authoritative.");
        setLoadError(userMessageV2(result.status === "error" ? result.stream.error : error));
        if (snapshotRef.current === null && !initialFailureReported.current) {
          initialFailureReported.current = true;
          onInitialSnapshotFailed?.(error);
        }
        return null;
      }
      snapshotRef.current = result.snapshot;
      setSnapshot(result.snapshot);
      setLoadError(null);
      if (!readyReported.current) {
        readyReported.current = true;
        onReady?.();
      }
      return result.snapshot;
    } catch (error) {
      if (sequence !== refreshSequence.current) return null;
      setLoadError(userMessageV2(error));
      if (snapshotRef.current === null && !initialFailureReported.current) {
        initialFailureReported.current = true;
        onInitialSnapshotFailed?.(error);
      }
      return null;
    }
  }, [onInitialSnapshotFailed, onReady, provider]);

  useEffect(() => {
    void refresh();
    return provider.subscribe(() => void refresh());
  }, [provider, refresh]);

  useEffect(() => {
    if (!openConnectionSettings || snapshot === null) return;
    setConnectionOpen(true);
    onConnectionSettingsOpened?.();
  }, [onConnectionSettingsOpened, openConnectionSettings, snapshot]);

  useEffect(() => {
    if (snapshot === null) return;
    const activeTasks = snapshot.tasks.filter((task) => (
      ["admitted", "preparing", "running", "cancelling", "waiting_for_successor"].includes(task.state)
    ));
    if (activeTasks.length === 0) return;
    let retained = true;
    void Promise.all(activeTasks.map(async (task) => (
      [task.task_id, (await provider.loadTaskLogs(task.task_id, { limit: 100 })).items] as const
    ))).then((pages) => {
      if (!retained) return;
      setTaskLogs((current) => ({ ...current, ...Object.fromEntries(pages) }));
    }).catch(() => {
      // Timeline authority remains visible; explicit refresh reports a typed error.
    });
    return () => {
      retained = false;
    };
  }, [provider, snapshot]);

  const act = useCallback(async <T,>(
    operation: () => Promise<T>,
    successMessage?: string,
  ): Promise<{ value: T; snapshot: DesktopProductSnapshotV2 | null } | null> => {
    setBusy(true);
    setActionError(null);
    setActionStatus(null);
    try {
      const value = await operation();
      const refreshed = await refresh();
      if (successMessage) setActionStatus(successMessage);
      return { value, snapshot: refreshed };
    } catch (error) {
      setActionError(userMessageV2(error));
      await refresh();
      return null;
    } finally {
      setBusy(false);
    }
  }, [refresh]);

  if (snapshot === null) {
    return (
      <InitialV2View
        error={loadError}
        onRetry={() => void refresh()}
        onAddRemote={() => {
          setConnectionOpen(true);
          void refresh();
        }}
      />
    );
  }

  const activeProject = snapshot.projects.find(
    (project) => project.project_id === snapshot.state.active_project_id,
  ) ?? null;
  const displayedProject = activeProject;
  const activeProfile = snapshot.profiles.find(
    (profile) => profile.profile_id === snapshot.state.active_profile_id,
  ) ?? null;
  const connectedProfiles = snapshot.profiles.filter(isConnectedProfile);
  const generation = displayedProject?.active_project_head?.generation ?? 0;
  const lifecycleStates = provider.listLifecycleOperations();
  const coreOperations = provider.listCoreOperations();
  const diagnostics = provider.listDiagnostics();
  const mutationIntents = provider.listMutationIntents();
  const visibleOperationCount = lifecycleStates.length + coreOperations.length + diagnostics.length;
  const developmentAgentBridge = provider.featureFlags.includes("development_agent_bridge");

  const runProject = async (
    project: ProjectV2,
    task: ScienceProjectConfigV2["task"],
    selectedEvolutionTargets: ScienceProjectConfigV2["evolution"]["targets"],
  ): Promise<void> => {
    if (project.state !== "ready") return;
    setBusy(true);
    setActionError(null);
    setActionStatus(null);
    try {
      let currentSnapshot = snapshot;
      let currentProject = project;
      const nextConfig = developmentAgentBridge
        ? withSessionDocumentEvolution(project.config, task, selectedEvolutionTargets)
        : { ...project.config, task };
      if (JSON.stringify(nextConfig) !== JSON.stringify(project.config)) {
        await provider.updateProject(
          project.project_id,
          project.display_name,
          nextConfig,
          intentFor(currentSnapshot, "update-task-before-session"),
        );
        const refreshed = await refresh();
        if (refreshed === null) throw new Error("The updated task could not be reloaded before starting the session.");
        const updatedProject = refreshed.projects.find((candidate) => candidate.project_id === project.project_id);
        if (!updatedProject || updatedProject.state !== "ready") {
          throw new Error("The updated task is not ready for a new session yet.");
        }
        currentSnapshot = refreshed;
        currentProject = updatedProject;
      }
      const validation = await provider.validateProject(
        currentProject.project_id,
        intentFor(currentSnapshot, "validate-project"),
      );
      if (!validation.valid) {
        setActionError("The active remote registry rejected this project configuration. Correct the failed checks before running.");
        return;
      }
      const submittedTask = await provider.submitTask(currentProject.project_id, intentFor(currentSnapshot, "submit-task"));
      await refresh();
      setWorkspace("research");
      setSelectedTaskId(submittedTask.task_id);
      setActionStatus(developmentAgentBridge
        ? "The remote Session started. Codex and the selected evolution methods are running in the background."
        : "Task admitted with immutable Project Head and execution authority.");
    } catch (error) {
      setActionError(userMessageV2(error));
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="product-shell product-v2-shell" data-provider-kind="desktop_sidecar" data-api-version="2">
      <aside className="product-sidebar" aria-label="Primary navigation">
        <div className="product-brand" aria-label="OpenEvo Desktop">
          <span className="product-mark"><OpenEvoMark /></span>
          <span>OpenEvo</span>
        </div>
        <nav className="product-nav" aria-label="Workspace views">
          <WorkspaceButton active={workspace === "research"} onClick={() => { setWorkspace("research"); setSelectedTaskId(null); }} icon={BookOpen}>Research</WorkspaceButton>
          {activeProject && snapshot.tasks.some((task) => task.project_id === activeProject.project_id) ? (
            <div className="sidebar-sessions" aria-label={`${activeProject.display_name} sessions`}>
              <div className="sidebar-sessions-label">Sessions</div>
              {snapshot.tasks.filter((task) => task.project_id === activeProject.project_id).map((task, index) => (
                <button type="button" className={`sidebar-session-item ${workspace === "research" && selectedTaskId === task.task_id ? "active" : ""}`} key={task.task_id} onClick={() => { setWorkspace("research"); setSelectedTaskId(task.task_id); }}>
                  <span>{snapshot.runtimePresentation?.tasks[task.task_id]?.instruction?.title ?? `Session ${index + 1}`}</span>
                  <small>{task.state.replaceAll("_", " ")}</small>
                </button>
              ))}
            </div>
          ) : null}
          <WorkspaceButton active={workspace === "evolution"} onClick={() => setWorkspace("evolution")} icon={Sparkles}>Evolution</WorkspaceButton>
          <WorkspaceButton active={workspace === "system"} onClick={() => setWorkspace("system")} icon={Activity}>System</WorkspaceButton>
        </nav>
        <div className="sidebar-foot">
          <div className="sidebar-foot-label">
            Active Project Head
          </div>
          <div className="sidebar-revision"><CircleDot size={15} /><span>Generation {generation}</span></div>
        </div>
      </aside>

      <div className="product-stage">
        <header className="product-topbar">
          <div className="project-switcher-wrap">
            <label htmlFor="v2-project-switcher">Project</label>
            <div className="project-switcher-control">
              <select
                id="v2-project-switcher"
                value={displayedProject ? `project:${displayedProject.project_id}` : ""}
                onChange={(event) => {
                  const projectId = event.target.value.slice(8);
                  const project = snapshot.projects.find((candidate) => candidate.project_id === projectId);
                  if (!project) return;
                  setSelectedTaskId(null);
                  setWorkspace("research");
                  if (project.project_id === activeProject?.project_id) return;
                  void act(
                    () => provider.activateProject(project.project_id, intentFor(snapshot, "activate-project")),
                    `Switching to ${project.display_name}.`,
                  );
                }}
              >
                {snapshot.projects.length === 0 ? <option value="">No projects</option> : null}
                {snapshot.projects.map((project) => (
                  <option key={project.project_id} value={`project:${project.project_id}`}>{project.display_name}</option>
                ))}
              </select>
            </div>
            {connectedProfiles.length > 0 ? <button type="button" className="icon-button" aria-label="Create project" title="Create project" disabled={busy} onClick={() => { setProjectEditing(false); setProjectOpen(true); }}><Plus size={17} /></button> : null}
          </div>
          <div className="topbar-actions">
            {displayedProject === null && connectedProfiles.length > 0 ? (
              <button type="button" className="secondary-button" onClick={() => { setProjectEditing(false); setProjectOpen(true); }}>
                <FolderOpen size={15} /> New project
              </button>
            ) : null}
            {activeProfile && activeProfile.profile_kind === "system_openssh" ? (
              <>
                <div className={`connection-badge ${activeProfile.connection_state === "connected" ? "success" : "neutral"}`} title={`${activeProfile.display_name}: ${connectionLabel(activeProfile.connection_state)}`}><span className="status-dot" /><span>{activeProfile.display_name}</span><strong>{connectionLabel(activeProfile.connection_state)}</strong></div>
                <button type="button" className="icon-button" aria-label="Remote workspace settings" onClick={() => setConnectionOpen(true)}><PanelLeft size={17} /></button>
              </>
            ) : (
              <button type="button" className="primary-button topbar-primary-action" onClick={() => setConnectionOpen(true)}><Plus size={16} /> Add remote workspace</button>
            )}
          </div>
        </header>

        <main className="product-main">
          {loadError ? <Notice tone="error" title="Refresh failed" detail={loadError} /> : null}
          {actionError ? <Notice tone="error" title="Action could not be completed" detail={actionError} onDismiss={() => setActionError(null)} /> : null}
          {developmentAgentBridge ? (
            <Notice
              tone="warning"
              title="Real-agent development mode"
              detail="Agent replies come from the remote Codex CLI. Project and Session history are persisted by the remote development daemon; release authority and evolution remain disabled."
            />
          ) : null}
          {actionStatus ? <Notice tone="success" title={developmentAgentBridge ? "Development session updated" : "Remote authority updated"} detail={actionStatus} onDismiss={() => setActionStatus(null)} /> : null}
          {snapshot.stream.status !== "fresh" ? (
            <Notice tone="warning" title="Refreshing authoritative state" detail="Actions remain paused until Desktop reloads current remote state." />
          ) : null}

          {displayedProject === null ? (
            <EmptyProjectWorkspace
              connected={connectedProfiles.length > 0}
              onConnectRemote={() => setConnectionOpen(true)}
              onCreateProject={() => { setProjectEditing(false); setProjectOpen(true); }}
            />
          ) : workspace === "research" ? (
            <ResearchWorkspaceV2
              project={displayedProject}
              tasks={snapshot.tasks}
              transitions={snapshot.transitions}
              timelines={snapshot.timelines}
              taskLogs={taskLogs}
              artifacts={snapshot.artifacts}
              capability={snapshot.capability}
              runtimePresentation={snapshot.runtimePresentation}
              selectedTaskId={selectedTaskId}
              busy={busy}
              sessionEvolutionAvailable={developmentAgentBridge}
              onSelectTask={setSelectedTaskId}
              onOpenSettings={() => { setProjectEditing(true); setProjectOpen(true); }}
              onRun={(task, selectedEvolutionTargets) => void runProject(displayedProject, task, selectedEvolutionTargets)}
              onCancelTask={(task) => void act(
                () => provider.cancelTask(task.task_id, intentFor(snapshot, "cancel-task")),
                "Task cancellation requested.",
              )}
              onRetryTask={(task) => void act(
                () => provider.retryTask(task.task_id, intentFor(snapshot, "retry-task")),
                "A new infrastructure Attempt was requested under the same Task Admission.",
              )}
              onLoadTaskLogs={async (taskId) => {
                setBusy(true);
                setActionError(null);
                try {
                  const page = await provider.loadTaskLogs(taskId, { limit: 100 });
                  setTaskLogs((current) => ({ ...current, [taskId]: page.items }));
                } catch (error) {
                  setActionError(userMessageV2(error));
                } finally {
                  setBusy(false);
                }
              }}
              onRetryTransition={(transition) => void act(
                () => provider.retryTransition(transition.transition.successor_transition_id, intentFor(snapshot, "retry-transition")),
                "Successor transition retry requested.",
              )}
              onAbandonTransition={(transition) => void act(
                () => provider.abandonTransition(transition.transition.successor_transition_id, intentFor(snapshot, "abandon-transition")),
                "Successor transition abandonment requested.",
              )}
            />
          ) : workspace === "evolution" ? (
            <EvolutionWorkspaceV2
              project={displayedProject}
              snapshot={snapshot}
              provider={provider}
              busy={busy}
              onSave={(config) => void act(
                () => provider.updateProject(displayedProject.project_id, displayedProject.display_name, config, intentFor(snapshot, "save-evolution")),
                "Project configuration saved. Validate again before the next Task.",
              )}
            />
          ) : (
            <SystemWorkspaceV2
              snapshot={snapshot}
              activeProfile={activeProfile}
              busy={busy}
              onOpenConnections={() => setConnectionOpen(true)}
              onRestartService={(serviceId) => void act(
                () => provider.restartService(serviceId, intentFor(snapshot, "restart-service")),
                "Service restart requested.",
              )}
              serviceLogs={serviceLogs}
              onLoadServiceLogs={async (serviceId) => {
                setBusy(true);
                setActionError(null);
                try {
                  const page = await provider.loadServiceLogs(serviceId, { limit: 100 });
                  setServiceLogs((current) => ({ ...current, [serviceId]: page.items }));
                } catch (error) {
                  setActionError(userMessageV2(error));
                } finally {
                  setBusy(false);
                }
              }}
              onCleanupCaches={() => void act(
                () => provider.cleanupCaches(intentFor(snapshot, "cleanup-caches")),
                "Safe remote cache cleanup requested.",
              )}
              onCreateDiagnostic={() => void act(
                () => provider.createDiagnostic(
                  { scope: "system", resource_id: null },
                  intentFor(snapshot, "collect-system-diagnostic"),
                ),
                "System diagnostic collection requested.",
              )}
            />
          )}
        </main>
      </div>

      {visibleOperationCount > 0 ? (
        <aside className="v2-global-operations" aria-label="Active operations">
          <div className="v2-global-operations-heading">
            <strong>{visibleOperationCount} operation{visibleOperationCount === 1 ? "" : "s"}</strong>
            <span>Work continues safely if this panel or Desktop is closed.</span>
          </div>
          <div className="v2-global-operation-list">
            {lifecycleStates.map((state) => {
              const cancellationIntent = mutationIntents.find((candidate) => candidate.state === "reserved"
                && candidate.mutation_kind === "lifecycle_cancel"
                && candidate.resource_scope === `lifecycle_operation:${state.operation.operation_id}`);
              const intent = cancellationIntent ?? mutationIntents.find(
                (candidate) => candidate.accepted_operation_id === state.operation.operation_id
                  || candidate.completed_operation_ids.includes(state.operation.operation_id),
              );
              return (
                <LifecycleOperationPanelV2
                  key={state.operation.operation_id}
                  model={lifecycleOperationPanelModelV2(state, undefined, { unresolvedMutation: intent !== undefined })}
                  onCancel={state.operation.cancellable ? () => act(
                    () => provider.cancelLifecycleOperation(
                      state.operation.operation_id,
                      intentFor(snapshot, "cancel-lifecycle"),
                    ),
                    "Lifecycle cancellation requested.",
                  ).then(() => undefined) : undefined}
                  onLoadOlder={() => provider.loadOlderLifecycleLogs(state.operation.operation_id).then(() => {
                    setSnapshot((current) => current === null ? null : { ...current });
                  })}
                  onLoadLatest={() => provider.loadLatestLifecycleLogs(state.operation.operation_id).then(() => {
                    setSnapshot((current) => current === null ? null : { ...current });
                  })}
                  onResume={intent === undefined ? undefined : () => provider.resumeMutationIntent(intent.action_id).then(() => refresh()).then(() => undefined)}
                />
              );
            })}
            {coreOperations.map((operation) => {
              const intent = mutationIntents.find((candidate) => candidate.accepted_operation_id === operation.operation_id
                || candidate.completed_operation_ids.includes(operation.operation_id));
              return (
                <LifecycleOperationPanelV2
                  key={operation.operation_id}
                  model={{
                    ...coreOperationPanelModelV2(operation),
                    unresolvedMutation: intent !== undefined,
                  }}
                  onCancel={["queued", "running"].includes(operation.status) ? () => act(
                    () => provider.cancelCoreOperation(
                      operation.operation_id,
                      intentFor(snapshot, "cancel-core-operation"),
                    ),
                    "Core operation cancellation requested.",
                  ).then(() => undefined) : undefined}
                  onResume={intent === undefined ? undefined : () => provider.resumeMutationIntent(intent.action_id).then(() => refresh()).then(() => undefined)}
                />
              );
            })}
            {diagnostics.map((diagnostic) => {
              const intent = mutationIntents.find((candidate) => candidate.accepted_operation_id === diagnostic.diagnostic_id
                || candidate.completed_operation_ids.includes(diagnostic.diagnostic_id));
              return (
                <LifecycleOperationPanelV2
                  key={diagnostic.diagnostic_id}
                  model={{
                    ...diagnosticPanelModelV2(diagnostic),
                    unresolvedMutation: intent !== undefined,
                  }}
                  onResume={intent === undefined ? undefined : () => provider.resumeMutationIntent(intent.action_id).then(() => refresh()).then(() => undefined)}
                />
              );
            })}
          </div>
        </aside>
      ) : null}

      {connectionOpen ? (
        <RemoteWorkspaceSetupV2
          snapshot={snapshot}
          provider={provider}
          busy={busy}
          error={actionError}
          onClearError={() => setActionError(null)}
          onClose={() => setConnectionOpen(false)}
          onRefresh={refresh}
          onBusy={setBusy}
          onError={(error) => setActionError(userMessageV2(error))}
          onConnected={() => {
            setActionStatus("Remote workspace connected through the selected system OpenSSH alias.");
            setConnectionOpen(false);
          }}
        />
      ) : null}

      {projectOpen && connectedProfiles.length > 0 ? (
        <NewProjectDialogV2
          profile={connectedProfiles[0]!}
          project={projectEditing ? displayedProject : null}
          snapshot={snapshot}
          provider={provider}
          busy={busy}
          onBusy={setBusy}
          onClose={() => { setProjectOpen(false); setProjectEditing(false); }}
          onCreated={async () => {
            await refresh();
            setSelectedTaskId(null);
            setWorkspace("research");
            setProjectOpen(false);
            setProjectEditing(false);
            setActionStatus(developmentAgentBridge
              ? "Project created in local development state. Start a session to call the remote Codex CLI."
              : "Project creation started. Progress and process logs remain available in Operations.");
          }}
          onError={(error) => setActionError(userMessageV2(error))}
        />
      ) : null}
    </div>
  );
}

function InitialV2View({
  error,
  onRetry,
  onAddRemote,
}: {
  readonly error: string | null;
  readonly onRetry: () => void;
  readonly onAddRemote: () => void;
}) {
  return (
    <div className="product-shell initial-sync-shell">
      <aside className="product-sidebar">
        <div className="product-brand"><span className="product-mark"><OpenEvoMark /></span><span>OpenEvo</span></div>
      </aside>
      <div className="product-stage">
        <header className="product-topbar"><strong>OpenEvo Desktop</strong><button className="primary-button" type="button" onClick={onAddRemote}><Plus size={16} /> Add remote workspace</button></header>
        <main className="product-main">
          <Notice
            tone={error ? "error" : "info"}
            title={error ? "OpenEvo Desktop could not load its local state" : "Loading your workspace"}
            detail={error ?? "Verifying the packaged Desktop Local API v2 authority."}
            action={error ? <button className="secondary-button" type="button" onClick={onRetry}><RefreshCw size={15} /> Retry</button> : <LoaderCircle className="spin" size={18} />}
          />
          <section className="quiet-empty empty-project-state">
            <Server size={28} />
            <h1>No authoritative workspace loaded</h1>
            <p>Connect a remote OpenEvo daemon to load projects, sessions, artifacts, and workspace files.</p>
            <button className="primary-button" type="button" onClick={onAddRemote}><Plus size={16} /> Add remote workspace</button>
          </section>
        </main>
      </div>
    </div>
  );
}

function EmptyProjectWorkspace({
  connected,
  onConnectRemote,
  onCreateProject,
}: {
  readonly connected: boolean;
  readonly onConnectRemote: () => void;
  readonly onCreateProject: () => void;
}) {
  return (
    <section className="quiet-empty empty-project-state">
      <FolderOpen size={28} />
      <h1>No project yet</h1>
      <p>{connected
        ? "Create a project. Its workspace, sessions, and artifacts will be stored by the connected daemon."
        : "Connect a remote daemon before creating a project."}</p>
      <button className="primary-button" type="button" onClick={connected ? onCreateProject : onConnectRemote}>
        <Plus size={16} /> {connected ? "New project" : "Add remote workspace"}
      </button>
    </section>
  );
}

function RemoteWorkspaceSetupV2({
  snapshot,
  provider,
  busy,
  error,
  onClearError,
  onClose,
  onRefresh,
  onBusy,
  onError,
  onConnected,
}: {
  readonly snapshot: DesktopProductSnapshotV2;
  readonly provider: DesktopProductProviderV2;
  readonly busy: boolean;
  readonly error: string | null;
  readonly onClearError: () => void;
  readonly onClose: () => void;
  readonly onRefresh: () => Promise<DesktopProductSnapshotV2 | null>;
  readonly onBusy: (value: boolean) => void;
  readonly onError: (error: unknown) => void;
  readonly onConnected: () => void;
}) {
  const selectableHosts = snapshot.catalog.hosts.filter((host) => host.availability !== "unsupported");
  const [alias, setAlias] = useState(selectableHosts[0]?.ssh_host_alias ?? "");
  const [displayName, setDisplayName] = useState("Research server");
  const [manualAlias, setManualAlias] = useState(selectableHosts.length === 0);
  const dialogRef = useDialogBoundary(onClose);

  const mutate = async (operation: () => Promise<unknown>, close = false): Promise<void> => {
    onBusy(true);
    onClearError();
    try {
      await operation();
      await onRefresh();
      if (close) onConnected();
    } catch (caught) {
      onError(caught);
      await onRefresh();
    } finally {
      onBusy(false);
    }
  };

  const saveAndConnect = async (): Promise<void> => {
    if (alias.trim() === "" || displayName.trim() === "") return;
    onBusy(true);
    onClearError();
    try {
      const created = await provider.createProfile(
        displayName.trim(),
        alias.trim(),
        intentFor(snapshot, "create-profile"),
      );
      const refreshed = await onRefresh();
      if (refreshed === null) throw new Error("The new profile could not be reloaded.");
      await provider.connectProfile(created.profile_id, intentFor(refreshed, "connect-profile"));
      await onRefresh();
      onConnected();
    } catch (caught) {
      onError(caught);
      await onRefresh();
    } finally {
      onBusy(false);
    }
  };

  return (
    <div className="drawer-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) onClose();
    }}>
      <aside ref={dialogRef} className="settings-drawer v2-remote-drawer" role="dialog" aria-modal="true" aria-labelledby="v2-remote-title" tabIndex={-1}>
        <div className="drawer-head">
          <div><span className="panel-kicker">Remote workspace</span><h2 id="v2-remote-title">Configured SSH host</h2></div>
          <button className="icon-button" type="button" aria-label="Close remote workspace setup" onClick={onClose} disabled={busy}><X size={18} /></button>
        </div>
        <div className="drawer-content">
          {error ? <Notice tone="error" title="Connection action failed" detail={error} onDismiss={onClearError} /> : null}
          <section className="form-section">
            <div className="v2-section-heading"><div><h3>Use your OpenSSH configuration</h3><p>Desktop invokes the equivalent of <code>ssh alias</code>. OpenSSH remains authoritative for routing, user, identities, agent, Keychain, and trust policy.</p></div><button type="button" className="text-button" disabled={busy} onClick={() => void mutate(() => provider.rescanSshHosts(intentFor(snapshot, "rescan-hosts")))}><RefreshCw size={14} /> Rescan</button></div>
            {snapshot.catalog.warnings.length > 0 ? (
              <div className="v2-catalog-warning" role="status"><AlertCircle size={16} /><span><strong>Some configured hosts cannot be listed.</strong> You can still enter their literal SSH alias below.</span></div>
            ) : null}
            <label>Workspace name<input maxLength={256} value={displayName} onChange={(event) => setDisplayName(event.target.value)} /></label>
            {manualAlias ? (
              <label>SSH host alias<input autoFocus maxLength={128} value={alias} placeholder="gpu-lab" onChange={(event) => setAlias(event.target.value)} /></label>
            ) : (
              <label>SSH host alias<select value={alias} onChange={(event) => setAlias(event.target.value)}>{selectableHosts.map((host) => <option key={host.ssh_host_alias} value={host.ssh_host_alias}>{host.ssh_host_alias}{host.availability === "manual_entry_only" ? " — manual check required" : ""}</option>)}</select></label>
            )}
            <button type="button" className="text-button v2-manual-alias" onClick={() => setManualAlias((current) => !current)}>{manualAlias ? "Choose a listed SSH alias" : "Use another SSH alias"}</button>
          </section>

          {snapshot.profiles.length > 0 ? (
            <section className="form-section">
              <h3>Saved workspaces</h3>
              <div className="v2-profile-list">
                {snapshot.profiles.map((profile) => (
                  <ProfileSetupCardV2
                    key={profile.profile_id}
                    profile={profile}
                    alias={alias}
                    snapshot={snapshot}
                    busy={busy}
                    mutate={mutate}
                    provider={provider}
                  />
                ))}
              </div>
            </section>
          ) : null}
        </div>
        <div className="drawer-footer">
          <button type="button" className="secondary-button" onClick={onClose} disabled={busy}>Cancel</button>
          <button type="button" className="primary-button" disabled={busy || alias.trim() === "" || displayName.trim() === ""} onClick={() => void saveAndConnect()}>{busy ? <LoaderCircle className="spin" size={15} /> : <Server size={15} />} Save and connect</button>
        </div>
      </aside>
    </div>
  );
}

function ProfileSetupCardV2({
  profile,
  alias,
  snapshot,
  busy,
  mutate,
  provider,
}: {
  readonly profile: RemoteProfileV2;
  readonly alias: string;
  readonly snapshot: DesktopProductSnapshotV2;
  readonly busy: boolean;
  readonly mutate: (operation: () => Promise<unknown>, close?: boolean) => Promise<void>;
  readonly provider: DesktopProductProviderV2;
}) {
  if (profile.profile_kind === "legacy_explicit") {
    return (
      <article className="v2-profile-card legacy">
        <div><strong>{profile.display_name}</strong><span>Retained Preview profile · rebind required</span></div>
        <button type="button" className="secondary-button" disabled={busy || alias.trim() === ""} onClick={() => void mutate(() => provider.rebindProfile(profile.profile_id, alias.trim(), intentFor(snapshot, "rebind-profile")))}>Rebind to configured SSH host</button>
      </article>
    );
  }
  const trust = profile.trust;
  const cleanupRetryable = profile.connection_state === "failed"
    && profile.failure?.code === "ssh_cleanup_failed";
  const cleanupAuthorityLost = profile.connection_state === "failed"
    && profile.failure?.code === "ssh_cleanup_authority_lost";
  return (
    <article className="v2-profile-card">
      <div className="v2-profile-card-head"><div><strong>{profile.display_name}</strong><span><code>{profile.ssh_host_alias}</code> · {connectionLabel(profile.connection_state)}</span></div><span className={`state-pill ${profile.connection_state}`}>{profile.connection_state.replaceAll("_", " ")}</span></div>
      {profile.prompt ? <div className="v2-prompt-status" role="status"><LoaderCircle className="spin" size={15} /><span>A secure {profile.prompt.kind} prompt is open in macOS. Its response never enters this page.</span></div> : null}
      {profile.failure ? <p className="form-error" role="alert">{profile.failure.summary}</p> : null}
      {trust.state === "changed_key_blocked" || trust.state === "first_use_review" ? (
        <div className="v2-host-key-review">
          <strong>{trust.state === "changed_key_blocked" ? "Changed host key blocked" : "First host key review"}</strong>
          {trust.key_fingerprints.map((fingerprint) => <code key={`${fingerprint.role}-${fingerprint.sha256_fingerprint}`}>{fingerprint.role}: {fingerprint.algorithm} {fingerprint.sha256_fingerprint}</code>)}
          <div className="v2-card-actions">
            {trust.state === "changed_key_blocked" && trust.repair_support === "automatic_replacement_available" ? <button type="button" className="danger-button" disabled={busy} onClick={() => void mutate(() => provider.reviewHostKey(profile.profile_id, "replace_changed_key", intentFor(snapshot, "replace-host-key")))}>Replace changed key and reconnect</button> : null}
            {trust.state === "first_use_review" ? <button type="button" className="primary-button" disabled={busy} onClick={() => void mutate(() => provider.reviewHostKey(profile.profile_id, "accept_first_use", intentFor(snapshot, "accept-host-key")))}>Trust this host and connect</button> : null}
            <button type="button" className="text-button" disabled={busy} onClick={() => void mutate(() => provider.reviewHostKey(profile.profile_id, "reject", intentFor(snapshot, "reject-host-key")))}>Reject</button>
          </div>
        </div>
      ) : (
        <div className="v2-card-actions">
          {profile.connection_state === "connected" ? (
            <button type="button" className="secondary-button" disabled={busy} onClick={() => void mutate(() => provider.disconnectProfile(profile.profile_id, intentFor(snapshot, "disconnect-profile")))}>Disconnect</button>
          ) : cleanupRetryable ? (
            <button type="button" className="secondary-button" disabled={busy} onClick={() => void mutate(() => provider.disconnectProfile(profile.profile_id, intentFor(snapshot, "retry-disconnect-profile")))}>Retry disconnect</button>
          ) : cleanupAuthorityLost ? (
            <span>Administrator action is required before this workspace can reconnect.</span>
          ) : (
            <button type="button" className="secondary-button" disabled={busy || ["connecting", "bootstrapping", "negotiating", "prompt_pending"].includes(profile.connection_state)} onClick={() => void mutate(() => provider.connectProfile(profile.profile_id, intentFor(snapshot, "connect-profile")))}>Connect</button>
          )}
        </div>
      )}
    </article>
  );
}

function NewProjectDialogV2({
  profile,
  project,
  snapshot,
  provider,
  busy,
  onBusy,
  onClose,
  onCreated,
  onError,
}: {
  readonly profile: RemoteWorkspaceProfileV2;
  readonly project: ProjectV2 | null;
  readonly snapshot: DesktopProductSnapshotV2;
  readonly provider: DesktopProductProviderV2;
  readonly busy: boolean;
  readonly onBusy: (value: boolean) => void;
  readonly onClose: () => void;
  readonly onCreated: () => Promise<void>;
  readonly onError: (error: unknown) => void;
}) {
  const [displayName, setDisplayName] = useState(project?.display_name ?? "New research project");
  const [title, setTitle] = useState(project?.config.task.title ?? "Research task");
  const [objective, setObjective] = useState(project?.config.task.objective ?? "");
  const [workspaceKind, setWorkspaceKind] = useState<"scratch" | "native_folder_snapshot">(project?.config.workspace.kind ?? "scratch");
  const [workspaceDisplayName, setWorkspaceDisplayName] = useState(project?.config.workspace.display_name ?? "Research workspace");
  const [executionMode, setExecutionMode] = useState<ScienceProjectConfigV2["execution"]["mode"]>(project?.config.execution.mode ?? "codex_subscription_transcript");
  const [selectedSourceDisplayName, setSelectedSourceDisplayName] = useState<string | null>(null);
  const [sourceActionId, setSourceActionId] = useState<string | null>(null);
  const closedRef = useRef(false);
  const dialogRef = useDialogBoundary(onClose);
  const baseDraftValid = displayName.trim() !== "" && title.trim() !== "" && objective.trim() !== "";
  const valid = baseDraftValid
    && (workspaceKind === "scratch" || sourceActionId !== null || project !== null);

  const chooseFolder = async (): Promise<void> => {
    const actionId = actionIdV2("select-workspace");
    setSourceActionId(actionId);
    onBusy(true);
    try {
      const config = scienceProjectConfig(
        title,
        objective,
        "native_folder_snapshot",
        workspaceDisplayName,
        executionMode,
      );
      const source = await provider.selectNativeWorkspace({
        kind: "native_folder_snapshot",
        actionId,
        streamEpoch: snapshot.stream.epoch,
        draft: {
          profileId: profile.profile_id,
          displayName: displayName.trim(),
          config,
        },
        profileAuthority: {
          profileId: profile.profile_id,
          connectionGeneration: profile.connection_generation,
          etag: profile.etag,
        },
      });
      if (closedRef.current) {
        await provider.settleNativeWorkspace(actionId, "discard").catch(() => {});
        return;
      }
      setWorkspaceKind("native_folder_snapshot");
      setSelectedSourceDisplayName(source.display_name);
    } catch (error) {
      await provider.settleNativeWorkspace(actionId, "discard").catch(() => {});
      if (!closedRef.current) {
        setSourceActionId(null);
        onError(error);
      }
    } finally {
      onBusy(false);
    }
  };

  const create = async (): Promise<void> => {
    if (!valid) return;
    const actionId = workspaceKind === "native_folder_snapshot" && project === null ? sourceActionId! : actionIdV2(project ? "update-project" : "create-project");
    const config = scienceProjectConfig(
      title,
      objective,
      workspaceKind,
      workspaceDisplayName,
      executionMode,
    );
    onBusy(true);
    try {
      if (project) {
        await provider.updateProject(project.project_id, displayName.trim(), { ...config, evolution: project.config.evolution }, { actionId, streamEpoch: snapshot.stream.epoch });
      } else {
        await provider.createProject({
          profileId: profile.profile_id,
          displayName: displayName.trim(),
          config,
        }, { actionId, streamEpoch: snapshot.stream.epoch });
      }
      await onCreated();
    } catch (error) {
      onError(error);
    } finally {
      onBusy(false);
    }
  };

  const close = async (): Promise<void> => {
    closedRef.current = true;
    if (sourceActionId !== null) {
      await provider.cancelNativeWorkspace(sourceActionId).catch(() => {});
      await provider.settleNativeWorkspace(sourceActionId, "discard").catch(() => {});
    }
    onClose();
  };

  return (
    <div className="v2-modal-backdrop" role="presentation">
      <section ref={dialogRef} className="v2-modal" role="dialog" aria-modal="true" aria-labelledby="new-project-title" tabIndex={-1}>
        <div className="drawer-head"><div><span className="panel-kicker">Remote project</span><h2 id="new-project-title">{project ? "Edit science project" : "Create science project"}</h2></div><button type="button" className="icon-button" aria-label="Close project setup" onClick={() => void close()}><X size={18} /></button></div>
        <div className="drawer-content">
          <section className="form-section">
            <label>Project name<input maxLength={256} value={displayName} disabled={sourceActionId !== null} onChange={(event) => setDisplayName(event.target.value)} /></label>
            <label>Task title<input maxLength={256} value={title} disabled={sourceActionId !== null} onChange={(event) => setTitle(event.target.value)} /></label>
            <label>Task objective<textarea rows={7} maxLength={65_536} value={objective} disabled={sourceActionId !== null} onChange={(event) => setObjective(event.target.value)} placeholder="Describe the scientific result the agent should produce." /></label>
          </section>
          <section className="form-section">
            <h3>Workspace snapshot</h3>
            <div className="v2-source-choice"><button type="button" className={workspaceKind === "scratch" ? "selected" : ""} disabled={sourceActionId !== null || project !== null} onClick={() => { setWorkspaceKind("scratch"); setSourceActionId(null); setSelectedSourceDisplayName(null); setWorkspaceDisplayName("Research workspace"); }}>New scratch workspace</button><button type="button" className={workspaceKind === "native_folder_snapshot" ? "selected" : ""} disabled={!baseDraftValid || sourceActionId !== null || project !== null} onClick={() => void chooseFolder()}><FolderOpen size={15} /> Choose folder snapshot</button></div>
            <p className="form-help">{project ? `${project.config.workspace.display_name} · the immutable workspace source cannot be replaced from project settings.` : workspaceKind === "native_folder_snapshot" ? selectedSourceDisplayName ?? "Preparing selected workspace…" : "Core will create an immutable empty Workspace Snapshot."}</p>
          </section>
          <section className="form-section">
            <h3>Execution</h3>
            <div className="v2-source-choice">
              <button type="button" className={executionMode === "codex_subscription_transcript" ? "selected" : ""} disabled={sourceActionId !== null} onClick={() => setExecutionMode("codex_subscription_transcript")}>Codex Subscription</button>
              <button type="button" className={executionMode === "self-deployed" ? "selected" : ""} disabled={sourceActionId !== null} onClick={() => setExecutionMode("self-deployed")}>Self-Deployed</button>
            </div>
            <div className="agent-note"><ShieldCheck size={17} /><span>{executionMode === "self-deployed" ? "Codex → Core Gateway → managed vLLM · Qwen3 0.6B · exact release profile" : "Codex Subscription · transcript capture · gpt-5.3-codex-spark · high effort"}</span></div>
            {executionMode === "self-deployed" ? <p className="form-help">Requires a supported NVIDIA GPU, NVIDIA Container Toolkit, Docker Engine, and at least 30 GiB free storage. OpenEvo prepares the pinned image and exact model snapshot on first run.</p> : null}
          </section>
        </div>
        <div className="drawer-footer"><button type="button" className="secondary-button" onClick={() => void close()}>Cancel</button><button type="button" className="primary-button" onClick={() => void create()} disabled={busy || !valid}>{busy ? <LoaderCircle className="spin" size={15} /> : project ? <Settings size={15} /> : <Plus size={15} />} {project ? "Save project" : "Create project"}</button></div>
      </section>
    </div>
  );
}

function ResearchWorkspaceV2({
  project,
  tasks,
  transitions,
  timelines,
  taskLogs,
  artifacts,
  capability,
  runtimePresentation,
  selectedTaskId,
  busy,
  sessionEvolutionAvailable,
  onSelectTask,
  onOpenSettings,
  onRun,
  onCancelTask,
  onRetryTask,
  onLoadTaskLogs,
  onRetryTransition,
  onAbandonTransition,
}: {
  readonly project: ProjectV2;
  readonly tasks: readonly TaskV2[];
  readonly transitions: Readonly<Record<string, SuccessorTransitionV2>>;
  readonly timelines: DesktopProductSnapshotV2["timelines"];
  readonly taskLogs: Readonly<Record<string, readonly LogEntryV2[]>>;
  readonly artifacts: DesktopProductSnapshotV2["artifacts"];
  readonly capability: DesktopProductSnapshotV2["capability"];
  readonly runtimePresentation: DesktopProductSnapshotV2["runtimePresentation"];
  readonly selectedTaskId: string | null;
  readonly busy: boolean;
  readonly sessionEvolutionAvailable: boolean;
  readonly onSelectTask: (taskId: string | null) => void;
  readonly onOpenSettings: () => void;
  readonly onRun: (
    task: ScienceProjectConfigV2["task"],
    selectedEvolutionTargets: ScienceProjectConfigV2["evolution"]["targets"],
  ) => void;
  readonly onCancelTask: (task: TaskV2) => void;
  readonly onRetryTask: (task: TaskV2) => void;
  readonly onLoadTaskLogs: (taskId: string) => void | Promise<void>;
  readonly onRetryTransition: (transition: SuccessorTransitionV2) => void;
  readonly onAbandonTransition: (transition: SuccessorTransitionV2) => void;
}) {
  const projectTasks = tasks.filter((task) => task.project_id === project.project_id);
  const selectedTask = projectTasks.find((task) => task.task_id === selectedTaskId) ?? null;
  const observedTask = selectedTask ?? projectTasks[0] ?? null;
  const activeTask = projectTasks.find((task) => ["admitted", "preparing", "running", "cancelling", "waiting_for_successor"].includes(task.state)) ?? null;
  useEffect(() => {
    if (selectedTaskId !== null && !projectTasks.some((task) => task.task_id === selectedTaskId)) onSelectTask(null);
    if (selectedTaskId === null && activeTask !== null) onSelectTask(activeTask.task_id);
  }, [activeTask?.task_id, onSelectTask, project.project_id, selectedTaskId, tasks]);
  const ready = project.state === "ready" && project.active_project_head !== null && project.admission_etag !== null;
  const [taskTitle, setTaskTitle] = useState(project.config.task.title);
  const [taskObjective, setTaskObjective] = useState(project.config.task.objective);
  const sessionEvolutionCapabilities = useMemo(() => (
    capability?.project_id === project.project_id
      ? capability.capabilities.targets
        .filter((target) => target.exposure === "desktop")
        .map((target) => ({
          ...target,
          methods: target.methods.filter((method) => method.support?.overall !== "unsupported" && method.support?.overall !== "unavailable"),
        }))
        .filter((target) => target.methods.length > 0)
      : []
  ), [capability, project.project_id]);
  const initialSessionEvolutionTargets = useCallback(() => Object.fromEntries(
    sessionEvolutionCapabilities.map((target) => {
      const current = project.config.evolution.targets[target.target_id];
      const selectedMethod = target.methods.find((method) => method.method_id === current?.method)
        ?? target.methods.find((method) => method.method_id === target.effective_default_method_id)
        ?? target.methods[0]!;
      let defaultConfig: ScienceProjectConfigV2["evolution"]["targets"][string]["config"] = {};
      try { defaultConfig = JSON.parse(selectedMethod.default_config_json) as typeof defaultConfig; } catch { defaultConfig = {}; }
      return [target.target_id, {
        enabled: current?.enabled === true,
        method: selectedMethod.method_id,
        config: current?.method === selectedMethod.method_id ? current.config : defaultConfig,
      }];
    }),
  ) as ScienceProjectConfigV2["evolution"]["targets"], [project.config.evolution.targets, sessionEvolutionCapabilities]);
  const [selectedEvolutionTargets, setSelectedEvolutionTargets] = useState<ScienceProjectConfigV2["evolution"]["targets"]>(
    initialSessionEvolutionTargets,
  );
  useEffect(() => {
    setTaskTitle(project.config.task.title);
    setTaskObjective(project.config.task.objective);
    setSelectedEvolutionTargets(initialSessionEvolutionTargets());
  }, [initialSessionEvolutionTargets, project.project_id, project.project_config_sha256]);
  const selectedEvolutionCount = Object.values(selectedEvolutionTargets).filter((target) => target.enabled).length;
  const normalizedTask = {
    title: taskTitle.trim(),
    objective: taskObjective.trim(),
  };
  const taskValid = normalizedTask.title.length > 0 && normalizedTask.objective.length > 0;
  if (selectedTask) {
    const transition = selectedTask.successor_transition
      ? transitions[selectedTask.successor_transition.successor_transition_id] ?? null
      : null;
    const taskContent = runtimePresentation?.tasks[selectedTask.task_id]?.instruction
      ?? (selectedTask.admission.project_config_sha256 === project.project_config_sha256
        ? project.config.task
        : null);
    return (
      <div className="workspace-stack session-detail-workspace" data-testid="session-detail-workspace">
        <div className="session-detail-navigation">
          <button type="button" className="session-back-button" onClick={() => onSelectTask(null)}>
            <ArrowLeft size={16} />
            Back to {project.display_name}
          </button>
        </div>
        <TaskAuthorityCardV2
          task={selectedTask}
          taskContent={taskContent}
          presentation={runtimePresentation?.tasks[selectedTask.task_id]}
          artifacts={artifacts}
          artifactPresentation={runtimePresentation?.artifacts}
          transition={transition}
          timeline={timelines[selectedTask.task_id] ?? []}
          logs={taskLogs[selectedTask.task_id] ?? []}
          busy={busy}
          onCancel={() => onCancelTask(selectedTask)}
          onRetry={() => onRetryTask(selectedTask)}
          onLoadLogs={() => onLoadTaskLogs(selectedTask.task_id)}
          onRetryTransition={() => transition && onRetryTransition(transition)}
          onAbandonTransition={() => transition && onAbandonTransition(transition)}
        />
        <details className="v2-authority-details session-authority-details">
          <summary>View this Session's pinned authority</summary>
          <TaskPinnedAuthorityCardsV2 task={selectedTask} />
        </details>
      </div>
    );
  }
  return (
    <div className="workspace-stack" data-testid="research-workspace">
      <div className="workspace-heading">
        <div><p className="eyebrow">Research</p><h1>{project.display_name}</h1><p>Prepare one task at a time against the current Project Head.</p></div>
        <div className="heading-actions"><button className="secondary-button" type="button" onClick={onOpenSettings}><Settings size={16} /> Edit project</button><button type="button" className="primary-button" disabled={busy || !ready || !taskValid} onClick={() => onRun(normalizedTask, selectedEvolutionTargets)}>{busy ? <LoaderCircle className="spin" size={15} /> : <Play size={15} fill="currentColor" />} Start session</button></div>
      </div>
      {!ready ? <div className="disabled-reason"><AlertCircle size={14} /><span><strong>Next task is not ready.</strong> The current successor, settings, workspace, or runtime transition must finish before Core can admit another Task.</span></div> : null}
      <div className="research-grid">
      <section className="product-panel task-panel">
        <div className="panel-heading"><div><span className="panel-kicker">Task draft</span><h2>What should the agent do next?</h2></div><span className={`state-pill ${project.state}`}>{project.state.replaceAll("_", " ")}</span></div>
        <div className="next-task-fields">
          <label>Task title<input maxLength={256} value={taskTitle} disabled={busy} onChange={(event) => setTaskTitle(event.target.value)} /></label>
          <label>Task instructions<textarea rows={6} maxLength={65_536} value={taskObjective} disabled={busy} onChange={(event) => setTaskObjective(event.target.value)} /></label>
          <p className="form-help">Starting the session saves these instructions and runs them as the next task.</p>
        </div>
        {sessionEvolutionAvailable ? <fieldset className="session-evolution-picker" disabled={busy}>
          <legend>Evolution after this session <span>Optional · select any number</span></legend>
          <div className="session-evolution-options">
            {sessionEvolutionCapabilities.map((target) => {
              const selection = selectedEvolutionTargets[target.target_id]!;
              return <article key={target.target_id} className={selection.enabled ? "selected" : ""}>
                <label>
                  <input type="checkbox" checked={selection.enabled} onChange={(event) => {
                    setSelectedEvolutionTargets((current) => ({
                      ...current,
                      [target.target_id]: { ...selection, enabled: event.target.checked },
                    }));
                  }} />
                  <span><strong>{target.display_name}</strong><small>{target.description}</small></span>
                </label>
                {target.methods.length > 1 ? <select aria-label={`${target.display_name} method`} value={selection.method ?? ""} onChange={(event) => {
                  const method = target.methods.find((candidate) => candidate.method_id === event.target.value)!;
                  let defaultConfig: ScienceProjectConfigV2["evolution"]["targets"][string]["config"] = {};
                  try { defaultConfig = JSON.parse(method.default_config_json) as typeof defaultConfig; } catch { defaultConfig = {}; }
                  setSelectedEvolutionTargets((current) => ({
                    ...current,
                    [target.target_id]: { enabled: true, method: method.method_id, config: defaultConfig },
                  }));
                }}>{target.methods.map((method) => <option key={method.method_id} value={method.method_id}>{method.display_name}</option>)}</select> : null}
              </article>;
            })}
          </div>
          <p>{selectedEvolutionCount === 0
            ? "No evolution will run after this session."
            : `${selectedEvolutionCount} evolution method${selectedEvolutionCount === 1 ? "" : "s"} will run after the agent replies.`}</p>
        </fieldset> : null}
        {!taskValid ? <p className="form-error" role="status">Enter both a task title and task instructions.</p> : null}
        <div className="brief-footer"><div><span>Mode</span><strong>{project.config.execution.mode === "codex_subscription_transcript" ? "Codex Subscription" : "Self-deployed"}</strong></div><div><span>Capture</span><strong>Session transcript</strong></div><div><span>Evolution</span><strong>{sessionEvolutionAvailable ? selectedEvolutionCount : Object.values(project.config.evolution.targets).filter((target) => target.enabled).length} selected</strong></div></div>
      </section>
      <section className="product-panel active-run-panel">
        <div className="panel-heading"><div><span className="panel-kicker">{observedTask ? "Selected session" : "Active session"}</span><h2>{observedTask ? runtimePresentation?.tasks[observedTask.task_id]?.instruction?.title ?? observedTask.task_id : "No session selected"}</h2></div>{observedTask ? <span className={`state-pill ${observedTask.state}`}>{observedTask.state.replaceAll("_", " ")}</span> : <span className="muted-pill">Ready</span>}</div>
        {observedTask ? <><div className="revision-pin"><div><span>Pinned context</span><strong>Project Head {observedTask.admission.predecessor_project_head.generation}</strong></div><ArrowRight size={16} /><div><span>Admission source</span><strong>Immutable Task Admission</strong></div><span className={`state-pill ${observedTask.state}`}>{observedTask.state.replaceAll("_", " ")}</span></div><p className="v2-session-summary">{runtimePresentation?.tasks[observedTask.task_id]?.instruction?.objective ?? "The historical task text is not included in this authority response."}</p><button type="button" className="text-button" onClick={() => onSelectTask(observedTask.task_id)}>Open session result <ArrowRight size={14} /></button></> : <div className="quiet-empty"><Play size={22} /><p>Start a session when the remote workspace is ready.</p></div>}
      </section>
      </div>
      <ProjectWorkspacePanelV2 workspace={runtimePresentation?.workspaces?.[project.project_id]} />
      <section className="history-section"><div className="section-heading"><div><History size={17} /><h2>Session history</h2></div><span>{projectTasks.length} total</span></div>{projectTasks.length ? <TaskHistoryTableV2 tasks={projectTasks} presentation={runtimePresentation?.tasks} selectedTaskId={selectedTaskId} transitions={transitions} onOpenTask={onSelectTask} /> : <div className="empty-row">Completed and active sessions will appear here.</div>}</section>
      {project.active_project_head ? <details className="v2-authority-details"><summary>View immutable project authority</summary><AuthorityCardsV2 project={project} /></details> : null}
    </div>
  );
}

function ProjectWorkspacePanelV2({
  workspace,
}: {
  readonly workspace: NonNullable<
    NonNullable<DesktopProductSnapshotV2["runtimePresentation"]>["workspaces"]
  >[string] | undefined;
}) {
  const entries = workspace?.entries ?? [];
  const files = entries.filter((entry) => entry.kind === "file");
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  useEffect(() => {
    if (selectedPath !== null && files.some((entry) => entry.path === selectedPath)) return;
    setSelectedPath(files[0]?.path ?? null);
  }, [files, selectedPath]);
  const selected = files.find((entry) => entry.path === selectedPath) ?? null;
  return (
    <section className="product-panel project-workspace-panel" data-testid="project-workspace-panel">
      <div className="panel-heading">
        <div><span className="panel-kicker">Persistent remote workspace</span><h2>Project files</h2></div>
        <span className="muted-pill">{files.length} files</span>
      </div>
      {entries.length === 0 ? (
        <div className="project-workspace-empty"><FolderOpen size={24} /><div><strong>This workspace is empty.</strong><p>Start a Session and ask Codex to create files. They will remain on your server for later Sessions.</p></div></div>
      ) : (
        <div className="project-workspace-browser">
          <div className="project-workspace-tree" aria-label="Project workspace files">
            {entries.map((entry) => (
              <button
                type="button"
                key={`${entry.kind}-${entry.path}`}
                className={entry.path === selectedPath ? "selected" : ""}
                disabled={entry.kind !== "file"}
                onClick={() => setSelectedPath(entry.path)}
              >
                {entry.kind === "directory" ? <FolderOpen size={15} /> : <FileText size={15} />}
                <span><strong>{entry.path}</strong><small>{entry.kind === "file" ? formatBytes(entry.byteSize) : entry.kind}</small></span>
              </button>
            ))}
          </div>
          <div className="project-workspace-preview">
            {selected ? <>
              <header><div><FileText size={15} /><strong>{selected.path}</strong></div><small>{selected.mediaType ?? "unknown"} · {formatBytes(selected.byteSize)}</small></header>
              {selected.content !== null ? <pre>{selected.content}</pre> : <p className="v2-empty-copy">This file is binary, unreadable, or too large for the bounded browser preview.</p>}
            </> : <p className="v2-empty-copy">Select a readable file to preview it.</p>}
          </div>
        </div>
      )}
      {workspace?.truncated ? <p className="form-help">The server workspace contains more data than the bounded preview can display.</p> : null}
    </section>
  );
}

function TaskPinnedAuthorityCardsV2({ task }: { readonly task: TaskV2 }) {
  const head = task.admission.predecessor_project_head;
  return (
    <section className="v2-authority-grid" aria-label="Session pinned immutable authority">
      <IdentityCard title="Project Head" id={head.project_head_id} detail={`Generation ${head.generation}`} digest={head.manifest_sha256} />
      <IdentityCard title="Evolution Revision" id={head.evolution_revision.evolution_revision_id} detail={`${head.evolution_revision.artifact_count} artifacts`} digest={head.evolution_revision.manifest_sha256} />
      <IdentityCard title="Runtime Context Snapshot" id={head.runtime_context_snapshot.runtime_context_snapshot_id} detail="Pinned session context" digest={head.runtime_context_snapshot.manifest_sha256} />
      <IdentityCard title="Effective Execution Snapshot" id={head.effective_execution_snapshot.effective_execution_snapshot_id} detail={`Producer ${head.effective_execution_snapshot.producer_id}`} digest={head.effective_execution_snapshot.snapshot_sha256} />
      <IdentityCard title="Workspace Snapshot" id={head.workspace_snapshot.workspace_snapshot_id} detail={`${head.workspace_snapshot.entry_count} entries`} digest={head.workspace_snapshot.manifest_sha256} />
    </section>
  );
}

function AuthorityCardsV2({ project }: { readonly project: ProjectV2 }) {
  const head = project.active_project_head!;
  return (
    <section className="v2-authority-grid" aria-label="Active immutable authority">
      <IdentityCard title="Project Head" id={head.project_head_id} detail={`Generation ${head.generation}`} digest={head.manifest_sha256} />
      <IdentityCard title="Evolution Revision" id={head.evolution_revision.evolution_revision_id} detail={`${head.evolution_revision.artifact_count} artifacts`} digest={head.evolution_revision.manifest_sha256} />
      <IdentityCard title="Runtime Context Snapshot" id={head.runtime_context_snapshot.runtime_context_snapshot_id} detail="Materialized next-session context" digest={head.runtime_context_snapshot.manifest_sha256} />
      <IdentityCard title="Effective Execution Snapshot" id={head.effective_execution_snapshot.effective_execution_snapshot_id} detail={`Producer ${head.effective_execution_snapshot.producer_id}`} digest={head.effective_execution_snapshot.snapshot_sha256} />
      <IdentityCard title="Workspace Snapshot" id={head.workspace_snapshot.workspace_snapshot_id} detail={`${head.workspace_snapshot.entry_count} entries`} digest={head.workspace_snapshot.manifest_sha256} />
    </section>
  );
}

function TaskHistoryTableV2({ tasks, presentation, selectedTaskId, transitions, onOpenTask }: {
  readonly tasks: readonly TaskV2[];
  readonly presentation: NonNullable<DesktopProductSnapshotV2["runtimePresentation"]>["tasks"] | undefined;
  readonly selectedTaskId: string | null;
  readonly transitions: Readonly<Record<string, SuccessorTransitionV2>>;
  readonly onOpenTask: (taskId: string) => void;
}) {
  return <div className="session-table" role="table" aria-label="Session history"><div className="session-table-head" role="row"><span role="columnheader">Session</span><span role="columnheader">State</span><span role="columnheader">Details</span><span role="columnheader">Pinned</span><span role="columnheader">Evolution output</span><span role="columnheader">Updated</span></div>{tasks.map((task, index) => {
    const transition = task.successor_transition ? transitions[task.successor_transition.successor_transition_id] : null;
    return <div className={`session-table-row ${task.task_id === selectedTaskId ? "selected" : ""}`} role="row" key={task.task_id}><span role="cell"><button type="button" className="session-history-button session-open-button" aria-pressed={task.task_id === selectedTaskId} onClick={() => onOpenTask(task.task_id)}><span>{presentation?.[task.task_id]?.instruction?.title ?? `Session ${index + 1}`}</span><ArrowRight size={14} /></button></span><span role="cell"><span className={`state-pill ${task.state}`}>{task.state.replaceAll("_", " ")}</span></span><span role="cell" className="session-detail">{task.attempts.length} attempt{task.attempts.length === 1 ? "" : "s"} · {task.authoritative_attempt_id ? "authoritative result selected" : "awaiting result"}</span><span role="cell">Project Head {task.admission.predecessor_project_head.generation}</span><span role="cell">{transition?.state === "committed" ? `Project Head ${task.successor_transition?.expected_successor_generation}` : transition?.state.replaceAll("_", " ") ?? "Pending"}</span><span role="cell">{formatTimeV2(task.updated_at)}</span></div>;
  })}</div>;
}

function TaskAuthorityCardV2({
  task,
  taskContent,
  presentation,
  artifacts,
  artifactPresentation,
  transition,
  timeline,
  logs,
  busy,
  onCancel,
  onRetry,
  onLoadLogs,
  onRetryTransition,
  onAbandonTransition,
}: {
  readonly task: TaskV2;
  readonly taskContent: ScienceProjectConfigV2["task"] | null;
  readonly presentation: NonNullable<DesktopProductSnapshotV2["runtimePresentation"]>["tasks"][string] | undefined;
  readonly artifacts: DesktopProductSnapshotV2["artifacts"];
  readonly artifactPresentation: NonNullable<DesktopProductSnapshotV2["runtimePresentation"]>["artifacts"] | undefined;
  readonly transition: SuccessorTransitionV2 | null;
  readonly timeline: DesktopProductSnapshotV2["timelines"][string];
  readonly logs: readonly LogEntryV2[];
  readonly busy: boolean;
  readonly onCancel: () => void;
  readonly onRetry: () => void;
  readonly onLoadLogs: () => void | Promise<void>;
  readonly onRetryTransition: () => void;
  readonly onAbandonTransition: () => void;
}) {
  const active = ["admitted", "preparing", "running", "cancelling"].includes(task.state);
  const producedArtifacts = artifacts.filter((artifact) => presentation?.producedArtifactIds.includes(artifact.artifact_id));
  const usedArtifacts = artifacts.filter((artifact) => presentation?.usedArtifactIds.includes(artifact.artifact_id));
  const [selectedResult, setSelectedResult] = useState<
    | { readonly kind: "artifact"; readonly artifactId: string }
    | { readonly kind: "output"; readonly fileName: string }
    | null
  >(null);
  useEffect(() => setSelectedResult(null), [task.task_id]);
  const selectedProducedArtifact = selectedResult?.kind === "artifact"
    && producedArtifacts.some((artifact) => artifact.artifact_id === selectedResult.artifactId);
  const resultInspector = selectedResult ? (
    <SessionResultInspectorV2
      selection={selectedResult}
      artifacts={artifacts}
      artifactPresentation={artifactPresentation}
      outputFiles={presentation?.outputFiles ?? []}
      onClose={() => setSelectedResult(null)}
    />
  ) : null;
  return (
    <article className="v2-task-card v2-task-result-detail">
      <div className="v2-profile-card-head"><div><span className="panel-kicker">Task result</span><strong>{taskContent?.title ?? `Task ${task.task_id}`}</strong><small>Task {task.task_id}</small></div><span className={`state-pill ${task.state}`}>{task.state.replaceAll("_", " ")}</span></div>
      <section className="session-task-detail v2-session-task-detail" data-session-priority="task"><span className="panel-kicker">Task instructions</span>{taskContent ? <p>{taskContent.objective}</p> : <p className="session-task-unavailable">The immutable admission contains the historical project-config digest, but this API response does not include that configuration's task text.</p>}</section>
      <section className="v2-result-section v2-conversation-section v2-session-module" data-session-priority="conversation">
        <header className="v2-session-module-heading"><div><h2>Conversation</h2><p>The request and the agent's response from this Session.</p></div><strong>{presentation?.transcript.length ?? logs.length} messages</strong></header>
        {presentation?.transcript.length ? <div className="v2-transcript">{presentation.transcript.map((entry, index) => <article key={`${entry.speaker}-${index}`} className={entry.speaker}><span>{entry.speaker}</span><p>{entry.text}</p></article>)}</div> : logs.length ? <div className="v2-transcript">{logs.map((entry) => <article key={entry.sequence} className="system"><span>{entry.stream}</span><p>{entry.message}</p></article>)}</div> : <p className="v2-empty-copy">The agent response is not loaded yet.</p>}
      </section>
      <section className="v2-evolution-priority v2-session-module" data-session-priority="evolution">
        <header className="v2-session-module-heading"><div><h2>Evolution</h2><p>Document changes learned from this Session for future Sessions.</p></div><strong>{producedArtifacts.length} produced</strong></header>
        <div className="session-evolution-summary">
          <div><span className="panel-kicker">Selected for this session</span><strong>{presentation?.selectedEvolution?.length ?? 0} methods</strong></div>
          {presentation?.selectedEvolution?.length ? <div className="session-evolution-statuses">{presentation.selectedEvolution.map((selection) => {
            const error = presentation.evolutionErrors?.find((candidate) => candidate.targetId === selection.targetId);
            const job = presentation.evolutionJobs?.find((candidate) => candidate.targetId === selection.targetId);
            const produced = (job?.artifactIds.length ?? 0) > 0;
            const state = error ? "failed" : job?.state ?? (produced ? "completed" : "pending");
            return <span key={selection.targetId} className={error ? "failed" : produced ? "produced" : "pending"} title={error?.message ?? `${selection.method} · ${state}`}>
              {selection.targetId.replaceAll("_", " ")} · {selection.method} · {state}
            </span>;
          })}</div> : <p>No document evolution was selected for this session.</p>}
        </div>
        <ResultCollection title="Evolution produced" empty="This Task did not publish an evolution artifact." artifacts={producedArtifacts} onOpen={(artifactId) => setSelectedResult({ kind: "artifact", artifactId })} />
        {selectedProducedArtifact ? resultInspector : null}
      </section>
      <section className="v2-supporting-module v2-session-module" data-session-priority="supporting">
        <header className="v2-session-module-heading secondary"><div><h2>Files and context</h2><p>Supporting inputs and files associated with this Session.</p></div></header>
        <div className="v2-supporting-results"><ResultCollection title="Context used" empty="No evolved context was recorded for this Task." artifacts={usedArtifacts} onOpen={(artifactId) => setSelectedResult({ kind: "artifact", artifactId })} /><div className="v2-result-section"><div className="v2-result-section-head"><span className="panel-kicker">Output files</span><strong>{presentation?.outputFiles.length ?? 0} files</strong></div>{presentation?.outputFiles.length ? <div className="v2-output-files">{presentation.outputFiles.map((file) => <button type="button" key={file.name} onClick={() => setSelectedResult({ kind: "output", fileName: file.name })}><FileText size={16} /><span><strong>{file.name}</strong><small>{file.summary}</small></span><ArrowRight size={14} /></button>)}</div> : <p className="v2-empty-copy">No readable output-file summary is available.</p>}</div></div>
        {selectedResult && !selectedProducedArtifact ? resultInspector : null}
      </section>
      <section className="v2-session-technical-details v2-session-module" data-session-priority="technical">
        <header className="v2-session-module-heading tertiary"><div><h2>Technical details</h2><p>Execution status and immutable identifiers for troubleshooting.</p></div></header>
      <div className="v2-task-authority"><div><span>Task Admission</span><code>{task.admission.task_admission_id}</code><small>{shortDigest(task.admission.admission_sha256)}</small></div><div><span>Predecessor Project Head</span><code>{task.admission.predecessor_project_head.project_head_id}</code><small>Generation {task.admission.predecessor_project_head.generation}</small></div></div>
      <div className="v2-attempt-list">{task.attempts.map((attempt) => {
        const authoritative = attempt.attempt_id === task.authoritative_attempt_id;
        return <div key={attempt.attempt_id}><strong>Attempt {attempt.ordinal}</strong><code>{attempt.attempt_id}</code><small>{formatTimeV2(attempt.created_at)}</small><span className="muted-pill">{authoritative ? `authoritative · ${task.state.replaceAll("_", " ")}` : "superseded"}</span></div>;
      })}</div>
      <LifecycleOperationPanelV2
        model={taskPanelModelV2(task, timeline, logs)}
        onCancel={active ? onCancel : undefined}
      />
      {transition !== null && transition.state !== "committed" ? (
        <LifecycleOperationPanelV2 model={transitionPanelModelV2(transition, timeline)} />
      ) : null}
      {transition ? <div className="v2-transition"><div><span>Successor Transition</span><strong>{transition.transition.successor_transition_id}</strong><small>Expected Project Head generation {transition.transition.expected_successor_generation}</small></div><span className={`state-pill ${transition.state}`}>{transition.state}</span>{transition.error ? <p>{transition.error.message}</p> : null}{transition.state === "failed" ? <div className="v2-card-actions"><button type="button" className="secondary-button" disabled={busy} onClick={onRetryTransition}>Retry successor transition</button><button type="button" className="text-button" disabled={busy} onClick={onAbandonTransition}>Abandon evolution result</button></div> : null}</div> : null}
      </section>
      <div className="v2-card-actions"><button type="button" className="secondary-button" disabled={busy} onClick={() => void onLoadLogs()}>Refresh task logs</button>{["failed", "cancelled"].includes(task.state) ? <button type="button" className="secondary-button" disabled={busy} onClick={onRetry}>Append infrastructure Attempt</button> : null}</div>
    </article>
  );
}

function SessionResultInspectorV2({
  selection,
  artifacts,
  artifactPresentation,
  outputFiles,
  onClose,
}: {
  readonly selection:
    | { readonly kind: "artifact"; readonly artifactId: string }
    | { readonly kind: "output"; readonly fileName: string };
  readonly artifacts: DesktopProductSnapshotV2["artifacts"];
  readonly artifactPresentation: NonNullable<DesktopProductSnapshotV2["runtimePresentation"]>["artifacts"] | undefined;
  readonly outputFiles: NonNullable<DesktopProductSnapshotV2["runtimePresentation"]>["tasks"][string]["outputFiles"];
  readonly onClose: () => void;
}) {
  const [view, setView] = useState<"content" | "changes">("changes");
  useEffect(() => setView("changes"), [selection.kind, selection.kind === "artifact" ? selection.artifactId : selection.fileName]);

  const artifact = selection.kind === "artifact"
    ? artifacts.find((candidate) => candidate.artifact_id === selection.artifactId) ?? null
    : null;
  const artifactPreview = artifact === null ? undefined : artifactPresentation?.[artifact.artifact_id];
  const output = selection.kind === "output"
    ? outputFiles.find((candidate) => candidate.name === selection.fileName)
    : undefined;
  const title = artifactPreview?.title ?? artifact?.artifact_id ?? output?.name ?? "Result preview";
  const diffLines = artifactPreview?.diffLines ?? output?.diffLines ?? [];
  const previousLabel = artifactPreview
    ? artifactPreview.previousArtifactId
    : output?.previousName ?? null;
  const contentUnavailable = artifact !== null && artifactPreview === undefined;

  return (
    <section className="session-result-inspector" data-testid="session-result-inspector" aria-label={`${title} preview`}>
      <div className="session-result-inspector-head">
        <div>
          <span className="panel-kicker">{artifact ? artifactTypeLabel(artifact.artifact_type) : "Output file"}</span>
          <h3>{title}</h3>
          <p>{artifactPreview?.statusDetail ?? output?.summary ?? "Readable artifact content is unavailable."}</p>
        </div>
        <button type="button" className="icon-button" aria-label="Close result preview" onClick={onClose}><X size={16} /></button>
      </div>
      <div className="segmented-control session-result-tabs" role="tablist" aria-label="Result preview mode">
        <button type="button" role="tab" aria-selected={view === "content"} className={view === "content" ? "active" : ""} onClick={() => setView("content")}><FileText size={14} /> Current content</button>
        <button type="button" role="tab" aria-selected={view === "changes"} className={view === "changes" ? "active" : ""} onClick={() => setView("changes")}><History size={14} /> Changes</button>
      </div>
      {view === "content" ? (
        artifact ? (
          artifactPreview?.documents.length ? <div className="v2-artifact-documents">{artifactPreview.documents.map((document) => <section key={document.path}><div><FileText size={14} /><strong>{document.path}</strong><small>text/markdown</small></div><pre>{document.content}</pre></section>)}</div> : <p className="v2-empty-copy">{contentUnavailable ? "The artifact metadata exists, but the daemon did not provide a readable body." : "No readable document body is available."}</p>
        ) : output?.content ? (
          <div className="v2-artifact-documents"><section><div><FileText size={14} /><strong>{output.name}</strong><small>workspace output</small></div><pre>{output.content}</pre></section></div>
        ) : <p className="v2-empty-copy">No readable output-file body is available.</p>
      ) : (
        <div className="v2-artifact-diff">
          <div className="v2-diff-summary">
            <span>{previousLabel ? <>Compared with <strong>{previousLabel}</strong></> : "No previous retained version"}</span>
            <span>{diffLines.length ? `${diffLines.length} changed lines` : artifactPreview?.status ?? "unavailable"}</span>
          </div>
          {diffLines.length ? <pre>{diffLines.map((line, index) => <span key={`${line.kind}-${index}`} className={line.kind}>{line.kind === "added" ? "+ " : line.kind === "removed" ? "− " : "  "}{line.text}</span>)}</pre> : <p className="v2-empty-copy">{previousLabel ? "No textual difference was recorded." : "This is the earliest retained version, so there is nothing older to compare."}</p>}
        </div>
      )}
    </section>
  );
}

function ResultCollection({ title, empty, artifacts, onOpen }: { readonly title: string; readonly empty: string; readonly artifacts: DesktopProductSnapshotV2["artifacts"]; readonly onOpen: (artifactId: string) => void }) {
  return <div className="v2-result-section"><div className="v2-result-section-head"><span className="panel-kicker">{title}</span><strong>{artifacts.length}</strong></div>{artifacts.length ? <div className="v2-result-artifacts">{artifacts.map((artifact) => <button type="button" key={artifact.artifact_id} onClick={() => onOpen(artifact.artifact_id)}><span className="v2-artifact-type">{artifactTypeLabel(artifact.artifact_type)}</span><span><strong>{artifact.artifact_id}</strong><small>{formatBytes(artifact.byte_size)}</small></span><ArrowRight size={14} /></button>)}</div> : <p className="v2-empty-copy">{empty}</p>}</div>;
}

function EvolutionWorkspaceV2({
  project,
  snapshot,
  provider,
  busy,
  onSave,
}: {
  readonly project: ProjectV2;
  readonly snapshot: DesktopProductSnapshotV2;
  readonly provider: DesktopProductProviderV2;
  readonly busy: boolean;
  readonly onSave: (config: ScienceProjectConfigV2) => void;
}) {
  const [targets, setTargets] = useState(project.config.evolution.targets);
  useEffect(() => setTargets(project.config.evolution.targets), [project.project_config_sha256]);
  const capabilities = snapshot.capability?.project_id === project.project_id
    ? snapshot.capability.capabilities.targets.filter((target) => target.exposure === "desktop")
    : [];
  const artifacts = snapshot.artifacts.filter((artifact) => (
    artifact.project_id === project.project_id
    && !["dataset", "workspace_result", "diagnostic"].includes(artifact.artifact_type)
  ));
  return (
    <div className="workspace-stack" data-testid="evolution-workspace">
      <div className="workspace-heading"><div><p className="eyebrow">Evolution</p><h1>Cross-session changes</h1><p>Review what changed and which Project Head the next session will use.</p></div></div>
      {project.active_project_head ? <section className="revision-strip"><div className="revision-node active"><span>Active Project Head</span><strong>Project Head {project.active_project_head.generation}</strong><small>Used by the next session</small></div></section> : null}
      <section className="product-panel task-panel">
        <div className="panel-heading"><div><span className="panel-kicker">Verified remote registry</span><h2>Evolution targets</h2></div><span className="muted-pill">{shortDigest(snapshot.capability?.registry_sha256 ?? "")}</span></div>
        {capabilities.length === 0 ? <Notice tone="warning" title="No visible evolution methods" detail="The active verified Core registry did not publish a Desktop-visible target for this execution profile." /> : <div className="v2-target-list">{capabilities.map((target) => {
          const current = targets[target.target_id] ?? { enabled: false, method: null, config: {} };
          const methodId = current.method ?? target.effective_default_method_id ?? "";
          const methods = target.methods.filter((method) => (
            (
              method.support?.overall !== "unsupported"
              && method.support?.overall !== "unavailable"
            ) || method.method_id === methodId
          ));
          const resolvers = target.selection_resolvers.map((resolver) => ({
            ...resolver,
            supported: resolver.resolved_methods.length > 0
              && resolver.resolved_methods.every((method) => method.support.overall === "supported"),
          }));
          const selectedResolver = resolvers.find((resolver) => resolver.selection_value === methodId);
          const selectionAccepted = target.accepted_methods.some((method) => method.method_id === methodId)
            || selectedResolver?.supported === true;
          return <article key={target.target_id}><label className="v2-target-toggle"><input type="checkbox" checked={current.enabled} onChange={(event) => setTargets((previous) => ({ ...previous, [target.target_id]: { enabled: event.target.checked, method: event.target.checked ? methodId || null : current.method, config: current.config } }))} /><span><strong>{target.display_name}</strong><small>{target.description}</small></span></label><label>Method<select value={methodId} disabled={!current.enabled} onChange={(event) => {
            const selected = methods.find((method) => method.method_id === event.target.value);
            let defaultConfig: ScienceProjectConfigV2["evolution"]["targets"][string]["config"] = {};
            try { defaultConfig = selected ? JSON.parse(selected.default_config_json) as typeof defaultConfig : {}; } catch { defaultConfig = {}; }
            setTargets((previous) => ({ ...previous, [target.target_id]: { enabled: true, method: event.target.value, config: defaultConfig } }));
          }}><option value="">No supported default</option>{resolvers.map((resolver) => <option key={`resolver:${resolver.selection_value}`} value={resolver.selection_value} disabled={!resolver.supported}>{resolver.display_name}</option>)}{methods.map((method) => <option key={`method:${method.method_id}`} value={method.method_id}>{method.display_name}</option>)}</select></label>{current.enabled && (!methodId || !selectionAccepted) ? <p className="form-error" role="alert">This enabled target has no method accepted by the active remote registry and blocks Task admission.</p> : null}</article>;
        })}</div>}
        <div className="v2-primary-row"><button type="button" className="primary-button" disabled={busy || snapshot.capability === null} onClick={() => onSave({ ...project.config, evolution: { targets } })}>Save evolution configuration</button></div>
      </section>
      <EvolutionArtifactBrowserV2 artifacts={artifacts} presentation={snapshot.runtimePresentation?.artifacts} provider={provider} />
    </div>
  );
}

function EvolutionArtifactBrowserV2({ artifacts, presentation, provider }: {
  readonly artifacts: DesktopProductSnapshotV2["artifacts"];
  readonly presentation: NonNullable<DesktopProductSnapshotV2["runtimePresentation"]>["artifacts"] | undefined;
  readonly provider: DesktopProductProviderV2;
}) {
  const [selectedId, setSelectedId] = useState<string | null>(artifacts[0]?.artifact_id ?? null);
  const [view, setView] = useState<"content" | "changes">("content");
  const [metadata, setMetadata] = useState<{ readonly mediaType: string; readonly byteSize: number; readonly diffStatus: string } | null>(null);
  const [metadataError, setMetadataError] = useState<string | null>(null);
  useEffect(() => {
    if (!artifacts.some((artifact) => artifact.artifact_id === selectedId)) setSelectedId(artifacts[0]?.artifact_id ?? null);
  }, [artifacts, selectedId]);
  useEffect(() => {
    if (selectedId === null) return;
    let retained = true;
    setMetadata(null);
    setMetadataError(null);
    void Promise.all([provider.getArtifactContent(selectedId), provider.getArtifactDiff(selectedId)]).then(([content, diff]) => {
      if (retained) setMetadata({ mediaType: content.media_type, byteSize: content.byte_size, diffStatus: diff.status });
    }).catch((error) => {
      if (retained) setMetadataError(userMessageV2(error));
    });
    return () => { retained = false; };
  }, [provider, selectedId]);
  const selected = artifacts.find((artifact) => artifact.artifact_id === selectedId) ?? null;
  const preview = selectedId === null ? undefined : presentation?.[selectedId];
  return <section className="v2-evolution-results">{artifacts.length === 0 ? <div className="empty-row">No textual evolution artifacts have been published for this project yet.</div> : <div className="artifact-layout"><aside className="artifact-list" aria-label="Evolution artifacts"><div className="artifact-list-heading"><span>Evolution results</span><strong>{artifacts.length} selected</strong></div>{artifacts.map((artifact) => { const item = presentation?.[artifact.artifact_id]; return <button type="button" key={artifact.artifact_id} className={`artifact-list-item ${artifact.artifact_id === selectedId ? "active" : ""}`} aria-pressed={artifact.artifact_id === selectedId} onClick={() => { setSelectedId(artifact.artifact_id); setView("content"); }}><span className={`artifact-icon ${artifact.artifact_type}`}><Sparkles size={15} /></span><span><strong>{item?.title ?? artifactTypeLabel(artifact.artifact_type)}</strong><small>{item?.statusDetail ?? `${artifactTypeLabel(artifact.artifact_type)} · ${formatTimeV2(artifact.created_at)}`}</small></span><ArrowRight size={14} /></button>; })}</aside>{selected ? <section className="artifact-viewer"><div className="artifact-viewer-head"><div><span className="panel-kicker">{artifactTypeLabel(selected.artifact_type)}</span><h2>{preview?.title ?? selected.artifact_id}</h2><p>{preview?.statusDetail ?? "The authoritative artifact metadata is available; readable content is not exposed by the current contract."}</p></div><div className="artifact-meta"><span>{artifactStatusLabel(preview?.status ?? "unavailable")}</span><span>{formatBytes(metadata?.byteSize ?? selected.byte_size)}</span></div></div><div className="segmented-control" role="tablist" aria-label="Artifact view"><button type="button" role="tab" aria-selected={view === "content"} className={view === "content" ? "active" : ""} onClick={() => setView("content")}><FileText size={14} /> Content</button><button type="button" role="tab" aria-selected={view === "changes"} className={view === "changes" ? "active" : ""} onClick={() => setView("changes")}><History size={14} /> Changes</button></div><div className="v2-artifact-facts"><span><small>Source Task</small><strong>{preview?.sourceTaskId ?? "Not reported"}</strong></span><span><small>Target path</small><strong>{preview?.targetPath ?? "Not applicable"}</strong></span><span><small>Digest</small><code>{shortDigest(selected.manifest_sha256)}</code></span></div>{metadataError && preview === undefined ? <Notice tone="warning" title="Readable content unavailable" detail={metadataError} /> : null}<div className="artifact-body">{view === "content" ? <div className="v2-artifact-documents">{preview?.documents.length ? preview.documents.map((document) => <section key={document.path}><div><FileText size={14} /><strong>{document.path}</strong><small>{metadata?.mediaType ?? "text/markdown"}</small></div><pre>{document.content}</pre></section>) : <p className="v2-empty-copy">No readable document body is available for this artifact.</p>}</div> : <div className="v2-artifact-diff"><div className="v2-diff-summary"><span>Compared with <strong>{preview?.previousArtifactId ?? "no previous version"}</strong></span><span>{metadata?.diffStatus ?? (preview?.diffLines.length ? "available" : "unavailable")}</span></div>{preview?.diffLines.length ? <pre>{preview.diffLines.map((line, index) => <span key={`${line.kind}-${index}`} className={line.kind}>{line.kind === "added" ? "+ " : line.kind === "removed" ? "− " : "  "}{line.text}</span>)}</pre> : <p className="v2-empty-copy">No textual change preview is available.</p>}</div>}</div></section> : null}</div>}</section>;
}

function SystemWorkspaceV2({
  snapshot,
  activeProfile,
  busy,
  onOpenConnections,
  onRestartService,
  serviceLogs,
  onLoadServiceLogs,
  onCleanupCaches,
  onCreateDiagnostic,
}: {
  readonly snapshot: DesktopProductSnapshotV2;
  readonly activeProfile: RemoteProfileV2 | null;
  readonly busy: boolean;
  readonly onOpenConnections: () => void;
  readonly onRestartService: (serviceId: string) => void;
  readonly serviceLogs: Readonly<Record<string, readonly LogEntryV2[]>>;
  readonly onLoadServiceLogs: (serviceId: string) => void | Promise<void>;
  readonly onCleanupCaches: () => void;
  readonly onCreateDiagnostic: () => void;
}) {
  return (
    <div className="v2-workspace-stack">
      <section className="product-panel task-panel"><div className="panel-heading"><div><span className="panel-kicker">Connection owner</span><h2>System OpenSSH workspace</h2></div><button type="button" className="secondary-button" onClick={onOpenConnections}>Manage workspaces</button></div>{activeProfile && activeProfile.profile_kind === "system_openssh" ? <div className="v2-system-summary"><div><span>Display name</span><strong>{activeProfile.display_name}</strong></div><div><span>SSH alias</span><code>{activeProfile.ssh_host_alias}</code></div><div><span>Connection generation</span><strong>{activeProfile.connection_generation}</strong></div><div><span>Core API</span><strong>{activeProfile.core_api_major === 2 ? "v2 verified" : "Not connected"}</strong></div></div> : <p className="v2-empty-copy">No active remote workspace.</p>}</section>
      <section className="product-panel task-panel">
        <div className="panel-heading">
          <div><span className="panel-kicker">Active project tunnel</span><h2>Remote services</h2></div>
          <div className="v2-card-actions">
            <button type="button" className="secondary-button" disabled={busy || activeProfile === null} onClick={onCreateDiagnostic}>Collect system diagnostics</button>
            <button type="button" className="secondary-button" disabled={busy || activeProfile === null} onClick={onCleanupCaches}>Clean safe caches</button>
          </div>
        </div>
        {snapshot.services.length === 0 ? (
          <p className="v2-empty-copy">Services appear only after a compatible Daemon and active project tunnel are verified.</p>
        ) : (
          <div className="v2-service-list">
            {snapshot.services.map((service) => (
              <div key={service.service_id} className="v2-service-observation">
                <div>
                  <span><strong>{service.kind}</strong><small>{service.service_id}</small></span>
                  <span className={`state-pill ${service.status}`}>{service.status}</span>
                  <div className="v2-card-actions">
                    <button type="button" className="secondary-button" disabled={busy} onClick={() => void onLoadServiceLogs(service.service_id)}>View logs</button>
                    <button type="button" className="secondary-button" disabled={busy} onClick={() => onRestartService(service.service_id)}>Restart</button>
                  </div>
                </div>
                {serviceLogs[service.service_id] === undefined ? null : (
                  <LifecycleOperationPanelV2 model={servicePanelModelV2(service, serviceLogs[service.service_id]!)} />
                )}
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function IdentityCard({ title, id, detail, digest }: { readonly title: string; readonly id: string; readonly detail: string; readonly digest: string }) {
  return <article className="v2-identity-card"><span>{title}</span><strong>{id}</strong><small>{detail}</small><code>{shortDigest(digest)}</code></article>;
}

function WorkspaceButton({ active, onClick, icon: Icon, children }: { readonly active: boolean; readonly onClick: () => void; readonly icon: typeof BookOpen; readonly children: string }) {
  return <button type="button" className={`product-nav-item ${active ? "active" : ""}`} aria-current={active ? "page" : undefined} onClick={onClick}><Icon size={17} /> {children}</button>;
}

function Notice({ tone, title, detail, action, onDismiss }: { readonly tone: "error" | "warning" | "success" | "info"; readonly title: string; readonly detail: string; readonly action?: React.ReactNode; readonly onDismiss?: () => void }) {
  return <div className={`v2-notice ${tone}`} role={tone === "error" ? "alert" : "status"}>{tone === "success" ? <CheckCircle2 size={18} /> : <AlertCircle size={18} />}<div><strong>{title}</strong><span>{detail}</span></div>{action}{onDismiss ? <button type="button" className="icon-button" aria-label="Dismiss message" onClick={onDismiss}><X size={15} /></button> : null}</div>;
}

function useDialogBoundary(onClose: () => void) {
  const ref = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
      }
    };
    document.addEventListener("keydown", keydown);
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", keydown);
      document.body.style.overflow = overflow;
      previous?.focus();
    };
  }, []);
  return ref;
}

function isConnectedProfile(profile: RemoteProfileV2): profile is RemoteWorkspaceProfileV2 {
  return profile.profile_kind === "system_openssh" && profile.connection_state === "connected";
}

function scienceProjectConfig(
  title: string,
  objective: string,
  workspaceKind: "scratch" | "native_folder_snapshot",
  workspaceDisplayName: string,
  executionMode: ScienceProjectConfigV2["execution"]["mode"],
): ScienceProjectConfigV2 {
  const execution: ScienceProjectConfigV2["execution"] = executionMode === "self-deployed"
    ? {
      mode: "self-deployed",
      capture_mode: "transcript",
      token_level_metrics_available: false,
      harness_id: "codex",
      model_profile_id: "qwen3-0.6b-v1",
      token_limit: 8_192,
      task_network_allow_internet: true,
    }
    : {
      mode: "codex_subscription_transcript",
      capture_mode: "transcript",
      token_level_metrics_available: false,
      harness_id: "codex",
      codex_model: "gpt-5.3-codex-spark",
      reasoning_effort: "high",
      token_limit: 32_000,
      task_network_allow_internet: true,
    };
  return {
    schema_version: "2",
    task: { title: title.trim(), objective: objective.trim() },
    workspace: { kind: workspaceKind, display_name: workspaceDisplayName.trim() },
    execution,
    evolution: { targets: {} },
  };
}

function intentFor(snapshot: DesktopProductSnapshotV2, prefix: string): ProductMutationIntentV2 {
  return { actionId: actionIdV2(prefix), streamEpoch: snapshot.stream.epoch };
}

let actionSequence = 0;
function actionIdV2(prefix: string): string {
  actionSequence += 1;
  return `${prefix}-${Date.now().toString(36)}-${actionSequence.toString(36).padStart(8, "0")}`;
}

function userMessageV2(error: unknown): string {
  if (error instanceof DesktopApiErrorV2) return error.apiError.summary;
  if (error instanceof Error && error.message.length > 0 && error.message.length <= 768) return error.message;
  return "OpenEvo Desktop could not complete this action. Refresh the current authority and try again.";
}

function connectionLabel(state: RemoteWorkspaceProfileV2["connection_state"]): string {
  const labels: Record<RemoteWorkspaceProfileV2["connection_state"], string> = {
    disconnected: "Disconnected",
    connecting: "Starting system OpenSSH",
    prompt_pending: "Waiting for native authentication",
    host_key_review: "Host identity review required",
    bootstrapping: "Checking or installing OpenEvo Daemon",
    negotiating: "Negotiating exact Core v2 authority",
    connected: "Connected",
    disconnecting: "Disconnecting",
    failed: "Connection failed",
  };
  return labels[state];
}

function shortDigest(value: string): string {
  return value.length === 64 ? `${value.slice(0, 12)}…${value.slice(-8)}` : value;
}

function artifactTypeLabel(type: DesktopProductSnapshotV2["artifacts"][number]["artifact_type"]): string {
  const labels: Record<DesktopProductSnapshotV2["artifacts"][number]["artifact_type"], string> = {
    dataset: "Dataset",
    workspace_result: "Workspace result",
    text_memory: "Text memory",
    skill_bundle: "Skill bundle",
    agent_system: "Agent system",
    parametric_memory: "Parametric memory",
    report: "Report",
    diagnostic: "Diagnostic",
  };
  return labels[type];
}

function artifactStatusLabel(status: NonNullable<DesktopProductSnapshotV2["runtimePresentation"]>["artifacts"][string]["status"]): string {
  const labels = {
    created: "First version",
    updated: "Updated",
    unchanged: "No change",
    failed: "Method failed",
    incompatible: "Incompatible",
    unavailable: "Content unavailable",
  } as const;
  return labels[status];
}

function formatBytes(bytes: number): string {
  if (bytes < 1_024) return `${bytes} B`;
  if (bytes < 1_024 * 1_024) return `${(bytes / 1_024).toFixed(1)} KB`;
  return `${(bytes / (1_024 * 1_024)).toFixed(1)} MB`;
}

function formatTimeV2(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
