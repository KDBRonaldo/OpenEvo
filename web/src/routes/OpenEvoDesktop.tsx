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
  fetchOpenEvoProjectConfigs,
  fetchOpenEvoDesktopShellModel,
  pollOpenEvoRunStatus,
  runOpenEvoBootstrap,
  runOpenEvoStartRun,
  runOpenEvoWorkspaceSync,
  saveOpenEvoProjectConfig,
  type OpenEvoProjectConfigDraft,
  type OpenEvoRunStatus,
  type OpenEvoSavedProjectConfig,
} from "../api/openevo";
import {
  type EvolutionStepState,
  type OpenEvoDesktopShellModel,
  type RemoteServiceState,
  getOpenEvoDesktopShellModel,
  getOpenEvoTimelineSummary,
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

export function OpenEvoDesktop() {
  const [model, setModel] = useState(() => getOpenEvoDesktopShellModel());
  const [sidecarConnected, setSidecarConnected] = useState(false);
  const [workspaceRunning, setWorkspaceRunning] = useState(false);
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const [bootstrapRunning, setBootstrapRunning] = useState(false);
  const [bootstrapError, setBootstrapError] = useState<string | null>(null);
  const [runRunning, setRunRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [latestRun, setLatestRun] = useState<OpenEvoRunStatus | null>(null);
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
    draftFromModel(getOpenEvoDesktopShellModel()),
  );
  const mounted = useRef(true);
  const catalogRefreshGeneration = useRef(0);
  const runPollGeneration = useRef(0);
  const runPollTimer = useRef<number | null>(null);
  const summary = getOpenEvoTimelineSummary(model);

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

    fetchOpenEvoDesktopShellModel()
      .then((nextModel) => {
        if (!cancelled) {
          setModel(nextModel);
          setConfigDraft(draftFromModel(nextModel));
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
  };

  const handleWorkspaceSync = async () => {
    setWorkspaceRunning(true);
    setWorkspaceError(null);
    try {
      const response = await runOpenEvoWorkspaceSync();
      setModel(response.status);
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

  const handleSaveConfig = async () => {
    setConfigSaving(true);
    setConfigError(null);
    const submittedDraft = configDraft;
    try {
      const response = await saveOpenEvoProjectConfig(submittedDraft);
      setModel(response.status);
      setConfigDraft(submittedDraft);
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
      setConfigDraft(draftFromModel(response.status));
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
    setBootstrapRunning(true);
    setBootstrapError(null);
    try {
      const response = await runOpenEvoBootstrap();
      setModel(response.status);
      clearLatestRunForContextChange();
      setSidecarConnected(true);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Bootstrap failed";
      setBootstrapError(message);
    } finally {
      setBootstrapRunning(false);
    }
  };

  const handleStartRun = async () => {
    invalidateRunPolling();
    const generation = runPollGeneration.current;
    setRunRunning(true);
    setRunError(null);
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
            <span>transcript evolution</span>
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
              runRunning ||
              configSaving ||
              activatingConfigSlug !== null
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
              runRunning ||
              configSaving ||
              activatingConfigSlug !== null
            }
            onClick={handleBootstrap}
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
              configSaving ||
              activatingConfigSlug !== null
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
          detail="subscription transcript mode"
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
          <TextInput
            label="Codex model"
            value={configDraft.codex_model}
            onChange={(value) => handleConfigDraftChange("codex_model", value)}
          />
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
          <div className="flex flex-wrap items-end gap-3 lg:col-span-4">
            <CheckboxInput
              label="Text memory"
              checked={configDraft.text_memory}
              onChange={(checked) =>
                handleConfigDraftChange("text_memory", checked)
              }
            />
            <CheckboxInput
              label="Skill bundle"
              checked={configDraft.skill_bundle}
              onChange={(checked) =>
                handleConfigDraftChange("skill_bundle", checked)
              }
            />
            <CheckboxInput
              label="Agent system"
              checked={configDraft.agent_system}
              onChange={(checked) =>
                handleConfigDraftChange("agent_system", checked)
              }
            />
            <CommandButton
              icon={<ShieldCheck size={16} />}
              label={configSaving ? "Saving" : "Save Config"}
              disabled={
                !sidecarConnected ||
                configSaving ||
                activatingConfigSlug !== null ||
                workspaceRunning ||
                bootstrapRunning ||
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
            <Field label="Codex model" value={model.execution.model} />
            <Field label="Objective" value={model.project.objective} wide />
          </div>
        </Panel>

        <Panel title="Remote Profile" icon={<Server size={17} />}>
          <div className="grid grid-cols-1 gap-3 text-sm">
            <Field label="Profile" value={`${model.remote.id} - ${model.remote.user}`} />
            <Field label="Host" value={`${model.remote.host}:${model.remote.port}`} />
            <Field label="Auth" value={model.remote.auth.method} />
            <Field label="Workspace root" value={model.remote.workspaceRoot} />
            <Field label="HTTP proxy" value={model.remote.proxy.httpProxy} />
            <Field label="HTTPS proxy" value={model.remote.proxy.httpsProxy} />
            <Field label="NO_PROXY" value={model.remote.proxy.noProxy} />
            <Field label="PIP index URL" value={model.remote.proxy.pipIndexUrl} />
            <Field
              label="Hugging Face endpoint"
              value={model.remote.proxy.huggingFaceEndpoint}
            />
            <Field label="HF home" value={model.remote.proxy.hfHome} />
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
            <Field label="Run ID" value={latestRun.id} />
            <Field label="State" value={latestRun.state} />
            <Field
              label="Return code"
              value={
                latestRun.returnCode === null
                  ? "pending"
                  : String(latestRun.returnCode)
              }
            />
            <Field label="Output dir" value={latestRun.outputDir} wide />
            <Field label="Started" value={latestRun.startedAt} />
            <Field label="Finished" value={latestRun.finishedAt ?? "pending"} />
            {latestRun.stdout ? (
              <LogBlock label="stdout" value={latestRun.stdout} />
            ) : null}
            {latestRun.stderr ? (
              <LogBlock label="stderr" value={latestRun.stderr} />
            ) : null}
          </div>
        </Panel>
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
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="inline-flex h-9 items-center gap-2 rounded-md border border-slate-200 bg-white px-3 text-sm text-slate-700">
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

function draftFromModel(
  model: OpenEvoDesktopShellModel,
): OpenEvoProjectConfigDraft {
  const source = sourceDraftFromLabel(model.project.source);
  return {
    project_name: model.project.name,
    task_id: model.project.taskId,
    objective: model.project.objective,
    source_type: source.source_type,
    source_path: source.source_path,
    source_url: source.source_url,
    source_branch: source.source_branch,
    remote_profile_id: model.remote.id,
    remote_host: model.remote.host,
    remote_port: model.remote.port,
    remote_user: model.remote.user,
    auth_method: model.remote.auth.method,
    private_key_path: model.remote.auth.privateKeyPath,
    password_ref: model.remote.auth.passwordRef,
    passphrase_ref: model.remote.auth.passphraseRef,
    workspace_root: model.remote.workspaceRoot,
    http_proxy: optionalConfigured(model.remote.proxy.httpProxy),
    https_proxy: optionalConfigured(model.remote.proxy.httpsProxy),
    no_proxy: optionalConfigured(model.remote.proxy.noProxy),
    pip_index_url: optionalConfigured(model.remote.proxy.pipIndexUrl),
    huggingface_endpoint: optionalConfigured(model.remote.proxy.huggingFaceEndpoint),
    hf_home: optionalConfigured(model.remote.proxy.hfHome),
    codex_model: model.execution.model || "gpt-5.1-codex-mini",
    text_memory: model.evolution.some((step) =>
      ["text-memory", "memory"].includes(step.id),
    ),
    skill_bundle: model.evolution.some((step) =>
      ["skill-bundle", "skills"].includes(step.id),
    ),
    agent_system: model.evolution.some((step) => step.id === "agent-system"),
  };
}

function sourceDraftFromLabel(
  label: string,
): Pick<
  OpenEvoProjectConfigDraft,
  "source_type" | "source_path" | "source_url" | "source_branch"
> {
  if (label.startsWith("Remote path: ")) {
    return {
      source_type: "remote_path",
      source_path: label.slice("Remote path: ".length),
      source_url: null,
      source_branch: null,
    };
  }
  if (label.startsWith("Local folder: ")) {
    return {
      source_type: "local_folder",
      source_path: label.slice("Local folder: ".length),
      source_url: null,
      source_branch: null,
    };
  }
  if (label.startsWith("Git repository: ")) {
    const value = label.slice("Git repository: ".length);
    const match = value.match(/^(.*) \((.*)\)$/);
    return {
      source_type: "git_repository",
      source_path: null,
      source_url: match ? match[1] : value,
      source_branch: match ? match[2] : null,
    };
  }
  return {
    source_type: "scratch",
    source_path: null,
    source_url: null,
    source_branch: null,
  };
}

function optionalConfigured(value: string): string | null {
  return value === "not configured" ? null : value;
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
