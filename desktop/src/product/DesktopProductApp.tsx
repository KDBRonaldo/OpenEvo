import {
  Activity,
  AlertCircle,
  ArrowRight,
  BookOpen,
  Check,
  CheckCircle2,
  ChevronDown,
  CircleDot,
  FileDiff,
  FileText,
  FolderOpen,
  History,
  LoaderCircle,
  MemoryStick,
  PanelLeft,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  Settings,
  ShieldCheck,
  Sparkles,
  Square,
  TerminalSquare,
  Wrench,
  X,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DesktopApiError } from "../api/v1/client";
import type { OpenEvoJsonObject } from "../api/evolutionConfigSchema";
import type {
  ArtifactContentV1,
  ArtifactDiffV1,
  ArtifactV1,
  ProfileCreateV1,
  ProjectCapabilitiesV1,
  ProjectPatchV1,
  ProjectSourceV1,
  ProjectV1,
  RemoteProfileV1,
  RunV1,
  ServiceV1,
} from "../api/v1/schemas";
import {
  DesktopProductProviderUnavailableError,
  DesktopProductUserError,
  type DesktopProductProvider,
  type DesktopProductSnapshot,
  type ProductMutationIntent,
  type ProductResourceMutationIntent,
  ProductRefreshOrder,
  unavailableDesktopProductProvider,
} from "./provider";
import { MethodConfigEditor, methodConfigErrors } from "./MethodConfigEditor";

type ProductEvolutionTargets = ProjectV1["evolution"]["targets"];

type Workspace = "research" | "evolution" | "system";
type AsyncState = "idle" | "working";
type ActionRecovery = "readmit_run" | null;

export interface DesktopProductAppProps {
  provider?: DesktopProductProvider;
}

export function DesktopProductApp({
  provider = unavailableDesktopProductProvider,
}: DesktopProductAppProps) {
  const [snapshot, setSnapshot] = useState<DesktopProductSnapshot | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [workspace, setWorkspace] = useState<Workspace>("research");
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [connectionSettingsOpen, setConnectionSettingsOpen] = useState(false);
  const [creatingProject, setCreatingProject] = useState(false);
  const [actionState, setActionState] = useState<AsyncState>("idle");
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionRecovery, setActionRecovery] = useState<ActionRecovery>(null);
  const refreshOrder = useRef(new ProductRefreshOrder());

  const refresh = useCallback(async () => {
    const sequence = refreshOrder.current.begin();
    try {
      const result = await provider.refresh();
      if (!refreshOrder.current.isCurrent(sequence)) return;
      if (result.status !== "fresh") {
        setSnapshot((current) => current ? { ...current, stream: result.stream } : current);
        if (result.status === "error") setLoadError(userMessage(result.stream.error));
        return;
      }
      const next = result.snapshot;
      setSnapshot(next);
      setLoadError(null);
      setSelectedProjectId((current) => {
        if (current && next.projects.some((project) => project.project_id === current)) {
          return current;
        }
        return next.state.active_project?.project_id ?? next.projects[0]?.project_id ?? null;
      });
    } catch (error) {
      if (refreshOrder.current.isCurrent(sequence)) {
        setSnapshot((current) => current ? {
          ...current,
          stream: { status: "error", epoch: current.stream.epoch, error: error instanceof DesktopApiError ? error.apiError : null },
        } : current);
        setLoadError(userMessage(error));
      }
    }
  }, [provider]);

  useEffect(() => {
    let active = true;
    const load = async () => {
      if (!active) return;
      await refresh();
    };
    void load();
    const unsubscribe = provider.subscribe((signal) => {
      if (signal.kind === "snapshot_changed") {
        setSnapshot((current) => current ? { ...current, stream: { status: "stale", epoch: current.stream.epoch, reason: "refresh_pending" } } : current);
        void load();
      } else if (signal.kind === "stream_stale") {
        setSnapshot((current) => current ? { ...current, stream: { status: "stale", epoch: current.stream.epoch, reason: signal.reason } } : current);
      } else if (signal.kind === "stream_error") {
        setSnapshot((current) => current ? { ...current, stream: { status: "error", epoch: current.stream.epoch, error: signal.error } } : current);
      } else {
        setSnapshot((current) => current ? { ...current, stream: { status: "cursor_reset", epoch: current.stream.epoch, resumeFromEventId: null } } : current);
        void load();
      }
    });
    return () => {
      active = false;
      unsubscribe();
    };
  }, [provider, refresh]);

  const project = useMemo(
    () => snapshot?.projects.find((item) => item.project_id === selectedProjectId) ?? snapshot?.projects[0] ?? null,
    [selectedProjectId, snapshot],
  );
  const profile = project
    ? snapshot?.profiles.find((item) => item.profile_id === project.profile_id) ?? null
    : snapshot?.profiles[0] ?? null;
  const projectRuns = stableRunOrder(snapshot?.runs.filter((run) => run.project_id === project?.project_id) ?? []);
  const activeRun = projectRuns.find((run) => !isTerminal(run.state)) ?? null;

  const act = useCallback(async (action: () => Promise<unknown>, conflictRecovery: ActionRecovery = null): Promise<boolean> => {
    setActionState("working");
    setActionError(null);
    setActionRecovery(null);
    try {
      await action();
      await refresh();
      return true;
    } catch (error) {
      if (error instanceof DesktopApiError && [409, 410, 412].includes(error.apiError.http_status)) {
        if (error.apiError.http_status === 410) {
          setSnapshot((current) => current ? { ...current, stream: { status: "cursor_reset", epoch: current.stream.epoch, resumeFromEventId: null } } : current);
        }
        await refresh();
        if (error.apiError.http_status === 409 && error.apiError.category === "run") {
          setActionRecovery(conflictRecovery);
        }
      }
      setActionError(userMessage(error));
      return false;
    } finally {
      setActionState("idle");
    }
  }, [refresh]);

  if (!snapshot) {
    return (
      <div className="product-boot" data-testid="product-loading">
        {loadError ? (
          <BlockingState
            title="OpenEvo Desktop is unavailable"
            detail={loadError}
            actionLabel="Try again"
            onAction={() => void refresh()}
          />
        ) : (
          <div className="product-loading-row"><LoaderCircle className="spin" size={18} /> Loading workspace...</div>
        )}
      </div>
    );
  }

  const connection = snapshot.state.core;
  const displayedConnectionState = connection.state === "online" && profile && connection.profile_id !== profile.profile_id
    ? "disconnected"
    : connection.state;
  const capabilities = readyCapabilities(snapshot, project);
  const startReason = getStartReason(snapshot, project, profile, activeRun, actionState);
  const canStart = startReason === null;

  return (
    <div className="product-shell">
      <aside className="product-sidebar" aria-label="Primary navigation">
        <div className="product-brand" aria-label="OpenEvo Desktop">
          <span className="product-mark"><Sparkles size={17} strokeWidth={2.2} /></span>
          <span>OpenEvo</span>
        </div>
        <nav className="product-nav">
          <NavButton icon={BookOpen} label="Research" active={workspace === "research"} onClick={() => setWorkspace("research")} />
          <NavButton icon={Sparkles} label="Evolution" active={workspace === "evolution"} onClick={() => setWorkspace("evolution")} />
          <NavButton icon={Activity} label="System" active={workspace === "system"} onClick={() => setWorkspace("system")} />
        </nav>
        <div className="sidebar-foot">
          <div className="sidebar-foot-label">Current revision</div>
          <div className="sidebar-revision">
            <CircleDot size={15} />
            <span>{revisionLabel(project, projectRuns)}</span>
          </div>
        </div>
      </aside>

      <div className="product-stage">
        <header className="product-topbar">
          <div className="project-switcher-wrap">
            <label htmlFor="project-switcher">Project</label>
            <div className="project-switcher-control">
              <select
                id="project-switcher"
                value={project?.project_id ?? ""}
                onChange={(event) => setSelectedProjectId(event.target.value)}
                disabled={snapshot.projects.length === 0}
              >
                {snapshot.projects.length === 0 ? <option value="">No project</option> : null}
                {snapshot.projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.name}</option>)}
              </select>
              <ChevronDown size={15} aria-hidden="true" />
            </div>
            <IconButton label="Create project" onClick={() => { setCreatingProject(true); setSettingsOpen(true); }}><Plus size={17} /></IconButton>
          </div>
          <div className="topbar-actions">
            <ConnectionBadge state={displayedConnectionState} profileName={profile?.name ?? "Remote workspace"} />
            <IconButton label="Remote workspace settings" onClick={() => setConnectionSettingsOpen(true)}><PanelLeft size={17} /></IconButton>
            <IconButton label="Project settings" onClick={() => { setCreatingProject(false); setSettingsOpen(true); }} disabled={!project}><Settings size={17} /></IconButton>
          </div>
        </header>

        <main className="product-main">
          {actionError ? <InlineNotice
            tone="error"
            title="Action could not be completed"
            detail={actionError}
            onDismiss={() => { setActionError(null); setActionRecovery(null); }}
            actionLabel={actionRecovery === "readmit_run" ? "Re-admit session" : undefined}
            onAction={actionRecovery === "readmit_run" && project
              ? () => void act(() => provider.startRun({ ...resourceIntent(snapshot, project.etag), projectId: project.project_id }), "readmit_run")
              : undefined}
          /> : null}
          <ConnectionGate
            snapshot={snapshot}
            profile={profile}
            busy={actionState === "working"}
            onConnect={(selectedProfile) => void act(() => provider.connectProfile(selectedProfile.profile_id, resourceIntent(snapshot, selectedProfile.etag)))}
            onAccept={(profileId) => {
              const review = snapshot.state.core.host_key_review;
              if (review && profile) void act(() => provider.acceptHostKey(profileId, review, resourceIntent(snapshot, profile.etag)));
            }}
            onSetup={() => setConnectionSettingsOpen(true)}
          />
          {project && (snapshot.state.active_project?.project_id !== project.project_id || project.state !== "active") ? (
            <ProjectActivationGate
              project={project}
              busy={actionState === "working"}
              onActivate={() => void act(() => provider.activateProject(project.project_id, resourceIntent(snapshot, project.etag)))}
              onRepair={() => void act(() => provider.repairProject(project.project_id, resourceIntent(snapshot, project.etag)))}
            />
          ) : null}

          {workspace === "research" ? (
            <ResearchWorkspace
              project={project}
              runs={projectRuns}
              activeRun={activeRun}
              timelines={snapshot.timelines}
              modelService={snapshot.services.find((service) => service.kind === "model") ?? null}
              canStart={canStart}
              startReason={startReason}
              busy={actionState === "working"}
              onStart={() => project && void act(() => provider.startRun({ ...resourceIntent(snapshot, project.etag), projectId: project.project_id }), "readmit_run")}
              onCancel={() => activeRun && void act(() => provider.cancelRun(activeRun.run_id, resourceIntent(snapshot, activeRun.etag)))}
              onOpenSettings={() => { setCreatingProject(false); setSettingsOpen(true); }}
              onOpenEvolution={() => setWorkspace("evolution")}
              onOpenSystem={() => setWorkspace("system")}
              onRefresh={() => void refresh()}
            />
          ) : null}
          {workspace === "evolution" ? (
            <EvolutionWorkspace
              project={project}
              runs={projectRuns}
              artifacts={snapshot.artifacts.filter((artifact) => artifact.project_id === project?.project_id)}
              provider={provider}
              onRefresh={() => void refresh()}
            />
          ) : null}
          {workspace === "system" ? (
            <SystemWorkspace
              snapshot={snapshot}
              project={project}
              profile={profile}
              busy={actionState === "working"}
              onConnect={() => profile && void act(() => provider.connectProfile(profile.profile_id, resourceIntent(snapshot, profile.etag)))}
              onRepair={() => project && void act(() => provider.repairProject(project.project_id, resourceIntent(snapshot, project.etag)))}
              onRestart={(serviceId) => {
                const service = snapshot.services.find((item) => item.service_id === serviceId);
                if (service) void act(() => provider.restartService(serviceId, resourceIntent(snapshot, service.etag)));
              }}
              onConfigure={() => setConnectionSettingsOpen(true)}
            />
          ) : null}
        </main>
      </div>

      {settingsOpen ? (
        <SettingsDrawer
          project={creatingProject ? null : project}
          profileId={profile?.profile_id ?? null}
          capability={snapshot.capability}
          capabilities={capabilities}
          busy={actionState === "working"}
          onClose={() => setSettingsOpen(false)}
          onRetryCapabilities={() => refresh()}
          onSave={async (input, actionId) => {
            const saved = await act(async () => {
              if (project && !creatingProject) {
                await provider.updateProject(project.project_id, input, resourceIntent(snapshot, project.etag, actionId));
              } else {
                if (!profile) throw new DesktopProductUserError("Add a remote workspace before creating a project.");
                const created = await provider.createProject({
                  name: input.name ?? "Untitled research",
                  profile_id: profile.profile_id,
                  task: input.task ?? { title: "Research task", objective: "Describe the research objective.", task_ref: null },
                  source: input.source ?? { kind: "scratch", display_name: "New workspace", source_ref: null },
                  execution: input.execution ?? selfDeployedExecution("Qwen/Qwen3-8B"),
                  evolution: input.evolution ?? { targets: {} },
                }, mutationIntent(snapshot, actionId));
                await provider.activateProject(created.project_id, resourceIntent(snapshot, created.etag, `${actionId}-activate`));
                setSelectedProjectId(created.project_id);
              }
            });
            if (saved) setSettingsOpen(false);
            return saved;
          }}
          onSelectSource={() => provider.selectProjectSource({ ...mutationIntent(snapshot), kind: "native_folder_snapshot" })}
          onSyncSource={project?.source.kind === "native_folder_snapshot" ? () => act(() => provider.syncProjectWorkspace(project.project_id, resourceIntent(snapshot, project.etag))) : undefined}
        />
      ) : null}
      {connectionSettingsOpen ? (
        <RemoteWorkspaceDrawer
          profile={profile}
          busy={actionState === "working"}
          onClose={() => setConnectionSettingsOpen(false)}
          onSave={async (input, actionId) => {
            const saved = await act(() => profile
              ? provider.updateProfile(profile.profile_id, input, resourceIntent(snapshot, profile.etag, actionId))
              : provider.createProfile(input, mutationIntent(snapshot, actionId)));
            if (saved) setConnectionSettingsOpen(false);
            return saved;
          }}
          onConfigureCredential={(slotKind) => profile
            ? act(() => provider.configureCredential(profile.profile_id, slotKind, resourceIntent(snapshot, profile.etag))).then(() => undefined)
            : Promise.resolve()}
        />
      ) : null}
    </div>
  );
}

function NavButton({ icon: Icon, label, active, onClick }: { icon: LucideIcon; label: string; active: boolean; onClick: () => void }) {
  return (
    <button type="button" className={`product-nav-item ${active ? "active" : ""}`} aria-current={active ? "page" : undefined} onClick={onClick}>
      <Icon size={18} strokeWidth={1.8} />
      <span>{label}</span>
    </button>
  );
}

function IconButton({ label, children, onClick, disabled = false }: { label: string; children: React.ReactNode; onClick: () => void; disabled?: boolean }) {
  return <button type="button" className="icon-button" title={disabled ? `${label} is unavailable` : label} aria-label={label} onClick={onClick} disabled={disabled}>{children}</button>;
}

function ConnectionBadge({ state, profileName }: { state: DesktopProductSnapshot["state"]["core"]["state"]; profileName: string }) {
  const tone = state === "online" ? "success" : state === "degraded" ? "warning" : isConnectionBusy(state) ? "progress" : "neutral";
  return (
    <div className={`connection-badge ${tone}`} title={`${profileName}: ${connectionLabel(state)}`}>
      {isConnectionBusy(state) ? <LoaderCircle className="spin" size={14} /> : <span className="status-dot" />}
      <span>{profileName}</span>
      <strong>{connectionLabel(state)}</strong>
    </div>
  );
}

function ConnectionGate({
  snapshot,
  profile,
  busy,
  onConnect,
  onAccept,
  onSetup,
}: {
  snapshot: DesktopProductSnapshot;
  profile: RemoteProfileV1 | null;
  busy: boolean;
  onConnect: (profile: RemoteProfileV1) => void;
  onAccept: (profileId: string) => void;
  onSetup: () => void;
}) {
  const core = snapshot.state.core;
  const profileId = profile?.profile_id ?? null;
  if (core.state === "online" && core.profile_id === profileId) return null;
  if (core.state === "degraded") {
    return <InlineNotice tone="warning" title="Remote workspace needs attention" detail={core.failure?.message ?? "Open System to review available repairs."} />;
  }
  if (core.state === "host_key_review" && core.host_key_review && profileId) {
    return (
      <section className="connection-gate host-review" aria-live="polite">
        <div className="gate-icon"><ShieldCheck size={21} /></div>
        <div className="gate-copy">
          <h2>Confirm server identity</h2>
          <p>Compare this fingerprint with the value provided by your server administrator.</p>
          <code>{core.host_key_review.fingerprint}</code>
        </div>
        <button className="primary-button" type="button" onClick={() => onAccept(profileId)} disabled={busy} title={busy ? "A connection action is already running" : "Trust this server identity"}>
          <Check size={16} /> Trust and continue
        </button>
      </section>
    );
  }
  if (isConnectionBusy(core.state)) {
    const progress = snapshot.activeOperation?.progress;
    return (
      <section className="connection-gate" aria-live="polite">
        <div className="gate-icon progress"><LoaderCircle className="spin" size={21} /></div>
        <div className="gate-copy">
          <h2>{connectionHeading(core.state)}</h2>
          <p>{progress?.label ?? "Preparing the remote research environment."}</p>
          {progress ? <Progress value={progress.current} max={progress.total} /> : null}
        </div>
      </section>
    );
  }
  if (!profile) {
    return (
      <section className="connection-gate" aria-live="polite">
        <div className="gate-icon"><PanelLeft size={21} /></div>
        <div className="gate-copy"><h2>Add a remote workspace</h2><p>Enter the server details used for research sessions.</p></div>
        <button className="primary-button" type="button" onClick={onSetup}><Plus size={16} /> Add workspace</button>
      </section>
    );
  }
  const credentialReason = missingCredentialReason(profile);
  return (
    <section className="connection-gate" aria-live="polite">
      <div className="gate-icon"><PanelLeft size={21} /></div>
      <div className="gate-copy">
        <h2>{core.state === "online" ? "Switch remote workspace" : "Remote workspace is offline"}</h2>
        <p>{credentialReason ?? (core.state === "online" ? "Connect this project's assigned workspace before activating or running it." : core.failure?.message ?? "Connect to run research sessions and inspect evolution.")}</p>
      </div>
      <button className="primary-button" type="button" onClick={() => credentialReason ? onSetup() : onConnect(profile)} disabled={busy} title={busy ? "A connection action is already running" : credentialReason ? "Configure the required credential" : "Connect remote workspace"}>
        {credentialReason ? <Settings size={16} /> : <ArrowRight size={16} />} {credentialReason ? "Configure" : "Connect"}
      </button>
    </section>
  );
}

function ProjectActivationGate({
  project,
  busy,
  onActivate,
  onRepair,
}: {
  project: ProjectV1;
  busy: boolean;
  onActivate: () => void;
  onRepair: () => void;
}) {
  const blocked = project.state === "blocked";
  return (
    <section className="connection-gate" aria-live="polite">
      <div className="gate-icon"><FolderOpen size={21} /></div>
      <div className="gate-copy">
        <h2>{blocked ? "Project needs attention" : "Activate this project"}</h2>
        <p>{blocked ? "Repair this project before it can use its assigned remote workspace." : "Activation binds this project to its own profile and tunnel."}</p>
      </div>
      <button className="primary-button" type="button" onClick={blocked ? onRepair : onActivate} disabled={busy || project.state === "archived"}>
        {blocked ? <Wrench size={16} /> : <ArrowRight size={16} />} {blocked ? "Repair project" : "Activate project"}
      </button>
    </section>
  );
}

function ResearchWorkspace({
  project,
  runs,
  activeRun,
  timelines,
  modelService,
  canStart,
  startReason,
  busy,
  onStart,
  onCancel,
  onOpenSettings,
  onOpenEvolution,
  onOpenSystem,
  onRefresh,
}: {
  project: ProjectV1 | null;
  runs: readonly RunV1[];
  activeRun: RunV1 | null;
  timelines: DesktopProductSnapshot["timelines"];
  modelService: ServiceV1 | null;
  canStart: boolean;
  startReason: string | null;
  busy: boolean;
  onStart: () => void;
  onCancel: () => void;
  onOpenSettings: () => void;
  onOpenEvolution: () => void;
  onOpenSystem: () => void;
  onRefresh: () => void;
}) {
  if (!project) {
    return <EmptyState icon={FolderOpen} title="Create a research project" detail="Define a task and source to begin a session." action="Create project" onAction={onOpenSettings} />;
  }
  const latestTerminal = runs.find((run) => isTerminal(run.state));
  const recover = (run: RunV1) => {
    const action = run.error?.repair_action;
    if (action === "reconnect_required" || action === "upgrade_required") return { label: "Open System", onClick: onOpenSystem };
    if (action === "user_input_required") return { label: "Edit project", onClick: onOpenSettings };
    return { label: "Retry session", onClick: onStart };
  };
  return (
    <div className="workspace-stack" data-testid="research-workspace">
      <div className="workspace-heading">
        <div>
          <p className="eyebrow">Research</p>
          <h1>{project.task.title}</h1>
          <p>{project.task.objective}</p>
        </div>
        <div className="heading-actions">
          <button className="secondary-button" type="button" onClick={onOpenSettings}><Settings size={16} /> Edit project</button>
          <button className="primary-button" type="button" onClick={onStart} disabled={!canStart} title={startReason ?? "Start a new research session"}>
            <Play size={16} fill="currentColor" /> Start session
          </button>
        </div>
      </div>
      {startReason && !activeRun ? <div className="disabled-reason"><AlertCircle size={14} /> <span>{startReason}</span>{startReason.startsWith("Refresh") ? <button type="button" className="text-button" onClick={onRefresh}><RefreshCw size={14} /> Refresh</button> : null}</div> : null}

      <div className="research-grid">
        <section className="product-panel task-panel">
          <div className="panel-heading">
            <div><span className="panel-kicker">Task input</span><h2>Research brief</h2></div>
            <span className="source-chip"><FolderOpen size={14} /> {project.source.display_name}</span>
          </div>
          <div className="brief-body">{project.task.objective}</div>
          <div className="brief-footer">
            <div><span>Mode</span><strong>{project.execution.mode === "self-deployed" ? "Managed model" : "Subscription"}</strong></div>
            <div><span>Capture</span><strong>Session transcript</strong></div>
            <div><span>Evolution</span><strong>{Object.values(project.evolution.targets).filter((target) => target.enabled).length} targets</strong></div>
          </div>
        </section>

        <section className="product-panel active-run-panel">
          <div className="panel-heading">
            <div><span className="panel-kicker">Active session</span><h2>{activeRun ? sessionTitle(activeRun, runs) : "No session running"}</h2></div>
            {activeRun ? <StatePill state={activeRun.state} /> : <span className="muted-pill">Ready</span>}
          </div>
          {activeRun ? (
            <>
              <RevisionPin run={activeRun} />
              <RunStatusDetail run={activeRun} modelService={modelService} onRefresh={onRefresh} />
              <Timeline entries={timelines[activeRun.run_id] ?? []} />
              <button className="danger-text-button" type="button" onClick={onCancel} disabled={busy || activeRun.state === "cancelling"} title={busy ? "Another action is running" : "Cancel this session"}>
                <Square size={14} fill="currentColor" /> Cancel session
              </button>
            </>
          ) : latestTerminal ? (
            <RunOutcomeSummary run={latestTerminal} onOpenEvolution={onOpenEvolution} recovery={recover(latestTerminal)} />
          ) : (
            <div className="quiet-empty"><Play size={22} /><p>Start a session when the remote workspace is ready.</p></div>
          )}
        </section>
      </div>

      <section className="history-section">
        <div className="section-heading"><div><History size={17} /><h2>Session history</h2></div><span>{runs.length} total</span></div>
        {runs.length ? <SessionTable runs={runs} activeRun={activeRun} modelService={modelService} onRefresh={onRefresh} onRecover={recover} /> : <div className="empty-row">Completed and active sessions will appear here.</div>}
      </section>
    </div>
  );
}

function RevisionPin({ run }: { run: RunV1 }) {
  return (
    <div className="revision-pin">
      <div><span>Pinned context</span><strong>Revision {run.pinned_revision.generation}</strong></div>
      <ArrowRight size={16} />
      <div><span>Successor revision</span><strong>{run.successor_revision ? `Revision ${run.successor_revision.generation}` : "Not reported"}</strong></div>
      {run.successor_revision ? <StatePill state={run.successor_revision.state} /> : null}
    </div>
  );
}

function Timeline({ entries }: { entries: readonly DesktopProductSnapshot["timelines"][string][number][] }) {
  const visible = [...entries].sort((left, right) => compareTimestampAndId(left.occurred_at, left.entry_id, right.occurred_at, right.entry_id)).slice(-4);
  return (
    <ol className="run-timeline">
      {visible.map((entry) => (
        <li key={entry.entry_id} className={entry.state}>
          <span className="timeline-marker">{entry.state === "succeeded" ? <Check size={11} /> : entry.state === "running" ? <LoaderCircle className="spin" size={11} /> : null}</span>
          <div><strong>{entry.title}</strong><span>{entry.summary}</span></div>
        </li>
      ))}
    </ol>
  );
}

function SessionTable({
  runs,
  activeRun,
  modelService,
  onRefresh,
  onRecover,
}: {
  runs: readonly RunV1[];
  activeRun: RunV1 | null;
  modelService: ServiceV1 | null;
  onRefresh: () => void;
  onRecover: (run: RunV1) => { label: string; onClick: () => void };
}) {
  return (
    <div className="session-table" role="table" aria-label="Session history">
      <div className="session-table-head" role="row"><span role="columnheader">Session</span><span role="columnheader">State</span><span role="columnheader">Details</span><span role="columnheader">Pinned</span><span role="columnheader">Successor</span><span role="columnheader">Updated</span></div>
      {runs.map((run) => {
        const recovery = onRecover(run);
        return (
        <div className="session-table-row" role="row" key={run.run_id}>
          <strong role="cell">{sessionTitle(run, runs)}</strong>
          <span role="cell"><StatePill state={run.state} /></span>
          <span role="cell" className="session-detail">
            <RunStatusText run={run} modelService={modelService} />
            {run.state === "queued" ? <button type="button" className="text-button" onClick={onRefresh}><RefreshCw size={13} /> Refresh status</button> : null}
            {run.state === "failed" ? <button type="button" className="text-button" onClick={recovery.onClick} disabled={activeRun !== null}>{recovery.label === "Retry session" ? <RotateCcw size={13} /> : <Wrench size={13} />} {recovery.label}</button> : null}
          </span>
          <span role="cell">Revision {run.pinned_revision.generation}</span>
          <span role="cell">{run.successor_revision ? `Revision ${run.successor_revision.generation}` : "Unknown"}</span>
          <span role="cell">{formatTime(run.updated_at)}</span>
        </div>
        );
      })}
    </div>
  );
}

function RunStatusDetail({ run, modelService, onRefresh }: { run: RunV1; modelService: ServiceV1 | null; onRefresh: () => void }) {
  if (run.state !== "queued" && run.state !== "failed") return null;
  return (
    <div className={`run-status-detail ${run.state}`}>
      <div>
        <strong>{run.state === "queued" && run.queued_reason ? queuedReasonLabel(run.queued_reason.code, modelService) : stateLabel(run.state)}</strong>
        <RunStatusText run={run} modelService={modelService} />
      </div>
      {run.state === "queued" ? <button type="button" className="secondary-button" onClick={onRefresh}><RefreshCw size={14} /> Refresh status</button> : null}
    </div>
  );
}

function RunStatusText({ run, modelService }: { run: RunV1; modelService: ServiceV1 | null }) {
  if (run.state === "queued" && run.queued_reason) {
    const retry = run.queued_reason.retry_after_seconds === null ? "" : ` Check again in about ${run.queued_reason.retry_after_seconds} seconds.`;
    const model = run.queued_reason.code === "service_starting" && modelService?.state === "starting" ? ` ${modelService.health_summary}` : "";
    return <span>{run.queued_reason.summary}{model}{retry}</span>;
  }
  if (run.state === "failed" && run.error) return <span>{run.error.message}{run.error.next_action ? ` ${run.error.next_action}` : ""}</span>;
  if (run.state === "cancelled") return <span>Cancelled without reporting a successful successor.</span>;
  if (run.state === "succeeded") return <span>{run.successor_revision?.state === "active" ? `Revision ${run.successor_revision.generation} is active.` : "The session succeeded; successor readiness is not yet known."}</span>;
  return <span>{stateLabel(run.state)}</span>;
}

function queuedReasonLabel(code: NonNullable<RunV1["queued_reason"]>["code"], modelService: ServiceV1 | null): string {
  if (code === "capacity_unavailable") return "Waiting for capacity";
  if (code === "required_revision_uncommitted") return "Waiting for revision";
  if (code === "project_activation_pending") return "Project activation";
  return modelService?.state === "starting" ? "Model preparation" : "Service preparation";
}

function RunOutcomeSummary({ run, onOpenEvolution, recovery }: { run: RunV1; onOpenEvolution: () => void; recovery: { label: string; onClick: () => void } }) {
  const succeeded = run.state === "succeeded";
  return (
    <div className={`completed-summary ${run.state}`}>
      {succeeded ? <CheckCircle2 size={25} /> : run.state === "failed" ? <XCircle size={25} /> : <Square size={22} />}
      <div><strong>{succeeded ? "Latest session complete" : run.state === "failed" ? "Latest session failed" : "Latest session cancelled"}</strong><RunStatusText run={run} modelService={null} /></div>
      {succeeded && run.successor_revision?.state === "active" ? <button className="text-button" type="button" onClick={onOpenEvolution}>View changes <ArrowRight size={14} /></button> : null}
      {run.state === "failed" ? <button className="text-button" type="button" onClick={recovery.onClick}>{recovery.label === "Retry session" ? <RotateCcw size={14} /> : <Wrench size={14} />} {recovery.label}</button> : null}
    </div>
  );
}

function EvolutionWorkspace({ project, runs, artifacts, provider, onRefresh }: { project: ProjectV1 | null; runs: readonly RunV1[]; artifacts: readonly ArtifactV1[]; provider: DesktopProductProvider; onRefresh: () => void }) {
  const activeRevision = project ? authoritativeActiveRevision(project, runs) : null;
  const orderedArtifacts = activeRevision ? selectedArtifactsForRevision(artifacts, activeRevision.revision_id) : [];
  const [selectedArtifactId, setSelectedArtifactId] = useState<string | null>(orderedArtifacts[0]?.artifact_id ?? null);
  const [view, setView] = useState<"content" | "diff">("content");
  const [content, setContent] = useState<ArtifactContentV1 | null>(null);
  const [diff, setDiff] = useState<ArtifactDiffV1 | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedArtifactId || artifacts.some((artifact) => artifact.artifact_id === selectedArtifactId)) return;
    setSelectedArtifactId(orderedArtifacts[0]?.artifact_id ?? null);
  }, [artifacts, selectedArtifactId]);
  useEffect(() => {
    if (!selectedArtifactId && orderedArtifacts[0]) setSelectedArtifactId(orderedArtifacts[0].artifact_id);
  }, [artifacts, selectedArtifactId]);
  useEffect(() => {
    if (!selectedArtifactId) return;
    let active = true;
    setLoading(true);
    setError(null);
    setContent(null);
    setDiff(null);
    const request = view === "content" ? provider.getArtifactContent(selectedArtifactId) : provider.getArtifactDiff(selectedArtifactId);
    void request.then((result) => {
      if (!active) return;
      if (view === "content") setContent(result as ArtifactContentV1);
      else setDiff(result as ArtifactDiffV1);
    }).catch((reason) => active && setError(userMessage(reason))).finally(() => active && setLoading(false));
    return () => { active = false; };
  }, [provider, selectedArtifactId, view]);

  if (!project) return <EmptyState icon={Sparkles} title="No evolution history" detail="Choose a project to inspect revisions and artifacts." />;
  const selected = orderedArtifacts.find((artifact) => artifact.artifact_id === selectedArtifactId) ?? null;
  const activeGeneration = activeRevision?.generation ?? null;
  return (
    <div className="workspace-stack" data-testid="evolution-workspace">
      <div className="workspace-heading">
        <div><p className="eyebrow">Evolution</p><h1>Cross-session changes</h1><p>Review what changed and which revision the next session will use.</p></div>
      </div>
      <section className="revision-strip">
        {activeRevision ? <div className="revision-node active"><span>Active</span><strong>Revision {activeRevision.generation}</strong><small>Used by the next session</small></div> : <div className="revision-node"><span>Active revision</span><strong>Revision unknown</strong><small>Waiting for an authoritative revision reference</small><button type="button" className="text-button" onClick={onRefresh}><RefreshCw size={13} /> Refetch revision</button></div>}
      </section>
      {!activeRevision ? (
        <EmptyState icon={RefreshCw} title="Revision relation is unknown" detail="Refetch before inspecting selected artifacts for the next session." action="Refetch revision" actionIcon={RefreshCw} onAction={onRefresh} />
      ) : orderedArtifacts.length === 0 ? (
        <EmptyState icon={MemoryStick} title="No evolved artifacts yet" detail="Complete a session to create memory, skills, and agent guidance for the next revision." />
      ) : (
        <div className="artifact-layout">
          <aside className="artifact-list" aria-label="Evolution artifacts">
            <div className="artifact-list-heading"><span>{activeGeneration === null ? "Revision unknown" : `Revision ${activeGeneration}`}</span><strong>{orderedArtifacts.length} selected</strong></div>
            {orderedArtifacts.map((artifact) => (
              <button key={artifact.artifact_id} type="button" className={`artifact-list-item ${artifact.artifact_id === selected?.artifact_id ? "active" : ""}`} onClick={() => setSelectedArtifactId(artifact.artifact_id)}>
                <span className={`artifact-icon ${artifact.artifact_type}`}>{artifactIcon(artifact.artifact_type)}</span>
                <span><strong>{artifactTypeLabel(artifact.artifact_type)}</strong><small>{artifact.summary}</small></span>
                <ArrowRight size={14} />
              </button>
            ))}
          </aside>
          <section className="artifact-viewer">
            {selected ? (
              <>
                <div className="artifact-viewer-head">
                  <div><span className="panel-kicker">{artifactTypeLabel(selected.artifact_type)}</span><h2>{selected.display_name}</h2><p>{selected.summary}</p></div>
                  <div className="artifact-meta"><span>{activeGeneration === null ? "Revision unknown" : `Revision ${activeGeneration}`}</span>{selected.scores[0] ? <span>Quality {Math.round(selected.scores[0].value * 100)}%</span> : null}</div>
                </div>
                <div className="segmented-control" role="tablist" aria-label="Artifact view">
                  <button type="button" role="tab" aria-selected={view === "content"} tabIndex={view === "content" ? 0 : -1} className={view === "content" ? "active" : ""} onClick={() => setView("content")}><FileText size={14} /> Content</button>
                  <button type="button" role="tab" aria-selected={view === "diff"} tabIndex={view === "diff" ? 0 : -1} className={view === "diff" ? "active" : ""} onClick={() => setView("diff")}><FileDiff size={14} /> Changes</button>
                </div>
                <div className="artifact-body" role="tabpanel">
                  {loading ? <div className="artifact-loading"><LoaderCircle className="spin" size={17} /> Loading artifact...</div> : null}
                  {error ? <InlineNotice tone="error" title="Artifact unavailable" detail={error} /> : null}
                  {!loading && !error && view === "content" && content ? <ArtifactContent content={content} /> : null}
                  {!loading && !error && view === "diff" && diff ? <ArtifactDiff diff={diff} /> : null}
                </div>
              </>
            ) : null}
          </section>
        </div>
      )}
    </div>
  );
}

function ArtifactContent({ content }: { content: ArtifactContentV1 }) {
  const [documentId, setDocumentId] = useState(content.documents[0]?.document_id ?? "");
  useEffect(() => setDocumentId(content.documents[0]?.document_id ?? ""), [content.artifact_id]);
  const document = content.documents.find((item) => item.document_id === documentId) ?? content.documents[0];
  return (
    <>
      {content.truncated ? <InlineNotice tone="warning" title="Preview is truncated" detail={`Showing ${content.documents.length} of ${content.total_documents} documents.`} /> : null}
      {content.documents.length > 1 ? (
        <div className="document-tabs" role="tablist" aria-label="Artifact documents">
          {content.documents.map((item) => <button role="tab" aria-selected={item.document_id === document?.document_id} key={item.document_id} type="button" className={item.document_id === document?.document_id ? "active" : ""} onClick={() => setDocumentId(item.document_id)}>{item.title}</button>)}
        </div>
      ) : null}
      {document ? <pre className="artifact-document">{document.content}</pre> : null}
    </>
  );
}

function ArtifactDiff({ diff }: { diff: ArtifactDiffV1 }) {
  if (diff.hunks.length === 0) return <div className="quiet-empty"><FileDiff size={22} /><p>No textual changes are available for this revision.</p></div>;
  return (
    <div className="diff-view">
      {diff.truncated ? <InlineNotice tone="warning" title="Change preview is truncated" detail="Some changes are not shown in this preview." /> : null}
      {diff.hunks.map((hunk) => (
        <section key={hunk.hunk_id} className="diff-hunk">
          <h3>{hunk.heading}</h3>
          {hunk.lines.map((line, index) => <div key={`${hunk.hunk_id}-${index}`} className={`diff-line ${line.kind}`}><span>{line.kind === "added" ? "+" : line.kind === "removed" ? "-" : " "}</span><code>{line.text}</code></div>)}
        </section>
      ))}
    </div>
  );
}

function SystemWorkspace({ snapshot, project, profile, busy, onConnect, onRepair, onRestart, onConfigure }: { snapshot: DesktopProductSnapshot; project: ProjectV1 | null; profile: RemoteProfileV1 | null; busy: boolean; onConnect: () => void; onRepair: () => void; onRestart: (serviceId: string) => void; onConfigure: () => void }) {
  const core = snapshot.state.core;
  const diagnostic = snapshot.diagnostic;
  const actionable = diagnostic?.checks.some((check) => check.repair_action === "openevo_can_retry") ?? false;
  return (
    <div className="workspace-stack" data-testid="system-workspace">
      <div className="workspace-heading"><div><p className="eyebrow">System</p><h1>Remote environment</h1><p>Connection, environment checks, and model availability.</p></div>{actionable ? <button className="primary-button" type="button" onClick={onRepair} disabled={busy || !project} title={busy ? "A repair is already running" : "Apply available repairs"}><Wrench size={16} /> Repair available</button> : null}</div>
      <div className="system-grid">
        <section className="product-panel connection-panel">
          <div className="panel-heading"><div><span className="panel-kicker">Connection</span><h2>{profile?.name ?? "No remote workspace"}</h2></div><StatePill state={core.state} /></div>
          <dl className="definition-list">
            <div><dt>Server</dt><dd>{profile ? `${profile.host}:${profile.port}` : "Not configured"}</dd></div>
            <div><dt>Secure connection</dt><dd>{core.active_tunnel ? "Active" : "Not connected"}</dd></div>
            <div><dt>Compatibility</dt><dd>{snapshot.state.contract.compatible ? "Compatible" : "Needs update"}</dd></div>
            <div><dt>Project access</dt><dd>{snapshot.state.active_project?.connection_state === "ready" ? "Ready" : "Unavailable"}</dd></div>
          </dl>
          {profile?.credential_slots.length ? <div className="credential-summary">{profile.credential_slots.map((slot) => <CredentialStatus key={slot.kind} slot={slot} />)}</div> : null}
          <div className="system-button-row">
            <button className="secondary-button" type="button" onClick={onConfigure}><Settings size={15} /> {profile ? "Edit" : "Add workspace"}</button>
            {profile && core.state !== "online" ? <button className="secondary-button" type="button" onClick={onConnect} disabled={busy || isConnectionBusy(core.state) || missingCredentialReason(profile) !== null} title={busy ? "A connection action is already running" : missingCredentialReason(profile) ?? "Reconnect remote workspace"}><RefreshCw size={15} /> Reconnect</button> : null}
          </div>
        </section>
        <section className="product-panel checks-panel">
          <div className="panel-heading"><div><span className="panel-kicker">Environment checks</span><h2>{diagnostic ? diagnosticStatusLabel(diagnostic.status) : "Waiting for connection"}</h2></div>{diagnostic ? <StatePill state={diagnostic.status} /> : null}</div>
          <div className="check-list">
            {diagnostic?.checks.map((check) => <div className="check-row" key={check.check_id}>{check.status === "passed" ? <CheckCircle2 className="good" size={18} /> : check.status === "warning" ? <AlertCircle className="warn" size={18} /> : <CircleDot size={18} />}<div><strong>{check.label}</strong><span>{check.summary}</span></div>{check.repair_action === "openevo_can_retry" ? <span className="repair-tag">Repairable</span> : null}</div>)}
            {!diagnostic ? <div className="empty-row">Checks run after the remote workspace connects.</div> : null}
          </div>
        </section>
      </div>
      <section className="services-section">
        <div className="section-heading"><div><Activity size={17} /><h2>Services</h2></div><span>{snapshot.services.filter((service) => service.state === "healthy").length} of {snapshot.services.length} ready</span></div>
        <div className="service-list">
          {snapshot.services.map((service) => <ServiceRow key={service.service_id} service={service} busy={busy} onRestart={() => onRestart(service.service_id)} />)}
        </div>
      </section>
    </div>
  );
}

function ServiceRow({ service, busy, onRestart }: { service: ServiceV1; busy: boolean; onRestart: () => void }) {
  return (
    <div className="service-row">
      <span className={`service-indicator ${service.state}`} />
      <div><strong>{service.display_name}</strong><span>{service.health_summary}</span></div>
      <StatePill state={service.state} />
      {service.restart_supported && ["degraded", "failed", "stopped"].includes(service.state) ? <IconButton label={`Restart ${service.display_name}`} onClick={onRestart} disabled={busy}><RefreshCw size={15} /></IconButton> : <span className="service-spacer" />}
    </div>
  );
}

function RemoteWorkspaceDrawer({
  profile,
  busy,
  onClose,
  onSave,
  onConfigureCredential,
}: {
  profile: RemoteProfileV1 | null;
  busy: boolean;
  onClose: () => void;
  onSave: (input: ProfileCreateV1, actionId: string) => Promise<boolean>;
  onConfigureCredential: (slotKind: RemoteProfileV1["credential_slots"][number]["kind"]) => Promise<unknown>;
}) {
  const [name, setName] = useState(profile?.name ?? "Research server");
  const [host, setHost] = useState(profile?.host ?? "");
  const [port, setPort] = useState(String(profile?.port ?? 22));
  const [user, setUser] = useState(profile?.user ?? "");
  const [authenticationKind, setAuthenticationKind] = useState<RemoteProfileV1["authentication_kind"]>(profile?.authentication_kind ?? "ssh_agent");
  const [httpProxy, setHttpProxy] = useState(profile?.proxy.http_url ?? "");
  const [httpsProxy, setHttpsProxy] = useState(profile?.proxy.https_url ?? "");
  const [noProxy, setNoProxy] = useState(profile?.proxy.no_proxy.join(", ") ?? "");
  const [dirty, setDirty] = useState(false);
  const guardedClose = useGuardedDrawerClose(dirty, onClose);
  const dialogRef = useDialogFocus(guardedClose.requestClose);
  const saveActionId = useRef(newActionId());
  const parsedPort = Number(port);
  const valid = name.trim() !== "" && host.trim() !== "" && user.trim() !== "" && Number.isInteger(parsedPort) && parsedPort > 0 && parsedPort <= 65_535;
  const markDirty = () => { saveActionId.current = newActionId(); setDirty(true); };
  const update = (setter: (value: string) => void) => (event: React.ChangeEvent<HTMLInputElement>) => { setter(event.target.value); markDirty(); };
  const visibleSlots = credentialSlotsForAuth(profile, authenticationKind);
  return (
    <div className="drawer-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) guardedClose.requestClose(); }}>
      <aside ref={dialogRef} className="settings-drawer" role="dialog" aria-modal="true" aria-labelledby="workspace-settings-title" tabIndex={-1}>
        <div className="drawer-head"><div><span className="panel-kicker">Remote workspace</span><h2 id="workspace-settings-title">Server connection</h2></div><IconButton label="Close connection settings" onClick={guardedClose.requestClose}><X size={18} /></IconButton></div>
        <div className="drawer-content">
          <section className="form-section">
            <h3>Server</h3>
            <label>Workspace name<input value={name} onChange={update(setName)} placeholder="Research server" /></label>
            <div className="form-grid host-grid"><label>Server address<input value={host} onChange={update(setHost)} placeholder="research.example.org" /></label><label>Port<input inputMode="numeric" value={port} onChange={update(setPort)} /></label></div>
            <label>User name<input value={user} onChange={update(setUser)} /></label>
          </section>
          <section className="form-section">
            <h3>Authentication</h3>
            <label>Method<select value={authenticationKind} onChange={(event) => { setAuthenticationKind(event.target.value as RemoteProfileV1["authentication_kind"]); markDirty(); }}><option value="ssh_agent">System agent</option><option value="native_private_key">Private key</option><option value="native_password">Password</option></select></label>
            <p className="form-help">Secrets are stored by macOS and are never shown in the app.</p>
            {visibleSlots.length ? <div className="credential-list">{visibleSlots.map((slot) => <div className="credential-row" key={slot.kind}><CredentialStatus slot={slot} /><button type="button" className="secondary-button" disabled={!profile || busy} title={!profile ? "Save this workspace before configuring credentials" : `Configure ${credentialLabel(slot.kind)}`} onClick={() => void onConfigureCredential(slot.kind)}>{slot.status === "stored" ? "Replace" : "Configure"}</button></div>)}</div> : <div className="agent-note"><ShieldCheck size={17} /><span>The system agent will provide authentication.</span></div>}
          </section>
          <section className="form-section">
            <h3>Network proxy</h3>
            <label>HTTP proxy<input value={httpProxy} onChange={update(setHttpProxy)} placeholder="Optional HTTP origin" /></label>
            <label>HTTPS proxy<input value={httpsProxy} onChange={update(setHttpsProxy)} placeholder="Optional HTTPS origin" /></label>
            <label>Bypass proxy for<input value={noProxy} onChange={update(setNoProxy)} placeholder="localhost, example.org" /></label>
          </section>
        </div>
        {guardedClose.confirming ? <DiscardChangesPrompt onKeep={guardedClose.keepEditing} onDiscard={guardedClose.discard} /> : null}
        <div className="drawer-footer"><button className="secondary-button" type="button" onClick={guardedClose.requestClose}>Cancel</button><button className="primary-button" type="button" disabled={!valid || busy || (profile !== null && !dirty)} title={!valid ? "Complete the required server fields" : profile && !dirty ? "No unsaved changes" : "Save remote workspace"} onClick={() => void onSave({
          name: name.trim(),
          host: host.trim(),
          port: parsedPort,
          user: user.trim(),
          authentication_kind: authenticationKind,
          proxy: {
            http_url: httpProxy.trim() || null,
            https_url: httpsProxy.trim() || null,
            no_proxy: noProxy.split(",").map((value) => value.trim()).filter(Boolean),
          },
        }, saveActionId.current).then((saved) => { if (!saved) saveActionId.current = newActionId(); })}><Save size={15} /> {busy ? "Saving..." : "Save workspace"}</button></div>
      </aside>
    </div>
  );
}

function CredentialStatus({ slot }: { slot: RemoteProfileV1["credential_slots"][number] }) {
  return <div className={`credential-status ${slot.status}`}>{slot.status === "stored" ? <CheckCircle2 size={16} /> : slot.status === "unavailable" ? <AlertCircle size={16} /> : <CircleDot size={16} />}<span><strong>{credentialLabel(slot.kind)}</strong><small>{slot.status === "stored" ? "Stored securely" : slot.status === "unavailable" ? "Unavailable" : "Not configured"}</small></span></div>;
}

function SettingsDrawer({
  project,
  profileId,
  capability,
  capabilities,
  busy,
  onClose,
  onRetryCapabilities,
  onSave,
  onSelectSource,
  onSyncSource,
}: {
  project: ProjectV1 | null;
  profileId: string | null;
  capability: DesktopProductSnapshot["capability"];
  capabilities: ProjectCapabilitiesV1 | null;
  busy: boolean;
  onClose: () => void;
  onRetryCapabilities: () => Promise<unknown>;
  onSave: (input: ProjectPatchV1, actionId: string) => Promise<boolean>;
  onSelectSource: () => Promise<ProjectSourceV1>;
  onSyncSource?: () => Promise<boolean>;
}) {
  const [name, setName] = useState(project?.name ?? "New research project");
  const [title, setTitle] = useState(project?.task.title ?? "Research task");
  const [objective, setObjective] = useState(project?.task.objective ?? "");
  const [source, setSource] = useState<ProjectSourceV1>(project?.source ?? { kind: "scratch", display_name: "New workspace", source_ref: null });
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [mode, setMode] = useState(project?.execution.mode ?? "self-deployed");
  const [hfModel, setHfModel] = useState(project?.execution.hf_model ?? "Qwen/Qwen3-8B");
  const [codexModel, setCodexModel] = useState(project?.execution.codex_model ?? "Codex");
  const [evolution, setEvolution] = useState<ProductEvolutionTargets>(project?.evolution.targets ?? defaultEvolution(capabilities));
  const [dirty, setDirty] = useState(false);
  const [retryingCapabilities, setRetryingCapabilities] = useState(false);
  const guardedClose = useGuardedDrawerClose(dirty, onClose);
  const dialogRef = useDialogFocus(guardedClose.requestClose);
  const saveActionId = useRef(newActionId());
  const activeModel = mode === "self-deployed" ? hfModel : codexModel;
  const modeCapabilities = capabilities?.execution_mode === mode ? capabilities : null;
  const capabilityMatchesDraft = Boolean(project
    && capability
    && capability.projectId === project.project_id
    && capability.executionMode === mode);
  const capabilityRetryable = capabilityMatchesDraft && capability?.status === "unavailable";
  const markDirty = () => { saveActionId.current = newActionId(); setDirty(true); };
  const change = (setter: (value: string) => void) => (event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => { setter(event.target.value); markDirty(); };
  const reset = () => {
    setName(project?.name ?? "New research project");
    setTitle(project?.task.title ?? "Research task");
    setObjective(project?.task.objective ?? "");
    setSource(project?.source ?? { kind: "scratch", display_name: "New workspace", source_ref: null });
    setMode(project?.execution.mode ?? "self-deployed");
    setHfModel(project?.execution.hf_model ?? "Qwen/Qwen3-8B");
    setCodexModel(project?.execution.codex_model ?? "Codex");
    setEvolution(project?.evolution.targets ?? defaultEvolution(capabilities));
    setDirty(false);
    setSourceError(null);
  };
  const rows = evolutionTargetRows(modeCapabilities, evolution);
  const valid = name.trim().length > 0
    && title.trim().length > 0
    && objective.trim().length > 0
    && activeModel.trim().length > 0
    && profileId !== null
    && (!modeCapabilities || rows.every((row) => !row.selection.enabled || row.valid));
  const retryCapabilities = async () => {
    setRetryingCapabilities(true);
    try {
      await onRetryCapabilities();
    } finally {
      setRetryingCapabilities(false);
    }
  };
  return (
    <div className="drawer-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) guardedClose.requestClose(); }}>
      <aside ref={dialogRef} className="settings-drawer" role="dialog" aria-modal="true" aria-labelledby="settings-title" tabIndex={-1}>
        <div className="drawer-head"><div><span className="panel-kicker">{project ? "Project settings" : "New project"}</span><h2 id="settings-title">Research configuration</h2></div><IconButton label="Close settings" onClick={guardedClose.requestClose}><X size={18} /></IconButton></div>
        <div className="drawer-content">
          <section className="form-section">
            <h3>Project</h3>
            <label>Project name<input value={name} onChange={change(setName)} /></label>
            <label>Task title<input value={title} onChange={change(setTitle)} /></label>
            <label>Objective<textarea rows={5} value={objective} onChange={change(setObjective)} /></label>
          </section>
          <section className="form-section">
            <h3>Research source</h3>
            <div className="segmented-control wide" role="tablist" aria-label="Research source">
              <button type="button" role="tab" aria-selected={source.kind === "scratch"} tabIndex={source.kind === "scratch" ? 0 : -1} className={source.kind === "scratch" ? "active" : ""} onClick={() => { setSource({ kind: "scratch", display_name: "New workspace", source_ref: null }); setSourceError(null); markDirty(); }}>Scratch</button>
              <button type="button" role="tab" aria-selected={source.kind === "native_folder_snapshot"} tabIndex={source.kind === "native_folder_snapshot" ? 0 : -1} className={source.kind === "native_folder_snapshot" ? "active" : ""} onClick={async () => {
                setSourceError(null);
                try { setSource(await onSelectSource()); markDirty(); } catch (error) { setSourceError(userMessage(error)); }
              }}>Folder snapshot</button>
            </div>
            <div className="source-summary"><FolderOpen size={17} /><span><strong>{source.display_name}</strong><small>{source.kind === "scratch" ? "A new managed workspace will be created." : "A native snapshot reference is ready."}</small></span></div>
            {sourceError ? <p className="form-error" role="alert">{sourceError}</p> : null}
            {source.kind === "native_folder_snapshot" && onSyncSource ? <button type="button" className="secondary-button" disabled={busy} onClick={() => void onSyncSource()}><RefreshCw size={15} /> Sync snapshot</button> : null}
          </section>
          <section className="form-section">
            <h3>Model mode</h3>
            <div className="segmented-control wide" role="tablist" aria-label="Model mode"><button type="button" role="tab" aria-selected={mode === "self-deployed"} tabIndex={mode === "self-deployed" ? 0 : -1} className={mode === "self-deployed" ? "active" : ""} onClick={() => { setMode("self-deployed"); markDirty(); }}>Managed model</button><button type="button" role="tab" aria-selected={mode === "codex_subscription_transcript"} tabIndex={mode === "codex_subscription_transcript" ? 0 : -1} className={mode === "codex_subscription_transcript" ? "active" : ""} onClick={() => { setMode("codex_subscription_transcript"); markDirty(); }}>Subscription</button></div>
            {mode === "self-deployed" ? <label>Hugging Face model<input value={hfModel} onChange={change(setHfModel)} placeholder="organization/model" /></label> : <label>Codex model<input value={codexModel} onChange={change(setCodexModel)} placeholder="Model name" /></label>}
            <p className="form-help">Sessions use transcript capture. Token-level metrics are unavailable in this mode.</p>
          </section>
          <section className="form-section">
            <h3>Evolution targets</h3>
            {!modeCapabilities ? <div className="capability-unavailable" role="status"><p className="form-help">{capabilityMatchesDraft && capability?.status === "loading" ? "Capabilities are loading for this project and mode." : "Capabilities are unavailable for this project and mode."}</p>{capabilityRetryable ? <button type="button" className="secondary-button" onClick={() => void retryCapabilities()} disabled={retryingCapabilities || busy}><RefreshCw className={retryingCapabilities ? "spin" : undefined} size={14} /> Retry capabilities</button> : null}</div> : null}
            <div className="target-list">{rows.map((row) => (
              <div className={`target-toggle ${row.valid ? "" : "invalid"}`} data-target-id={row.targetId} key={row.targetId}>
                <label>
                  <input type="checkbox" role="switch" checked={row.selection.enabled} disabled={!row.selection.enabled && !row.canEnable} onChange={(event) => {
                    const enabled = event.currentTarget.checked;
                    setEvolution((current) => ({ ...current, [row.targetId]: enabled ? enableTarget(row) : { ...row.selection, enabled: false } }));
                    markDirty();
                  }} />
                  <span className="switch-track"><span /></span>
                  <span><strong>{row.displayName}</strong><small>{row.description}</small></span>
                </label>
                <select aria-label={`${row.displayName} method`} value={row.selection.method ?? ""} disabled={!row.capability || !row.selection.enabled} onChange={(event) => {
                  const selected = row.choices.find((choice) => choice.id === event.currentTarget.value);
                  if (!selected?.selectable) return;
                  setEvolution((current) => ({ ...current, [row.targetId]: { enabled: row.selection.enabled, method: selected.id, config: selected.defaultConfig } }));
                  markDirty();
                }}>
                  {row.choices.map((choice) => <option key={`${choice.kind}-${choice.id}`} value={choice.id ?? ""} disabled={!choice.selectable}>{choice.label}{choice.supported ? "" : " (unavailable)"}</option>)}
                </select>
                {row.selection.enabled && row.selectedChoice?.configSchema ? <MethodConfigEditor
                  schema={row.selectedChoice.configSchema}
                  value={row.selection.config as OpenEvoJsonObject}
                  disabled={busy}
                  onChange={(config) => {
                    setEvolution((current) => ({ ...current, [row.targetId]: { ...row.selection, config } }));
                    markDirty();
                  }}
                /> : null}
                {!row.valid && row.selection.enabled ? <small className="target-error">{row.reason}</small> : null}
                {!row.selection.enabled && !row.canEnable ? <small className="target-error">{row.reason}</small> : null}
              </div>
            ))}</div>
          </section>
        </div>
        {guardedClose.confirming ? <DiscardChangesPrompt onKeep={guardedClose.keepEditing} onDiscard={guardedClose.discard} /> : null}
        <div className="drawer-footer"><button className="secondary-button" type="button" onClick={reset} disabled={!dirty || busy} title={!dirty ? "No unsaved changes" : "Undo changes"}><RotateCcw size={15} /> Undo</button><button className="primary-button" type="button" disabled={!valid || busy || (project !== null && !dirty)} title={!profileId ? "Add a remote workspace first" : !valid ? "Complete all required fields and valid method settings" : project && !dirty ? "No unsaved changes" : "Save project settings"} onClick={() => void onSave({
          name: name.trim(),
          task: { title: title.trim(), objective: objective.trim(), task_ref: project?.task.task_ref ?? null },
          source,
          execution: mode === "self-deployed" ? selfDeployedExecution(activeModel.trim()) : subscriptionExecution(activeModel.trim()),
          evolution: { targets: evolution },
        }, saveActionId.current).then((saved) => { if (!saved) saveActionId.current = newActionId(); })}><Save size={15} /> {busy ? "Saving..." : "Save"}</button></div>
      </aside>
    </div>
  );
}

function StatePill({ state }: { state: string }) {
  return <span className={`state-pill ${state}`}>{stateLabel(state)}</span>;
}

function Progress({ value, max }: { value: number; max: number }) {
  return <div className="progress-track" role="progressbar" aria-valuenow={value} aria-valuemin={0} aria-valuemax={max}><span style={{ width: `${Math.min(100, (value / max) * 100)}%` }} /></div>;
}

function InlineNotice({ tone, title, detail, onDismiss, actionLabel, onAction }: { tone: "warning" | "error"; title: string; detail: string; onDismiss?: () => void; actionLabel?: string; onAction?: () => void }) {
  return <div className={`inline-notice ${tone}`} role={tone === "error" ? "alert" : "status"}>{tone === "error" ? <XCircle size={18} /> : <AlertCircle size={18} />}<div><strong>{title}</strong><span>{detail}</span></div>{actionLabel && onAction ? <button type="button" className="secondary-button" onClick={onAction}><RotateCcw size={14} /> {actionLabel}</button> : null}{onDismiss ? <IconButton label="Dismiss" onClick={onDismiss}><X size={15} /></IconButton> : null}</div>;
}

function DiscardChangesPrompt({ onKeep, onDiscard }: { onKeep: () => void; onDiscard: () => void }) {
  return (
    <div className="discard-changes-prompt" role="alertdialog" aria-labelledby="discard-title" aria-describedby="discard-detail">
      <div><strong id="discard-title">Discard unsaved changes?</strong><span id="discard-detail">Your draft stays open until you choose to discard it.</span></div>
      <button type="button" className="secondary-button" onClick={onKeep}>Keep editing</button>
      <button type="button" className="danger-text-button" onClick={onDiscard}>Discard changes</button>
    </div>
  );
}

function BlockingState({ title, detail, actionLabel, onAction }: { title: string; detail: string; actionLabel: string; onAction: () => void }) {
  return <div className="blocking-state" role="alert"><span className="product-mark large"><AlertCircle size={22} /></span><h1>{title}</h1><p>{detail}</p><button type="button" className="primary-button" onClick={onAction}><RefreshCw size={16} /> {actionLabel}</button></div>;
}

function EmptyState({ icon: Icon, title, detail, action, actionIcon: ActionIcon = Plus, onAction }: { icon: LucideIcon; title: string; detail: string; action?: string; actionIcon?: LucideIcon; onAction?: () => void }) {
  return <div className="product-empty"><Icon size={28} /><h2>{title}</h2><p>{detail}</p>{action && onAction ? <button className="primary-button" type="button" onClick={onAction}><ActionIcon size={16} /> {action}</button> : null}</div>;
}

function artifactIcon(type: string) {
  if (type === "text_memory") return <MemoryStick size={17} />;
  if (type === "skill_bundle") return <Sparkles size={17} />;
  return <TerminalSquare size={17} />;
}

interface EvolutionChoiceRow {
  readonly id: string | null;
  readonly label: string;
  readonly kind: "method" | "resolver" | "accepted" | "missing";
  readonly supported: boolean;
  readonly selectable: boolean;
  readonly defaultConfig: ProjectV1["evolution"]["targets"][string]["config"];
  readonly configSchema: OpenEvoJsonObject | null;
}

interface EvolutionTargetConfigRow {
  readonly targetId: string;
  readonly displayName: string;
  readonly description: string;
  readonly capability: ProjectCapabilitiesV1["targets"][number] | null;
  readonly selection: ProductEvolutionTargets[string];
  readonly choices: readonly EvolutionChoiceRow[];
  readonly selectedChoice: EvolutionChoiceRow | null;
  readonly valid: boolean;
  readonly canEnable: boolean;
  readonly reason: string;
}

function evolutionTargetRows(
  capabilities: ProjectCapabilitiesV1 | null,
  selections: ProductEvolutionTargets,
): EvolutionTargetConfigRow[] {
  const remoteIds = capabilities?.targets.map((target) => target.target_id) ?? [];
  const targetIds = [...remoteIds, ...Object.keys(selections).filter((targetId) => !remoteIds.includes(targetId))];
  return targetIds.map((targetId) => {
    const capability = capabilities?.targets.find((target) => target.target_id === targetId) ?? null;
    const selection = selections[targetId] ?? { enabled: false, method: null, config: {} };
    const visibleChoices: EvolutionChoiceRow[] = capability?.methods.map((method) => ({
      id: method.method_id,
      label: method.display_name,
      kind: "method",
      supported: method.support.overall === "supported",
      selectable: method.support.overall === "supported",
      defaultConfig: method.default_config,
      configSchema: method.config_schema as OpenEvoJsonObject,
    })) ?? [];
    const resolverChoices: EvolutionChoiceRow[] = capability?.selection_resolvers.map((resolver) => {
      const supported = resolver.resolved_methods.length > 0 && resolver.resolved_methods.every((method) => method.support.overall === "supported");
      return { id: resolver.selection_value, label: resolver.display_name, kind: "resolver", supported, selectable: supported, defaultConfig: {}, configSchema: null };
    }) ?? [];
    const choices = [...visibleChoices, ...resolverChoices];
    const selected = choices.find((choice) => choice.id === selection.method);
    if (!selected && selection.method !== null) {
      const accepted = capability?.accepted_methods.find((method) => method.method_id === selection.method);
      choices.unshift({
        id: selection.method,
        label: accepted ? `${selection.method} (existing selection)` : `${selection.method} (no longer available)`,
        kind: accepted ? "accepted" : "missing",
        supported: accepted?.support.overall === "supported",
        selectable: false,
        defaultConfig: selection.config,
        configSchema: null,
      });
    }
    if (selection.method === null) {
      choices.unshift({ id: null, label: "No method selected", kind: "missing", supported: false, selectable: false, defaultConfig: {}, configSchema: null });
    }
    const selectedChoice = choices.find((choice) => choice.id === selection.method);
    const defaultChoice = choices.find((choice) => choice.id === capability?.effective_default_method_id && choice.kind === "method" && choice.supported);
    const configErrors = selectedChoice?.configSchema ? methodConfigErrors(selectedChoice.configSchema, selection.config as OpenEvoJsonObject) : [];
    const valid = !selection.enabled || Boolean(selectedChoice?.supported && configErrors.length === 0);
    return {
      targetId,
      displayName: capability?.display_name ?? artifactTypeLabel(targetId),
      description: capability?.description ?? "This saved target is absent from current remote capabilities.",
      capability,
      selection,
      choices,
      selectedChoice: selectedChoice ?? null,
      valid,
      canEnable: capability?.effective_default_method_id !== null && Boolean((selectedChoice?.supported && selection.method) || defaultChoice),
      reason: capability
        ? capability.effective_default_method_id === null
          ? "No supported default is available from the remote registry."
          : selection.method === null
            ? "Choose a supported method before running."
          : selectedChoice?.supported
            ? configErrors[0] ?? ""
            : "The saved method is unsupported for this project and mode. Disable the target or choose a supported method."
        : "This target is unavailable in the remote registry. Disable it before running.",
    };
  });
}

function enableTarget(row: EvolutionTargetConfigRow): ProductEvolutionTargets[string] {
  const selected = row.choices.find((choice) => choice.id === row.selection.method);
  if (selected?.supported && row.selection.method !== null) return { ...row.selection, enabled: true };
  const defaultChoice = row.choices.find((choice) => choice.id === row.capability?.effective_default_method_id && choice.kind === "method" && choice.supported);
  if (!defaultChoice?.id) return row.selection;
  return { enabled: true, method: defaultChoice.id, config: structuredClone(defaultChoice.defaultConfig) };
}

function defaultEvolution(capabilities: ProjectCapabilitiesV1 | null): ProductEvolutionTargets {
  return Object.fromEntries((capabilities?.targets ?? []).filter((target) => target.release_enabled && target.effective_default_method_id !== null).map((target) => [target.target_id, {
    enabled: true,
    method: target.effective_default_method_id,
    config: target.methods.find((method) => method.method_id === target.effective_default_method_id)?.default_config ?? {},
  }])) as ProductEvolutionTargets;
}

function selfDeployedExecution(hfModel: string): ProjectV1["execution"] {
  return {
    mode: "self-deployed",
    capture_mode: "transcript",
    token_level_metrics_available: false,
    codex_model: null,
    hf_model: hfModel,
  };
}

function subscriptionExecution(codexModel: string): ProjectV1["execution"] {
  return {
    mode: "codex_subscription_transcript",
    capture_mode: "transcript",
    token_level_metrics_available: false,
    codex_model: codexModel,
    hf_model: null,
  };
}

function selectedArtifactsForRevision(artifacts: readonly ArtifactV1[], revisionId: string): ArtifactV1[] {
  const seen = new Set<string>();
  const ordered = artifacts
    .filter((artifact) => artifact.selected && artifact.artifact_type !== "parametric_memory" && artifact.revision_ids.includes(revisionId))
    .sort((left, right) => {
      const time = Date.parse(right.created_at) - Date.parse(left.created_at);
      return time || left.artifact_id.localeCompare(right.artifact_id);
    });
  return ordered.filter((artifact) => {
    if (seen.has(artifact.target_id) || artifact.artifact_type === "parametric_memory") return false;
    seen.add(artifact.target_id);
    return true;
  });
}

function currentGeneration(project: ProjectV1, runs: readonly RunV1[]): number | null {
  return authoritativeActiveRevision(project, runs)?.generation ?? null;
}

function authoritativeActiveRevision(project: ProjectV1, runs: readonly RunV1[]) {
  if (project.current_revision_id === null) return null;
  const revisions = runs
    .flatMap((run) => [run.pinned_revision, ...(run.successor_revision ? [run.successor_revision] : [])])
    .filter((item) => item.revision_id === project.current_revision_id);
  if (revisions.length === 0) return null;
  const identities = new Set(revisions.map((revision) => `${revision.generation}:${revision.manifest_digest}`));
  if (identities.size !== 1) return null;
  return revisions.find((revision) => revision.state === "active") ?? null;
}

function revisionLabel(project: ProjectV1 | null, runs: readonly RunV1[]): string {
  if (!project) return "Not available";
  const generation = currentGeneration(project, runs);
  return generation === null ? "Revision unknown" : `Revision ${generation}`;
}

function getStartReason(snapshot: DesktopProductSnapshot, project: ProjectV1 | null, profile: RemoteProfileV1 | null, activeRun: RunV1 | null, actionState: AsyncState): string | null {
  if (!project) return "Create or select a project first.";
  if (snapshot.stream.status !== "fresh") return "Refresh this view before starting a session.";
  if (!profile || snapshot.state.core.state !== "online" || !snapshot.state.core.active_tunnel || snapshot.state.core.profile_id !== profile.profile_id) return "Connect this project's remote workspace before starting a session.";
  const active = snapshot.state.active_project;
  if (!active || active.project_id !== project.project_id || active.profile_id !== project.profile_id || active.project_etag !== project.etag || active.connection_state !== "ready") return "Activate this project on its assigned remote workspace before starting a session.";
  if (project.state !== "active") return "Activate this project before starting a session.";
  const capability = snapshot.capability;
  if (!capability || capability.status !== "ready" || capability.projectId !== project.project_id || capability.executionMode !== project.execution.mode || capability.value.project_id !== project.project_id || capability.value.execution_mode !== project.execution.mode) return "Remote capabilities are unavailable for this project and mode.";
  const invalidTarget = evolutionTargetRows(capability.value, project.evolution.targets).find((row) => row.selection.enabled && !row.valid);
  if (invalidTarget) return invalidTarget.reason;
  const validation = snapshot.validation;
  if (!validation || validation.status !== "ready" || validation.projectId !== project.project_id || validation.executionMode !== project.execution.mode || validation.projectEtag !== project.etag || validation.value.project_id !== project.project_id || validation.value.project_etag !== project.etag || validation.value.capability_registry_digest !== capability.value.registry_digest || !validation.value.valid) return "Project validation is not current for this project and mode.";
  if (activeRun) return "Wait for the active session to finish or cancel it.";
  if (actionState === "working") return "Wait for the current action to finish.";
  return null;
}

function isConnectionBusy(state: DesktopProductSnapshot["state"]["core"]["state"]): boolean {
  return ["connecting", "checking", "bootstrapping", "core_starting", "reconnecting"].includes(state);
}

function connectionHeading(state: DesktopProductSnapshot["state"]["core"]["state"]): string {
  if (state === "connecting" || state === "reconnecting") return "Connecting to remote workspace";
  if (state === "checking") return "Checking environment";
  if (state === "bootstrapping") return "Preparing OpenEvo";
  return "Starting remote services";
}

function connectionLabel(state: DesktopProductSnapshot["state"]["core"]["state"]): string {
  const labels: Record<string, string> = { disconnected: "Disconnected", connecting: "Connecting", host_key_review: "Review required", checking: "Checking", bootstrapping: "Preparing", core_starting: "Starting", online: "Online", degraded: "Needs attention", reconnecting: "Reconnecting", offline: "Offline" };
  return labels[state] ?? state;
}

function stateLabel(state: string): string {
  const labels: Record<string, string> = { queued: "Queued", preparing: "Preparing", running: "Running", cancelling: "Cancelling", succeeded: "Complete", failed: "Failed", cancelled: "Cancelled", active: "Active", healthy: "Healthy", degraded: "Needs attention", stopped: "Stopped", unavailable: "Unavailable", starting: "Starting", offline: "Offline", online: "Online", blocked: "Blocked" };
  return labels[state] ?? state.replaceAll("_", " ");
}

function diagnosticStatusLabel(state: "healthy" | "degraded" | "blocked"): string {
  return state === "healthy" ? "All checks passed" : state === "degraded" ? "Attention recommended" : "Action required";
}

function artifactTypeLabel(type: string): string {
  const labels: Record<string, string> = { text_memory: "Text memory", skill_bundle: "Skills", agent_system: "Agent guidance", parametric_memory: "Parametric memory" };
  return labels[type] ?? type.replaceAll("_", " ");
}

function sessionTitle(run: RunV1, runs: readonly RunV1[]): string {
  const chronological = [...runs].sort((left, right) => compareTimestampAndId(left.created_at, left.run_id, right.created_at, right.run_id));
  return `Session ${Math.max(1, chronological.findIndex((item) => item.run_id === run.run_id) + 1)}`;
}

function stableRunOrder(runs: readonly RunV1[]): RunV1[] {
  return [...runs].sort((left, right) => {
    const time = Date.parse(right.updated_at) - Date.parse(left.updated_at);
    return time || left.run_id.localeCompare(right.run_id);
  });
}

function compareTimestampAndId(leftTime: string, leftId: string, rightTime: string, rightId: string): number {
  const time = Date.parse(leftTime) - Date.parse(rightTime);
  return time || leftId.localeCompare(rightId);
}

function formatTime(timestamp: string): string {
  return new Intl.DateTimeFormat("en", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "UTC" }).format(new Date(timestamp));
}

function isTerminal(state: RunV1["state"]): boolean {
  return state === "succeeded" || state === "failed" || state === "cancelled";
}

function userMessage(error: unknown): string {
  if (error instanceof DesktopProductProviderUnavailableError) return "The local Desktop service is not ready. Restart OpenEvo Desktop and try again.";
  if (error instanceof DesktopApiError) return error.apiError.message;
  if (error instanceof DesktopProductUserError) return error.userMessage;
  return "The request could not be completed.";
}

function readyCapabilities(snapshot: DesktopProductSnapshot, project: ProjectV1 | null): ProjectCapabilitiesV1 | null {
  const state = snapshot.capability;
  if (!project || !state || state.status !== "ready") return null;
  return state.projectId === project.project_id && state.executionMode === project.execution.mode ? state.value : null;
}

let actionSequence = 0;
function newActionId(): string {
  actionSequence += 1;
  return `renderer-action-${Date.now().toString(36)}-${actionSequence.toString(36)}`;
}

function mutationIntent(snapshot: DesktopProductSnapshot, actionId = newActionId()): ProductMutationIntent {
  if (snapshot.stream.status !== "fresh") throw new DesktopProductUserError("Refresh this view before trying again.");
  return { actionId, streamEpoch: snapshot.stream.epoch };
}

function resourceIntent(snapshot: DesktopProductSnapshot, etag: string, actionId = newActionId()): ProductResourceMutationIntent {
  return { ...mutationIntent(snapshot, actionId), etag };
}

function useGuardedDrawerClose(dirty: boolean, onClose: () => void) {
  const [confirming, setConfirming] = useState(false);
  const requestClose = useCallback(() => {
    if (dirty) {
      setConfirming(true);
      return;
    }
    onClose();
  }, [dirty, onClose]);
  return {
    confirming,
    requestClose,
    keepEditing: () => setConfirming(false),
    discard: onClose,
  };
}

function useDialogFocus(onClose: () => void) {
  const dialogRef = useRef<HTMLElement | null>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const dialog = dialogRef.current;
    const focusable = dialog?.querySelector<HTMLElement>("button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex='0']");
    (focusable ?? dialog)?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeRef.current();
      } else if (event.key === "Tab" && dialog) {
        const items = Array.from(dialog.querySelectorAll<HTMLElement>("button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex='0']"));
        const first = items[0];
        const last = items.at(-1);
        if (event.shiftKey && document.activeElement === first && last) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last && first) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      previous?.focus();
    };
  }, []);
  return dialogRef;
}

function missingCredentialReason(profile: RemoteProfileV1): string | null {
  const requiredKind = profile.authentication_kind === "native_password"
    ? "ssh_password"
    : profile.authentication_kind === "native_private_key"
      ? "ssh_private_key"
      : null;
  if (!requiredKind) return null;
  const slot = profile.credential_slots.find((item) => item.kind === requiredKind);
  return slot?.status === "stored" ? null : `Configure the ${credentialLabel(requiredKind)} before connecting.`;
}

function credentialSlotsForAuth(
  profile: RemoteProfileV1 | null,
  authenticationKind: RemoteProfileV1["authentication_kind"],
): RemoteProfileV1["credential_slots"] {
  const kinds: RemoteProfileV1["credential_slots"][number]["kind"][] = authenticationKind === "native_password"
    ? ["ssh_password"]
    : authenticationKind === "native_private_key"
      ? ["ssh_private_key", "ssh_private_key_passphrase"]
      : [];
  for (const proxyKind of ["http_proxy_password", "https_proxy_password"] as const) {
    if (profile?.credential_slots.some((slot) => slot.kind === proxyKind)) kinds.push(proxyKind);
  }
  return kinds.map((kind) => profile?.credential_slots.find((slot) => slot.kind === kind) ?? { kind, status: "empty", updated_at: null });
}

function credentialLabel(kind: RemoteProfileV1["credential_slots"][number]["kind"]): string {
  const labels: Record<RemoteProfileV1["credential_slots"][number]["kind"], string> = {
    ssh_password: "Server password",
    ssh_private_key: "Private key",
    ssh_private_key_passphrase: "Key passphrase",
    http_proxy_password: "HTTP proxy credential",
    https_proxy_password: "HTTPS proxy credential",
  };
  return labels[kind];
}
