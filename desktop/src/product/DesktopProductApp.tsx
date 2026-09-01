import {
  Activity,
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  BookOpen,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleDot,
  Download,
  EllipsisVertical,
  FolderTree,
  FolderOpen,
  FileText,
  FolderUp,
  History,
  Image as ImageIcon,
  LoaderCircle,
  Play,
  Plus,
  RefreshCw,
  Search,
  Server,
  Settings,
  ShieldCheck,
  Sparkles,
  Trash2,
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
  type CSSProperties,
  type ClipboardEvent as ReactClipboardEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import { DesktopApiErrorV2 } from "../api/v2/client";
import type { LogEntryV2 } from "../api/v2/logs";
import type {
  DesktopErrorV2,
  ProjectHeadRefV2,
  ProjectV2,
  RemoteProfileV2,
  RemoteWorkspaceProfileV2,
  ScienceProjectConfigV2,
  SuccessorTransitionV2,
  TaskV2,
} from "../api/v2/schemas";
import {
  LifecycleOperationPanelV2,
  coreOperationPanelModelV2,
  diagnosticPanelModelV2,
  lifecycleOperationPanelModelV2,
} from "./LifecycleOperationPanelV2";
import {
  unavailableDesktopProductProviderV2,
  withoutProjectV2,
  withoutTaskV2,
  withoutWorkspaceFileV2,
  type DesktopProductProviderV2,
  type DesktopProductSnapshotV2,
  type ProductMutationIntentV2,
} from "./providerV2";

type Workspace = "research" | "evolution";

type StartingSessionV2 = {
  readonly projectId: string;
  readonly projectHeadGeneration: number;
  readonly task: ScienceProjectConfigV2["task"];
  readonly phase: "validating" | "admitting";
};

type BrowserWorkspaceUploadV2 = {
  readonly file: File;
  readonly path: string;
};

type SessionImageAttachmentV2 = {
  readonly id: string;
  readonly file: File;
  readonly path: string;
  readonly previewUrl: string | null;
};

type BrowserFolderSelectionV2 = {
  readonly displayName: string;
  readonly uploads: readonly BrowserWorkspaceUploadV2[];
};

type WorkspaceUploadStateV2 = {
  readonly id: string;
  readonly projectId: string;
  readonly path: string;
  readonly percentage: number;
  readonly status: "queued" | "uploading" | "uploaded" | "failed";
  readonly error: string | null;
};

type DeleteConfirmationV2 =
  | { readonly kind: "project"; readonly project: ProjectV2 }
  | { readonly kind: "session"; readonly task: TaskV2; readonly title: string }
  | { readonly kind: "file"; readonly projectId: string; readonly path: string };

const PROJECT_PANE_WIDTH_KEY = "openevo.desktop.layout.project-pane-width";
const SESSION_PANE_WIDTH_KEY = "openevo.desktop.layout.session-pane-width";
const SESSION_INSPECTOR_WIDTH_KEY = "openevo.desktop.layout.session-inspector-width-v2";
const PROJECT_SESSION_SCROLLS_KEY = "openevo.desktop.navigation.project-session-scrolls";
const WORKSPACE_UPLOAD_MINIMUM_VISIBLE_MS = 1_400;
const BROWSER_FOLDER_SNAPSHOT_FEATURE = "browser_folder_snapshot";
const BROWSER_FOLDER_MAX_FILES = 1_000;
const BROWSER_FOLDER_MAX_FILE_BYTES = 32 * 1024 * 1024;
const BROWSER_FOLDER_MAX_TOTAL_BYTES = 512 * 1024 * 1024;
const SESSION_IMAGE_MAX_COUNT = 8;
const SESSION_IMAGE_MAX_TOTAL_BYTES = 64 * 1024 * 1024;
const SESSION_IMAGE_MEDIA_EXTENSIONS: Readonly<Record<string, string>> = {
  "image/png": "png",
  "image/jpeg": "jpg",
  "image/webp": "webp",
  "image/gif": "gif",
};
const PROJECT_TASK_PLACEHOLDER = {
  title: "Untitled Session",
  objective: "Task details are provided when the Session starts.",
} satisfies ScienceProjectConfigV2["task"];

function readPersistedRecord(storageKey: string): Record<string, string> {
  try {
    const parsed = JSON.parse(globalThis.localStorage?.getItem(storageKey) ?? "{}") as unknown;
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return Object.fromEntries(Object.entries(parsed).filter((entry): entry is [string, string] => (
      typeof entry[1] === "string"
    )));
  } catch {
    return {};
  }
}

function persistRecord(storageKey: string, value: Readonly<Record<string, string>>): void {
  try {
    globalThis.localStorage?.setItem(storageKey, JSON.stringify(value));
  } catch {
    // Navigation persistence is optional when browser storage is unavailable.
  }
}

function compareTasksNewestFirst(left: TaskV2, right: TaskV2): number {
  return right.updated_at.localeCompare(left.updated_at) || right.task_id.localeCompare(left.task_id);
}

function activeTaskIdV2(projectId: string, tasks: readonly TaskV2[]): string | null {
  return tasks.find((task) => (
    task.project_id === projectId
      && ["admitted", "preparing", "running", "cancelling", "waiting_for_successor"].includes(task.state)
  ))?.task_id ?? null;
}

function evolutionIsActiveV2(snapshot: DesktopProductSnapshotV2, projectId?: string): boolean {
  if (snapshot.runtimePresentation?.evolutionRuns?.some((run) => (
    run.state === "running" && (projectId === undefined || run.projectId === projectId)
  ))) return true;

  const projectTaskIds = projectId === undefined
    ? null
    : new Set(snapshot.tasks.filter((task) => task.project_id === projectId).map((task) => task.task_id));
  return Object.entries(snapshot.runtimePresentation?.tasks ?? {}).some(([taskId, presentation]) => (
    (projectTaskIds === null || projectTaskIds.has(taskId))
      && presentation.evolutionJobs?.some((job) => job.state === "queued" || job.state === "running") === true
  ));
}

function taskStateLabelV2(state: TaskV2["state"]): string {
  const labels: Record<TaskV2["state"], string> = {
    admitted: "Admitted",
    preparing: "Preparing",
    running: "Running",
    cancelling: "Cancelling",
    completed: "Completed",
    failed: "Failed",
    cancelled: "Cancelled",
    closed: "Completed",
    waiting_for_successor: "Waiting for evolution",
  };
  return labels[state];
}

function clampPaneWidth(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, Math.round(value)));
}

function usePersistedPaneWidth(
  storageKey: string,
  initialWidth: number,
  minimum: number,
  maximum: number,
): readonly [number, (width: number) => void] {
  const [width, setWidth] = useState(() => {
    try {
      const stored = Number.parseInt(globalThis.localStorage?.getItem(storageKey) ?? "", 10);
      return Number.isFinite(stored)
        ? clampPaneWidth(stored, minimum, maximum)
        : initialWidth;
    } catch {
      return initialWidth;
    }
  });
  const updateWidth = useCallback((nextWidth: number) => {
    const normalized = clampPaneWidth(nextWidth, minimum, maximum);
    setWidth(normalized);
    try {
      globalThis.localStorage?.setItem(storageKey, String(normalized));
    } catch {
      // Layout persistence is a convenience; browser privacy settings may disable it.
    }
  }, [maximum, minimum, storageKey]);
  return [width, updateWidth] as const;
}

function VerticalResizeHandle({
  label,
  value,
  defaultValue,
  minimum,
  maximum,
  onChange,
  direction = 1,
  edge = "right",
}: {
  readonly label: string;
  readonly value: number;
  readonly defaultValue: number;
  readonly minimum: number;
  readonly maximum: number;
  readonly onChange: (width: number) => void;
  readonly direction?: 1 | -1;
  readonly edge?: "left" | "right";
}) {
  const drag = useRef<{ readonly pointerId: number; readonly startX: number; readonly startWidth: number } | null>(null);
  useEffect(() => () => {
    if (drag.current) document.body.classList.remove("product-pane-resizing");
  }, []);
  const finish = (target: HTMLDivElement, pointerId: number): void => {
    if (drag.current?.pointerId !== pointerId) return;
    drag.current = null;
    target.releasePointerCapture?.(pointerId);
    document.body.classList.remove("product-pane-resizing");
  };
  return (
    <div
      className={`product-pane-resizer edge-${edge}`}
      role="separator"
      aria-label={label}
      aria-orientation="vertical"
      aria-valuemin={minimum}
      aria-valuemax={maximum}
      aria-valuenow={value}
      tabIndex={0}
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        drag.current = { pointerId: event.pointerId, startX: event.clientX, startWidth: value };
        event.currentTarget.setPointerCapture?.(event.pointerId);
        document.body.classList.add("product-pane-resizing");
        event.preventDefault();
      }}
      onPointerMove={(event) => {
        const activeDrag = drag.current;
        if (activeDrag?.pointerId !== event.pointerId) return;
        onChange(activeDrag.startWidth + ((event.clientX - activeDrag.startX) * direction));
      }}
      onPointerUp={(event) => finish(event.currentTarget, event.pointerId)}
      onPointerCancel={(event) => finish(event.currentTarget, event.pointerId)}
      onDoubleClick={() => onChange(defaultValue)}
      onKeyDown={(event) => {
        const step = event.shiftKey ? 32 : 12;
        if (event.key === "ArrowLeft") onChange(value - (step * direction));
        else if (event.key === "ArrowRight") onChange(value + (step * direction));
        else if (event.key === "Home") onChange(minimum);
        else if (event.key === "End") onChange(maximum);
        else return;
        event.preventDefault();
      }}
    ><span /></div>
  );
}

function withSessionDocumentEvolution(
  config: ScienceProjectConfigV2,
  task: ScienceProjectConfigV2["task"],
  targets: ScienceProjectConfigV2["evolution"]["targets"],
): ScienceProjectConfigV2 {
  return { ...config, task, evolution: { targets } } as ScienceProjectConfigV2;
}

function availableProjectHeadsV2(
  project: ProjectV2,
  tasks: readonly TaskV2[],
): readonly ProjectHeadRefV2[] {
  const heads = new Map<string, ProjectHeadRefV2>();
  if (project.active_project_head !== null) {
    heads.set(project.active_project_head.project_head_id, project.active_project_head);
  }
  for (const task of tasks) {
    if (task.project_id !== project.project_id) continue;
    const head = task.admission.predecessor_project_head;
    heads.set(head.project_head_id, head);
  }
  return [...heads.values()].sort((left, right) => right.generation - left.generation);
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
  const [switchingProjectId, setSwitchingProjectId] = useState<string | null>(null);
  const [workspace, setWorkspace] = useState<Workspace>("research");
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [newSessionDraftKey, setNewSessionDraftKey] = useState(0);
  const [startingSession, setStartingSession] = useState<StartingSessionV2 | null>(null);
  const [selectedWorkspacePath, setSelectedWorkspacePath] = useState<string | null>(null);
  const [deleteConfirmation, setDeleteConfirmation] = useState<DeleteConfirmationV2 | null>(null);
  const [workspaceUploads, setWorkspaceUploads] = useState<readonly WorkspaceUploadStateV2[]>([]);
  const [projectPaneWidth, setProjectPaneWidth] = usePersistedPaneWidth(
    PROJECT_PANE_WIDTH_KEY,
    248,
    180,
    440,
  );
  const [sessionPaneWidth, setSessionPaneWidth] = usePersistedPaneWidth(
    SESSION_PANE_WIDTH_KEY,
    232,
    180,
    420,
  );
  const [connectionOpen, setConnectionOpen] = useState(false);
  const [projectOpen, setProjectOpen] = useState(false);
  const [projectEditing, setProjectEditing] = useState(false);
  const [taskLogs, setTaskLogs] = useState<Readonly<Record<string, readonly LogEntryV2[]>>>({});
  const readyReported = useRef(false);
  const initialFailureReported = useRef(false);
  const refreshRequestSequence = useRef(0);
  const refreshInFlight = useRef<Promise<DesktopProductSnapshotV2 | null> | null>(null);
  const snapshotRef = useRef<DesktopProductSnapshotV2 | null>(null);
  const projectRecoveryRefreshInFlight = useRef(false);
  const resolvedSelectionProjectId = useRef<string | null>(null);

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
              const error = new Error("EvoLab state is not currently authoritative.");
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
    const activeProjectId = snapshot?.state.active_project_id ?? null;
    if (activeProjectId === null || switchingProjectId !== null) return;
    if (resolvedSelectionProjectId.current === activeProjectId) return;
    resolvedSelectionProjectId.current = activeProjectId;
    setSelectedTaskId(activeTaskIdV2(activeProjectId, snapshot?.tasks ?? []));
    setNewSessionDraftKey((current) => current + 1);
  }, [snapshot?.state.active_project_id, snapshot?.tasks, switchingProjectId]);

  useEffect(() => {
    if (actionStatus === null) return;
    const timer = globalThis.setTimeout(() => setActionStatus(null), 3_200);
    return () => globalThis.clearTimeout(timer);
  }, [actionStatus]);

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
      />
    );
  }

  const activeProject = snapshot.projects.find(
    (project) => project.project_id === snapshot.state.active_project_id,
  ) ?? null;
  const displayedProject = snapshot.projects.find(
    (project) => project.project_id === (switchingProjectId ?? snapshot.state.active_project_id),
  ) ?? activeProject;
  const connectedProfiles = snapshot.profiles.filter(isConnectedProfile);
  const generation = displayedProject?.active_project_head?.generation ?? 0;
  const displayedProjectTasks = displayedProject === null ? [] : snapshot.tasks
    .filter((task) => task.project_id === displayedProject.project_id)
    .sort(compareTasksNewestFirst);
  const displayedWorkspace = displayedProject === null
    ? undefined
    : snapshot.runtimePresentation?.workspaces?.[displayedProject.project_id];
  const displayedWorkspaceUploads = displayedProject === null
    ? []
    : workspaceUploads.filter((upload) => upload.projectId === displayedProject.project_id);
  const selectedWorkspaceEntry = displayedWorkspace?.entries.find(
    (entry) => entry.kind === "file" && entry.path === selectedWorkspacePath,
  ) ?? null;
  const lifecycleStates = provider.listLifecycleOperations();
  const coreOperations = provider.listCoreOperations();
  const diagnostics = provider.listDiagnostics();
  const mutationIntents = provider.listMutationIntents();
  const visibleOperationCount = lifecycleStates.length + coreOperations.length + diagnostics.length;
  const developmentAgentBridge = provider.featureFlags.includes("development_agent_bridge");
  const developmentEvolutionActive = developmentAgentBridge && evolutionIsActiveV2(snapshot);
  const sessionEvolutionAvailable = !developmentAgentBridge && (
    displayedProject !== null && snapshot.capability?.project_id === displayedProject.project_id
  );
  const runProject = async (
    project: ProjectV2,
    task: ScienceProjectConfigV2["task"],
    selectedEvolutionTargets: ScienceProjectConfigV2["evolution"]["targets"],
    selectedProjectHead: ProjectHeadRefV2,
    imageAttachments: readonly SessionImageAttachmentV2[] = [],
  ): Promise<boolean> => {
    if (project.state !== "ready") return false;
    setWorkspace("research");
    setSelectedWorkspacePath(null);
    setSelectedTaskId(null);
    setStartingSession({
      projectId: project.project_id,
      projectHeadGeneration: selectedProjectHead.generation,
      task,
      phase: "validating",
    });
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
      if (imageAttachments.length > 0) {
        if (!provider.uploadWorkspaceFile) {
          throw new Error("This backend does not support pasted image uploads.");
        }
        for (const attachment of imageAttachments) {
          await provider.uploadWorkspaceFile(
            currentProject.project_id,
            {
              path: attachment.path,
              data: attachment.file,
              mediaType: attachment.file.type,
              overwrite: true,
            },
            intentFor(currentSnapshot, "upload-session-image"),
          );
          const refreshed = await refresh();
          if (refreshed === null) {
            throw new Error("The pasted image was uploaded, but the current project state could not be reloaded.");
          }
          const refreshedProject = refreshed.projects.find((candidate) => candidate.project_id === project.project_id);
          if (!refreshedProject || refreshedProject.state !== "ready") {
            throw new Error("The project is not ready after uploading the pasted image.");
          }
          currentSnapshot = refreshed;
          currentProject = refreshedProject;
        }
      }
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
        return false;
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
      const admissionHead = availableProjectHeadsV2(currentProject, currentSnapshot.tasks)
        .find((head) => head.project_head_id === selectedProjectHead.project_head_id);
      if (admissionHead === undefined) {
        throw new Error("The selected Project Head is no longer available for this Session.");
      }
      setStartingSession({
        projectId: currentProject.project_id,
        projectHeadGeneration: admissionHead.generation,
        task,
        phase: "admitting",
      });
      const submittedTask = await provider.submitTask(
        currentProject.project_id,
        intentFor(currentSnapshot, "submit-task"),
        admissionHead,
      );
      const submittedSnapshot = await refresh();
      if (submittedSnapshot === null || !submittedSnapshot.tasks.some((candidate) => candidate.task_id === submittedTask.task_id)) {
        throw new Error("The admitted Session was not visible in the refreshed remote state.");
      }
      setSelectedTaskId(submittedTask.task_id);
      setActionStatus("Session started. Its completed transcript can be used in future Evolution runs.");
      return true;
    } catch (error) {
      setActionError(userMessageV2(error));
      await refresh();
      return false;
    } finally {
      setStartingSession(null);
    }
  };

  const selectProject = (projectId: string): void => {
    const project = snapshot.projects.find((candidate) => candidate.project_id === projectId);
    if (!project || switchingProjectId !== null) return;
    setSelectedWorkspacePath(null);
    setWorkspace("research");
    setSelectedTaskId(activeTaskIdV2(projectId, snapshot.tasks));
    setNewSessionDraftKey((current) => current + 1);
    if (project.project_id === activeProject?.project_id) {
      return;
    }
    setSwitchingProjectId(projectId);
    setActionError(null);
    setActionStatus(null);
    void (async () => {
      try {
        await provider.activateProject(project.project_id, intentFor(snapshot, "activate-project"));
        const refreshed = await refresh();
        if (refreshed === null || refreshed.state.active_project_id !== projectId) {
          throw new Error("The selected Project has not synchronized yet. Please try again.");
        }
        resolvedSelectionProjectId.current = projectId;
        setSelectedTaskId(activeTaskIdV2(projectId, refreshed.tasks));
      } catch (error) {
        setActionError(userMessageV2(error));
        resolvedSelectionProjectId.current = null;
        await refresh();
      } finally {
        setSwitchingProjectId(null);
      }
    })();
  };

  const uploadWorkspaceFiles = (uploads: readonly BrowserWorkspaceUploadV2[], overwrite: boolean): void => {
    if (displayedProject === null) return;
    const visibleSince = Date.now();
    const projectId = displayedProject.project_id;
    const pending = uploads.map((upload): WorkspaceUploadStateV2 => ({
      id: actionIdV2("workspace-upload-row"),
      projectId,
      path: upload.path,
      percentage: 0,
      status: "queued",
      error: null,
    }));
    const pendingByPath = new Map(pending.map((upload) => [upload.path, upload]));
    const updateUpload = (id: string, update: Partial<WorkspaceUploadStateV2>): void => {
      setWorkspaceUploads((current) => current.map((upload) => (
        upload.id === id ? { ...upload, ...update } : upload
      )));
    };
    setWorkspaceUploads((current) => [
      ...current.filter((upload) => upload.projectId !== projectId || !pendingByPath.has(upload.path)),
      ...pending,
    ]);
    setBusy(true);
    setActionError(null);
    setActionStatus(null);
    void (async () => {
      const failed: string[] = [];
      try {
        if (!provider.uploadWorkspaceFile) throw new Error("This backend does not support workspace uploads.");
        for (const [index, upload] of uploads.entries()) {
          const uploadState = pending[index]!;
          updateUpload(uploadState.id, { status: "uploading" });
          try {
            await provider.uploadWorkspaceFile(
              projectId,
              {
                path: upload.path,
                data: upload.file,
                mediaType: upload.file.type || "application/octet-stream",
                overwrite,
                onProgress: (percentage) => updateUpload(uploadState.id, {
                  percentage: Math.round(Math.min(100, Math.max(0, percentage))),
                }),
              },
              intentFor(snapshot, "upload-workspace-file"),
            );
            updateUpload(uploadState.id, { percentage: 100, status: "uploaded" });
          } catch (error) {
            const message = userMessageV2(error);
            failed.push(`${upload.path}: ${message}`);
            updateUpload(uploadState.id, { status: "failed", error: message });
          }
        }
        const refreshed = await refresh();
        if (refreshed !== null) {
          const remainingVisibleMs = WORKSPACE_UPLOAD_MINIMUM_VISIBLE_MS - (Date.now() - visibleSince);
          if (remainingVisibleMs > 0) {
            await new Promise<void>((resolve) => globalThis.setTimeout(resolve, remainingVisibleMs));
          }
          setWorkspaceUploads((current) => current.filter((upload) => (
            upload.projectId !== projectId || upload.status !== "uploaded"
          )));
        }
        if (failed.length > 0) {
          setActionError(`${failed.length} workspace file${failed.length === 1 ? "" : "s"} failed to upload. ${failed[0]}`);
        } else {
          setActionStatus(`${uploads.length} workspace file${uploads.length === 1 ? "" : "s"} uploaded to the remote server.`);
        }
      } catch (error) {
        const message = userMessageV2(error);
        setWorkspaceUploads((current) => current.map((upload) => (
          pending.some((candidate) => candidate.id === upload.id) && upload.status === "queued"
            ? { ...upload, status: "failed", error: message }
            : upload
        )));
        setActionError(message);
        await refresh();
      } finally {
        setBusy(false);
      }
    })();
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

  const deleteProject = (project: ProjectV2): void => {
    if (!provider.deleteProject) return;
    setDeleteConfirmation({ kind: "project", project });
  };

  const confirmDeleteProject = (project: ProjectV2): void => {
    void (async () => {
      setBusy(true);
      setActionError(null);
      setActionStatus(null);
      try {
        await provider.deleteProject!(project.project_id, intentFor(snapshot, "delete-project"));
        const current = snapshotRef.current;
        if (current !== null) {
          const next = withoutProjectV2(current, project.project_id);
          snapshotRef.current = next;
          setSnapshot(next);
          setSelectedWorkspacePath(null);
          setWorkspace("research");
          const nextProjectId = next.state.active_project_id;
          setSelectedTaskId(nextProjectId === null ? null : activeTaskIdV2(nextProjectId, next.tasks));
        }
        setActionStatus(`${project.display_name} deleted.`);
      } catch (error) {
        setActionError(userMessageV2(error));
        void refresh();
      } finally {
        setBusy(false);
      }
    })();
  };

  const deleteSession = (task: TaskV2, title: string): void => {
    if (!provider.deleteTask) return;
    setDeleteConfirmation({ kind: "session", task, title });
  };

  const confirmDeleteSession = (task: TaskV2, title: string): void => {
    void (async () => {
      setBusy(true);
      setActionError(null);
      setActionStatus(null);
      try {
        await provider.deleteTask!(task.task_id, intentFor(snapshot, "delete-session"));
        const current = snapshotRef.current;
        if (current !== null) {
          const next = withoutTaskV2(current, task.task_id);
          snapshotRef.current = next;
          setSnapshot(next);
        }
        if (selectedTaskId === task.task_id) setSelectedTaskId(null);
        setActionStatus(`${title} deleted.`);
      } catch (error) {
        setActionError(userMessageV2(error));
        void refresh();
      } finally {
        setBusy(false);
      }
    })();
  };

  const deleteWorkspaceFile = (path: string): void => {
    if (displayedProject === null || !provider.deleteWorkspaceFile) return;
    setDeleteConfirmation({ kind: "file", projectId: displayedProject.project_id, path });
  };

  const confirmDeleteWorkspaceFile = (projectId: string, path: string): void => {
    void (async () => {
      setBusy(true);
      setActionError(null);
      setActionStatus(null);
      try {
        await provider.deleteWorkspaceFile!(
          projectId,
          path,
          intentFor(snapshot, "delete-workspace-file"),
        );
        const current = snapshotRef.current;
        if (current !== null) {
          const next = withoutWorkspaceFileV2(current, projectId, path);
          snapshotRef.current = next;
          setSnapshot(next);
        }
        if (selectedWorkspacePath === path) setSelectedWorkspacePath(null);
        setActionStatus(`${path} deleted from the remote workspace.`);
      } catch (error) {
        setActionError(userMessageV2(error));
        void refresh();
      } finally {
        setBusy(false);
      }
    })();
  };

  const confirmDeletion = (): void => {
    if (deleteConfirmation === null || busy) return;
    const confirmed = deleteConfirmation;
    setDeleteConfirmation(null);
    if (confirmed.kind === "project") {
      confirmDeleteProject(confirmed.project);
    } else if (confirmed.kind === "session") {
      confirmDeleteSession(confirmed.task, confirmed.title);
    } else {
      confirmDeleteWorkspaceFile(confirmed.projectId, confirmed.path);
    }
  };

  return (
    <div
      className="product-shell product-v2-shell"
      data-provider-kind="desktop_sidecar"
      data-api-version="2"
      style={{
        "--project-pane-width": `${projectPaneWidth}px`,
        "--session-pane-width": `${sessionPaneWidth}px`,
      } as CSSProperties}
    >
      <aside className="product-activitybar" aria-label="Primary navigation">
        <div className="product-brand" aria-label="EvoLab" title="EvoLab"><span>EvoLab</span></div>
        <nav className="product-nav" aria-label="Workspace views">
          <WorkspaceButton active={workspace === "research"} onClick={() => setWorkspace("research")} icon={BookOpen}>Research</WorkspaceButton>
          <WorkspaceButton active={workspace === "evolution"} onClick={() => setWorkspace("evolution")} icon={Sparkles}>Evolution</WorkspaceButton>
        </nav>
        <button type="button" className="activitybar-settings" aria-label="Remote workspace settings" title="Remote workspace settings" onClick={() => setConnectionOpen(true)}><Settings size={19} /></button>
      </aside>

      <ProjectExplorerV2
        projects={snapshot.projects}
        activeProject={displayedProject}
        workspace={displayedWorkspace}
        uploads={displayedWorkspaceUploads}
        selectedPath={selectedWorkspacePath}
        busy={busy || startingSession !== null}
        switching={switchingProjectId !== null}
        fileTransferAvailable={provider.uploadWorkspaceFile !== undefined && provider.downloadWorkspaceFile !== undefined}
        projectDeleteAvailable={provider.deleteProject !== undefined}
        fileDeleteAvailable={provider.deleteWorkspaceFile !== undefined}
        onSelectProject={selectProject}
        onDeleteProject={deleteProject}
        onCreateProject={() => { setProjectEditing(false); setProjectOpen(true); }}
        onSelectFile={(path) => { setWorkspace("research"); setSelectedTaskId(null); setSelectedWorkspacePath(path); }}
        onDeleteFile={deleteWorkspaceFile}
        onUpload={uploadWorkspaceFiles}
        generation={generation}
        paneWidth={projectPaneWidth}
        onResizePane={setProjectPaneWidth}
      />

      <SessionExplorerV2
        project={displayedProject}
        tasks={displayedProjectTasks}
        presentation={snapshot.runtimePresentation?.tasks}
        selectedTaskId={selectedTaskId}
        onSelectTask={(taskId) => {
          setWorkspace("research");
          setSelectedWorkspacePath(null);
          setSelectedTaskId(taskId);
        }}
        onNewSession={() => {
          setWorkspace("research");
          setSelectedWorkspacePath(null);
          setSelectedTaskId(null);
          setNewSessionDraftKey((current) => current + 1);
        }}
        deleteAvailable={provider.deleteTask !== undefined}
        busy={busy || startingSession !== null}
        onDeleteTask={deleteSession}
        paneWidth={sessionPaneWidth}
        onResizePane={setSessionPaneWidth}
      />

      <div className="product-stage">
        <main className="product-main">
          {loadError || actionError || actionStatus || snapshot.stream.status !== "fresh" ? (
            <div className="product-feedback-stack" aria-label="Product status">
              {loadError ? <ErrorNoticeV2 context="refresh" error={loadError} onRetry={() => void refresh()} /> : null}
              {actionError ? <ErrorNoticeV2 context="action" error={actionError} onDismiss={() => setActionError(null)} /> : null}
              {actionStatus ? <Notice tone="success" title="Done" detail={actionStatus} compact onDismiss={() => setActionStatus(null)} /> : null}
              {snapshot.stream.status !== "fresh" ? (
                <Notice tone="warning" title="Updating remote state" detail="Changes are briefly paused while EvoLab confirms the latest server state." compact />
              ) : null}
            </div>
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
              busy={busy || switchingProjectId !== null}
              onBack={() => {
                setSelectedWorkspacePath(null);
                setSelectedTaskId(null);
                setNewSessionDraftKey((current) => current + 1);
              }}
              onDownload={() => downloadWorkspaceFile(selectedWorkspaceEntry.path)}
            />
          ) : workspace === "research" ? (
            <ResearchWorkspaceV2
              project={displayedProject}
              tasks={snapshot.tasks}
              transitions={snapshot.transitions}
              taskLogs={taskLogs}
              artifacts={snapshot.artifacts}
              capability={snapshot.capability}
              runtimePresentation={snapshot.runtimePresentation}
              selectedTaskId={selectedTaskId}
              newSessionDraftKey={newSessionDraftKey}
              startingSession={startingSession?.projectId === displayedProject.project_id ? startingSession : null}
              busy={busy || switchingProjectId !== null}
              sessionStartBlocked={developmentEvolutionActive}
              sessionEvolutionAvailable={sessionEvolutionAvailable}
              sessionImageUploadAvailable={provider.uploadWorkspaceFile !== undefined}
              onSelectTask={(taskId) => {
                setSelectedTaskId(taskId);
                if (taskId === null) setNewSessionDraftKey((current) => current + 1);
              }}
              onOpenSettings={() => { setProjectEditing(true); setProjectOpen(true); }}
              onRetryInitialization={() => void refresh()}
              onRun={(task, selectedEvolutionTargets, projectHead, imageAttachments) => (
                runProject(displayedProject, task, selectedEvolutionTargets, projectHead, imageAttachments)
              )}
              onCancelTask={(task) => void act(
                () => provider.cancelTask(task.task_id, intentFor(snapshot, "cancel-task")),
                "Session cancellation requested.",
              )}
              onRetryTask={(task) => void act(
                () => provider.retryTask(task.task_id, intentFor(snapshot, "retry-task")),
                "A new run attempt was requested for the same Session admission.",
              )}
              onRetryEvolutionJob={(jobId) => void act(
                async () => {
                  if (!provider.retryEvolutionJob) throw new Error("This backend does not support retrying an individual Evolution method.");
                  await provider.retryEvolutionJob(jobId, intentFor(snapshot, "retry-evolution-job"));
                },
                "The failed Evolution method is running again with the original Session inputs.",
              )}
              onRetryTransition={(transition) => void act(
                () => provider.retryTransition(transition.transition.successor_transition_id, intentFor(snapshot, "retry-transition")),
                "Successor transition retry requested.",
              )}
              onAbandonTransition={(transition) => void act(
                () => provider.abandonTransition(transition.transition.successor_transition_id, intentFor(snapshot, "abandon-transition")),
                "The successor Evolution result was abandoned.",
              )}
            />
          ) : (
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
                    displayedProject.active_project_head ?? undefined,
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
              onRefresh={() => { void refresh(); }}
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

      {deleteConfirmation ? (
        <DeleteConfirmationDialogV2
          confirmation={deleteConfirmation}
          busy={busy}
          onCancel={() => setDeleteConfirmation(null)}
          onConfirm={confirmDeletion}
        />
      ) : null}
    </div>
  );
}

function DeleteConfirmationDialogV2({
  confirmation,
  busy,
  onCancel,
  onConfirm,
}: {
  readonly confirmation: DeleteConfirmationV2;
  readonly busy: boolean;
  readonly onCancel: () => void;
  readonly onConfirm: () => void;
}) {
  const title = confirmation.kind === "project"
    ? "Delete Project?"
    : confirmation.kind === "session"
      ? "Delete Session?"
      : "Delete file?";
  const resourceName = confirmation.kind === "project"
    ? confirmation.project.display_name
    : confirmation.kind === "session"
      ? confirmation.title
      : confirmation.path;
  const detail = confirmation.kind === "project"
    ? "The Project and its Sessions will be removed from EvoLab."
    : confirmation.kind === "session"
      ? "This Session will be removed from the Project history view."
      : "This file will be permanently removed from the remote workspace.";
  const confirmLabel = confirmation.kind === "project"
    ? "Delete Project"
    : confirmation.kind === "session"
      ? "Delete Session"
      : "Delete file";
  const dialogRef = useDialogBoundary(() => {
    if (!busy) onCancel();
  });
  return (
    <div className="v2-modal-backdrop delete-confirm-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) onCancel();
    }}>
      <section
        ref={dialogRef}
        className="v2-modal delete-confirm-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="delete-confirm-title"
        aria-describedby="delete-confirm-description"
        tabIndex={-1}
      >
        <div className="delete-confirm-head">
          <span className="delete-confirm-icon" aria-hidden="true"><Trash2 size={19} /></span>
          <div><span className="panel-kicker">Confirm deletion</span><h2 id="delete-confirm-title">{title}</h2></div>
        </div>
        <div className="delete-confirm-content">
          <strong title={resourceName}>{resourceName}</strong>
          <p id="delete-confirm-description">{detail}</p>
        </div>
        <div className="delete-confirm-actions">
          <button type="button" className="secondary-button" disabled={busy} onClick={onCancel}>Cancel</button>
          <button type="button" className="danger-button" disabled={busy} onClick={onConfirm}>{busy ? <LoaderCircle className="spin" size={15} /> : <Trash2 size={15} />}{confirmLabel}</button>
        </div>
      </section>
    </div>
  );
}

function InitialV2View({
  error,
  onRetry,
}: {
  readonly error: string | null;
  readonly onRetry: () => void;
}) {
  const errorCopy = error === null ? null : errorNoticeCopyV2(error, "startup");
  return (
    <div className={`product-shell initial-launch-shell${error ? " has-error" : ""}`}>
      <main className="initial-launch-main">
        <section className="initial-launch-copy" aria-live="polite">
          <h1 className={error ? undefined : "initial-launch-title-shimmer"}>
            {error ? "EvoLab needs " : "EvoLab is "}
            <span>{error ? "a moment." : "starting."}</span>
          </h1>
          {error ? (
            <>
              <p>{errorCopy?.detail}</p>
              <div className="initial-launch-status is-error" role="status">
                <AlertCircle size={17} />
                <div><strong>{errorCopy?.title}</strong><span>{errorCopy?.guidance}</span></div>
              </div>
            </>
          ) : (
            <div className="initial-launch-preparing" role="status">
              <LoaderCircle className="spin" size={21} aria-hidden="true" />
              <span>Preparing your workspace and connecting to the remote service — EvoLab will open automatically when everything is ready.</span>
            </div>
          )}
          {error ? (
            <button className="initial-launch-retry" type="button" onClick={onRetry}>
              <RefreshCw size={16} /> Retry startup
            </button>
          ) : null}
        </section>
        <div className="initial-launch-progress" aria-hidden="true"><span /></div>
      </main>
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
          {error ? <ErrorNoticeV2 context="connection" error={error} onDismiss={onClearError} /> : null}
          <section className="form-section">
            <div className="v2-section-heading"><div><h3>Configured SSH host</h3><p>EvoLab reads aliases from your system <code>~/.ssh/config</code> and invokes the equivalent of <code>ssh alias</code>. OpenSSH remains authoritative for host, port, user, identities, ProxyJump, agent, Keychain, and trust policy.</p></div><button type="button" className="text-button" disabled={busy} onClick={() => void mutate(() => provider.rescanSshHosts(intentFor(snapshot, "rescan-hosts")))}><RefreshCw size={14} /> Rescan</button></div>
            {snapshot.catalog.warnings.length > 0 ? (
              <div className="v2-catalog-warning" role="status"><AlertCircle size={16} /><span><strong>Some SSH configuration entries could not be listed.</strong> Fix your system OpenSSH configuration, then rescan.</span></div>
            ) : null}
            <label>Workspace name<input maxLength={256} value={displayName} onChange={(event) => setDisplayName(event.target.value)} /></label>
            {selectableHosts.length > 0 ? (
              <div className="soft-select-field"><span>SSH host alias</span><SoftSelectV2 ariaLabel="SSH host alias" autoFocus value={alias} options={selectableHosts.map((host) => ({ value: host.ssh_host_alias, label: host.ssh_host_alias }))} onChange={setAlias} /></div>
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
  const [workspaceKind, setWorkspaceKind] = useState<"scratch" | "native_folder_snapshot">(project?.config.workspace.kind ?? "scratch");
  const [workspaceDisplayName, setWorkspaceDisplayName] = useState(project?.config.workspace.display_name ?? "Research workspace");
  const [selectedSourceDisplayName, setSelectedSourceDisplayName] = useState<string | null>(null);
  const [browserFolderUploads, setBrowserFolderUploads] = useState<readonly BrowserWorkspaceUploadV2[]>([]);
  const [folderImportProgress, setFolderImportProgress] = useState<number | null>(null);
  const [sourceActionId, setSourceActionId] = useState<string | null>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);
  const closedRef = useRef(false);
  const dialogRef = useDialogBoundary(onClose);
  const projectTask = project?.config.task ?? PROJECT_TASK_PLACEHOLDER;
  const browserFolderAvailable = project === null
    && provider.featureFlags.includes(BROWSER_FOLDER_SNAPSHOT_FEATURE)
    && provider.uploadWorkspaceFile !== undefined;
  const browserFolderBytes = browserFolderUploads.reduce((total, upload) => total + upload.file.size, 0);
  const baseDraftValid = displayName.trim() !== "";
  const valid = baseDraftValid
    && (workspaceKind === "scratch" || sourceActionId !== null || browserFolderUploads.length > 0 || project !== null);
  const supportedExecution = project?.config.execution.mode !== "self-deployed";

  useEffect(() => {
    folderInputRef.current?.setAttribute("webkitdirectory", "");
    folderInputRef.current?.setAttribute("directory", "");
  }, []);

  const selectBrowserFolder = (files: readonly File[]): void => {
    try {
      const selection = browserFolderSelectionV2(files);
      setBrowserFolderUploads(selection.uploads);
      setWorkspaceKind("native_folder_snapshot");
      setSelectedSourceDisplayName(selection.displayName);
      setWorkspaceDisplayName(selection.displayName);
    } catch (error) {
      setBrowserFolderUploads([]);
      setWorkspaceKind("scratch");
      setSelectedSourceDisplayName(null);
      onError(error);
    }
  };

  const chooseFolder = async (): Promise<void> => {
    const actionId = actionIdV2("select-workspace");
    setSourceActionId(actionId);
    onBusy(true);
    try {
      const config = scienceProjectConfig(
        projectTask.title,
        projectTask.objective,
        "native_folder_snapshot",
        workspaceDisplayName,
        project?.config.execution,
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
      projectTask.title,
      projectTask.objective,
      workspaceKind,
      workspaceDisplayName,
      project?.config.execution,
    );
    onBusy(true);
    let browserCreatedProjectId: string | null = null;
    try {
      if (project) {
        await provider.updateProject(project.project_id, displayName.trim(), { ...config, evolution: project.config.evolution }, { actionId, streamEpoch: snapshot.stream.epoch });
      } else if (workspaceKind === "native_folder_snapshot" && browserFolderUploads.length > 0) {
        if (!provider.uploadWorkspaceFile) throw new Error("This browser cannot upload a folder snapshot.");
        const scratchConfig: ScienceProjectConfigV2 = {
          ...config,
          workspace: { ...config.workspace, kind: "scratch" },
        };
        const created = await provider.createProject({
          profileId: profile.profile_id,
          displayName: displayName.trim(),
          config: scratchConfig,
        }, { actionId: actionIdV2("create-folder-project"), streamEpoch: snapshot.stream.epoch });
        if (created.status !== "succeeded" || created.result?.result_kind !== "project") {
          throw new Error("The remote project was not ready for its folder snapshot.");
        }
        browserCreatedProjectId = created.result.project_id;
        const totalBytes = Math.max(1, browserFolderBytes);
        let completedBytes = 0;
        setFolderImportProgress(0);
        for (const upload of browserFolderUploads) {
          await provider.uploadWorkspaceFile(
            browserCreatedProjectId,
            {
              path: upload.path,
              data: upload.file,
              mediaType: upload.file.type || "application/octet-stream",
              overwrite: false,
              onProgress: (percentage) => setFolderImportProgress(Math.round(
                ((completedBytes + (upload.file.size * percentage / 100)) / totalBytes) * 100,
              )),
            },
            { actionId: actionIdV2("upload-folder-file"), streamEpoch: snapshot.stream.epoch },
          );
          completedBytes += upload.file.size;
          setFolderImportProgress(Math.round((completedBytes / totalBytes) * 100));
        }
        setFolderImportProgress(100);
        const refreshed = await provider.refresh();
        if (refreshed.status !== "fresh") {
          throw new Error("The imported Project could not be refreshed before finalization.");
        }
        const importedProject = refreshed.snapshot.projects.find((candidate) => (
          candidate.project_id === browserCreatedProjectId
        ));
        if (importedProject === undefined) {
          throw new Error("The imported Project is missing from the remote state.");
        }
        await provider.updateProject(
          browserCreatedProjectId,
          displayName.trim(),
          { ...config, evolution: importedProject.config.evolution },
          { actionId: actionIdV2("finalize-folder-project"), streamEpoch: refreshed.snapshot.stream.epoch },
        );
      } else {
        await provider.createProject({
          profileId: profile.profile_id,
          displayName: displayName.trim(),
          config,
        }, { actionId, streamEpoch: snapshot.stream.epoch });
      }
      await onCreated();
    } catch (error) {
      if (browserCreatedProjectId !== null && provider.deleteProject) {
        await provider.deleteProject(
          browserCreatedProjectId,
          { actionId: actionIdV2("rollback-folder-project"), streamEpoch: snapshot.stream.epoch },
        ).catch(() => {});
      }
      onError(error);
    } finally {
      setFolderImportProgress(null);
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
    <div className="v2-modal-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) void close();
    }}>
      <section ref={dialogRef} className="v2-modal project-setup-modal" role="dialog" aria-modal="true" aria-labelledby="new-project-title" tabIndex={-1}>
        <div className="project-setup-head">
          <div>
            <span className="project-setup-kicker">{project ? "Project settings" : "New project"}</span>
            <h2 id="new-project-title">{project ? "Edit project" : "Create a project"}</h2>
            <p>{project ? "Update how this project appears in EvoLab." : "Name your project and choose its starting workspace."}</p>
          </div>
          <button type="button" className="icon-button" aria-label="Close project setup" onClick={() => void close()}><X size={17} /></button>
        </div>
        <div className="project-setup-content">
          <section className="project-setup-section project-name-section">
            <label htmlFor="project-name-input"><span>Project name</span><small>Shown in the project switcher and Session workspace.</small><input id="project-name-input" autoFocus maxLength={256} value={displayName} disabled={sourceActionId !== null} onChange={(event) => setDisplayName(event.target.value)} /></label>
          </section>
          <section className="project-setup-section">
            <div className="project-setup-section-heading"><div><h3>Starting workspace</h3><p>Choose an empty workspace or import a folder snapshot.</p></div></div>
            {browserFolderAvailable ? <input ref={folderInputRef} className="project-workspace-file-input" type="file" multiple aria-label="Choose project folder snapshot" onChange={(event) => {
              const selectedFiles = Array.from(event.currentTarget.files ?? []);
              event.currentTarget.value = "";
              if (selectedFiles.length > 0) selectBrowserFolder(selectedFiles);
            }} /> : null}
            {project ? (
              <div className="project-workspace-locked">
                <span><FolderOpen size={17} /></span>
                <div><strong>{project.config.workspace.display_name}</strong><small>The workspace source is fixed after project creation.</small></div>
                <span className="project-workspace-lock-label">Locked</span>
              </div>
            ) : (
              <div className="project-workspace-options" role="radiogroup" aria-label="Starting workspace">
                <button type="button" role="radio" aria-checked={workspaceKind === "scratch"} className={workspaceKind === "scratch" ? "selected" : ""} disabled={sourceActionId !== null} onClick={() => { setWorkspaceKind("scratch"); setSourceActionId(null); setBrowserFolderUploads([]); setSelectedSourceDisplayName(null); setWorkspaceDisplayName("Research workspace"); }}>
                  <span className="project-workspace-option-icon"><Plus size={17} /></span>
                  <span className="project-workspace-option-copy"><strong>Start empty</strong><small>Create a clean workspace for a new line of work.</small></span>
                  <CheckCircle2 className="project-workspace-option-check" size={17} />
                </button>
                <button type="button" role="radio" aria-checked={workspaceKind === "native_folder_snapshot"} className={workspaceKind === "native_folder_snapshot" ? "selected" : ""} disabled={!baseDraftValid || sourceActionId !== null} onClick={() => { if (browserFolderAvailable) folderInputRef.current?.click(); else void chooseFolder(); }}>
                  <span className="project-workspace-option-icon"><FolderOpen size={17} /></span>
                  <span className="project-workspace-option-copy"><strong>Import a folder</strong><small>Copy a local folder into the remote workspace.</small></span>
                  <CheckCircle2 className="project-workspace-option-check" size={17} />
                </button>
              </div>
            )}
            {!project && workspaceKind === "native_folder_snapshot" ? <div className="project-workspace-selection" role="status"><FolderOpen size={14} /><span>{browserFolderUploads.length > 0 ? <><strong>{selectedSourceDisplayName}</strong> · {browserFolderUploads.length} file{browserFolderUploads.length === 1 ? "" : "s"} · {formatBytes(browserFolderBytes)}</> : selectedSourceDisplayName ?? "Preparing selected workspace…"}</span></div> : null}
            {folderImportProgress !== null ? <div className="folder-snapshot-progress" role="progressbar" aria-label="Uploading folder snapshot" aria-valuemin={0} aria-valuemax={100} aria-valuenow={folderImportProgress}><span style={{ width: `${folderImportProgress}%` }} /><small>{folderImportProgress}% uploaded</small></div> : null}
          </section>
          <div className="project-execution-summary"><span><ShieldCheck size={16} /></span><div><small>Execution</small><strong>{supportedExecution ? "Codex Subscription" : "Existing project profile"}</strong></div><p>{supportedExecution ? "Transcript capture · high effort" : "Preserved until a supported migration is available"}</p></div>
        </div>
        <div className="project-setup-footer"><button type="button" className="secondary-button" onClick={() => void close()}>Cancel</button><button type="button" className="primary-button" onClick={() => void create()} disabled={busy || !valid}>{busy ? <LoaderCircle className="spin" size={15} /> : project ? <Settings size={15} /> : <Plus size={15} />} {project ? "Save changes" : "Create project"}</button></div>
      </section>
    </div>
  );
}

type ProjectWorkspacePresentationV2 = NonNullable<
  NonNullable<DesktopProductSnapshotV2["runtimePresentation"]>["workspaces"]
>[string];

type ProjectWorkspaceEntryV2 = ProjectWorkspacePresentationV2["entries"][number];

type ProjectFileTreeNodeV2 = {
  path: string;
  name: string;
  kind: ProjectWorkspaceEntryV2["kind"];
  upload: WorkspaceUploadStateV2 | null;
  children: ProjectFileTreeNodeV2[];
};

function buildProjectFileTreeV2(
  entries: readonly ProjectWorkspaceEntryV2[],
  uploads: readonly WorkspaceUploadStateV2[] = [],
): ProjectFileTreeNodeV2[] {
  const roots: ProjectFileTreeNodeV2[] = [];
  const nodes = new Map<string, ProjectFileTreeNodeV2>();
  const addPath = (
    entry: Pick<ProjectWorkspaceEntryV2, "path" | "kind">,
    upload: WorkspaceUploadStateV2 | null,
  ): void => {
    const parts = entry.path.split("/").filter(Boolean);
    parts.forEach((name, index) => {
      const path = parts.slice(0, index + 1).join("/");
      const leaf = index === parts.length - 1;
      let node = nodes.get(path);
      if (node === undefined) {
        node = { path, name, kind: leaf ? entry.kind : "directory", upload: leaf ? upload : null, children: [] };
        nodes.set(path, node);
        const parentPath = parts.slice(0, index).join("/");
        const parent = nodes.get(parentPath);
        if (parent) parent.children.push(node); else roots.push(node);
      } else if (leaf) {
        node.kind = entry.kind;
        node.upload = upload;
      }
    });
  };
  for (const entry of [...entries].sort((left, right) => left.path.localeCompare(right.path))) {
    addPath(entry, null);
  }
  for (const upload of uploads) {
    addPath({ path: upload.path, kind: "file" }, upload);
  }
  const sortNodes = (items: ProjectFileTreeNodeV2[]): void => {
    items.sort((left, right) => {
      const directoryDelta = Number(right.kind === "directory") - Number(left.kind === "directory");
      return directoryDelta || left.name.localeCompare(right.name, undefined, { numeric: true });
    });
    items.forEach((item) => sortNodes(item.children));
  };
  sortNodes(roots);
  return roots;
}

function filterProjectFileTreeV2(
  nodes: readonly ProjectFileTreeNodeV2[],
  normalizedQuery: string,
): ProjectFileTreeNodeV2[] {
  if (normalizedQuery === "") return [...nodes];
  return nodes.flatMap((node) => {
    const children = filterProjectFileTreeV2(node.children, normalizedQuery);
    if (!node.path.toLocaleLowerCase().includes(normalizedQuery) && children.length === 0) return [];
    return [{ ...node, children }];
  });
}

type ProjectFileVisualV2 = {
  kind: string;
  label: string | null;
};

function projectFileVisualV2(path: string): ProjectFileVisualV2 {
  const name = path.split("/").at(-1)?.toLocaleLowerCase() ?? path.toLocaleLowerCase();
  const extension = name.includes(".") ? name.slice(name.lastIndexOf(".") + 1) : "";
  if (["py", "pyi", "pyw"].includes(extension)) return { kind: "python", label: "Py" };
  if (["md", "mdx", "markdown"].includes(extension)) return { kind: "markdown", label: "M↓" };
  if (["cpp", "cc", "cxx", "hpp", "hh", "hxx"].includes(extension)) return { kind: "cpp", label: "C+" };
  if (["c", "h"].includes(extension)) return { kind: "c", label: "C" };
  if (["ts", "tsx"].includes(extension)) return { kind: "typescript", label: "TS" };
  if (["js", "jsx", "mjs", "cjs"].includes(extension)) return { kind: "javascript", label: "JS" };
  if (["json", "jsonl"].includes(extension)) return { kind: "json", label: "{}" };
  if (["yaml", "yml"].includes(extension)) return { kind: "yaml", label: "Y" };
  if (["html", "htm", "vue", "svelte"].includes(extension)) return { kind: "html", label: "<>" };
  if (["css", "scss", "sass", "less"].includes(extension)) return { kind: "css", label: "#" };
  if (["sh", "bash", "zsh", "fish", "ps1", "bat", "cmd"].includes(extension)) return { kind: "shell", label: ">_" };
  if (extension === "pdf") return { kind: "pdf", label: "PDF" };
  if (["png", "jpg", "jpeg", "gif", "webp", "svg", "ico"].includes(extension)) return { kind: "image", label: "IMG" };
  if (["zip", "tar", "gz", "bz2", "xz", "7z", "rar"].includes(extension)) return { kind: "archive", label: "ZIP" };
  if (["csv", "tsv", "xls", "xlsx", "parquet"].includes(extension)) return { kind: "data", label: "CSV" };
  if (["toml", "ini", "cfg", "conf", "env"].includes(extension)) return { kind: "config", label: "CFG" };
  if (["rs"].includes(extension)) return { kind: "rust", label: "Rs" };
  if (["go"].includes(extension)) return { kind: "go", label: "Go" };
  if (["java", "kt", "kts"].includes(extension)) return { kind: "java", label: "Jv" };
  if (["ipynb"].includes(extension)) return { kind: "notebook", label: "J" };
  if (name === "dockerfile" || name.startsWith("dockerfile.")) return { kind: "docker", label: "D" };
  if (name === "makefile" || name === "cmakelists.txt") return { kind: "build", label: "MK" };
  return { kind: "default", label: null };
}

function ProjectFileTypeIconV2({ path }: { readonly path: string }) {
  const visual = projectFileVisualV2(path);
  return (
    <span className={`explorer-file-type-icon ${visual.kind}`} aria-hidden="true">
      {visual.label === null ? <FileText size={15} /> : <span>{visual.label}</span>}
    </span>
  );
}

type SoftSelectOptionV2 = {
  readonly key?: string;
  readonly value: string;
  readonly label: string;
  readonly disabled?: boolean;
  readonly menuAction?: {
    readonly label: string;
    readonly ariaLabel: string;
    readonly disabled?: boolean;
    readonly onSelect: () => void;
  };
};

function SoftSelectV2({
  id,
  ariaLabel,
  value,
  options,
  disabled = false,
  autoFocus = false,
  placement = "bottom",
  className = "",
  onChange,
}: {
  readonly id?: string;
  readonly ariaLabel: string;
  readonly value: string;
  readonly options: readonly SoftSelectOptionV2[];
  readonly disabled?: boolean;
  readonly autoFocus?: boolean;
  readonly placement?: "top" | "bottom";
  readonly className?: string;
  readonly onChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [actionValue, setActionValue] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const matchedIndex = options.findIndex((option) => option.value === value);
  const selectedIndex = matchedIndex >= 0 ? matchedIndex : options.length ? 0 : -1;
  const selected = selectedIndex >= 0 ? options[selectedIndex]! : null;
  useEffect(() => {
    if (!open) return undefined;
    const closeOnOutsidePointer = (event: PointerEvent): void => {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false);
        setActionValue(null);
      }
    };
    const closeOnEscape = (event: KeyboardEvent): void => {
      if (event.key !== "Escape") return;
      setOpen(false);
      setActionValue(null);
      triggerRef.current?.focus();
    };
    globalThis.document.addEventListener("pointerdown", closeOnOutsidePointer);
    globalThis.document.addEventListener("keydown", closeOnEscape);
    return () => {
      globalThis.document.removeEventListener("pointerdown", closeOnOutsidePointer);
      globalThis.document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);
  useEffect(() => {
    if (disabled) {
      setOpen(false);
      setActionValue(null);
    }
  }, [disabled]);
  const moveOptionFocus = (event: ReactKeyboardEvent<HTMLButtonElement>, direction: 1 | -1): void => {
    event.preventDefault();
    const choices = [...(rootRef.current?.querySelectorAll<HTMLButtonElement>('[role="option"]:not(:disabled)') ?? [])];
    const current = choices.indexOf(event.currentTarget);
    choices[(current + direction + choices.length) % choices.length]?.focus();
  };
  return (
    <div ref={rootRef} className={`soft-select ${placement}${open ? " open" : ""}${className ? ` ${className}` : ""}`}>
      <button
        ref={triggerRef}
        id={id}
        type="button"
        className="soft-select-trigger"
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        data-value={value}
        autoFocus={autoFocus}
        disabled={disabled || options.length === 0}
        title={selected?.label}
        onClick={() => {
          setOpen((current) => !current);
          setActionValue(null);
        }}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            setOpen(true);
          }
        }}
      >
        <span>{selected?.label ?? "No options"}</span>
        <ChevronDown size={15} />
      </button>
      {open ? (
        <div className="soft-select-menu" role="listbox" aria-label={ariaLabel}>
          {options.map((option, index) => {
            const optionSelected = index === selectedIndex;
            return (
              <div className="soft-select-option" key={option.key ?? option.value}>
                <button
                  type="button"
                  role="option"
                  aria-selected={optionSelected}
                  className={optionSelected ? "selected" : ""}
                  data-value={option.value}
                  disabled={option.disabled}
                  title={option.label}
                  autoFocus={optionSelected}
                  onKeyDown={(event) => {
                    if (event.key === "ArrowDown") moveOptionFocus(event, 1);
                    if (event.key === "ArrowUp") moveOptionFocus(event, -1);
                  }}
                  onClick={() => {
                    setOpen(false);
                    setActionValue(null);
                    if (!optionSelected) onChange(option.value);
                    triggerRef.current?.focus();
                  }}
                >
                  <span>{option.label}</span>
                  {optionSelected ? <CheckCircle2 size={15} /> : null}
                </button>
                {option.menuAction ? (
                  <button
                    type="button"
                    className="soft-select-option-more"
                    aria-label={option.menuAction.ariaLabel}
                    aria-haspopup="menu"
                    aria-expanded={actionValue === option.value}
                    disabled={option.menuAction.disabled}
                    onClick={() => setActionValue((current) => current === option.value ? null : option.value)}
                  >
                    <EllipsisVertical size={15} />
                  </button>
                ) : null}
                {option.menuAction && actionValue === option.value ? (
                  <div className="soft-select-option-popover" role="menu">
                    <button type="button" role="menuitem" onClick={() => {
                      setOpen(false);
                      setActionValue(null);
                      option.menuAction?.onSelect();
                    }}><Trash2 size={14} /><span>{option.menuAction.label}</span></button>
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

function ProjectExplorerV2({
  projects,
  activeProject,
  workspace,
  uploads,
  selectedPath,
  busy,
  switching,
  fileTransferAvailable,
  projectDeleteAvailable,
  fileDeleteAvailable,
  onSelectProject,
  onDeleteProject,
  onCreateProject,
  onSelectFile,
  onDeleteFile,
  onUpload,
  generation,
  paneWidth,
  onResizePane,
}: {
  readonly projects: readonly ProjectV2[];
  readonly activeProject: ProjectV2 | null;
  readonly workspace: ProjectWorkspacePresentationV2 | undefined;
  readonly uploads: readonly WorkspaceUploadStateV2[];
  readonly selectedPath: string | null;
  readonly busy: boolean;
  readonly switching: boolean;
  readonly fileTransferAvailable: boolean;
  readonly projectDeleteAvailable: boolean;
  readonly fileDeleteAvailable: boolean;
  readonly onSelectProject: (projectId: string) => void;
  readonly onDeleteProject: (project: ProjectV2) => void;
  readonly onCreateProject: () => void;
  readonly onSelectFile: (path: string) => void;
  readonly onDeleteFile: (path: string) => void;
  readonly onUpload: (uploads: readonly BrowserWorkspaceUploadV2[], overwrite: boolean) => void;
  readonly generation: number;
  readonly paneWidth: number;
  readonly onResizePane: (width: number) => void;
}) {
  const entries = workspace?.entries ?? [];
  const files = entries.filter((entry) => entry.kind === "file");
  const displayedFileCount = new Set([...files.map((file) => file.path), ...uploads.map((upload) => upload.path)]).size;
  const [collapsedDirectories, setCollapsedDirectories] = useState<ReadonlySet<string>>(new Set());
  const [fileQuery, setFileQuery] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [uploadMenuOpen, setUploadMenuOpen] = useState(false);
  const uploadInputRef = useRef<HTMLInputElement>(null);
  const folderUploadInputRef = useRef<HTMLInputElement>(null);
  const uploadMenuRef = useRef<HTMLDivElement>(null);
  const fileTree = useMemo(() => buildProjectFileTreeV2(entries, uploads), [entries, uploads]);
  const normalizedFileQuery = fileQuery.trim().toLocaleLowerCase();
  const visibleFileTree = useMemo(
    () => filterProjectFileTreeV2(fileTree, normalizedFileQuery),
    [fileTree, normalizedFileQuery],
  );
  useEffect(() => {
    setCollapsedDirectories(new Set());
    setFileQuery("");
    setSearchOpen(false);
    setUploadMenuOpen(false);
  }, [activeProject?.project_id]);
  useEffect(() => {
    folderUploadInputRef.current?.setAttribute("webkitdirectory", "");
    folderUploadInputRef.current?.setAttribute("directory", "");
  }, []);
  useEffect(() => {
    if (!uploadMenuOpen) return undefined;
    const closeOnOutsidePointer = (event: PointerEvent): void => {
      if (!uploadMenuRef.current?.contains(event.target as Node)) setUploadMenuOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent): void => {
      if (event.key === "Escape") setUploadMenuOpen(false);
    };
    globalThis.document.addEventListener("pointerdown", closeOnOutsidePointer);
    globalThis.document.addEventListener("keydown", closeOnEscape);
    return () => {
      globalThis.document.removeEventListener("pointerdown", closeOnOutsidePointer);
      globalThis.document.removeEventListener("keydown", closeOnEscape);
    };
  }, [uploadMenuOpen]);
  const submitUploadSelection = (selectedFiles: readonly File[], preserveHierarchy: boolean): void => {
    const uploads = selectedFiles.map((file) => {
      const browserPath = preserveHierarchy && file.webkitRelativePath ? file.webkitRelativePath : file.name;
      const path = browserPath.replaceAll("\\", "/").split("/").filter((part) => part !== "" && part !== ".").join("/");
      return { file, path };
    }).filter((upload) => upload.path !== "");
    if (uploads.length === 0) return;
    const existingPaths = new Set(files.map((file) => file.path));
    const collisionCount = uploads.filter((upload) => existingPaths.has(upload.path)).length;
    if (collisionCount > 0 && !globalThis.confirm(`${collisionCount} selected workspace ${collisionCount === 1 ? "file already exists" : "files already exist"}. Replace ${collisionCount === 1 ? "it" : "them"}?`)) return;
    onUpload(uploads, collisionCount > 0);
  };
  const renderTreeNodes = (nodes: readonly ProjectFileTreeNodeV2[], level = 1): ReactNode => nodes.map((node) => {
    const directory = node.kind === "directory";
    const selected = node.kind === "file" && node.path === selectedPath;
    const collapsed = directory && collapsedDirectories.has(node.path);
    const expanded = directory && (normalizedFileQuery !== "" || !collapsed);
    const unavailable = !directory && node.kind !== "file";
    const upload = node.upload;
    const uploadPending = upload !== null && upload.status !== "failed";
    const uploadLabel = upload?.status === "queued"
      ? "Waiting to upload"
      : upload?.status === "uploaded"
        ? "Finishing upload"
        : upload?.status === "failed"
          ? "Upload failed"
          : upload === null
            ? null
            : `${upload.percentage}% uploaded`;
    return (
      <div className={`explorer-tree-node ${directory ? "directory" : "file"}${upload ? ` upload-${upload.status}` : ""}`} key={`${node.kind}:${node.path}`}>
        <button
          type="button"
          role="treeitem"
          aria-level={level}
          aria-selected={selected}
          aria-expanded={directory ? expanded : undefined}
          aria-disabled={unavailable || upload !== null || undefined}
          aria-busy={uploadPending || undefined}
          className={`${selected ? "selected" : ""}${unavailable || upload ? " unavailable" : ""}`}
          title={upload?.error ? `${node.path}: ${upload.error}` : node.path}
          onClick={() => {
            if (directory) {
              setCollapsedDirectories((current) => {
                const next = new Set(current);
                if (next.has(node.path)) next.delete(node.path); else next.add(node.path);
                return next;
              });
            } else if (node.kind === "file" && upload === null) onSelectFile(node.path);
          }}
        >
          <span className="explorer-tree-toggle" aria-hidden="true">
            {directory ? expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} /> : null}
          </span>
          <span className="explorer-tree-kind" aria-hidden="true">
            {directory ? <FolderOpen size={15} /> : <ProjectFileTypeIconV2 path={node.path} />}
          </span>
          <span className="explorer-tree-content">
            <span className="explorer-tree-label">{node.name}</span>
            {upload ? (
              <span className="explorer-upload-state" data-status={upload.status}>
                <span className="explorer-upload-state-label">{uploadLabel}</span>
                {upload.status === "uploading" || upload.status === "queued" || upload.status === "uploaded" ? (
                  <span
                    className="explorer-upload-progress"
                    role="progressbar"
                    aria-label={`Uploading ${node.path}`}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={upload.percentage}
                  >
                    <span style={{ width: `${upload.percentage}%` }} />
                  </span>
                ) : null}
              </span>
            ) : null}
          </span>
        </button>
        {!directory && node.kind === "file" && upload === null && fileDeleteAvailable ? (
          <button
            type="button"
            className="explorer-file-delete"
            aria-label={`Delete ${node.path}`}
            title={`Delete ${node.path}`}
            disabled={busy}
            onClick={() => onDeleteFile(node.path)}
          >
            <Trash2 size={13} />
          </button>
        ) : null}
        {directory && expanded && node.children.length > 0 ? (
          <div className="explorer-tree-branch" role="group">{renderTreeNodes(node.children, level + 1)}</div>
        ) : null}
      </div>
    );
  });
  return (
    <aside className="project-explorer" aria-label="Project explorer">
      <div className="explorer-heading"><span>Project</span><button type="button" aria-label="Create project" title="Create project" disabled={busy || switching} onClick={onCreateProject}><Plus size={15} /></button></div>
      <div className={`project-switcher-shell${switching ? " switching" : ""}`}>
        <SoftSelectV2
          id="v2-project-switcher"
          ariaLabel="Select project"
          value={activeProject?.project_id ?? ""}
          options={projects.map((project) => ({
            value: project.project_id,
            label: project.display_name,
            menuAction: projectDeleteAvailable ? {
              label: "Delete project",
              ariaLabel: `More actions for ${project.display_name}`,
              disabled: busy || switching,
              onSelect: () => onDeleteProject(project),
            } : undefined,
          }))}
          disabled={busy || switching || projects.length === 0}
          className="project-switcher-select"
          onChange={onSelectProject}
        />
        {switching ? <span className="project-switching-indicator" role="status">Switching</span> : null}
      </div>
      <div className="explorer-files-header">
        <div className="explorer-files-title"><span className="explorer-files-mark"><FolderTree size={17} /></span><span><strong>Workspace</strong><small>{displayedFileCount} {displayedFileCount === 1 ? "file" : "files"}</small></span></div>
        <div className="explorer-files-actions"><button type="button" className={searchOpen ? "active" : ""} aria-label="Search files" title="Search files" aria-pressed={searchOpen} onClick={() => { setSearchOpen((current) => !current); if (searchOpen) setFileQuery(""); }}><Search size={15} /></button>{fileTransferAvailable ? <><input ref={uploadInputRef} className="project-workspace-file-input" type="file" multiple aria-label="Choose files to upload" onChange={(event) => {
        const selectedFiles = Array.from(event.currentTarget.files ?? []);
        event.currentTarget.value = "";
        submitUploadSelection(selectedFiles, false);
      }} /><input ref={folderUploadInputRef} className="project-workspace-file-input" type="file" multiple aria-label="Choose folder to upload" onChange={(event) => {
        const selectedFiles = Array.from(event.currentTarget.files ?? []);
        event.currentTarget.value = "";
        submitUploadSelection(selectedFiles, true);
      }} /><div className="explorer-upload-menu" ref={uploadMenuRef}><button type="button" className={uploadMenuOpen ? "active" : ""} aria-label="Upload to workspace" title="Upload" aria-haspopup="menu" aria-expanded={uploadMenuOpen} disabled={busy || activeProject === null} onClick={() => setUploadMenuOpen((current) => !current)}><Upload size={15} /></button>{uploadMenuOpen ? <div className="explorer-upload-popover" role="menu"><button type="button" role="menuitem" onClick={() => { setUploadMenuOpen(false); uploadInputRef.current?.click(); }}><Upload size={14} /><span>Upload files</span></button><button type="button" role="menuitem" onClick={() => { setUploadMenuOpen(false); folderUploadInputRef.current?.click(); }}><FolderUp size={14} /><span>Upload folder</span></button></div> : null}</div></> : null}</div>
      </div>
      {searchOpen ? <label className="explorer-file-search"><Search size={14} /><input autoFocus type="search" aria-label="Filter workspace files" placeholder="Filter files" value={fileQuery} onChange={(event) => setFileQuery(event.target.value)} />{fileQuery ? <button type="button" aria-label="Clear file search" onClick={() => setFileQuery("")}><X size={13} /></button> : null}</label> : null}
      <div className="explorer-file-tree" role="tree" aria-label="Project workspace files">
        {visibleFileTree.length ? renderTreeNodes(visibleFileTree) : <div className="explorer-empty">{entries.length || uploads.length ? "No matching files" : "No files yet"}</div>}
      </div>
      {workspace?.truncated ? <div className="explorer-warning">File preview is truncated.</div> : null}
      <div className="explorer-foot"><CircleDot size={13} /><span>Active Project Head {generation}</span></div>
      <VerticalResizeHandle label="Resize Project pane" value={paneWidth} defaultValue={248} minimum={180} maximum={440} onChange={onResizePane} />
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
  deleteAvailable,
  busy,
  onDeleteTask,
  paneWidth,
  onResizePane,
}: {
  readonly project: ProjectV2 | null;
  readonly tasks: readonly TaskV2[];
  readonly presentation: NonNullable<DesktopProductSnapshotV2["runtimePresentation"]>["tasks"] | undefined;
  readonly selectedTaskId: string | null;
  readonly onSelectTask: (taskId: string) => void;
  readonly onNewSession: () => void;
  readonly deleteAvailable: boolean;
  readonly busy: boolean;
  readonly onDeleteTask: (task: TaskV2, title: string) => void;
  readonly paneWidth: number;
  readonly onResizePane: (width: number) => void;
}) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<"all" | "active" | "completed" | "failed">("all");
  const [openTaskMenu, setOpenTaskMenu] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const projectId = project?.project_id ?? null;
  const sortedTasks = useMemo(() => [...tasks].sort(compareTasksNewestFirst), [tasks]);
  const visibleTasks = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    return sortedTasks.filter((task) => {
      const stateMatches = filter === "all"
        || (filter === "active" && ["admitted", "preparing", "running", "cancelling", "waiting_for_successor"].includes(task.state))
        || (filter === "completed" && ["completed", "closed"].includes(task.state))
        || (filter === "failed" && ["failed", "cancelled"].includes(task.state));
      const title = presentation?.[task.task_id]?.instruction?.title ?? task.task_id;
      return stateMatches && (normalizedQuery === "" || title.toLocaleLowerCase().includes(normalizedQuery));
    });
  }, [filter, presentation, query, sortedTasks]);
  useEffect(() => {
    setQuery("");
    setFilter("all");
    setOpenTaskMenu(null);
    if (projectId === null) return;
    const remembered = Number.parseFloat(readPersistedRecord(PROJECT_SESSION_SCROLLS_KEY)[projectId] ?? "0");
    const timer = globalThis.setTimeout(() => {
      if (listRef.current) listRef.current.scrollTop = Number.isFinite(remembered) ? remembered : 0;
    }, 0);
    return () => globalThis.clearTimeout(timer);
  }, [projectId]);
  useEffect(() => {
    if (openTaskMenu === null) return undefined;
    const closeMenu = (event: PointerEvent): void => {
      const item = (event.target as Element | null)?.closest<HTMLElement>(".session-explorer-item");
      if (item?.dataset.taskId !== openTaskMenu) setOpenTaskMenu(null);
    };
    const closeOnEscape = (event: KeyboardEvent): void => {
      if (event.key === "Escape") setOpenTaskMenu(null);
    };
    globalThis.document.addEventListener("pointerdown", closeMenu);
    globalThis.document.addEventListener("keydown", closeOnEscape);
    return () => {
      globalThis.document.removeEventListener("pointerdown", closeMenu);
      globalThis.document.removeEventListener("keydown", closeOnEscape);
    };
  }, [openTaskMenu]);
  const rememberScroll = (): void => {
    if (projectId === null || listRef.current === null) return;
    persistRecord(PROJECT_SESSION_SCROLLS_KEY, {
      ...readPersistedRecord(PROJECT_SESSION_SCROLLS_KEY),
      [projectId]: String(Math.round(listRef.current.scrollTop)),
    });
  };
  return (
    <aside className="session-explorer" aria-label="Project sessions">
      <div className="explorer-heading"><span>Sessions</span><button type="button" className="new-session-button" aria-label="New Session" title="New Session" disabled={project === null} onClick={onNewSession}><Plus size={14} /><span>New</span></button></div>
      <div className="session-explorer-project" title={project?.display_name}>{project?.display_name ?? "No Project selected"}</div>
      <div className="session-explorer-tools">
        <label className="session-search"><Search size={13} /><input type="search" aria-label="Search Sessions" placeholder="Search Sessions" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
        <SoftSelectV2
          ariaLabel="Filter Sessions by status"
          value={filter}
          options={[
            { value: "all", label: "All" },
            { value: "active", label: "Active" },
            { value: "completed", label: "Completed" },
            { value: "failed", label: "Failed / cancelled" },
          ]}
          className="session-filter-select"
          onChange={(next) => setFilter(next as typeof filter)}
        />
      </div>
      <div className="session-result-count">Showing {visibleTasks.length} of {tasks.length}</div>
      <div ref={listRef} className="session-explorer-list" onScroll={rememberScroll}>
        {visibleTasks.length ? visibleTasks.map((task, index) => {
          const title = presentation?.[task.task_id]?.instruction?.title ?? `Session ${sortedTasks.length - index}`;
          const terminal = ["completed", "closed", "failed", "cancelled"].includes(task.state);
          return <div className={`session-explorer-item${task.task_id === selectedTaskId ? " active" : ""}`} data-task-id={task.task_id} key={task.task_id}>
            <button type="button" className="session-explorer-item-main" onClick={() => { setOpenTaskMenu(null); onSelectTask(task.task_id); }} title={title}><span className={`session-state-dot ${task.state}`} aria-hidden="true" /><span><strong>{title}</strong><small>{formatTimeV2(task.updated_at)}</small></span><em>{taskStateLabelV2(task.state)}</em></button>
            {deleteAvailable && terminal ? <button type="button" className="session-item-more" aria-label={`More actions for ${title}`} aria-haspopup="menu" aria-expanded={openTaskMenu === task.task_id} disabled={busy} onClick={() => setOpenTaskMenu((current) => current === task.task_id ? null : task.task_id)}><EllipsisVertical size={15} /></button> : null}
            {openTaskMenu === task.task_id ? <div className="session-item-popover" role="menu"><button type="button" role="menuitem" onClick={() => { setOpenTaskMenu(null); onDeleteTask(task, title); }}><Trash2 size={14} /><span>Delete session</span></button></div> : null}
          </div>;
        }) : <div className="explorer-empty">{tasks.length ? "No Sessions match the current filters" : "No Sessions yet. Use + to create one."}</div>}
      </div>
      <VerticalResizeHandle label="Resize Session pane" value={paneWidth} defaultValue={232} minimum={180} maximum={420} onChange={onResizePane} />
    </aside>
  );
}

function ProjectFileWorkspaceV2({
  project,
  entry,
  fileTransferAvailable,
  busy,
  onBack,
  onDownload,
}: {
  readonly project: ProjectV2;
  readonly entry: ProjectWorkspacePresentationV2["entries"][number];
  readonly fileTransferAvailable: boolean;
  readonly busy: boolean;
  readonly onBack: () => void;
  readonly onDownload: () => void;
}) {
  return (
    <div className="workspace-stack project-file-workspace" data-testid="project-file-workspace">
      <div className="session-detail-navigation">
        <button type="button" className="session-back-button" onClick={onBack}>
          <ArrowLeft size={14} /> New Session
        </button>
      </div>
      <div className="workspace-heading"><div><p className="eyebrow">{project.display_name} / File</p><h1>{entry.path}</h1><p>{entry.mediaType ?? "unknown format"} · {formatBytes(entry.byteSize)}</p></div>{fileTransferAvailable ? <button type="button" className="secondary-button" disabled={busy} onClick={onDownload}><Download size={15} /> Download</button> : null}</div>
      <section className="product-panel project-file-editor"><header><FileText size={16} /><strong>{entry.path}</strong><span>{entry.contentSha256 ? shortDigest(entry.contentSha256) : "No digest"}</span></header>{entry.content !== null ? <pre>{entry.content}</pre> : <div className="quiet-empty"><FileText size={24} /><p>This file is available to the remote Agent, but its format or size is outside the bounded browser preview.</p></div>}</section>
    </div>
  );
}

function SessionImageAttachmentsV2({
  attachments,
  disabled,
  onRemove,
}: {
  readonly attachments: readonly SessionImageAttachmentV2[];
  readonly disabled: boolean;
  readonly onRemove: (id: string) => void;
}) {
  return (
    <div className="session-image-attachments" aria-label="Pasted images">
      {attachments.map((attachment, index) => (
        <figure key={attachment.id} className="session-image-attachment">
          {attachment.previewUrl
            ? <img src={attachment.previewUrl} alt={`Pasted image ${index + 1}`} />
            : <span className="session-image-placeholder"><ImageIcon size={22} /></span>}
          <figcaption>
            <strong>Image {index + 1}</strong>
            <small>{formatBytes(attachment.file.size)}</small>
          </figcaption>
          <button
            type="button"
            aria-label={`Remove pasted image ${index + 1}`}
            disabled={disabled}
            onClick={() => onRemove(attachment.id)}
          >
            <X size={13} />
          </button>
        </figure>
      ))}
    </div>
  );
}

function StartingSessionChatV2({
  project,
  session,
}: {
  readonly project: ProjectV2;
  readonly session: StartingSessionV2;
}) {
  const phaseDetail = session.phase === "validating"
    ? "Checking the current Project Head and remote execution authority."
    : "The daemon is admitting this Session and starting the remote Agent.";
  return (
    <div className="workspace-stack session-detail-workspace" data-testid="starting-session-workspace">
      <div className="session-detail-navigation">
        <span className="session-starting-label">Starting a new Session in {project.display_name} · Project Head {session.projectHeadGeneration}</span>
      </div>
      <article className="v2-starting-session-card">
        <section className="v2-conversation-section session-chat-canvas" aria-label="Starting Session conversation">
          <div className="v2-transcript">
            <article className="user" aria-label="You"><span aria-hidden="true">You</span><p>{session.task.objective}</p></article>
          </div>
          <div className="v2-agent-activity" role="status" aria-live="polite" data-testid="starting-session-activity">
            <span className="v2-agent-running-dot" aria-hidden="true" />
            <p className="v2-agent-running-text"><strong>Starting Session</strong><span>{phaseDetail}</span></p>
          </div>
        </section>
        <div className="session-chat-composer" aria-label="Session is starting">
          <div className="session-chat-composer-box">
            <textarea aria-label="Message for the active Session" placeholder="Wait for this Session to finish before sending another message." rows={2} disabled />
            <button type="button" aria-label="Send message" disabled><ArrowUp size={17} /></button>
          </div>
        </div>
      </article>
    </div>
  );
}

function ResearchWorkspaceV2({
  project,
  tasks,
  transitions,
  taskLogs,
  artifacts,
  capability,
  runtimePresentation,
  selectedTaskId,
  newSessionDraftKey,
  startingSession,
  busy,
  sessionStartBlocked,
  sessionEvolutionAvailable,
  sessionImageUploadAvailable,
  onSelectTask,
  onOpenSettings,
  onRetryInitialization,
  onRun,
  onCancelTask,
  onRetryTask,
  onRetryEvolutionJob,
  onRetryTransition,
  onAbandonTransition,
}: {
  readonly project: ProjectV2;
  readonly tasks: readonly TaskV2[];
  readonly transitions: Readonly<Record<string, SuccessorTransitionV2>>;
  readonly taskLogs: Readonly<Record<string, readonly LogEntryV2[]>>;
  readonly artifacts: DesktopProductSnapshotV2["artifacts"];
  readonly capability: DesktopProductSnapshotV2["capability"];
  readonly runtimePresentation: DesktopProductSnapshotV2["runtimePresentation"];
  readonly selectedTaskId: string | null;
  readonly newSessionDraftKey: number;
  readonly startingSession: StartingSessionV2 | null;
  readonly busy: boolean;
  readonly sessionStartBlocked: boolean;
  readonly sessionEvolutionAvailable: boolean;
  readonly sessionImageUploadAvailable: boolean;
  readonly onSelectTask: (taskId: string | null) => void;
  readonly onOpenSettings: () => void;
  readonly onRetryInitialization: () => void;
  readonly onRun: (
    task: ScienceProjectConfigV2["task"],
    selectedEvolutionTargets: ScienceProjectConfigV2["evolution"]["targets"],
    projectHead: ProjectHeadRefV2,
    imageAttachments?: readonly SessionImageAttachmentV2[],
  ) => Promise<boolean>;
  readonly onCancelTask: (task: TaskV2) => void;
  readonly onRetryTask: (task: TaskV2) => void;
  readonly onRetryEvolutionJob: (jobId: string) => void;
  readonly onRetryTransition: (transition: SuccessorTransitionV2) => void;
  readonly onAbandonTransition: (transition: SuccessorTransitionV2) => void;
}) {
  const projectTasks = tasks
    .filter((task) => task.project_id === project.project_id)
    .sort(compareTasksNewestFirst);
  const selectedTask = projectTasks.find((task) => task.task_id === selectedTaskId) ?? null;
  const activeTask = projectTasks.find((task) => ["admitted", "preparing", "running", "cancelling", "waiting_for_successor"].includes(task.state)) ?? null;
  const [optimisticStartingSession, setOptimisticStartingSession] = useState<StartingSessionV2 | null>(null);
  const visibleStartingSession = startingSession ?? optimisticStartingSession;
  const autoOpenedActiveTaskId = useRef<string | null>(null);
  useEffect(() => {
    if (selectedTaskId !== null && !projectTasks.some((task) => task.task_id === selectedTaskId)) onSelectTask(null);
    if (activeTask !== null && selectedTaskId === activeTask.task_id) {
      autoOpenedActiveTaskId.current = activeTask.task_id;
    } else if (selectedTaskId === null && activeTask !== null && autoOpenedActiveTaskId.current !== activeTask.task_id) {
      autoOpenedActiveTaskId.current = activeTask.task_id;
      onSelectTask(activeTask.task_id);
    }
  }, [activeTask?.task_id, onSelectTask, project.project_id, selectedTaskId, tasks]);
  const ready = project.state === "ready" && project.active_project_head !== null && project.admission_etag !== null;
  const availableProjectHeads = useMemo(
    () => availableProjectHeadsV2(project, projectTasks),
    [project, projectTasks],
  );
  const [selectedProjectHeadId, setSelectedProjectHeadId] = useState(
    project.active_project_head?.project_head_id ?? "",
  );
  const [taskTitle, setTaskTitle] = useState("");
  const [taskObjective, setTaskObjective] = useState("");
  const [imageAttachments, setImageAttachments] = useState<readonly SessionImageAttachmentV2[]>([]);
  const [imagePasteError, setImagePasteError] = useState<string | null>(null);
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
    setSelectedEvolutionTargets(initialSessionEvolutionTargets());
  }, [
    initialSessionEvolutionTargets,
    project.project_id,
    project.project_config_sha256,
  ]);
  useEffect(() => {
    setTaskTitle("");
    setTaskObjective("");
    setImagePasteError(null);
    setImageAttachments((current) => {
      revokeSessionImagePreviewsV2(current);
      return [];
    });
  }, [newSessionDraftKey, project.project_id]);
  useEffect(() => {
    if (availableProjectHeads.some((head) => head.project_head_id === selectedProjectHeadId)) return;
    setSelectedProjectHeadId(project.active_project_head?.project_head_id ?? "");
  }, [availableProjectHeads, project.active_project_head?.project_head_id, selectedProjectHeadId]);
  const selectedProjectHead = availableProjectHeads.find(
    (head) => head.project_head_id === selectedProjectHeadId,
  ) ?? null;
  const selectedEvolutionCount = Object.values(selectedEvolutionTargets).filter((target) => target.enabled).length;
  const formBusy = busy || visibleStartingSession !== null;
  const normalizedTask = {
    title: taskTitle.trim(),
    objective: taskObjective.trim(),
  };
  const submittedTask = withSessionImageReferencesV2(normalizedTask, imageAttachments);
  const displayedTaskTitle = visibleStartingSession?.task.title ?? taskTitle;
  const displayedTaskObjective = visibleStartingSession?.task.objective ?? taskObjective;
  const taskValid = submittedTask.title.length > 0 && submittedTask.objective.length > 0;
  const canStartDraft = !formBusy
    && !sessionStartBlocked
    && ready
    && taskValid
    && selectedProjectHead !== null;
  const startDraftSession = async (): Promise<void> => {
    if (selectedProjectHead === null) return;
    const optimistic = {
      projectId: project.project_id,
      projectHeadGeneration: selectedProjectHead.generation,
      task: submittedTask,
      phase: "validating" as const,
    };
    setOptimisticStartingSession(optimistic);
    try {
      if (await onRun(submittedTask, selectedEvolutionTargets, selectedProjectHead, imageAttachments)) {
        revokeSessionImagePreviewsV2(imageAttachments);
        setImageAttachments([]);
        setImagePasteError(null);
      }
    } finally {
      setOptimisticStartingSession(null);
    }
  };
  const pasteSessionImages = (event: ReactClipboardEvent<HTMLTextAreaElement>): void => {
    const pastedFiles = [...event.clipboardData.files].filter((file) => file.type.startsWith("image/"));
    if (pastedFiles.length === 0) return;
    event.preventDefault();
    if (!sessionImageUploadAvailable) {
      setImagePasteError("This backend does not support pasted image uploads.");
      return;
    }
    try {
      setImageAttachments(addPastedSessionImagesV2(imageAttachments, pastedFiles));
      setImagePasteError(null);
    } catch (error) {
      setImagePasteError(userMessageV2(error));
    }
  };
  const removeSessionImage = (id: string): void => {
    setImageAttachments((current) => current.filter((attachment) => {
      if (attachment.id !== id) return true;
      revokeSessionImagePreviewsV2([attachment]);
      return false;
    }));
    setImagePasteError(null);
  };
  if (visibleStartingSession?.phase === "admitting" && selectedTask === null) {
    return <StartingSessionChatV2 project={project} session={visibleStartingSession} />;
  }
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
            <Plus size={16} />
            New Session
          </button>
        </div>
        <TaskAuthorityCardV2
          task={selectedTask}
          taskContent={taskContent}
          presentation={runtimePresentation?.tasks[selectedTask.task_id]}
          artifacts={artifacts}
          artifactPresentation={runtimePresentation?.artifacts}
          transition={transition}
          logs={taskLogs[selectedTask.task_id] ?? []}
          busy={busy}
          canContinue={ready && activeTask === null && !sessionStartBlocked}
          sessionImageUploadAvailable={sessionImageUploadAvailable}
          onContinue={(task, imageAttachments) => selectedProjectHead === null
            ? Promise.resolve(false)
            : onRun(task, selectedEvolutionTargets, selectedProjectHead, imageAttachments)}
          onCancel={() => onCancelTask(selectedTask)}
          onRetry={() => onRetryTask(selectedTask)}
          onRetryEvolutionJob={onRetryEvolutionJob}
          onRetryTransition={() => transition && onRetryTransition(transition)}
          onAbandonTransition={() => transition && onAbandonTransition(transition)}
        />
      </div>
    );
  }
  return (
    <div className="workspace-stack new-session-workspace" data-testid="research-workspace">
      <div className="workspace-heading">
        <div><h1>New Session</h1><p>{project.display_name}</p></div>
        <div className="heading-actions"><button className="secondary-button" type="button" disabled={formBusy || sessionStartBlocked} onClick={onOpenSettings}><Settings size={16} /> Edit project</button></div>
      </div>
      <div className="new-session-canvas">
        <div className="session-composer-wrap">
          {visibleStartingSession ? (
            <div className="session-submit-progress" role="status">
              <LoaderCircle className="spin" size={17} />
              <div><strong>{visibleStartingSession.phase === "validating" ? "Validating Session" : "Starting Session"}</strong><span>Your draft is preserved while you continue browsing.</span></div>
            </div>
          ) : null}
          {!ready ? (
            <div className="disabled-reason">
              <AlertCircle size={14} />
              <span>
                <strong>The next task cannot start yet.</strong>{" "}
                {project.state === "not_ready" && project.active_project_head === null
                  ? "EvoLab is preparing the remote service and initial Project Head. This page will retry automatically."
                  : "Wait for the current Evolution, settings, workspace, or runtime change to finish."}
              </span>
              {project.state === "not_ready" && project.active_project_head === null ? (
                <button type="button" className="text-button" disabled={formBusy} onClick={onRetryInitialization}>
                  Retry now
                </button>
              ) : null}
            </div>
          ) : null}
          <form
            className="session-composer next-task-fields"
            data-testid="session-composer"
            aria-label="Start a new Session"
            aria-busy={visibleStartingSession !== null}
            onSubmit={(event) => {
              event.preventDefault();
              if (canStartDraft) void startDraftSession();
            }}
          >
            <label className="session-composer-title">
              <span>Task title</span>
              <input
                maxLength={256}
                value={displayedTaskTitle}
                placeholder="Name this Session"
                disabled={formBusy || sessionStartBlocked}
                onChange={(event) => setTaskTitle(event.target.value)}
              />
            </label>
            <label className="session-composer-instructions">
              <span className="visually-hidden">Task instructions</span>
              <textarea
                rows={4}
                maxLength={65_536}
                value={displayedTaskObjective}
                placeholder="What should the Agent do next?"
                disabled={formBusy || sessionStartBlocked}
                onChange={(event) => setTaskObjective(event.target.value)}
                onPaste={pasteSessionImages}
                onKeyDown={(event) => {
                  if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
                  event.preventDefault();
                  if (canStartDraft) void startDraftSession();
                }}
              />
            </label>
            {imageAttachments.length > 0 ? (
              <SessionImageAttachmentsV2 attachments={imageAttachments} disabled={formBusy} onRemove={removeSessionImage} />
            ) : null}
            {imagePasteError ? <p className="form-error" role="status">{imagePasteError}</p> : null}
            {sessionEvolutionAvailable ? <fieldset className="session-evolution-picker" disabled={formBusy || sessionStartBlocked}>
              <legend>Evolution after this Session <span>{selectedEvolutionCount} selected</span></legend>
              <div className="session-evolution-options">{Object.entries(selectedEvolutionTargets).map(([targetId, selection]) => <article key={targetId} className={selection.enabled ? "selected" : ""}><label><input type="checkbox" checked={selection.enabled} onChange={(event) => setSelectedEvolutionTargets((current) => ({ ...current, [targetId]: { ...selection, enabled: event.target.checked } }))} /><span><strong>{targetId.replaceAll("_", " ")}</strong><small>{selection.method ?? "No method selected"}</small></span></label></article>)}</div>
            </fieldset> : null}
            {!taskValid && (taskTitle.trim().length > 0 || taskObjective.trim().length > 0) ? <p className="form-error" role="status">Enter both a task title and task instructions.</p> : null}
            <div className="session-composer-footer">
              <div className="session-head-picker">
                <Sparkles size={15} />
                <span className="visually-hidden">Evolution context</span>
                <SoftSelectV2
                  ariaLabel="Evolution context"
                  value={selectedProjectHeadId}
                  options={availableProjectHeads.map((head) => ({
                    value: head.project_head_id,
                    label: `Project Head ${head.generation}${head.project_head_id === project.active_project_head?.project_head_id ? " · recommended" : " · historical"}`,
                  }))}
                  disabled={formBusy || sessionStartBlocked || availableProjectHeads.length === 0}
                  placement="top"
                  className="session-head-select"
                  onChange={setSelectedProjectHeadId}
                />
              </div>
              <button
                type="submit"
                className="session-send-button"
                aria-label={sessionStartBlocked ? "Evolution running" : visibleStartingSession ? "Starting Session" : "Start session"}
                title={sessionStartBlocked ? "Evolution running" : visibleStartingSession ? "Starting Session" : "Start session"}
                disabled={!canStartDraft}
              >
                {formBusy ? <LoaderCircle className="spin" size={18} /> : <ArrowUp size={19} strokeWidth={2.4} />}
                <span className="visually-hidden">{sessionStartBlocked ? "Evolution running" : visibleStartingSession ? "Starting Session" : "Start session"}</span>
              </button>
            </div>
          </form>
        </div>
      </div>
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
    return "The Agent replied. EvoLab is applying the selected evolution methods.";
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

function InspectorSectionHeadingV2({
  icon: Icon,
  title,
  metric,
}: {
  readonly icon: LucideIcon;
  readonly title: string;
  readonly metric: string;
}) {
  return (
    <header className="session-inspector-section-heading">
      <span aria-hidden="true"><Icon size={16} /></span>
      <h3>{title}</h3>
      <strong>{metric}</strong>
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
  logs,
  busy,
  canContinue,
  sessionImageUploadAvailable,
  onContinue,
  onCancel,
  onRetry,
  onRetryEvolutionJob,
  onRetryTransition,
  onAbandonTransition,
}: {
  readonly task: TaskV2;
  readonly taskContent: ScienceProjectConfigV2["task"] | null;
  readonly presentation: NonNullable<DesktopProductSnapshotV2["runtimePresentation"]>["tasks"][string] | undefined;
  readonly artifacts: DesktopProductSnapshotV2["artifacts"];
  readonly artifactPresentation: NonNullable<DesktopProductSnapshotV2["runtimePresentation"]>["artifacts"] | undefined;
  readonly transition: SuccessorTransitionV2 | null;
  readonly logs: readonly LogEntryV2[];
  readonly busy: boolean;
  readonly canContinue: boolean;
  readonly sessionImageUploadAvailable: boolean;
  readonly onContinue: (
    task: ScienceProjectConfigV2["task"],
    imageAttachments?: readonly SessionImageAttachmentV2[],
  ) => Promise<boolean>;
  readonly onCancel: () => void;
  readonly onRetry: () => void;
  readonly onRetryEvolutionJob: (jobId: string) => void;
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
  const visibleTranscript = presentation?.transcript.length
    ? presentation.transcript
    : fallbackTranscript;
  const activityStage = sessionActivityStageV2(task.state, logs);
  const producedArtifacts = artifacts.filter((artifact) => presentation?.producedArtifactIds.includes(artifact.artifact_id));
  const usedArtifacts = artifacts.filter((artifact) => presentation?.usedArtifactIds.includes(artifact.artifact_id));
  const outputFiles = presentation?.outputFiles ?? [];
  const evolutionEvidenceAvailable = presentation?.evolutionEvidenceReady
    ?? task.state === "closed";
  const taskNeedsRecovery = task.state === "failed";
  const transitionNeedsRecovery = transition?.state === "failed";
  const [selectedResult, setSelectedResult] = useState<
    | { readonly kind: "artifact"; readonly artifactId: string }
    | { readonly kind: "output"; readonly fileName: string }
    | { readonly kind: "project_head" }
    | null
  >(null);
  const [followUp, setFollowUp] = useState("");
  const [imageAttachments, setImageAttachments] = useState<readonly SessionImageAttachmentV2[]>([]);
  const [imagePasteError, setImagePasteError] = useState<string | null>(null);
  const [submittingFollowUp, setSubmittingFollowUp] = useState(false);
  useEffect(() => {
    setSelectedResult(null);
    setFollowUp("");
    setImagePasteError(null);
    setImageAttachments((current) => {
      revokeSessionImagePreviewsV2(current);
      return [];
    });
  }, [task.task_id]);
  const submitFollowUp = async (): Promise<void> => {
    const objective = followUp.trim();
    if ((!objective && imageAttachments.length === 0) || !canContinue || busy || submittingFollowUp) return;
    const taskWithImages = withSessionImageReferencesV2({
      title: "",
      objective,
    }, imageAttachments);
    const firstLine = taskWithImages.objective.split(/\r?\n/, 1)[0]!.trim();
    const title = (firstLine || "Follow-up research task").slice(0, 256);
    setSubmittingFollowUp(true);
    try {
      if (await onContinue({ title, objective: taskWithImages.objective }, imageAttachments)) {
        setFollowUp("");
        revokeSessionImagePreviewsV2(imageAttachments);
        setImageAttachments([]);
        setImagePasteError(null);
      }
    } finally {
      setSubmittingFollowUp(false);
    }
  };
  const pasteSessionImages = (event: ReactClipboardEvent<HTMLTextAreaElement>): void => {
    const pastedFiles = [...event.clipboardData.files].filter((file) => file.type.startsWith("image/"));
    if (pastedFiles.length === 0) return;
    event.preventDefault();
    if (!sessionImageUploadAvailable) {
      setImagePasteError("This backend does not support pasted image uploads.");
      return;
    }
    try {
      setImageAttachments(addPastedSessionImagesV2(imageAttachments, pastedFiles));
      setImagePasteError(null);
    } catch (error) {
      setImagePasteError(userMessageV2(error));
    }
  };
  const removeSessionImage = (id: string): void => {
    setImageAttachments((current) => current.filter((attachment) => {
      if (attachment.id !== id) return true;
      revokeSessionImagePreviewsV2([attachment]);
      return false;
    }));
    setImagePasteError(null);
  };
  const [inspectorWidth, setInspectorWidth] = usePersistedPaneWidth(
    SESSION_INSPECTOR_WIDTH_KEY,
    420,
    360,
    640,
  );
  const resultInspector = selectedResult?.kind === "project_head" ? (
    <ProjectHeadInspectorV2
      projectHead={task.admission.predecessor_project_head}
      artifacts={usedArtifacts}
      artifactPresentation={artifactPresentation}
      onOpenArtifact={(artifactId) => setSelectedResult({ kind: "artifact", artifactId })}
      onClose={() => setSelectedResult(null)}
    />
  ) : selectedResult ? (
    <SessionResultInspectorV2
      selection={selectedResult}
      artifacts={artifacts}
      artifactPresentation={artifactPresentation}
      outputFiles={presentation?.outputFiles ?? []}
      onClose={() => setSelectedResult(null)}
    />
  ) : null;
  return (
    <article
      className="v2-task-card v2-task-result-detail"
      style={{ "--session-inspector-width": `${inspectorWidth}px` } as CSSProperties}
    >
      <div className="session-conversation-pane">
      <section className="v2-conversation-section session-chat-canvas" data-session-priority="conversation" aria-label="Session conversation">
        {visibleTranscript.length ? <div className="v2-transcript">{visibleTranscript.map((entry, index) => <article key={`${entry.speaker}-${"sequence" in entry ? String(entry.sequence) : index}`} className={entry.speaker} aria-label={entry.speaker === "user" ? "You" : "Agent"}><span aria-hidden="true">{entry.speaker === "user" ? "You" : "Agent"}</span><p>{entry.text}</p></article>)}</div> : !sessionInProgress ? <p className="session-chat-empty">The agent response is not loaded yet.</p> : null}
        {sessionInProgress ? (
          <div className="v2-agent-activity" role="status" aria-live="polite" data-testid="session-agent-activity">
            <span className="v2-agent-running-dot" aria-hidden="true" />
            <p className="v2-agent-running-text"><strong>{task.state === "cancelling" ? "Stopping" : "Running"}</strong><span>{activityStage}</span></p>
            {active ? <button type="button" className="danger-button" disabled={busy || task.state === "cancelling"} onClick={onCancel}>{task.state === "cancelling" ? "Cancelling" : "Cancel session"}</button> : null}
          </div>
        ) : null}
      </section>
      <form
        className="session-chat-composer"
        aria-label="Continue this project"
        onSubmit={(event) => {
          event.preventDefault();
          void submitFollowUp();
        }}
      >
        {imageAttachments.length > 0 ? (
          <SessionImageAttachmentsV2 attachments={imageAttachments} disabled={busy || submittingFollowUp} onRemove={removeSessionImage} />
        ) : null}
        {imagePasteError ? <p className="form-error" role="status">{imagePasteError}</p> : null}
        <div className="session-chat-composer-box">
          <textarea
            aria-label="Message for the next Session"
            placeholder={canContinue
              ? "Continue this research..."
              : "The next Session will be available when the current Project Head is ready."}
            rows={2}
            maxLength={65_536}
            value={followUp}
            disabled={busy || submittingFollowUp || !canContinue}
            onChange={(event) => setFollowUp(event.target.value)}
            onPaste={pasteSessionImages}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }}
          />
          <button
            type="submit"
            aria-label="Start next Session"
            disabled={(!followUp.trim() && imageAttachments.length === 0) || busy || submittingFollowUp || !canContinue}
          >
            {submittingFollowUp || busy
              ? <LoaderCircle className="spin" size={18} />
              : <ArrowUp size={18} />}
          </button>
        </div>
      </form>
      </div>
      <aside className="session-inspector-pane" aria-label="Session inspector">
      <VerticalResizeHandle label="Resize Session inspector" value={inspectorWidth} defaultValue={420} minimum={360} maximum={640} onChange={setInspectorWidth} direction={-1} edge="left" />
      {selectedResult ? <div className="session-inspector-preview-mode">{resultInspector}</div> : <>
      <header className="session-inspector-heading">
        <div><span className="panel-kicker">Session details</span><h2>{taskContent?.title ?? `Task ${task.task_id}`}</h2><small>{formatTimeV2(task.updated_at)}</small></div>
        <span className={`state-pill ${task.state}`}>{task.state.replaceAll("_", " ")}</span>
      </header>
      <section className="session-inspector-section" data-session-priority="outputs">
        <InspectorSectionHeadingV2 icon={FileText} title="Output Files" metric={`${outputFiles.length}`} />
        {outputFiles.length ? <div className="session-inspector-output-list">{outputFiles.map((file) => <button type="button" key={file.name} onClick={() => setSelectedResult({ kind: "output", fileName: file.name })}><FileText size={16} /><span><strong>{file.name}</strong><small>{file.summary}</small></span><ArrowRight size={14} /></button>)}</div> : <p className="session-inspector-empty">No files were produced by this Session.</p>}
      </section>
      <section className="session-inspector-section" data-session-priority="context">
        <InspectorSectionHeadingV2 icon={Sparkles} title="Applied Evolution Context" metric={usedArtifacts.length ? `${usedArtifacts.length}` : "Base only"} />
        <button type="button" className="session-project-head-row" onClick={() => setSelectedResult({ kind: "project_head" })}><CircleDot size={17} /><span><strong>Project Head {task.admission.predecessor_project_head.generation}</strong></span><ArrowRight size={14} /></button>
        {usedArtifacts.length ? <div className="session-inspector-context-list">{usedArtifacts.map((artifact) => {
          const preview = artifactPresentation?.[artifact.artifact_id];
          return <button type="button" key={artifact.artifact_id} title={artifact.artifact_id} onClick={() => setSelectedResult({ kind: "artifact", artifactId: artifact.artifact_id })}><span className="v2-artifact-type">{artifactTypeLabel(artifact.artifact_type)}</span><span><strong>{preview?.title ?? artifactTypeLabel(artifact.artifact_type)}</strong></span><ArrowRight size={14} /></button>;
        })}</div> : <p className="session-inspector-empty context-empty">No evolved artifacts were added beyond the pinned Project Head.</p>}
      </section>
      <section className="session-evolution-availability" data-session-priority="evolution">
        <div className="session-evolution-availability-heading"><span aria-hidden="true"><Sparkles size={17} /></span><div><strong>Available for Evolution</strong><small>{evolutionEvidenceAvailable ? "This transcript can be selected as evidence in the Evolution workspace." : "This transcript will become selectable after the Session is complete."}</small></div><span className={`muted-pill ${evolutionEvidenceAvailable ? "available" : "pending"}`}>{evolutionEvidenceAvailable ? "Available" : "Pending"}</span></div>
        {presentation?.selectedEvolution?.length ? <EvolutionJobStatusCollectionV2 selections={presentation.selectedEvolution} jobs={presentation.evolutionJobs ?? []} errors={presentation.evolutionErrors ?? []} busy={busy} onRetry={onRetryEvolutionJob} /> : null}
        {producedArtifacts.length ? <EvolutionResultCollection artifacts={producedArtifacts} artifactPresentation={artifactPresentation} jobs={presentation?.evolutionJobs ?? []} onOpen={(artifactId) => setSelectedResult({ kind: "artifact", artifactId })} /> : null}
      </section>
      {taskNeedsRecovery || transitionNeedsRecovery ? <section className="session-recovery-card" data-session-priority="recovery" role="alert">
        <AlertCircle size={18} />
        <div><strong>{taskNeedsRecovery ? "Session failed" : "Project Head update failed"}</strong><p>{taskNeedsRecovery ? "EvoLab could not complete this Session. You can retry it with the same pinned context." : transition?.error?.message ?? "EvoLab could not prepare the next Project Head."}</p><div className="session-recovery-actions">{taskNeedsRecovery ? <button type="button" className="secondary-button" disabled={busy} onClick={onRetry}>Retry Session</button> : null}{transitionNeedsRecovery ? <><button type="button" className="secondary-button" disabled={busy} onClick={onRetryTransition}>Retry successor transition</button><button type="button" className="text-button" disabled={busy} onClick={onAbandonTransition}>Discard evolution result</button></> : null}</div></div>
      </section> : null}
      </>}
      </aside>
    </article>
  );
}

function ProjectHeadInspectorV2({
  projectHead,
  artifacts,
  artifactPresentation,
  onOpenArtifact,
  onClose,
}: {
  readonly projectHead: ProjectHeadRefV2;
  readonly artifacts: DesktopProductSnapshotV2["artifacts"];
  readonly artifactPresentation: NonNullable<DesktopProductSnapshotV2["runtimePresentation"]>["artifacts"] | undefined;
  readonly onOpenArtifact: (artifactId: string) => void;
  readonly onClose: () => void;
}) {
  const executionLabel = projectHead.effective_execution_snapshot.execution_mode === "codex_subscription_transcript"
    ? "Codex subscription"
    : "Self-hosted";
  const predecessorGeneration = projectHead.generation > 0 ? projectHead.generation - 1 : null;
  const artifactCount = projectHead.evolution_revision.artifact_count;
  return (
    <section
      className="project-head-inspector"
      data-testid="project-head-inspector"
      aria-label={`Project Head ${projectHead.generation} details`}
    >
      <div className="session-result-inspector-head">
        <div>
          <span className="panel-kicker">Project Head</span>
          <h3>Project Head {projectHead.generation}</h3>
          <p>{predecessorGeneration === null ? "Initial project context" : `Built from Project Head ${predecessorGeneration}`}</p>
        </div>
        <button type="button" className="session-result-back-button" onClick={onClose}>
          <ArrowLeft size={14} /> Session details
        </button>
      </div>
      <div className="project-head-facts" aria-label="Project Head summary">
        <div><span>Evolution context</span><strong>{artifactCount} {artifactCount === 1 ? "artifact" : "artifacts"}</strong></div>
        <div><span>Workspace</span><strong>{projectHead.workspace_snapshot.entry_count} {projectHead.workspace_snapshot.entry_count === 1 ? "file" : "files"}</strong></div>
        <div><span>Workspace size</span><strong>{formatBytes(projectHead.workspace_snapshot.byte_size)}</strong></div>
        <div><span>Execution</span><strong>{executionLabel}</strong></div>
      </div>
      <section className="project-head-contents">
        <header><h4>Included evolution context</h4><span>{artifactCount}</span></header>
        {artifacts.length ? (
          <div className="project-head-artifact-list">
            {artifacts.map((artifact) => {
              const preview = artifactPresentation?.[artifact.artifact_id];
              return (
                <button type="button" key={artifact.artifact_id} onClick={() => onOpenArtifact(artifact.artifact_id)}>
                  <span className="v2-artifact-type">{artifactTypeLabel(artifact.artifact_type)}</span>
                  <strong>{preview?.title ?? artifactTypeLabel(artifact.artifact_type)}</strong>
                  <ArrowRight size={14} />
                </button>
              );
            })}
          </div>
        ) : (
          <p className="project-head-empty">{artifactCount
            ? "Artifact details are not available for this Session."
            : "This Project Head contains only the base project context."}</p>
        )}
      </section>
    </section>
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
        <button type="button" className="session-result-back-button" aria-label="Back to Session details" onClick={onClose}><ArrowLeft size={14} /> Session details</button>
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

function trackEvolutionSpotlight(event: ReactPointerEvent<HTMLElement>): void {
  const bounds = event.currentTarget.getBoundingClientRect();
  event.currentTarget.style.setProperty("--evolution-pointer-x", `${Math.round(event.clientX - bounds.left)}px`);
  event.currentTarget.style.setProperty("--evolution-pointer-y", `${Math.round(event.clientY - bounds.top)}px`);
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
  onRefresh,
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
  readonly onRefresh: () => void;
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
  const newestRun = runs[0];
  const runningRun = runs.find((run) => run.state === "running");
  const projectedEvolutionRunning = evolutionIsActiveV2(snapshot, project.project_id);
  const runningJobTaskCount = snapshot.tasks.filter((task) => (
    task.project_id === project.project_id
      && snapshot.runtimePresentation?.tasks[task.task_id]?.evolutionJobs?.some(
        (job) => job.state === "queued" || job.state === "running",
      ) === true
  )).length;
  const runningSourceCount = runningRun?.sourceTaskIds.length
    ?? (runningJobTaskCount || selectedTaskIds.length);
  const [runStarting, setRunStarting] = useState(false);
  const evolutionRunning = runStarting || projectedEvolutionRunning;
  useEffect(() => {
    if (!busy && !projectedEvolutionRunning) setRunStarting(false);
  }, [busy, projectedEvolutionRunning]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [resultView, setResultView] = useState<"history" | "detail">("detail");
  useEffect(() => {
    setSelectedRunId(newestRun?.runId ?? null);
    setResultView(newestRun === undefined ? "history" : "detail");
  }, [project.project_id, newestRun?.runId]);
  const selectedRun = runs.find((run) => run.runId === selectedRunId) ?? newestRun;
  const latestRun = selectedRun;
  useEffect(() => {
    if (!standaloneAvailable || newestRun === undefined) return;
    setTargets({
      ...project.config.evolution.targets,
      ...Object.fromEntries(newestRun.selections.map((selection) => [selection.targetId, {
        enabled: true,
        method: selection.method,
        config: selection.config as ScienceProjectConfigV2["evolution"]["targets"][string]["config"],
      }])),
    });
  }, [newestRun?.runId, project.project_config_sha256, standaloneAvailable]);
  const allJobs = Object.values(snapshot.runtimePresentation?.tasks ?? {})
    .flatMap((task) => task.evolutionJobs ?? []);
  const selectedRunArtifacts = selectedRun === undefined
    ? []
    : selectedRun.artifactIds.flatMap((artifactId) => {
      const artifact = artifacts.find((candidate) => candidate.artifact_id === artifactId);
      return artifact === undefined ? [] : [artifact];
    });
  const selectedRunArtifactKey = selectedRunArtifacts.map((artifact) => artifact.artifact_id).join("|");
  const [selectedRunArtifactId, setSelectedRunArtifactId] = useState<string | null>(null);
  const [selectedArtifactView, setSelectedArtifactView] = useState<"content" | "changes">("content");
  useEffect(() => {
    setSelectedRunArtifactId(selectedRunArtifacts[0]?.artifact_id ?? null);
    setSelectedArtifactView("content");
  }, [selectedRun?.runId, selectedRunArtifactKey]);
  const selectedArtifact = selectedRunArtifacts.find(
    (artifact) => artifact.artifact_id === selectedRunArtifactId,
  ) ?? selectedRunArtifacts[0] ?? null;
  const selectedArtifactPreview = selectedArtifact === null
    ? undefined
    : snapshot.runtimePresentation?.artifacts[selectedArtifact.artifact_id];
  const latestRunArtifacts = selectedRunArtifacts;
  const selectedLatestArtifact = selectedArtifact;
  const selectedLatestPreview = selectedArtifactPreview;
  const latestArtifactView = selectedArtifactView;
  const setLatestArtifactView = setSelectedArtifactView;
  const setSelectedLatestArtifactId = setSelectedRunArtifactId;
  const enabledSelections = Object.entries(targets).flatMap(([targetId, selection]) => (
    selection.enabled && selection.method
      ? [{ targetId, method: selection.method, config: selection.config }]
      : []
  ));
  const selectedArtifactsPending = selectedRun !== undefined
    && ["candidate_ready", "applied"].includes(selectedRun.state)
    && selectedRun.artifactIds.length > 0
    && selectedRunArtifacts.length === 0;
  const latestArtifactsPending = selectedArtifactsPending;
  const resultSectionRef = useRef<HTMLElement>(null);
  const lastResultSignature = useRef(
    latestRun === undefined ? null : `${latestRun.runId}:${latestRun.state}`,
  );
  useEffect(() => {
    const signature = latestRun === undefined ? null : `${latestRun.runId}:${latestRun.state}`;
    const previous = lastResultSignature.current;
    lastResultSignature.current = signature;
    if (
      previous !== null
      && signature !== null
      && previous !== signature
      && latestRun !== undefined
      && latestRun.state !== "running"
    ) {
      resultSectionRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [latestRun?.runId, latestRun?.state]);
  return (
    <div className="workspace-stack evolution-workspace-v2" data-testid="evolution-workspace">
      <div className="evolution-atmosphere" aria-hidden="true"><span /><span /><span /></div>
      <div className={`workspace-heading evolution-workspace-heading${evolutionRunning ? " is-running" : ""}`}>
        <div>
          <div className="evolution-heading-title">
            {evolutionRunning ? <span className="evolution-heading-running-icon" aria-hidden="true"><LoaderCircle className="spin" size={22} /></span> : null}
            <h1>{evolutionRunning ? "Evolution Running" : "Improve future Sessions"}</h1>
          </div>
          <p>{evolutionRunning ? `Creating improvements from ${runningSourceCount} selected Session${runningSourceCount === 1 ? "" : "s"}.` : "Select completed Sessions to create improvements. Review the result, then apply it to new Sessions."}</p>
        </div>
        {project.active_project_head ? <div className="evolution-head-context"><span className="evolution-head-context-icon"><ShieldCheck size={16} /></span><span><small>Currently applied</small><strong>Version {project.active_project_head.generation}</strong><em><CircleDot size={8} /> Used by new Sessions</em></span></div> : null}
      </div>
      {standaloneAvailable ? <section id="evolution-evidence" className="product-panel task-panel evolution-step-section">
        <div className="panel-heading"><div><h2>Completed Sessions</h2></div><span className="muted-pill">{selectedTaskIds.length} selected</span></div>
        <div className="evolution-section-body">{completedTasks.length === 0 ? <div className="empty-row">Complete at least one Session before running Evolution.</div> : <div className="session-evolution-options">{completedTasks.map((task, index) => {
          const selected = selectedTaskIds.includes(task.task_id);
          const evidenceReady = snapshot.runtimePresentation?.tasks[task.task_id]?.evolutionEvidenceReady === true;
          const title = snapshot.runtimePresentation?.tasks[task.task_id]?.instruction?.title ?? task.task_id;
          return <article key={task.task_id} className={selected ? "selected" : ""} data-evidence-state={evidenceReady ? "ready" : "unavailable"} onPointerMove={trackEvolutionSpotlight}><label><span className="evolution-session-index" aria-hidden="true">{String(index + 1).padStart(2, "0")}</span><input type="checkbox" checked={selected} disabled={busy || !evidenceReady} onChange={(event) => setSelectedTaskIds((current) => event.target.checked ? [...current, task.task_id] : current.filter((id) => id !== task.task_id))} /><span><strong>{title}</strong><small>{formatTimeV2(task.updated_at)} · {evidenceReady ? "transcript evidence" : "evidence unavailable"}</small></span><em><CircleDot size={9} /> {evidenceReady ? "Ready" : "Unavailable"}</em></label></article>;
        })}</div>}
        {completedTasks.length > evidenceTasks.length ? <Notice tone="warning" title="Some Sessions are unavailable" detail="Their transcript datasets were not sealed. Restart the updated development daemon to repair recoverable legacy Sessions; unavailable entries cannot be selected." /> : null}</div>
      </section> : null}
      <section id="evolution-methods" className="product-panel task-panel evolution-step-section">
        <div className="panel-heading"><div>{!standaloneAvailable ? <span className="panel-kicker">Verified remote registry</span> : null}<h2>Evolution targets</h2></div></div>
        <div className="evolution-section-body evolution-methods-body">{capabilities.length === 0 ? <Notice tone="warning" title="No visible evolution methods" detail="The active verified Core registry did not publish a Desktop-visible target for this execution profile." /> : <div className="v2-target-list">{capabilities.map((target) => {
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
          return <article key={target.target_id} className={current.enabled ? "enabled" : ""} data-target-id={target.target_id} onPointerMove={trackEvolutionSpotlight}><label className="v2-target-toggle"><span className="evolution-target-glyph" aria-hidden="true">{evolutionTargetGlyph(target.target_id)}</span><input type="checkbox" checked={current.enabled} disabled={busy} onChange={(event) => setTargets((previous) => ({ ...previous, [target.target_id]: { enabled: event.target.checked, method: event.target.checked ? methodId || null : current.method, config: current.config } }))} /><span><strong>{target.display_name}</strong><small>{target.description}</small></span></label><div className="soft-select-field compact"><span>Method</span><SoftSelectV2 ariaLabel={`${target.display_name} method`} value={methodId} disabled={busy || !current.enabled} options={[
            { key: "default", value: "", label: "No supported default" },
            ...resolvers.map((resolver) => ({ key: `resolver:${resolver.selection_value}`, value: resolver.selection_value, label: resolver.display_name, disabled: !resolver.supported })),
            ...methods.map((method) => ({ key: `method:${method.method_id}`, value: method.method_id, label: method.display_name })),
          ]} onChange={(nextMethodId) => {
            const selected = methods.find((method) => method.method_id === nextMethodId);
            let defaultConfig: ScienceProjectConfigV2["evolution"]["targets"][string]["config"] = {};
            try { defaultConfig = selected ? JSON.parse(selected.default_config_json) as typeof defaultConfig : {}; } catch { defaultConfig = {}; }
            setTargets((previous) => ({ ...previous, [target.target_id]: { enabled: true, method: nextMethodId, config: defaultConfig } }));
          }} /></div>{current.enabled && (!methodId || !selectionAccepted) ? <p className="form-error" role="alert">This target has no method accepted by the active registry.</p> : null}</article>;
        })}</div>}
        <div className="v2-primary-row">{standaloneAvailable ? <button type="button" className="primary-button" disabled={busy || snapshot.capability === null || selectedTaskIds.length === 0 || enabledSelections.length === 0} onClick={() => { setRunStarting(true); onStartRun(selectedTaskIds, enabledSelections); }}>{busy ? <LoaderCircle className="spin" size={15} /> : <Sparkles size={15} />} Run Evolution</button> : <button type="button" className="primary-button" disabled={busy || snapshot.capability === null} onClick={() => onSave({ ...project.config, evolution: { targets } })}>Save evolution configuration</button>}</div></div>
      </section>
      {standaloneAvailable ? <section ref={resultSectionRef} id="evolution-result" className="product-panel task-panel evolution-step-section">
        {resultView === "history" ? <>
          <div className="panel-heading"><div><h2>Evolution History</h2></div><span className="muted-pill">{runs.length} run{runs.length === 1 ? "" : "s"}</span></div>
          <div className="evolution-section-body evolution-history-body">{runs.length > 0 ? <nav className="v2-evolution-run-selector" aria-label="Evolution history">{runs.map((run, index) => {
          const selected = run.runId === latestRun?.runId;
          return <button type="button" key={run.runId} className={selected ? "active" : ""} data-run-state={run.state} aria-pressed={selected} onPointerMove={trackEvolutionSpotlight} onClick={() => { setSelectedRunId(run.runId); setResultView("detail"); }}><span className={`v2-evolution-job-state ${run.state}`} aria-hidden="true">{run.state === "running" ? <LoaderCircle className="spin" size={15} /> : run.state === "applied" || run.state === "candidate_ready" ? <CheckCircle2 size={15} /> : <AlertCircle size={15} />}</span><span><strong>{run.selections.map((selection) => evolutionTargetLabel(selection.targetId)).join(", ")}</strong><small>{index === 0 ? "Latest · " : ""}{formatTimeV2(run.createdAt)} · {run.sourceTaskIds.length} Session{run.sourceTaskIds.length === 1 ? "" : "s"}</small></span><span className={`state-pill ${run.state}`}>{evolutionRunStateLabel(run.state)}</span></button>;
          })}</nav> : <div className="empty-row">No Evolution Run yet. Session evidence remains available until you choose to use it.</div>}</div>
        </> : latestRun === undefined ? <div className="empty-row">No Evolution Run yet. Session evidence remains available until you choose to use it.</div> : <>
          <div className="panel-heading v2-evolution-detail-heading"><div><button type="button" className="v2-evolution-history-back" onClick={() => setResultView("history")}><ArrowLeft size={14} /> Evolution History</button><h2>Evolution Details</h2></div><span className="muted-pill">{runs.findIndex((run) => run.runId === latestRun.runId) === 0 ? "Latest run" : formatTimeV2(latestRun.createdAt)}</span></div>
          <div className="evolution-section-body evolution-result-body"><article className={`v2-evolution-job v2-current-evolution-run ${latestRun.state}`}>
          <header><div className="v2-evolution-job-title"><span className={`v2-evolution-job-state ${latestRun.state}`} aria-hidden="true">{latestRun.state === "running" ? <LoaderCircle className="spin" size={16} /> : latestRun.state === "applied" || latestRun.state === "candidate_ready" ? <CheckCircle2 size={16} /> : <AlertCircle size={16} />}</span><div><strong>{latestRun.selections.map((selection) => evolutionTargetLabel(selection.targetId)).join(", ")}</strong><small>{latestRun.sourceTaskIds.length} Session{latestRun.sourceTaskIds.length === 1 ? "" : "s"} · {formatTimeV2(latestRun.createdAt)}</small></div></div><span className={`state-pill ${latestRun.state}`}>{evolutionRunStateLabel(latestRun.state)}</span></header>
          {latestRun.baseProjectHeadId ? <div className="v2-evolution-head-flow"><span><small>Base Head</small><strong>{projectHeadDisplayName(latestRun.baseProjectHeadId)}</strong></span><ArrowRight size={16} /><span className={latestRun.appliedProjectHeadId ? "created" : "pending"}><small>{latestRun.appliedProjectHeadId ? "Created Head" : "On apply"}</small><strong>{latestRun.appliedProjectHeadId ? projectHeadDisplayName(latestRun.appliedProjectHeadId) : "New Project Head"}</strong></span></div> : null}
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
          </div> : latestArtifactsPending ? <div className="v2-current-evolution-empty loading"><LoaderCircle className="spin" size={18} /><div><strong>Loading result artifacts</strong><span>The candidate is ready. EvoLab is refreshing its readable artifact inventory.</span></div><button type="button" className="secondary-button" disabled={busy} onClick={onRefresh}>Refresh result</button></div> : latestRun.state !== "running" && latestRun.state !== "failed" ? <div className="v2-current-evolution-empty"><div><strong>No displayable result was published</strong><span>The run completed, but it did not declare an artifact that this WebUI can preview.</span></div></div> : null}
          <div className="v2-evolution-apply-bar"><div><strong>{latestRun.state === "applied" ? latestRun.appliedProjectHeadId ? `Applied as ${projectHeadDisplayName(latestRun.appliedProjectHeadId)}` : "Applied Evolution" : latestRun.state === "candidate_ready" ? "Candidate ready to apply" : "Evolution Run controls"}</strong><span>{latestRun.state === "candidate_ready" ? "Applying this candidate creates the context used by the next Project Head." : latestRun.state === "applied" ? "This historical candidate was applied to the Project Head shown above." : "Retry only the failed methods while preserving the original evidence."}</span></div><div className="v2-card-actions">{latestRun.state === "candidate_ready" ? <button type="button" className="primary-button" disabled={busy || latestArtifactsPending} onClick={() => onApplyRun(latestRun.runId)}>Apply to future Sessions</button> : null}{latestRun.state === "applied" ? <span className="muted-pill">Applied</span> : null}{allJobs.filter((job) => latestRun.jobIds.includes(job.jobId) && job.state === "failed").map((job) => <button type="button" className="secondary-button" disabled={busy} key={job.jobId} onClick={() => onRetryJob(job.jobId)}>Retry {evolutionTargetLabel(job.targetId)}</button>)}</div></div>
          </article></div>
        </>}
      </section> : null}
      {!standaloneAvailable ? <EvolutionArtifactBrowserV2 artifacts={artifacts} presentation={snapshot.runtimePresentation?.artifacts} provider={provider} /> : null}
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

function WorkspaceButton({ active, onClick, icon: Icon, children }: { readonly active: boolean; readonly onClick: () => void; readonly icon: typeof BookOpen; readonly children: string }) {
  return <button type="button" className={`product-nav-item ${active ? "active" : ""}`} aria-current={active ? "page" : undefined} onClick={onClick}><Icon size={17} /> {children}</button>;
}

type ErrorNoticeContextV2 = "refresh" | "action" | "connection" | "startup";

type ErrorNoticeCopyV2 = {
  readonly title: string;
  readonly detail: string;
  readonly guidance: string;
};

function ErrorNoticeV2({ context, error, onRetry, onDismiss }: {
  readonly context: ErrorNoticeContextV2;
  readonly error: string;
  readonly onRetry?: () => void;
  readonly onDismiss?: () => void;
}) {
  const copy = errorNoticeCopyV2(error, context);
  return <Notice
    tone="error"
    title={copy.title}
    detail={copy.detail}
    guidance={copy.guidance}
    action={onRetry ? <button type="button" className="v2-notice-action" onClick={onRetry}><RefreshCw size={12} /> Retry</button> : undefined}
    onDismiss={onDismiss}
  />;
}

function Notice({ tone, title, detail, guidance, action, compact = false, onDismiss }: {
  readonly tone: "error" | "warning" | "success" | "info";
  readonly title: string;
  readonly detail: string;
  readonly guidance?: string;
  readonly action?: React.ReactNode;
  readonly compact?: boolean;
  readonly onDismiss?: () => void;
}) {
  const icon = tone === "success" ? <CheckCircle2 size={15} /> : tone === "info" ? <CircleDot size={14} /> : <AlertCircle size={15} />;
  return <div className={`v2-notice ${tone}${compact ? " compact" : ""}`} role={tone === "error" ? "alert" : "status"}>
    <span className="v2-notice-mark" aria-hidden="true">{icon}</span>
    <div className="v2-notice-copy"><strong>{title}</strong><span>{detail}</span>{guidance ? <small>{guidance}</small> : null}</div>
    {action ? <div className="v2-notice-actions">{action}</div> : null}
    {onDismiss ? <button type="button" className="v2-notice-dismiss" aria-label="Dismiss notice" onClick={onDismiss}><X size={14} /></button> : null}
  </div>;
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
  existingExecution?: ScienceProjectConfigV2["execution"],
): ScienceProjectConfigV2 {
  const execution: ScienceProjectConfigV2["execution"] = existingExecution ?? {
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
let sessionImageSequence = 0;
function actionIdV2(prefix: string): string {
  actionSequence += 1;
  return `${prefix}-${Date.now().toString(36)}-${actionSequence.toString(36).padStart(8, "0")}`;
}

function userMessageV2(error: unknown): string {
  if (error instanceof DesktopApiErrorV2) return error.apiError.summary;
  if (isDesktopErrorV2(error)) return error.summary;
  if (error instanceof Error && error.message.length > 0 && error.message.length <= 768) return error.message;
  return "EvoLab could not complete this action. Refresh the remote state and try again.";
}

function errorNoticeCopyV2(error: string, context: ErrorNoticeContextV2): ErrorNoticeCopyV2 {
  const extracted = extractRemoteErrorDetailV2(error);
  const message = (extracted ?? error).replace(/\s+/g, " ").trim();
  const searchable = `${error} ${message}`.toLocaleLowerCase();
  const fallbackTitle: Record<ErrorNoticeContextV2, string> = {
    refresh: "Remote state unavailable",
    action: "Couldn’t complete that action",
    connection: "Couldn’t connect to the server",
    startup: "Startup couldn’t finish",
  };
  if (searchable.includes("desktop local api request failed")) {
    return {
      title: "Connection to EvoLab was lost",
      detail: "The browser cannot reach the local API that carries requests through the SSH tunnel.",
      guidance: "Keep the `openevo webui` launcher terminal running. If it stopped, start it again, then retry.",
    };
  }
  if (searchable.includes("connection refused") || searchable.includes("failed to fetch") || searchable.includes("networkerror") || searchable.includes("ssh tunnel")) {
    return {
      title: "Remote service is unreachable",
      detail: "The local tunnel is closed, or the remote Web Layer is not accepting connections yet.",
      guidance: "Check the launcher terminal and remote daemon status, then retry when both services are ready.",
    };
  }
  if (searchable.includes("timed out") || searchable.includes("timeout")) {
    return {
      title: "The server took too long to respond",
      detail: "EvoLab stopped waiting before the remote operation returned a valid response.",
      guidance: "Check the network and remote load. Retrying is safe because mutations use an idempotent action ID.",
    };
  }
  if (searchable.includes("content-length is invalid")) {
    return {
      title: "Browser and daemon versions do not match",
      detail: "The remote daemon rejected the request body before it could process the action.",
      guidance: "Commit the current WebUI build and restart `openevo webui` so the browser and remote daemon use the same revision.",
    };
  }
  if (searchable.includes("bootstrap") || searchable.includes("session expired") || /\b(401|403)\b/.test(searchable)) {
    return {
      title: "This browser session is no longer valid",
      detail: "The Web Layer rejected the browser’s temporary authentication session.",
      guidance: "Open EvoLab again from the URL printed by a running `openevo webui` launcher.",
    };
  }
  if (searchable.includes("contract") || searchable.includes("digest") || /\b426\b/.test(searchable)) {
    return {
      title: "EvoLab components are out of sync",
      detail: "The browser and remote service reported incompatible API or schema versions.",
      guidance: "Commit the current source and restart the remote launcher to deploy one matching revision.",
    };
  }
  if (searchable.includes("conflict") || searchable.includes("etag") || searchable.includes("stale") || /\b(409|412)\b/.test(searchable)) {
    return {
      title: "The remote state changed first",
      detail: "EvoLab did not apply the action because its local view was older than the server state.",
      guidance: "Refresh the page state, review the latest Project or Session, and try the action again.",
    };
  }
  if (searchable.includes("not found") || /\b404\b/.test(searchable)) {
    return {
      title: "That item is no longer available",
      detail: "The Project, Session, or file was removed before this request reached the server.",
      guidance: "Refresh the remote state to remove the outdated item from this view.",
    };
  }
  if (searchable.includes("invalid_request") || searchable.includes("invalid request") || /\b400\b/.test(searchable)) {
    return {
      title: "The remote service rejected the request",
      detail: message || "The request did not match the daemon’s expected format.",
      guidance: "Review the selected values. If they are valid, restart the launcher to ensure both sides use the same revision.",
    };
  }
  return {
    title: fallbackTitle[context],
    detail: message || "EvoLab did not receive a usable response from the remote service.",
    guidance: context === "action"
      ? "The action was not confirmed. Review the current state before trying again."
      : "Check the launcher connection and retry. Existing remote data has not been removed.",
  };
}

function extractRemoteErrorDetailV2(message: string): string | null {
  const objectStart = message.indexOf("{");
  if (objectStart < 0) return null;
  try {
    const payload = JSON.parse(message.slice(objectStart)) as unknown;
    if (typeof payload !== "object" || payload === null || Array.isArray(payload)) return null;
    const record = payload as Record<string, unknown>;
    const nested = typeof record.error === "object" && record.error !== null && !Array.isArray(record.error)
      ? record.error as Record<string, unknown>
      : null;
    const candidate = nested?.message ?? record.message ?? record.detail;
    return typeof candidate === "string" && candidate.trim() !== "" ? candidate.trim() : null;
  } catch {
    return null;
  }
}

function addPastedSessionImagesV2(
  current: readonly SessionImageAttachmentV2[],
  files: readonly File[],
): readonly SessionImageAttachmentV2[] {
  if (current.length + files.length > SESSION_IMAGE_MAX_COUNT) {
    throw new Error(`A Session can include up to ${SESSION_IMAGE_MAX_COUNT} pasted images.`);
  }
  const totalBytes = [...current.map((attachment) => attachment.file.size), ...files.map((file) => file.size)]
    .reduce((total, size) => total + size, 0);
  if (totalBytes > SESSION_IMAGE_MAX_TOTAL_BYTES) {
    throw new Error("Pasted images exceed the 64 MiB Session limit.");
  }
  for (const file of files) {
    if (SESSION_IMAGE_MEDIA_EXTENSIONS[file.type] === undefined) {
      throw new Error("Pasted images must be PNG, JPEG, WebP, or GIF files.");
    }
    if (file.size > BROWSER_FOLDER_MAX_FILE_BYTES) {
      throw new Error("A pasted image exceeds the 32 MiB per-file upload limit.");
    }
  }
  const additions = files.map((file): SessionImageAttachmentV2 => {
    const extension = SESSION_IMAGE_MEDIA_EXTENSIONS[file.type]!;
    sessionImageSequence += 1;
    const id = `pasted-image-${Date.now().toString(36)}-${sessionImageSequence.toString(36)}`;
    let previewUrl: string | null = null;
    if (typeof globalThis.URL.createObjectURL === "function") {
      try {
        previewUrl = globalThis.URL.createObjectURL(file);
      } catch {
        // A filename/size tile remains available when a test DOM or older browser
        // cannot create a local object URL for this clipboard File.
      }
    }
    return {
      id,
      file,
      path: `session-attachments/${id}.${extension}`,
      previewUrl,
    };
  });
  return [...current, ...additions];
}

function revokeSessionImagePreviewsV2(attachments: readonly SessionImageAttachmentV2[]): void {
  if (typeof globalThis.URL.revokeObjectURL !== "function") return;
  for (const attachment of attachments) {
    if (attachment.previewUrl !== null) globalThis.URL.revokeObjectURL(attachment.previewUrl);
  }
}

function withSessionImageReferencesV2(
  task: ScienceProjectConfigV2["task"],
  attachments: readonly SessionImageAttachmentV2[],
): ScienceProjectConfigV2["task"] {
  if (attachments.length === 0) return task;
  const objective = task.objective.trim() || "Review the attached image evidence.";
  const references = attachments.map((attachment) => `- ${attachment.path}`).join("\n");
  return {
    title: task.title,
    objective: `${objective}\n\nAttached images are available in the project workspace:\n${references}`,
  };
}

function browserFolderSelectionV2(files: readonly File[]): BrowserFolderSelectionV2 {
  if (files.length === 0) throw new Error("Choose a folder that contains at least one file.");
  if (files.length > BROWSER_FOLDER_MAX_FILES) {
    throw new Error(`The selected folder contains more than ${BROWSER_FOLDER_MAX_FILES} files.`);
  }
  let displayName: string | null = null;
  let totalBytes = 0;
  const paths = new Set<string>();
  const uploads = files.map((file): BrowserWorkspaceUploadV2 => {
    const parts = file.webkitRelativePath.replaceAll("\\", "/").split("/");
    if (parts.length < 2 || parts.some((part) => part === "" || part === "." || part === ".." || /[\u0000-\u001f\u007f]/.test(part))) {
      throw new Error(`The selected folder contains an unsafe path: ${file.webkitRelativePath || file.name}`);
    }
    const rootName = parts[0]!;
    if (rootName.length > 256) throw new Error("The selected folder name exceeds 256 characters.");
    if (displayName === null) displayName = rootName;
    if (displayName !== rootName) throw new Error("Choose exactly one folder snapshot.");
    const path = parts.slice(1).join("/");
    if (paths.has(path)) throw new Error(`The selected folder contains the duplicate path ${path}.`);
    paths.add(path);
    if (file.size > BROWSER_FOLDER_MAX_FILE_BYTES) {
      throw new Error(`${path} exceeds the 32 MiB per-file upload limit.`);
    }
    totalBytes += file.size;
    if (totalBytes > BROWSER_FOLDER_MAX_TOTAL_BYTES) {
      throw new Error("The selected folder exceeds the 512 MiB workspace limit.");
    }
    return { file, path };
  });
  if (displayName === null) throw new Error("Choose a folder that contains at least one file.");
  return { displayName, uploads };
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
    connecting: "Connecting with OpenSSH",
    prompt_pending: "Waiting for local authentication",
    host_key_review: "Host identity review required",
    bootstrapping: "Checking the EvoLab service",
    negotiating: "Negotiating Core authority",
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

function evolutionRunStateLabel(state: "running" | "candidate_ready" | "applied" | "failed"): string {
  const labels = {
    running: "Running",
    candidate_ready: "Candidate ready",
    applied: "Applied",
    failed: "Failed",
  } as const;
  return labels[state];
}

function projectHeadDisplayName(projectHeadId: string): string {
  const generation = projectHeadId.match(/-head-(\d+)$/)?.[1];
  return generation === undefined ? projectHeadId : `Project Head ${generation}`;
}

function evolutionTargetLabel(targetId: string): string {
  const known = {
    text_memory: "Text memory",
    skill_bundle: "Skill bundle",
    agent_system: "Agent system",
    parametric_memory: "Parametric memory",
  } as const;
  if (targetId in known) return known[targetId as keyof typeof known];
  const words = targetId.replaceAll("_", " ");
  return words.length === 0 ? targetId : `${words[0]!.toUpperCase()}${words.slice(1)}`;
}

function evolutionTargetGlyph(targetId: string): ReactNode {
  if (targetId === "agent_system") return <Activity size={16} />;
  if (targetId === "skill_bundle") return <Sparkles size={16} />;
  return <History size={16} />;
}

function formatBytes(bytes: number): string {
  if (bytes < 1_024) return `${bytes} B`;
  if (bytes < 1_024 * 1_024) return `${(bytes / 1_024).toFixed(1)} KB`;
  return `${(bytes / (1_024 * 1_024)).toFixed(1)} MB`;
}

function formatTimeV2(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  });
}
