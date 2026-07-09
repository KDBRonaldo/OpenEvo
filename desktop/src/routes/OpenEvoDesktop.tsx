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
import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  activateOpenEvoProjectConfig,
  fetchOpenEvoBackendArtifactPreview,
  fetchOpenEvoBackendRunArtifacts,
  fetchOpenEvoBackendRunTimeline,
  fetchOpenEvoDesktopCapabilities,
  fetchOpenEvoProjectConfigs,
  fetchOpenEvoDesktopShellModel,
  pollOpenEvoRunStatus,
  runOpenEvoBootstrap,
  runOpenEvoServices,
  runOpenEvoStartRun,
  runOpenEvoWorkspaceSync,
  saveOpenEvoProjectConfig,
  type OpenEvoBackendArtifactPreview,
  type OpenEvoBackendArtifactSummary,
  type OpenEvoBackendTimelineEvent,
  type OpenEvoProjectConfigDraft,
  type OpenEvoDesktopCapabilities,
  type OpenEvoRunStatus,
  type OpenEvoSavedProjectConfig,
} from "../api/openevo";
import {
  type EvolutionStepState,
  type OpenEvoDesktopShellModel,
  type RemoteServiceState,
  getOpenEvoDesktopShellModel,
  getOpenEvoTimelineSummary,
  toDraftPayload,
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

type LifecycleReportPayload = Record<string, unknown>;

export function OpenEvoDesktop() {
  const [model, setModel] = useState(() => getOpenEvoDesktopShellModel());
  const [sidecarConnected, setSidecarConnected] = useState(false);
  const [workspaceRunning, setWorkspaceRunning] = useState(false);
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const [bootstrapRunning, setBootstrapRunning] = useState(false);
  const [bootstrapError, setBootstrapError] = useState<string | null>(null);
  const [bootstrapReport, setBootstrapReport] =
    useState<LifecycleReportPayload | null>(null);
  const [servicesRunning, setServicesRunning] = useState(false);
  const [servicesError, setServicesError] = useState<string | null>(null);
  const [servicesReport, setServicesReport] =
    useState<LifecycleReportPayload | null>(null);
  const [runRunning, setRunRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [workspaceReport, setWorkspaceReport] =
    useState<LifecycleReportPayload | null>(null);
  const [latestRun, setLatestRun] = useState<OpenEvoRunStatus | null>(null);
  const [runTimeline, setRunTimeline] = useState<
    OpenEvoBackendTimelineEvent[] | null
  >(null);
  const [runArtifacts, setRunArtifacts] = useState<
    OpenEvoBackendArtifactSummary[] | null
  >(null);
  const [runArtifactsLoading, setRunArtifactsLoading] = useState(false);
  const [runArtifactsError, setRunArtifactsError] = useState<string | null>(null);
  const [artifactContent, setArtifactContent] =
    useState<OpenEvoBackendArtifactPreview | null>(null);
  const [artifactContentLoading, setArtifactContentLoading] = useState(false);
  const [artifactContentError, setArtifactContentError] = useState<string | null>(
    null,
  );
  const [desktopCapabilities, setDesktopCapabilities] =
    useState<OpenEvoDesktopCapabilities | null>(null);
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [configSaving, setConfigSaving] = useState(false);
  const [configError, setConfigError] = useState<string | null>(null);
  const [savedConfigs, setSavedConfigs] = useState<OpenEvoSavedProjectConfig[]>(
    [],
  );
  const [configCatalogLoading, setConfigCatalogLoading] = useState(false);
  const [configCatalogError, setConfigCatalogError] = useState<string | null>(
    null,
  );
  const [activatingConfigSlug, setActivatingConfigSlug] = useState<string | null>(
    null,
  );
  const [configDraft, setConfigDraft] = useState<OpenEvoProjectConfigDraft>(() =>
    toDraftPayload(getOpenEvoDesktopShellModel()),
  );
  const mounted = useRef(true);
  const catalogRefreshGeneration = useRef(0);
  const runPollGeneration = useRef(0);
  const runPollTimer = useRef<number | null>(null);
  const summary = getOpenEvoTimelineSummary(model);
  const lifecycleAuthError = unsupportedLifecycleAuthMessage(model);
  const workspaceReady = model.services.some(
    (service) => service.id === "workspace" && service.state === "ready",
  );
  const bootstrapReady = model.bootstrap.ready;
  const servicesPrerequisitesReady = workspaceReady && bootstrapReady;
  const runtimeServicesReady = model.services.some(
    (service) => service.id === "openevo-backend" && service.state === "ready",
  );
  const evolutionTargets = desktopEvolutionTargets(
    desktopCapabilities,
    configDraft.execution_mode,
  );
  const artifactDisplayNames = artifactTargetDisplayNames(desktopCapabilities);

  const refreshSavedConfigs = async () => {
    catalogRefreshGeneration.current += 1;
    const generation = catalogRefreshGeneration.current;
    setConfigCatalogLoading(true);
    setConfigCatalogError(null);
    try {
      const configs = await fetchOpenEvoProjectConfigs();
      if (!mounted.current || generation !== catalogRefreshGeneration.current) {
        return;
      }
      setSavedConfigs(configs);
    } catch (error) {
      if (!mounted.current || generation !== catalogRefreshGeneration.current) {
        return;
      }
      const message =
        error instanceof Error ? error.message : "Saved config catalog failed";
      setConfigCatalogError(message);
    } finally {
      if (mounted.current && generation === catalogRefreshGeneration.current) {
        setConfigCatalogLoading(false);
      }
    }
  };

  useEffect(() => {
    let cancelled = false;

    fetchOpenEvoDesktopCapabilities()
      .then((capabilities) => {
        if (!cancelled) {
          setDesktopCapabilities(capabilities);
        }
      })
      .catch(() => undefined);

    fetchOpenEvoDesktopShellModel()
      .then((nextModel) => {
        if (!cancelled) {
          setModel(nextModel);
          setConfigDraft(toDraftPayload(nextModel));
          setSidecarConnected(true);
          void refreshSavedConfigs();
        }
      })
      .catch(() => undefined);

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      invalidateRunPolling();
    };
  }, []);

  const clearRunPollTimer = () => {
    if (runPollTimer.current !== null) {
      window.clearTimeout(runPollTimer.current);
      runPollTimer.current = null;
    }
  };

  const invalidateRunPolling = () => {
    runPollGeneration.current += 1;
    clearRunPollTimer();
  };

  const clearLatestRunForContextChange = () => {
    invalidateRunPolling();
    setRunRunning(false);
    setLatestRun(null);
    setRunTimeline(null);
    setRunArtifacts(null);
    setRunArtifactsLoading(false);
    setRunArtifactsError(null);
    setArtifactContent(null);
    setArtifactContentLoading(false);
    setArtifactContentError(null);
    setDiagnosticsOpen(false);
  };

  const clearLifecycleReportsForContextChange = () => {
    setWorkspaceReport(null);
    setBootstrapReport(null);
    setServicesReport(null);
  };

  const handleWorkspaceSync = async () => {
    if (lifecycleAuthError) {
      setWorkspaceError(lifecycleAuthError);
      return;
    }
    setWorkspaceRunning(true);
    setWorkspaceError(null);
    setWorkspaceReport(null);
    try {
      const response = await runOpenEvoWorkspaceSync();
      setModel(response.status);
      setWorkspaceReport(response.report);
      setServicesReport(null);
      setServicesError(null);
      clearLatestRunForContextChange();
      setSidecarConnected(true);
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Workspace sync failed";
      setWorkspaceError(message);
    } finally {
      setWorkspaceRunning(false);
    }
  };

  const handleConfigDraftChange = (
    field: keyof OpenEvoProjectConfigDraft,
    value: string | number | boolean | null,
  ) => {
    setConfigDraft((current) => ({ ...current, [field]: value }));
  };

  const handleSourceTypeChange = (
    sourceType: OpenEvoProjectConfigDraft["source_type"],
  ) => {
    setConfigDraft((current) => ({
      ...current,
      source_type: sourceType,
      source_path:
        sourceType === "remote_path" || sourceType === "local_folder"
          ? current.source_path ?? ""
          : null,
      source_url: sourceType === "git_repository" ? current.source_url ?? "" : null,
      source_branch:
        sourceType === "git_repository" ? current.source_branch ?? null : null,
    }));
  };

  const handleAuthMethodChange = (
    authMethod: OpenEvoProjectConfigDraft["auth_method"],
  ) => {
    setConfigDraft((current) => ({
      ...current,
      auth_method: authMethod,
      private_key_path:
        authMethod === "private_key" ? current.private_key_path ?? "" : null,
      password_ref: authMethod === "password_ref" ? current.password_ref ?? "" : null,
      passphrase_ref:
        authMethod === "private_key" ? current.passphrase_ref ?? null : null,
    }));
  };

  const handleExecutionModeChange = (
    executionMode: OpenEvoProjectConfigDraft["execution_mode"],
  ) => {
    setConfigDraft((current) => ({
      ...current,
      execution_mode: executionMode,
      codex_model:
        executionMode === "codex_subscription_transcript"
          ? current.codex_model || "gpt-5.1-codex-mini"
          : null,
      hf_model: executionMode === "self-deployed" ? current.hf_model || "" : null,
    }));
  };

  const handleSaveConfig = async () => {
    setConfigSaving(true);
    setConfigError(null);
    const submittedDraft = configDraft;
    try {
      const response = await saveOpenEvoProjectConfig(submittedDraft);
      setModel(response.status);
      setConfigDraft(submittedDraft);
      clearLifecycleReportsForContextChange();
      clearLatestRunForContextChange();
      setSidecarConnected(true);
      await refreshSavedConfigs();
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Project config save failed";
      setConfigError(message);
    } finally {
      setConfigSaving(false);
    }
  };

  const handleActivateConfig = async (config: OpenEvoSavedProjectConfig) => {
    if (!config.valid) {
      return;
    }
    setActivatingConfigSlug(config.projectSlug);
    setConfigError(null);
    try {
      const response = await activateOpenEvoProjectConfig(config.projectSlug);
      setModel(response.status);
      setConfigDraft(toDraftPayload(response.status));
      clearLifecycleReportsForContextChange();
      clearLatestRunForContextChange();
      setSidecarConnected(true);
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Project config activation failed";
      setConfigError(message);
    } finally {
      setActivatingConfigSlug(null);
    }
  };

  const handleBootstrap = async () => {
    if (lifecycleAuthError) {
      setBootstrapError(lifecycleAuthError);
      return;
    }
    setBootstrapRunning(true);
    setBootstrapError(null);
    setBootstrapReport(null);
    try {
      const response = await runOpenEvoBootstrap();
      setModel(response.status);
      setBootstrapReport(response.report);
      setServicesReport(null);
      setServicesError(null);
      clearLatestRunForContextChange();
      setSidecarConnected(true);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Bootstrap failed";
      setBootstrapError(message);
    } finally {
      setBootstrapRunning(false);
    }
  };

  const handleServices = async () => {
    if (lifecycleAuthError) {
      setServicesError(lifecycleAuthError);
      return;
    }
    setServicesRunning(true);
    setServicesError(null);
    setServicesReport(null);
    try {
      const response = await runOpenEvoServices();
      setModel(response.status);
      setServicesReport(response.report);
      clearLatestRunForContextChange();
      setSidecarConnected(true);
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Service startup failed";
      setServicesError(message);
    } finally {
      setServicesRunning(false);
    }
  };

  const handleStartRun = async () => {
    if (lifecycleAuthError) {
      setRunError(lifecycleAuthError);
      return;
    }
    invalidateRunPolling();
    const generation = runPollGeneration.current;
    setRunRunning(true);
    setRunError(null);
    setRunTimeline(null);
    setRunArtifacts(null);
    setRunArtifactsLoading(false);
    setRunArtifactsError(null);
    setArtifactContent(null);
    setArtifactContentLoading(false);
    setArtifactContentError(null);
    setDiagnosticsOpen(false);
    try {
      const response = await runOpenEvoStartRun();
      if (!mounted.current || generation !== runPollGeneration.current) {
        return;
      }
      setModel(response.status);
      setLatestRun(response.run);
      setSidecarConnected(true);
      if (response.run.state === "running") {
        void pollLatestRun(generation);
      } else {
        setRunRunning(false);
        void loadRunArtifacts(response.run.id, generation);
      }
    } catch (error) {
      if (!mounted.current || generation !== runPollGeneration.current) {
        return;
      }
      const message = error instanceof Error ? error.message : "Run launch failed";
      setRunError(message);
      setRunRunning(false);
    }
  };

  const pollLatestRun = async (generation: number) => {
    clearRunPollTimer();
    try {
      const response = await pollOpenEvoRunStatus();
      if (!mounted.current || generation !== runPollGeneration.current) {
        return;
      }
      setModel(response.status);
      setLatestRun(response.run);
      setSidecarConnected(true);
      if (response.run.state === "running") {
        runPollTimer.current = window.setTimeout(() => {
          void pollLatestRun(generation);
        }, 1000);
      } else {
        setRunRunning(false);
        void loadRunArtifacts(response.run.id, generation);
      }
    } catch (error) {
      if (!mounted.current || generation !== runPollGeneration.current) {
        return;
      }
      const message = error instanceof Error ? error.message : "Run status failed";
      setRunError(message);
      setRunRunning(false);
    }
  };

  const loadRunArtifacts = async (runId: string, generation: number) => {
    setRunArtifactsLoading(true);
    setRunArtifactsError(null);
    try {
      const [timeline, artifacts] = await Promise.all([
        fetchOpenEvoBackendRunTimeline(runId),
        fetchOpenEvoBackendRunArtifacts(runId),
      ]);
      if (!mounted.current || generation !== runPollGeneration.current) {
        return;
      }
      setRunTimeline(timeline);
      setRunArtifacts(artifacts);
      const previewArtifactId = firstDisplayArtifactId(artifacts);
      if (previewArtifactId) {
        void loadArtifactContent(previewArtifactId, generation);
      }
    } catch (error) {
      if (!mounted.current || generation !== runPollGeneration.current) {
        return;
      }
      const message =
        error instanceof Error ? error.message : "Run artifact loading failed";
      setRunTimeline(null);
      setRunArtifacts(null);
      setRunArtifactsError(message);
    } finally {
      if (mounted.current && generation === runPollGeneration.current) {
        setRunArtifactsLoading(false);
      }
    }
  };

  const loadArtifactContent = async (artifactId: string, generation: number) => {
    setArtifactContentLoading(true);
    setArtifactContentError(null);
    try {
      const content = await fetchOpenEvoBackendArtifactPreview(artifactId);
      if (!mounted.current || generation !== runPollGeneration.current) {
        return;
      }
      setArtifactContent(content);
    } catch (error) {
      if (!mounted.current || generation !== runPollGeneration.current) {
        return;
      }
      const message =
        error instanceof Error ? error.message : "Artifact content failed";
      setArtifactContent(null);
      setArtifactContentError(message);
    } finally {
      if (mounted.current && generation === runPollGeneration.current) {
        setArtifactContentLoading(false);
      }
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
            <span>
              {model.execution.tokenMetricsAvailable
                ? "token capture path"
                : "transcript evolution"}
            </span>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <CommandButton
            icon={<Upload size={16} />}
            label={workspaceRunning ? "Syncing" : "Sync Workspace"}
            disabled={
              !sidecarConnected ||
              workspaceRunning ||
              bootstrapRunning ||
              servicesRunning ||
              runRunning ||
              configSaving ||
              activatingConfigSlug !== null ||
              lifecycleAuthError !== null
            }
            onClick={handleWorkspaceSync}
          />
          <CommandButton
            icon={<RefreshCw size={16} />}
            label={bootstrapRunning ? "Bootstrapping" : "Bootstrap"}
            disabled={
              !sidecarConnected ||
              bootstrapRunning ||
              workspaceRunning ||
              servicesRunning ||
              runRunning ||
              configSaving ||
              activatingConfigSlug !== null ||
              lifecycleAuthError !== null
            }
            onClick={handleBootstrap}
          />
          <CommandButton
            icon={<Activity size={16} />}
            label={servicesRunning ? "Starting Services" : "Start Services"}
            disabled={
              !sidecarConnected ||
              servicesRunning ||
              workspaceRunning ||
              bootstrapRunning ||
              runRunning ||
              configSaving ||
              activatingConfigSlug !== null ||
              !servicesPrerequisitesReady ||
              lifecycleAuthError !== null
            }
            onClick={handleServices}
          />
          <CommandButton
            icon={<Play size={16} />}
            label={runRunning ? "Running" : "Start Run"}
            primary
            disabled={
              !sidecarConnected ||
              runRunning ||
              workspaceRunning ||
              bootstrapRunning ||
              servicesRunning ||
              !runtimeServicesReady ||
              configSaving ||
              activatingConfigSlug !== null ||
              lifecycleAuthError !== null
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
          detail={
            model.execution.tokenMetricsAvailable
              ? "proxy capture path"
              : "subscription transcript mode"
          }
        />
        <Metric
          label="Bootstrap"
          value={summary.bootstrapReady ? "ready" : "needs setup"}
          detail="remote preflight"
        />
      </section>

      <Panel title="Project Setup" icon={<Settings size={17} />}>
        <div className="space-y-4">
          {savedConfigs.length > 0 || configCatalogLoading || configCatalogError ? (
            <div className="space-y-3 border-b border-slate-100 pb-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="text-sm font-semibold text-slate-900">
                  Saved Configs
                </div>
                {configCatalogLoading ? (
                  <div className="text-xs uppercase text-slate-500">Loading</div>
                ) : null}
              </div>
              {configCatalogError ? (
                <div className="border-l-2 border-rose-300 pl-3 text-sm text-rose-900">
                  {configCatalogError}
                </div>
              ) : null}
              {savedConfigs.length > 0 ? (
                <div className="divide-y divide-slate-100">
                  {savedConfigs.map((config) => {
                    const displayName = config.projectName ?? config.projectSlug;
                    const activating = activatingConfigSlug === config.projectSlug;
                    return (
                      <div
                        key={config.projectSlug}
                        className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 py-3 first:pt-0 last:pb-0"
                      >
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="text-sm font-medium text-slate-900">
                              {displayName}
                            </span>
                            <span
                              className={`rounded-full px-2 py-0.5 text-xs ${
                                config.valid
                                  ? "bg-emerald-50 text-emerald-800"
                                  : "bg-rose-50 text-rose-800"
                              }`}
                            >
                              {config.valid ? "valid" : "invalid"}
                            </span>
                          </div>
                          <div className="mt-1 truncate text-xs text-slate-500">
                            {config.taskId ?? config.projectSlug}
                            {config.remoteHost ? ` / ${config.remoteHost}` : ""}
                            {config.sourceLabel ? ` / ${config.sourceLabel}` : ""}
                          </div>
                          {config.error ? (
                            <div className="mt-1 text-xs text-rose-800">
                              {config.error}
                            </div>
                          ) : null}
                        </div>
                        <CommandButton
                          icon={<CheckCircle2 size={16} />}
                          label={activating ? "Activating" : "Activate"}
                          ariaLabel={`Activate ${displayName}`}
                          disabled={
                            !sidecarConnected ||
                            !config.valid ||
                            activatingConfigSlug !== null ||
                            configSaving ||
                            workspaceRunning ||
                            bootstrapRunning ||
                            servicesRunning ||
                            runRunning
                          }
                          onClick={() => void handleActivateConfig(config)}
                        />
                      </div>
                    );
                  })}
                </div>
              ) : null}
            </div>
          ) : null}
        <form
          className="grid grid-cols-1 gap-3 lg:grid-cols-4"
          onSubmit={(event) => {
            event.preventDefault();
            void handleSaveConfig();
          }}
        >
          <TextInput
            label="Project name"
            value={configDraft.project_name}
            onChange={(value) => handleConfigDraftChange("project_name", value)}
          />
          <TextInput
            label="Task ID"
            value={configDraft.task_id}
            onChange={(value) => handleConfigDraftChange("task_id", value)}
          />
          <TextInput
            label="Remote profile ID"
            value={configDraft.remote_profile_id}
            onChange={(value) =>
              handleConfigDraftChange("remote_profile_id", value)
            }
          />
          <TextInput
            label="Remote host"
            value={configDraft.remote_host}
            onChange={(value) => handleConfigDraftChange("remote_host", value)}
          />
          <TextInput
            label="Remote user"
            value={configDraft.remote_user}
            onChange={(value) => handleConfigDraftChange("remote_user", value)}
          />
          <NumberInput
            label="Remote port"
            value={configDraft.remote_port}
            onChange={(value) => handleConfigDraftChange("remote_port", value)}
          />
          <SelectInput
            label="Auth method"
            value={configDraft.auth_method}
            options={["ssh_agent", "private_key", "password_ref"]}
            onChange={(value) =>
              handleAuthMethodChange(
                value as OpenEvoProjectConfigDraft["auth_method"],
              )
            }
          />
          {configDraft.auth_method === "private_key" ? (
            <>
              <TextInput
                label="Private key path"
                value={configDraft.private_key_path ?? ""}
                onChange={(value) =>
                  handleConfigDraftChange("private_key_path", value || null)
                }
              />
              <TextInput
                label="Passphrase ref"
                value={configDraft.passphrase_ref ?? ""}
                onChange={(value) =>
                  handleConfigDraftChange("passphrase_ref", value || null)
                }
              />
            </>
          ) : null}
          {configDraft.auth_method === "password_ref" ? (
            <TextInput
              label="Password ref"
              value={configDraft.password_ref ?? ""}
              onChange={(value) =>
                handleConfigDraftChange("password_ref", value || null)
              }
            />
          ) : null}
          <TextInput
            label="Workspace root"
            value={configDraft.workspace_root ?? ""}
            onChange={(value) =>
              handleConfigDraftChange("workspace_root", value || null)
            }
          />
          <SelectInput
            label="Source type"
            value={configDraft.source_type}
            options={[
              "remote_path",
              "local_folder",
              "git_repository",
              "scratch",
            ]}
            onChange={(value) =>
              handleSourceTypeChange(
                value as OpenEvoProjectConfigDraft["source_type"],
              )
            }
          />
          {configDraft.source_type === "git_repository" ? (
            <>
              <TextInput
                label="Source URL"
                value={configDraft.source_url ?? ""}
                onChange={(value) =>
                  handleConfigDraftChange("source_url", value || null)
                }
              />
              <TextInput
                label="Source branch"
                value={configDraft.source_branch ?? ""}
                onChange={(value) =>
                  handleConfigDraftChange("source_branch", value || null)
                }
              />
            </>
          ) : configDraft.source_type === "scratch" ? null : (
            <TextInput
              label="Source path"
              value={configDraft.source_path ?? ""}
              onChange={(value) =>
                handleConfigDraftChange("source_path", value || null)
              }
            />
          )}
          <SelectInput
            label="Execution mode"
            value={configDraft.execution_mode}
            options={[
              "codex_subscription_transcript",
              "self-deployed",
            ]}
            onChange={(value) =>
              handleExecutionModeChange(
                value as OpenEvoProjectConfigDraft["execution_mode"],
              )
            }
          />
          {configDraft.execution_mode === "self-deployed" ? (
            <TextInput
              label="HF model"
              value={configDraft.hf_model ?? ""}
              onChange={(value) =>
                handleConfigDraftChange("hf_model", value || null)
              }
            />
          ) : null}
          <TextInput
            label="Objective"
            value={configDraft.objective}
            wide
            onChange={(value) => handleConfigDraftChange("objective", value)}
          />
          <TextInput
            label="HTTP proxy"
            value={configDraft.http_proxy ?? ""}
            onChange={(value) =>
              handleConfigDraftChange("http_proxy", value || null)
            }
          />
          <TextInput
            label="HTTPS proxy"
            value={configDraft.https_proxy ?? ""}
            onChange={(value) =>
              handleConfigDraftChange("https_proxy", value || null)
            }
          />
          <TextInput
            label="NO_PROXY"
            value={configDraft.no_proxy ?? ""}
            onChange={(value) =>
              handleConfigDraftChange("no_proxy", value || null)
            }
          />
          <TextInput
            label="PIP index URL"
            value={configDraft.pip_index_url ?? ""}
            onChange={(value) =>
              handleConfigDraftChange("pip_index_url", value || null)
            }
          />
          {configDraft.execution_mode === "self-deployed" ? (
            <>
              <TextInput
                label="Hugging Face endpoint"
                value={configDraft.huggingface_endpoint ?? ""}
                onChange={(value) =>
                  handleConfigDraftChange("huggingface_endpoint", value || null)
                }
              />
              <TextInput
                label="HF home"
                value={configDraft.hf_home ?? ""}
                onChange={(value) =>
                  handleConfigDraftChange("hf_home", value || null)
                }
              />
            </>
          ) : null}
          <div className="flex flex-wrap items-end gap-3 lg:col-span-4">
            {evolutionTargets.map((target) => (
              <CheckboxInput
                key={target.artifactType}
                label={target.displayName}
                checked={Boolean(configDraft[target.configKey])}
                testId="evolution-target"
                onChange={(checked) =>
                  handleConfigDraftChange(target.configKey, checked)
                }
              />
            ))}
            <CommandButton
              icon={<ShieldCheck size={16} />}
              label={configSaving ? "Saving" : "Save Config"}
              disabled={
                !sidecarConnected ||
                configSaving ||
                activatingConfigSlug !== null ||
                workspaceRunning ||
                bootstrapRunning ||
                servicesRunning ||
                runRunning
              }
              onClick={handleSaveConfig}
            />
          </div>
          {configError ? (
            <div className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-900 lg:col-span-4">
              {configError}
            </div>
          ) : null}
        </form>
        </div>
      </Panel>

      <section className="grid grid-cols-1 gap-4 xl:grid-cols-[1.15fr_0.85fr]">
        <Panel title="Science Project" icon={<FileText size={17} />}>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <Field label="Task source" value={model.project.source} />
            <Field label="Execution mode" value={model.execution.mode} />
            {model.execution.mode === "self-deployed" ? (
              <Field label="Model" value={model.execution.model} />
            ) : null}
            <Field label="Objective" value={model.project.objective} wide />
          </div>
        </Panel>

        <Panel title="Remote Profile" icon={<Server size={17} />}>
          <div className="grid grid-cols-1 gap-3 text-sm">
            <Field label="Profile" value={`${model.remote.id} - ${model.remote.user}`} />
            <Field label="Host" value={`${model.remote.host}:${model.remote.port}`} />
            <Field label="Auth" value={model.remote.auth.method} />
            <Field label="Transport" value={model.sidecar.transport.label} />
            <Field label="Workspace root" value={model.remote.workspaceRoot} />
            <Field label="HTTP proxy" value={model.remote.proxy.httpProxy} />
            <Field label="HTTPS proxy" value={model.remote.proxy.httpsProxy} />
            <Field label="NO_PROXY" value={model.remote.proxy.noProxy} />
            <Field label="PIP index URL" value={model.remote.proxy.pipIndexUrl} />
            {model.execution.mode === "self-deployed" ? (
              <>
                <Field
                  label="Hugging Face endpoint"
                  value={model.remote.proxy.huggingFaceEndpoint}
                />
                <Field label="HF home" value={model.remote.proxy.hfHome} />
              </>
            ) : null}
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
            {lifecycleAuthError ? (
              <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
                {lifecycleAuthError}
              </div>
            ) : null}
            {workspaceReport ? (
              <LifecycleReport title="Workspace Report" report={workspaceReport} />
            ) : null}
            {bootstrapReport ? (
              <LifecycleReport title="Bootstrap Report" report={bootstrapReport} />
            ) : null}
            {servicesReport ? (
              <LifecycleReport title="Services Report" report={servicesReport} />
            ) : null}
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
            {servicesError ? (
              <div className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-900">
                {servicesError}
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

      {latestRun ? (
        <Panel title="Run Status" icon={<Play size={17} />}>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            <Field label="State" value={latestRun.state} />
            <Field
              label="Return code"
              value={
                latestRun.returnCode === null
                  ? "pending"
                  : String(latestRun.returnCode)
              }
            />
            <Field label="Started" value={latestRun.startedAt} />
            <Field label="Finished" value={latestRun.finishedAt ?? "pending"} />
          </div>
        </Panel>
      ) : null}

      {latestRun && latestRun.state !== "running" ? (
        <Panel title="Run Artifact Timeline" icon={<FileText size={17} />}>
          <RunArtifactTimeline
            timeline={runTimeline}
            artifacts={runArtifacts}
            loading={runArtifactsLoading}
            error={runArtifactsError}
            displayNames={artifactDisplayNames}
          />
        </Panel>
      ) : null}

      {latestRun && latestRun.state !== "running" ? (
        <Panel title="Artifact Content" icon={<FileText size={17} />}>
          <ArtifactContentPanel
            content={artifactContent}
            loading={artifactContentLoading}
            error={artifactContentError}
            displayNames={artifactDisplayNames}
          />
        </Panel>
      ) : null}

      {latestRun && model.developerMode.enabled ? (
        <DiagnosticsDisclosure
          latestRun={latestRun}
          timeline={runTimeline}
          artifacts={runArtifacts}
          open={diagnosticsOpen}
          onToggle={setDiagnosticsOpen}
        />
      ) : null}

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
  ariaLabel,
  primary = false,
  disabled = false,
  onClick,
}: {
  icon: ReactNode;
  label: string;
  ariaLabel?: string;
  primary?: boolean;
  disabled?: boolean;
  onClick?: () => void;
}) {
  return (
    <button
      type="button"
      aria-label={ariaLabel}
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
      <div className="mt-1 break-words text-sm leading-6 text-slate-900">
        {value}
      </div>
    </div>
  );
}

function RunArtifactTimeline({
  timeline,
  artifacts,
  loading,
  error,
  displayNames,
}: {
  timeline: OpenEvoBackendTimelineEvent[] | null;
  artifacts: OpenEvoBackendArtifactSummary[] | null;
  loading: boolean;
  error: string | null;
  displayNames: Record<string, string>;
}) {
  return (
    <div className="space-y-4">
      {loading ? (
        <div className="rounded-md border border-blue-100 bg-blue-50 px-3 py-2 text-sm text-blue-800">
          Reading remote backend timeline and artifacts
        </div>
      ) : null}
      {error ? (
        <div className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-900">
          {error}
        </div>
      ) : null}
      {timeline || artifacts ? (
        <>
          {timeline && timeline.length > 0 ? (
            <div className="divide-y divide-slate-100 border-b border-slate-100">
              {timeline.map((event) => (
                <div
                  key={event.id}
                  className="grid grid-cols-1 gap-2 py-3 first:pt-0 lg:grid-cols-[8rem_minmax(0,1fr)]"
                >
                  <div className="text-xs font-medium uppercase text-slate-500">
                    {event.phase}
                  </div>
                  <div className="min-w-0">
                    <div className="break-words text-sm font-medium text-slate-900">
                      {event.label}
                    </div>
                    <div className="mt-1 break-words text-sm text-slate-600">
                      {event.message}
                    </div>
                    {event.artifactIds.length > 0 ? (
                      <div className="mt-1 text-xs text-slate-500">
                        {event.artifactIds.length} artifact
                        {event.artifactIds.length === 1 ? "" : "s"}
                      </div>
                    ) : null}
                  </div>
                </div>
              ))}
            </div>
          ) : null}
          {artifacts && artifacts.length > 0 ? (
            <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
              {artifacts.map((artifact) => (
                <div
                  key={artifact.id}
                  className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="break-words text-sm font-medium text-slate-900">
                      {artifact.title}
                    </span>
                    {artifact.promoted ? (
                      <span className="rounded-md bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-800">
                        Promoted
                      </span>
                    ) : (
                      <span className="rounded-md bg-slate-200 px-2 py-0.5 text-xs font-medium text-slate-700">
                        Draft
                      </span>
                    )}
                  </div>
                  <div className="mt-1 text-xs text-slate-500">
                    {displayNames[artifact.artifactType] ??
                      prettyArtifactType(artifact.artifactType)}
                  </div>
                </div>
              ))}
            </div>
          ) : !loading && !error ? (
            <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-600">
              No backend artifact records yet.
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function ArtifactIdLine({
  label,
  values,
}: {
  label: string;
  values: string[];
}) {
  if (values.length === 0) {
    return null;
  }
  return (
    <div>
      <div className="text-xs font-medium uppercase text-slate-500">{label}</div>
      <div className="mt-1 flex flex-wrap gap-1.5">
        {values.map((value) => (
          <span
            key={value}
            className="break-all rounded-md bg-slate-100 px-2 py-1 font-mono text-xs text-slate-700"
          >
            {value}
          </span>
        ))}
      </div>
    </div>
  );
}

function ArtifactContentPanel({
  content,
  loading,
  error,
  displayNames,
}: {
  content: OpenEvoBackendArtifactPreview | null;
  loading: boolean;
  error: string | null;
  displayNames: Record<string, string>;
}) {
  return (
    <div className="space-y-3">
      {loading ? (
        <div className="rounded-md border border-blue-100 bg-blue-50 px-3 py-2 text-sm text-blue-800">
          Reading promoted artifact content
        </div>
      ) : null}
      {error ? (
        <div className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-900">
          {error}
        </div>
      ) : null}
      {content ? (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="font-medium text-slate-900">
              {displayNames[content.kind] ?? prettyArtifactType(content.kind)}
            </span>
            {content.targetPath ? (
              <span className="font-mono text-xs text-slate-500">
                {content.targetPath}
              </span>
            ) : null}
          </div>
          <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm leading-6 text-slate-900">
            {content.body}
          </pre>
          {content.diff.before || content.diff.after ? (
            <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
              <DiffBlock label="Before" value={content.diff.before} />
              <DiffBlock label="After" value={content.diff.after} />
            </div>
          ) : null}
        </div>
      ) : !loading && !error ? (
        <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-600">
          No promoted artifact content available yet.
        </div>
      ) : null}
    </div>
  );
}

function DiagnosticsDisclosure({
  latestRun,
  timeline,
  artifacts,
  open,
  onToggle,
}: {
  latestRun: OpenEvoRunStatus;
  timeline: OpenEvoBackendTimelineEvent[] | null;
  artifacts: OpenEvoBackendArtifactSummary[] | null;
  open: boolean;
  onToggle: (open: boolean) => void;
}) {
  return (
    <details
      open={open}
      onToggle={(event) => onToggle(event.currentTarget.open)}
      className="rounded-lg border border-slate-200 bg-white"
    >
      <summary className="cursor-pointer px-4 py-3 text-sm font-semibold text-slate-900">
        Diagnostics
      </summary>
      {open ? (
        <div className="space-y-4 border-t border-slate-100 p-4">
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            <Field label="Run ID" value={latestRun.id} />
            <Field label="Output dir" value={latestRun.outputDir} wide />
            <Field label="Command" value={latestRun.command} wide />
            {latestRun.stdout ? (
              <LogBlock label="stdout" value={latestRun.stdout} />
            ) : null}
            {latestRun.stderr ? (
              <LogBlock label="stderr" value={latestRun.stderr} />
            ) : null}
          </div>
          {timeline && timeline.length > 0 ? (
            <div className="space-y-4">
              <div className="text-xs font-medium uppercase text-slate-500">
                Backend timeline
              </div>
              {timeline.map((event) => (
                <div key={event.id} className="border-l border-slate-200 pl-3">
                  <div className="break-words text-sm font-semibold text-slate-900">
                    {event.phase}: {event.label}
                  </div>
                  <div className="mt-1 break-words text-sm text-slate-600">
                    {event.message}
                  </div>
                  <ArtifactIdLine label="Artifacts" values={event.artifactIds} />
                </div>
              ))}
            </div>
          ) : null}
          {artifacts && artifacts.length > 0 ? (
            <div className="space-y-3">
              <div className="text-xs font-medium uppercase text-slate-500">
                Backend artifacts
              </div>
              {artifacts.map((artifact) => (
                <div key={artifact.id} className="border-l border-slate-200 pl-3">
                  <Field label="Artifact ID" value={artifact.id} />
                  <div className="mt-2 grid grid-cols-1 gap-3 lg:grid-cols-3">
                    <Field label="Run ID" value={artifact.runId} />
                    <Field label="Type" value={artifact.artifactType} />
                    <Field
                      label="Promoted"
                      value={artifact.promoted ? "true" : "false"}
                    />
                  </div>
                  <pre className="mt-2 max-h-36 overflow-auto whitespace-pre-wrap rounded-md bg-slate-50 px-3 py-2 text-xs leading-5 text-slate-700">
                    {JSON.stringify(artifact.lineage, null, 2)}
                  </pre>
                </div>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
    </details>
  );
}

function LifecycleReport({
  title,
  report,
}: {
  title: string;
  report: LifecycleReportPayload;
}) {
  const nextActions = stringArray(report.next_actions);
  const items = lifecycleReportItems(report);

  if (nextActions.length === 0 && items.length === 0) {
    return null;
  }

  return (
    <div className="border-l border-slate-200 pl-3">
      <div className="text-xs font-medium uppercase text-slate-500">{title}</div>
      {nextActions.length > 0 ? (
        <div className="mt-2 space-y-1">
          {nextActions.map((action) => (
            <div
              key={action}
              className="break-words text-sm font-medium text-slate-900"
            >
              {action}
            </div>
          ))}
        </div>
      ) : null}
      {items.length > 0 ? (
        <div className="mt-3 space-y-2">
          {items.map((item) => (
            <div
              key={`${item.kind}:${item.name}:${item.message}`}
              className={`border-l-2 pl-3 ${
                item.status === "fail" ? "border-rose-300" : "border-amber-300"
              }`}
            >
              <div className="break-words text-sm font-medium text-slate-900">
                {item.kind}: {item.name} / {item.status}
              </div>
              <div className="mt-1 break-words text-sm text-slate-600">
                {item.message}
              </div>
              {item.remediation ? (
                <div className="mt-1 break-words text-xs uppercase text-slate-500">
                  {item.remediation}
                </div>
              ) : null}
              {item.command ? (
                <div className="mt-1 break-words font-mono text-xs text-slate-500">
                  {item.command}
                </div>
              ) : null}
              {item.stderr ? (
                <div className="mt-1 break-words font-mono text-xs text-rose-800">
                  {item.stderr}
                </div>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function lifecycleReportItems(report: LifecycleReportPayload) {
  const preflight = recordValue(report.preflight);
  const workspace = recordValue(report.workspace);
  return [
    ...reportItems("Preflight", recordArray(preflight?.checks), "name"),
    ...reportItems("Workspace", recordArray(workspace?.actions), "type"),
    ...reportItems("Step", recordArray(report.steps), "id"),
  ];
}

function reportItems(
  kind: string,
  items: Record<string, unknown>[],
  nameKey: string,
) {
  return items
    .filter((item) => {
      const status = stringValue(item.status);
      return status === "fail" || status === "warn";
    })
    .map((item) => ({
      kind,
      name: stringValue(item[nameKey]) ?? "unknown",
      status: stringValue(item.status) ?? "unknown",
      message: stringValue(item.message) ?? "No message.",
      remediation: stringValue(item.remediation_kind),
      command: stringValue(item.command),
      stderr: stringValue(item.stderr),
    }));
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function recordArray(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => recordValue(item) !== null)
    : [];
}

function recordValue(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function desktopEvolutionTargets(
  capabilities: OpenEvoDesktopCapabilities | null,
  executionMode: OpenEvoProjectConfigDraft["execution_mode"],
) {
  if (!capabilities) {
    return [];
  }
  const displayNames = artifactTargetDisplayNames(capabilities);
  return capabilities.evolutionMethods
    .filter(
      (method) =>
        method.visibleInDesktop &&
        method.supportedExecutionModes.includes(executionMode),
    )
    .map((method) => ({
      artifactType: method.artifactType,
      displayName:
        displayNames[method.artifactType] ?? sentenceCase(method.displayName),
      configKey: configKeyForArtifactType(method.artifactType),
    }))
    .filter(
      (target): target is {
        artifactType: "text_memory" | "skill_bundle" | "agent_system";
        displayName: string;
        configKey: "text_memory" | "skill_bundle" | "agent_system";
      } => target.configKey !== null,
    );
}

function artifactTargetDisplayNames(
  capabilities: OpenEvoDesktopCapabilities | null,
): Record<string, string> {
  const displayNames: Record<string, string> = {};
  for (const target of capabilities?.artifactTargets ?? []) {
    if (target.visibleInDesktop) {
      displayNames[target.artifactType] = sentenceCase(target.displayName);
    }
  }
  for (const method of capabilities?.evolutionMethods ?? []) {
    if (
      method.visibleInDesktop &&
      !displayNames[method.artifactType] &&
      configKeyForArtifactType(method.artifactType)
    ) {
      displayNames[method.artifactType] = sentenceCase(
        prettyArtifactType(method.artifactType),
      );
    }
  }
  return displayNames;
}

function configKeyForArtifactType(
  artifactType: string,
): "text_memory" | "skill_bundle" | "agent_system" | null {
  if (
    artifactType === "text_memory" ||
    artifactType === "skill_bundle" ||
    artifactType === "agent_system"
  ) {
    return artifactType;
  }
  return null;
}

function firstDisplayArtifactId(
  artifacts: OpenEvoBackendArtifactSummary[],
): string | null {
  const promoted = artifacts.find((artifact) => artifact.promoted);
  return promoted?.id ?? null;
}

function prettyArtifactType(artifactType: string): string {
  return artifactType
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function sentenceCase(label: string): string {
  if (!label) {
    return label;
  }
  return label.charAt(0).toUpperCase() + label.slice(1).toLowerCase();
}

function DiffBlock({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs font-medium uppercase text-slate-500">{label}</div>
      <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded-md border border-slate-200 bg-white px-3 py-2 text-xs leading-5 text-slate-700">
        {value || "No content"}
      </pre>
    </div>
  );
}

function LogBlock({ label, value }: { label: string; value: string }) {
  return (
    <div className="lg:col-span-3">
      <div className="text-xs font-medium uppercase text-slate-500">{label}</div>
      <pre className="mt-1 max-h-36 overflow-auto rounded-md bg-slate-950 px-3 py-2 text-xs leading-5 text-slate-50">
        {value}
      </pre>
    </div>
  );
}

function TextInput({
  label,
  value,
  wide = false,
  onChange,
}: {
  label: string;
  value: string;
  wide?: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <label className={wide ? "lg:col-span-2" : undefined}>
      <span className="text-xs font-medium uppercase text-slate-500">{label}</span>
      <input
        aria-label={label}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="mt-1 h-9 w-full rounded-md border border-slate-200 bg-white px-2 text-sm text-slate-900"
      />
    </label>
  );
}

function NumberInput({
  label,
  value,
  onChange,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
}) {
  return (
    <label>
      <span className="text-xs font-medium uppercase text-slate-500">{label}</span>
      <input
        aria-label={label}
        type="number"
        min={1}
        max={65535}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
        className="mt-1 h-9 w-full rounded-md border border-slate-200 bg-white px-2 text-sm text-slate-900"
      />
    </label>
  );
}

function SelectInput({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
}) {
  return (
    <label>
      <span className="text-xs font-medium uppercase text-slate-500">{label}</span>
      <select
        aria-label={label}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="mt-1 h-9 w-full rounded-md border border-slate-200 bg-white px-2 text-sm text-slate-900"
      >
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    </label>
  );
}

function CheckboxInput({
  label,
  checked,
  testId,
  onChange,
}: {
  label: string;
  checked: boolean;
  testId?: string;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label
      data-testid={testId}
      className="inline-flex h-9 items-center gap-2 rounded-md border border-slate-200 bg-white px-3 text-sm text-slate-700"
    >
      <input
        aria-label={label}
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span>{label}</span>
    </label>
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

function unsupportedLifecycleAuthMessage(
  model: OpenEvoDesktopShellModel,
): string | null {
  const { auth } = model.remote;
  const { transport } = model.sidecar;
  if (auth.method === "password_ref" && !transport.supportsPasswordRef) {
    return (
      `${transport.label} cannot resolve password_ref yet. ` +
      "Use SSH agent or a private key without a secret reference."
    );
  }
  if (auth.passphraseRef !== null && !transport.supportsPassphraseRef) {
    return (
      `${transport.label} cannot resolve passphrase_ref yet. ` +
      "Use SSH agent or a private key without a secret reference."
    );
  }
  return null;
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
