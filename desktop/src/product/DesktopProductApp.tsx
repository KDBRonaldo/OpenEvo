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
  Download,
  FolderOpen,
  FileText,
  History,
  LoaderCircle,
  MessageSquareText,
  PanelLeft,
  Play,
  Plus,
  RefreshCw,
  Server,
  Settings,
  ShieldCheck,
  Sparkles,
  Upload,
  X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
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
  DesktopErrorV2,
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
  return { ...config, task, evolution: { targets } } as ScienceProjectConfigV2;
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
  const [selectedWorkspacePath, setSelectedWorkspacePath] = useState<string | null>(null);
  const [connectionOpen, setConnectionOpen] = useState(false);
  const [projectOpen, setProjectOpen] = useState(false);
  const [projectEditing, setProjectEditing] = useState(false);
  const [taskLogs, setTaskLogs] = useState<Readonly<Record<string, readonly LogEntryV2[]>>>({});
  const [serviceLogs, setServiceLogs] = useState<Readonly<Record<string, readonly LogEntryV2[]>>>({});
  const readyReported = useRef(false);
  const initialFailureReported = useRef(false);
  const refreshRequestSequence = useRef(0);
  const refreshInFlight = useRef<Promise<DesktopProductSnapshotV2 | null> | null>(null);
  const snapshotRef = useRef<DesktopProductSnapshotV2 | null>(null);
  const projectRecoveryRefreshInFlight = useRef(false);

  const refresh = useCallback((): Promise<DesktopProductSnapshotV2 | null> => {
    refreshRequestSequence.current += 1;
    if (refreshInFlight.current !== null) return refreshInFlight.current;

    const worker = (async (): Promise<DesktopProductSnapshotV2 | null> => {
      try {
        for (;;) {
          const requestSequence = refreshRequestSequence.current;
          try {
            const result = await provider.refresh();
            // An SSE/lifecycle notification that arrives while authority is
            // loading requests one trailing refresh.  Every waiter shares this
            // worker, so an imperative action never receives a false null just
            // because a subscription refresh superseded its preflight read.
            if (requestSequence !== refreshRequestSequence.current) continue;
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
            if (requestSequence !== refreshRequestSequence.current) continue;
            setLoadError(userMessageV2(error));
            if (snapshotRef.current === null && !initialFailureReported.current) {
              initialFailureReported.current = true;
              onInitialSnapshotFailed?.(error);
            }
            return null;
          }
        }
      } finally {
        refreshInFlight.current = null;
      }
    })();
    refreshInFlight.current = worker;
    return worker;
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
    const tasksToLoad = [...new Map(snapshot.tasks.filter((task) => (
      task.task_id === selectedTaskId
        || ["admitted", "preparing", "running", "cancelling", "waiting_for_successor"].includes(task.state)
    )).map((task) => [task.task_id, task])).values()];
    if (tasksToLoad.length === 0) return;
    let retained = true;
    void Promise.all(tasksToLoad.map(async (task) => (
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
  }, [provider, selectedTaskId, snapshot]);

  const recoveringProjectId = snapshot?.projects.find((project) => (
    project.project_id === snapshot.state.active_project_id
      && project.state === "not_ready"
      && project.active_project_head === null
  ))?.project_id ?? null;

  useEffect(() => {
    if (recoveringProjectId === null) return;
    const retry = (): void => {
      if (projectRecoveryRefreshInFlight.current) return;
      projectRecoveryRefreshInFlight.current = true;
      void refresh().finally(() => {
        projectRecoveryRefreshInFlight.current = false;
      });
    };
    retry();
    const timer = window.setInterval(retry, 2_000);
    return () => window.clearInterval(timer);
  }, [recoveringProjectId, refresh]);

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
  const displayedProjectTasks = displayedProject === null ? [] : snapshot.tasks.filter(
    (task) => task.project_id === displayedProject.project_id,
  );
  const displayedWorkspace = displayedProject === null
    ? undefined
    : snapshot.runtimePresentation?.workspaces?.[displayedProject.project_id];
  const selectedWorkspaceEntry = displayedWorkspace?.entries.find(
    (entry) => entry.kind === "file" && entry.path === selectedWorkspacePath,
  ) ?? null;
  const lifecycleStates = provider.listLifecycleOperations();
  const coreOperations = provider.listCoreOperations();
  const diagnostics = provider.listDiagnostics();
  const mutationIntents = provider.listMutationIntents();
  const visibleOperationCount = lifecycleStates.length + coreOperations.length + diagnostics.length;
  const developmentAgentBridge = provider.featureFlags.includes("development_agent_bridge");
  const developmentEvolutionActive = developmentAgentBridge && (
    snapshot.runtimePresentation?.evolutionRuns?.some((run) => run.state === "running") === true
  );
  const sessionEvolutionAvailable = !developmentAgentBridge && (
    displayedProject !== null && snapshot.capability?.project_id === displayedProject.project_id
  );
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
      // Starting a Task crosses multiple remote authority boundaries. Rebase
      // on the latest snapshot before the first mutation instead of trusting
      // the render-time snapshot, which an SSE event may already have made
      // stale while the user was editing the draft.
      const startingSnapshot = await refresh();
      if (startingSnapshot === null) throw new Error("The current remote project state could not be loaded before starting the session.");
      const startingProject = startingSnapshot.projects.find((candidate) => candidate.project_id === project.project_id);
      if (!startingProject || startingProject.state !== "ready") {
        throw new Error("The project is not ready for a new session yet.");
      }
      let currentSnapshot = startingSnapshot;
      let currentProject = startingProject;
      const nextConfig = sessionEvolutionAvailable
        ? withSessionDocumentEvolution(currentProject.config, task, selectedEvolutionTargets)
        : { ...currentProject.config, task };
      if (JSON.stringify(nextConfig) !== JSON.stringify(currentProject.config)) {
        await provider.updateProject(
          currentProject.project_id,
          currentProject.display_name,
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
      // Project validation is itself an authoritative remote mutation and can
      // emit a Desktop event. Refresh again so Task admission is bound to the
      // post-validation stream epoch rather than the snapshot used to request
      // validation.
      const validatedSnapshot = await refresh();
      if (validatedSnapshot === null) throw new Error("The validated project state could not be reloaded before starting the session.");
      const validatedProject = validatedSnapshot.projects.find((candidate) => candidate.project_id === currentProject.project_id);
      if (!validatedProject || validatedProject.state !== "ready") {
        throw new Error("The validated project is not ready for a new session yet.");
      }
      currentSnapshot = validatedSnapshot;
      currentProject = validatedProject;
      const submittedTask = await provider.submitTask(currentProject.project_id, intentFor(currentSnapshot, "submit-task"));
      await refresh();
      setWorkspace("research");
      setSelectedTaskId(submittedTask.task_id);
      setActionStatus(developmentAgentBridge
        ? "The remote Session started. Its transcript will become reusable evidence in Evolution."
        : "Task admitted with immutable Project Head and execution authority.");
    } catch (error) {
      setActionError(userMessageV2(error));
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  const selectProject = (projectId: string): void => {
    const project = snapshot.projects.find((candidate) => candidate.project_id === projectId);
    if (!project) return;
    setSelectedWorkspacePath(null);
    setWorkspace("research");
    if (project.project_id === activeProject?.project_id) {
      setSelectedTaskId(snapshot.tasks.find((task) => task.project_id === projectId)?.task_id ?? null);
      return;
    }
    setSelectedTaskId(null);
    void act(
      () => provider.activateProject(project.project_id, intentFor(snapshot, "activate-project")),
      `Switched to ${project.display_name}.`,
    ).then((result) => {
      if (result === null || result.snapshot === null) return;
      setSelectedTaskId(result.snapshot.tasks.find((task) => task.project_id === projectId)?.task_id ?? null);
    });
  };

  const uploadWorkspaceFiles = (files: readonly File[], overwrite: boolean): void => {
    if (displayedProject === null) return;
    void act(
      async () => {
        if (!provider.uploadWorkspaceFile) throw new Error("This backend does not support workspace uploads.");
        for (const file of files) {
          await provider.uploadWorkspaceFile(
            displayedProject.project_id,
            {
              path: file.name,
              data: file,
              mediaType: file.type || "application/octet-stream",
              overwrite,
            },
            intentFor(snapshot, "upload-workspace-file"),
          );
        }
      },
      `${files.length} workspace file${files.length === 1 ? "" : "s"} uploaded to the remote server.`,
    );
  };

  const downloadWorkspaceFile = (path: string): void => {
    if (displayedProject === null) return;
    void act(
      async () => {
        if (!provider.downloadWorkspaceFile) throw new Error("This backend does not support workspace downloads.");
        const download = await provider.downloadWorkspaceFile(displayedProject.project_id, path);
        saveBrowserDownload(download.data, download.fileName);
      },
      `${path} downloaded from the remote workspace.`,
    );
  };

  return (
    <div className="product-shell product-v2-shell" data-provider-kind="desktop_sidecar" data-api-version="2">
      <aside className="product-activitybar" aria-label="Primary navigation">
        <div className="product-brand" aria-label="OpenEvo Desktop" title="OpenEvo Desktop">
          <span className="product-mark"><OpenEvoMark /></span>
        </div>
        <nav className="product-nav" aria-label="Workspace views">
          <WorkspaceButton active={workspace === "research"} onClick={() => setWorkspace("research")} icon={BookOpen}>Research</WorkspaceButton>
          <WorkspaceButton active={workspace === "evolution"} onClick={() => setWorkspace("evolution")} icon={Sparkles}>Evolution</WorkspaceButton>
          <WorkspaceButton active={workspace === "system"} onClick={() => setWorkspace("system")} icon={Activity}>System</WorkspaceButton>
        </nav>
        <button type="button" className="activitybar-settings" aria-label="Remote workspace settings" title="Remote workspace settings" onClick={() => setConnectionOpen(true)}><Settings size={19} /></button>
      </aside>

      <ProjectExplorerV2
        projects={snapshot.projects}
        activeProject={displayedProject}
        workspace={displayedWorkspace}
        selectedPath={selectedWorkspacePath}
        busy={busy}
        fileTransferAvailable={provider.uploadWorkspaceFile !== undefined && provider.downloadWorkspaceFile !== undefined}
        onSelectProject={selectProject}
        onCreateProject={() => { setProjectEditing(false); setProjectOpen(true); }}
        onSelectFile={(path) => { setWorkspace("research"); setSelectedTaskId(null); setSelectedWorkspacePath(path); }}
        onUpload={uploadWorkspaceFiles}
        generation={generation}
      />

      <SessionExplorerV2
        project={displayedProject}
        tasks={displayedProjectTasks}
        presentation={snapshot.runtimePresentation?.tasks}
        selectedTaskId={selectedTaskId}
        onSelectTask={(taskId) => { setWorkspace("research"); setSelectedWorkspacePath(null); setSelectedTaskId(taskId); }}
        onNewSession={() => { setWorkspace("research"); setSelectedWorkspacePath(null); setSelectedTaskId(null); }}
      />

      <div className="product-stage">
        <header className="product-topbar">
          <div className="workspace-breadcrumb"><span>{workspace === "research" ? selectedWorkspaceEntry ? "File" : "Session" : workspace === "evolution" ? "Evolution" : "System"}</span><ChevronRight size={14} /><strong>{selectedWorkspaceEntry?.path ?? (selectedTaskId ? snapshot.runtimePresentation?.tasks[selectedTaskId]?.instruction?.title : displayedProject?.display_name) ?? "OpenEvo"}</strong></div>
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
              detail="Agent replies come from the remote Codex CLI. Project and Session history are persisted by the remote development daemon; standalone Evolution is available through the unverified development registry."
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
          ) : workspace === "research" && selectedWorkspaceEntry !== null ? (
            <ProjectFileWorkspaceV2
              project={displayedProject}
              entry={selectedWorkspaceEntry}
              fileTransferAvailable={provider.downloadWorkspaceFile !== undefined}
              busy={busy}
              onDownload={() => downloadWorkspaceFile(selectedWorkspaceEntry.path)}
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
              busy={busy || developmentEvolutionActive}
              sessionEvolutionAvailable={sessionEvolutionAvailable}
              onSelectTask={setSelectedTaskId}
              onOpenSettings={() => { setProjectEditing(true); setProjectOpen(true); }}
              onRetryInitialization={() => void refresh()}
              onRun={(task, selectedEvolutionTargets) => void runProject(displayedProject, task, selectedEvolutionTargets)}
              onCancelTask={(task) => void act(
                () => provider.cancelTask(task.task_id, intentFor(snapshot, "cancel-task")),
                "Task cancellation requested.",
              )}
              onRetryTask={(task) => void act(
                () => provider.retryTask(task.task_id, intentFor(snapshot, "retry-task")),
                "A new infrastructure Attempt was requested under the same Task Admission.",
              )}
              onRetryEvolutionJob={(jobId) => void act(
                async () => {
                  if (!provider.retryEvolutionJob) throw new Error("This backend does not support retrying an individual Evolution method.");
                  await provider.retryEvolutionJob(jobId, intentFor(snapshot, "retry-evolution-job"));
                },
                "The failed Evolution method is running again with the original Session inputs.",
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
              busy={busy || developmentEvolutionActive}
              onSave={(config) => void act(
                () => provider.updateProject(displayedProject.project_id, displayedProject.display_name, config, intentFor(snapshot, "save-evolution")),
                "Project configuration saved. Validate again before the next Task.",
              )}
              onStartRun={(sourceTaskIds, selections) => void act(
                async () => {
                  if (!provider.startEvolutionRun) throw new Error("Standalone Evolution Runs are unavailable in this build.");
                  await provider.startEvolutionRun(
                    displayedProject.project_id,
                    sourceTaskIds,
                    selections,
                    intentFor(snapshot, "start-evolution-run"),
                  );
                },
                "Evolution Run started. Its outputs remain candidates until you apply them.",
              )}
              onApplyRun={(runId) => void act(
                async () => {
                  if (!provider.applyEvolutionRun) throw new Error("Evolution candidate apply is unavailable in this build.");
                  await provider.applyEvolutionRun(runId, intentFor(snapshot, "apply-evolution-run"));
                },
                "Evolution candidate applied. Future Sessions will use the updated context.",
              )}
              onRetryJob={(jobId) => void act(
                async () => {
                  if (!provider.retryEvolutionJob) throw new Error("Evolution retry is unavailable in this build.");
                  await provider.retryEvolutionJob(jobId, intentFor(snapshot, "retry-evolution-job"));
                },
                "Evolution method retry started with the same evidence.",
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
  const selectableHosts = useMemo(
    () =>
      snapshot.catalog.hosts.filter(
        (host) => host.availability === "selectable",
      ),
    [snapshot.catalog.hosts],
  );
  const [alias, setAlias] = useState(selectableHosts[0]?.ssh_host_alias ?? "");
  const [displayName, setDisplayName] = useState("Research server");
  const visibleProfiles = useMemo(
    () => visibleConnectionProfiles(snapshot.profiles),
    [snapshot.profiles],
  );
  const dialogRef = useDialogBoundary(onClose);

  useEffect(() => {
    if (selectableHosts.length === 0) {
      if (alias !== "") setAlias("");
      return;
    }
    if (!selectableHosts.some((host) => host.ssh_host_alias === alias)) {
      setAlias(selectableHosts[0]!.ssh_host_alias);
    }
  }, [alias, selectableHosts]);

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
    const selectedAlias = alias.trim();
    if (displayName.trim() === "" || selectedAlias === "") return;
    onBusy(true);
    onClearError();
    try {
      let authoritySnapshot = snapshot;
      let targetProfile = reusableSystemOpenSshProfile(
        authoritySnapshot.profiles,
        selectedAlias,
      );
      if (targetProfile === null) {
        const created = await provider.createProfile(
          displayName.trim(),
          selectedAlias,
          intentFor(authoritySnapshot, "create-profile"),
        );
        const refreshed = await onRefresh();
        if (refreshed === null) throw new Error("The new profile could not be reloaded.");
        authoritySnapshot = refreshed;
        targetProfile = refreshed.profiles.find(
          (profile): profile is RemoteWorkspaceProfileV2 =>
            profile.profile_kind === "system_openssh"
            && profile.profile_id === created.profile_id,
        ) ?? null;
        if (targetProfile === null) throw new Error("The new profile could not be reloaded.");
      }
      if (targetProfile.connection_state !== "connected") {
        await provider.connectProfile(
          targetProfile.profile_id,
          intentFor(authoritySnapshot, "connect-profile"),
        );
      }
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
          <div><span className="panel-kicker">Remote workspace</span><h2 id="v2-remote-title">Connect a server</h2></div>
          <button className="icon-button" type="button" aria-label="Close remote workspace setup" onClick={onClose} disabled={busy}><X size={18} /></button>
        </div>
        <div className="drawer-content">
          {error ? <Notice tone="error" title="Connection action failed" detail={error} onDismiss={onClearError} /> : null}
          <section className="form-section">
            <div className="v2-section-heading"><div><h3>Configured SSH host</h3><p>OpenEvo reads aliases from your system <code>~/.ssh/config</code> and invokes the equivalent of <code>ssh alias</code>. OpenSSH remains authoritative for host, port, user, identities, ProxyJump, agent, Keychain, and trust policy.</p></div><button type="button" className="text-button" disabled={busy} onClick={() => void mutate(() => provider.rescanSshHosts(intentFor(snapshot, "rescan-hosts")))}><RefreshCw size={14} /> Rescan</button></div>
            {snapshot.catalog.warnings.length > 0 ? (
              <div className="v2-catalog-warning" role="status"><AlertCircle size={16} /><span><strong>Some SSH configuration entries could not be listed.</strong> Fix your system OpenSSH configuration, then rescan.</span></div>
            ) : null}
            <label>Workspace name<input maxLength={256} value={displayName} onChange={(event) => setDisplayName(event.target.value)} /></label>
            {selectableHosts.length > 0 ? (
              <label>SSH host alias<select autoFocus value={alias} onChange={(event) => setAlias(event.target.value)}>{selectableHosts.map((host) => <option key={host.ssh_host_alias} value={host.ssh_host_alias}>{host.ssh_host_alias}</option>)}</select></label>
            ) : (
              <div className="v2-catalog-warning" role="status"><AlertCircle size={16} /><span><strong>No usable SSH aliases were found.</strong> Add a literal Host entry to your system <code>~/.ssh/config</code>, confirm that <code>ssh alias</code> works, then select Rescan.</span></div>
            )}
            {selectableHosts.length === 0 ? <pre className="v2-ssh-config-example">{`Host gpu-lab
    HostName gpu.example.edu
    User researcher
    Port 22`}</pre> : null}
          </section>

          {visibleProfiles.length > 0 ? (
            <section className="form-section">
              <h3>Saved workspaces</h3>
              <div className="v2-profile-list">
                {visibleProfiles.map((profile) => (
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
          <button type="button" className="primary-button" disabled={busy || displayName.trim() === "" || alias.trim() === ""} onClick={() => void saveAndConnect()}>{busy ? <LoaderCircle className="spin" size={15} /> : <Server size={15} />} Save and connect</button>
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
            <>
              <button type="button" className="secondary-button" disabled={busy || ["connecting", "bootstrapping", "negotiating", "prompt_pending"].includes(profile.connection_state)} onClick={() => void mutate(() => provider.connectProfile(profile.profile_id, intentFor(snapshot, "connect-profile")))}>Connect</button>
              {profile.connection_state === "disconnected" || profile.connection_state === "failed" ? (
                <button type="button" className="text-button" disabled={busy} onClick={() => void mutate(() => provider.deleteProfile(profile.profile_id, intentFor(snapshot, "delete-profile")))}>Remove</button>
              ) : null}
            </>
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

type ProjectWorkspacePresentationV2 = NonNullable<
  NonNullable<DesktopProductSnapshotV2["runtimePresentation"]>["workspaces"]
>[string];

function ProjectExplorerV2({
  projects,
  activeProject,
  workspace,
  selectedPath,
  busy,
  fileTransferAvailable,
  onSelectProject,
  onCreateProject,
  onSelectFile,
  onUpload,
  generation,
}: {
  readonly projects: readonly ProjectV2[];
  readonly activeProject: ProjectV2 | null;
  readonly workspace: ProjectWorkspacePresentationV2 | undefined;
  readonly selectedPath: string | null;
  readonly busy: boolean;
  readonly fileTransferAvailable: boolean;
  readonly onSelectProject: (projectId: string) => void;
  readonly onCreateProject: () => void;
  readonly onSelectFile: (path: string) => void;
  readonly onUpload: (files: readonly File[], overwrite: boolean) => void;
  readonly generation: number;
}) {
  const entries = [...(workspace?.entries ?? [])].sort((left, right) => left.path.localeCompare(right.path));
  const files = entries.filter((entry) => entry.kind === "file");
  const [collapsedDirectories, setCollapsedDirectories] = useState<ReadonlySet<string>>(new Set());
  const uploadInputRef = useRef<HTMLInputElement>(null);
  useEffect(() => setCollapsedDirectories(new Set()), [activeProject?.project_id]);
  const visibleEntries = entries.filter((entry) => {
    const parts = entry.path.split("/");
    return !parts.slice(0, -1).some((_, index) => collapsedDirectories.has(parts.slice(0, index + 1).join("/")));
  });
  return (
    <aside className="project-explorer" aria-label="Project explorer">
      <div className="explorer-heading"><span>Project</span><button type="button" aria-label="Create project" title="Create project" disabled={busy} onClick={onCreateProject}><Plus size={15} /></button></div>
      <select id="v2-project-switcher" aria-label="Select project" value={activeProject ? `project:${activeProject.project_id}` : ""} disabled={busy || projects.length === 0} onChange={(event) => onSelectProject(event.target.value.replace(/^project:/, ""))}>
        {projects.length === 0 ? <option value="">No projects</option> : null}
        {projects.map((project) => <option key={project.project_id} value={`project:${project.project_id}`}>{project.display_name}</option>)}
      </select>
      <div className="explorer-section-heading"><span>Files</span><div><span>{files.length}</span>{fileTransferAvailable ? <><input ref={uploadInputRef} className="project-workspace-file-input" type="file" multiple aria-label="Choose files to upload" onChange={(event) => {
        const selectedFiles = Array.from(event.currentTarget.files ?? []);
        event.currentTarget.value = "";
        if (selectedFiles.length === 0) return;
        const existingPaths = new Set(files.map((file) => file.path));
        const hasCollision = selectedFiles.some((file) => existingPaths.has(file.name));
        if (hasCollision && !globalThis.confirm("One or more files already exist in this workspace. Replace them?")) return;
        onUpload(selectedFiles, hasCollision);
      }} /><button type="button" aria-label="Upload files" title="Upload files" disabled={busy || activeProject === null} onClick={() => uploadInputRef.current?.click()}><Upload size={14} /></button></> : null}</div></div>
      <div className="explorer-file-tree" role="tree" aria-label="Project workspace files">
        {visibleEntries.length ? visibleEntries.map((entry) => {
          const depth = Math.max(0, entry.path.split("/").length - 1);
          const name = entry.path.split("/").at(-1) ?? entry.path;
          const selected = entry.kind === "file" && entry.path === selectedPath;
          const collapsed = entry.kind === "directory" && collapsedDirectories.has(entry.path);
          return <button type="button" role="treeitem" aria-selected={selected} aria-expanded={entry.kind === "directory" ? !collapsed : undefined} key={`${entry.kind}:${entry.path}`} className={selected ? "selected" : ""} style={{ paddingLeft: `${9 + depth * 14}px` }} onClick={() => {
            if (entry.kind === "directory") {
              setCollapsedDirectories((current) => {
                const next = new Set(current);
                if (next.has(entry.path)) next.delete(entry.path); else next.add(entry.path);
                return next;
              });
            } else if (entry.kind === "file") onSelectFile(entry.path);
          }}>
            {entry.kind === "directory" ? collapsed ? <ChevronRight size={13} /> : <ChevronDown size={13} /> : <span className="explorer-tree-spacer" />}
            {entry.kind === "directory" ? <FolderOpen size={14} /> : <FileText size={14} />}
            <span title={entry.path}>{name}</span>
          </button>;
        }) : <div className="explorer-empty">No files yet</div>}
      </div>
      {workspace?.truncated ? <div className="explorer-warning">File preview is truncated.</div> : null}
      <div className="explorer-foot"><CircleDot size={13} /><span>Active Project Head {generation}</span></div>
    </aside>
  );
}

function SessionExplorerV2({
  project,
  tasks,
  presentation,
  selectedTaskId,
  onSelectTask,
  onNewSession,
}: {
  readonly project: ProjectV2 | null;
  readonly tasks: readonly TaskV2[];
  readonly presentation: NonNullable<DesktopProductSnapshotV2["runtimePresentation"]>["tasks"] | undefined;
  readonly selectedTaskId: string | null;
  readonly onSelectTask: (taskId: string) => void;
  readonly onNewSession: () => void;
}) {
  return (
    <aside className="session-explorer" aria-label="Project sessions">
      <div className="explorer-heading"><span>Sessions</span><button type="button" aria-label="New session" title="New session" disabled={project === null} onClick={onNewSession}><Plus size={15} /></button></div>
      <div className="session-explorer-project">{project?.display_name ?? "No project selected"}</div>
      <div className="session-explorer-list">
        {tasks.length ? tasks.map((task, index) => <button type="button" className={task.task_id === selectedTaskId ? "active" : ""} key={task.task_id} onClick={() => onSelectTask(task.task_id)}><span className={`session-state-dot ${task.state}`} aria-hidden="true" /><span><strong>{presentation?.[task.task_id]?.instruction?.title ?? `Session ${tasks.length - index}`}</strong><small>{formatTimeV2(task.updated_at)}</small></span><em>{task.state.replaceAll("_", " ")}</em></button>) : <div className="explorer-empty">No Sessions yet. Create the first task in the central workspace.</div>}
      </div>
    </aside>
  );
}

function ProjectFileWorkspaceV2({
  project,
  entry,
  fileTransferAvailable,
  busy,
  onDownload,
}: {
  readonly project: ProjectV2;
  readonly entry: ProjectWorkspacePresentationV2["entries"][number];
  readonly fileTransferAvailable: boolean;
  readonly busy: boolean;
  readonly onDownload: () => void;
}) {
  return (
    <div className="workspace-stack project-file-workspace" data-testid="project-file-workspace">
      <div className="workspace-heading"><div><p className="eyebrow">{project.display_name} / File</p><h1>{entry.path}</h1><p>{entry.mediaType ?? "unknown format"} · {formatBytes(entry.byteSize)}</p></div>{fileTransferAvailable ? <button type="button" className="secondary-button" disabled={busy} onClick={onDownload}><Download size={15} /> Download</button> : null}</div>
      <section className="product-panel project-file-editor"><header><FileText size={16} /><strong>{entry.path}</strong><span>{entry.contentSha256 ? shortDigest(entry.contentSha256) : "No digest"}</span></header>{entry.content !== null ? <pre>{entry.content}</pre> : <div className="quiet-empty"><FileText size={24} /><p>This file is available to the remote Agent, but its format or size is outside the bounded browser preview.</p></div>}</section>
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
  onRetryInitialization,
  onRun,
  onCancelTask,
  onRetryTask,
  onRetryEvolutionJob,
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
  readonly onRetryInitialization: () => void;
  readonly onRun: (
    task: ScienceProjectConfigV2["task"],
    selectedEvolutionTargets: ScienceProjectConfigV2["evolution"]["targets"],
  ) => void;
  readonly onCancelTask: (task: TaskV2) => void;
  readonly onRetryTask: (task: TaskV2) => void;
  readonly onRetryEvolutionJob: (jobId: string) => void;
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
          onRetryEvolutionJob={onRetryEvolutionJob}
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
      {!ready ? (
        <div className="disabled-reason">
          <AlertCircle size={14} />
          <span>
            <strong>Next task is not ready.</strong>{" "}
            {project.state === "not_ready" && project.active_project_head === null
              ? "OpenEvo is preparing the remote services and Generation 0 Project Head. This page retries automatically."
              : "The current successor, settings, workspace, or runtime transition must finish before Core can admit another Task."}
          </span>
          {project.state === "not_ready" && project.active_project_head === null ? (
            <button type="button" className="text-button" disabled={busy} onClick={onRetryInitialization}>
              Retry now
            </button>
          ) : null}
        </div>
      ) : null}
      <div className="research-grid">
      <section className="product-panel task-panel">
        <div className="panel-heading"><div><span className="panel-kicker">Task draft</span><h2>What should the agent do next?</h2></div><span className={`state-pill ${project.state}`}>{project.state.replaceAll("_", " ")}</span></div>
        <div className="next-task-fields">
          <label>Task title<input maxLength={256} value={taskTitle} disabled={busy} onChange={(event) => setTaskTitle(event.target.value)} /></label>
          <label>Task instructions<textarea rows={6} maxLength={65_536} value={taskObjective} disabled={busy} onChange={(event) => setTaskObjective(event.target.value)} /></label>
          <p className="form-help">Starting the session saves these instructions and runs them as the next task.</p>
        </div>
        {sessionEvolutionAvailable ? <fieldset className="session-evolution-picker" disabled={busy}>
          <legend>Evolution after this session <span>Optional · formal contract</span></legend>
          <div className="session-evolution-options">{Object.entries(selectedEvolutionTargets).map(([targetId, selection]) => <article key={targetId} className={selection.enabled ? "selected" : ""}><label><input type="checkbox" checked={selection.enabled} onChange={(event) => setSelectedEvolutionTargets((current) => ({ ...current, [targetId]: { ...selection, enabled: event.target.checked } }))} /><span><strong>{targetId.replaceAll("_", " ")}</strong><small>{selection.method ?? "No method selected"}</small></span></label></article>)}</div>
          <p>{selectedEvolutionCount === 0 ? "No evolution will run after this session." : `${selectedEvolutionCount} evolution method${selectedEvolutionCount === 1 ? "" : "s"} will run after the agent replies.`}</p>
        </fieldset> : null}
        {!taskValid ? <p className="form-error" role="status">Enter both a task title and task instructions.</p> : null}
        <div className="brief-footer"><div><span>Mode</span><strong>{project.config.execution.mode === "codex_subscription_transcript" ? "Codex Subscription" : "Self-deployed"}</strong></div><div><span>Capture</span><strong>Session transcript</strong></div><div><span>Evolution</span><strong>{sessionEvolutionAvailable ? `${selectedEvolutionCount} selected` : "Run separately"}</strong></div></div>
      </section>
      <section className="product-panel active-run-panel">
        <div className="panel-heading"><div><span className="panel-kicker">{observedTask ? "Selected session" : "Active session"}</span><h2>{observedTask ? runtimePresentation?.tasks[observedTask.task_id]?.instruction?.title ?? observedTask.task_id : "No session selected"}</h2></div>{observedTask ? <span className={`state-pill ${observedTask.state}`}>{observedTask.state.replaceAll("_", " ")}</span> : <span className="muted-pill">Ready</span>}</div>
        {observedTask ? <><div className="revision-pin"><div><span>Pinned context</span><strong>Project Head {observedTask.admission.predecessor_project_head.generation}</strong></div><ArrowRight size={16} /><div><span>Admission source</span><strong>Immutable Task Admission</strong></div><span className={`state-pill ${observedTask.state}`}>{observedTask.state.replaceAll("_", " ")}</span></div><p className="v2-session-summary">{runtimePresentation?.tasks[observedTask.task_id]?.instruction?.objective ?? "The historical task text is not included in this authority response."}</p><button type="button" className="text-button" onClick={() => onSelectTask(observedTask.task_id)}>Open session result <ArrowRight size={14} /></button></> : <div className="quiet-empty"><Play size={22} /><p>Start a session when the remote workspace is ready.</p></div>}
      </section>
      </div>
      {project.active_project_head ? <details className="v2-authority-details"><summary>View immutable project authority</summary><AuthorityCardsV2 project={project} /></details> : null}
    </div>
  );
}

function reusableSystemOpenSshProfile(
  profiles: readonly RemoteProfileV2[],
  sshHostAlias: string,
): RemoteWorkspaceProfileV2 | null {
  const matching = visibleConnectionProfiles(profiles).filter(
    (profile): profile is RemoteWorkspaceProfileV2 =>
      profile.profile_kind === "system_openssh"
      && profile.ssh_host_alias === sshHostAlias,
  );
  return matching[0] ?? null;
}

function visibleConnectionProfiles(
  profiles: readonly RemoteProfileV2[],
): readonly RemoteProfileV2[] {
  const canonicalByAlias = new Map<string, RemoteWorkspaceProfileV2>();
  for (const profile of profiles) {
    if (profile.profile_kind !== "system_openssh") continue;
    const current = canonicalByAlias.get(profile.ssh_host_alias);
    if (current === undefined || compareReusableProfiles(profile, current) > 0) {
      canonicalByAlias.set(profile.ssh_host_alias, profile);
    }
  }
  const canonicalIds = new Set(
    [...canonicalByAlias.values()].map((profile) => profile.profile_id),
  );
  const systemDisplayNames = new Set(
    [...canonicalByAlias.values()].map((profile) => profile.display_name),
  );
  return profiles.filter((profile) => (
    profile.profile_kind === "system_openssh"
      ? canonicalIds.has(profile.profile_id)
      : !systemDisplayNames.has(profile.display_name)
  ));
}

function compareReusableProfiles(
  left: RemoteWorkspaceProfileV2,
  right: RemoteWorkspaceProfileV2,
): number {
  const rank = (profile: RemoteWorkspaceProfileV2): number => {
    if (["ssh_cleanup_failed", "ssh_cleanup_authority_lost"].includes(profile.failure?.code ?? "")) return 6;
    if (profile.connection_state === "connected") return 5;
    if (["connecting", "prompt_pending", "host_key_review", "bootstrapping", "negotiating", "disconnecting"].includes(profile.connection_state)) return 4;
    if (profile.active_project_id !== null) return 3;
    if (profile.connection_state === "failed") return 2;
    return 1;
  };
  const rankDelta = rank(left) - rank(right);
  if (rankDelta !== 0) return rankDelta;
  const updatedDelta = left.updated_at.localeCompare(right.updated_at);
  if (updatedDelta !== 0) return updatedDelta;
  return left.profile_id.localeCompare(right.profile_id);
}

function ProjectWorkspacePanelV2({
  workspace,
  busy,
  fileTransferAvailable,
  onUpload,
  onDownload,
}: {
  readonly workspace: NonNullable<
    NonNullable<DesktopProductSnapshotV2["runtimePresentation"]>["workspaces"]
  >[string] | undefined;
  readonly busy: boolean;
  readonly fileTransferAvailable: boolean;
  readonly onUpload: (files: readonly File[], overwrite: boolean) => void;
  readonly onDownload: (path: string) => void;
}) {
  const entries = workspace?.entries ?? [];
  const files = entries.filter((entry) => entry.kind === "file");
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const uploadInputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (selectedPath !== null && files.some((entry) => entry.path === selectedPath)) return;
    setSelectedPath(files[0]?.path ?? null);
  }, [files, selectedPath]);
  const selected = files.find((entry) => entry.path === selectedPath) ?? null;
  return (
    <section className="product-panel project-workspace-panel" data-testid="project-workspace-panel">
      <div className="panel-heading">
        <div><span className="panel-kicker">Persistent remote workspace</span><h2>Project files</h2></div>
        <div className="project-workspace-actions">
          <span className="muted-pill">{files.length} files</span>
          {fileTransferAvailable ? <>
            <input
              ref={uploadInputRef}
              className="project-workspace-file-input"
              type="file"
              multiple
              aria-label="Choose files to upload"
              onChange={(event) => {
                const selectedFiles = Array.from(event.currentTarget.files ?? []);
                event.currentTarget.value = "";
                if (selectedFiles.length === 0) return;
                const existingPaths = new Set(files.map((file) => file.path));
                const hasCollision = selectedFiles.some((file) => existingPaths.has(file.name));
                if (hasCollision && !globalThis.confirm("One or more files already exist in this workspace. Replace them?")) return;
                onUpload(selectedFiles, hasCollision);
              }}
            />
            <button type="button" className="secondary-button" disabled={busy} onClick={() => uploadInputRef.current?.click()}><Upload size={15} /> Upload files</button>
            <button type="button" className="secondary-button" disabled={busy || selected === null} onClick={() => selected && onDownload(selected.path)}><Download size={15} /> Download</button>
          </> : null}
        </div>
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
              {selected.content !== null ? <pre>{selected.content}</pre> : <p className="v2-empty-copy">No bounded browser preview is available. The Agent can still inspect the real remote-workspace file during a Session when the active harness supports its format.</p>}
            </> : <p className="v2-empty-copy">Select a readable file to preview it.</p>}
          </div>
        </div>
      )}
      {workspace?.truncated ? <p className="form-help">The server workspace contains more data than the bounded preview can display.</p> : null}
    </section>
  );
}

function saveBrowserDownload(data: Blob, fileName: string): void {
  const url = URL.createObjectURL(data);
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  link.style.display = "none";
  document.body.appendChild(link);
  link.click();
  link.remove();
  globalThis.setTimeout(() => URL.revokeObjectURL(url), 0);
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

function sessionActivityStageV2(
  state: TaskV2["state"],
  logs: readonly LogEntryV2[],
): string {
  if (state === "cancelling") return "Stopping the active Agent process safely.";
  if (state === "waiting_for_successor") return "Saving the Session result and preparing the next Project Head.";
  if (state === "admitted") return "The Session was accepted and is waiting for the background worker.";
  if (state === "preparing") return "Preparing the project workspace and evolved context.";

  const latest = logs.at(-1)?.message ?? "";
  if (latest.includes("Preparing the Codex runtime workspace")) {
    return "Preparing the project workspace and evolved context.";
  }
  if (latest.includes("Starting the Codex harness process")) {
    return "Codex is reasoning and working in the project workspace.";
  }
  if (latest.includes("Running the selected evolution methods")) {
    return "The Agent replied. OpenEvo is applying the selected evolution methods.";
  }
  if (latest.includes("published") && latest.includes("for the next session")) {
    return "Saving evolved context for the next Session.";
  }
  return latest || "Codex is reasoning and working on the task.";
}

function SessionModuleHeadingV2({
  index,
  label,
  title,
  description,
  metric,
  icon: Icon,
  tone,
}: {
  readonly index: string;
  readonly label: string;
  readonly title: string;
  readonly description: string;
  readonly metric: string;
  readonly icon: LucideIcon;
  readonly tone: "conversation" | "evolution" | "context" | "technical";
}) {
  return (
    <header className={`v2-session-module-heading tone-${tone}`}>
      <div className="v2-session-module-identity">
        <span className="v2-session-module-icon" aria-hidden="true"><Icon size={19} /></span>
        <div className="v2-session-module-copy">
          <span className="v2-session-module-overline">{index} / {label}</span>
          <h2>{title}</h2>
          <p>{description}</p>
        </div>
      </div>
      <strong className="v2-session-module-metric">{metric}</strong>
    </header>
  );
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
  onRetryEvolutionJob,
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
  readonly onRetryEvolutionJob: (jobId: string) => void;
  readonly onLoadLogs: () => void | Promise<void>;
  readonly onRetryTransition: () => void;
  readonly onAbandonTransition: () => void;
}) {
  const active = ["admitted", "preparing", "running", "cancelling"].includes(task.state);
  const sessionInProgress = [
    "admitted",
    "preparing",
    "running",
    "cancelling",
    "waiting_for_successor",
  ].includes(task.state);
  const transcriptLogs = logs.filter((entry) => entry.stream === "transcript");
  const fallbackTranscript = [
    ...(taskContent ? [{ speaker: "user" as const, text: taskContent.objective }] : []),
    ...transcriptLogs.map((entry) => ({
      speaker: "agent" as const,
      text: entry.message.replace(/^(assistant|agent):\s*/i, ""),
      sequence: entry.sequence,
    })),
  ];
  const conversationCount = presentation?.transcript.length ?? fallbackTranscript.length;
  const activityStage = sessionActivityStageV2(task.state, logs);
  const producedArtifacts = artifacts.filter((artifact) => presentation?.producedArtifactIds.includes(artifact.artifact_id));
  const usedArtifacts = artifacts.filter((artifact) => presentation?.usedArtifactIds.includes(artifact.artifact_id));
  const supportingItemCount = usedArtifacts.length + (presentation?.outputFiles.length ?? 0);
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
        <SessionModuleHeadingV2 index="01" label="Session dialogue" title="Conversation" description="The request and the Agent's response from this Session." metric={sessionInProgress ? "Agent working" : `${conversationCount} messages`} icon={MessageSquareText} tone="conversation" />
        {presentation?.transcript.length ? <div className="v2-transcript">{presentation.transcript.map((entry, index) => <article key={`${entry.speaker}-${index}`} className={entry.speaker}><span>{entry.speaker}</span><p>{entry.text}</p></article>)}</div> : !sessionInProgress && transcriptLogs.length ? <div className="v2-transcript">{fallbackTranscript.map((entry, index) => <article key={("sequence" in entry ? entry.sequence : `${entry.speaker}-${index}`)} className={entry.speaker}><span>{entry.speaker}</span><p>{entry.text}</p></article>)}</div> : !sessionInProgress ? <p className="v2-empty-copy">The agent response is not loaded yet.</p> : null}
        {sessionInProgress ? (
          <div className="v2-agent-activity" role="status" aria-live="polite" data-testid="session-agent-activity">
            <div className="v2-agent-activity-indicator" aria-hidden="true"><LoaderCircle className="spin" size={18} /></div>
            <div className="v2-agent-activity-copy">
              <strong>{task.state === "cancelling" ? "Stopping the agent" : "Agent is working"}<span className="v2-thinking-dots"><span>.</span><span>.</span><span>.</span></span></strong>
              <p>{activityStage}</p>
            </div>
            {active ? <button type="button" className="secondary-button" disabled={busy || task.state === "cancelling"} onClick={onCancel}>{task.state === "cancelling" ? "Stopping" : "Stop session"}</button> : null}
          </div>
        ) : null}
      </section>
      <section className="v2-evolution-priority v2-session-module" data-session-priority="evolution">
        <SessionModuleHeadingV2 index="02" label="Cross-session adaptation" title={presentation?.selectedEvolution?.length ? "Evolution" : "Evolution evidence"} description={presentation?.selectedEvolution?.length ? "Evolution methods attached by the legacy per-Session workflow." : "This Session transcript can be combined with other completed Sessions from the Evolution workspace."} metric={presentation?.selectedEvolution?.length ? `${producedArtifacts.length} produced` : "Available"} icon={Sparkles} tone="evolution" />
        <div className="session-evolution-summary">
          <div><span className="panel-kicker">{presentation?.selectedEvolution?.length ? "Selected for this legacy session" : "Independent workflow"}</span><strong>{presentation?.selectedEvolution?.length ? `${presentation.selectedEvolution.length} methods` : "Ready for selection"}</strong></div>
          {presentation?.selectedEvolution?.length ? (
            <EvolutionJobStatusCollectionV2
              selections={presentation.selectedEvolution}
              jobs={presentation.evolutionJobs ?? []}
              errors={presentation.evolutionErrors ?? []}
              busy={busy}
              onRetry={onRetryEvolutionJob}
            />
          ) : <p>No Evolution method ran inside this Session. Open Evolution to select this transcript, combine evidence, and create a reviewable candidate.</p>}
        </div>
        <EvolutionResultCollection
          artifacts={producedArtifacts}
          artifactPresentation={artifactPresentation}
          jobs={presentation?.evolutionJobs ?? []}
          onOpen={(artifactId) => setSelectedResult({ kind: "artifact", artifactId })}
        />
        {selectedProducedArtifact ? resultInspector : null}
      </section>
      <section className="v2-supporting-module v2-session-module" data-session-priority="supporting">
        <SessionModuleHeadingV2 index="03" label="Workspace evidence" title="Context and files" description="Inputs, workspace changes, and files associated with this Session." metric={`${supportingItemCount} items`} icon={FolderOpen} tone="context" />
        <div className="v2-supporting-results"><ResultCollection title="Context used" empty="No evolved context was recorded for this Task." artifacts={usedArtifacts} onOpen={(artifactId) => setSelectedResult({ kind: "artifact", artifactId })} /><div className="v2-result-section"><div className="v2-result-section-head"><span className="panel-kicker">Output files</span><strong>{presentation?.outputFiles.length ?? 0} files</strong></div>{presentation?.outputFiles.length ? <div className="v2-output-files">{presentation.outputFiles.map((file) => <button type="button" key={file.name} onClick={() => setSelectedResult({ kind: "output", fileName: file.name })}><FileText size={16} /><span><strong>{file.name}</strong><small>{file.summary}</small></span><ArrowRight size={14} /></button>)}</div> : <p className="v2-empty-copy">No readable output-file summary is available.</p>}</div></div>
        {selectedResult && !selectedProducedArtifact ? resultInspector : null}
      </section>
      <section className="v2-session-technical-details v2-session-module" data-session-priority="technical">
        <SessionModuleHeadingV2 index="04" label="Execution trace" title="Technical details" description="Execution status and immutable identifiers for troubleshooting." metric={task.state.replaceAll("_", " ")} icon={Activity} tone="technical" />
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

function evolutionAttemptStageLabelV2(stage: string): string {
  return ({
    input_resolution: "Resolving fixed inputs",
    method_execution: "Running Evolution method",
    output_validation: "Validating method outputs",
    artifact_persistence: "Saving produced artifacts",
    completed: "Published for the next Session",
    failed: "Evolution attempt failed",
  } as Record<string, string>)[stage] ?? stage.replaceAll("_", " ");
}

function EvolutionJobStatusCollectionV2({
  selections,
  jobs,
  errors,
  busy,
  onRetry,
}: {
  readonly selections: NonNullable<NonNullable<DesktopProductSnapshotV2["runtimePresentation"]>["tasks"][string]["selectedEvolution"]>;
  readonly jobs: NonNullable<NonNullable<DesktopProductSnapshotV2["runtimePresentation"]>["tasks"][string]["evolutionJobs"]>;
  readonly errors: NonNullable<NonNullable<DesktopProductSnapshotV2["runtimePresentation"]>["tasks"][string]["evolutionErrors"]>;
  readonly busy: boolean;
  readonly onRetry: (jobId: string) => void;
}) {
  return (
    <div className="v2-evolution-job-list">
      {selections.map((selection) => {
        const job = jobs.find((candidate) => candidate.targetId === selection.targetId);
        const latestAttempt = job?.attempts.at(-1);
        const persistedError = errors.find((candidate) => candidate.targetId === selection.targetId);
        const errorMessage = latestAttempt?.errorMessage ?? job?.error ?? persistedError?.message ?? null;
        const state = job?.state ?? "queued";
        const running = state === "queued" || state === "running";
        return (
          <article className={`v2-evolution-job ${state}`} key={selection.targetId}>
            <header>
              <div className="v2-evolution-job-title">
                <span className={`v2-evolution-job-state ${state}`} aria-hidden="true">
                  {running ? <LoaderCircle className="spin" size={16} /> : state === "completed" ? <CheckCircle2 size={16} /> : <AlertCircle size={16} />}
                </span>
                <div>
                  <strong>{selection.targetId.replaceAll("_", " ")}</strong>
                  <small>{job?.requestedMethodId ?? selection.method}</small>
                </div>
              </div>
              <span className={`state-pill ${state}`}>{state}</span>
            </header>
            <div className="v2-evolution-job-progress">
              <span>{latestAttempt ? `Attempt ${latestAttempt.ordinal}` : "Waiting for job admission"}</span>
              <strong>{latestAttempt ? evolutionAttemptStageLabelV2(latestAttempt.stage) : "Pending"}</strong>
            </div>
            {errorMessage ? (
              <div className="v2-evolution-job-error" role="alert">
                <strong>{latestAttempt?.errorCode ?? "evolution_failed"}</strong>
                <p>{errorMessage}</p>
              </div>
            ) : null}
            {job ? (
              <details className="v2-evolution-attempt-details" open={state === "failed"}>
                <summary>Attempt history and logs</summary>
                <div className="v2-evolution-attempt-list">
                  {job.attempts.map((attempt) => (
                    <section key={attempt.attemptId}>
                      <div><strong>Attempt {attempt.ordinal}</strong><span className={`state-pill ${attempt.state}`}>{attempt.state}</span></div>
                      <small>{evolutionAttemptStageLabelV2(attempt.stage)} · {formatTimeV2(attempt.updatedAt)}</small>
                      {attempt.logs.length ? <ol>{attempt.logs.map((message, index) => <li key={`${attempt.attemptId}-log-${index}`}>{message}</li>)}</ol> : <p>No attempt log was recorded.</p>}
                    </section>
                  ))}
                  <p className="v2-evolution-fixed-inputs">Retry uses the same Session transcript, prior datasets, previous artifact, method, and config. It does not rerun the Agent.</p>
                </div>
              </details>
            ) : null}
            {state === "failed" && job ? (
              <div className="v2-evolution-job-actions">
                <button type="button" className="secondary-button" disabled={busy} onClick={() => onRetry(job.jobId)}>
                  <RefreshCw size={14} /> Retry this method
                </button>
              </div>
            ) : null}
          </article>
        );
      })}
    </div>
  );
}

function EvolutionResultCollection({
  artifacts,
  artifactPresentation,
  jobs,
  onOpen,
}: {
  readonly artifacts: DesktopProductSnapshotV2["artifacts"];
  readonly artifactPresentation: NonNullable<DesktopProductSnapshotV2["runtimePresentation"]>["artifacts"] | undefined;
  readonly jobs: NonNullable<NonNullable<DesktopProductSnapshotV2["runtimePresentation"]>["tasks"][string]["evolutionJobs"]>;
  readonly onOpen: (artifactId: string) => void;
}) {
  const definitions = [
    { targetId: "text_memory", title: "Memory", itemTitle: "Memory", description: "Reusable knowledge and preferences for future Sessions.", icon: BookOpen },
    { targetId: "skill_bundle", title: "Skills", itemTitle: "Skill", description: "Reusable workflows the Agent can load when relevant.", icon: Sparkles },
    { targetId: "agent_system", title: "Agent system", itemTitle: "Agent system", description: "Instructions and coordination behavior for future runs.", icon: Activity },
  ] as const;
  const targetForArtifact = (artifact: DesktopProductSnapshotV2["artifacts"][number]): string => (
    jobs.find((job) => job.artifactIds.includes(artifact.artifact_id))?.targetId
      ?? (["text_memory", "skill_bundle", "agent_system"].includes(artifact.artifact_type)
        ? artifact.artifact_type
        : "other")
  );
  const groups = definitions.map((definition) => ({
    ...definition,
    artifacts: artifacts.filter((artifact) => targetForArtifact(artifact) === definition.targetId),
  }));
  const related = artifacts.filter((artifact) => targetForArtifact(artifact) === "other");

  return (
    <div className="v2-result-section v2-evolution-result-collection">
      <div className="v2-result-section-head"><span className="panel-kicker">Evolution produced</span><strong>{artifacts.length}</strong></div>
      {artifacts.length ? (
        <div className="v2-evolution-result-groups">
          {groups.map((group) => {
            const Icon = group.icon;
            return (
              <section className={`v2-evolution-result-group ${group.targetId}`} key={group.targetId}>
                <header>
                  <span aria-hidden="true"><Icon size={18} /></span>
                  <div><h3>{group.title}</h3><p>{group.description}</p></div>
                  <strong>{group.artifacts.length}</strong>
                </header>
                {group.artifacts.length ? <div className="v2-evolution-group-items">{group.artifacts.map((artifact, index) => {
                  const preview = artifactPresentation?.[artifact.artifact_id];
                  const versionLabel = group.artifacts.length > 1 ? `Candidate ${index + 1}` : "Update";
                  return <button type="button" key={artifact.artifact_id} title={artifact.artifact_id} onClick={() => onOpen(artifact.artifact_id)}><span><strong>{group.itemTitle} · {versionLabel}</strong><small>{preview?.status ? `${artifactStatusLabel(preview.status)} · ` : ""}{formatBytes(artifact.byte_size)}</small></span><ArrowRight size={16} /></button>;
                })}</div> : <p className="v2-evolution-group-empty">No output from this Session.</p>}
              </section>
            );
          })}
          {related.length ? <section className="v2-evolution-related"><h3>Related outputs</h3>{related.map((artifact, index) => <button type="button" key={artifact.artifact_id} title={artifact.artifact_id} onClick={() => onOpen(artifact.artifact_id)}><span><strong>Supporting result {index + 1}</strong><small>{artifactTypeLabel(artifact.artifact_type)} · {formatBytes(artifact.byte_size)}</small></span><ArrowRight size={16} /></button>)}</section> : null}
        </div>
      ) : <p className="v2-empty-copy">This Task did not publish an evolution artifact.</p>}
    </div>
  );
}

function EvolutionWorkspaceV2({
  project,
  snapshot,
  provider,
  busy,
  onSave,
  onStartRun,
  onApplyRun,
  onRetryJob,
}: {
  readonly project: ProjectV2;
  readonly snapshot: DesktopProductSnapshotV2;
  readonly provider: DesktopProductProviderV2;
  readonly busy: boolean;
  readonly onSave: (config: ScienceProjectConfigV2) => void;
  readonly onStartRun: (
    sourceTaskIds: readonly string[],
    selections: readonly {
      readonly targetId: string;
      readonly method: string;
      readonly config: Readonly<Record<string, unknown>>;
    }[],
  ) => void;
  readonly onApplyRun: (runId: string) => void;
  readonly onRetryJob: (jobId: string) => void;
}) {
  const [targets, setTargets] = useState(project.config.evolution.targets);
  useEffect(() => setTargets(project.config.evolution.targets), [project.project_config_sha256]);
  const standaloneAvailable = provider.startEvolutionRun !== undefined
    && provider.applyEvolutionRun !== undefined;
  const completedTasks = useMemo(() => snapshot.tasks.filter((task) => (
    task.project_id === project.project_id && task.state === "closed"
  )), [project.project_id, snapshot.tasks]);
  const evidenceTasks = useMemo(() => completedTasks.filter((task) => (
    snapshot.runtimePresentation?.tasks[task.task_id]?.evolutionEvidenceReady === true
  )), [completedTasks, snapshot.runtimePresentation?.tasks]);
  const evidenceTaskKey = evidenceTasks.map((task) => task.task_id).join("|");
  const [selectedTaskIds, setSelectedTaskIds] = useState<readonly string[]>([]);
  useEffect(() => {
    if (!standaloneAvailable) return;
    setSelectedTaskIds(evidenceTasks.map((task) => task.task_id));
  }, [evidenceTaskKey, project.project_id, standaloneAvailable]);
  const capabilities = snapshot.capability?.project_id === project.project_id
    ? snapshot.capability.capabilities.targets.filter((target) => target.exposure === "desktop")
    : [];
  const artifacts = snapshot.artifacts.filter((artifact) => (
    artifact.project_id === project.project_id
    && !["dataset", "workspace_result", "diagnostic"].includes(artifact.artifact_type)
  ));
  const runs = [...(snapshot.runtimePresentation?.evolutionRuns ?? [])
    .filter((run) => run.projectId === project.project_id)]
    .reverse();
  const latestRun = runs[0];
  useEffect(() => {
    if (!standaloneAvailable || latestRun === undefined) return;
    setTargets({
      ...project.config.evolution.targets,
      ...Object.fromEntries(latestRun.selections.map((selection) => [selection.targetId, {
        enabled: true,
        method: selection.method,
        config: selection.config as ScienceProjectConfigV2["evolution"]["targets"][string]["config"],
      }])),
    });
  }, [latestRun?.runId, project.project_config_sha256, standaloneAvailable]);
  const allJobs = Object.values(snapshot.runtimePresentation?.tasks ?? {})
    .flatMap((task) => task.evolutionJobs ?? []);
  const latestRunArtifacts = latestRun === undefined
    ? []
    : latestRun.artifactIds.flatMap((artifactId) => {
      const artifact = artifacts.find((candidate) => candidate.artifact_id === artifactId);
      return artifact === undefined ? [] : [artifact];
    });
  const latestRunArtifactKey = latestRunArtifacts.map((artifact) => artifact.artifact_id).join("|");
  const [selectedLatestArtifactId, setSelectedLatestArtifactId] = useState<string | null>(null);
  const [latestArtifactView, setLatestArtifactView] = useState<"content" | "changes">("content");
  useEffect(() => {
    setSelectedLatestArtifactId(latestRunArtifacts[0]?.artifact_id ?? null);
    setLatestArtifactView("content");
  }, [latestRun?.runId, latestRunArtifactKey]);
  const selectedLatestArtifact = latestRunArtifacts.find(
    (artifact) => artifact.artifact_id === selectedLatestArtifactId,
  ) ?? latestRunArtifacts[0] ?? null;
  const selectedLatestPreview = selectedLatestArtifact === null
    ? undefined
    : snapshot.runtimePresentation?.artifacts[selectedLatestArtifact.artifact_id];
  const enabledSelections = Object.entries(targets).flatMap(([targetId, selection]) => (
    selection.enabled && selection.method
      ? [{ targetId, method: selection.method, config: selection.config }]
      : []
  ));
  return (
    <div className="workspace-stack" data-testid="evolution-workspace">
      <div className="workspace-heading"><div><p className="eyebrow">Evolution</p><h1>Cross-session changes</h1><p>Choose completed Session evidence, produce a candidate, review it, then apply it to future Sessions.</p></div></div>
      {project.active_project_head ? <section className="revision-strip"><div className="revision-node active"><span>Active Project Head</span><strong>Project Head {project.active_project_head.generation}</strong><small>Used by the next session</small></div></section> : null}
      {standaloneAvailable ? <section className="product-panel task-panel">
        <div className="panel-heading"><div><span className="panel-kicker">Step 1 · Evidence</span><h2>Completed Sessions</h2></div><span className="muted-pill">{selectedTaskIds.length} selected</span></div>
        {completedTasks.length === 0 ? <div className="empty-row">Complete at least one Session before running Evolution.</div> : <div className="session-evolution-options">{completedTasks.map((task) => {
          const selected = selectedTaskIds.includes(task.task_id);
          const evidenceReady = snapshot.runtimePresentation?.tasks[task.task_id]?.evolutionEvidenceReady === true;
          const title = snapshot.runtimePresentation?.tasks[task.task_id]?.instruction?.title ?? task.task_id;
          return <article key={task.task_id} className={selected ? "selected" : ""}><label><input type="checkbox" checked={selected} disabled={busy || !evidenceReady} onChange={(event) => setSelectedTaskIds((current) => event.target.checked ? [...current, task.task_id] : current.filter((id) => id !== task.task_id))} /><span><strong>{title}</strong><small>{formatTimeV2(task.updated_at)} · {evidenceReady ? "transcript evidence" : "evidence unavailable"}</small></span></label></article>;
        })}</div>}
        {completedTasks.length > evidenceTasks.length ? <Notice tone="warning" title="Some Sessions are unavailable" detail="Their transcript datasets were not sealed. Restart the updated development daemon to repair recoverable legacy Sessions; unavailable entries cannot be selected." /> : null}
      </section> : null}
      <section className="product-panel task-panel">
        <div className="panel-heading"><div><span className="panel-kicker">{standaloneAvailable ? "Step 2 · Methods" : "Verified remote registry"}</span><h2>Evolution targets</h2></div><span className="muted-pill">{shortDigest(snapshot.capability?.registry_sha256 ?? "")}</span></div>
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
          return <article key={target.target_id}><label className="v2-target-toggle"><input type="checkbox" checked={current.enabled} disabled={busy} onChange={(event) => setTargets((previous) => ({ ...previous, [target.target_id]: { enabled: event.target.checked, method: event.target.checked ? methodId || null : current.method, config: current.config } }))} /><span><strong>{target.display_name}</strong><small>{target.description}</small></span></label><label>Method<select value={methodId} disabled={busy || !current.enabled} onChange={(event) => {
            const selected = methods.find((method) => method.method_id === event.target.value);
            let defaultConfig: ScienceProjectConfigV2["evolution"]["targets"][string]["config"] = {};
            try { defaultConfig = selected ? JSON.parse(selected.default_config_json) as typeof defaultConfig : {}; } catch { defaultConfig = {}; }
            setTargets((previous) => ({ ...previous, [target.target_id]: { enabled: true, method: event.target.value, config: defaultConfig } }));
          }}><option value="">No supported default</option>{resolvers.map((resolver) => <option key={`resolver:${resolver.selection_value}`} value={resolver.selection_value} disabled={!resolver.supported}>{resolver.display_name}</option>)}{methods.map((method) => <option key={`method:${method.method_id}`} value={method.method_id}>{method.display_name}</option>)}</select></label>{current.enabled && (!methodId || !selectionAccepted) ? <p className="form-error" role="alert">This target has no method accepted by the active registry.</p> : null}</article>;
        })}</div>}
        <div className="v2-primary-row">{standaloneAvailable ? <button type="button" className="primary-button" disabled={busy || snapshot.capability === null || selectedTaskIds.length === 0 || enabledSelections.length === 0} onClick={() => onStartRun(selectedTaskIds, enabledSelections)}>{busy ? <LoaderCircle className="spin" size={15} /> : <Sparkles size={15} />} Run Evolution</button> : <button type="button" className="primary-button" disabled={busy || snapshot.capability === null} onClick={() => onSave({ ...project.config, evolution: { targets } })}>Save evolution configuration</button>}</div>
      </section>
      {standaloneAvailable ? <section className="product-panel task-panel">
        <div className="panel-heading"><div><span className="panel-kicker">Step 3 · Review and apply</span><h2>Current Evolution Result</h2></div><span className="muted-pill">{runs.length} total run{runs.length === 1 ? "" : "s"}</span></div>
        {latestRun === undefined ? <div className="empty-row">No Evolution Run yet. Session evidence remains available until you choose to use it.</div> : <article className={`v2-evolution-job v2-current-evolution-run ${latestRun.state}`}>
          <header><div className="v2-evolution-job-title"><span className={`v2-evolution-job-state ${latestRun.state}`} aria-hidden="true">{latestRun.state === "running" ? <LoaderCircle className="spin" size={16} /> : latestRun.state === "applied" || latestRun.state === "candidate_ready" ? <CheckCircle2 size={16} /> : <AlertCircle size={16} />}</span><div><strong>{latestRun.selections.map((selection) => selection.targetId.replaceAll("_", " ")).join(", ")}</strong><small>{latestRun.sourceTaskIds.length} Session{latestRun.sourceTaskIds.length === 1 ? "" : "s"} · {formatTimeV2(latestRun.createdAt)}</small></div></div><span className={`state-pill ${latestRun.state}`}>{latestRun.state.replaceAll("_", " ")}</span></header>
          {latestRun.error ? <div className="v2-evolution-job-error" role="alert"><strong>Evolution Run failed</strong><p>{latestRun.error}</p></div> : null}
          {latestRun.state === "running" ? <div className="v2-current-evolution-empty"><LoaderCircle className="spin" size={18} /><span>Producing the current candidate…</span></div> : null}
          {latestRunArtifacts.length ? <div className="v2-current-evolution-result">
            <div className="v2-current-evolution-tabs" role="tablist" aria-label="Current evolution outputs">{latestRunArtifacts.map((artifact) => {
              const preview = snapshot.runtimePresentation?.artifacts[artifact.artifact_id];
              const active = artifact.artifact_id === selectedLatestArtifact?.artifact_id;
              return <button type="button" role="tab" aria-selected={active} className={active ? "active" : ""} key={artifact.artifact_id} onClick={() => { setSelectedLatestArtifactId(artifact.artifact_id); setLatestArtifactView("content"); }}><Sparkles size={14} /><span><strong>{artifactTypeLabel(artifact.artifact_type)}</strong><small>{artifactStatusLabel(preview?.status ?? "unavailable")}</small></span></button>;
            })}</div>
            {selectedLatestArtifact ? <div className="v2-current-evolution-viewer"><div className="v2-current-evolution-viewer-head"><div><span className="panel-kicker">{artifactTypeLabel(selectedLatestArtifact.artifact_type)}</span><h3>{selectedLatestPreview?.title ?? selectedLatestArtifact.artifact_id}</h3><p>{selectedLatestPreview?.statusDetail ?? "No readable result description is available."}</p></div><span className="muted-pill">{formatBytes(selectedLatestArtifact.byte_size)}</span></div>
              <div className="segmented-control" role="tablist" aria-label="Current result view"><button type="button" role="tab" aria-selected={latestArtifactView === "content"} className={latestArtifactView === "content" ? "active" : ""} onClick={() => setLatestArtifactView("content")}><FileText size={14} /> Content</button><button type="button" role="tab" aria-selected={latestArtifactView === "changes"} className={latestArtifactView === "changes" ? "active" : ""} onClick={() => setLatestArtifactView("changes")}><History size={14} /> Changes</button></div>
              {latestArtifactView === "content" ? <div className="v2-artifact-documents">{selectedLatestPreview?.documents.length ? selectedLatestPreview.documents.map((document) => <section key={document.path}><div><FileText size={14} /><strong>{document.path}</strong><small>{document.path.endsWith(".md") ? "text/markdown" : "text/plain"}</small></div><pre>{document.content}</pre></section>) : <p className="v2-empty-copy">No readable document body is available for this result.</p>}</div> : <div className="v2-artifact-diff"><div className="v2-diff-summary"><span>Compared with <strong>{selectedLatestPreview?.previousArtifactId ?? "no previous version"}</strong></span></div>{selectedLatestPreview?.diffLines.length ? <pre>{selectedLatestPreview.diffLines.map((line, index) => <span key={`${line.kind}-${index}`} className={line.kind}>{line.kind === "added" ? "+ " : line.kind === "removed" ? "− " : "  "}{line.text}</span>)}</pre> : <p className="v2-empty-copy">No textual change preview is available.</p>}</div>}
            </div> : null}
          </div> : latestRun.state !== "running" && latestRun.state !== "failed" ? <div className="v2-current-evolution-empty"><span>This run completed without a readable textual artifact.</span></div> : null}
          <div className="v2-card-actions">{latestRun.state === "candidate_ready" ? <button type="button" className="primary-button" disabled={busy} onClick={() => onApplyRun(latestRun.runId)}>Apply to future Sessions</button> : null}{latestRun.state === "applied" ? <span className="muted-pill">Active context</span> : null}{allJobs.filter((job) => latestRun.jobIds.includes(job.jobId) && job.state === "failed").map((job) => <button type="button" className="secondary-button" disabled={busy} key={job.jobId} onClick={() => onRetryJob(job.jobId)}>Retry {job.targetId.replaceAll("_", " ")}</button>)}</div>
        </article>}
        {runs.length > 1 ? <details className="v2-evolution-history"><summary><span><History size={15} /> Previous Evolution Runs</span><strong>{runs.length - 1}</strong></summary><div className="v2-evolution-job-list">{runs.slice(1).map((run) => {
          const jobs = allJobs.filter((job) => run.jobIds.includes(job.jobId));
          return <article className={`v2-evolution-job ${run.state}`} key={run.runId}><header><div className="v2-evolution-job-title"><span className={`v2-evolution-job-state ${run.state}`} aria-hidden="true">{run.state === "running" ? <LoaderCircle className="spin" size={16} /> : run.state === "applied" || run.state === "candidate_ready" ? <CheckCircle2 size={16} /> : <AlertCircle size={16} />}</span><div><strong>{run.selections.map((selection) => selection.targetId.replaceAll("_", " ")).join(", ")}</strong><small>{run.sourceTaskIds.length} Session{run.sourceTaskIds.length === 1 ? "" : "s"} · {formatTimeV2(run.createdAt)}</small></div></div><span className={`state-pill ${run.state}`}>{run.state.replaceAll("_", " ")}</span></header>{run.error ? <div className="v2-evolution-job-error" role="alert"><strong>Evolution Run failed</strong><p>{run.error}</p></div> : null}<div className="v2-card-actions">{run.state === "candidate_ready" ? <button type="button" className="primary-button" disabled={busy} onClick={() => onApplyRun(run.runId)}>Apply to future Sessions</button> : null}{run.state === "applied" ? <span className="muted-pill">Active context</span> : null}{jobs.filter((job) => job.state === "failed").map((job) => <button type="button" className="secondary-button" disabled={busy} key={job.jobId} onClick={() => onRetryJob(job.jobId)}>Retry {job.targetId.replaceAll("_", " ")}</button>)}</div></article>;
        })}</div></details> : null}
      </section> : null}
      {standaloneAvailable && artifacts.length ? <details className="v2-all-evolution-artifacts"><summary><span><FolderOpen size={15} /> Browse all Evolution artifacts</span><strong>{artifacts.length}</strong></summary><EvolutionArtifactBrowserV2 artifacts={artifacts} presentation={snapshot.runtimePresentation?.artifacts} provider={provider} /></details> : <EvolutionArtifactBrowserV2 artifacts={artifacts} presentation={snapshot.runtimePresentation?.artifacts} provider={provider} />}
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
  if (isDesktopErrorV2(error)) return error.summary;
  if (error instanceof Error && error.message.length > 0 && error.message.length <= 768) return error.message;
  return "OpenEvo Desktop could not complete this action. Refresh the current authority and try again.";
}

function isDesktopErrorV2(error: unknown): error is DesktopErrorV2 {
  if (typeof error !== "object" || error === null) return false;
  const candidate = error as Partial<DesktopErrorV2>;
  return candidate.schema_version === "2"
    && typeof candidate.code === "string"
    && typeof candidate.summary === "string"
    && candidate.summary.length > 0
    && candidate.summary.length <= 768
    && typeof candidate.retryable === "boolean"
    && typeof candidate.action === "string";
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
