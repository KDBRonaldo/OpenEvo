import {
  ArrowRight,
  BookOpenCheck,
  BrainCircuit,
  Check,
  CheckCircle2,
  FileDiff,
  FileText,
  FolderOpen,
  MemoryStick,
  Microscope,
  PanelLeft,
  Plus,
  ShieldCheck,
  Sparkles,
  TerminalSquare,
  TriangleAlert,
  Wrench,
} from "lucide-react";
import { useEffect, useState } from "react";
import {
  SAMPLE_SCIENTIFIC_PROJECT,
  type SampleEvolutionTarget,
  type SampleEvolutionTargetId,
  type SampleOutcome,
  type SampleScientificProject,
  type SampleSession,
  type SampleTraceKind,
} from "./scientificProjectSampleData";

type SampleWorkspace = "research" | "evolution" | "system";

export function SampleScientificProjectView({
  workspace,
  onConnectRemote,
  project = SAMPLE_SCIENTIFIC_PROJECT,
}: {
  workspace: SampleWorkspace;
  onConnectRemote?: () => void;
  project?: SampleScientificProject;
}) {
  if (workspace === "evolution") return <SampleEvolutionWorkspace project={project} />;
  if (workspace === "system") return <SampleAboutWorkspace project={project} onConnectRemote={onConnectRemote} />;
  return <SampleResearchWorkspace project={project} />;
}

function SampleResearchWorkspace({ project }: { project: SampleScientificProject }) {
  const [selectedSessionId, setSelectedSessionId] = useState(project.sessions.at(-1)?.id ?? "");
  useEffect(() => setSelectedSessionId(project.sessions.at(-1)?.id ?? ""), [project.id]);
  const selected =
    project.sessions.find((session) => session.id === selectedSessionId) ?? project.sessions[0];

  return (
    <div className="workspace-stack sample-workspace" data-testid="sample-research-workspace" lang="en">
      <div className="workspace-heading sample-heading">
        <div>
          <p className="eyebrow">Scientific project tour</p>
          <h1>{project.name}</h1>
          <p>{project.summary}</p>
        </div>
        <span className="sample-readonly-label"><ShieldCheck size={15} /> {project.badge}</span>
      </div>

      <section className="sample-project-facts" aria-label="Demo project overview">
        <div><FolderOpen size={16} /><span>Data</span><strong>{project.sourceLabel}</strong></div>
        <div><TerminalSquare size={16} /><span>Research engine</span><strong>{project.executionLabel}</strong></div>
        <div><BookOpenCheck size={16} /><span>Sessions</span><strong>{project.sessions.length} completed</strong></div>
        <div><Sparkles size={16} /><span>Evolution</span><strong>{project.evolutionTargets.length} components improved</strong></div>
      </section>

      <section className="sample-progression" aria-labelledby="sample-progression-title">
        <div className="section-heading">
          <div><Microscope size={17} /><h2 id="sample-progression-title">Three consecutive sessions</h2></div>
          <span>Baseline failure → validated result</span>
        </div>
        <div className="sample-session-cards" role="tablist" aria-label="Demo sessions" onKeyDown={handleSampleTabKeyDown}>
          {project.sessions.map((session) => (
            <button
              key={session.id}
              id={`sample-session-tab-${session.sequence}`}
              type="button"
              role="tab"
              aria-controls="sample-session-panel"
              aria-selected={session.id === selected.id}
              tabIndex={session.id === selected.id ? 0 : -1}
              className={`sample-session-card ${session.id === selected.id ? "active" : ""}`}
              onClick={() => setSelectedSessionId(session.id)}
            >
              <span className="sample-session-number">Task {session.sequence}</span>
              <strong>{session.title}</strong>
              <span className={`sample-outcome ${session.outcome}`}>
                {outcomeIcon(session.outcome)}
                {session.outcomeLabel}
              </span>
              <small>Generation {session.pinnedProjectHeadGeneration} → {session.successorProjectHeadGeneration}</small>
              <small>Memory, skill, and agent system updated</small>
            </button>
          ))}
        </div>
      </section>

      <SampleSessionDetail session={selected} />
    </div>
  );
}

function SampleSessionDetail({ session }: { session: SampleSession }) {
  return (
    <div
      id="sample-session-panel"
      className="sample-session-detail"
      role="tabpanel"
      aria-labelledby={`sample-session-tab-${session.sequence}`}
    >
      <section className="product-panel sample-brief-panel">
        <div className="panel-heading">
          <div><span className="panel-kicker">Task {session.sequence}</span><h2>{session.title}</h2></div>
          <span className={`sample-outcome ${session.outcome}`}>{outcomeIcon(session.outcome)}{session.outcomeLabel}</span>
        </div>
        <p className="sample-objective">{session.objective}</p>
        <div className="sample-finding">
          {session.outcome === "needs_revision" ? <TriangleAlert size={18} /> : <CheckCircle2 size={18} />}
          <div><span>Scientific finding</span><strong>{session.finding}</strong></div>
        </div>
        <div className="brief-footer">
          <div><span>Time</span><strong>{session.occurredAt}</strong></div>
          <div><span>Duration</span><strong>{session.duration}</strong></div>
          <div><span>Prior improvements</span><strong>{session.contextUsed.length ? `${session.contextUsed.length} evolved components` : "None"}</strong></div>
        </div>
      </section>

      <section className="product-panel sample-timeline-panel">
        <div className="panel-heading">
          <div><span className="panel-kicker">Task / session timeline</span><h2>Session timeline</h2></div>
          <span className="sample-revision-flow">
            <span>Generation {session.pinnedProjectHeadGeneration}<ArrowRight size={13} />{session.successorProjectHeadGeneration}</span>
            <span>Update {session.pinnedEvolutionRevision}<ArrowRight size={13} />{session.successorEvolutionRevision}</span>
          </span>
        </div>
        <ol className="sample-timeline">
          {session.timeline.map((entry) => (
            <li key={entry.id} className={entry.state}>
              <span>{entry.state === "completed" ? <Check size={12} /> : <TriangleAlert size={12} />}</span>
              <div><strong>{entry.title}</strong><p>{entry.detail}</p></div>
            </li>
          ))}
        </ol>
      </section>

      <section className="product-panel sample-trace-panel">
        <div className="panel-heading">
          <div><span className="panel-kicker">Codex transcript</span><h2>Reasoning and tool activity</h2></div>
        </div>
        <div className="sample-trace-list">
          {session.trace.map((entry) => (
            <article key={entry.id} className={`sample-trace-entry ${entry.kind}`}>
              <span className="sample-trace-icon">{traceIcon(entry.kind)}</span>
              <div>
                <div className="sample-trace-title">
                  <strong>{entry.title}</strong>
                  {entry.tool ? <span>{entry.tool}</span> : null}
                </div>
                <p>{entry.summary}</p>
              </div>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}

function SampleEvolutionWorkspace({ project }: { project: SampleScientificProject }) {
  const [selectedTargetId, setSelectedTargetId] = useState<SampleEvolutionTargetId>("text_memory");
  const selected =
    project.evolutionTargets.find((target) => target.id === selectedTargetId) ??
    project.evolutionTargets[0];

  return (
    <div className="workspace-stack sample-workspace" data-testid="sample-evolution-workspace" lang="en">
      <div className="workspace-heading sample-heading">
        <div>
          <p className="eyebrow">How OpenEvo learns</p>
          <h1>How OpenEvo improved this project</h1>
          <p>Each completed session updates selected components for the next task.</p>
        </div>
        <span className="sample-readonly-label"><ShieldCheck size={15} /> Demo</span>
      </div>

      <section className="sample-evolution-pipeline" aria-label="Cross-session improvement process">
        {["Run research", "Review result", "Update components", "Validate updates", "Use next session"].map((label, index) => (
          <div key={label}>
            <span>{index + 1}</span>
            <strong>{label}</strong>
            {index < 4 ? <ArrowRight size={15} /> : null}
          </div>
        ))}
      </section>

      <div className="sample-evolution-layout">
        <aside className="sample-target-list" aria-label="Evolved components">
          {project.evolutionTargets.map((target) => (
            <button
              key={target.id}
              type="button"
              className={target.id === selected.id ? "active" : ""}
              aria-pressed={target.id === selected.id}
              onClick={() => setSelectedTargetId(target.id)}
            >
              <span className={`artifact-icon ${target.id}`}>{targetIcon(target.id)}</span>
              <span><strong>{target.label}</strong><small>{target.methodLabel}</small></span>
              <ArrowRight size={14} />
            </button>
          ))}
        </aside>
        <SampleEvolutionTargetDetail target={selected} activeEvolutionRevision={project.activeEvolutionRevision} />
      </div>
    </div>
  );
}

function SampleEvolutionTargetDetail({ target, activeEvolutionRevision }: { target: SampleEvolutionTarget; activeEvolutionRevision: string }) {
  const [view, setView] = useState<"process" | "artifact" | "diff">("process");
  useEffect(() => setView("process"), [target.id]);

  return (
    <section className="sample-target-detail">
      <div className="artifact-viewer-head">
        <div>
          <span className="panel-kicker">{target.shortLabel}</span>
          <h2>{target.label}</h2>
          <p>{target.description}</p>
        </div>
        <div className="artifact-meta"><span>Update {activeEvolutionRevision}</span><span><CheckCircle2 size={13} /> Active</span></div>
      </div>
      <div className="segmented-control" role="tablist" aria-label={`${target.label} views`} onKeyDown={handleSampleTabKeyDown}>
        <button id="sample-evolution-process-tab" aria-controls="sample-evolution-view-panel" type="button" role="tab" aria-selected={view === "process"} tabIndex={view === "process" ? 0 : -1} className={view === "process" ? "active" : ""} onClick={() => setView("process")}><Sparkles size={14} /> Evolution</button>
        <button id="sample-evolution-artifact-tab" aria-controls="sample-evolution-view-panel" type="button" role="tab" aria-selected={view === "artifact"} tabIndex={view === "artifact" ? 0 : -1} className={view === "artifact" ? "active" : ""} onClick={() => setView("artifact")}><FileText size={14} /> Output</button>
        <button id="sample-evolution-diff-tab" aria-controls="sample-evolution-view-panel" type="button" role="tab" aria-selected={view === "diff"} tabIndex={view === "diff" ? 0 : -1} className={view === "diff" ? "active" : ""} onClick={() => setView("diff")}><FileDiff size={14} /> Changes</button>
      </div>
      <div
        id="sample-evolution-view-panel"
        role="tabpanel"
        aria-labelledby={view === "process" ? "sample-evolution-process-tab" : view === "artifact" ? "sample-evolution-artifact-tab" : "sample-evolution-diff-tab"}
      >
        {view === "process" ? (
          <ol className="sample-evolution-steps">
            {target.steps.map((step, index) => (
              <li key={step.sessionId}>
                <span className="sample-step-index">{index + 1}</span>
                <div className="sample-step-copy">
                  <div><strong>After session {index + 1} · {step.evolutionRevision}</strong><span><CheckCircle2 size={13} /> Active</span></div>
                  <small>{step.input}</small>
                  <p>{step.change}</p>
                </div>
              </li>
            ))}
          </ol>
        ) : view === "artifact" ? (
          <div className="sample-artifact-document">
            <div><FileText size={15} /><strong>{target.artifact.title}</strong><span>Preview</span></div>
            <pre>{target.artifact.content}</pre>
          </div>
        ) : (
          <div className="diff-view sample-artifact-diff" aria-label={`${target.artifact.title} changes`}>
            <div className="diff-document-heading">
              <span>modified</span>
              <h3>{target.artifact.title} · {target.artifact.previousRevision} → {target.artifact.currentRevision}</h3>
            </div>
            {target.artifact.diff.map((line, index) => (
              <div key={`${line.kind}-${index}`} className={`diff-line ${line.kind}`}>
                <span>{line.kind === "added" ? "+" : "-"}</span>
                <code>{line.text}</code>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

function SampleAboutWorkspace({ onConnectRemote }: { project: SampleScientificProject; onConnectRemote?: () => void }) {
  return (
    <div className="workspace-stack sample-workspace" data-testid="sample-about-workspace" lang="en">
      <div className="workspace-heading sample-heading">
        <div>
          <p className="eyebrow">System</p>
          <h1>No remote workspace</h1>
          <p>Add the Linux server that will run your research sessions.</p>
        </div>
      </div>
      <section className="sample-next-step">
        <div><PanelLeft size={20} /><div><strong>Connect a research server</strong><p>OpenEvo Desktop will install and manage the remote Daemon.</p></div></div>
        {onConnectRemote ? (
          <button type="button" className="primary-button" onClick={onConnectRemote}><Plus size={16} /> Add remote workspace</button>
        ) : null}
      </section>
    </div>
  );
}

function outcomeIcon(outcome: SampleOutcome) {
  if (outcome === "needs_revision") return <TriangleAlert size={13} />;
  if (outcome === "partial") return <Wrench size={13} />;
  return <CheckCircle2 size={13} />;
}

function traceIcon(kind: SampleTraceKind) {
  if (kind === "reasoning_summary") return <BrainCircuit size={17} />;
  if (kind === "tool_call") return <Wrench size={17} />;
  return <CheckCircle2 size={17} />;
}

function targetIcon(target: SampleEvolutionTargetId) {
  if (target === "text_memory") return <MemoryStick size={17} />;
  if (target === "skill_bundle") return <Sparkles size={17} />;
  return <TerminalSquare size={17} />;
}

function handleSampleTabKeyDown(event: React.KeyboardEvent<HTMLElement>) {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  const tabs = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]'));
  const current = tabs.indexOf(document.activeElement as HTMLButtonElement);
  if (current < 0 || tabs.length === 0) return;
  event.preventDefault();
  const next = event.key === "Home"
    ? 0
    : event.key === "End"
      ? tabs.length - 1
      : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
  tabs[next]?.focus();
  tabs[next]?.click();
}
