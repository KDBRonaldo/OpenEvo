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
  DiagnosticReportV1,
  ExecutionModeCapabilitiesV1,
  ExecutionModeCapabilityV1,
  LogEntryV1,
  LocalOperationV1,
  OperationV1,
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
  DesktopProductAmbiguousMutationError,
  DesktopProductProviderUnavailableError,
  DesktopProductUserError,
  type DesktopProductProvider,
  type DesktopProductSnapshot,
  type ProductMutationIntent,
  type ProductArtifactCollectionState,
  type ProductResourceMutationIntent,
  type ProductRefreshResult,
  unavailableDesktopProductProvider,
  type ProductRunRetryRecovery,
} from "./provider";
import { MethodConfigEditor, methodConfigErrors } from "./MethodConfigEditor";
import { retryRunProvesSingleAppend } from "./runRetryRecovery";
import { SampleScientificProjectView } from "./ScientificProjectSample";
import { SAMPLE_SCIENTIFIC_PROJECT } from "./scientificProjectSampleData";
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
const SYSTEM_OPERATION_REFRESH_INTERVAL_MS = 750;
const SYSTEM_OPERATION_REFRESH_LIMIT = 480;
const PENDING_RETRY_REFRESH_LIMIT = 60;
const REQUIRED_EVOLUTION_TARGETS = ["text_memory", "skill_bundle", "agent_system"] as const;
const SAMPLE_PROJECT_OPTION_KEY = "sample";
const PROJECT_OPTION_PREFIX = "project:";
const WORKSPACE_OPTION_PREFIX = "workspace:";

type Workspace = "research" | "evolution" | "system";
type ProjectSelection =
  | { readonly kind: "sample" }
  | { readonly kind: "project"; readonly projectId: string }
  | { readonly kind: "workspace"; readonly profileId: string };
type RemoteWorkspaceDrawerMode =
  | { readonly kind: "create" }
  | { readonly kind: "edit"; readonly profileId: string };
type AsyncState = "idle" | "working";
type ActionRecovery = { readonly kind: "readmit_run"; readonly projectId: string } | null;
type ActionAttemptResult = {
  readonly saved: boolean;
  readonly error: unknown | null;
  readonly refreshedSnapshot: DesktopProductSnapshot | null;
  readonly errorOwner: number;
};
type ActionErrorState = {
  readonly owner: number;
  readonly message: string;
  readonly selectionIdentity: string;
};
type PendingProjectActivation = {
  readonly projectId: string;
  readonly activationActionId: string;
};
type PendingRunRetry = {
  readonly runId: string;
  readonly projectId: string;
  readonly intent: ProductResourceMutationIntent;
  readonly errorOwner: number;
  readonly originalRun: RunV1;
  acceptedRun: RunV1 | null;
  transportSettled: boolean;
  reconciled: boolean;
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
    | {
        readonly kind: "create";
        readonly intent: ProductMutationIntent;
        readonly confirmedProfileId: string | null;
      }
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
  onInitialSnapshotFailed?: () => void;
  onReady?: () => void;
}

export function DesktopProductApp({
  provider = unavailableDesktopProductProvider,
  onInitialSnapshotFailed,
  onReady,
}: DesktopProductAppProps) {
  const [snapshot, setSnapshot] = useState<DesktopProductSnapshot | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [workspace, setWorkspace] = useState<Workspace>("research");
  const [projectSelection, setProjectSelection] = useState<ProjectSelection | null>(null);
  const selectionIdentity = projectSelectionIdentity(projectSelection);
  const selectionIdentityRef = useRef(selectionIdentity);
  selectionIdentityRef.current = selectionIdentity;
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [connectionSettingsMode, setConnectionSettingsMode] =
    useState<RemoteWorkspaceDrawerMode | null>(null);
  const [creatingProject, setCreatingProject] = useState(false);
  const [actionState, setActionState] = useState<AsyncState>("idle");
  const [actionError, setActionError] = useState<ActionErrorState | null>(null);
  const [actionRecovery, setActionRecovery] = useState<ActionRecovery>(null);
  const [localMaintenanceBusy, setLocalMaintenanceBusy] = useState(false);
  const readyReported = useRef(false);
  const authoritativeSnapshotPublished = useRef(false);
  const initialSnapshotFailureReported = useRef(false);
  const actionErrorGeneration = useRef(0);
  const actionStateGeneration = useRef(0);
  const pendingProjectActivation = useRef<PendingProjectActivation | null>(null);
  const pendingRunRetry = useRef<PendingRunRetry | null>(null);
  const [pendingRetryPoll, setPendingRetryPoll] = useState<PendingRunRetry | null>(null);
  const recoveredProjectSetup = useRef<string | null>(null);
  const [cancellingOperation, setCancellingOperation] = useState(false);
  const refreshCoordinator = useRef<SnapshotRefreshCoordinator | null>(null);
  if (refreshCoordinator.current === null) refreshCoordinator.current = new SnapshotRefreshCoordinator();

  const reserveActionErrorOwner = useCallback((): number => {
    const owner = actionErrorGeneration.current + 1;
    actionErrorGeneration.current = owner;
    setActionError(null);
    setActionRecovery(null);
    return owner;
  }, []);

  const clearActionError = useCallback((owner: number): void => {
    setActionError((current) => current?.owner === owner ? null : current);
  }, []);

  const clearPendingRetry = useCallback((pending: PendingRunRetry): void => {
    if (pendingRunRetry.current !== pending) return;
    pending.reconciled = true;
    pendingRunRetry.current = null;
    setPendingRetryPoll((current) => current === pending ? null : current);
    clearActionError(pending.errorOwner);
  }, [clearActionError]);

  const abandonPendingRetry = useCallback((pending: PendingRunRetry): void => {
    if (pendingRunRetry.current !== pending) return;
    pendingRunRetry.current = null;
    setPendingRetryPoll((current) => current === pending ? null : current);
  }, []);

  const reportInitialSnapshotFailure = useCallback((): void => {
    if (authoritativeSnapshotPublished.current || initialSnapshotFailureReported.current) return;
    initialSnapshotFailureReported.current = true;
    try {
      onInitialSnapshotFailed?.();
    } catch {
      // Bootstrap diagnostics cannot alter the product error state.
    }
  }, [onInitialSnapshotFailed]);

  const publishRefresh = useCallback((publication: SnapshotRefreshPublication): void => {
    if (publication.kind === "pending") {
      setSnapshot((current) => current ? {
        ...current,
        stream: { status: "stale", epoch: publication.epoch ?? current.stream.epoch, reason: "refresh_pending" },
      } : current);
      return;
    }
    if (publication.kind === "rejected") {
      reportInitialSnapshotFailure();
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
    let pendingRetry = pendingRunRetry.current;
    let recoveredRetry: ProductRunRetryRecovery | null = null;
    let retryRecoveryReadFailed = false;
    let retryRecoveryFailure: unknown = null;
    try {
      recoveredRetry = provider.getRunRetryRecovery?.() ?? null;
    } catch (error) {
      retryRecoveryReadFailed = true;
      retryRecoveryFailure = error;
    }
    if (retryRecoveryReadFailed) {
      if (pendingRetry) {
        abandonPendingRetry(pendingRetry);
        pendingRetry = null;
      }
      const errorOwner = actionErrorGeneration.current + 1;
      actionErrorGeneration.current = errorOwner;
      setActionRecovery(null);
      setPendingRetryPoll(null);
      setActionError({
        owner: errorOwner,
        message: userMessage(retryRecoveryFailure),
        selectionIdentity: selectionIdentityRef.current,
      });
    }
    if (result.status !== "fresh") {
      if (result.status === "error") reportInitialSnapshotFailure();
      setSnapshot((current) => current ? { ...current, stream: result.stream } : current);
      if (result.status === "error") {
        setLoadError(userMessage(
          retryRecoveryReadFailed ? retryRecoveryFailure : result.stream.error,
        ));
      }
      return;
    }
    let next = result.snapshot;
    if (!retryRecoveryReadFailed && !pendingRetry && recoveredRetry) {
      const errorOwner = actionErrorGeneration.current + 1;
      actionErrorGeneration.current = errorOwner;
      pendingRetry = pendingRetryFromRecovery(recoveredRetry, errorOwner);
      pendingRunRetry.current = pendingRetry;
      setActionRecovery(null);
      if (!pendingRetry.acceptedRun) {
        setActionError({
          owner: errorOwner,
          message: "The retry outcome is not yet confirmed. OpenEvo will keep checking the remote session.",
          selectionIdentity: selectionIdentityRef.current,
        });
        setPendingRetryPoll(pendingRetry);
      }
    }
    if (pendingRetry?.transportSettled && retryAdvancedInSnapshot(next, pendingRetry)) {
      clearPendingRetry(pendingRetry);
    } else if (pendingRetry?.acceptedRun) {
      next = mergeAuthoritativeRetryRun(next, pendingRetry.acceptedRun, pendingRetry);
    }
    authoritativeSnapshotPublished.current = true;
    setSnapshot(next);
    setLoadError(null);
    setProjectSelection((current) => {
      if (current?.kind === "sample") return current;
      if (
        current?.kind === "project"
        && next.projects.some((project) => project.project_id === current.projectId)
      ) {
        return current;
      }
      if (
        current?.kind === "workspace"
        && next.profiles.some((profile) => profile.profile_id === current.profileId)
      ) {
        return current;
      }
      const projectId = next.state.active_project?.project_id ?? next.projects[0]?.project_id;
      if (projectId) return { kind: "project", projectId };
      const profileId = next.profiles[0]?.profile_id;
      return profileId ? { kind: "workspace", profileId } : { kind: "sample" };
    });
  }, [abandonPendingRetry, clearPendingRetry, provider, reportInitialSnapshotFailure]);

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

  const viewingSample = projectSelection?.kind === "sample";
  const selectedProjectId =
    projectSelection?.kind === "project" ? projectSelection.projectId : null;
  const selectedWorkspaceProfileId =
    projectSelection?.kind === "workspace" ? projectSelection.profileId : null;
  const project = useMemo(() => {
    if (viewingSample || selectedWorkspaceProfileId) return null;
    return snapshot?.projects.find((item) => item.project_id === selectedProjectId)
      ?? snapshot?.projects[0]
      ?? null;
  }, [selectedProjectId, selectedWorkspaceProfileId, snapshot, viewingSample]);
  const profile = viewingSample
    ? null
    : project
      ? snapshot?.profiles.find((item) => item.profile_id === project.profile_id) ?? null
      : selectedWorkspaceProfileId
        ? snapshot?.profiles.find((item) => item.profile_id === selectedWorkspaceProfileId) ?? null
        : snapshot?.profiles[0] ?? null;
  const connectionSettingsProfile = connectionSettingsMode?.kind === "edit"
    ? snapshot?.profiles.find((item) => item.profile_id === connectionSettingsMode.profileId)
      ?? null
    : null;
  useEffect(() => {
    if (
      connectionSettingsMode?.kind === "edit"
      && snapshot
      && !snapshot.profiles.some(
        (item) => item.profile_id === connectionSettingsMode.profileId,
      )
    ) {
      setConnectionSettingsMode(null);
    }
  }, [connectionSettingsMode, snapshot]);
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

  const act = useCallback(async (
    action: () => Promise<unknown>,
    conflictRecovery: ActionRecovery = null,
    refreshOnUnknown = false,
    reservedErrorOwner?: number,
  ): Promise<ActionAttemptResult> => {
    const actionSelectionIdentity = selectionIdentityRef.current;
    const errorOwner = reservedErrorOwner ?? reserveActionErrorOwner();
    const stateOwner = actionStateGeneration.current + 1;
    actionStateGeneration.current = stateOwner;
    setActionState("working");
    setActionRecovery(null);
    try {
      await action();
      const refreshedSnapshot = await refresh("mutation");
      return { saved: true, error: null, refreshedSnapshot, errorOwner };
    } catch (error) {
      let refreshedSnapshot: DesktopProductSnapshot | null = null;
      if (error instanceof DesktopApiError && [409, 410, 412].includes(error.apiError.http_status)) {
        if (error.apiError.http_status === 410) {
          setSnapshot((current) => current ? { ...current, stream: { status: "cursor_reset", epoch: current.stream.epoch, resumeFromEventId: null } } : current);
        }
        refreshedSnapshot = await refresh("mutation");
        if (conflictRecovery
          && actionErrorGeneration.current === errorOwner
          && canReadmitRun(error.apiError, refreshedSnapshot, conflictRecovery.projectId)) {
          setActionRecovery(conflictRecovery);
        }
      } else if (refreshOnUnknown) {
        refreshedSnapshot = await refresh("mutation");
      }
      if (actionErrorGeneration.current === errorOwner) {
        setActionError({
          owner: errorOwner,
          message: userMessage(error),
          selectionIdentity: actionSelectionIdentity,
        });
      }
      return { saved: false, error, refreshedSnapshot, errorOwner };
    } finally {
      if (actionStateGeneration.current === stateOwner) setActionState("idle");
    }
  }, [refresh, reserveActionErrorOwner]);

  useActiveRunPolling(runPollingIdentity, refresh);

  useEffect(() => {
    if (!pendingRetryPoll) return;
    let active = true;
    let attempts = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const poll = async () => {
      if (!active || pendingRunRetry.current !== pendingRetryPoll || attempts >= PENDING_RETRY_REFRESH_LIMIT) return;
      attempts += 1;
      await refresh("poll", () => active && pendingRunRetry.current === pendingRetryPoll);
      if (active && pendingRunRetry.current === pendingRetryPoll && attempts < PENDING_RETRY_REFRESH_LIMIT) {
        timer = setTimeout(() => void poll(), ACTIVE_RUN_REFRESH_INTERVAL_MS);
      }
    };
    timer = setTimeout(() => void poll(), ACTIVE_RUN_REFRESH_INTERVAL_MS);
    return () => {
      active = false;
      if (timer !== null) clearTimeout(timer);
    };
  }, [pendingRetryPoll, provider, refresh]);

  useEffect(() => {
    if (!snapshot) return;
    const setupProjectId = snapshot.activeOperation?.operation_kind === "project_activate"
      ? snapshot.activeOperation.resource.resource_id
      : snapshot.state.active_project?.project_id ?? null;
    const setupProject = snapshot.projects.find((item) => item.project_id === setupProjectId);
    if (!setupProject || setupProject.evolution_configuration_state !== "pending") return;
    if (recoveredProjectSetup.current === setupProject.project_id) return;
    recoveredProjectSetup.current = setupProject.project_id;
    setProjectSelection({ kind: "project", projectId: setupProject.project_id });
    setCreatingProject(false);
    setSettingsOpen(true);
  }, [snapshot]);

  useEffect(() => {
    if (!snapshot || readyReported.current) return;
    readyReported.current = true;
    onReady?.();
  }, [onReady, snapshot]);

  if (!snapshot) {
    if (loadError) {
      return (
        <InitialSnapshotFailure
          workspace={workspace}
          error={loadError}
          onWorkspaceChange={setWorkspace}
          onRetry={() => void refresh("manual")}
        />
      );
    }
    return (
      <div className="product-boot" data-testid="product-loading">
        <div className="product-loading-row"><LoaderCircle className="spin" size={18} /> Loading workspace...</div>
      </div>
    );
  }

  const connection = snapshot.state.core;
  const displayedConnectionState = profile && connection.profile_id !== profile.profile_id
    ? "disconnected"
    : connection.state;
  const settingsProject = creatingProject ? null : project;
  const settingsFormIdentity = settingsProject ? `project:${settingsProject.project_id}` : "create";
  const settingsCapability = projectCapability(snapshot, settingsProject);
  const projectServices = projectSessionReady ? snapshot.services : [];
  const servicesNeedAttention = projectSessionReady
    && (projectServices.length === 0 || projectServices.some((service) => service.status !== "running"));
  const profilesWithoutProjects = snapshot.profiles.filter(
    (candidate) => !snapshot.projects.some(
      (candidateProject) => candidateProject.profile_id === candidate.profile_id,
    ),
  );
  const selectedOperation = snapshot.activeOperation
    && operationBelongsToSelection(snapshot.activeOperation, project, profile)
    ? snapshot.activeOperation
    : null;
  const activeOperationRunning = snapshot.activeOperation !== null
    && !["succeeded", "failed", "cancelled"].includes(snapshot.activeOperation.state);
  const recoveredMaintenanceBusy = activeOperationRunning
    && selectedOperation !== null
    && ["project_doctor", "project_repair"].includes(selectedOperation.operation_kind);
  const maintenanceBusy = localMaintenanceBusy || recoveredMaintenanceBusy;
  const canCreateProject = profile?.connection_state === "connected" && !maintenanceBusy;
  const selectedOperationCanCancel = selectedOperation !== null
    && !["project_doctor", "project_repair"].includes(selectedOperation.operation_kind);
  const lifecycleMutationBusy =
    maintenanceBusy
    || actionState === "working"
    || (activeOperationRunning && selectedOperation === null);
  const visibleActionError =
    actionError?.selectionIdentity === selectionIdentity ? actionError : null;
  const coreCanActivateProject = connection.profile_id === profile?.profile_id
    && (displayedConnectionState === "online"
      || displayedConnectionState === "degraded"
      || (connection.state === "offline" && connection.failure?.code === "core_not_started"));
  const canShowActivation = coreCanActivateProject
    && profile?.connection_state === "connected"
    && snapshot.state.contract.compatible;
  const activationReason = maintenanceBusy
    ? "Wait for System maintenance to finish."
    : getProjectActivationReason(snapshot, project, profile, actionState);
  const startReason = maintenanceBusy
    ? "Wait for System maintenance to finish."
    : getStartReason(snapshot, project, profile, projectServices, activeRun, actionState);
  const canStart = startReason === null;

  const cancelActiveOperation = async () => {
    const operation = selectedOperation;
    if (!operation || cancellingOperation || maintenanceBusy) return;
    const errorOwner = reserveActionErrorOwner();
    setCancellingOperation(true);
    try {
      await provider.cancelOperation(
        operation.operation_id,
        resourceIntent(snapshot, operation.etag),
      );
      await refresh("mutation");
    } catch (error) {
      if (actionErrorGeneration.current === errorOwner) {
        setActionError({
          owner: errorOwner,
          message: userMessage(error),
          selectionIdentity,
        });
      }
      await refresh("mutation");
    } finally {
      setCancellingOperation(false);
    }
  };

  const retryFailedRun = (run: RunV1): void => {
    if (actionState === "working" || maintenanceBusy) return;
    const retryRun = provider.retryRun;
    if (!retryRun) {
      void act(() => Promise.reject(new DesktopProductProviderUnavailableError()));
      return;
    }
    const existing = pendingRunRetry.current;
    const errorOwner = reserveActionErrorOwner();
    const sameUnprovenRetry = existing?.runId === run.id && !retryAdvancedRun(run, existing);
    const intent = sameUnprovenRetry
      ? existing.intent
      : resourceIntent(snapshot, run.etag);
    const pending: PendingRunRetry = {
      runId: run.id,
      projectId: sameUnprovenRetry ? existing.projectId : run.project_id,
      intent,
      errorOwner,
      originalRun: sameUnprovenRetry ? existing.originalRun : run,
      acceptedRun: null,
      transportSettled: false,
      reconciled: false,
    };
    pendingRunRetry.current = pending;
    setPendingRetryPoll(null);
    let retryResponse: RunV1 | null = null;
    void act(
      async () => {
        try {
          retryResponse = await retryRun.call(provider, run.id, pending.intent);
          pending.transportSettled = true;
          return retryResponse;
        } catch (error) {
          pending.transportSettled = true;
          if (!isAmbiguousRetryOutcome(error)) abandonPendingRetry(pending);
          throw error;
        }
      },
      null,
      true,
      errorOwner,
    ).then((result) => {
      if (pendingRunRetry.current !== pending) {
        if (pending.reconciled
          && (result.error === null || isAmbiguousRetryOutcome(result.error))) {
          clearActionError(errorOwner);
        }
        return;
      }
      const acceptedRetryResponse = retryResponse;
      if (acceptedRetryResponse && retryAdvancedRun(acceptedRetryResponse, pending)) {
        pending.acceptedRun = acceptedRetryResponse;
        setSnapshot((current) => current ? mergeAuthoritativeRetryRun(current, acceptedRetryResponse, pending) : current);
        setPendingRetryPoll(null);
        clearActionError(pending.errorOwner);
      } else if (result.refreshedSnapshot && retryAdvancedInSnapshot(result.refreshedSnapshot, pending)) {
        clearPendingRetry(pending);
      } else if (result.error === null || isAmbiguousRetryOutcome(result.error)) {
        setPendingRetryPoll(pending);
      } else {
        abandonPendingRetry(pending);
      }
    });
  };

  return (
    <div
      className="product-shell"
      data-provider-kind={provider.providerKind}
      data-system-maintenance-available={String(provider.systemMaintenanceAvailable)}
    >
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
          <div className="sidebar-foot-label">Current Project Head</div>
          <div className="sidebar-revision">
            <CircleDot size={15} />
            <span>{viewingSample ? `Project Head ${SAMPLE_SCIENTIFIC_PROJECT.activeProjectHeadGeneration}` : revisionLabel(project, projectRuns)}</span>
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
                value={viewingSample
                  ? SAMPLE_PROJECT_OPTION_KEY
                  : project
                    ? projectOptionKey(project.project_id)
                    : profile
                      ? workspaceOptionKey(profile.profile_id)
                      : ""}
                onChange={(event) => {
                  const key = event.target.value;
                  setActionError(null);
                  setActionRecovery(null);
                  if (key === SAMPLE_PROJECT_OPTION_KEY) {
                    setProjectSelection({ kind: "sample" });
                    return;
                  }
                  const selected = snapshot.projects.find(
                    (item) => projectOptionKey(item.project_id) === key,
                  );
                  if (selected) {
                    setProjectSelection({ kind: "project", projectId: selected.project_id });
                    return;
                  }
                  const selectedProfile = profilesWithoutProjects.find(
                    (item) => workspaceOptionKey(item.profile_id) === key,
                  );
                  setProjectSelection(selectedProfile
                    ? { kind: "workspace", profileId: selectedProfile.profile_id }
                    : null);
                }}
                disabled={lifecycleMutationBusy}
              >
                <optgroup label="内置示例">
                  <option value={SAMPLE_PROJECT_OPTION_KEY}>[只读] {SAMPLE_SCIENTIFIC_PROJECT.name}</option>
                </optgroup>
                {snapshot.projects.length > 0 ? (
                  <optgroup label="我的项目">
                    {snapshot.projects.map((item) => (
                      <option
                        key={item.project_id}
                        value={projectOptionKey(item.project_id)}
                        data-project-id={item.project_id}
                      >
                        {item.name}
                      </option>
                    ))}
                  </optgroup>
                ) : null}
                {profilesWithoutProjects.length > 0 ? (
                  <optgroup label="待创建项目的工作区">
                    {profilesWithoutProjects.map((item) => (
                      <option
                        key={item.profile_id}
                        value={workspaceOptionKey(item.profile_id)}
                        data-profile-id={item.profile_id}
                      >
                        {item.name} · 尚无项目
                      </option>
                    ))}
                  </optgroup>
                ) : null}
              </select>
              <ChevronDown size={15} aria-hidden="true" />
            </div>
            <IconButton label="Create project" onClick={() => { setCreatingProject(true); setSettingsOpen(true); }} disabled={!canCreateProject}><Plus size={17} /></IconButton>
          </div>
          <div className="topbar-actions">
            {viewingSample
              ? <span className="sample-topbar-badge"><ShieldCheck size={14} /> 示例 · 未连接</span>
              : <ConnectionBadge state={displayedConnectionState} profileName={profile?.name ?? "Remote workspace"} />}
            <IconButton
              label="Remote workspace settings"
              onClick={() => setConnectionSettingsMode(
                viewingSample || !profile
                  ? { kind: "create" }
                  : { kind: "edit", profileId: profile.profile_id },
              )}
              disabled={maintenanceBusy}
            >
              <PanelLeft size={17} />
            </IconButton>
            <IconButton label="Project settings" onClick={() => { setCreatingProject(false); setSettingsOpen(true); }} disabled={viewingSample || !project || maintenanceBusy}><Settings size={17} /></IconButton>
          </div>
        </header>

        <main className="product-main">
          {viewingSample ? (
            <SampleScientificProjectView
              workspace={workspace}
              onConnectRemote={() => setConnectionSettingsMode({ kind: "create" })}
            />
          ) : null}
          {!connectionSettingsMode && visibleActionError ? <InlineNotice
            tone="error"
            title="Action could not be completed"
            detail={visibleActionError.message}
            onDismiss={() => { setActionError(null); setActionRecovery(null); }}
            actionLabel={actionRecovery?.kind === "readmit_run" ? "Re-admit session" : undefined}
            onAction={actionRecovery?.kind === "readmit_run" && project && actionRecovery.projectId === project.project_id
              ? () => void act(() => provider.startRun({ ...resourceIntent(snapshot, project.etag), projectId: project.project_id }), actionRecovery)
              : undefined}
          /> : null}
          {!viewingSample && selectedOperation && !["succeeded", "failed", "cancelled"].includes(selectedOperation.state) ? (
            <section className="operation-cancel-bar" aria-live="polite">
              <div><LoaderCircle className="spin" size={17} /><span>{selectedOperation.progress?.label ?? "Local operation in progress"}</span></div>
              {selectedOperationCanCancel ? (
                <button className="secondary-button" type="button" onClick={() => void cancelActiveOperation()} disabled={cancellingOperation || maintenanceBusy}><Square size={14} /> {cancellingOperation ? "Cancelling..." : "Cancel operation"}</button>
              ) : null}
            </section>
          ) : null}
          {!viewingSample ? (
            <ConnectionGate
              snapshot={snapshot}
              profile={profile}
              operation={selectedOperation}
              busy={lifecycleMutationBusy}
              onConnect={(selectedProfile) => void act(() => provider.connectProfile(selectedProfile.profile_id, resourceIntent(snapshot, selectedProfile.etag)))}
              onAccept={(profileId) => {
                const review = snapshot.state.core.host_key_review;
                if (review && profile) void act(() => provider.acceptHostKey(profileId, review, resourceIntent(snapshot, profile.etag)));
              }}
              onSetup={() => setConnectionSettingsMode(
                profile
                  ? { kind: "edit", profileId: profile.profile_id }
                  : { kind: "create" },
              )}
            />
          ) : null}
          {!viewingSample && project && !projectSessionReady && canShowActivation ? (
            <ProjectActivationGate
              project={project}
              busy={lifecycleMutationBusy}
              disabledReason={activationReason}
              onActivate={() => void act(() => provider.activateProject(project.project_id, resourceIntent(snapshot, project.etag)))}
            />
          ) : null}

          {!viewingSample && workspace === "research" && servicesNeedAttention ? (
            <div className="service-health-notice" role="status">
              <AlertCircle size={17} />
              <div><strong>Remote services need attention</strong><span>Review the remote environment before starting another session.</span></div>
              <button type="button" className="secondary-button" onClick={() => setWorkspace("system")}><Activity size={15} /> Open System</button>
            </div>
          ) : null}

          {!viewingSample && workspace === "research" ? (
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
              busy={lifecycleMutationBusy}
              onStart={() => project && !maintenanceBusy && void act(() => provider.startRun({ ...resourceIntent(snapshot, project.etag), projectId: project.project_id }), { kind: "readmit_run", projectId: project.project_id })}
              onRetry={retryFailedRun}
              onCancel={() => activeRun && void act(() => provider.cancelRun(activeRun.id, resourceIntent(snapshot, activeRun.etag)))}
              onOpenSettings={() => { setCreatingProject(false); setSettingsOpen(true); }}
              onOpenConnection={() => setConnectionSettingsMode(
                profile
                  ? { kind: "edit", profileId: profile.profile_id }
                  : { kind: "create" },
              )}
              onOpenEvolution={() => setWorkspace("evolution")}
              onOpenSystem={() => setWorkspace("system")}
              onRefresh={() => void refresh("manual")}
            />
          ) : null}
          {!viewingSample && workspace === "evolution" ? (
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
          {!viewingSample ? (
            <div hidden={workspace !== "system"}>
              <SystemWorkspace
                snapshot={snapshot}
                project={project}
                profile={profile}
                services={projectServices}
                maintenanceAvailable={provider.systemMaintenanceAvailable}
                projectSessionReady={projectSessionReady}
                busy={lifecycleMutationBusy}
                maintenanceBusy={maintenanceBusy}
                provider={provider}
                onConnect={() => profile && void act(() => provider.connectProfile(profile.profile_id, resourceIntent(snapshot, profile.etag)))}
                onConfigure={() => setConnectionSettingsMode(
                  profile
                    ? { kind: "edit", profileId: profile.profile_id }
                    : { kind: "create" },
                )}
                onRefresh={async () => {
                  await refresh("manual");
                }}
                onMaintenanceBusyChange={setLocalMaintenanceBusy}
              />
            </div>
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
          busy={lifecycleMutationBusy}
          onClose={() => {
            const pending = pendingProjectActivation.current;
            if (pending) {
              setProjectSelection({ kind: "project", projectId: pending.projectId });
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
                setProjectSelection({ kind: "project", projectId: current.project_id });
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
      {connectionSettingsMode
      && (connectionSettingsMode.kind === "create" || connectionSettingsProfile) ? (
        <RemoteWorkspaceDrawer
          profile={connectionSettingsProfile}
          observedProfiles={snapshot.profiles}
          streamEpoch={snapshot.stream.status === "fresh" ? snapshot.stream.epoch : null}
          busy={lifecycleMutationBusy}
          errorMessage={visibleActionError?.message ?? null}
          onDismissError={() => { setActionError(null); setActionRecovery(null); }}
          onClose={() => setConnectionSettingsMode(null)}
          createSaveIntent={(input) => profileSaveIntent(
            snapshot,
            connectionSettingsMode,
            connectionSettingsProfile,
            input,
          )}
          onSave={async (intent) => {
            const route = intent.route;
            const returnedProfile = { current: null as RemoteProfileV1 | null };
            const result = await act(async () => {
              returnedProfile.current = route.kind === "create"
                ? await provider.createProfile(intent.input, route.intent)
                : await provider.updateProfile(route.profileId, intent.input, route.intent);
            }, null, true);
            const confirmedProfileId = route.kind === "create"
              ? returnedProfile.current?.profile_id ?? route.confirmedProfileId
              : null;
            const createdProfile = confirmedProfileId
              ? matchingConfirmedProfile(
                result.refreshedSnapshot?.profiles,
                intent.canonicalPayload,
                confirmedProfileId,
              )
              : null;
            const saveConfirmed = route.kind === "create"
              ? createdProfile !== null
              : result.saved;
            if (saveConfirmed) {
              setActionError(null);
              setConnectionSettingsMode(null);
              if (createdProfile) {
                setProjectSelection({
                  kind: "workspace",
                  profileId: createdProfile.profile_id,
                });
              }
              return { saved: true, pendingIntent: null };
            }
            const requestEpoch = route.intent.streamEpoch;
            const resource = route.kind === "update"
              ? { kind: "profile" as const, id: route.profileId, etag: route.intent.etag }
              : null;
            return {
              saved: false,
              pendingIntent: requestPreconditionChanged(result, requestEpoch, resource)
                ? null
                : route.kind === "create" && confirmedProfileId
                  ? {
                      ...intent,
                      route: { ...route, confirmedProfileId },
                    }
                  : intent,
            };
          }}
          onCreateObserved={(observedProfile) => {
            setActionError(null);
            setConnectionSettingsMode(null);
            const matchingProject = snapshot.projects.find(
              (item) => item.profile_id === observedProfile.profile_id,
            );
            setProjectSelection(matchingProject
              ? { kind: "project", projectId: matchingProject.project_id }
              : { kind: "workspace", profileId: observedProfile.profile_id });
          }}
        />
      ) : null}
    </div>
  );
}

function InitialSnapshotFailure({
  workspace,
  error,
  onWorkspaceChange,
  onRetry,
}: {
  workspace: Workspace;
  error: string;
  onWorkspaceChange: (workspace: Workspace) => void;
  onRetry: () => void;
}) {
  return (
    <div className="product-shell initial-sync-shell" data-testid="initial-sync-failure">
      <aside className="product-sidebar" aria-label="Primary navigation">
        <div className="product-brand" aria-label="OpenEvo Desktop">
          <span className="product-mark"><Sparkles size={17} strokeWidth={2.2} /></span>
          <span>OpenEvo</span>
        </div>
        <nav className="product-nav">
          <NavButton icon={BookOpen} label="Research" active={workspace === "research"} onClick={() => onWorkspaceChange("research")} />
          <NavButton icon={Sparkles} label="Evolution" active={workspace === "evolution"} onClick={() => onWorkspaceChange("evolution")} />
          <NavButton icon={Activity} label="System" active={workspace === "system"} onClick={() => onWorkspaceChange("system")} />
        </nav>
        <div className="sidebar-foot">
          <div className="sidebar-foot-label">Current Project Head</div>
          <div className="sidebar-revision">
            <CircleDot size={15} />
            <span>Project Head {SAMPLE_SCIENTIFIC_PROJECT.activeProjectHeadGeneration}</span>
          </div>
        </div>
      </aside>
      <div className="product-stage">
        <header className="product-topbar">
          <div className="project-switcher-wrap">
            <label htmlFor="project-switcher">Project</label>
            <div className="project-switcher-control">
              <select id="project-switcher" value={SAMPLE_PROJECT_OPTION_KEY} disabled>
                <option value={SAMPLE_PROJECT_OPTION_KEY}>[只读] {SAMPLE_SCIENTIFIC_PROJECT.name}</option>
              </select>
              <ChevronDown size={15} aria-hidden="true" />
            </div>
            <IconButton label="Create project" onClick={() => undefined} disabled><Plus size={17} /></IconButton>
          </div>
          <div className="topbar-actions">
            <span className="sample-topbar-badge"><AlertCircle size={14} /> Sync unavailable</span>
            <IconButton label="Remote workspace settings" onClick={() => undefined} disabled><PanelLeft size={17} /></IconButton>
            <IconButton label="Project settings" onClick={() => undefined} disabled><Settings size={17} /></IconButton>
          </div>
        </header>
        <main className="product-main">
          <div className="initial-sync-notice" role="alert">
            <AlertCircle size={18} />
            <div>
              <strong>Remote projects could not be synchronized</strong>
              <span>{error} The built-in project remains available in read-only mode.</span>
            </div>
            <button type="button" className="secondary-button" onClick={onRetry}>
              <RefreshCw size={15} /> Try again
            </button>
          </div>
          <div className="initial-sync-sample">
            <SampleScientificProjectView
              workspace={workspace}
            />
          </div>
        </main>
      </div>
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
  operation,
  busy,
  onConnect,
  onAccept,
  onSetup,
}: {
  snapshot: DesktopProductSnapshot;
  profile: RemoteProfileV1 | null;
  operation: LocalOperationV1 | null;
  busy: boolean;
  onConnect: (profile: RemoteProfileV1) => void;
  onAccept: (profileId: string) => void;
  onSetup: () => void;
}) {
  const core = snapshot.state.core;
  const profileId = profile?.profile_id ?? null;
  const coreBelongsToSelectedProfile =
    profileId !== null && core.profile_id === profileId;
  if (core.state === "online" && coreBelongsToSelectedProfile) return null;
  if (core.state === "offline"
    && core.failure?.code === "core_not_started"
    && profile?.connection_state === "connected"
    && coreBelongsToSelectedProfile) return null;
  if (coreBelongsToSelectedProfile && core.state === "degraded") {
    return <InlineNotice tone="warning" title="Remote workspace needs attention" detail={core.failure?.message ?? "Open System to review service status and operation logs."} />;
  }
  if (
    coreBelongsToSelectedProfile
    && core.state === "host_key_review"
    && core.host_key_review
  ) {
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
  if (coreBelongsToSelectedProfile && isConnectionBusy(core.state)) {
    const progress = operation?.progress;
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
          <p>{credentialReason ?? (core.state === "online" ? "Connect this project's assigned workspace before activating or running it." : coreBelongsToSelectedProfile ? core.failure?.message ?? "Connect to run research sessions and inspect evolution." : "Connect this remote workspace to continue.")}</p>
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
                  <button id="artifact-content-tab" aria-controls="artifact-view-panel" type="button" role="tab" aria-selected={view === "content"} tabIndex={view === "content" ? 0 : -1} className={view === "content" ? "active" : ""} onClick={() => setView("content")}><FileText size={14} /> Content</button>
                  <button id="artifact-diff-tab" aria-controls="artifact-view-panel" type="button" role="tab" aria-selected={view === "diff"} tabIndex={view === "diff" ? 0 : -1} className={view === "diff" ? "active" : ""} onClick={() => setView("diff")}><FileDiff size={14} /> Changes</button>
                </div>
                <div id="artifact-view-panel" aria-labelledby={view === "content" ? "artifact-content-tab" : "artifact-diff-tab"} className="artifact-body" role="tabpanel">
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
  const documentIndex = document ? content.documents.findIndex((item) => item.document_id === document.document_id) : -1;
  return (
    <>
      {content.truncated ? <InlineNotice tone="warning" title="Preview is truncated" detail={`Showing ${content.documents.length} of ${content.total_documents} documents.`} /> : null}
      {content.documents.length > 1 ? (
        <div className="document-tabs" role="tablist" aria-label="Artifact documents" onKeyDown={handleTablistKeyDown}>
          {content.documents.map((item, index) => <button id={`artifact-document-tab-${index}`} aria-controls="artifact-document-panel" role="tab" aria-selected={item.document_id === document?.document_id} tabIndex={item.document_id === document?.document_id ? 0 : -1} key={item.document_id} type="button" className={item.document_id === document?.document_id ? "active" : ""} onClick={() => setDocumentId(item.document_id)}>{item.display_name}</button>)}
        </div>
      ) : null}
      {document ? <pre id="artifact-document-panel" role={content.documents.length > 1 ? "tabpanel" : undefined} aria-labelledby={content.documents.length > 1 ? `artifact-document-tab-${documentIndex}` : undefined} className="artifact-document">{document.content}</pre> : null}
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

type SystemConfirmation =
  | { readonly kind: "repair" }
  | { readonly kind: "restart"; readonly service: ServiceV1 }
  | { readonly kind: "cleanup" };

type SystemActivity =
  | {
      readonly kind: "local";
      readonly title: string;
      readonly operation: LocalOperationV1;
    }
  | {
      readonly kind: "core";
      readonly title: string;
      readonly operation: OperationV1;
    }
  | {
      readonly kind: "diagnostic";
      readonly title: string;
      readonly report: DiagnosticReportV1;
    };

function SystemWorkspace({
  snapshot,
  project,
  profile,
  services,
  maintenanceAvailable,
  projectSessionReady,
  busy,
  maintenanceBusy,
  provider,
  onConnect,
  onConfigure,
  onRefresh,
  onMaintenanceBusyChange,
}: {
  snapshot: DesktopProductSnapshot;
  project: ProjectV1 | null;
  profile: RemoteProfileV1 | null;
  services: readonly ServiceV1[];
  maintenanceAvailable: boolean;
  projectSessionReady: boolean;
  busy: boolean;
  maintenanceBusy: boolean;
  provider: DesktopProductProvider;
  onConnect: () => void;
  onConfigure: () => void;
  onRefresh: () => Promise<void>;
  onMaintenanceBusyChange: (busy: boolean) => void;
}) {
  const core = snapshot.state.core;
  const profileOwnsCore =
    profile !== null && core.profile_id === profile.profile_id;
  const displayedCoreState = profileOwnsCore ? core.state : "disconnected";
  const displayedTunnel = profileOwnsCore && core.active_tunnel;
  const readyServices = services.filter((service) => service.status === "running").length;
  const servicesNeedAttention = projectSessionReady
    && (services.length === 0 || services.some((service) => service.status !== "running"));
  const [confirmation, setConfirmation] = useState<SystemConfirmation | null>(null);
  const [activity, setActivity] = useState<SystemActivity | null>(null);
  const [lastDoctorOperation, setLastDoctorOperation] = useState<LocalOperationV1 | null>(null);
  const [maintenanceError, setMaintenanceError] = useState<string | null>(null);
  const maintenanceGeneration = useRef(0);
  const ownsMaintenanceBusy = useRef(false);
  const confirmationTrigger = useRef<HTMLElement | null>(null);
  const activityProjectId = project?.project_id ?? null;
  const activityCoreProjectId = project?.remote?.core_project_id ?? null;
  const activityRevisionGeneration =
    project?.remote?.active_revision?.generation ?? null;
  const authoritativeProjectId =
    snapshot.state.active_project?.project_id ?? null;
  const authoritativeProjectProfileId =
    snapshot.state.active_project?.profile_id ?? null;
  const authoritativeProjectEtag =
    snapshot.state.active_project?.project_etag ?? null;
  const releaseMaintenanceBusy = useCallback((): void => {
    if (!ownsMaintenanceBusy.current) return;
    ownsMaintenanceBusy.current = false;
    onMaintenanceBusyChange(false);
  }, [onMaintenanceBusyChange]);
  const beginMaintenance = (): number => {
    const generation = maintenanceGeneration.current + 1;
    maintenanceGeneration.current = generation;
    if (!ownsMaintenanceBusy.current) {
      ownsMaintenanceBusy.current = true;
      onMaintenanceBusyChange(true);
    }
    setConfirmation(null);
    setMaintenanceError(null);
    return generation;
  };
  useEffect(() => {
    maintenanceGeneration.current += 1;
    releaseMaintenanceBusy();
    setConfirmation(null);
    setActivity(null);
    setLastDoctorOperation(null);
    setMaintenanceError(null);
    return () => {
      maintenanceGeneration.current += 1;
      releaseMaintenanceBusy();
    };
  }, [
    activityCoreProjectId,
    activityProjectId,
    activityRevisionGeneration,
    authoritativeProjectEtag,
    authoritativeProjectId,
    authoritativeProjectProfileId,
    releaseMaintenanceBusy,
  ]);

  const canOperate =
    maintenanceAvailable
    && snapshot.stream.status === "fresh"
    && projectSessionReady
    && project !== null
    && !busy
    && !maintenanceBusy;
  const repairAuthority = systemRepairAuthority(lastDoctorOperation);
  const canRepair = canOperate && repairAuthority.enabled;
  useEffect(() => {
    if (!canOperate) setConfirmation(null);
  }, [canOperate]);
  const openConfirmation = (next: SystemConfirmation): void => {
    if (!canOperate) return;
    confirmationTrigger.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setConfirmation(next);
  };

  const runLocalOperation = async (
    title: string,
    start: () => Promise<LocalOperationV1>,
  ): Promise<void> => {
    const generation = beginMaintenance();
    try {
      let operation = await start();
      if (maintenanceGeneration.current !== generation) return;
      setActivity({ kind: "local", title, operation });
      if (operation.operation_kind === "project_doctor") {
        setLastDoctorOperation(operation);
      }
      for (
        let attempt = 0;
        !isLocalOperationTerminal(operation.state) && attempt < SYSTEM_OPERATION_REFRESH_LIMIT;
        attempt += 1
      ) {
        await waitForSystemRefresh();
        if (maintenanceGeneration.current !== generation) return;
        operation = await provider.getLocalOperation(operation.operation_id);
        if (maintenanceGeneration.current !== generation) return;
        setActivity({ kind: "local", title, operation });
        if (operation.operation_kind === "project_doctor") {
          setLastDoctorOperation(operation);
        }
      }
      if (!isLocalOperationTerminal(operation.state)) {
        throw new DesktopProductUserError(
          "The remote operation is still running. Refresh System to check its latest state.",
        );
      }
      await onRefresh();
      if (
        operation.operation_kind === "project_repair"
        && operation.state === "succeeded"
      ) {
        setLastDoctorOperation(null);
      }
    } catch (error) {
      if (maintenanceGeneration.current === generation) {
        setMaintenanceError(userMessage(error));
      }
    } finally {
      if (maintenanceGeneration.current === generation) {
        releaseMaintenanceBusy();
      }
    }
  };

  const runCoreOperation = async (
    title: string,
    start: () => Promise<OperationV1>,
  ): Promise<void> => {
    const generation = beginMaintenance();
    try {
      let operation = await start();
      if (maintenanceGeneration.current !== generation) return;
      setActivity({ kind: "core", title, operation });
      for (
        let attempt = 0;
        !isCoreOperationTerminal(operation.status) && attempt < SYSTEM_OPERATION_REFRESH_LIMIT;
        attempt += 1
      ) {
        await waitForSystemRefresh();
        if (maintenanceGeneration.current !== generation) return;
        operation = await provider.getCoreOperation(operation.id);
        if (maintenanceGeneration.current !== generation) return;
        setActivity({ kind: "core", title, operation });
      }
      if (!isCoreOperationTerminal(operation.status)) {
        throw new DesktopProductUserError(
          "The remote operation is still running. Refresh System to check its latest state.",
        );
      }
      await onRefresh();
    } catch (error) {
      if (maintenanceGeneration.current === generation) {
        setMaintenanceError(userMessage(error));
      }
    } finally {
      if (maintenanceGeneration.current === generation) {
        releaseMaintenanceBusy();
      }
    }
  };

  const runDiagnostics = async (): Promise<void> => {
    if (!project?.remote) return;
    const generation = beginMaintenance();
    try {
      let report = await provider.createDiagnostic(
        {
          schema_version: "1",
          scopes: ["environment", "services", "registry", "storage"],
          target: { kind: "global" },
        },
        mutationIntent(snapshot),
      );
      if (maintenanceGeneration.current !== generation) return;
      setActivity({ kind: "diagnostic", title: "Remote diagnostics", report });
      for (
        let attempt = 0;
        !isDiagnosticTerminal(report.status) && attempt < SYSTEM_OPERATION_REFRESH_LIMIT;
        attempt += 1
      ) {
        await waitForSystemRefresh();
        if (maintenanceGeneration.current !== generation) return;
        report = await provider.getDiagnostic(report.id);
        if (maintenanceGeneration.current !== generation) return;
        setActivity({ kind: "diagnostic", title: "Remote diagnostics", report });
      }
      if (!isDiagnosticTerminal(report.status)) {
        throw new DesktopProductUserError(
          "Diagnostics are still running. Refresh System to check the report.",
        );
      }
      await onRefresh();
    } catch (error) {
      if (maintenanceGeneration.current === generation) {
        setMaintenanceError(userMessage(error));
      }
    } finally {
      if (maintenanceGeneration.current === generation) {
        releaseMaintenanceBusy();
      }
    }
  };

  const confirmAction = (): void => {
    const pending = confirmation;
    if (!pending || !project || !canOperate) return;
    if (pending.kind === "repair") {
      if (!repairAuthority.enabled) return;
      void runLocalOperation(
        "Repair remote environment",
        () => provider.repairProject(
          project.project_id,
          resourceIntent(snapshot, project.etag),
        ),
      );
      return;
    }
    if (pending.kind === "restart") {
      void runCoreOperation(
        `Restart ${pending.service.display_name}`,
        () => provider.restartService(
          pending.service.id,
          resourceIntent(snapshot, pending.service.etag),
        ),
      );
      return;
    }
    void runCoreOperation(
      "Clean diagnostic history",
      () => provider.cleanupCaches(
        {
          schema_version: "1",
          scopes: ["completed_diagnostics"],
          older_than_days: 30,
        },
        mutationIntent(snapshot),
      ),
    );
  };

  return (
    <>
    <div
      className="workspace-stack"
      data-testid="system-workspace"
      inert={confirmation ? true : undefined}
      aria-hidden={confirmation ? true : undefined}
    >
      <div className="workspace-heading">
        <div><p className="eyebrow">System</p><h1>Remote environment</h1><p>Connection, service health, diagnostics, and recovery.</p></div>
        {maintenanceAvailable ? <div className="workspace-heading-actions">
          <button
            type="button"
            className="secondary-button"
            onClick={() => {
              if (!canOperate || !project) return;
              void runLocalOperation(
                "Check remote environment",
                () => provider.doctorProject(
                  project.project_id,
                  resourceIntent(snapshot, project.etag),
                ),
              );
            }}
            disabled={!canOperate}
          >
            <ShieldCheck size={15} /> Check
          </button>
          <button
            type="button"
            className="secondary-button"
            onClick={() => {
              if (canOperate) void runDiagnostics();
            }}
            disabled={!canOperate}
          >
            <FileText size={15} /> Diagnostics
          </button>
        </div> : null}
      </div>
      {maintenanceError ? (
        <InlineNotice
          tone="error"
          title="System action could not be completed"
          detail={maintenanceError}
          onDismiss={() => setMaintenanceError(null)}
        />
      ) : null}
      {activity ? <SystemActivityView activity={activity} /> : null}
      <div className="system-grid">
        <section className="product-panel connection-panel">
          <div className="panel-heading"><div><span className="panel-kicker">Connection</span><h2>{profile?.name ?? "No remote workspace"}</h2></div><StatePill state={displayedCoreState} /></div>
          <dl className="definition-list">
            <div><dt>Server</dt><dd>{profile ? `${profile.host}:${profile.port}` : "Not configured"}</dd></div>
            <div><dt>Secure connection</dt><dd>{displayedTunnel ? "Active" : "Not connected"}</dd></div>
            <div><dt>Compatibility</dt><dd>{profileOwnsCore ? (snapshot.state.contract.compatible ? "Compatible" : "Needs update") : "Not connected"}</dd></div>
            <div><dt>Project access</dt><dd>{projectSessionReady ? "Ready" : "Unavailable"}</dd></div>
          </dl>
          <div className="system-button-row">
            <button className="secondary-button" type="button" onClick={onConfigure} disabled={busy || maintenanceBusy}><Settings size={15} /> {profile ? "Edit" : "Add workspace"}</button>
            {profile && (displayedCoreState !== "online" || servicesNeedAttention) ? <button className="secondary-button" type="button" onClick={onConnect} disabled={busy || (profileOwnsCore && isConnectionBusy(core.state)) || missingCredentialReason(profile) !== null} title={busy ? "A connection action is already running" : missingCredentialReason(profile) ?? "Reconnect remote workspace"}><RefreshCw size={15} /> Reconnect</button> : null}
            {project && maintenanceAvailable ? (
              <button
                className="secondary-button"
                type="button"
                onClick={() => openConfirmation({ kind: "repair" })}
                disabled={!canRepair}
                title={repairAuthority.title}
              >
                <Wrench size={15} /> Repair
              </button>
            ) : null}
          </div>
        </section>
      </div>
      <section className="services-section">
        <div className="section-heading"><div><Activity size={17} /><h2>Services</h2></div><div className="section-heading-actions"><span>{readyServices} of {services.length} ready</span><button type="button" className="text-button" onClick={() => void onRefresh()} disabled={busy || maintenanceBusy}><RefreshCw size={14} /> Refresh status</button></div></div>
        {servicesNeedAttention ? (
          <InlineNotice
            tone="warning"
            title="Remote services need attention"
            detail={maintenanceAvailable
              ? "Run a check, repair the environment, or restart an affected service."
              : "Reconnect the remote workspace. Automated maintenance is unavailable in this Preview."}
          />
        ) : null}
        <div className="service-list">
          {services.map((service) => (
            <ServiceRow
              key={service.id}
              service={service}
              disabled={!canOperate}
              onRestart={maintenanceAvailable && service.restartable
                ? () => openConfirmation({ kind: "restart", service })
                : null}
            />
          ))}
          {!services.length ? <div className="empty-row">Services are unavailable for this project.</div> : null}
        </div>
      </section>
      {maintenanceAvailable ? <section className="services-section">
        <div className="section-heading">
          <div><Wrench size={17} /><h2>Data management</h2></div>
        </div>
        <div className="maintenance-row">
          <div>
            <strong>Diagnostic history</strong>
            <span>Remove completed diagnostic reports older than 30 days. Project inputs, runs, outputs, and active evolution artifacts are retained.</span>
          </div>
          <button
            type="button"
            className="secondary-button"
            onClick={() => openConfirmation({ kind: "cleanup" })}
            disabled={!canOperate}
          >
            <Wrench size={15} /> Clean diagnostic history
          </button>
        </div>
      </section> : null}
    </div>
    {confirmation ? (
      <SystemConfirmationDialog
        confirmation={confirmation}
        confirmDisabled={!canOperate}
        returnFocus={confirmationTrigger.current}
        onCancel={() => setConfirmation(null)}
        onConfirm={confirmAction}
      />
    ) : null}
    </>
  );
}

function ServiceRow({
  service,
  disabled,
  onRestart,
}: {
  service: ServiceV1;
  disabled: boolean;
  onRestart: (() => void) | null;
}) {
  return (
    <div className="service-row">
      <span className={`service-indicator ${service.status}`} />
      <div><strong>{service.display_name}</strong><span>{service.status_message ?? stateLabel(service.status)}</span></div>
      <StatePill state={service.status} />
      {onRestart ? (
        <IconButton
          label={`Restart ${service.display_name}`}
          onClick={onRestart}
          disabled={disabled}
        >
          <RotateCcw size={15} />
        </IconButton>
      ) : null}
    </div>
  );
}

type SystemActivityState =
  | LocalOperationV1["state"]
  | OperationV1["status"]
  | DiagnosticReportV1["status"];

function SystemActivityView({ activity }: { activity: SystemActivity }) {
  const state = systemActivityState(activity);
  const error = activity.kind === "local"
    ? activity.operation.error
    : activity.kind === "core"
      ? activity.operation.error
      : activity.report.error;
  const checks = activity.kind === "local"
    ? activity.operation.checks.map((check) => ({
        id: check.check_id,
        label: check.label,
        status: check.status,
        message: check.summary,
        repairAction: check.repair_action,
      }))
    : activity.kind === "diagnostic"
      ? activity.report.checks.map((check) => ({
          id: check.id,
          label: diagnosticScopeLabel(check.scope),
          status: check.status,
          message: check.message,
          repairAction: check.repair_action,
        }))
      : [];
  return (
    <section className="system-activity" aria-live="polite">
      <div className="system-activity-heading">
        <div>
          <SystemActivityStateIcon state={state} />
          <strong>{activity.title}</strong>
        </div>
        <StatePill state={state} />
      </div>
      {error ? (
        <div className="system-activity-error" role="alert">
          <strong>{error.message}</strong>
          <span><b>Next action:</b> {error.next_action}</span>
        </div>
      ) : null}
      {checks.length > 0 ? (
        <div className="system-check-list">
          {checks.map((check) => (
            <div key={check.id}>
              <StatePill state={check.status} />
              <strong>{check.label}</strong>
              <div className="system-check-detail">
                <span>{check.message}</span>
                {repairActionGuidance(check.repairAction) ? (
                  <small>Next action: {repairActionGuidance(check.repairAction)}</small>
                ) : null}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <p className="system-activity-message">
          {systemActivityMessage(state)}
        </p>
      )}
    </section>
  );
}

function SystemActivityStateIcon({ state }: { state: SystemActivityState }) {
  switch (state) {
    case "queued":
      return <CircleDot className="system-activity-state-icon queued" size={16} />;
    case "running":
      return <LoaderCircle className="system-activity-state-icon running spin" size={16} />;
    case "cancelling":
      return <LoaderCircle className="system-activity-state-icon cancelling spin" size={16} />;
    case "succeeded":
      return <CheckCircle2 className="system-activity-state-icon succeeded" size={16} />;
    case "failed":
      return <XCircle className="system-activity-state-icon failed" size={16} />;
    case "cancelled":
      return <XCircle className="system-activity-state-icon cancelled" size={16} />;
    default:
      return assertNever(state);
  }
}

function systemActivityState(activity: SystemActivity): SystemActivityState {
  if (activity.kind === "local") return activity.operation.state;
  return activity.kind === "core" ? activity.operation.status : activity.report.status;
}

function systemActivityMessage(state: SystemActivityState): string {
  switch (state) {
    case "queued":
      return "The remote operation is queued.";
    case "running":
      return "Waiting for the remote operation to finish.";
    case "cancelling":
      return "Cancellation is in progress.";
    case "succeeded":
      return "The remote operation completed without additional findings.";
    case "failed":
      return "The remote operation failed.";
    case "cancelled":
      return "The remote operation was cancelled.";
    default:
      return assertNever(state);
  }
}

function isCoreOperationTerminal(status: OperationV1["status"]): boolean {
  return status === "succeeded" || status === "failed" || status === "cancelled";
}

function isLocalOperationTerminal(status: LocalOperationV1["state"]): boolean {
  return status === "succeeded" || status === "failed" || status === "cancelled";
}

function isDiagnosticTerminal(status: DiagnosticReportV1["status"]): boolean {
  return status === "succeeded" || status === "failed";
}

function isSystemActivityTerminal(activity: SystemActivity): boolean {
  if (activity.kind === "local") {
    return isLocalOperationTerminal(activity.operation.state);
  }
  return activity.kind === "core"
    ? isCoreOperationTerminal(activity.operation.status)
    : isDiagnosticTerminal(activity.report.status);
}

type SystemRepairAction =
  | LocalOperationV1["checks"][number]["repair_action"]
  | DiagnosticReportV1["checks"][number]["repair_action"];

function systemRepairAuthority(
  operation: LocalOperationV1 | null,
): { readonly enabled: boolean; readonly title: string } {
  if (operation === null || operation.operation_kind !== "project_doctor") {
    return {
      enabled: false,
      title: "Run Check first so OpenEvo can identify supported repair actions.",
    };
  }
  if (!isLocalOperationTerminal(operation.state)) {
    return {
      enabled: false,
      title: "Wait for the environment check to finish.",
    };
  }
  const automatedCheck = operation.checks.some(
    (check) =>
      (check.status === "warning" || check.status === "failed")
      && check.repair_action === "openevo_can_retry",
  );
  const automatedError = operation.error !== null
    && [
      "openevo_can_retry",
      "openevo_can_install",
      "openevo_can_reconfigure",
    ].includes(operation.error.repair_action);
  if (automatedCheck || automatedError) {
    return {
      enabled: true,
      title: "Apply the repair actions exposed by OpenEvo.",
    };
  }
  const externalActionRequired = operation.error !== null
    && operation.error.repair_action === "user_action_required";
  return {
    enabled: false,
    title: operation.checks.some((check) =>
      check.repair_action === "user_input_required"
      || check.repair_action === "reconnect_required")
      || externalActionRequired
      ? "Complete the required user or reconnection action shown in the check results."
      : "The latest check did not expose an automated repair action.",
  };
}

function repairActionGuidance(action: SystemRepairAction): string | null {
  switch (action) {
    case "openevo_can_retry":
      return "OpenEvo can apply this repair.";
    case "openevo_can_install":
      return "OpenEvo can install the missing dependency.";
    case "openevo_can_reconfigure":
      return "OpenEvo can update the managed configuration.";
    case "user_input_required":
    case "user_action_required":
      return "Complete the required server action, then run Check again.";
    case "reconnect_required":
      return "Reconnect the remote workspace, then run Check again.";
    case "none":
    case "unsupported":
      return null;
    default:
      return assertNever(action);
  }
}

function SystemConfirmationDialog({
  confirmation,
  confirmDisabled,
  returnFocus,
  onCancel,
  onConfirm,
}: {
  confirmation: SystemConfirmation;
  confirmDisabled: boolean;
  returnFocus: HTMLElement | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const dialogRef = useDialogFocus(onCancel, returnFocus);
  return (
    <div
      className="system-confirmation-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          event.preventDefault();
          onCancel();
        }
      }}
    >
      <section
        ref={dialogRef}
        className="system-confirmation"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="system-confirmation-title"
        aria-describedby="system-confirmation-detail"
        tabIndex={-1}
      >
        <div>
          <strong id="system-confirmation-title">{systemConfirmationTitle(confirmation)}</strong>
          <span id="system-confirmation-detail">{systemConfirmationDetail(confirmation)}</span>
        </div>
        <div className="system-button-row">
          <button type="button" className="secondary-button" onClick={onCancel}>Cancel</button>
          <button type="button" className="danger-button" onClick={onConfirm} disabled={confirmDisabled}>
            {confirmation.kind === "cleanup" ? <Wrench size={15} /> : <RotateCcw size={15} />}
            Confirm
          </button>
        </div>
      </section>
    </div>
  );
}

function waitForSystemRefresh(): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, SYSTEM_OPERATION_REFRESH_INTERVAL_MS);
  });
}

function systemConfirmationTitle(confirmation: SystemConfirmation): string {
  if (confirmation.kind === "repair") return "Repair the remote environment?";
  if (confirmation.kind === "cleanup") return "Clean diagnostic history?";
  return `Restart ${confirmation.service.display_name}?`;
}

function systemConfirmationDetail(confirmation: SystemConfirmation): string {
  if (confirmation.kind === "repair") {
    return "OpenEvo will apply only the repair actions exposed by the remote Daemon. Running research sessions are not modified.";
  }
  if (confirmation.kind === "cleanup") {
    return "Completed diagnostic reports older than 30 days may be removed. Project inputs, runs, outputs, and current evolution artifacts are retained.";
  }
  return "The selected managed service may be briefly unavailable. Other services and project data are retained.";
}

function diagnosticScopeLabel(scope: DiagnosticReportV1["scopes"][number]): string {
  const labels: Record<DiagnosticReportV1["scopes"][number], string> = {
    environment: "Environment",
    project: "Project",
    run: "Session",
    services: "Services",
    registry: "Evolution registry",
    storage: "Storage",
  };
  return labels[scope];
}

function RemoteWorkspaceDrawer({
  profile,
  observedProfiles,
  streamEpoch,
  busy,
  errorMessage,
  onDismissError,
  onClose,
  createSaveIntent,
  onSave,
  onCreateObserved,
}: {
  profile: RemoteProfileV1 | null;
  observedProfiles: readonly RemoteProfileV1[];
  streamEpoch: number | null;
  busy: boolean;
  errorMessage: string | null;
  onDismissError: () => void;
  onClose: () => void;
  createSaveIntent: (input: ProfileCreateV1) => ProfileSaveIntent;
  onSave: (intent: ProfileSaveIntent) => Promise<ProfileSaveAttemptResult>;
  onCreateObserved: (profile: RemoteProfileV1) => void;
}) {
  useBodyScrollLock();
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
  const [pendingCreateObservation, setPendingCreateObservation] = useState<{
    readonly profileId: string;
    readonly canonicalPayload: string;
  } | null>(null);
  const parsedPort = Number(port);
  const valid = name.trim() !== "" && host.trim() !== "" && user.trim() !== "" && Number.isInteger(parsedPort) && parsedPort > 0 && parsedPort <= 65_535;
  const requiredFieldMessage = name.trim() === ""
    ? "Enter a workspace name."
    : host.trim() === ""
      ? "Enter the remote server address."
      : !Number.isInteger(parsedPort) || parsedPort <= 0 || parsedPort > 65_535
        ? "Enter a port from 1 to 65535."
        : user.trim() === ""
          ? "Enter the remote server user name."
          : null;
  const markDirty = () => {
    pendingSaveIntent.current = null;
    setPendingCreateObservation(null);
    onDismissError();
    setDirty(true);
  };
  const update = (setter: (value: string) => void) => (event: React.ChangeEvent<HTMLInputElement>) => { setter(event.target.value); markDirty(); };
  useEffect(() => {
    const createdProfile = pendingCreateObservation
      ? matchingConfirmedProfile(
        observedProfiles,
        pendingCreateObservation.canonicalPayload,
        pendingCreateObservation.profileId,
      )
      : null;
    if (createdProfile) {
      pendingSaveIntent.current = null;
      setPendingCreateObservation(null);
      onCreateObserved(createdProfile);
    }
  }, [observedProfiles, onCreateObserved, pendingCreateObservation]);
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
          {errorMessage ? <InlineNotice
            tone="error"
            title="Action could not be completed"
            detail={errorMessage}
            onDismiss={onDismissError}
          /> : null}
          <section className="form-section">
            <h3>Server</h3>
            <label>Workspace name<input required value={name} onChange={update(setName)} placeholder="Research server" /></label>
            <div className="form-grid host-grid"><label>Server address<input required value={host} onChange={update(setHost)} placeholder="research.example.org" /></label><label>Port<input required inputMode="numeric" value={port} onChange={update(setPort)} /></label></div>
            <label>User name<input required value={user} onChange={update(setUser)} /></label>
            {requiredFieldMessage ? <p className="form-error" id="workspace-required-fields" role="status">{requiredFieldMessage}</p> : null}
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
        <div className="drawer-footer" inert={guardedClose.confirming ? true : undefined} aria-hidden={guardedClose.confirming || undefined}><button className="secondary-button" type="button" onClick={guardedClose.requestClose}>Cancel</button><button className="primary-button" type="button" aria-describedby={requiredFieldMessage ? "workspace-required-fields" : undefined} disabled={!valid || busy || streamEpoch === null || (profile !== null && !dirty)} title={!valid ? "Complete the required server fields" : streamEpoch === null ? "Refresh this view before saving" : profile && !dirty ? "No unsaved changes" : "Save remote workspace"} onClick={() => {
          if (streamEpoch === null) return;
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
          const intent = pending?.canonicalPayload === canonicalProfilePayload(input)
            ? rebaseProfileSaveIntent(pending, streamEpoch)
            : createSaveIntent(input);
          pendingSaveIntent.current = intent;
          void onSave(intent).then((result) => {
            pendingSaveIntent.current = result.pendingIntent;
            if (result.saved) return;
            const pendingRoute = result.pendingIntent?.route;
            setPendingCreateObservation(
              pendingRoute?.kind === "create" && pendingRoute.confirmedProfileId
                ? {
                    profileId: pendingRoute.confirmedProfileId,
                    canonicalPayload: result.pendingIntent!.canonicalPayload,
                  }
                : null,
            );
          });
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
  useBodyScrollLock();
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
  const folderSourceButtonRef = useRef<HTMLButtonElement | null>(null);
  const restoreFolderSourceFocus = useRef(false);
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
    restoreFolderSourceFocus.current = false;
    invalidateSourceSelection();
    void cancelActiveSource();
    void settlePendingSource("discard").finally(onClose);
  }, [cancelActiveSource, invalidateSourceSelection, onClose, settlePendingSource]);
  const guardedClose = useGuardedDrawerClose(dirty, close);
  const requestClose = () => {
    restoreFolderSourceFocus.current = false;
    invalidateSourceSelection();
    void cancelActiveSource();
    guardedClose.requestClose();
  };
  const dialogRef = useDialogFocus(requestClose);
  const saveActionId = useRef(newActionId());
  const activeModel = mode === "self-deployed" ? hfModel : codexModel;
  const activeModeCapability = executionModeCapability(executionModeCapabilities, mode);
  const visibleModeCapabilities = executionModeCapabilities.modes.filter(
    (capability) => capability.support_state === "supported"
      || (project !== null && capability.mode === project.execution.mode),
  );
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
  const selectSource = async (restoreFocus: boolean) => {
    if (sourceSelectionInFlight.current) return;
    sourceSelectionInFlight.current = true;
    restoreFolderSourceFocus.current = restoreFocus;
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
  useLayoutEffect(() => {
    if (selectingSource || !restoreFolderSourceFocus.current) return;
    restoreFolderSourceFocus.current = false;
    queueMicrotask(() => folderSourceButtonRef.current?.focus());
  }, [selectingSource]);
  useEffect(() => {
    sourceSelectionMounted.current = true;
    return () => {
      sourceSelectionMounted.current = false;
      restoreFolderSourceFocus.current = false;
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
  const requiredFieldMessage = name.trim() === ""
    ? "Enter a project name."
    : title.trim() === ""
      ? "Enter a task title."
      : objective.trim() === ""
        ? "Describe the research objective."
        : activeModel.trim() === ""
          ? "Enter the model name."
          : null;
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
            <label>Project name<input required value={name} onChange={change(setName)} /></label>
            <label>Task title<input required value={title} onChange={change(setTitle)} /></label>
            <label>Objective<textarea required rows={5} value={objective} onChange={change(setObjective)} /></label>
            {requiredFieldMessage ? <p className="form-error" id="project-required-fields" role="status">{requiredFieldMessage}</p> : null}
          </section>
          <section className="form-section">
            <h3>Research source</h3>
            <div className="segmented-control wide" role="radiogroup" aria-label="Research source" onKeyDown={handleTablistKeyDown}>
              <button type="button" role="radio" aria-checked={source.kind === "scratch"} tabIndex={source.kind === "scratch" ? 0 : -1} className={source.kind === "scratch" ? "active" : ""} disabled={selectingSource || busy} onClick={() => { invalidateSourceSelection(); void settlePendingSource("discard").then(() => { setSource({ kind: "scratch", display_name: "New workspace", import_ref: null }); setSourceError(null); markDirty(); }); }}>Scratch</button>
              <button ref={folderSourceButtonRef} type="button" role="radio" aria-checked={source.kind === "native_folder_snapshot"} tabIndex={source.kind === "native_folder_snapshot" ? 0 : -1} className={source.kind === "native_folder_snapshot" ? "active" : ""} disabled={selectingSource || busy} onClick={(event) => void selectSource(document.activeElement === event.currentTarget)}>{selectingSource ? "Selecting..." : "Folder snapshot"}</button>
            </div>
            <div className="source-summary"><FolderOpen size={17} /><span><strong>{source.display_name}</strong><small>{source.kind === "scratch" ? "A new managed workspace will be created." : "A native snapshot reference is ready."}</small></span></div>
            {sourceError ? <p className="form-error" role="alert">{sourceError}</p> : null}
          </section>
          <section className="form-section">
            <h3>Model mode</h3>
            <div className="segmented-control wide" role="radiogroup" aria-label="Model mode" onKeyDown={handleTablistKeyDown}>{visibleModeCapabilities.map((capability) => (
              <button
                type="button"
                role="radio"
                key={capability.mode}
                aria-checked={mode === capability.mode}
                aria-describedby={capability.support_state === "supported" ? undefined : "execution-mode-support-message"}
                tabIndex={focusMode === capability.mode ? 0 : -1}
                className={mode === capability.mode ? "active" : ""}
                disabled={capability.support_state !== "supported"}
                title={capability.message}
                onClick={() => { setMode(capability.mode); markDirty(); }}
              >{capability.display_name}</button>
            ))}</div>
            {mode === "self-deployed" ? <label>Hugging Face model<input required value={hfModel} onChange={change(setHfModel)} placeholder="organization/model" /></label> : <label>Codex model<input required value={codexModel} onChange={change(setCodexModel)} placeholder="Model name" /></label>}
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
        <div className="drawer-footer" inert={guardedClose.confirming ? true : undefined} aria-hidden={guardedClose.confirming || undefined}><button className="secondary-button" type="button" onClick={() => void reset()} disabled={!dirty || busy || selectingSource} title={!dirty ? "No unsaved changes" : "Undo changes"}><RotateCcw size={15} /> Undo</button><button className="primary-button" type="button" aria-describedby={requiredFieldMessage ? "project-required-fields" : undefined} disabled={!valid || busy || selectingSource || (project !== null && !dirty)} title={!profileId ? "Add a remote workspace first" : activeModeCapability.support_state !== "supported" ? activeModeCapability.message : !valid ? "Complete all required fields and valid method settings" : project && !dirty ? "No unsaved changes" : "Save project settings"} onClick={() => { invalidateSourceSelection(); const pendingActionId = pendingSourceActionId.current; void onSave({
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

function projectOptionKey(projectId: string): string {
  return `${PROJECT_OPTION_PREFIX}${projectId}`;
}

function workspaceOptionKey(profileId: string): string {
  return `${WORKSPACE_OPTION_PREFIX}${profileId}`;
}

function projectSelectionIdentity(selection: ProjectSelection | null): string {
  if (!selection) return "none";
  if (selection.kind === "sample") return SAMPLE_PROJECT_OPTION_KEY;
  return selection.kind === "project"
    ? projectOptionKey(selection.projectId)
    : workspaceOptionKey(selection.profileId);
}

function operationBelongsToSelection(
  operation: LocalOperationV1,
  project: ProjectV1 | null,
  profile: RemoteProfileV1 | null,
): boolean {
  if (operation.resource.resource_type === "profile") {
    return profile?.profile_id === operation.resource.resource_id;
  }
  return project?.project_id === operation.resource.resource_id;
}

function profileSaveIntent(
  snapshot: DesktopProductSnapshot,
  mode: RemoteWorkspaceDrawerMode,
  profile: RemoteProfileV1 | null,
  input: ProfileCreateV1,
): ProfileSaveIntent {
  const canonicalPayload = canonicalProfilePayload(input);
  if (mode.kind === "create") {
    return {
      canonicalPayload,
      input,
      route: {
        kind: "create",
        intent: mutationIntent(snapshot),
        confirmedProfileId: null,
      },
    };
  }
  if (!profile || profile.profile_id !== mode.profileId) {
    throw new Error("The selected remote workspace is no longer available.");
  }
  return {
    canonicalPayload,
    input,
    route: {
      kind: "update",
      profileId: mode.profileId,
      intent: resourceIntent(snapshot, profile.etag),
    },
  };
}

function rebaseProfileSaveIntent(
  intent: ProfileSaveIntent,
  streamEpoch: number,
): ProfileSaveIntent {
  if (intent.route.kind === "create") {
    return {
      ...intent,
      route: {
        ...intent.route,
        intent: { ...intent.route.intent, streamEpoch },
      },
    };
  }
  return {
    ...intent,
    route: {
      ...intent.route,
      intent: { ...intent.route.intent, streamEpoch },
    },
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

function matchingConfirmedProfile(
  profiles: readonly RemoteProfileV1[] | undefined,
  canonicalPayload: string,
  confirmedProfileId: string,
): RemoteProfileV1 | null {
  return profiles?.find(
    (profile) => profile.profile_id === confirmedProfileId
      && canonicalProfile(profile) === canonicalPayload,
  ) ?? null;
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

function getStartReason(snapshot: DesktopProductSnapshot, project: ProjectV1 | null, profile: RemoteProfileV1 | null, services: readonly ServiceV1[], activeRun: RunV1 | null, actionState: AsyncState): string | null {
  if (!project) return "Create or select a project first.";
  if (project.evolution_configuration_state === "pending") return "Finish evolution setup before starting a session.";
  const modeCapability = executionModeCapability(snapshot.executionModeCapabilities, project.execution.mode);
  if (modeCapability.support_state !== "supported") return modeCapability.message;
  if (snapshot.stream.status !== "fresh") return "Refresh this view before starting a session.";
  if (!profile || profile.profile_id !== project.profile_id) return "Configure this project's remote workspace before starting a session.";
  if (!project.remote || project.state !== "active") return "Activate this project on its assigned remote workspace before starting a session.";
  const active = snapshot.state.active_project;
  if (!active || active.project_id !== project.project_id || active.profile_id !== project.profile_id || active.project_etag !== project.etag) return "Activate this project on its assigned remote workspace before starting a session.";
  if (snapshot.state.core.state !== "online" || !snapshot.state.core.active_tunnel || snapshot.state.core.profile_id !== profile.profile_id || active.connection_state !== "ready") return "Connect this project's remote workspace before starting a session.";
  const capability = snapshot.capability;
  if (!capability || capability.status !== "ready" || capability.projectId !== project.project_id || capability.executionMode !== project.execution.mode || capability.value.project_id !== project.project_id || capabilityExecutionMode(capability.value.capabilities) !== project.execution.mode) return "Remote capabilities are unavailable for this project and mode.";
  const invalidTarget = evolutionTargetRows(capability.value.capabilities, project.evolution.targets).find((row) => row.selection.enabled && !row.valid);
  if (invalidTarget) return invalidTarget.reason;
  const validation = snapshot.validation;
  if (!validation || validation.status !== "ready" || validation.projectId !== project.project_id || validation.executionMode !== project.execution.mode || validation.projectEtag !== project.etag || validation.value.project_id !== project.project_id || validation.value.project_etag !== project.etag || validation.value.registry_digest !== capability.value.capabilities.registry_digest || !validation.value.valid) return "Project validation is not current for this project and mode.";
  if (services.length === 0) return "Remote service status is unavailable. Open System and reconnect before starting a session.";
  if (services.some((service) => service.status !== "running")) return "Remote services need attention. Open System and reconnect before starting a session.";
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

function retryAdvancedInSnapshot(
  snapshot: DesktopProductSnapshot,
  pending: PendingRunRetry,
): boolean {
  if (snapshot.stream.status !== "fresh") return false;
  const run = snapshot.runs.find((item) => item.id === pending.runId);
  return run ? retryAdvancedRun(run, pending) : false;
}

function retryAdvancedRun(run: RunV1, pending: PendingRunRetry): boolean {
  return retryRunProvesSingleAppend(run, {
    schemaVersion: 1,
    runId: pending.runId,
    projectId: pending.projectId,
    intent: pending.intent,
    originalRun: pending.originalRun,
    acceptedRun: pending.acceptedRun,
  });
}

function mergeAuthoritativeRetryRun(
  snapshot: DesktopProductSnapshot,
  response: RunV1,
  pending: PendingRunRetry,
): DesktopProductSnapshot {
  const index = snapshot.runs.findIndex((run) => run.id === pending.runId);
  if (index < 0) return { ...snapshot, runs: [response, ...snapshot.runs] };
  const current = snapshot.runs[index];
  if (!current || retryAdvancedRun(current, pending)) return snapshot;
  const runs = [...snapshot.runs];
  runs[index] = response;
  return { ...snapshot, runs };
}

function pendingRetryFromRecovery(
  recovery: ProductRunRetryRecovery,
  errorOwner: number,
): PendingRunRetry {
  return {
    runId: recovery.runId,
    projectId: recovery.projectId,
    intent: recovery.intent,
    errorOwner,
    originalRun: recovery.originalRun,
    acceptedRun: recovery.acceptedRun,
    transportSettled: true,
    reconciled: false,
  };
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
  if (error instanceof DesktopProductAmbiguousMutationError) return error.userMessage;
  if (error instanceof DesktopProductUserError) return error.userMessage;
  return "The request could not be completed.";
}

function isAmbiguousRetryOutcome(error: unknown): boolean {
  return error instanceof DesktopProductAmbiguousMutationError
    || (!(error instanceof DesktopApiError)
      && !(error instanceof DesktopProductProviderUnavailableError)
      && !(error instanceof DesktopProductUserError));
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

let bodyScrollLockCount = 0;
let bodyOverflowBeforeLock = "";

function useBodyScrollLock(): void {
  useLayoutEffect(() => {
    if (bodyScrollLockCount === 0) {
      bodyOverflowBeforeLock = document.body.style.overflow;
      document.body.style.overflow = "hidden";
    }
    bodyScrollLockCount += 1;
    return () => {
      bodyScrollLockCount = Math.max(0, bodyScrollLockCount - 1);
      if (bodyScrollLockCount === 0) {
        document.body.style.overflow = bodyOverflowBeforeLock;
        bodyOverflowBeforeLock = "";
      }
    };
  }, []);
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
    if (active instanceof HTMLButtonElement && ["tab", "radio"].includes(active.getAttribute("role") ?? "") && event.currentTarget.contains(active)) {
      event.preventDefault();
      active.click();
    }
    return;
  }
  const direction = event.key === "ArrowRight" || event.key === "ArrowDown"
    ? 1
    : event.key === "ArrowLeft" || event.key === "ArrowUp"
      ? -1
      : 0;
  if (direction === 0 && event.key !== "Home" && event.key !== "End") return;
  const choices = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]:not(:disabled), [role="radio"]:not(:disabled)'));
  if (choices.length === 0) return;
  const currentIndex = choices.findIndex((choice) => choice === document.activeElement);
  const nextIndex = event.key === "Home"
    ? 0
    : event.key === "End"
      ? choices.length - 1
      : (Math.max(0, currentIndex) + direction + choices.length) % choices.length;
  event.preventDefault();
  const next = choices[nextIndex];
  next?.focus();
  if (next?.getAttribute("role") === "radio") next.click();
}

function useDialogFocus(onClose: () => void, returnFocus: HTMLElement | null = null) {
  const dialogRef = useRef<HTMLElement | null>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const previous = returnFocus
      ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
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
  }, [returnFocus]);
  return dialogRef;
}

function assertNever(value: never): never {
  throw new Error(`Unexpected value: ${String(value)}`);
}

function missingCredentialReason(profile: RemoteProfileV1): string | null {
  return profile.authentication_kind === "ssh_agent"
    ? null
    : "Switch this remote workspace to SSH agent authentication before connecting.";
}
