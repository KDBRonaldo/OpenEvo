import {
  Activity,
  AlertCircle,
  BookOpen,
  CheckCircle2,
  CircleDot,
  FolderOpen,
  LoaderCircle,
  Plus,
  RefreshCw,
  Server,
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
import { SampleScientificProjectView } from "./ScientificProjectSample";
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
  SAMPLE_SCIENTIFIC_PROJECT,
  SAMPLE_SCIENTIFIC_PROJECTS,
  sampleScientificProject,
  type SampleScientificProjectId,
} from "./scientificProjectSampleData";
import {
  unavailableDesktopProductProviderV2,
  type DesktopProductProviderV2,
  type DesktopProductSnapshotV2,
  type ProductMutationIntentV2,
} from "./providerV2";

type Workspace = "research" | "evolution" | "system";

export interface DesktopProductAppV2Props {
  readonly provider?: DesktopProductProviderV2;
  readonly onInitialSnapshotFailed?: (error: unknown) => void;
  readonly onReady?: () => void;
  readonly openConnectionSettings?: boolean;
  readonly onConnectionSettingsOpened?: () => void;
}

export function DesktopProductAppV2({
  provider = unavailableDesktopProductProviderV2,
  onInitialSnapshotFailed,
  onReady,
  openConnectionSettings = false,
  onConnectionSettingsOpened,
}: DesktopProductAppV2Props) {
  const [snapshot, setSnapshot] = useState<DesktopProductSnapshotV2 | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionStatus, setActionStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [workspace, setWorkspace] = useState<Workspace>("research");
  const [connectionOpen, setConnectionOpen] = useState(false);
  const [projectOpen, setProjectOpen] = useState(false);
  const [serviceLogs, setServiceLogs] = useState<Readonly<Record<string, readonly LogEntryV2[]>>>({});
  const [selectedSampleId, setSelectedSampleId] = useState<SampleScientificProjectId>(
    SAMPLE_SCIENTIFIC_PROJECT.id,
  );
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
  const activeProfile = snapshot.profiles.find(
    (profile) => profile.profile_id === snapshot.state.active_profile_id,
  ) ?? null;
  const connectedProfiles = snapshot.profiles.filter(isConnectedProfile);
  const selectedSample = sampleScientificProject(selectedSampleId);
  const generation = activeProject?.active_project_head?.generation
    ?? selectedSample.activeProjectHeadGeneration;
  const lifecycleStates = provider.listLifecycleOperations();
  const coreOperations = provider.listCoreOperations();
  const diagnostics = provider.listDiagnostics();
  const mutationIntents = provider.listMutationIntents();
  const visibleOperationCount = lifecycleStates.length + coreOperations.length + diagnostics.length;

  const runProject = async (project: ProjectV2): Promise<void> => {
    if (project.state !== "ready") return;
    setBusy(true);
    setActionError(null);
    setActionStatus(null);
    try {
      const validation = await provider.validateProject(project.project_id, intentFor(snapshot, "validate-project"));
      if (!validation.valid) {
        setActionError("The active remote registry rejected this project configuration. Correct the failed checks before running.");
        return;
      }
      await provider.submitTask(project.project_id, intentFor(snapshot, "submit-task"));
      await refresh();
      setActionStatus("Task admitted with immutable Project Head and execution authority.");
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
          <WorkspaceButton active={workspace === "research"} onClick={() => setWorkspace("research")} icon={BookOpen}>Research</WorkspaceButton>
          <WorkspaceButton active={workspace === "evolution"} onClick={() => setWorkspace("evolution")} icon={Sparkles}>Evolution</WorkspaceButton>
          <WorkspaceButton active={workspace === "system"} onClick={() => setWorkspace("system")} icon={Activity}>System</WorkspaceButton>
        </nav>
        <div className="sidebar-foot">
          <div className="sidebar-foot-label">
            {activeProject === null ? "Demo Project Head" : "Active Project Head"}
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
                value={activeProject ? `project:${activeProject.project_id}` : `sample:${selectedSampleId}`}
                onChange={(event) => {
                  if (event.target.value.startsWith("sample:")) {
                    setSelectedSampleId(event.target.value.slice(7) as SampleScientificProjectId);
                  }
                }}
              >
                {activeProject ? <option value={`project:${activeProject.project_id}`}>{activeProject.display_name}</option> : null}
                {SAMPLE_SCIENTIFIC_PROJECTS.map((sample) => (
                  <option key={sample.id} value={`sample:${sample.id}`}>[Demo] {sample.name}</option>
                ))}
              </select>
            </div>
          </div>
          <div className="topbar-actions">
            {activeProject === null && connectedProfiles.length > 0 ? (
              <button type="button" className="secondary-button" onClick={() => setProjectOpen(true)}>
                <FolderOpen size={15} /> New project
              </button>
            ) : null}
            <button type="button" className="primary-button topbar-primary-action" onClick={() => setConnectionOpen(true)}>
              <Plus size={16} /> Add remote workspace
            </button>
          </div>
        </header>

        <main className="product-main">
          {loadError ? <Notice tone="error" title="Refresh failed" detail={loadError} /> : null}
          {actionError ? <Notice tone="error" title="Action could not be completed" detail={actionError} onDismiss={() => setActionError(null)} /> : null}
          {actionStatus ? <Notice tone="success" title="Remote authority updated" detail={actionStatus} onDismiss={() => setActionStatus(null)} /> : null}
          {snapshot.stream.status !== "fresh" ? (
            <Notice tone="warning" title="Refreshing authoritative state" detail="Actions remain paused until Desktop reloads current remote state." />
          ) : null}

          {activeProject === null ? (
            <SampleScientificProjectView
              workspace={workspace}
              project={selectedSample}
              onConnectRemote={() => setConnectionOpen(true)}
            />
          ) : workspace === "research" ? (
            <ResearchWorkspaceV2
              project={activeProject}
              tasks={snapshot.tasks}
              transitions={snapshot.transitions}
              timelines={snapshot.timelines}
              busy={busy}
              onRun={() => void runProject(activeProject)}
              onCancelTask={(task) => void act(
                () => provider.cancelTask(task.task_id, intentFor(snapshot, "cancel-task")),
                "Task cancellation requested.",
              )}
              onRetryTask={(task) => void act(
                () => provider.retryTask(task.task_id, intentFor(snapshot, "retry-task")),
                "A new infrastructure Attempt was requested under the same Task Admission.",
              )}
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
              project={activeProject}
              snapshot={snapshot}
              busy={busy}
              onSave={(config) => void act(
                () => provider.updateProject(activeProject.project_id, activeProject.display_name, config, intentFor(snapshot, "save-evolution")),
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
          snapshot={snapshot}
          provider={provider}
          busy={busy}
          onBusy={setBusy}
          onClose={() => setProjectOpen(false)}
          onCreated={async () => {
            await refresh();
            setProjectOpen(false);
            setActionStatus("Project creation started. Progress and process logs remain available in Operations.");
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
  const sample = sampleScientificProject(SAMPLE_SCIENTIFIC_PROJECT.id);
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
          <SampleScientificProjectView workspace="research" project={sample} onConnectRemote={onAddRemote} />
        </main>
      </div>
    </div>
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
  snapshot,
  provider,
  busy,
  onBusy,
  onClose,
  onCreated,
  onError,
}: {
  readonly profile: RemoteWorkspaceProfileV2;
  readonly snapshot: DesktopProductSnapshotV2;
  readonly provider: DesktopProductProviderV2;
  readonly busy: boolean;
  readonly onBusy: (value: boolean) => void;
  readonly onClose: () => void;
  readonly onCreated: () => Promise<void>;
  readonly onError: (error: unknown) => void;
}) {
  const [displayName, setDisplayName] = useState("New research project");
  const [title, setTitle] = useState("Research task");
  const [objective, setObjective] = useState("");
  const [workspaceKind, setWorkspaceKind] = useState<"scratch" | "native_folder_snapshot">("scratch");
  const [workspaceDisplayName, setWorkspaceDisplayName] = useState("Research workspace");
  const [selectedSourceDisplayName, setSelectedSourceDisplayName] = useState<string | null>(null);
  const [sourceActionId, setSourceActionId] = useState<string | null>(null);
  const closedRef = useRef(false);
  const dialogRef = useDialogBoundary(onClose);
  const baseDraftValid = displayName.trim() !== "" && title.trim() !== "" && objective.trim() !== "";
  const valid = baseDraftValid
    && (workspaceKind === "scratch" || sourceActionId !== null);

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
    const actionId = workspaceKind === "native_folder_snapshot" ? sourceActionId! : actionIdV2("create-project");
    const config = scienceProjectConfig(title, objective, workspaceKind, workspaceDisplayName);
    onBusy(true);
    try {
      await provider.createProject({
        profileId: profile.profile_id,
        displayName: displayName.trim(),
        config,
      }, { actionId, streamEpoch: snapshot.stream.epoch });
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
        <div className="drawer-head"><div><span className="panel-kicker">Remote project</span><h2 id="new-project-title">Create science project</h2></div><button type="button" className="icon-button" aria-label="Close project setup" onClick={() => void close()}><X size={18} /></button></div>
        <div className="drawer-content">
          <section className="form-section">
            <label>Project name<input maxLength={256} value={displayName} disabled={sourceActionId !== null} onChange={(event) => setDisplayName(event.target.value)} /></label>
            <label>Task title<input maxLength={256} value={title} disabled={sourceActionId !== null} onChange={(event) => setTitle(event.target.value)} /></label>
            <label>Task objective<textarea rows={7} maxLength={65_536} value={objective} disabled={sourceActionId !== null} onChange={(event) => setObjective(event.target.value)} placeholder="Describe the scientific result the agent should produce." /></label>
          </section>
          <section className="form-section">
            <h3>Workspace snapshot</h3>
            <div className="v2-source-choice"><button type="button" className={workspaceKind === "scratch" ? "selected" : ""} disabled={sourceActionId !== null} onClick={() => { setWorkspaceKind("scratch"); setSourceActionId(null); setSelectedSourceDisplayName(null); setWorkspaceDisplayName("Research workspace"); }}>New scratch workspace</button><button type="button" className={workspaceKind === "native_folder_snapshot" ? "selected" : ""} disabled={!baseDraftValid || sourceActionId !== null} onClick={() => void chooseFolder()}><FolderOpen size={15} /> Choose folder snapshot</button></div>
            <p className="form-help">{workspaceKind === "native_folder_snapshot" ? selectedSourceDisplayName ?? "Preparing selected workspace…" : "Core will create an immutable empty Workspace Snapshot."}</p>
          </section>
          <section className="form-section"><h3>Execution</h3><div className="agent-note"><ShieldCheck size={17} /><span>Codex Subscription · transcript capture · gpt-5.3-codex-spark · high effort</span></div></section>
        </div>
        <div className="drawer-footer"><button type="button" className="secondary-button" onClick={() => void close()}>Cancel</button><button type="button" className="primary-button" onClick={() => void create()} disabled={busy || !valid}>{busy ? <LoaderCircle className="spin" size={15} /> : <Plus size={15} />} Create project</button></div>
      </section>
    </div>
  );
}

function ResearchWorkspaceV2({
  project,
  tasks,
  transitions,
  timelines,
  busy,
  onRun,
  onCancelTask,
  onRetryTask,
  onRetryTransition,
  onAbandonTransition,
}: {
  readonly project: ProjectV2;
  readonly tasks: readonly TaskV2[];
  readonly transitions: Readonly<Record<string, SuccessorTransitionV2>>;
  readonly timelines: DesktopProductSnapshotV2["timelines"];
  readonly busy: boolean;
  readonly onRun: () => void;
  readonly onCancelTask: (task: TaskV2) => void;
  readonly onRetryTask: (task: TaskV2) => void;
  readonly onRetryTransition: (transition: SuccessorTransitionV2) => void;
  readonly onAbandonTransition: (transition: SuccessorTransitionV2) => void;
}) {
  const projectTasks = tasks.filter((task) => task.project_id === project.project_id);
  const ready = project.state === "ready" && project.active_project_head !== null && project.admission_etag !== null;
  return (
    <div className="v2-workspace-stack">
      <section className="product-panel task-panel">
        <div className="panel-heading"><div><span className="panel-kicker">Current science task</span><h2>{project.config.task.title}</h2></div><span className={`state-pill ${project.state}`}>{project.state.replaceAll("_", " ")}</span></div>
        <p className="brief-body">{project.config.task.objective}</p>
        {!ready ? <Notice tone="warning" title="Next task is not ready" detail="The current successor, settings, workspace, or runtime transition must finish before Core can create another Task Admission." /> : null}
        <div className="v2-primary-row"><button type="button" className="primary-button" disabled={busy || !ready} onClick={onRun}>{busy ? <LoaderCircle className="spin" size={15} /> : <Sparkles size={15} />} Validate and run task</button></div>
      </section>
      {project.active_project_head ? <AuthorityCardsV2 project={project} /> : null}
      <section className="product-panel task-panel">
        <div className="panel-heading"><div><span className="panel-kicker">Immutable history</span><h2>Tasks and infrastructure Attempts</h2></div><span className="muted-pill">{projectTasks.length} Task{projectTasks.length === 1 ? "" : "s"}</span></div>
        {projectTasks.length === 0 ? <p className="v2-empty-copy">No admitted Task yet. Project edits remain drafts until validation and admission succeed.</p> : <div className="v2-task-list">{projectTasks.map((task) => {
          const transition = task.successor_transition ? transitions[task.successor_transition.successor_transition_id] : null;
          return <TaskAuthorityCardV2 key={task.task_id} task={task} transition={transition ?? null} timeline={timelines[task.task_id] ?? []} busy={busy} onCancel={() => onCancelTask(task)} onRetry={() => onRetryTask(task)} onRetryTransition={() => transition && onRetryTransition(transition)} onAbandonTransition={() => transition && onAbandonTransition(transition)} />;
        })}</div>}
      </section>
    </div>
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

function TaskAuthorityCardV2({
  task,
  transition,
  timeline,
  busy,
  onCancel,
  onRetry,
  onRetryTransition,
  onAbandonTransition,
}: {
  readonly task: TaskV2;
  readonly transition: SuccessorTransitionV2 | null;
  readonly timeline: DesktopProductSnapshotV2["timelines"][string];
  readonly busy: boolean;
  readonly onCancel: () => void;
  readonly onRetry: () => void;
  readonly onRetryTransition: () => void;
  readonly onAbandonTransition: () => void;
}) {
  const active = ["admitted", "preparing", "running", "cancelling"].includes(task.state);
  return (
    <article className="v2-task-card">
      <div className="v2-profile-card-head"><div><strong>Task {task.task_id}</strong><span>{task.state.replaceAll("_", " ")}</span></div><span className={`state-pill ${task.state}`}>{task.state.replaceAll("_", " ")}</span></div>
      <div className="v2-task-authority"><div><span>Task Admission</span><code>{task.admission.task_admission_id}</code><small>{shortDigest(task.admission.admission_sha256)}</small></div><div><span>Predecessor Project Head</span><code>{task.admission.predecessor_project_head.project_head_id}</code><small>Generation {task.admission.predecessor_project_head.generation}</small></div></div>
      <div className="v2-attempt-list">{task.attempts.map((attempt) => <div key={attempt.attempt_id}><strong>Attempt {attempt.ordinal}</strong><code>{attempt.attempt_id}</code>{attempt.attempt_id === task.authoritative_attempt_id ? <span className="muted-pill">authoritative</span> : null}</div>)}</div>
      {!["completed", "closed"].includes(task.state) ? (
        <LifecycleOperationPanelV2
          model={taskPanelModelV2(task, timeline)}
          onCancel={active ? onCancel : undefined}
        />
      ) : null}
      {transition !== null && transition.state !== "committed" ? (
        <LifecycleOperationPanelV2 model={transitionPanelModelV2(transition, timeline)} />
      ) : null}
      {transition ? <div className="v2-transition"><div><span>Successor Transition</span><strong>{transition.transition.successor_transition_id}</strong><small>Expected Project Head generation {transition.transition.expected_successor_generation}</small></div><span className={`state-pill ${transition.state}`}>{transition.state}</span>{transition.error ? <p>{transition.error.message}</p> : null}{transition.state === "failed" ? <div className="v2-card-actions"><button type="button" className="secondary-button" disabled={busy} onClick={onRetryTransition}>Retry successor transition</button><button type="button" className="text-button" disabled={busy} onClick={onAbandonTransition}>Abandon evolution result</button></div> : null}</div> : null}
      <div className="v2-card-actions">{["failed", "cancelled"].includes(task.state) ? <button type="button" className="secondary-button" disabled={busy} onClick={onRetry}>Append infrastructure Attempt</button> : null}</div>
    </article>
  );
}

function EvolutionWorkspaceV2({
  project,
  snapshot,
  busy,
  onSave,
}: {
  readonly project: ProjectV2;
  readonly snapshot: DesktopProductSnapshotV2;
  readonly busy: boolean;
  readonly onSave: (config: ScienceProjectConfigV2) => void;
}) {
  const [targets, setTargets] = useState(project.config.evolution.targets);
  useEffect(() => setTargets(project.config.evolution.targets), [project.project_config_sha256]);
  const capabilities = snapshot.capability?.project_id === project.project_id
    ? snapshot.capability.capabilities.targets.filter((target) => target.exposure === "desktop")
    : [];
  return (
    <div className="v2-workspace-stack">
      {project.active_project_head ? <AuthorityCardsV2 project={project} /> : null}
      <section className="product-panel task-panel">
        <div className="panel-heading"><div><span className="panel-kicker">Verified remote registry</span><h2>Evolution targets</h2></div><span className="muted-pill">{shortDigest(snapshot.capability?.registry_sha256 ?? "")}</span></div>
        {capabilities.length === 0 ? <Notice tone="warning" title="No visible evolution methods" detail="The active verified Core registry did not publish a Desktop-visible target for this execution profile." /> : <div className="v2-target-list">{capabilities.map((target) => {
          const current = targets[target.target_id] ?? { enabled: false, method: null, config: {} };
          const methodId = current.method ?? target.effective_default_method_id ?? "";
          const methods = target.methods;
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
    </div>
  );
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
): ScienceProjectConfigV2 {
  return {
    schema_version: "2",
    task: { title: title.trim(), objective: objective.trim() },
    workspace: { kind: workspaceKind, display_name: workspaceDisplayName.trim() },
    execution: {
      mode: "codex_subscription_transcript",
      capture_mode: "transcript",
      token_level_metrics_available: false,
      harness_id: "codex",
      codex_model: "gpt-5.3-codex-spark",
      reasoning_effort: "high",
      token_limit: 32_000,
      task_network_allow_internet: true,
    },
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
