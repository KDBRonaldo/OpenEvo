// Renderer-owned product-tour content. It never enters provider or Core state.
export const ENZYME_KINETICS_SAMPLE_PROJECT_ID = "desktop-sample-enzyme-kinetics";
export const PROTEIN_STABILITY_SAMPLE_PROJECT_ID = "desktop-sample-protein-stability";
export const SAMPLE_SCIENTIFIC_PROJECT_ID = ENZYME_KINETICS_SAMPLE_PROJECT_ID;

export type SampleScientificProjectId =
  | typeof ENZYME_KINETICS_SAMPLE_PROJECT_ID
  | typeof PROTEIN_STABILITY_SAMPLE_PROJECT_ID;

export type SampleOutcome = "needs_revision" | "partial" | "validated";
export type SampleTraceKind = "reasoning_summary" | "tool_call" | "tool_result";
export type SampleEvolutionTargetId = "text_memory" | "skill_bundle" | "agent_system";

export interface SampleTimelineEntry {
  readonly id: string;
  readonly title: string;
  readonly detail: string;
  readonly state: "completed" | "attention";
}

export interface SampleTraceEntry {
  readonly id: string;
  readonly kind: SampleTraceKind;
  readonly title: string;
  readonly summary: string;
  readonly tool?: string;
}

export interface SampleSession {
  readonly id: string;
  readonly sequence: number;
  readonly title: string;
  readonly objective: string;
  readonly occurredAt: string;
  readonly duration: string;
  readonly outcome: SampleOutcome;
  readonly outcomeLabel: string;
  readonly finding: string;
  readonly pinnedProjectHeadGeneration: number;
  readonly successorProjectHeadGeneration: number;
  readonly pinnedEvolutionRevision: string;
  readonly successorEvolutionRevision: string;
  readonly contextUsed: readonly SampleEvolutionTargetId[];
  readonly timeline: readonly SampleTimelineEntry[];
  readonly trace: readonly SampleTraceEntry[];
}

export interface SampleEvolutionStep {
  readonly sessionId: string;
  readonly evolutionRevision: string;
  readonly input: string;
  readonly change: string;
  readonly status: "active";
}

export interface SampleArtifactDocument {
  readonly title: string;
  readonly content: string;
  readonly previousRevision: string;
  readonly currentRevision: string;
  readonly diff: readonly {
    readonly kind: "removed" | "added";
    readonly text: string;
  }[];
}

export interface SampleEvolutionTarget {
  readonly id: SampleEvolutionTargetId;
  readonly label: string;
  readonly shortLabel: string;
  readonly methodLabel: string;
  readonly description: string;
  readonly steps: readonly SampleEvolutionStep[];
  readonly artifact: SampleArtifactDocument;
}

export interface SampleScientificProject {
  readonly id: SampleScientificProjectId;
  readonly name: string;
  readonly badge: string;
  readonly summary: string;
  readonly sourceLabel: string;
  readonly executionLabel: string;
  readonly captureLabel: string;
  readonly activeProjectHeadGeneration: number;
  readonly activeEvolutionRevision: string;
  readonly sessions: readonly SampleSession[];
  readonly evolutionTargets: readonly SampleEvolutionTarget[];
}

export const SAMPLE_SCIENTIFIC_PROJECT: SampleScientificProject = {
  id: SAMPLE_SCIENTIFIC_PROJECT_ID,
  name: "酶动力学模型复核",
  badge: "内置示例 · 只读",
  summary:
    "用一组脱敏的合成酶速率数据，从基线拟合失败逐步得到可复核的底物抑制模型。",
  sourceLabel: "内置合成数据 · 12 个观测点",
  executionLabel: "Codex subscription",
  captureLabel: "Transcript",
  activeProjectHeadGeneration: 3,
  activeEvolutionRevision: "ER-3",
  sessions: [
    {
      id: "sample-session-01",
      sequence: 1,
      title: "建立 Michaelis-Menten 基线",
      objective:
        "估计 Vmax 与 Km，报告拟合质量，并检查输入浓度与速率单位是否一致。",
      occurredAt: "2026-06-18 09:20",
      duration: "3 分 42 秒",
      outcome: "needs_revision",
      outcomeLabel: "结论未通过",
      finding:
        "线性化拟合对高浓度点产生系统性偏差，且浓度换算未在拟合前显式验证；Codex 主动拒绝给出可信参数。",
      pinnedProjectHeadGeneration: 0,
      successorProjectHeadGeneration: 1,
      pinnedEvolutionRevision: "ER-0",
      successorEvolutionRevision: "ER-1",
      contextUsed: [],
      timeline: [
        {
          id: "s1-admitted",
          title: "任务已固定",
          detail: "固定输入表、任务目标、Project Head 0 与 Evolution Revision ER-0。",
          state: "completed",
        },
        {
          id: "s1-inspected",
          title: "数据检查完成",
          detail: "识别 12 个观测点和两种单位标记。",
          state: "completed",
        },
        {
          id: "s1-fit",
          title: "基线拟合完成",
          detail: "残差在高底物浓度区持续为负。",
          state: "attention",
        },
        {
          id: "s1-closed",
          title: "会话封存",
          detail: "保留失败证据，并原子提交包含 Evolution Revision ER-1 的 Project Head 1。",
          state: "completed",
        },
      ],
      trace: [
        {
          id: "s1-r1",
          kind: "reasoning_summary",
          title: "推理摘要",
          summary:
            "先检查量纲，再比较原始空间与倒数空间残差；线性化结果不能单独支持参数结论。",
        },
        {
          id: "s1-t1",
          kind: "tool_call",
          title: "读取数据表",
          tool: "Dataset reader",
          summary: "读取列名、单位和 12 行数值；未展示文件位置或原始运行参数。",
        },
        {
          id: "s1-t2",
          kind: "tool_call",
          title: "拟合基线模型",
          tool: "Scientific fit",
          summary: "拟合二参数 Michaelis-Menten 模型，并计算标准化残差。",
        },
        {
          id: "s1-o1",
          kind: "tool_result",
          title: "安全结果摘要",
          summary:
            "高浓度区残差同向，留出误差为 18.4%；结果被标记为不可用于最终结论。",
        },
      ],
    },
    {
      id: "sample-session-02",
      sequence: 2,
      title: "按记忆与技能重新拟合",
      objective:
        "统一浓度单位，使用原始空间的稳健非线性拟合，并对高浓度残差做独立诊断。",
      occurredAt: "2026-06-18 10:05",
      duration: "4 分 18 秒",
      outcome: "partial",
      outcomeLabel: "拟合成功，模型待扩展",
      finding:
        "Vmax 与 Km 在重复拟合中稳定，但高浓度速率下降无法由基础模型解释，提示需要检验底物抑制。",
      pinnedProjectHeadGeneration: 1,
      successorProjectHeadGeneration: 2,
      pinnedEvolutionRevision: "ER-1",
      successorEvolutionRevision: "ER-2",
      contextUsed: ["text_memory", "skill_bundle", "agent_system"],
      timeline: [
        {
          id: "s2-admitted",
          title: "Project Head 1 已固定",
          detail: "从 Evolution Revision ER-1 加载单位检查记忆、稳健拟合技能和验证指令。",
          state: "completed",
        },
        {
          id: "s2-normalized",
          title: "单位已规范化",
          detail: "所有浓度统一为 mM，并保留换算记录。",
          state: "completed",
        },
        {
          id: "s2-fit",
          title: "稳健拟合完成",
          detail: "参数稳定，但高浓度残差仍有结构。",
          state: "attention",
        },
        {
          id: "s2-closed",
          title: "会话封存",
          detail: "记录模型缺口，并原子提交包含 Evolution Revision ER-2 的 Project Head 2。",
          state: "completed",
        },
      ],
      trace: [
        {
          id: "s2-r1",
          kind: "reasoning_summary",
          title: "推理摘要",
          summary:
            "执行注入的检查顺序：单位审计、稳健拟合、残差分层，再决定是否接受基础模型。",
        },
        {
          id: "s2-t1",
          kind: "tool_call",
          title: "规范化观测值",
          tool: "Table transform",
          summary: "将浓度统一到 mM；只记录列级转换与行数。",
        },
        {
          id: "s2-t2",
          kind: "tool_call",
          title: "稳健非线性拟合",
          tool: "Scientific fit",
          summary: "使用多起点拟合并检查参数边界、收敛一致性与残差分层。",
        },
        {
          id: "s2-o1",
          kind: "tool_result",
          title: "安全结果摘要",
          summary:
            "重复拟合参数变异低于 2%，但最高浓度三点均被基础模型高估。",
        },
      ],
    },
    {
      id: "sample-session-03",
      sequence: 3,
      title: "验证底物抑制模型",
      objective:
        "比较基础模型与底物抑制模型，使用预先保留的观测点验证，并报告不确定性。",
      occurredAt: "2026-06-18 11:10",
      duration: "5 分 06 秒",
      outcome: "validated",
      outcomeLabel: "验证通过",
      finding:
        "底物抑制模型显著降低留出误差，残差不再呈浓度相关结构；参数区间与诊断均已报告。",
      pinnedProjectHeadGeneration: 2,
      successorProjectHeadGeneration: 3,
      pinnedEvolutionRevision: "ER-2",
      successorEvolutionRevision: "ER-3",
      contextUsed: ["text_memory", "skill_bundle", "agent_system"],
      timeline: [
        {
          id: "s3-admitted",
          title: "Project Head 2 已固定",
          detail: "从 Evolution Revision ER-2 加载模型比较检查表与留出验证约束。",
          state: "completed",
        },
        {
          id: "s3-compared",
          title: "候选模型已比较",
          detail: "在相同训练观测上比较两个候选模型。",
          state: "completed",
        },
        {
          id: "s3-heldout",
          title: "留出验证通过",
          detail: "留出误差从 16.9% 降至 4.7%。",
          state: "completed",
        },
        {
          id: "s3-closed",
          title: "项目头已推进",
          detail: "产物验证后原子提交包含 Evolution Revision ER-3 的 Project Head 3。",
          state: "completed",
        },
      ],
      trace: [
        {
          id: "s3-r1",
          kind: "reasoning_summary",
          title: "推理摘要",
          summary:
            "不以训练误差决定模型；先固定留出点，再比较预测误差、残差结构与参数可辨识性。",
        },
        {
          id: "s3-t1",
          kind: "tool_call",
          title: "比较候选模型",
          tool: "Model comparison",
          summary: "在同一训练划分上拟合基础模型和底物抑制模型。",
        },
        {
          id: "s3-t2",
          kind: "tool_call",
          title: "执行留出验证",
          tool: "Validation",
          summary: "计算预留观测的相对误差和残差趋势，不包含原始命令或环境信息。",
        },
        {
          id: "s3-o1",
          kind: "tool_result",
          title: "安全结果摘要",
          summary:
            "底物抑制模型留出误差 4.7%，残差无单调趋势；结论通过项目验证规则。",
        },
      ],
    },
  ],
  evolutionTargets: [
    {
      id: "text_memory",
      label: "文本记忆",
      shortLabel: "Memory",
      methodLabel: "Textual memory",
      description: "把跨任务仍有价值的实验事实与失败教训写入下一会话上下文。",
      steps: [
        {
          sessionId: "sample-session-01",
          evolutionRevision: "ER-1",
          input: "Session 1 transcript",
          change: "记住单位必须在拟合前统一，并保留换算记录。",
          status: "active",
        },
        {
          sessionId: "sample-session-02",
          evolutionRevision: "ER-2",
          input: "Session 2 transcript",
          change: "加入高浓度残差持续同向时检查底物抑制的项目事实。",
          status: "active",
        },
        {
          sessionId: "sample-session-03",
          evolutionRevision: "ER-3",
          input: "Session 3 transcript",
          change: "记录已验证模型、误差与适用范围，供后续任务复用。",
          status: "active",
        },
      ],
      artifact: {
        title: "memory.md",
        content: `# 项目记忆

## 数据约定
- 底物浓度统一使用 mM；原始单位与换算必须保留在结果摘要中。
- 速率单位为 µmol/min，拟合前检查零值、重复值与量纲。

## 已验证事实
- 基础 Michaelis-Menten 模型在高浓度区产生同向残差。
- 底物抑制模型的留出相对误差为 4.7%，当前可用于解释这组数据。

## 下次会话
- 新增观测后重新计算参数区间，不沿用旧置信区间。`,
        previousRevision: "ER-2",
        currentRevision: "ER-3",
        diff: [
          { kind: "removed", text: "高浓度残差提示底物抑制，结论仍待留出验证。" },
          { kind: "added", text: "底物抑制模型留出相对误差为 4.7%，残差不再呈浓度趋势。" },
        ],
      },
    },
    {
      id: "skill_bundle",
      label: "轨迹到技能",
      shortLabel: "Skill",
      methodLabel: "Trajectory-to-skill",
      description: "从会话轨迹提炼可重复执行的科研分析步骤，而不是保存一次性命令。",
      steps: [
        {
          sessionId: "sample-session-01",
          evolutionRevision: "ER-1",
          input: "Failed fit trajectory",
          change: "生成“先审计单位，再拟合”的最小检查流程。",
          status: "active",
        },
        {
          sessionId: "sample-session-02",
          evolutionRevision: "ER-2",
          input: "Robust fit trajectory",
          change: "补充多起点拟合、参数边界与分层残差诊断。",
          status: "active",
        },
        {
          sessionId: "sample-session-03",
          evolutionRevision: "ER-3",
          input: "Validated comparison trajectory",
          change: "加入固定留出集、候选模型比较和不确定性报告。",
          status: "active",
        },
      ],
      artifact: {
        title: "SKILL.md",
        content: `# Enzyme Kinetics Review

1. 读取列级 schema，确认浓度和速率单位。
2. 在原始响应空间拟合；至少使用三个可复现起点。
3. 检查参数边界、收敛一致性和按浓度排序的残差。
4. 若高浓度残差持续同向，比较底物抑制候选模型。
5. 使用任务开始前固定的留出点做模型选择。
6. 报告参数区间、留出误差、适用范围和未解决限制。`,
        previousRevision: "ER-2",
        currentRevision: "ER-3",
        diff: [
          { kind: "removed", text: "发现结构残差后记录候选机制。" },
          { kind: "added", text: "使用任务开始前固定的留出点比较候选模型并报告不确定性。" },
        ],
      },
    },
    {
      id: "agent_system",
      label: "Agent 系统",
      shortLabel: "Agent system",
      methodLabel: "Agent-system evolution",
      description: "把项目级验证纪律写成 Codex 下一会话必须遵循的指令。",
      steps: [
        {
          sessionId: "sample-session-01",
          evolutionRevision: "ER-1",
          input: "Rejected conclusion",
          change: "要求量纲或残差检查失败时不得给出最终参数。",
          status: "active",
        },
        {
          sessionId: "sample-session-02",
          evolutionRevision: "ER-2",
          input: "Structured residual evidence",
          change: "要求基础模型存在结构残差时显式比较机制候选。",
          status: "active",
        },
        {
          sessionId: "sample-session-03",
          evolutionRevision: "ER-3",
          input: "Held-out validation",
          change: "要求最终结论同时给出留出指标和不确定性。",
          status: "active",
        },
      ],
      artifact: {
        title: "AGENTS.md",
        content: `# 项目分析约束

- 在任何拟合前确认单位、缺失值和观测范围。
- 不以线性化图或训练误差单独接受动力学参数。
- 发现有结构的残差时，先提出可证伪的机制候选。
- 模型比较必须使用预先固定的留出观测。
- 最终答复必须包含参数区间、验证指标和适用范围。
- 任一检查失败时，明确标记结论未通过，不补造结果。`,
        previousRevision: "ER-2",
        currentRevision: "ER-3",
        diff: [
          { kind: "removed", text: "基础模型存在结构残差时显式比较机制候选。" },
          { kind: "added", text: "最终结论必须同时给出固定留出指标、不确定性和适用范围。" },
        ],
      },
    },
  ],
};

export const PROTEIN_STABILITY_SAMPLE_PROJECT: SampleScientificProject = {
  id: PROTEIN_STABILITY_SAMPLE_PROJECT_ID,
  name: "蛋白质稳定性证据整合",
  badge: "内置示例 · 合成数据 · 只读",
  summary:
    "用合成 DSF 与 SEC 观测，从受板次和缓冲液混杂的失败排序，逐步得到有条件边界的 L42F 稳定性结论。",
  sourceLabel: "内置 synthetic 数据 · 48 条 DSF 曲线 + 12 条 SEC 汇总",
  executionLabel: "Codex subscription",
  captureLabel: "Transcript",
  activeProjectHeadGeneration: 3,
  activeEvolutionRevision: "ER-PS-3",
  sessions: [
    {
      id: "protein-session-01",
      sequence: 1,
      title: "审查合并后的热转移排序",
      objective: "比较野生型与 L42F 的表观 Tm，并确认板次、缓冲液和重复结构是否支持直接排序。",
      occurredAt: "2026-06-22 09:10",
      duration: "4 分 12 秒",
      outcome: "needs_revision",
      outcomeLabel: "结论未通过",
      finding: "构建体与板次、pH 条件共线，跨板绝对荧光也未校准；合并后的热转移排序不能支持稳定性结论。",
      pinnedProjectHeadGeneration: 0,
      successorProjectHeadGeneration: 1,
      pinnedEvolutionRevision: "ER-PS-0",
      successorEvolutionRevision: "ER-PS-1",
      contextUsed: [],
      timeline: [
        { id: "ps1-admitted", title: "任务已固定", detail: "固定 Project Head 0、Evolution Revision ER-PS-0 和 synthetic 输入摘要。", state: "completed" },
        { id: "ps1-audit", title: "实验设计审计", detail: "发现构建体、板次和缓冲液条件无法在合并表中分离。", state: "attention" },
        { id: "ps1-curves", title: "曲线质量检查", detail: "12 条曲线存在聚集前兆，跨板荧光尺度不一致。", state: "attention" },
        { id: "ps1-closed", title: "失败证据已封存", detail: "拒绝构建体排序，并提交包含 ER-PS-1 的 Project Head 1。", state: "completed" },
      ],
      trace: [
        { id: "ps1-r1", kind: "reasoning_summary", title: "推理摘要", summary: "先验证实验设计能否区分构建体效应，再检查曲线形状；不把表观 Tm 自动等同于稳定性。" },
        { id: "ps1-t1", kind: "tool_call", title: "读取实验设计", tool: "Plate metadata review", summary: "汇总构建体、板次、pH、重复和对照覆盖情况，不展示文件路径或命令。" },
        { id: "ps1-t2", kind: "tool_call", title: "检查 DSF 曲线", tool: "Curve quality review", summary: "检查 48 条 synthetic 曲线的基线、转变区和聚集前兆。" },
        { id: "ps1-o1", kind: "tool_result", title: "安全结果摘要", summary: "混杂与尺度差异足以推翻原排序；当前不支持推荐任何构建体。" },
      ],
    },
    {
      id: "protein-session-02",
      sequence: 2,
      title: "执行板内归一化与重复拟合",
      objective: "按匹配的 pH 7.4 板次归一化，估计重复感知的热转移效应，并检查聚集是否阻断解释。",
      occurredAt: "2026-06-23 10:05",
      duration: "5 分 31 秒",
      outcome: "partial",
      outcomeLabel: "暂定结果，待正交验证",
      finding: "L42F 的板内 ΔTm 为 2.8 C，但转变附近出现聚集信号；该结果只能标记为暂定，不能形成构建体推荐。",
      pinnedProjectHeadGeneration: 1,
      successorProjectHeadGeneration: 2,
      pinnedEvolutionRevision: "ER-PS-1",
      successorEvolutionRevision: "ER-PS-2",
      contextUsed: ["text_memory", "skill_bundle", "agent_system"],
      timeline: [
        { id: "ps2-admitted", title: "Project Head 1 已固定", detail: "从 ER-PS-1 加载混杂事实、板内分析技能和禁止过度结论的指令。", state: "completed" },
        { id: "ps2-normalized", title: "板内归一化完成", detail: "仅在匹配的 pH 7.4 板次内比较构建体和对照。", state: "completed" },
        { id: "ps2-fit", title: "重复拟合完成", detail: "L42F 的 ΔTm 估计稳定，但聚集标记仍然存在。", state: "attention" },
        { id: "ps2-closed", title: "暂定证据已封存", detail: "要求 SEC 单体保留率验证，并提交包含 ER-PS-2 的 Project Head 2。", state: "completed" },
      ],
      trace: [
        { id: "ps2-r1", kind: "reasoning_summary", title: "推理摘要", summary: "遵循 ER-PS-1：板内归一化、重复感知拟合、曲线形状审查，任一聚集警告都会阻止推荐。" },
        { id: "ps2-t1", kind: "tool_call", title: "归一化板内对照", tool: "Plate normalization", summary: "按板次和缓冲液匹配对照，保留每个重复的身份。" },
        { id: "ps2-t2", kind: "tool_call", title: "拟合热转移效应", tool: "Replicate-aware fit", summary: "估计 ΔTm 与 bootstrap 区间，并输出曲线质量标记。" },
        { id: "ps2-o1", kind: "tool_result", title: "安全结果摘要", summary: "ΔTm 为 2.8 C，但聚集前兆使结论保持暂定；下一会话必须引入正交 assay。" },
      ],
    },
    {
      id: "protein-session-03",
      sequence: 3,
      title: "用 SEC 正交验证稳定性",
      objective: "在盲化构建体匹配后整合 DSF 与 SEC 单体保留率，并按预先声明的规则判断是否支持 L42F。",
      occurredAt: "2026-06-24 11:20",
      duration: "6 分 08 秒",
      outcome: "validated",
      outcomeLabel: "条件化结论通过",
      finding: "在测试的 pH 7.4 条件下，L42F ΔTm 为 3.1 C（95% 区间 2.4–3.8 C），SEC 单体保留率为 92%，野生型为 87%；其他缓冲液和长期储存仍未验证。",
      pinnedProjectHeadGeneration: 2,
      successorProjectHeadGeneration: 3,
      pinnedEvolutionRevision: "ER-PS-2",
      successorEvolutionRevision: "ER-PS-3",
      contextUsed: ["text_memory", "skill_bundle", "agent_system"],
      timeline: [
        { id: "ps3-admitted", title: "Project Head 2 已固定", detail: "从 ER-PS-2 加载暂定效应、正交验证技能和报告边界。", state: "completed" },
        { id: "ps3-joined", title: "盲化 assay 已匹配", detail: "按构建体和批次连接 DSF 重复与 SEC 汇总。", state: "completed" },
        { id: "ps3-validated", title: "预声明规则通过", detail: "热转移与单体保留率方向一致，区间和适用条件完整。", state: "completed" },
        { id: "ps3-closed", title: "下一项目头已就绪", detail: "提交包含 ER-PS-3 的 Project Head 3；下一 session 使用该新 revision。", state: "completed" },
      ],
      trace: [
        { id: "ps3-r1", kind: "reasoning_summary", title: "推理摘要", summary: "不重新定义通过标准；按盲化映射整合两个 assay，并把结论限制在已测试的 pH 7.4 条件。" },
        { id: "ps3-t1", kind: "tool_call", title: "连接正交证据", tool: "Evidence join", summary: "连接构建体匹配的 synthetic DSF 与 SEC 摘要，保留批次和重复身份。" },
        { id: "ps3-t2", kind: "tool_call", title: "检查接受规则", tool: "Validation checklist", summary: "检查效应区间、assay 一致性、单体保留率和适用范围。" },
        { id: "ps3-o1", kind: "tool_result", title: "安全结果摘要", summary: "L42F 在 pH 7.4 条件下获得支持；未声称其他缓冲液或长期储存稳定性。" },
      ],
    },
  ],
  evolutionTargets: [
    {
      id: "text_memory",
      label: "文本记忆",
      shortLabel: "Memory",
      methodLabel: "Textual memory",
      description: "保存实验设计限制、失败结论、暂定效应和最终适用范围。",
      steps: [
        { sessionId: "protein-session-01", evolutionRevision: "ER-PS-1", input: "Confounded baseline transcript", change: "记录板次、pH 与构建体共线，禁止复用合并排序。", status: "active" },
        { sessionId: "protein-session-02", evolutionRevision: "ER-PS-2", input: "Plate-aware fit transcript", change: "保存 2.8 C 暂定效应与聚集警告，并要求 SEC 验证。", status: "active" },
        { sessionId: "protein-session-03", evolutionRevision: "ER-PS-3", input: "Orthogonal validation transcript", change: "记录 3.1 C 区间、单体保留率和 pH 7.4 适用边界。", status: "active" },
      ],
      artifact: {
        title: "memory.md",
        content: `# 蛋白质稳定性项目记忆

## 已验证事实
- 在 pH 7.4 条件下，L42F ΔTm 为 3.1 C（95% 区间 2.4–3.8 C）。
- synthetic SEC 汇总显示 L42F 单体保留率 92%，野生型 87%。

## 限制
- 不跨未匹配板次合并绝对荧光。
- 其他缓冲液和长期储存稳定性尚未验证。

## 下一会话
- 使用 Project Head 3 与 Evolution Revision ER-PS-3；新增批次后重新验证区间和单体保留率。`,
        previousRevision: "ER-PS-2",
        currentRevision: "ER-PS-3",
        diff: [
          { kind: "removed", text: "L42F 板内 ΔTm 为 2.8 C，聚集未解决，结论暂定。" },
          { kind: "added", text: "L42F ΔTm 为 3.1 C（95% 区间 2.4–3.8 C），SEC 单体保留率为 92%。" },
        ],
      },
    },
    {
      id: "skill_bundle",
      label: "轨迹到技能",
      shortLabel: "Skill",
      methodLabel: "Trajectory-to-skill",
      description: "把板内 DSF 分析与构建体匹配的 SEC 验证提炼成可重复流程。",
      steps: [
        { sessionId: "protein-session-01", evolutionRevision: "ER-PS-1", input: "Rejected ranking trajectory", change: "生成实验设计审计与板内归一化步骤。", status: "active" },
        { sessionId: "protein-session-02", evolutionRevision: "ER-PS-2", input: "Replicate-aware fit trajectory", change: "加入重复拟合、曲线形状与聚集标记。", status: "active" },
        { sessionId: "protein-session-03", evolutionRevision: "ER-PS-3", input: "DSF and SEC synthesis trajectory", change: "加入盲化映射、assay 一致性和条件化报告。", status: "active" },
      ],
      artifact: {
        title: "SKILL.md",
        content: `# Protein Stability Evidence Review

1. 审计构建体、重复、板次、缓冲液与 assay 身份。
2. 仅在匹配板次内归一化 DSF 对照并拟合重复感知的 ΔTm。
3. 检查聚集或非两态曲线；存在警告时保持暂定。
4. 只连接盲化且构建体匹配的 SEC 汇总。
5. 要求热转移与单体保留率方向一致。
6. 报告效应、区间、assay 一致性、测试条件和下一项证伪实验。`,
        previousRevision: "ER-PS-2",
        currentRevision: "ER-PS-3",
        diff: [
          { kind: "removed", text: "聚集未解决时要求后续正交 assay。" },
          { kind: "added", text: "连接盲化 SEC 汇总，并要求热转移与单体保留率方向一致。" },
        ],
      },
    },
    {
      id: "agent_system",
      label: "Agent 系统",
      shortLabel: "Agent system",
      methodLabel: "Agent-system evolution",
      description: "把禁止混杂排序、正交验证和适用范围纪律写入下一会话指令。",
      steps: [
        { sessionId: "protein-session-01", evolutionRevision: "ER-PS-1", input: "Unsupported conclusion", change: "禁止跨未匹配板次比较或把表观 Tm 当作稳定性结论。", status: "active" },
        { sessionId: "protein-session-02", evolutionRevision: "ER-PS-2", input: "Aggregation warning", change: "有聚集警告时必须标记暂定并要求正交验证。", status: "active" },
        { sessionId: "protein-session-03", evolutionRevision: "ER-PS-3", input: "Scoped validated result", change: "要求结论包含区间、assay 一致性与测试条件。", status: "active" },
      ],
      artifact: {
        title: "AGENTS.md",
        content: `# 蛋白质稳定性分析约束

- 不跨未匹配板次或缓冲液比较绝对荧光。
- 不把表观 Tm 单独解释为稳定性或构建体推荐。
- 稳定性结论必须同时有重复感知的 DSF 与构建体匹配的 SEC 证据。
- 最终结果必须包含效应区间、assay 一致性、测试条件和未验证范围。
- 下一会话使用 Project Head 3 中独立标识的 Evolution Revision ER-PS-3。`,
        previousRevision: "ER-PS-2",
        currentRevision: "ER-PS-3",
        diff: [
          { kind: "removed", text: "聚集未解决时不得推荐构建体。" },
          { kind: "added", text: "稳定性结论必须同时有重复感知的 DSF 与构建体匹配的 SEC 证据。" },
        ],
      },
    },
  ],
};

export const SAMPLE_SCIENTIFIC_PROJECTS: readonly SampleScientificProject[] = [
  SAMPLE_SCIENTIFIC_PROJECT,
  PROTEIN_STABILITY_SAMPLE_PROJECT,
];

export function sampleScientificProject(projectId: SampleScientificProjectId): SampleScientificProject {
  return SAMPLE_SCIENTIFIC_PROJECTS.find((project) => project.id === projectId)
    ?? SAMPLE_SCIENTIFIC_PROJECT;
}
