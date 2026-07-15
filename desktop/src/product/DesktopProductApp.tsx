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
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { DesktopApiError } from "../api/v1/client";
import type { OpenEvoJsonObject } from "../api/evolutionConfigSchema";
import type {
  ApiErrorV1,
  ArtifactContentV1,
  ArtifactDiffV1,
  ArtifactV1,
  ExecutionModeCapabilitiesV1,
  ExecutionModeCapabilityV1,
  LogEntryV1,
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
  type ProductArtifactCollectionState,
  type ProductResourceMutationIntent,
  type ProductRefreshResult,
  unavailableDesktopProductProvider,
} from "./provider";
import { MethodConfigEditor, methodConfigErrors } from "./MethodConfigEditor";
import {
  sameSessionOutputIdentity,
  sessionOutputIdentity,
  type SessionOutputIdentity,
} from "./sessionOutputIdentity";

type ProductEvolutionTargets = ProjectV1["evolution"]["targets"];
type EvolutionCapabilitiesV1 = ProjectCapabilitiesV1["capabilities"];
type RevisionRefV1 = NonNullable<NonNullable<ProjectV1["remote"]>["active_revision"]>;

const DEFAULT_CODEX_MODEL = "gpt-5.5";
const DEFAULT_HF_MODEL = "Qwen/Qwen3-8B";
const ACTIVE_RUN_REFRESH_INTERVAL_MS = 1_000;
const REQUIRED_EVOLUTION_TARGETS = ["text_memory", "skill_bundle", "agent_system"] as const;

type Workspace = "research" | "evolution" | "system";
type AsyncState = "idle" | "working";
type ActionRecovery = { readonly kind: "readmit_run"; readonly projectId: string } | null;
type ActionAttemptResult = {
  readonly saved: boolean;
  readonly error: unknown | null;
  readonly refreshedSnapshot: DesktopProductSnapshot | null;
};
type PendingProjectActivation = {
  readonly projectId: string;
  readonly activationActionId: string;
};
type PendingRunRetry = {
  readonly runId: string;
  readonly runEtag: string;
  readonly actionId: string;
};
type SaveAttemptResult = {
  readonly saved: boolean;
  readonly replaceActionId: boolean;
  readonly pendingSourceOutcome: "adopted" | "discarded" | null;
};
type ProfileSaveIntent = {
  readonly canonicalPayload: string;
  readonly input: ProfileCreateV1;
  readonly route:
    | { readonly kind: "create"; readonly intent: ProductMutationIntent }
    | { readonly kind: "update"; readonly profileId: string; readonly intent: ProductResourceMutationIntent };
};
type ProfileSaveAttemptResult = { readonly saved: boolean; readonly pendingIntent: ProfileSaveIntent | null };
type SnapshotRefreshGuard = () => boolean;
type SnapshotRefreshSource = "mutation" | "reconcile" | "manual" | "lifecycle" | "sse" | "poll";
type RunPollingIdentity = {
  readonly provider: DesktopProductProvider;
  readonly desktopProjectId: string;
  readonly projectEtag: string;
  readonly profileId: string;
  readonly coreProjectId: string;
  readonly runId: string;
  readonly activeProjectId: string;
  readonly activeProjectEtag: string;
  readonly activeProfileId: string;
};
type SnapshotRefreshWaiter = {
  readonly source: SnapshotRefreshSource;
  readonly watermark: number;
  readonly isRelevant: SnapshotRefreshGuard;
  readonly resolve: (snapshot: DesktopProductSnapshot | null) => void;
};
type SnapshotRefreshBatch = {
  readonly provider: DesktopProductProvider;
  readonly throughWatermark: number;
  readonly waiters: SnapshotRefreshWaiter[];
};
type SnapshotRefreshPublication =
  | { readonly kind: "result"; readonly result: ProductRefreshResult }
  | { readonly kind: "rejected"; readonly error: unknown }
  | { readonly kind: "pending"; readonly epoch: number | null };

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
  const pendingProjectActivation = useRef<PendingProjectActivation | null>(null);
  const pendingRunRetry = useRef<PendingRunRetry | null>(null);
  const recoveredProjectSetup = useRef<string | null>(null);
  const [cancellingOperation, setCancellingOperation] = useState(false);
  const refreshCoordinator = useRef<SnapshotRefreshCoordinator | null>(null);
  if (refreshCoordinator.current === null) refreshCoordinator.current = new SnapshotRefreshCoordinator();

  const publishRefresh = useCallback((publication: SnapshotRefreshPublication): void => {
    if (publication.kind === "pending") {
      setSnapshot((current) => current ? {
        ...current,
        stream: { status: "stale", epoch: publication.epoch ?? current.stream.epoch, reason: "refresh_pending" },
      } : current);
      return;
    }
    if (publication.kind === "rejected") {
      setSnapshot((current) => current ? {
        ...current,
        stream: {
          status: "error",
          epoch: current.stream.epoch,
          error: publication.error instanceof DesktopApiError ? publication.error.apiError : null,
        },
      } : current);
      setLoadError(userMessage(publication.error));
      return;
    }
    const result = publication.result;
    if (result.status !== "fresh") {
      setSnapshot((current) => current ? { ...current, stream: result.stream } : current);
      if (result.status === "error") setLoadError(userMessage(result.stream.error));
      return;
    }
    const next = result.snapshot;
    setSnapshot(next);
    setLoadError(null);
    setSelectedProjectId((current) => {
      if (current && next.projects.some((project) => project.project_id === current)) return current;
      return next.state.active_project?.project_id ?? next.projects[0]?.project_id ?? null;
    });
  }, []);

  useLayoutEffect(() => {
    const coordinator = refreshCoordinator.current!;
    coordinator.mount(provider, publishRefresh);
    return () => coordinator.unmount(provider);
  }, [provider, publishRefresh]);

  const refresh = useCallback(
    (source: SnapshotRefreshSource, isRelevant: SnapshotRefreshGuard = () => true) =>
      refreshCoordinator.current!.request(provider, source, isRelevant),
    [provider],
  );

  useEffect(() => {
    let active = true;
    const load = async (source: "lifecycle" | "sse") => {
      if (!active) return;
      await refresh(source, () => active);
    };
    void load("lifecycle");
    const unsubscribe = provider.subscribe((signal) => {
      if (signal.kind === "snapshot_changed") {
        setSnapshot((current) => current ? { ...current, stream: { status: "stale", epoch: current.stream.epoch, reason: "refresh_pending" } } : current);
        void load("sse");
      } else if (signal.kind === "stream_stale") {
        setSnapshot((current) => current ? { ...current, stream: { status: "stale", epoch: current.stream.epoch, reason: signal.reason } } : current);
      } else if (signal.kind === "stream_error") {
        setSnapshot((current) => current ? { ...current, stream: { status: "error", epoch: current.stream.epoch, error: signal.error } } : current);
      } else {
        setSnapshot((current) => current ? { ...current, stream: { status: "cursor_reset", epoch: current.stream.epoch, resumeFromEventId: null } } : current);
        void load("sse");
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
  const coreProjectId = project?.remote?.core_project_id ?? null;
  const projectRuns = stableRunOrder(snapshot?.runs.filter((run) => run.project_id === coreProjectId) ?? []);
  const activeRun = projectRuns.find((run) => !isTerminal(run.status)) ?? null;
  const projectSessionReady = snapshot ? hasReadySelectedProjectSession(snapshot, project) : false;
  const runPollingIdentity = useMemo(
    () => runPollingIdentityFor(provider, snapshot, project, activeRun, projectSessionReady),
    [
      activeRun?.id,
      coreProjectId,
      project?.etag,
      project?.profile_id,
      project?.project_id,
      projectSessionReady,
      provider,
      snapshot?.state.active_project?.profile_id,
      snapshot?.state.active_project?.project_etag,
      snapshot?.state.active_project?.project_id,
    ],
  );

  const act = useCallback(async (action: () => Promise<unknown>, conflictRecovery: ActionRecovery = null, refreshOnUnknown = false): Promise<ActionAttemptResult> => {
    setActionState("working");
    setActionError(null);
    setActionRecovery(null);
    try {
      await action();
      const refreshedSnapshot = await refresh("mutation");
      return { saved: true, error: null, refreshedSnapshot };
    } catch (error) {
      let refreshedSnapshot: DesktopProductSnapshot | null = null;
      if (error instanceof DesktopApiError && [409, 410, 412].includes(error.apiError.http_status)) {
        if (error.apiError.http_status === 410) {
          setSnapshot((current) => current ? { ...current, stream: { status: "cursor_reset", epoch: current.stream.epoch, resumeFromEventId: null } } : current);
        }
        refreshedSnapshot = await refresh("mutation");
        if (conflictRecovery && canReadmitRun(error.apiError, refreshedSnapshot, conflictRecovery.projectId)) {
          setActionRecovery(conflictRecovery);
        }
      } else if (refreshOnUnknown) {
        refreshedSnapshot = await refresh("mutation");
      }
      setActionError(userMessage(error));
      return { saved: false, error, refreshedSnapshot };
    } finally {
      setActionState("idle");
    }
  }, [refresh]);

  useActiveRunPolling(runPollingIdentity, refresh);

  useEffect(() => {
    if (!snapshot) return;
    const setupProjectId = snapshot.activeOperation?.operation_kind === "project_activate"
      ? snapshot.activeOperation.resource.resource_id
      : snapshot.state.active_project?.project_id ?? null;
    const setupProject = snapshot.projects.find((item) => item.project_id === setupProjectId);
    if (!setupProject || setupProject.evolution_configuration_state !== "pending") return;
    if (recoveredProjectSetup.current === setupProject.project_id) return;
    recoveredProjectSetup.current = setupProject.project_id;
    setSelectedProjectId(setupProject.project_id);
    setCreatingProject(false);
    setSettingsOpen(true);
  }, [snapshot]);

  if (!snapshot) {
    return (
      <div className="product-boot" data-testid="product-loading">
        {loadError ? (
          <BlockingState
            title="OpenEvo Desktop is unavailable"
            detail={loadError}
            actionLabel="Try again"
            onAction={() => void refresh("manual")}
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
  const settingsProject = creatingProject ? null : project;
  const settingsFormIdentity = settingsProject ? `project:${settingsProject.project_id}` : "create";
  const settingsCapability = projectCapability(snapshot, settingsProject);
  const projectServices = projectSessionReady ? snapshot.services : [];
  const canCreateProject = profile?.connection_state === "connected";
  const activationReason = getProjectActivationReason(snapshot, project, profile, actionState);
  const startReason = getStartReason(snapshot, project, profile, activeRun, actionState);
  const canStart = startReason === null;

  const cancelActiveOperation = async () => {
    const operation = snapshot.activeOperation;
    if (!operation || cancellingOperation) return;
    setCancellingOperation(true);
    setActionError(null);
    try {
      await provider.cancelOperation(
        operation.operation_id,
        resourceIntent(snapshot, operation.etag),
      );
      await refresh("mutation");
    } catch (error) {
      setActionError(userMessage(error));
      await refresh("mutation");
    } finally {
      setCancellingOperation(false);
    }
  };

  const retryFailedRun = (run: RunV1): void => {
    if (actionState === "working") return;
    const retryRun = provider.retryRun;
    if (!retryRun) {
      void act(() => Promise.reject(new DesktopProductProviderUnavailableError()));
      return;
    }
    const existing = pendingRunRetry.current;
    const pending = existing?.runId === run.id && existing.runEtag === run.etag
      ? existing
      : { runId: run.id, runEtag: run.etag, actionId: newActionId() };
    pendingRunRetry.current = pending;
    void act(
      () => retryRun.call(provider, run.id, resourceIntent(snapshot, run.etag, pending.actionId)),
      null,
      true,
    ).then((result) => {
      if (pendingRunRetry.current !== pending) return;
      const refreshedRun = result.refreshedSnapshot?.runs.find((item) => item.id === run.id);
      if (result.saved
        || (result.refreshedSnapshot?.stream.status === "fresh" && !refreshedRun)
        || (refreshedRun && (refreshedRun.etag !== run.etag || refreshedRun.status !== "failed"))) {
        pendingRunRetry.current = null;
        setActionError(null);
      }
    });
  };

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
            <IconButton label="Create project" onClick={() => { setCreatingProject(true); setSettingsOpen(true); }} disabled={!canCreateProject}><Plus size={17} /></IconButton>
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
            actionLabel={actionRecovery?.kind === "readmit_run" ? "Re-admit session" : undefined}
            onAction={actionRecovery?.kind === "readmit_run" && project && actionRecovery.projectId === project.project_id
              ? () => void act(() => provider.startRun({ ...resourceIntent(snapshot, project.etag), projectId: project.project_id }), actionRecovery)
              : undefined}
          /> : null}
          {snapshot.activeOperation && !["succeeded", "failed", "cancelled"].includes(snapshot.activeOperation.state) ? (
            <section className="operation-cancel-bar" aria-live="polite">
              <div><LoaderCircle className="spin" size={17} /><span>{snapshot.activeOperation.progress?.label ?? "Local operation in progress"}</span></div>
              <button className="secondary-button" type="button" onClick={() => void cancelActiveOperation()} disabled={cancellingOperation}><Square size={14} /> {cancellingOperation ? "Cancelling..." : "Cancel operation"}</button>
            </section>
          ) : null}
          <ConnectionGate
            snapshot={snapshot}
            profile={profile}
            hasProject={project !== null}
            busy={actionState === "working"}
            onConnect={(selectedProfile) => void act(() => provider.connectProfile(selectedProfile.profile_id, resourceIntent(snapshot, selectedProfile.etag)))}
            onAccept={(profileId) => {
              const review = snapshot.state.core.host_key_review;
              if (review && profile) void act(() => provider.acceptHostKey(profileId, review, resourceIntent(snapshot, profile.etag)));
            }}
            onSetup={() => setConnectionSettingsOpen(true)}
          />
          {project && !projectSessionReady ? (
            <ProjectActivationGate
              project={project}
              busy={actionState === "working"}
              disabledReason={activationReason}
              onActivate={() => void act(() => provider.activateProject(project.project_id, resourceIntent(snapshot, project.etag)))}
            />
          ) : null}

          {workspace === "research" ? (
            <ResearchWorkspace
              project={project}
              hasProfile={profile !== null}
              canCreateProject={canCreateProject}
              executionModeLabel={project ? executionModeCapability(snapshot.executionModeCapabilities, project.execution.mode).display_name : null}
              runs={projectRuns}
              activeRun={activeRun}
              timelines={snapshot.timelines}
              provider={provider}
              streamEpoch={snapshot.stream.epoch}
              modelService={projectServices.find((service) => service.kind === "inference") ?? null}
              canStart={canStart}
              startReason={startReason}
              busy={actionState === "working"}
              onStart={() => project && void act(() => provider.startRun({ ...resourceIntent(snapshot, project.etag), projectId: project.project_id }), { kind: "readmit_run", projectId: project.project_id })}
              onRetry={retryFailedRun}
              onCancel={() => activeRun && void act(() => provider.cancelRun(activeRun.id, resourceIntent(snapshot, activeRun.etag)))}
              onOpenSettings={() => { setCreatingProject(false); setSettingsOpen(true); }}
              onOpenConnection={() => setConnectionSettingsOpen(true)}
              onOpenEvolution={() => setWorkspace("evolution")}
              onOpenSystem={() => setWorkspace("system")}
              onRefresh={() => void refresh("manual")}
            />
          ) : null}
          {workspace === "evolution" ? (
            <EvolutionWorkspace
              project={project}
              runs={projectRuns}
              artifacts={snapshot.artifacts.filter((artifact) => artifact.project_id === coreProjectId)}
              artifactCollection={snapshot.artifactCollection}
              provider={provider}
              onRefresh={() => void refresh("manual")}
              onOpenSettings={() => { setCreatingProject(false); setSettingsOpen(true); }}
            />
          ) : null}
          {workspace === "system" ? (
            <SystemWorkspace
              snapshot={snapshot}
              profile={profile}
              services={projectServices}
              projectSessionReady={projectSessionReady}
              busy={actionState === "working"}
              onConnect={() => profile && void act(() => provider.connectProfile(profile.profile_id, resourceIntent(snapshot, profile.etag)))}
              onConfigure={() => setConnectionSettingsOpen(true)}
            />
          ) : null}
        </main>
      </div>

      {settingsOpen ? (
        <SettingsDrawer
          key={settingsFormIdentity}
          project={settingsProject}
          profileId={profile?.profile_id ?? null}
          executionModeCapabilities={snapshot.executionModeCapabilities}
          capability={settingsCapability}
          capabilities={readyCapabilities(settingsCapability, settingsProject)}
          busy={actionState === "working"}
          onClose={() => {
            const pending = pendingProjectActivation.current;
            if (pending) {
              setSelectedProjectId(pending.projectId);
              setCreatingProject(false);
              pendingProjectActivation.current = null;
            }
            setSettingsOpen(false);
          }}
          onRetryCapabilities={() => refresh("manual")}
          onSave={async (input, actionId, pendingSourceActionId) => {
            const requestEpoch = snapshot.stream.epoch;
            const requestProject = settingsProject;
            let pendingSourceOutcome: SaveAttemptResult["pendingSourceOutcome"] = null;
            const result = await act(async () => {
              if (requestProject) {
                await provider.updateProject(
                  requestProject.project_id,
                  requestProject.evolution_configuration_state === "pending"
                    ? { ...input, evolution_configuration_state: "configured" }
                    : input,
                  resourceIntent(snapshot, requestProject.etag, actionId),
                );
                if (pendingSourceActionId) {
                  await provider.settleProjectSource(pendingSourceActionId, "adopt");
                  pendingSourceOutcome = "adopted";
                }
                if (requestProject.evolution_configuration_state === "pending") {
                  const afterUpdate = await refresh("mutation");
                  const updated = afterUpdate?.projects.find((item) => item.project_id === requestProject.project_id);
                  if (!afterUpdate || !updated) {
                    throw new DesktopProductUserError("The prepared project is not available for activation. Refresh and try again.");
                  }
                  await provider.activateProject(
                    updated.project_id,
                    resourceIntent(afterUpdate, updated.etag, `${actionId}-activate`),
                  );
                }
              } else {
                if (!profile) throw new DesktopProductUserError("Add a remote workspace before creating a project.");
                let pending = pendingProjectActivation.current;
                if (!pending) {
                  const created = await provider.createProject({
                    name: input.name ?? "Untitled research",
                    profile_id: profile.profile_id,
                    task: input.task ?? { title: "Research task", objective: "Describe the research objective." },
                    source: input.source ?? { kind: "scratch", display_name: "New workspace" },
                    execution: input.execution ?? subscriptionExecution(DEFAULT_CODEX_MODEL),
                    evolution: input.evolution ?? { targets: {} },
                    evolution_configuration_state: "pending",
                  }, mutationIntent(snapshot, actionId));
                  pending = { projectId: created.project_id, activationActionId: `${actionId}-activate` };
                  pendingProjectActivation.current = pending;
                  if (pendingSourceActionId) {
                    await provider.settleProjectSource(pendingSourceActionId, "adopt");
                    pendingSourceOutcome = "adopted";
                  }
                }
                const afterCreate = await refresh("mutation");
                const current = afterCreate?.projects.find((item) => item.project_id === pending.projectId);
                if (!afterCreate || !current) {
                  throw new DesktopProductUserError("The new project is not available for activation. Refresh and try again.");
                }
                await provider.activateProject(
                  current.project_id,
                  resourceIntent(afterCreate, current.etag, pending.activationActionId),
                );
                pendingProjectActivation.current = null;
                setSelectedProjectId(current.project_id);
                setCreatingProject(false);
              }
            });
            if (!result.saved
              && pendingProjectActivation.current
              && result.error instanceof DesktopApiError
              && [409, 412].includes(result.error.apiError.http_status)) {
              pendingProjectActivation.current = {
                ...pendingProjectActivation.current,
                activationActionId: `${newActionId()}-activate`,
              };
            }
            if (pendingSourceActionId && pendingSourceOutcome === null) {
              try {
                await provider.settleProjectSource(pendingSourceActionId, "discard");
                pendingSourceOutcome = "discarded";
              } catch {
                // The native host retains failed settles for retry or startup recovery.
              }
            }
            if (result.saved && requestProject !== null) setSettingsOpen(false);
            return {
              saved: result.saved,
              replaceActionId: requestPreconditionChanged(result, requestEpoch, requestProject ? { kind: "project", id: requestProject.project_id, etag: requestProject.etag } : null),
              pendingSourceOutcome,
            };
          }}
          onSelectSource={(actionId) => provider.selectProjectSource({
            ...mutationIntent(snapshot, actionId),
            kind: "native_folder_snapshot",
            ...(settingsProject ? { projectId: settingsProject.project_id } : {}),
          })}
          onCancelSource={(actionId) => provider.cancelProjectSource(actionId)}
          onSettleSource={(actionId, outcome) => provider.settleProjectSource(actionId, outcome)}
        />
      ) : null}
      {connectionSettingsOpen ? (
        <RemoteWorkspaceDrawer
          profile={profile}
          observedProfiles={snapshot.profiles}
          streamEpoch={snapshot.stream.status === "fresh" ? snapshot.stream.epoch : null}
          busy={actionState === "working"}
          onClose={() => setConnectionSettingsOpen(false)}
          createSaveIntent={(input) => profileSaveIntent(snapshot, profile, input)}
          onSave={async (intent) => {
            const route = intent.route;
            const result = await act(() => route.kind === "create"
              ? provider.createProfile(intent.input, route.intent)
              : provider.updateProfile(route.profileId, intent.input, route.intent), null, true);
            const createdProfile = route.kind === "create"
              ? matchingProfile(result.refreshedSnapshot, intent.canonicalPayload)
              : null;
            if (result.saved || createdProfile) {
              setActionError(null);
              setConnectionSettingsOpen(false);
              return { saved: true, pendingIntent: null };
            }
            const requestEpoch = route.intent.streamEpoch;
            const resource = route.kind === "update"
              ? { kind: "profile" as const, id: route.profileId, etag: route.intent.etag }
              : null;
            return {
              saved: false,
              pendingIntent: requestPreconditionChanged(result, requestEpoch, resource) ? null : intent,
            };
          }}
          onCreateObserved={(observedProfile) => {
            setActionError(null);
            setConnectionSettingsOpen(false);
            setSelectedProjectId((current) => current ?? snapshot.projects.find((item) => item.profile_id === observedProfile.profile_id)?.project_id ?? null);
          }}
        />
      ) : null}
    </div>
  );
}

class SnapshotRefreshCoordinator {
  private provider: DesktopProductProvider | null = null;
  private publish: ((publication: SnapshotRefreshPublication) => void) | null = null;
  private mounted = false;
  private running = false;
  private nextWatermark = 0;
  private trailing: SnapshotRefreshBatch | null = null;

  mount(
    provider: DesktopProductProvider,
    publish: (publication: SnapshotRefreshPublication) => void,
  ): void {
    this.provider = provider;
    this.publish = publish;
    this.mounted = true;
    this.pump();
  }

  unmount(provider: DesktopProductProvider): void {
    if (this.provider !== provider) return;
    this.mounted = false;
    this.provider = null;
    this.publish = null;
    this.clearTrailing();
  }

  request(
    provider: DesktopProductProvider,
    source: SnapshotRefreshSource,
    isRelevant: SnapshotRefreshGuard,
  ): Promise<DesktopProductSnapshot | null> {
    if (!this.mounted || this.provider !== provider) return Promise.resolve(null);
    this.nextWatermark += 1;
    const watermark = this.nextWatermark;
    return new Promise((resolve) => {
      this.queuedBatch(provider, watermark).waiters.push({ source, watermark, isRelevant, resolve });
      this.pump();
    });
  }

  private queuedBatch(provider: DesktopProductProvider, watermark: number): SnapshotRefreshBatch {
    if (this.trailing?.provider === provider) {
      this.trailing = { ...this.trailing, throughWatermark: watermark };
      return this.trailing;
    }
    const batch: SnapshotRefreshBatch = { provider, throughWatermark: watermark, waiters: [] };
    this.trailing = batch;
    return batch;
  }

  private pump(): void {
    if (this.running) return;
    if (!this.mounted) {
      this.clearTrailing();
      return;
    }
    const batch = this.trailing;
    this.trailing = null;
    if (!batch) return;
    // Detaching the batch freezes its dispatch watermark; later requests can only enter the tail.
    if (batch.provider !== this.provider) {
      this.resolveBatch(batch, null);
      this.pump();
      return;
    }
    this.running = true;
    void this.execute(batch).finally(() => {
      this.running = false;
      this.pump();
    });
  }

  private async execute(batch: SnapshotRefreshBatch): Promise<void> {
    let publication: SnapshotRefreshPublication;
    let freshSnapshot: DesktopProductSnapshot | null = null;
    try {
      const result = await batch.provider.refresh();
      publication = { kind: "result", result };
      if (result.status === "fresh") freshSnapshot = result.snapshot;
    } catch (error) {
      publication = { kind: "rejected", error };
    }

    if (!this.mounted || this.provider !== batch.provider || this.publish === null) {
      this.resolveBatch(batch, null);
      return;
    }

    const relevance = batch.waiters.map((waiter) => safeRefreshGuard(waiter.isRelevant));
    if (!relevance.some(Boolean)) {
      this.resolveBatch(batch, null);
      this.publish({ kind: "pending", epoch: refreshPublicationEpoch(publication) });
      this.ensureReconciliation(batch.provider);
      return;
    }

    this.publish(publication);
    // The dispatched batch owns these waiters even when a higher-watermark tail already exists.
    for (const waiter of orderedRefreshWaiters(batch.waiters)) {
      const index = batch.waiters.indexOf(waiter);
      waiter.resolve(relevance[index] ? freshSnapshot : null);
    }
    if (this.hasTrailingAfter(batch)) {
      this.publish({ kind: "pending", epoch: refreshPublicationEpoch(publication) });
    }
  }

  private hasTrailingAfter(batch: SnapshotRefreshBatch): boolean {
    return this.trailing?.provider === batch.provider
      && this.trailing.throughWatermark > batch.throughWatermark;
  }

  private ensureReconciliation(provider: DesktopProductProvider): void {
    if (this.trailing?.provider === provider) return;
    void this.request(
      provider,
      "reconcile",
      () => this.mounted && this.provider === provider,
    );
  }

  private resolveBatch(batch: SnapshotRefreshBatch, snapshot: DesktopProductSnapshot | null): void {
    for (const waiter of batch.waiters) waiter.resolve(snapshot);
  }

  private clearTrailing(): void {
    if (!this.trailing) return;
    this.resolveBatch(this.trailing, null);
    this.trailing = null;
  }
}

const SNAPSHOT_REFRESH_SOURCE_PRIORITY: Readonly<Record<SnapshotRefreshSource, number>> = {
  mutation: 6,
  reconcile: 5,
  manual: 4,
  lifecycle: 3,
  sse: 2,
  poll: 1,
};

function orderedRefreshWaiters(waiters: readonly SnapshotRefreshWaiter[]): SnapshotRefreshWaiter[] {
  return [...waiters].sort((left, right) =>
    SNAPSHOT_REFRESH_SOURCE_PRIORITY[right.source] - SNAPSHOT_REFRESH_SOURCE_PRIORITY[left.source]
      || left.watermark - right.watermark,
  );
}

function refreshPublicationEpoch(publication: SnapshotRefreshPublication): number | null {
  if (publication.kind !== "result") return null;
  return publication.result.status === "fresh"
    ? publication.result.snapshot.stream.epoch
    : publication.result.stream.epoch;
}

function safeRefreshGuard(isRelevant: SnapshotRefreshGuard): boolean {
  try {
    return isRelevant();
  } catch {
    return false;
  }
}

function useActiveRunPolling(
  identity: RunPollingIdentity | null,
  refresh: (source: SnapshotRefreshSource, isRelevant?: SnapshotRefreshGuard) => Promise<DesktopProductSnapshot | null>,
): void {
  const currentIdentity = useRef(identity);

  useLayoutEffect(() => {
    currentIdentity.current = identity;
  }, [identity]);

  useEffect(() => {
    if (!identity) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const isCurrent = () => !cancelled && sameRunPollingIdentity(currentIdentity.current, identity);
    const schedule = () => {
      if (!isCurrent()) return;
      timer = setTimeout(() => {
        timer = null;
        void poll();
      }, ACTIVE_RUN_REFRESH_INTERVAL_MS);
    };
    const poll = async () => {
      if (!isCurrent()) return;
      const next = await refresh("poll", isCurrent);
      if (!isCurrent()) return;
      if (next && !snapshotHasRunPollingIdentity(next, identity)) return;
      schedule();
    };

    schedule();
    return () => {
      cancelled = true;
      if (timer !== null) clearTimeout(timer);
    };
  }, [identity, refresh]);
}

function runPollingIdentityFor(
  provider: DesktopProductProvider,
  snapshot: DesktopProductSnapshot | null,
  project: ProjectV1 | null,
  run: RunV1 | null,
  projectSessionReady: boolean,
): RunPollingIdentity | null {
  const activeProject = snapshot?.state.active_project;
  const coreProjectId = project?.remote?.core_project_id;
  if (!snapshot || !project || !run || !projectSessionReady || !activeProject || !coreProjectId) return null;
  return {
    provider,
    desktopProjectId: project.project_id,
    projectEtag: project.etag,
    profileId: project.profile_id,
    coreProjectId,
    runId: run.id,
    activeProjectId: activeProject.project_id,
    activeProjectEtag: activeProject.project_etag,
    activeProfileId: activeProject.profile_id,
  };
}

function sameRunPollingIdentity(
  current: RunPollingIdentity | null,
  expected: RunPollingIdentity,
): boolean {
  return current !== null
    && current.provider === expected.provider
    && current.desktopProjectId === expected.desktopProjectId
    && current.projectEtag === expected.projectEtag
    && current.profileId === expected.profileId
    && current.coreProjectId === expected.coreProjectId
    && current.runId === expected.runId
    && current.activeProjectId === expected.activeProjectId
    && current.activeProjectEtag === expected.activeProjectEtag
    && current.activeProfileId === expected.activeProfileId;
}

function snapshotHasRunPollingIdentity(
  snapshot: DesktopProductSnapshot,
  expected: RunPollingIdentity,
): boolean {
  const project = snapshot.projects.find((item) => item.project_id === expected.desktopProjectId) ?? null;
  const activeProject = snapshot.state.active_project;
  if (!project
    || project.etag !== expected.projectEtag
    || project.profile_id !== expected.profileId
    || project.remote?.core_project_id !== expected.coreProjectId
    || !hasReadySelectedProjectSession(snapshot, project)
    || activeProject?.project_id !== expected.activeProjectId
    || activeProject.project_etag !== expected.activeProjectEtag
    || activeProject.profile_id !== expected.activeProfileId) {
    return false;
  }
  const run = snapshot.runs.find((item) => item.id === expected.runId);
  return run?.project_id === expected.coreProjectId && !isTerminal(run.status);
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
  hasProject,
  busy,
  onConnect,
  onAccept,
  onSetup,
}: {
  snapshot: DesktopProductSnapshot;
  profile: RemoteProfileV1 | null;
  hasProject: boolean;
  busy: boolean;
  onConnect: (profile: RemoteProfileV1) => void;
  onAccept: (profileId: string) => void;
  onSetup: () => void;
}) {
  const core = snapshot.state.core;
  const profileId = profile?.profile_id ?? null;
  if (core.state === "online" && core.profile_id === profileId) return null;
  if (!hasProject && core.state === "offline" && core.failure?.code === "core_not_started" && profile?.connection_state === "connected") return null;
  if (core.state === "degraded") {
    return <InlineNotice tone="warning" title="Remote workspace needs attention" detail={core.failure?.message ?? "Open System to review service status and operation logs."} />;
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
    return null;
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
  disabledReason,
  onActivate,
}: {
  project: ProjectV1;
  busy: boolean;
  disabledReason: string | null;
  onActivate: () => void;
}) {
  const blocked = project.state === "blocked";
  return (
    <section className="connection-gate" aria-live="polite">
      <div className="gate-icon"><FolderOpen size={21} /></div>
      <div className="gate-copy">
        <h2>{blocked ? "Project needs attention" : "Activate this project"}</h2>
        <p>{blocked ? "This project cannot be activated by this Desktop release. Review the remote Core state before retrying." : "Activation binds this project to its own profile and tunnel."}</p>
      </div>
      {!blocked ? <button className="primary-button" type="button" onClick={onActivate} disabled={busy || disabledReason !== null} title={disabledReason ?? "Activate this project"}><ArrowRight size={16} /> Activate project</button> : null}
    </section>
  );
}

function ResearchWorkspace({
  project,
  hasProfile,
  canCreateProject,
  executionModeLabel,
  runs,
  activeRun,
  timelines,
  provider,
  streamEpoch,
  modelService,
  canStart,
  startReason,
  busy,
  onStart,
  onRetry,
  onCancel,
  onOpenSettings,
  onOpenConnection,
  onOpenEvolution,
  onOpenSystem,
  onRefresh,
}: {
  project: ProjectV1 | null;
  hasProfile: boolean;
  canCreateProject: boolean;
  executionModeLabel: string | null;
  runs: readonly RunV1[];
  activeRun: RunV1 | null;
  timelines: DesktopProductSnapshot["timelines"];
  provider: DesktopProductProvider;
  streamEpoch: number;
  modelService: ServiceV1 | null;
  canStart: boolean;
  startReason: string | null;
  busy: boolean;
  onStart: () => void;
  onRetry: (run: RunV1) => void;
  onCancel: () => void;
  onOpenSettings: () => void;
  onOpenConnection: () => void;
  onOpenEvolution: () => void;
  onOpenSystem: () => void;
  onRefresh: () => void;
}) {
  if (!project) {
    if (!hasProfile) return <EmptyState icon={PanelLeft} title="Add a remote workspace" detail="Enter the server that will run research sessions." action="Add workspace" actionIcon={Plus} onAction={onOpenConnection} />;
    if (!canCreateProject) return <EmptyState icon={PanelLeft} title="Connect the remote workspace" detail="Confirm the server connection before creating a research project." />;
    return <EmptyState icon={FolderOpen} title="Create a research project" detail="Define a task and source to begin a session." action="Create project" onAction={onOpenSettings} />;
  }
  const latestTerminal = runs.find((run) => isTerminal(run.status));
  const outputRun = activeRun ?? latestTerminal ?? null;
  const recover = (run: RunV1) => {
    const action = run.current_error?.repair_action;
    const disabled = busy || activeRun !== null;
    if (action === "openevo_can_install" || action === "openevo_can_reconfigure" || action === "unsupported") return { label: "Open System", onClick: onOpenSystem, disabled };
    if (action === "user_action_required") return { label: "Edit project", onClick: onOpenSettings, disabled };
    return { label: "Retry session", onClick: () => onRetry(run), disabled };
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
            <div><span>Mode</span><strong>{executionModeLabel}</strong></div>
            <div><span>Capture</span><strong>Session transcript</strong></div>
            <div><span>Evolution</span><strong>{Object.values(project.evolution.targets).filter((target) => target.enabled).length} targets</strong></div>
          </div>
        </section>

        <section className="product-panel active-run-panel">
          <div className="panel-heading">
            <div><span className="panel-kicker">Active session</span><h2>{activeRun ? sessionTitle(activeRun, runs) : "No session running"}</h2></div>
            {activeRun ? <StatePill state={activeRun.status} /> : <span className="muted-pill">Ready</span>}
          </div>
          {activeRun ? (
            <>
              <RevisionPin run={activeRun} />
              <RunStatusDetail run={activeRun} modelService={modelService} onRefresh={onRefresh} />
              <Timeline entries={timelines[activeRun.id] ?? []} />
              <button className="danger-text-button" type="button" onClick={onCancel} disabled={busy || activeRun.status === "cancelling"} title={busy ? "Another action is running" : "Cancel this session"}>
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

      {outputRun ? (
        <SessionOutput run={outputRun} provider={provider} streamEpoch={streamEpoch} />
      ) : null}

      <section className="history-section">
        <div className="section-heading"><div><History size={17} /><h2>Session history</h2></div><span>{runs.length} total</span></div>
        {runs.length ? <SessionTable runs={runs} activeRun={activeRun} modelService={modelService} onRefresh={onRefresh} onRecover={recover} /> : <div className="empty-row">Completed and active sessions will appear here.</div>}
      </section>
    </div>
  );
}

function RevisionPin({ run }: { run: RunV1 }) {
  const successor = run.revision_transition?.successor_revision ?? null;
  return (
    <div className="revision-pin">
      <div><span>Pinned context</span><strong>{run.pinned_revision ? `Revision ${run.pinned_revision.generation}` : "Admission pending"}</strong></div>
      <ArrowRight size={16} />
      <div><span>Successor revision</span><strong>{successor ? `Revision ${successor.generation}` : "Not reported"}</strong></div>
      {run.revision_transition ? <StatePill state={run.revision_transition.state} /> : null}
    </div>
  );
}

function Timeline({ entries }: { entries: readonly DesktopProductSnapshot["timelines"][string][number][] }) {
  const visible = [...entries].sort((left, right) => compareTimestampAndId(left.occurred_at, left.id, right.occurred_at, right.id)).slice(-4);
  return (
    <ol className="run-timeline">
      {visible.map((entry) => (
        <li key={entry.id} className={entry.status}>
          <span className="timeline-marker">{entry.status === "succeeded" ? <Check size={11} /> : entry.status === "running" ? <LoaderCircle className="spin" size={11} /> : null}</span>
          <div><strong>{entry.title}</strong><span>{entry.message}</span></div>
        </li>
      ))}
    </ol>
  );
}

type SessionLogFilter = "all" | "agent" | "evolution" | "system";

function SessionOutput({
  run,
  provider,
  streamEpoch,
}: {
  run: RunV1;
  provider: DesktopProductProvider;
  streamEpoch: number;
}) {
  const identity = useMemo(
    () => sessionOutputIdentity(run),
    [run.current_attempt_id, run.id],
  );
  const [output, setOutput] = useState<{
    readonly identity: SessionOutputIdentity;
    readonly logs: readonly LogEntryV1[];
    readonly loading: boolean;
    readonly error: string | null;
  }>({ identity, logs: [], loading: true, error: null });
  const [filter, setFilter] = useState<SessionLogFilter>("all");
  const [retry, setRetry] = useState(0);
  const requestSequence = useRef(0);
  const currentIdentity = useRef(identity);

  useLayoutEffect(() => {
    currentIdentity.current = identity;
  }, [identity]);

  useEffect(() => {
    setFilter("all");
  }, [identity]);

  useEffect(() => {
    const request = requestSequence.current + 1;
    requestSequence.current = request;
    setOutput({ identity, logs: [], loading: true, error: null });
    void provider.getRunLogs(identity.runId)
      .then((next) => {
        if (requestSequence.current === request
          && sameSessionOutputIdentity(currentIdentity.current, identity)) {
          setOutput({ identity, logs: next, loading: false, error: null });
        }
      })
      .catch((reason) => {
        if (requestSequence.current === request
          && sameSessionOutputIdentity(currentIdentity.current, identity)) {
          setOutput({ identity, logs: [], loading: false, error: userMessage(reason) });
        }
      });
    return () => {
      if (requestSequence.current === request) requestSequence.current += 1;
    };
  }, [identity, provider, retry, run.updated_at, streamEpoch]);

  const currentOutput = sameSessionOutputIdentity(output.identity, identity)
    ? output
    : { identity, logs: [] as readonly LogEntryV1[], loading: true, error: null };
  const { logs, loading, error } = currentOutput;

  const filtered = logs.filter((entry) => {
    if (filter === "all") return true;
    if (filter === "system") return entry.stream === "core" || entry.stream === "service";
    return entry.stream === filter;
  });
  const visible = filtered.slice(-200);
  return (
    <section className="product-panel session-output-panel" aria-label="Session output">
      <div className="panel-heading">
        <div><span className="panel-kicker">Live transcript</span><h2>Session output</h2></div>
        <div className="session-output-state">
          {loading ? <LoaderCircle className="spin" size={14} aria-label="Refreshing session output" /> : null}
          <span>{logs.length} records</span>
        </div>
      </div>
      <div className="session-output-toolbar" role="group" aria-label="Session output filter">
        {([
          ["all", "All logs"],
          ["agent", "Agent logs"],
          ["evolution", "Evolution logs"],
          ["system", "System logs"],
        ] as const).map(([value, label]) => (
          <button
            key={value}
            type="button"
            className={filter === value ? "active" : ""}
            aria-pressed={filter === value}
            onClick={() => setFilter(value)}
          >
            {label}
          </button>
        ))}
      </div>
      {error ? (
        <div className="session-output-error" role="alert">
          <AlertCircle size={16} />
          <span>{error}</span>
          <button type="button" className="text-button" onClick={() => setRetry((value) => value + 1)}>
            <RefreshCw size={14} /> Retry output
          </button>
        </div>
      ) : visible.length > 0 ? (
        <div className="session-output-list" aria-live="polite">
          {filtered.length > visible.length ? (
            <div className="session-output-limit">Showing the latest 200 matching records.</div>
          ) : null}
          {visible.map((entry) => (
            <article key={entry.id} className={`session-output-entry ${entry.level}`}>
              <div className="session-output-meta">
                <span className={`session-stream ${entry.stream}`}>{sessionStreamLabel(entry.stream)}</span>
                <time dateTime={entry.occurred_at}>{formatTime(entry.occurred_at)}</time>
              </div>
              <div className="session-output-message">{entry.message}</div>
            </article>
          ))}
        </div>
      ) : loading ? (
        <div className="session-output-empty">Loading session output...</div>
      ) : (
        <div className="session-output-empty">No {filter === "all" ? "session" : filter} output has been reported.</div>
      )}
    </section>
  );
}

function sessionStreamLabel(stream: LogEntryV1["stream"]): string {
  if (stream === "agent") return "Agent";
  if (stream === "evolution") return "Evolution";
  if (stream === "service") return "Service";
  return "Core";
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
  onRecover: (run: RunV1) => { label: string; onClick: () => void; disabled: boolean };
}) {
  return (
    <div className="session-table" role="table" aria-label="Session history">
      <div className="session-table-head" role="row"><span role="columnheader">Session</span><span role="columnheader">State</span><span role="columnheader">Details</span><span role="columnheader">Pinned</span><span role="columnheader">Successor</span><span role="columnheader">Updated</span></div>
      {runs.map((run) => {
        const recovery = onRecover(run);
        return (
        <div className="session-table-row" role="row" key={run.id}>
          <strong role="cell">{sessionTitle(run, runs)}</strong>
          <span role="cell"><StatePill state={run.status} /></span>
          <span role="cell" className="session-detail">
            <RunStatusText run={run} modelService={modelService} />
            {run.status === "queued" ? <button type="button" className="text-button" onClick={onRefresh}><RefreshCw size={13} /> Refresh status</button> : null}
            {run.status === "failed" ? <button type="button" className="text-button" onClick={recovery.onClick} disabled={recovery.disabled}>{recovery.label === "Retry session" ? <RotateCcw size={13} /> : <Wrench size={13} />} {recovery.label}</button> : null}
          </span>
          <span role="cell">{run.pinned_revision ? `Revision ${run.pinned_revision.generation}` : "Pending"}</span>
          <span role="cell">{run.revision_transition ? `Revision ${run.revision_transition.successor_revision.generation}` : "Unknown"}</span>
          <span role="cell">{formatTime(run.updated_at)}</span>
        </div>
        );
      })}
    </div>
  );
}

function RunStatusDetail({ run, modelService, onRefresh }: { run: RunV1; modelService: ServiceV1 | null; onRefresh: () => void }) {
  if (run.status !== "queued" && run.status !== "failed") return null;
  return (
    <div className={`run-status-detail ${run.status}`}>
      <div>
        <strong>{run.status === "queued" && run.queued_reason ? queuedReasonLabel(run.queued_reason.code, modelService) : stateLabel(run.status)}</strong>
        <RunStatusText run={run} modelService={modelService} />
      </div>
      {run.status === "queued" ? <button type="button" className="secondary-button" onClick={onRefresh}><RefreshCw size={14} /> Refresh status</button> : null}
    </div>
  );
}

function RunStatusText({ run, modelService }: { run: RunV1; modelService: ServiceV1 | null }) {
  if (run.status === "queued" && run.queued_reason) {
    const retry = run.queued_reason.retry_after_seconds === null ? "" : ` Check again in about ${run.queued_reason.retry_after_seconds} seconds.`;
    const model = run.queued_reason.code === "service_starting" && modelService?.status === "starting" ? ` ${modelService.status_message ?? ""}` : "";
    return <span>{run.queued_reason.summary}{model}{retry}</span>;
  }
  if (run.status === "failed" && run.current_error) return <span>{run.current_error.message}{run.current_error.next_action ? ` ${run.current_error.next_action}` : ""}</span>;
  if (run.status === "cancelled") return <span>Cancelled without reporting a successful successor.</span>;
  if (run.status === "succeeded") return <span>{run.revision_transition?.state === "active" ? `Revision ${run.revision_transition.successor_revision.generation} is active.` : "The session succeeded; successor readiness is not yet known."}</span>;
  return <span>{stateLabel(run.status)}</span>;
}

function queuedReasonLabel(code: NonNullable<RunV1["queued_reason"]>["code"], modelService: ServiceV1 | null): string {
  if (code === "capacity") return "Waiting for capacity";
  if (code === "required_revision_uncommitted") return "Waiting for revision";
  if (code === "admission_pending") return "Admission pending";
  return modelService?.status === "starting" ? "Model preparation" : "Service preparation";
}

function RunOutcomeSummary({ run, onOpenEvolution, recovery }: { run: RunV1; onOpenEvolution: () => void; recovery: { label: string; onClick: () => void; disabled?: boolean } }) {
  const succeeded = run.status === "succeeded";
  return (
    <div className={`completed-summary ${run.status}`}>
      {succeeded ? <CheckCircle2 size={25} /> : run.status === "failed" ? <XCircle size={25} /> : <Square size={22} />}
      <div><strong>{succeeded ? "Latest session complete" : run.status === "failed" ? "Latest session failed" : "Latest session cancelled"}</strong><RunStatusText run={run} modelService={null} /></div>
      {succeeded && run.revision_transition?.state === "active" ? <button className="text-button" type="button" onClick={onOpenEvolution}>View changes <ArrowRight size={14} /></button> : null}
      {run.status === "failed" ? <button className="text-button" type="button" onClick={recovery.onClick} disabled={recovery.disabled}>{recovery.label === "Retry session" ? <RotateCcw size={14} /> : <Wrench size={14} />} {recovery.label}</button> : null}
    </div>
  );
}

function EvolutionWorkspace({ project, runs, artifacts, artifactCollection, provider, onRefresh, onOpenSettings }: { project: ProjectV1 | null; runs: readonly RunV1[]; artifacts: readonly ArtifactV1[]; artifactCollection: ProductArtifactCollectionState; provider: DesktopProductProvider; onRefresh: () => void; onOpenSettings: () => void }) {
  const activeRevision = project ? authoritativeActiveRevision(project, runs) : null;
  const orderedArtifacts = activeRevision && artifactCollection.status === "complete"
    ? selectedArtifactsForRevision(artifacts, activeRevision)
    : [];
  const [selectedArtifactId, setSelectedArtifactId] = useState<string | null>(orderedArtifacts[0]?.id ?? null);
  const [view, setView] = useState<"content" | "diff">("content");
  const [content, setContent] = useState<ArtifactContentV1 | null>(null);
  const [diff, setDiff] = useState<ArtifactDiffV1 | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedArtifactId || orderedArtifacts.some((artifact) => artifact.id === selectedArtifactId)) return;
    setSelectedArtifactId(orderedArtifacts[0]?.id ?? null);
  }, [artifactCollection.status, artifacts, selectedArtifactId]);
  useEffect(() => {
    if (!selectedArtifactId && orderedArtifacts[0]) setSelectedArtifactId(orderedArtifacts[0].id);
  }, [artifacts, selectedArtifactId]);
  useEffect(() => {
    if (!selectedArtifactId) return;
    const selectedArtifact = orderedArtifacts.find((artifact) => artifact.id === selectedArtifactId);
    if (!selectedArtifact) return;
    let active = true;
    setLoading(true);
    setError(null);
    setContent(null);
    setDiff(null);
    const load = async () => {
      if (view === "content") {
        const result = await provider.getArtifactContent(selectedArtifact.id);
        const identityError = artifactContentIdentityError(selectedArtifact, result);
        if (identityError) throw new DesktopProductUserError(identityError);
        if (active) setContent(result);
      } else {
        const result = await provider.getArtifactDiff(selectedArtifact.id);
        const identityError = artifactDiffIdentityError(selectedArtifact, result, artifacts);
        if (identityError) throw new DesktopProductUserError(identityError);
        if (active) setDiff(result);
      }
    };
    void load().catch((reason) => active && setError(userMessage(reason))).finally(() => active && setLoading(false));
    return () => { active = false; };
  }, [artifacts, provider, selectedArtifactId, view]);

  if (!project) return <EmptyState icon={Sparkles} title="No evolution history" detail="Choose a project to inspect revisions and artifacts." />;
  const selected = orderedArtifacts.find((artifact) => artifact.id === selectedArtifactId) ?? null;
  const activeGeneration = activeRevision?.generation ?? null;
  const evolutionEnabled = Object.values(project.evolution.targets).some((target) => target.enabled);
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
      ) : artifactCollection.status !== "complete" ? (
        <EmptyState icon={RefreshCw} title="Artifact collection is incomplete" detail="Refetch all artifact pages before inspecting revision membership." action="Refetch artifacts" actionIcon={RefreshCw} onAction={onRefresh} />
      ) : orderedArtifacts.length === 0 ? (
        evolutionEnabled
          ? <EmptyState icon={MemoryStick} title="No evolved artifacts yet" detail="Complete a session to create memory, skills, and agent guidance for the next revision." />
          : <EmptyState icon={Settings} title="Evolution is off" detail="No evolution targets are enabled for future sessions." action="Configure evolution" actionIcon={Settings} onAction={onOpenSettings} />
      ) : (
        <div className="artifact-layout">
          <aside className="artifact-list" aria-label="Evolution artifacts">
            <div className="artifact-list-heading"><span>{activeGeneration === null ? "Revision unknown" : `Revision ${activeGeneration}`}</span><strong>{orderedArtifacts.length} selected</strong></div>
            {orderedArtifacts.map((artifact) => (
              <button key={artifact.id} type="button" className={`artifact-list-item ${artifact.id === selected?.id ? "active" : ""}`} onClick={() => setSelectedArtifactId(artifact.id)}>
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
                <div className="segmented-control" role="tablist" aria-label="Artifact view" onKeyDown={handleTablistKeyDown}>
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
        <div className="document-tabs" role="tablist" aria-label="Artifact documents" onKeyDown={handleTablistKeyDown}>
          {content.documents.map((item) => <button role="tab" aria-selected={item.document_id === document?.document_id} tabIndex={item.document_id === document?.document_id ? 0 : -1} key={item.document_id} type="button" className={item.document_id === document?.document_id ? "active" : ""} onClick={() => setDocumentId(item.document_id)}>{item.display_name}</button>)}
        </div>
      ) : null}
      {document ? <pre className="artifact-document">{document.content}</pre> : null}
    </>
  );
}

function ArtifactDiff({ diff }: { diff: ArtifactDiffV1 }) {
  if (diff.document_changes.length === 0) return <div className="quiet-empty"><FileDiff size={22} /><p>No document changes are available for this revision.</p></div>;
  return (
    <div className="diff-view">
      {diff.truncated ? <InlineNotice tone="warning" title="Change preview is truncated" detail="Some changes are not shown in this preview." /> : null}
      {diff.document_changes.map((change, changeIndex) => {
        const oldPath = "old_document" in change ? change.old_document.relative_path : null;
        const newPath = "new_document" in change ? change.new_document.relative_path : null;
        const heading = change.kind === "renamed" ? `${oldPath} to ${newPath}` : (newPath ?? oldPath);
        const emptyMessage = change.kind === "renamed"
          ? "Renamed without content changes."
          : change.kind === "added"
            ? "Empty document added."
            : change.kind === "removed"
              ? "Empty document removed."
              : "Content identity changed without line changes.";
        return <section key={`${change.kind}-${changeIndex}`} className="diff-hunk">
          <div className="diff-document-heading"><span>{change.kind}</span><h3>{heading}</h3></div>
          {change.hunks.map((hunk, hunkIndex) => hunk.lines.map((line, lineIndex) => <div key={`${change.kind}-${changeIndex}-${hunkIndex}-${lineIndex}`} className={`diff-line ${line.kind}`}><span>{line.kind === "added" ? "+" : line.kind === "removed" ? "-" : " "}</span><code>{line.text}</code></div>))}
          {change.hunks.length === 0 ? <p className="diff-document-empty">{emptyMessage}</p> : null}
        </section>
      })}
    </div>
  );
}

function SystemWorkspace({ snapshot, profile, services, projectSessionReady, busy, onConnect, onConfigure }: { snapshot: DesktopProductSnapshot; profile: RemoteProfileV1 | null; services: readonly ServiceV1[]; projectSessionReady: boolean; busy: boolean; onConnect: () => void; onConfigure: () => void }) {
  const core = snapshot.state.core;
  return (
    <div className="workspace-stack" data-testid="system-workspace">
      <div className="workspace-heading"><div><p className="eyebrow">System</p><h1>Remote environment</h1><p>Connection, service status, and model availability.</p></div></div>
      <div className="system-grid">
        <section className="product-panel connection-panel">
          <div className="panel-heading"><div><span className="panel-kicker">Connection</span><h2>{profile?.name ?? "No remote workspace"}</h2></div><StatePill state={core.state} /></div>
          <dl className="definition-list">
            <div><dt>Server</dt><dd>{profile ? `${profile.host}:${profile.port}` : "Not configured"}</dd></div>
            <div><dt>Secure connection</dt><dd>{core.active_tunnel ? "Active" : "Not connected"}</dd></div>
            <div><dt>Compatibility</dt><dd>{snapshot.state.contract.compatible ? "Compatible" : "Needs update"}</dd></div>
            <div><dt>Project access</dt><dd>{projectSessionReady ? "Ready" : "Unavailable"}</dd></div>
          </dl>
          <div className="system-button-row">
            <button className="secondary-button" type="button" onClick={onConfigure}><Settings size={15} /> {profile ? "Edit" : "Add workspace"}</button>
            {profile && core.state !== "online" ? <button className="secondary-button" type="button" onClick={onConnect} disabled={busy || isConnectionBusy(core.state) || missingCredentialReason(profile) !== null} title={busy ? "A connection action is already running" : missingCredentialReason(profile) ?? "Reconnect remote workspace"}><RefreshCw size={15} /> Reconnect</button> : null}
          </div>
        </section>
      </div>
      <section className="services-section">
        <div className="section-heading"><div><Activity size={17} /><h2>Services</h2></div><span>{services.filter((service) => service.status === "running").length} of {services.length} ready</span></div>
        <div className="service-list">
          {services.map((service) => <ServiceRow key={service.id} service={service} />)}
          {!services.length ? <div className="empty-row">Services are unavailable for this project.</div> : null}
        </div>
      </section>
    </div>
  );
}

function ServiceRow({ service }: { service: ServiceV1 }) {
  return (
    <div className="service-row">
      <span className={`service-indicator ${service.status}`} />
      <div><strong>{service.display_name}</strong><span>{service.status_message ?? stateLabel(service.status)}</span></div>
      <StatePill state={service.status} />
      <span className="service-spacer" />
    </div>
  );
}

function RemoteWorkspaceDrawer({
  profile,
  observedProfiles,
  streamEpoch,
  busy,
  onClose,
  createSaveIntent,
  onSave,
  onCreateObserved,
}: {
  profile: RemoteProfileV1 | null;
  observedProfiles: readonly RemoteProfileV1[];
  streamEpoch: number | null;
  busy: boolean;
  onClose: () => void;
  createSaveIntent: (input: ProfileCreateV1) => ProfileSaveIntent;
  onSave: (intent: ProfileSaveIntent) => Promise<ProfileSaveAttemptResult>;
  onCreateObserved: (profile: RemoteProfileV1) => void;
}) {
  const [name, setName] = useState(profile?.name ?? "Research server");
  const [host, setHost] = useState(profile?.host ?? "");
  const [port, setPort] = useState(String(profile?.port ?? 22));
  const [user, setUser] = useState(profile?.user ?? "");
  const [httpProxy, setHttpProxy] = useState(profile?.proxy.http_url ?? "");
  const [httpsProxy, setHttpsProxy] = useState(profile?.proxy.https_url ?? "");
  const [noProxy, setNoProxy] = useState(profile?.proxy.no_proxy.join(", ") ?? "");
  const [dirty, setDirty] = useState(profile !== null && profile.authentication_kind !== "ssh_agent");
  const guardedClose = useGuardedDrawerClose(dirty, onClose);
  const dialogRef = useDialogFocus(guardedClose.requestClose);
  const pendingSaveIntent = useRef<ProfileSaveIntent | null>(null);
  const parsedPort = Number(port);
  const valid = name.trim() !== "" && host.trim() !== "" && user.trim() !== "" && Number.isInteger(parsedPort) && parsedPort > 0 && parsedPort <= 65_535;
  const markDirty = () => { pendingSaveIntent.current = null; setDirty(true); };
  const update = (setter: (value: string) => void) => (event: React.ChangeEvent<HTMLInputElement>) => { setter(event.target.value); markDirty(); };
  useEffect(() => {
    const pending = pendingSaveIntent.current;
    const createdProfile = pending?.route.kind === "create"
      ? observedProfiles.find((item) => canonicalProfile(item) === pending.canonicalPayload)
      : null;
    if (createdProfile) {
      pendingSaveIntent.current = null;
      onCreateObserved(createdProfile);
    }
  }, [observedProfiles, onCreateObserved]);
  return (
    <div className="drawer-backdrop" role="presentation" onMouseDown={(event) => {
      if (guardedClose.confirming) {
        event.preventDefault();
      } else if (event.target === event.currentTarget) {
        event.preventDefault();
        guardedClose.requestClose();
      }
    }}>
      <aside ref={dialogRef} className="settings-drawer" role="dialog" aria-modal="true" aria-labelledby="workspace-settings-title" tabIndex={-1}>
        <div className="drawer-head" inert={guardedClose.confirming ? true : undefined} aria-hidden={guardedClose.confirming || undefined}><div><span className="panel-kicker">Remote workspace</span><h2 id="workspace-settings-title">Server connection</h2></div><IconButton label="Close connection settings" onClick={guardedClose.requestClose}><X size={18} /></IconButton></div>
        <div className="drawer-content" inert={guardedClose.confirming ? true : undefined} aria-hidden={guardedClose.confirming || undefined}>
          <section className="form-section">
            <h3>Server</h3>
            <label>Workspace name<input value={name} onChange={update(setName)} placeholder="Research server" /></label>
            <div className="form-grid host-grid"><label>Server address<input value={host} onChange={update(setHost)} placeholder="research.example.org" /></label><label>Port<input inputMode="numeric" value={port} onChange={update(setPort)} /></label></div>
            <label>User name<input value={user} onChange={update(setUser)} /></label>
          </section>
          <section className="form-section">
            <h3>Authentication</h3>
            <div className="agent-note"><ShieldCheck size={17} /><span>SSH agent</span></div>
            {profile && profile.authentication_kind !== "ssh_agent" ? <p className="form-error" role="alert">The saved authentication method is unavailable in this release. Save this workspace to use SSH agent.</p> : null}
          </section>
          <section className="form-section">
            <h3>Network proxy</h3>
            <label>HTTP proxy<input value={httpProxy} onChange={update(setHttpProxy)} placeholder="Optional HTTP origin" /></label>
            <label>HTTPS proxy<input value={httpsProxy} onChange={update(setHttpsProxy)} placeholder="Optional HTTPS origin" /></label>
            <label>Bypass proxy for<input value={noProxy} onChange={update(setNoProxy)} placeholder="localhost, example.org" /></label>
          </section>
        </div>
        {guardedClose.confirming ? <DiscardChangesPrompt onKeep={guardedClose.keepEditing} onDiscard={guardedClose.discard} /> : null}
        <div className="drawer-footer" inert={guardedClose.confirming ? true : undefined} aria-hidden={guardedClose.confirming || undefined}><button className="secondary-button" type="button" onClick={guardedClose.requestClose}>Cancel</button><button className="primary-button" type="button" disabled={!valid || busy || streamEpoch === null || (profile !== null && !dirty)} title={!valid ? "Complete the required server fields" : streamEpoch === null ? "Refresh this view before saving" : profile && !dirty ? "No unsaved changes" : "Save remote workspace"} onClick={() => {
          const input: ProfileCreateV1 = {
            name: name.trim(),
            host: host.trim(),
            port: parsedPort,
            user: user.trim(),
            authentication_kind: "ssh_agent",
            proxy: {
              http_url: httpProxy.trim() || null,
              https_url: httpsProxy.trim() || null,
              no_proxy: noProxy.split(",").map((value) => value.trim()).filter(Boolean),
            },
          };
          const pending = pendingSaveIntent.current;
          const intent = pending?.canonicalPayload === canonicalProfilePayload(input) ? pending : createSaveIntent(input);
          pendingSaveIntent.current = intent;
          void onSave(intent).then((result) => { pendingSaveIntent.current = result.pendingIntent; });
        }}><Save size={15} /> {busy ? "Saving..." : "Save workspace"}</button></div>
      </aside>
    </div>
  );
}

function SettingsDrawer({
  project,
  profileId,
  executionModeCapabilities,
  capability,
  capabilities,
  busy,
  onClose,
  onRetryCapabilities,
  onSave,
  onSelectSource,
  onCancelSource,
  onSettleSource,
}: {
  project: ProjectV1 | null;
  profileId: string | null;
  executionModeCapabilities: ExecutionModeCapabilitiesV1;
  capability: DesktopProductSnapshot["capability"];
  capabilities: EvolutionCapabilitiesV1 | null;
  busy: boolean;
  onClose: () => void;
  onRetryCapabilities: () => Promise<unknown>;
  onSave: (
    input: ProjectPatchV1,
    actionId: string,
    pendingSourceActionId: string | null,
  ) => Promise<SaveAttemptResult>;
  onSelectSource: (actionId: string) => Promise<ProjectSourceV1>;
  onCancelSource: (actionId: string) => Promise<void>;
  onSettleSource: (actionId: string, outcome: "adopt" | "discard") => Promise<void>;
}) {
  const [name, setName] = useState(project?.name ?? "New research project");
  const [title, setTitle] = useState(project?.task.title ?? "Research task");
  const [objective, setObjective] = useState(project?.task.objective ?? "");
  const [source, setSource] = useState<ProjectSourceV1>(project?.source ?? { kind: "scratch", display_name: "New workspace", import_ref: null });
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [selectingSource, setSelectingSource] = useState(false);
  const defaultMode = project?.execution.mode
    ?? firstSupportedExecutionMode(executionModeCapabilities)?.mode
    ?? executionModeCapabilities.modes[0].mode;
  const [mode, setMode] = useState<ProjectV1["execution"]["mode"]>(defaultMode);
  const [hfModel, setHfModel] = useState(project?.execution.hf_model ?? DEFAULT_HF_MODEL);
  const [codexModel, setCodexModel] = useState(project?.execution.codex_model ?? DEFAULT_CODEX_MODEL);
  const [evolution, setEvolution] = useState<ProductEvolutionTargets>(project?.evolution.targets ?? {});
  const [dirty, setDirty] = useState(false);
  const [retryingCapabilities, setRetryingCapabilities] = useState(false);
  const setupDefaultsApplied = useRef(false);
  const sourceSelectionGeneration = useRef(0);
  const sourceSelectionInFlight = useRef(false);
  const sourceSelectionMounted = useRef(true);
  const activeSourceActionId = useRef<string | null>(null);
  const pendingSourceActionId = useRef<string | null>(null);
  const onCancelSourceRef = useRef(onCancelSource);
  onCancelSourceRef.current = onCancelSource;
  const onSettleSourceRef = useRef(onSettleSource);
  onSettleSourceRef.current = onSettleSource;
  const invalidateSourceSelection = useCallback(() => {
    sourceSelectionGeneration.current += 1;
  }, []);
  const cancelActiveSource = useCallback(async () => {
    const actionId = activeSourceActionId.current;
    activeSourceActionId.current = null;
    if (actionId !== null) await onCancelSource(actionId);
  }, [onCancelSource]);
  const takePendingSourceAction = useCallback(() => {
    const actionId = pendingSourceActionId.current;
    pendingSourceActionId.current = null;
    return actionId;
  }, []);
  const settlePendingSource = useCallback(async (outcome: "adopt" | "discard") => {
    const actionId = takePendingSourceAction();
    if (actionId !== null) await onSettleSource(actionId, outcome);
  }, [onSettleSource, takePendingSourceAction]);
  const close = useCallback(() => {
    invalidateSourceSelection();
    void cancelActiveSource();
    void settlePendingSource("discard").finally(onClose);
  }, [cancelActiveSource, invalidateSourceSelection, onClose, settlePendingSource]);
  const guardedClose = useGuardedDrawerClose(dirty, close);
  const requestClose = () => {
    invalidateSourceSelection();
    void cancelActiveSource();
    guardedClose.requestClose();
  };
  const dialogRef = useDialogFocus(requestClose);
  const saveActionId = useRef(newActionId());
  const activeModel = mode === "self-deployed" ? hfModel : codexModel;
  const activeModeCapability = executionModeCapability(executionModeCapabilities, mode);
  const focusMode = activeModeCapability.support_state === "supported"
    ? mode
    : firstSupportedExecutionMode(executionModeCapabilities)?.mode;
  const modeCapabilities = capabilities && capabilityExecutionMode(capabilities) === mode ? capabilities : null;
  const incompleteSetup = project?.evolution_configuration_state === "pending";
  const capabilityMatchesDraft = Boolean(project
    && capability
    && capability.projectId === project.project_id
    && capability.executionMode === mode);
  const capabilityRetryable = capabilityMatchesDraft && capability?.status === "unavailable";
  const markDirty = () => { saveActionId.current = newActionId(); setDirty(true); };
  const change = (setter: (value: string) => void) => (event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => { setter(event.target.value); markDirty(); };
  const reset = async () => {
    invalidateSourceSelection();
    await Promise.all([cancelActiveSource(), settlePendingSource("discard")]);
    setName(project?.name ?? "New research project");
    setTitle(project?.task.title ?? "Research task");
    setObjective(project?.task.objective ?? "");
    setSource(project?.source ?? { kind: "scratch", display_name: "New workspace", import_ref: null });
    setMode(project?.execution.mode ?? firstSupportedExecutionMode(executionModeCapabilities)?.mode ?? executionModeCapabilities.modes[0].mode);
    setHfModel(project?.execution.hf_model ?? DEFAULT_HF_MODEL);
    setCodexModel(project?.execution.codex_model ?? DEFAULT_CODEX_MODEL);
    setEvolution(project?.evolution.targets ?? {});
    setDirty(false);
    setSourceError(null);
  };
  const selectSource = async () => {
    if (sourceSelectionInFlight.current) return;
    sourceSelectionInFlight.current = true;
    const generation = sourceSelectionGeneration.current + 1;
    sourceSelectionGeneration.current = generation;
    setSelectingSource(true);
    setSourceError(null);
    const actionId = newActionId();
    try {
      await settlePendingSource("discard");
      if (sourceSelectionGeneration.current !== generation) return;
      activeSourceActionId.current = actionId;
      const selected = await onSelectSource(actionId);
      if (activeSourceActionId.current === actionId) activeSourceActionId.current = null;
      if (sourceSelectionGeneration.current !== generation) {
        await onSettleSource(actionId, "discard");
        return;
      }
      pendingSourceActionId.current = actionId;
      setSource(selected);
      markDirty();
    } catch (error) {
      try {
        await onSettleSource(actionId, "discard");
      } catch {
        // The native host keeps a failed settle pending for retry or startup recovery.
      }
      if (sourceSelectionGeneration.current !== generation) return;
      if (!isWorkspaceSelectionCancelled(error)) setSourceError(userMessage(error));
    } finally {
      if (activeSourceActionId.current === actionId) activeSourceActionId.current = null;
      sourceSelectionInFlight.current = false;
      if (sourceSelectionMounted.current) setSelectingSource(false);
    }
  };
  useEffect(() => {
    sourceSelectionMounted.current = true;
    return () => {
      sourceSelectionMounted.current = false;
      sourceSelectionGeneration.current += 1;
      const activeActionId = activeSourceActionId.current;
      activeSourceActionId.current = null;
      if (activeActionId !== null) void onCancelSourceRef.current(activeActionId);
      const actionId = takePendingSourceAction();
      if (actionId !== null) void onSettleSourceRef.current(actionId, "discard");
    };
  }, [takePendingSourceAction]);
  const rows = evolutionTargetRows(modeCapabilities, evolution);
  useEffect(() => {
    if (!incompleteSetup || !modeCapabilities || setupDefaultsApplied.current) return;
    const requiredRows = REQUIRED_EVOLUTION_TARGETS.map((targetId) => rows.find((row) => row.targetId === targetId));
    if (requiredRows.some((row) => !row?.canEnable)) return;
    setupDefaultsApplied.current = true;
    setEvolution(Object.fromEntries(requiredRows.map((row) => [row!.targetId, enableTarget(row!)])));
    saveActionId.current = newActionId();
    setDirty(true);
  }, [incompleteSetup, modeCapabilities, rows]);
  const setupReady = !incompleteSetup || (modeCapabilities !== null && rows.every((row) => !row.selection.enabled || row.valid));
  const valid = name.trim().length > 0
    && title.trim().length > 0
    && objective.trim().length > 0
    && activeModel.trim().length > 0
    && activeModeCapability.support_state === "supported"
    && profileId !== null
    && setupReady
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
    <div className="drawer-backdrop" role="presentation" onMouseDown={(event) => {
      if (guardedClose.confirming) {
        event.preventDefault();
      } else if (event.target === event.currentTarget) {
        event.preventDefault();
        requestClose();
      }
    }}>
      <aside ref={dialogRef} className="settings-drawer" role="dialog" aria-modal="true" aria-labelledby="settings-title" tabIndex={-1}>
        <div className="drawer-head" inert={guardedClose.confirming ? true : undefined} aria-hidden={guardedClose.confirming || undefined}><div><span className="panel-kicker">{project ? "Project settings" : "New project"}</span><h2 id="settings-title">Research configuration</h2></div><IconButton label="Close settings" onClick={requestClose}><X size={18} /></IconButton></div>
        <div className="drawer-content" inert={guardedClose.confirming ? true : undefined} aria-hidden={guardedClose.confirming || undefined}>
          <section className="form-section">
            <h3>Project</h3>
            <label>Project name<input value={name} onChange={change(setName)} /></label>
            <label>Task title<input value={title} onChange={change(setTitle)} /></label>
            <label>Objective<textarea rows={5} value={objective} onChange={change(setObjective)} /></label>
          </section>
          <section className="form-section">
            <h3>Research source</h3>
            <div className="segmented-control wide" role="tablist" aria-label="Research source" onKeyDown={handleTablistKeyDown}>
              <button type="button" role="tab" aria-selected={source.kind === "scratch"} tabIndex={source.kind === "scratch" ? 0 : -1} className={source.kind === "scratch" ? "active" : ""} disabled={selectingSource || busy} onClick={() => { invalidateSourceSelection(); void settlePendingSource("discard").then(() => { setSource({ kind: "scratch", display_name: "New workspace", import_ref: null }); setSourceError(null); markDirty(); }); }}>Scratch</button>
              <button type="button" role="tab" aria-selected={source.kind === "native_folder_snapshot"} tabIndex={source.kind === "native_folder_snapshot" ? 0 : -1} className={source.kind === "native_folder_snapshot" ? "active" : ""} disabled={selectingSource || busy} onClick={() => void selectSource()}>{selectingSource ? "Selecting..." : "Folder snapshot"}</button>
            </div>
            <div className="source-summary"><FolderOpen size={17} /><span><strong>{source.display_name}</strong><small>{source.kind === "scratch" ? "A new managed workspace will be created." : "A native snapshot reference is ready."}</small></span></div>
            {sourceError ? <p className="form-error" role="alert">{sourceError}</p> : null}
          </section>
          <section className="form-section">
            <h3>Model mode</h3>
            <div className="segmented-control wide" role="tablist" aria-label="Model mode" onKeyDown={handleTablistKeyDown}>{executionModeCapabilities.modes.map((capability) => (
              <button
                type="button"
                role="tab"
                key={capability.mode}
                aria-selected={mode === capability.mode}
                aria-describedby={capability.support_state === "supported" ? undefined : "execution-mode-support-message"}
                tabIndex={focusMode === capability.mode ? 0 : -1}
                className={mode === capability.mode ? "active" : ""}
                disabled={capability.support_state !== "supported"}
                title={capability.message}
                onClick={() => { setMode(capability.mode); markDirty(); }}
              >{capability.display_name}</button>
            ))}</div>
            {mode === "self-deployed" ? <label>Hugging Face model<input value={hfModel} onChange={change(setHfModel)} placeholder="organization/model" /></label> : <label>Codex model<input value={codexModel} onChange={change(setCodexModel)} placeholder="Model name" /></label>}
            {activeModeCapability.support_state !== "supported" ? <p className="mode-support-message" id="execution-mode-support-message" role="status">{activeModeCapability.message}</p> : null}
            <p className="form-help">Sessions use transcript capture. Token-level metrics are unavailable in this mode.</p>
          </section>
          <section className="form-section">
            <h3>Evolution targets</h3>
            {incompleteSetup && modeCapabilities ? <div className="capability-ready" role="status"><CheckCircle2 size={15} /><p className="form-help">Remote evolution methods are ready. Review the defaults, then save and activate.</p></div> : null}
            {!modeCapabilities ? <div className="capability-unavailable" role="status"><p className="form-help">{project === null ? "Remote methods will be loaded after the project session is prepared." : capabilityMatchesDraft && capability?.status === "loading" ? "Capabilities are loading for this project and mode." : "Capabilities are unavailable for this project and mode."}</p>{capabilityRetryable ? <button type="button" className="secondary-button" onClick={() => void retryCapabilities()} disabled={retryingCapabilities || busy}><RefreshCw className={retryingCapabilities ? "spin" : undefined} size={14} /> Retry capabilities</button> : null}</div> : null}
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
                  setEvolution((current) => ({ ...current, [row.targetId]: { enabled: row.selection.enabled, method: selected.id, config: selected.defaultConfig as OpenEvoJsonObject } }));
                  markDirty();
                }}>
                  {row.choices.map((choice) => <option key={`${choice.kind}-${choice.id}`} value={choice.id ?? ""} disabled={!choice.selectable}>{choice.label}{choice.supported ? "" : " (unavailable)"}</option>)}
                </select>
                {row.selection.enabled && row.selectedChoice?.configSchema ? <MethodConfigEditor
                  schema={row.selectedChoice.configSchema}
                  defaultConfig={row.selectedChoice.defaultConfig as OpenEvoJsonObject}
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
        <div className="drawer-footer" inert={guardedClose.confirming ? true : undefined} aria-hidden={guardedClose.confirming || undefined}><button className="secondary-button" type="button" onClick={() => void reset()} disabled={!dirty || busy || selectingSource} title={!dirty ? "No unsaved changes" : "Undo changes"}><RotateCcw size={15} /> Undo</button><button className="primary-button" type="button" disabled={!valid || busy || selectingSource || (project !== null && !dirty)} title={!profileId ? "Add a remote workspace first" : activeModeCapability.support_state !== "supported" ? activeModeCapability.message : !valid ? "Complete all required fields and valid method settings" : project && !dirty ? "No unsaved changes" : "Save project settings"} onClick={() => { invalidateSourceSelection(); const pendingActionId = pendingSourceActionId.current; void onSave({
          name: name.trim(),
          task: { title: title.trim(), objective: objective.trim() },
          source,
          execution: mode === "self-deployed" ? selfDeployedExecution(activeModel.trim()) : subscriptionExecution(activeModel.trim()),
          evolution: { targets: evolution },
        }, saveActionId.current, pendingActionId).then((result) => {
          if (result.replaceActionId) saveActionId.current = newActionId();
          if (pendingActionId !== null && result.pendingSourceOutcome !== null && pendingSourceActionId.current === pendingActionId) {
            pendingSourceActionId.current = null;
            if (result.pendingSourceOutcome === "discarded") {
              setSource(project?.source ?? { kind: "scratch", display_name: "New workspace", import_ref: null });
            }
          }
        }); }}><Save size={15} /> {busy ? "Saving..." : project === null ? "Prepare evolution" : incompleteSetup ? "Save and activate" : "Save"}</button></div>
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
  const promptRef = useRef<HTMLDivElement | null>(null);
  const keepRef = useRef<HTMLButtonElement | null>(null);
  useLayoutEffect(() => {
    const keepFocusInside = (event: FocusEvent) => {
      if (event.target instanceof Node && !promptRef.current?.contains(event.target)) {
        keepRef.current?.focus();
      }
    };
    const preventOutsidePointerFocus = (event: MouseEvent) => {
      if (event.target instanceof Node && !promptRef.current?.contains(event.target)) {
        event.preventDefault();
        keepRef.current?.focus();
      }
    };
    keepRef.current?.focus();
    document.addEventListener("focusin", keepFocusInside);
    document.addEventListener("mousedown", preventOutsidePointerFocus, true);
    return () => {
      document.removeEventListener("focusin", keepFocusInside);
      document.removeEventListener("mousedown", preventOutsidePointerFocus, true);
    };
  }, []);
  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    event.stopPropagation();
    if (event.key === "Escape") {
      event.preventDefault();
      onKeep();
      return;
    }
    if (event.key !== "Tab") return;
    const actions = Array.from(promptRef.current?.querySelectorAll<HTMLButtonElement>("button:not([disabled])") ?? []);
    const first = actions[0];
    const last = actions.at(-1);
    if (event.shiftKey && document.activeElement === first && last) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last && first) {
      event.preventDefault();
      first.focus();
    }
  };
  return (
    <div className="discard-changes-modal" role="presentation">
      <div ref={promptRef} className="discard-changes-prompt" role="alertdialog" aria-modal="true" aria-labelledby="discard-title" aria-describedby="discard-detail" onKeyDown={handleKeyDown}>
        <div><strong id="discard-title">Discard unsaved changes?</strong><span id="discard-detail">Your draft stays open until you choose to discard it.</span></div>
        <button ref={keepRef} type="button" className="secondary-button" onClick={onKeep}>Keep editing</button>
        <button type="button" className="danger-text-button" onClick={onDiscard}>Discard changes</button>
      </div>
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
  readonly capability: EvolutionCapabilitiesV1["targets"][number] | null;
  readonly selection: ProductEvolutionTargets[string];
  readonly choices: readonly EvolutionChoiceRow[];
  readonly selectedChoice: EvolutionChoiceRow | null;
  readonly valid: boolean;
  readonly canEnable: boolean;
  readonly reason: string;
}

function evolutionTargetRows(
  capabilities: EvolutionCapabilitiesV1 | null,
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
      defaultConfig: parseCapabilityJsonObject(method.default_config_json),
      configSchema: parseCapabilityJsonObject(method.config_schema_json),
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
    const configErrors = selectedChoice?.configSchema
      ? methodConfigErrors(selectedChoice.configSchema, selectedChoice.defaultConfig as OpenEvoJsonObject, selection.config as OpenEvoJsonObject)
      : [];
    const valid = !selection.enabled || Boolean(selectedChoice?.supported && configErrors.length === 0);
    const supportedSelection = Boolean(selectedChoice?.supported && selection.method !== null);
    return {
      targetId,
      displayName: capability?.display_name ?? artifactTypeLabel(targetId),
      description: capability?.description ?? "This saved target is absent from current remote capabilities.",
      capability,
      selection,
      choices,
      selectedChoice: selectedChoice ?? null,
      valid,
      canEnable: Boolean(supportedSelection || defaultChoice),
      reason: capability
        ? supportedSelection
          ? configErrors[0] ?? ""
          : selection.method === null && capability.effective_default_method_id === null
            ? "No supported default is available from the remote registry."
            : selection.method === null
            ? "Choose a supported method before running."
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
  return { enabled: true, method: defaultChoice.id, config: defaultChoice.defaultConfig as OpenEvoJsonObject };
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

function profileSaveIntent(snapshot: DesktopProductSnapshot, profile: RemoteProfileV1 | null, input: ProfileCreateV1): ProfileSaveIntent {
  const canonicalPayload = canonicalProfilePayload(input);
  return profile
    ? {
        canonicalPayload,
        input,
        route: {
          kind: "update",
          profileId: profile.profile_id,
          intent: resourceIntent(snapshot, profile.etag),
        },
      }
    : {
        canonicalPayload,
        input,
        route: { kind: "create", intent: mutationIntent(snapshot) },
      };
}

function canonicalProfilePayload(input: ProfileCreateV1): string {
  return JSON.stringify({
    name: input.name,
    host: input.host,
    port: input.port ?? 22,
    user: input.user,
    authentication_kind: input.authentication_kind ?? "ssh_agent",
    proxy: {
      http_url: input.proxy?.http_url ?? null,
      https_url: input.proxy?.https_url ?? null,
      no_proxy: input.proxy?.no_proxy ?? [],
    },
  });
}

function canonicalProfile(profile: RemoteProfileV1): string {
  return canonicalProfilePayload({
    name: profile.name,
    host: profile.host,
    port: profile.port,
    user: profile.user,
    authentication_kind: profile.authentication_kind,
    proxy: profile.proxy,
  });
}

function matchingProfile(snapshot: DesktopProductSnapshot | null, canonicalPayload: string): RemoteProfileV1 | null {
  return snapshot?.profiles.find((profile) => canonicalProfile(profile) === canonicalPayload) ?? null;
}

function selectedArtifactsForRevision(artifacts: readonly ArtifactV1[], revision: RevisionRefV1): ArtifactV1[] {
  return artifacts
    .filter((artifact) => artifact.selected
      && artifact.membership_revisions.some((member) => sameRevisionRef(member, revision))
      && !artifactRevisionRefs(artifact).some((candidate) => candidate.id === revision.id && !sameRevisionRef(candidate, revision)))
    .sort((left, right) => {
      const time = Date.parse(right.created_at) - Date.parse(left.created_at);
      return time || left.id.localeCompare(right.id);
    });
}

function currentGeneration(project: ProjectV1, runs: readonly RunV1[]): number | null {
  return authoritativeActiveRevision(project, runs)?.generation ?? null;
}

function authoritativeActiveRevision(project: ProjectV1, runs: readonly RunV1[]) {
  const active = project.remote?.active_revision ?? null;
  if (!active || active.project_id !== project.remote?.core_project_id) return null;
  const matchingRunRefs = runs.flatMap(runRevisionRefs).filter((revision) => revision.id === active.id);
  if (matchingRunRefs.some((revision) => !sameRevisionRef(revision, active))) return null;
  return active;
}

function runRevisionRefs(run: RunV1): RevisionRefV1[] {
  return [
    ...(run.pinned_revision ? [run.pinned_revision] : []),
    run.required_revision.revision,
    ...(run.revision_transition
      ? [run.revision_transition.predecessor_revision, run.revision_transition.successor_revision]
      : []),
  ];
}

function artifactRevisionRefs(artifact: ArtifactV1): RevisionRefV1[] {
  return [artifact.produced_revision, ...artifact.membership_revisions];
}

function sameRevisionRef(left: RevisionRefV1, right: RevisionRefV1): boolean {
  return left.id === right.id
    && left.project_id === right.project_id
    && left.generation === right.generation
    && left.manifest_sha256 === right.manifest_sha256;
}

function artifactContentIdentityError(artifact: ArtifactV1, content: ArtifactContentV1): string | null {
  return content.artifact_id === artifact.id && content.artifact_type === artifact.artifact_type
    ? null
    : "Artifact content identity does not match the selected artifact. Refetch before viewing it.";
}

function artifactDiffIdentityError(artifact: ArtifactV1, diff: ArtifactDiffV1, artifacts: readonly ArtifactV1[]): string | null {
  if (diff.artifact_id !== artifact.id || diff.artifact_content_sha256 !== artifact.content_sha256) {
    return "Artifact change identity does not match the selected artifact. Refetch before viewing it.";
  }
  if (!artifact.lineage.source_artifact_ids.includes(diff.previous_artifact_id)) {
    return "Artifact change history does not match the selected artifact. Refetch before viewing it.";
  }
  const previousMatches = artifacts.filter((candidate) => candidate.id === diff.previous_artifact_id);
  if (previousMatches.length !== 1) {
    return "Artifact change history could not be verified. Refetch before viewing it.";
  }
  const previous = previousMatches[0];
  return previous.content_sha256 === diff.previous_artifact_content_sha256
    && previous.project_id === artifact.project_id
    && previous.target_id === artifact.target_id
    && previous.artifact_type === artifact.artifact_type
    ? null
    : "Artifact change history does not match the selected artifact. Refetch before viewing it.";
}

function revisionLabel(project: ProjectV1 | null, runs: readonly RunV1[]): string {
  if (!project) return "Not available";
  const generation = currentGeneration(project, runs);
  return generation === null ? "Revision unknown" : `Revision ${generation}`;
}

function getProjectActivationReason(
  snapshot: DesktopProductSnapshot,
  project: ProjectV1 | null,
  profile: RemoteProfileV1 | null,
  actionState: AsyncState,
): string | null {
  if (!project) return null;
  if (actionState === "working") return "Wait for the current action to finish.";
  if (project.state === "archived") return "Archived projects cannot be activated.";
  const modeCapability = executionModeCapability(snapshot.executionModeCapabilities, project.execution.mode);
  if (modeCapability.support_state !== "supported") return modeCapability.message;
  if (!profile || profile.profile_id !== project.profile_id) return "Configure this project's remote workspace before activation.";
  if (profile.connection_state !== "connected") return "Connect this project's remote workspace before activation.";
  const core = snapshot.state.core;
  if (core.profile_id !== project.profile_id) return "Connect this project's remote workspace before activation.";
  if (core.state === "online" || core.state === "degraded") return null;
  if (core.state === "offline" && core.failure?.code === "core_not_started") return null;
  return isConnectionBusy(core.state)
    ? "Wait for the remote workspace connection to finish."
    : "Reconnect this project's remote workspace before activation.";
}

function getStartReason(snapshot: DesktopProductSnapshot, project: ProjectV1 | null, profile: RemoteProfileV1 | null, activeRun: RunV1 | null, actionState: AsyncState): string | null {
  if (!project) return "Create or select a project first.";
  if (project.evolution_configuration_state === "pending") return "Finish evolution setup before starting a session.";
  const modeCapability = executionModeCapability(snapshot.executionModeCapabilities, project.execution.mode);
  if (modeCapability.support_state !== "supported") return modeCapability.message;
  if (snapshot.stream.status !== "fresh") return "Refresh this view before starting a session.";
  if (!profile || snapshot.state.core.state !== "online" || !snapshot.state.core.active_tunnel || snapshot.state.core.profile_id !== profile.profile_id) return "Connect this project's remote workspace before starting a session.";
  if (!project.remote) return "Activate this project on its assigned remote workspace before starting a session.";
  const active = snapshot.state.active_project;
  if (!active || active.project_id !== project.project_id || active.profile_id !== project.profile_id || active.project_etag !== project.etag || active.connection_state !== "ready") return "Activate this project on its assigned remote workspace before starting a session.";
  if (project.state !== "active") return "Activate this project before starting a session.";
  const capability = snapshot.capability;
  if (!capability || capability.status !== "ready" || capability.projectId !== project.project_id || capability.executionMode !== project.execution.mode || capability.value.project_id !== project.project_id || capabilityExecutionMode(capability.value.capabilities) !== project.execution.mode) return "Remote capabilities are unavailable for this project and mode.";
  const invalidTarget = evolutionTargetRows(capability.value.capabilities, project.evolution.targets).find((row) => row.selection.enabled && !row.valid);
  if (invalidTarget) return invalidTarget.reason;
  const validation = snapshot.validation;
  if (!validation || validation.status !== "ready" || validation.projectId !== project.project_id || validation.executionMode !== project.execution.mode || validation.projectEtag !== project.etag || validation.value.project_id !== project.project_id || validation.value.project_etag !== project.etag || validation.value.registry_digest !== capability.value.capabilities.registry_digest || !validation.value.valid) return "Project validation is not current for this project and mode.";
  if (activeRun) return "Wait for the active session to finish or cancel it.";
  if (actionState === "working") return "Wait for the current action to finish.";
  return null;
}

function hasReadySelectedProjectSession(
  snapshot: DesktopProductSnapshot,
  project: ProjectV1 | null,
): boolean {
  if (!project || project.state !== "active") return false;
  const active = snapshot.state.active_project;
  const core = snapshot.state.core;
  return active !== null
    && active.project_id === project.project_id
    && active.profile_id === project.profile_id
    && active.project_etag === project.etag
    && active.connection_state === "ready"
    && core.profile_id === project.profile_id
    && core.active_tunnel
    && core.core !== null
    && ["online", "degraded"].includes(core.state);
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

function artifactTypeLabel(type: string): string {
  const labels: Record<string, string> = { text_memory: "Text memory", skill_bundle: "Skills", agent_system: "Agent guidance", parametric_memory: "Parametric memory" };
  return labels[type] ?? type.replaceAll("_", " ");
}

function sessionTitle(run: RunV1, runs: readonly RunV1[]): string {
  const chronological = [...runs].sort((left, right) => compareTimestampAndId(left.created_at, left.id, right.created_at, right.id));
  return `Session ${Math.max(1, chronological.findIndex((item) => item.id === run.id) + 1)}`;
}

function stableRunOrder(runs: readonly RunV1[]): RunV1[] {
  return [...runs].sort((left, right) => {
    const time = Date.parse(right.updated_at) - Date.parse(left.updated_at);
    return time || left.id.localeCompare(right.id);
  });
}

function compareTimestampAndId(leftTime: string, leftId: string, rightTime: string, rightId: string): number {
  const time = Date.parse(leftTime) - Date.parse(rightTime);
  return time || leftId.localeCompare(rightId);
}

function formatTime(timestamp: string): string {
  return new Intl.DateTimeFormat("en", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "UTC" }).format(new Date(timestamp));
}

function isTerminal(state: RunV1["status"]): boolean {
  return state === "succeeded" || state === "failed" || state === "cancelled";
}

function canReadmitRun(
  error: ApiErrorV1,
  snapshot: DesktopProductSnapshot | null,
  projectId: string,
): boolean {
  const coreProjectId = snapshot?.projects.find((project) => project.project_id === projectId)?.remote?.core_project_id;
  return error.http_status === 409
    && error.category === "run"
    && error.code === "run_admission_conflict"
    && error.retryable
    && error.repair_action === "openevo_can_retry"
    && snapshot?.stream.status === "fresh"
    && coreProjectId !== undefined
    && !snapshot.runs.some((run) => run.project_id === coreProjectId && !isTerminal(run.status));
}

function requestPreconditionChanged(
  result: ActionAttemptResult,
  requestEpoch: number,
  resource: { readonly kind: "profile" | "project"; readonly id: string; readonly etag: string } | null,
): boolean {
  if (!(result.error instanceof DesktopApiError) || ![409, 412].includes(result.error.apiError.http_status)) return false;
  const refreshed = result.refreshedSnapshot;
  if (!refreshed || refreshed.stream.status !== "fresh") return false;
  if (!resource) return refreshed.stream.epoch !== requestEpoch;
  const current = resource.kind === "profile"
    ? refreshed.profiles.find((profile) => profile.profile_id === resource.id)
    : refreshed.projects.find((project) => project.project_id === resource.id);
  return refreshed.stream.epoch !== requestEpoch || current?.etag !== resource.etag;
}

function userMessage(error: unknown): string {
  if (error instanceof DesktopProductProviderUnavailableError) return "The local Desktop service is not ready. Restart OpenEvo Desktop and try again.";
  if (error instanceof DesktopApiError) return error.apiError.message;
  if (error instanceof DesktopProductUserError) return error.userMessage;
  return "The request could not be completed.";
}

function isWorkspaceSelectionCancelled(error: unknown): boolean {
  return typeof error === "object"
    && error !== null
    && "code" in error
    && error.code === "workspace_selection_cancelled";
}

function projectCapability(
  snapshot: DesktopProductSnapshot,
  project: ProjectV1 | null,
): DesktopProductSnapshot["capability"] {
  const state = snapshot.capability;
  if (!project || !state) return null;
  return state.projectId === project.project_id && state.executionMode === project.execution.mode ? state : null;
}

function readyCapabilities(
  state: DesktopProductSnapshot["capability"],
  project: ProjectV1 | null,
): EvolutionCapabilitiesV1 | null {
  if (!project || !state || state.status !== "ready") return null;
  return state.value.project_id === project.project_id
    && capabilityExecutionMode(state.value.capabilities) === project.execution.mode
    ? state.value.capabilities
    : null;
}

function capabilityExecutionMode(capabilities: EvolutionCapabilitiesV1): ProjectV1["execution"]["mode"] {
  return capabilities.evaluated_profile.execution_mode === "self_deployed" ? "self-deployed" : "codex_subscription_transcript";
}

function executionModeCapability(
  capabilities: ExecutionModeCapabilitiesV1,
  mode: ProjectV1["execution"]["mode"],
): ExecutionModeCapabilityV1 {
  const capability = capabilities.modes.find((item) => item.mode === mode);
  if (!capability) throw new Error("Desktop execution mode capability is missing.");
  return capability;
}

function firstSupportedExecutionMode(
  capabilities: ExecutionModeCapabilitiesV1,
): ExecutionModeCapabilityV1 | null {
  return capabilities.modes.find((item) => item.support_state === "supported") ?? null;
}

function parseCapabilityJsonObject(value: string): OpenEvoJsonObject {
  const parsed: unknown = JSON.parse(value);
  if (!isOpenEvoJsonObject(parsed)) throw new Error("Remote capability JSON must be an object.");
  return parsed;
}

function isOpenEvoJsonObject(value: unknown): value is OpenEvoJsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    && Object.values(value).every(isOpenEvoJsonValue);
}

function isOpenEvoJsonValue(value: unknown): boolean {
  return value === null || typeof value === "string" || typeof value === "boolean" || (typeof value === "number" && Number.isFinite(value))
    || (Array.isArray(value) && value.every(isOpenEvoJsonValue)) || isOpenEvoJsonObject(value);
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
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const requestClose = useCallback(() => {
    if (dirty) {
      returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      setConfirming(true);
      return;
    }
    onClose();
  }, [dirty, onClose]);
  const keepEditing = useCallback(() => {
    setConfirming(false);
    queueMicrotask(() => returnFocusRef.current?.focus());
  }, []);
  const discard = useCallback(() => {
    setConfirming(false);
    onClose();
  }, [onClose]);
  return {
    confirming,
    requestClose,
    keepEditing,
    discard,
  };
}

function handleTablistKeyDown(event: React.KeyboardEvent<HTMLElement>) {
  if (event.key === "Enter" || event.key === " ") {
    const active = document.activeElement;
    if (active instanceof HTMLButtonElement && active.getAttribute("role") === "tab" && event.currentTarget.contains(active)) {
      event.preventDefault();
      active.click();
    }
    return;
  }
  const direction = event.key === "ArrowRight"
    ? 1
    : event.key === "ArrowLeft"
      ? -1
      : 0;
  if (direction === 0 && event.key !== "Home" && event.key !== "End") return;
  const tabs = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]:not(:disabled)'));
  if (tabs.length === 0) return;
  const currentIndex = tabs.findIndex((tab) => tab === document.activeElement);
  const nextIndex = event.key === "Home"
    ? 0
    : event.key === "End"
      ? tabs.length - 1
      : (Math.max(0, currentIndex) + direction + tabs.length) % tabs.length;
  event.preventDefault();
  tabs[nextIndex]?.focus();
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
  return profile.authentication_kind === "ssh_agent"
    ? null
    : "Switch this remote workspace to SSH agent authentication before connecting.";
}
