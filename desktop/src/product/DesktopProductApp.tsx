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
import { useCallback, useEffect, useMemo, useState } from "react";
import type {
  ArtifactContentV1,
  ArtifactDiffV1,
  ArtifactV1,
  ProfileCreateV1,
  ProjectPatchV1,
  ProjectV1,
  RemoteProfileV1,
  RunV1,
  ServiceV1,
} from "../api/v1/schemas";
import {
  DesktopProductProviderUnavailableError,
  type DesktopProductProvider,
  type DesktopProductSnapshot,
  unavailableDesktopProductProvider,
} from "./provider";

type ProductEvolutionTargets = ProjectV1["evolution"]["targets"];

type Workspace = "research" | "evolution" | "system";
type AsyncState = "idle" | "working";

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

  const refresh = useCallback(async () => {
    try {
      const next = await provider.getSnapshot();
      setSnapshot(next);
      setLoadError(null);
      setSelectedProjectId((current) => {
        if (current && next.projects.some((project) => project.project_id === current)) {
          return current;
        }
        return next.state.active_project?.project_id ?? next.projects[0]?.project_id ?? null;
      });
    } catch (error) {
      setLoadError(userMessage(error));
    }
  }, [provider]);

  useEffect(() => {
    let active = true;
    const load = async () => {
      if (!active) return;
      await refresh();
    };
    void load();
    const unsubscribe = provider.subscribe(() => void load());
    return () => {
      active = false;
      unsubscribe();
    };
  }, [provider, refresh]);

  const project = useMemo(
    () => snapshot?.projects.find((item) => item.project_id === selectedProjectId) ?? snapshot?.projects[0] ?? null,
    [selectedProjectId, snapshot],
  );
  const profile = snapshot?.profiles.find((item) => item.profile_id === project?.profile_id) ?? snapshot?.profiles[0] ?? null;
  const projectRuns = snapshot?.runs.filter((run) => run.project_id === project?.project_id) ?? [];
  const activeRun = projectRuns.find((run) => !isTerminal(run.state)) ?? null;

  const act = useCallback(async (action: () => Promise<unknown>) => {
    setActionState("working");
    setActionError(null);
    try {
      await action();
      await refresh();
    } catch (error) {
      setActionError(userMessage(error));
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
  const canStart = connection.state === "online" && project?.state === "active" && !activeRun && actionState !== "working";
  const startReason = getStartReason(connection.state, project, activeRun, actionState);

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
            <ConnectionBadge state={connection.state} profileName={profile?.name ?? "Remote workspace"} />
            <IconButton label="Remote workspace settings" onClick={() => setConnectionSettingsOpen(true)}><PanelLeft size={17} /></IconButton>
            <IconButton label="Project settings" onClick={() => { setCreatingProject(false); setSettingsOpen(true); }} disabled={!project}><Settings size={17} /></IconButton>
          </div>
        </header>

        <main className="product-main">
          {actionError ? <InlineNotice tone="error" title="Action could not be completed" detail={actionError} onDismiss={() => setActionError(null)} /> : null}
          <ConnectionGate
            snapshot={snapshot}
            profile={profile}
            busy={actionState === "working"}
            onConnect={(profileId) => void act(() => provider.connectProfile(profileId))}
            onAccept={(profileId) => {
              const review = snapshot.state.core.host_key_review;
              if (review) void act(() => provider.acceptHostKey(profileId, review));
            }}
            onSetup={() => setConnectionSettingsOpen(true)}
          />

          {workspace === "research" ? (
            <ResearchWorkspace
              project={project}
              runs={projectRuns}
              activeRun={activeRun}
              timelines={snapshot.timelines}
              canStart={canStart}
              startReason={startReason}
              busy={actionState === "working"}
              onStart={() => project && void act(() => provider.startRun(project.project_id))}
              onCancel={() => activeRun && void act(() => provider.cancelRun(activeRun.run_id))}
              onOpenSettings={() => { setCreatingProject(false); setSettingsOpen(true); }}
              onOpenEvolution={() => setWorkspace("evolution")}
            />
          ) : null}
          {workspace === "evolution" ? (
            <EvolutionWorkspace
              project={project}
              runs={projectRuns}
              artifacts={snapshot.artifacts.filter((artifact) => artifact.project_id === project?.project_id)}
              provider={provider}
            />
          ) : null}
          {workspace === "system" ? (
            <SystemWorkspace
              snapshot={snapshot}
              project={project}
              profile={profile}
              busy={actionState === "working"}
              onConnect={() => profile && void act(() => provider.connectProfile(profile.profile_id))}
              onRepair={() => project && void act(() => provider.repairProject(project.project_id))}
              onRestart={(serviceId) => void act(() => provider.restartService(serviceId))}
              onConfigure={() => setConnectionSettingsOpen(true)}
            />
          ) : null}
        </main>
      </div>

      {settingsOpen ? (
        <SettingsDrawer
          project={creatingProject ? null : project}
          profileId={profile?.profile_id ?? null}
          capabilities={snapshot.capabilities}
          busy={actionState === "working"}
          onClose={() => setSettingsOpen(false)}
          onSave={async (input) => {
            await act(async () => {
              if (project && !creatingProject) {
                await provider.updateProject(project.project_id, input);
              } else {
                if (!profile) throw new Error("Add a remote workspace before creating a project.");
                const created = await provider.createProject({
                  name: input.name ?? "Untitled research",
                  profile_id: profile.profile_id,
                  task: input.task ?? { title: "Research task", objective: "Describe the research objective.", task_ref: null },
                  source: input.source ?? { kind: "scratch", display_name: "New workspace", source_ref: null },
                  execution: input.execution ?? selfDeployedExecution("Qwen/Qwen3-8B"),
                  evolution: input.evolution ?? { targets: {} },
                });
                await provider.activateProject(created.project_id);
                setSelectedProjectId(created.project_id);
              }
            });
            setSettingsOpen(false);
          }}
        />
      ) : null}
      {connectionSettingsOpen ? (
        <RemoteWorkspaceDrawer
          profile={profile}
          busy={actionState === "working"}
          onClose={() => setConnectionSettingsOpen(false)}
          onSave={async (input) => {
            await act(() => profile
              ? provider.updateProfile(profile.profile_id, input)
              : provider.createProfile(input));
            setConnectionSettingsOpen(false);
          }}
          onConfigureCredential={(slotKind) => profile
            ? act(() => provider.configureCredential(profile.profile_id, slotKind))
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
  onConnect: (profileId: string) => void;
  onAccept: (profileId: string) => void;
  onSetup: () => void;
}) {
  const core = snapshot.state.core;
  const profileId = profile?.profile_id ?? null;
  if (core.state === "online") return null;
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
        <h2>Remote workspace is offline</h2>
        <p>{credentialReason ?? core.failure?.message ?? "Connect to run research sessions and inspect evolution."}</p>
      </div>
      <button className="primary-button" type="button" onClick={() => credentialReason ? onSetup() : profileId && onConnect(profileId)} disabled={busy} title={busy ? "A connection action is already running" : credentialReason ? "Configure the required credential" : "Connect remote workspace"}>
        {credentialReason ? <Settings size={16} /> : <ArrowRight size={16} />} {credentialReason ? "Configure" : "Connect"}
      </button>
    </section>
  );
}

function ResearchWorkspace({
  project,
  runs,
  activeRun,
  timelines,
  canStart,
  startReason,
  busy,
  onStart,
  onCancel,
  onOpenSettings,
  onOpenEvolution,
}: {
  project: ProjectV1 | null;
  runs: readonly RunV1[];
  activeRun: RunV1 | null;
  timelines: DesktopProductSnapshot["timelines"];
  canStart: boolean;
  startReason: string | null;
  busy: boolean;
  onStart: () => void;
  onCancel: () => void;
  onOpenSettings: () => void;
  onOpenEvolution: () => void;
}) {
  if (!project) {
    return <EmptyState icon={FolderOpen} title="Create a research project" detail="Define a task and source to begin a session." action="Create project" onAction={onOpenSettings} />;
  }
  const latestCompleted = runs.find((run) => isTerminal(run.state));
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
      {startReason && !activeRun ? <div className="disabled-reason"><AlertCircle size={14} /> {startReason}</div> : null}

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
              <Timeline entries={timelines[activeRun.run_id] ?? []} />
              <button className="danger-text-button" type="button" onClick={onCancel} disabled={busy || activeRun.state === "cancelling"} title={busy ? "Another action is running" : "Cancel this session"}>
                <Square size={14} fill="currentColor" /> Cancel session
              </button>
            </>
          ) : latestCompleted ? (
            <div className="completed-summary">
              <CheckCircle2 size={25} />
              <div><strong>Latest session complete</strong><span>{latestCompleted.successor_revision ? `Revision ${latestCompleted.successor_revision.generation} is active.` : "No successor revision was created."}</span></div>
              {latestCompleted.successor_revision?.state === "active" ? <button className="text-button" type="button" onClick={onOpenEvolution}>View changes <ArrowRight size={14} /></button> : null}
            </div>
          ) : (
            <div className="quiet-empty"><Play size={22} /><p>Start a session when the remote workspace is ready.</p></div>
          )}
        </section>
      </div>

      <section className="history-section">
        <div className="section-heading"><div><History size={17} /><h2>Session history</h2></div><span>{runs.length} total</span></div>
        {runs.length ? <SessionTable runs={runs} /> : <div className="empty-row">Completed and active sessions will appear here.</div>}
      </section>
    </div>
  );
}

function RevisionPin({ run }: { run: RunV1 }) {
  return (
    <div className="revision-pin">
      <div><span>Pinned context</span><strong>Revision {run.pinned_revision.generation}</strong></div>
      <ArrowRight size={16} />
      <div><span>Next revision</span><strong>{run.successor_revision ? `Revision ${run.successor_revision.generation}` : "Pending"}</strong></div>
      {run.successor_revision ? <StatePill state={run.successor_revision.state} /> : null}
    </div>
  );
}

function Timeline({ entries }: { entries: readonly DesktopProductSnapshot["timelines"][string][number][] }) {
  const visible = entries.slice(-4);
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

function SessionTable({ runs }: { runs: readonly RunV1[] }) {
  return (
    <div className="session-table" role="table" aria-label="Session history">
      <div className="session-table-head" role="row"><span>Session</span><span>State</span><span>Pinned</span><span>Successor</span><span>Updated</span></div>
      {runs.map((run, index) => (
        <div className="session-table-row" role="row" key={run.run_id}>
          <strong>Session {runs.length - index}</strong>
          <StatePill state={run.state} />
          <span>Revision {run.pinned_revision.generation}</span>
          <span>{run.successor_revision ? `Revision ${run.successor_revision.generation}` : "-"}</span>
          <span>{formatTime(run.updated_at)}</span>
        </div>
      ))}
    </div>
  );
}

function EvolutionWorkspace({ project, runs, artifacts, provider }: { project: ProjectV1 | null; runs: readonly RunV1[]; artifacts: readonly ArtifactV1[]; provider: DesktopProductProvider }) {
  const orderedArtifacts = latestArtifactsByTarget(artifacts);
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
    const request = view === "content" ? provider.getArtifactContent(selectedArtifactId) : provider.getArtifactDiff(selectedArtifactId);
    void request.then((result) => {
      if (!active) return;
      if (view === "content") setContent(result as ArtifactContentV1);
      else setDiff(result as ArtifactDiffV1);
    }).catch((reason) => active && setError(userMessage(reason))).finally(() => active && setLoading(false));
    return () => { active = false; };
  }, [provider, selectedArtifactId, view]);

  if (!project) return <EmptyState icon={Sparkles} title="No evolution history" detail="Choose a project to inspect revisions and artifacts." />;
  const selected = artifacts.find((artifact) => artifact.artifact_id === selectedArtifactId) ?? artifacts[0] ?? null;
  const activeGeneration = currentGeneration(project, runs);
  const previousGeneration = Math.max(0, activeGeneration - 1);
  return (
    <div className="workspace-stack" data-testid="evolution-workspace">
      <div className="workspace-heading">
        <div><p className="eyebrow">Evolution</p><h1>Cross-session changes</h1><p>Review what changed and which revision the next session will use.</p></div>
      </div>
      <section className="revision-strip">
        <div className="revision-node previous"><span>Previous</span><strong>Revision {previousGeneration}</strong></div>
        <div className="revision-line"><ArrowRight size={17} /></div>
        <div className="revision-node active"><span>Active</span><strong>Revision {activeGeneration}</strong><small>Used by the next session</small></div>
      </section>
      {artifacts.length === 0 ? (
        <EmptyState icon={MemoryStick} title="No evolved artifacts yet" detail="Complete a session to create memory, skills, and agent guidance for the next revision." />
      ) : (
        <div className="artifact-layout">
          <aside className="artifact-list" aria-label="Evolution artifacts">
            <div className="artifact-list-heading"><span>Revision {activeGeneration}</span><strong>{orderedArtifacts.filter((item) => revisionGeneration(item, runs) === activeGeneration).length || orderedArtifacts.length} changes</strong></div>
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
                  <div className="artifact-meta"><span>Revision {revisionGeneration(selected, runs)}</span>{selected.scores[0] ? <span>Quality {Math.round(selected.scores[0].value * 100)}%</span> : null}</div>
                </div>
                <div className="segmented-control" aria-label="Artifact view">
                  <button type="button" className={view === "content" ? "active" : ""} onClick={() => setView("content")}><FileText size={14} /> Content</button>
                  <button type="button" className={view === "diff" ? "active" : ""} onClick={() => setView("diff")}><FileDiff size={14} /> Changes</button>
                </div>
                <div className="artifact-body">
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
  onSave: (input: ProfileCreateV1) => Promise<void>;
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
  const parsedPort = Number(port);
  const valid = name.trim() !== "" && host.trim() !== "" && user.trim() !== "" && Number.isInteger(parsedPort) && parsedPort > 0 && parsedPort <= 65_535;
  const update = (setter: (value: string) => void) => (event: React.ChangeEvent<HTMLInputElement>) => { setter(event.target.value); setDirty(true); };
  const visibleSlots = credentialSlotsForAuth(profile, authenticationKind);
  return (
    <div className="drawer-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <aside className="settings-drawer" role="dialog" aria-modal="true" aria-labelledby="workspace-settings-title">
        <div className="drawer-head"><div><span className="panel-kicker">Remote workspace</span><h2 id="workspace-settings-title">Server connection</h2></div><IconButton label="Close connection settings" onClick={onClose}><X size={18} /></IconButton></div>
        <div className="drawer-content">
          <section className="form-section">
            <h3>Server</h3>
            <label>Workspace name<input value={name} onChange={update(setName)} placeholder="Research server" /></label>
            <div className="form-grid host-grid"><label>Server address<input value={host} onChange={update(setHost)} placeholder="research.example.org" /></label><label>Port<input inputMode="numeric" value={port} onChange={update(setPort)} /></label></div>
            <label>User name<input value={user} onChange={update(setUser)} /></label>
          </section>
          <section className="form-section">
            <h3>Authentication</h3>
            <label>Method<select value={authenticationKind} onChange={(event) => { setAuthenticationKind(event.target.value as RemoteProfileV1["authentication_kind"]); setDirty(true); }}><option value="ssh_agent">System agent</option><option value="native_private_key">Private key</option><option value="native_password">Password</option></select></label>
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
        <div className="drawer-footer"><button className="secondary-button" type="button" onClick={onClose}>Cancel</button><button className="primary-button" type="button" disabled={!valid || busy || (profile !== null && !dirty)} title={!valid ? "Complete the required server fields" : profile && !dirty ? "No unsaved changes" : "Save remote workspace"} onClick={() => void onSave({
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
        })}><Save size={15} /> {busy ? "Saving..." : "Save workspace"}</button></div>
      </aside>
    </div>
  );
}

function CredentialStatus({ slot }: { slot: RemoteProfileV1["credential_slots"][number] }) {
  return <div className={`credential-status ${slot.status}`}>{slot.status === "stored" ? <CheckCircle2 size={16} /> : slot.status === "unavailable" ? <AlertCircle size={16} /> : <CircleDot size={16} />}<span><strong>{credentialLabel(slot.kind)}</strong><small>{slot.status === "stored" ? "Stored securely" : slot.status === "unavailable" ? "Unavailable" : "Not configured"}</small></span></div>;
}

function SettingsDrawer({ project, profileId, capabilities, busy, onClose, onSave }: { project: ProjectV1 | null; profileId: string | null; capabilities: DesktopProductSnapshot["capabilities"]; busy: boolean; onClose: () => void; onSave: (input: ProjectPatchV1) => Promise<void> }) {
  const [name, setName] = useState(project?.name ?? "New research project");
  const [title, setTitle] = useState(project?.task.title ?? "Research task");
  const [objective, setObjective] = useState(project?.task.objective ?? "");
  const [sourceName, setSourceName] = useState(project?.source.display_name ?? "New workspace");
  const [mode, setMode] = useState(project?.execution.mode ?? "self-deployed");
  const [hfModel, setHfModel] = useState(project?.execution.hf_model ?? "Qwen/Qwen3-8B");
  const [codexModel, setCodexModel] = useState(project?.execution.codex_model ?? "Codex");
  const [evolution, setEvolution] = useState<ProductEvolutionTargets>(project?.evolution.targets ?? defaultEvolution(capabilities));
  const [dirty, setDirty] = useState(false);
  const activeModel = mode === "self-deployed" ? hfModel : codexModel;
  const valid = name.trim().length > 0 && title.trim().length > 0 && objective.trim().length > 0 && activeModel.trim().length > 0 && profileId !== null;
  const change = (setter: (value: string) => void) => (event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => { setter(event.target.value); setDirty(true); };
  const reset = () => {
    setName(project?.name ?? "New research project");
    setTitle(project?.task.title ?? "Research task");
    setObjective(project?.task.objective ?? "");
    setSourceName(project?.source.display_name ?? "New workspace");
    setMode(project?.execution.mode ?? "self-deployed");
    setHfModel(project?.execution.hf_model ?? "Qwen/Qwen3-8B");
    setCodexModel(project?.execution.codex_model ?? "Codex");
    setEvolution(project?.evolution.targets ?? defaultEvolution(capabilities));
    setDirty(false);
  };
  return (
    <div className="drawer-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <aside className="settings-drawer" role="dialog" aria-modal="true" aria-labelledby="settings-title">
        <div className="drawer-head"><div><span className="panel-kicker">{project ? "Project settings" : "New project"}</span><h2 id="settings-title">Research configuration</h2></div><IconButton label="Close settings" onClick={onClose}><X size={18} /></IconButton></div>
        <div className="drawer-content">
          <section className="form-section"><h3>Project</h3><label>Project name<input value={name} onChange={change(setName)} /></label><label>Task title<input value={title} onChange={change(setTitle)} /></label><label>Objective<textarea rows={5} value={objective} onChange={change(setObjective)} /></label><label>Source name<input value={sourceName} onChange={change(setSourceName)} /></label></section>
          <section className="form-section"><h3>Model mode</h3><div className="segmented-control wide" aria-label="Model mode"><button type="button" className={mode === "self-deployed" ? "active" : ""} onClick={() => { setMode("self-deployed"); setDirty(true); }}>Managed model</button><button type="button" className={mode === "codex_subscription_transcript" ? "active" : ""} onClick={() => { setMode("codex_subscription_transcript"); setDirty(true); }}>Subscription</button></div>{mode === "self-deployed" ? <label>Hugging Face model<input value={hfModel} onChange={change(setHfModel)} placeholder="organization/model" /></label> : <label>Codex model<input value={codexModel} onChange={change(setCodexModel)} placeholder="Model name" /></label>}<p className="form-help">Sessions use transcript capture. Token-level metrics are unavailable in this mode.</p></section>
          <section className="form-section"><h3>Evolution targets</h3><div className="target-list">{Object.entries(evolution).map(([targetId, target]) => {
            const capability = capabilities?.targets.find((item) => item.target_id === targetId);
            const canEnable = target.method !== null || capability?.effective_default_method_id != null;
            return <label className="target-toggle" key={targetId}><input type="checkbox" role="switch" checked={target.enabled} disabled={!canEnable} onChange={(event) => { const enabled = event.target.checked; setEvolution((current) => ({ ...current, [targetId]: { ...target, enabled, method: target.method ?? capability?.effective_default_method_id ?? null, config: target.method ? target.config : capability?.methods.find((method) => method.method_id === capability.effective_default_method_id)?.default_config ?? target.config } })); setDirty(true); }} /><span className="switch-track"><span /></span><span><strong>{capability?.display_name ?? artifactTypeLabel(targetId)}</strong><small>{capability?.description ?? (canEnable ? "Available for future sessions." : "Not available for this model mode.")}</small></span></label>;
          })}</div></section>
        </div>
        <div className="drawer-footer"><button className="secondary-button" type="button" onClick={reset} disabled={!dirty || busy} title={!dirty ? "No unsaved changes" : "Undo changes"}><RotateCcw size={15} /> Undo</button><button className="primary-button" type="button" disabled={!valid || busy || (project !== null && !dirty)} title={!profileId ? "Add a remote workspace first" : !valid ? "Complete all required fields" : project && !dirty ? "No unsaved changes" : "Save project settings"} onClick={() => void onSave({
          name: name.trim(),
          task: { title: title.trim(), objective: objective.trim(), task_ref: project?.task.task_ref ?? null },
          source: { kind: project?.source.kind ?? "scratch", display_name: sourceName.trim() || "New workspace", source_ref: project?.source.source_ref ?? null },
          execution: mode === "self-deployed" ? selfDeployedExecution(activeModel.trim()) : subscriptionExecution(activeModel.trim()),
          evolution: { targets: evolution },
        })}><Save size={15} /> {busy ? "Saving..." : "Save"}</button></div>
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

function InlineNotice({ tone, title, detail, onDismiss }: { tone: "warning" | "error"; title: string; detail: string; onDismiss?: () => void }) {
  return <div className={`inline-notice ${tone}`} role={tone === "error" ? "alert" : "status"}>{tone === "error" ? <XCircle size={18} /> : <AlertCircle size={18} />}<div><strong>{title}</strong><span>{detail}</span></div>{onDismiss ? <IconButton label="Dismiss" onClick={onDismiss}><X size={15} /></IconButton> : null}</div>;
}

function BlockingState({ title, detail, actionLabel, onAction }: { title: string; detail: string; actionLabel: string; onAction: () => void }) {
  return <div className="blocking-state" role="alert"><span className="product-mark large"><AlertCircle size={22} /></span><h1>{title}</h1><p>{detail}</p><button type="button" className="primary-button" onClick={onAction}><RefreshCw size={16} /> {actionLabel}</button></div>;
}

function EmptyState({ icon: Icon, title, detail, action, onAction }: { icon: LucideIcon; title: string; detail: string; action?: string; onAction?: () => void }) {
  return <div className="product-empty"><Icon size={28} /><h2>{title}</h2><p>{detail}</p>{action && onAction ? <button className="primary-button" type="button" onClick={onAction}><Plus size={16} /> {action}</button> : null}</div>;
}

function artifactIcon(type: string) {
  if (type === "text_memory") return <MemoryStick size={17} />;
  if (type === "skill_bundle") return <Sparkles size={17} />;
  return <TerminalSquare size={17} />;
}

function defaultEvolution(capabilities: DesktopProductSnapshot["capabilities"]): ProductEvolutionTargets {
  return Object.fromEntries((capabilities?.targets ?? []).filter((target) => target.release_enabled).map((target) => [target.target_id, {
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

function latestArtifactsByTarget(artifacts: readonly ArtifactV1[]): ArtifactV1[] {
  const seen = new Set<string>();
  const latest = artifacts.filter((artifact) => {
    if (seen.has(artifact.target_id) || artifact.artifact_type === "parametric_memory") return false;
    seen.add(artifact.target_id);
    return true;
  });
  const order = new Map([["text_memory", 0], ["skill_bundle", 1], ["agent_system", 2]]);
  return latest.sort((left, right) => (order.get(left.artifact_type) ?? 99) - (order.get(right.artifact_type) ?? 99));
}

function currentGeneration(project: ProjectV1, runs: readonly RunV1[]): number {
  const revision = runs.flatMap((run) => [run.pinned_revision, ...(run.successor_revision ? [run.successor_revision] : [])]).find((item) => item.revision_id === project.current_revision_id);
  return revision?.generation ?? 1;
}

function revisionGeneration(artifact: ArtifactV1, runs: readonly RunV1[]): number {
  const revisionId = artifact.revision_ids[0];
  return runs.flatMap((run) => [run.pinned_revision, ...(run.successor_revision ? [run.successor_revision] : [])]).find((item) => item.revision_id === revisionId)?.generation ?? 1;
}

function revisionLabel(project: ProjectV1 | null, runs: readonly RunV1[]): string {
  if (!project) return "Not available";
  return `Revision ${currentGeneration(project, runs)}`;
}

function getStartReason(connection: DesktopProductSnapshot["state"]["core"]["state"], project: ProjectV1 | null, activeRun: RunV1 | null, actionState: AsyncState): string | null {
  if (!project) return "Create or select a project first.";
  if (connection !== "online") return "Connect the remote workspace before starting a session.";
  if (project.state !== "active") return "Activate this project before starting a session.";
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
  return `Session ${Math.max(1, runs.length - runs.indexOf(run))}`;
}

function formatTime(timestamp: string): string {
  return new Intl.DateTimeFormat("en", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "UTC" }).format(new Date(timestamp));
}

function isTerminal(state: RunV1["state"]): boolean {
  return state === "succeeded" || state === "failed" || state === "cancelled";
}

function userMessage(error: unknown): string {
  if (error instanceof DesktopProductProviderUnavailableError) return "The local Desktop service is not ready. Restart OpenEvo Desktop and try again.";
  if (error instanceof Error && error.message) return error.message;
  return "The request could not be completed.";
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
