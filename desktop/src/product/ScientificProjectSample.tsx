import {
  ArrowRight,
  BookOpenCheck,
  BrainCircuit,
  Check,
  CheckCircle2,
  CircleDot,
  FileText,
  FlaskConical,
  FolderOpen,
  Info,
  MemoryStick,
  Microscope,
  PanelLeft,
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
  type SampleSession,
  type SampleTraceKind,
} from "./scientificProjectSampleData";

type SampleWorkspace = "research" | "evolution" | "system";

export function SampleScientificProjectView({
  workspace,
  onConnectRemote,
}: {
  workspace: SampleWorkspace;
  onConnectRemote?: () => void;
}) {
  if (workspace === "evolution") return <SampleEvolutionWorkspace />;
  if (workspace === "system") return <SampleAboutWorkspace onConnectRemote={onConnectRemote} />;
  return <SampleResearchWorkspace onConnectRemote={onConnectRemote} />;
}

function SampleResearchWorkspace({ onConnectRemote }: { onConnectRemote?: () => void }) {
  const project = SAMPLE_SCIENTIFIC_PROJECT;
  const [selectedSessionId, setSelectedSessionId] = useState(project.sessions.at(-1)?.id ?? "");
  const selected =
    project.sessions.find((session) => session.id === selectedSessionId) ?? project.sessions[0];

  return (
    <div className="workspace-stack sample-workspace" data-testid="sample-research-workspace" lang="zh-CN">
      <SampleBanner onConnectRemote={onConnectRemote} />
      <div className="workspace-heading sample-heading">
        <div>
          <p className="eyebrow">Scientific project tour</p>
          <h1>{project.name}</h1>
          <p>{project.summary}</p>
        </div>
        <span className="sample-readonly-label"><ShieldCheck size={15} /> {project.badge}</span>
      </div>

      <section className="sample-project-facts" aria-label="示例项目概况">
        <div><FolderOpen size={16} /><span>输入</span><strong>{project.sourceLabel}</strong></div>
        <div><TerminalSquare size={16} /><span>执行</span><strong>{project.executionLabel}</strong></div>
        <div><BookOpenCheck size={16} /><span>捕获</span><strong>{project.captureLabel}</strong></div>
        <div><CircleDot size={16} /><span>Project Head</span><strong>Generation {project.activeProjectHeadGeneration}</strong></div>
        <div><Sparkles size={16} /><span>Evolution Revision</span><strong>{project.activeEvolutionRevision}</strong></div>
      </section>

      <section className="sample-progression" aria-labelledby="sample-progression-title">
        <div className="section-heading">
          <div><Microscope size={17} /><h2 id="sample-progression-title">三次连续任务</h2></div>
          <span>基线失败 → 机制验证</span>
        </div>
        <div className="sample-session-cards" role="tablist" aria-label="示例任务与会话" onKeyDown={handleSampleTabKeyDown}>
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
              <small>Project Head {session.pinnedProjectHeadGeneration} → {session.successorProjectHeadGeneration}</small>
              <small>Evolution {session.pinnedEvolutionRevision} → {session.successorEvolutionRevision}</small>
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
          <div><span>科研结论</span><strong>{session.finding}</strong></div>
        </div>
        <div className="brief-footer">
          <div><span>时间</span><strong>{session.occurredAt}</strong></div>
          <div><span>用时</span><strong>{session.duration}</strong></div>
          <div><span>上下文</span><strong>{session.contextUsed.length ? `${session.contextUsed.length} 类 evolution` : "Genesis"}</strong></div>
        </div>
      </section>

      <section className="product-panel sample-timeline-panel">
        <div className="panel-heading">
          <div><span className="panel-kicker">Task / session timeline</span><h2>会话时间线</h2></div>
          <span className="sample-revision-flow">
            <span>Head {session.pinnedProjectHeadGeneration}<ArrowRight size={13} />{session.successorProjectHeadGeneration}</span>
            <span>{session.pinnedEvolutionRevision}<ArrowRight size={13} />{session.successorEvolutionRevision}</span>
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
          <div><span className="panel-kicker">Codex transcript</span><h2>推理与工具调用安全摘要</h2></div>
          <span className="sample-safe-label"><ShieldCheck size={14} /> 已脱敏</span>
        </div>
        <p className="sample-trace-note">
          仅展示决策摘要和工具类别，不包含原始思维链、命令、主机位置、凭据或服务地址。
        </p>
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

function SampleEvolutionWorkspace() {
  const project = SAMPLE_SCIENTIFIC_PROJECT;
  const [selectedTargetId, setSelectedTargetId] = useState<SampleEvolutionTargetId>("text_memory");
  const selected =
    project.evolutionTargets.find((target) => target.id === selectedTargetId) ??
    project.evolutionTargets[0];

  return (
    <div className="workspace-stack sample-workspace" data-testid="sample-evolution-workspace" lang="zh-CN">
      <SampleBanner />
      <div className="workspace-heading sample-heading">
        <div>
          <p className="eyebrow">Cross-session evolution</p>
          <h1>从轨迹到 Evolution Revision {project.activeEvolutionRevision}</h1>
          <p>每次会话结束后封存输入，产物只在后续会话生效。</p>
        </div>
        <span className="sample-readonly-label"><ShieldCheck size={15} /> 内置示例 · 只读</span>
      </div>

      <section className="sample-evolution-pipeline" aria-label="跨会话演化流程">
        {["会话封存", "轨迹整理", "三类演化", "产物验证", "下一 Evolution Revision"].map((label, index) => (
          <div key={label}>
            <span>{index + 1}</span>
            <strong>{label}</strong>
            {index < 4 ? <ArrowRight size={15} /> : null}
          </div>
        ))}
      </section>

      <div className="sample-evolution-layout">
        <aside className="sample-target-list" aria-label="Evolution targets">
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
        <SampleEvolutionTargetDetail target={selected} />
      </div>
    </div>
  );
}

function SampleEvolutionTargetDetail({ target }: { target: SampleEvolutionTarget }) {
  const [view, setView] = useState<"process" | "artifact">("process");
  useEffect(() => setView("process"), [target.id]);

  return (
    <section className="sample-target-detail">
      <div className="artifact-viewer-head">
        <div>
          <span className="panel-kicker">{target.shortLabel}</span>
          <h2>{target.label}</h2>
          <p>{target.description}</p>
        </div>
        <div className="artifact-meta"><span>Evolution Revision {SAMPLE_SCIENTIFIC_PROJECT.activeEvolutionRevision}</span><span><CheckCircle2 size={13} /> Active</span></div>
      </div>
      <div className="segmented-control" role="tablist" aria-label={`${target.label}视图`} onKeyDown={handleSampleTabKeyDown}>
        <button id="sample-evolution-process-tab" aria-controls="sample-evolution-view-panel" type="button" role="tab" aria-selected={view === "process"} tabIndex={view === "process" ? 0 : -1} className={view === "process" ? "active" : ""} onClick={() => setView("process")}><Sparkles size={14} /> 演化过程</button>
        <button id="sample-evolution-artifact-tab" aria-controls="sample-evolution-view-panel" type="button" role="tab" aria-selected={view === "artifact"} tabIndex={view === "artifact" ? 0 : -1} className={view === "artifact" ? "active" : ""} onClick={() => setView("artifact")}><FileText size={14} /> 可读产物</button>
      </div>
      <div
        id="sample-evolution-view-panel"
        role="tabpanel"
        aria-labelledby={view === "process" ? "sample-evolution-process-tab" : "sample-evolution-artifact-tab"}
      >
        {view === "process" ? (
          <ol className="sample-evolution-steps">
            {target.steps.map((step, index) => (
              <li key={step.sessionId}>
                <span className="sample-step-index">{index + 1}</span>
                <div className="sample-step-copy">
                  <div><strong>Session {index + 1} → Evolution Revision {step.evolutionRevision}</strong><span><CheckCircle2 size={13} /> 已激活</span></div>
                  <small>{step.input}</small>
                  <p>{step.change}</p>
                </div>
              </li>
            ))}
          </ol>
        ) : (
          <div className="sample-artifact-document">
            <div><FileText size={15} /><strong>{target.artifact.title}</strong><span>只读预览</span></div>
            <pre>{target.artifact.content}</pre>
          </div>
        )}
      </div>
    </section>
  );
}

function SampleAboutWorkspace({ onConnectRemote }: { onConnectRemote?: () => void }) {
  return (
    <div className="workspace-stack sample-workspace" data-testid="sample-about-workspace" lang="zh-CN">
      <SampleBanner />
      <div className="workspace-heading sample-heading">
        <div>
          <p className="eyebrow">About this sample</p>
          <h1>静态、只读、不会运行</h1>
          <p>这个项目用于首启浏览产品结构，不代表本机或远端已有运行结果。</p>
        </div>
      </div>
      <section className="sample-about-grid">
        <article><ShieldCheck size={21} /><h2>明确隔离</h2><p>数据随 Desktop 发布，不请求 SSH、远端 OpenEvo 服务或外部网络。</p></article>
        <article><Info size={21} /><h2>安全摘要</h2><p>只包含策展后的科研过程，不包含原始思维链、命令、凭据或主机信息。</p></article>
        <article><FlaskConical size={21} /><h2>合成数据</h2><p>任务与数值为演示用途，不应作为真实科学结论引用。</p></article>
      </section>
      <section className="sample-next-step">
        <div><PanelLeft size={20} /><div><strong>开始真实项目</strong><p>添加远端工作区后，真实项目将使用独立的受鉴权连接与权威数据。</p></div></div>
        {onConnectRemote ? (
          <button type="button" className="primary-button" onClick={onConnectRemote}><PanelLeft size={16} /> 添加远端工作区</button>
        ) : null}
      </section>
    </div>
  );
}

function SampleBanner({ onConnectRemote }: { onConnectRemote?: () => void }) {
  return (
    <section className="sample-banner" aria-label="内置示例说明">
      <div><Info size={17} /><p><strong>内置示例 · 只读</strong><span>合成内容，仅用于浏览产品；Add a remote workspace 后才会连接或执行真实任务。</span></p></div>
      {onConnectRemote ? <button type="button" className="text-button" onClick={onConnectRemote}>Add workspace <ArrowRight size={14} /></button> : null}
    </section>
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
