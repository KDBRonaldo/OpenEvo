type TimelineEvent = {
  id: string;
  phase: string;
  title: string;
  message: string;
  artifact_ids: string[];
};

type ArtifactContent = {
  id: string;
  artifact_type: string;
  content: string;
  metadata: Record<string, unknown>;
};

type ArtifactDiff = {
  id: string;
  before: string;
  after: string;
  format: "unified_text";
};

type BackendError = {
  code: string;
  message: string;
  severity: "info" | "warning" | "blocking";
  category: string;
  retryable: boolean;
  repair_action:
    | "openevo_can_retry"
    | "openevo_can_install"
    | "openevo_can_reconfigure"
    | "user_action_required"
    | "unsupported";
  details: Record<string, unknown>;
};

export function timelineView(events: TimelineEvent[]) {
  return events.map((event) => ({
    id: event.id,
    phase: event.phase,
    label: event.title,
    message: event.message,
    artifactIds: event.artifact_ids,
  }));
}

export function artifactPreview(content: ArtifactContent, diff: ArtifactDiff) {
  return {
    id: content.id,
    kind: content.artifact_type,
    body: content.content,
    targetPath:
      typeof content.metadata.target_path === "string"
        ? content.metadata.target_path
        : undefined,
    lineage:
      typeof content.metadata.lineage === "object" && content.metadata.lineage !== null
        ? content.metadata.lineage
        : {},
    diff,
  };
}

export function nextActionForError(error: BackendError) {
  if (error.repair_action === "openevo_can_retry") return "Retry";
  if (error.repair_action === "openevo_can_install") return "Install with OpenEvo";
  if (error.repair_action === "openevo_can_reconfigure") return "Update configuration";
  if (error.repair_action === "user_action_required") return "User action required";
  return "Unsupported";
}
