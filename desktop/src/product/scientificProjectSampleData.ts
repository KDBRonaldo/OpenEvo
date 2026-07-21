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
  readonly activeProjectHeadGeneration: number;
  readonly activeEvolutionRevision: string;
  readonly sessions: readonly SampleSession[];
  readonly evolutionTargets: readonly SampleEvolutionTarget[];
}

export const SAMPLE_SCIENTIFIC_PROJECT: SampleScientificProject = {
  id: SAMPLE_SCIENTIFIC_PROJECT_ID,
  name: "Enzyme Kinetics Model Review",
  badge: "Demo",
  summary:
    "Uses enzyme-rate observations to progress from a failed baseline fit to a validated substrate-inhibition model.",
  sourceLabel: "Demo data · 12 observations",
  executionLabel: "Codex on remote server",
  activeProjectHeadGeneration: 3,
  activeEvolutionRevision: "ER-3",
  sessions: [
    {
      id: "sample-session-01",
      sequence: 1,
      title: "Establish a Michaelis-Menten Baseline",
      objective:
        "Estimate Vmax and Km, assess fit quality, and verify concentration and rate units.",
      occurredAt: "2026-06-18 09:20",
      duration: "3 min 42 sec",
      outcome: "needs_revision",
      outcomeLabel: "Conclusion not accepted",
      finding:
        "The linearized fit systematically misses high-concentration observations, and concentration conversion was not verified before fitting; no reliable parameters are reported.",
      pinnedProjectHeadGeneration: 0,
      successorProjectHeadGeneration: 1,
      pinnedEvolutionRevision: "ER-0",
      successorEvolutionRevision: "ER-1",
      contextUsed: [],
      timeline: [
        {
          id: "s1-admitted",
          title: "Task pinned",
          detail: "Pinned the input table, task objective, Project Head 0, and Evolution Revision ER-0.",
          state: "completed",
        },
        {
          id: "s1-inspected",
          title: "Data inspection complete",
          detail: "Identified 12 observations and two unit labels.",
          state: "completed",
        },
        {
          id: "s1-fit",
          title: "Baseline fit complete",
          detail: "Residuals remain negative at high substrate concentrations.",
          state: "attention",
        },
        {
          id: "s1-closed",
          title: "Session closed",
          detail: "Preserved the failed-fit evidence and committed Project Head 1 with Evolution Revision ER-1.",
          state: "completed",
        },
      ],
      trace: [
        {
          id: "s1-r1",
          kind: "reasoning_summary",
          title: "Analysis summary",
          summary:
            "Check dimensions first, then compare residuals in raw and reciprocal space; the linearized result alone cannot support parameter estimates.",
        },
        {
          id: "s1-t1",
          kind: "tool_call",
          title: "Read data table",
          tool: "Dataset reader",
          summary: "Read column names, units, and 12 values.",
        },
        {
          id: "s1-t2",
          kind: "tool_call",
          title: "Fit baseline model",
          tool: "Scientific fit",
          summary: "Fit a two-parameter Michaelis-Menten model and calculated standardized residuals.",
        },
        {
          id: "s1-o1",
          kind: "tool_result",
          title: "Result summary",
          summary:
            "High-concentration residuals have the same direction, with 18.4% held-out error; the result is not suitable for a final conclusion.",
        },
      ],
    },
    {
      id: "sample-session-02",
      sequence: 2,
      title: "Refit with Memory and Skills",
      objective:
        "Standardize concentration units, use robust nonlinear fitting in raw space, and independently diagnose high-concentration residuals.",
      occurredAt: "2026-06-18 10:05",
      duration: "4 min 18 sec",
      outcome: "partial",
      outcomeLabel: "Fit accepted, model needs extension",
      finding:
        "Vmax and Km are stable across repeated fits, but the rate decline at high concentration is unexplained by the baseline model, indicating substrate inhibition should be tested.",
      pinnedProjectHeadGeneration: 1,
      successorProjectHeadGeneration: 2,
      pinnedEvolutionRevision: "ER-1",
      successorEvolutionRevision: "ER-2",
      contextUsed: ["text_memory", "skill_bundle", "agent_system"],
      timeline: [
        {
          id: "s2-admitted",
          title: "Project Head 1 pinned",
          detail: "Loaded unit-check memory, robust-fitting skills, and validation instructions from Evolution Revision ER-1.",
          state: "completed",
        },
        {
          id: "s2-normalized",
          title: "Units standardized",
          detail: "Standardized all concentrations to mM and retained conversion records.",
          state: "completed",
        },
        {
          id: "s2-fit",
          title: "Robust fit complete",
          detail: "Parameters are stable, but high-concentration residuals remain structured.",
          state: "attention",
        },
        {
          id: "s2-closed",
          title: "Session closed",
          detail: "Recorded the model gap and committed Project Head 2 with Evolution Revision ER-2.",
          state: "completed",
        },
      ],
      trace: [
        {
          id: "s2-r1",
          kind: "reasoning_summary",
          title: "Analysis summary",
          summary:
            "Follow the injected sequence: unit audit, robust fit, and stratified residual review before accepting the baseline model.",
        },
        {
          id: "s2-t1",
          kind: "tool_call",
          title: "Normalize observations",
          tool: "Table transform",
          summary: "Standardized concentrations to mM and recorded column-level transformations and row count.",
        },
        {
          id: "s2-t2",
          kind: "tool_call",
          title: "Robust nonlinear fit",
          tool: "Scientific fit",
          summary: "Used multi-start fitting and checked parameter bounds, convergence consistency, and stratified residuals.",
        },
        {
          id: "s2-o1",
          kind: "tool_result",
          title: "Result summary",
          summary:
            "Repeated-fit parameter variation is below 2%, but the baseline model overestimates all three highest-concentration observations.",
        },
      ],
    },
    {
      id: "sample-session-03",
      sequence: 3,
      title: "Validate the Substrate-Inhibition Model",
      objective:
        "Compare the baseline and substrate-inhibition models using pre-held observations and report uncertainty.",
      occurredAt: "2026-06-18 11:10",
      duration: "5 min 06 sec",
      outcome: "validated",
      outcomeLabel: "Validated",
      finding:
        "The substrate-inhibition model substantially reduces held-out error, and residuals no longer show concentration-related structure; parameter intervals and diagnostics are reported.",
      pinnedProjectHeadGeneration: 2,
      successorProjectHeadGeneration: 3,
      pinnedEvolutionRevision: "ER-2",
      successorEvolutionRevision: "ER-3",
      contextUsed: ["text_memory", "skill_bundle", "agent_system"],
      timeline: [
        {
          id: "s3-admitted",
          title: "Project Head 2 pinned",
          detail: "Loaded the model-comparison checklist and held-out validation constraints from Evolution Revision ER-2.",
          state: "completed",
        },
        {
          id: "s3-compared",
          title: "Candidate models compared",
          detail: "Compared both candidate models on the same training observations.",
          state: "completed",
        },
        {
          id: "s3-heldout",
          title: "Held-out validation passed",
          detail: "Held-out error decreased from 16.9% to 4.7%.",
          state: "completed",
        },
        {
          id: "s3-closed",
          title: "Project Head advanced",
          detail: "After artifact validation, committed Project Head 3 with Evolution Revision ER-3.",
          state: "completed",
        },
      ],
      trace: [
        {
          id: "s3-r1",
          kind: "reasoning_summary",
          title: "Analysis summary",
          summary:
            "Do not select a model from training error alone; fix the held-out observations first, then compare prediction error, residual structure, and parameter identifiability.",
        },
        {
          id: "s3-t1",
          kind: "tool_call",
          title: "Compare candidate models",
          tool: "Model comparison",
          summary: "Fit the baseline and substrate-inhibition models on the same training split.",
        },
        {
          id: "s3-t2",
          kind: "tool_call",
          title: "Run held-out validation",
          tool: "Validation",
          summary: "Calculated relative error and residual trends for pre-held observations.",
        },
        {
          id: "s3-o1",
          kind: "tool_result",
          title: "Result summary",
          summary:
            "The substrate-inhibition model has 4.7% held-out error with no monotonic residual trend; the conclusion meets the project validation criteria.",
        },
      ],
    },
  ],
  evolutionTargets: [
    {
      id: "text_memory",
      label: "Text Memory",
      shortLabel: "Memory",
      methodLabel: "Textual memory",
      description: "Carries useful experimental facts and lessons from failed fits into the next session.",
      steps: [
        {
          sessionId: "sample-session-01",
          evolutionRevision: "ER-1",
          input: "Session 1 transcript",
          change: "Record that units must be standardized before fitting and conversion records retained.",
          status: "active",
        },
        {
          sessionId: "sample-session-02",
          evolutionRevision: "ER-2",
          input: "Session 2 transcript",
          change: "Add the project fact that consistently directed high-concentration residuals warrant a substrate-inhibition check.",
          status: "active",
        },
        {
          sessionId: "sample-session-03",
          evolutionRevision: "ER-3",
          input: "Session 3 transcript",
          change: "Record the validated model, error, and applicability for later tasks.",
          status: "active",
        },
      ],
      artifact: {
        title: "memory.md",
        content: `# Project Memory

## Data Conventions
- Use mM for substrate concentration; retain original units and conversions in the result summary.
- Rate is measured in µmol/min; check zero values, duplicates, and dimensions before fitting.

## Validated Facts
- The baseline Michaelis-Menten model produces consistently directed residuals at high concentration.
- The substrate-inhibition model has 4.7% held-out relative error and can explain this dataset.

## Next Session
- Recalculate parameter intervals after adding observations; do not reuse prior confidence intervals.`,
        previousRevision: "ER-2",
        currentRevision: "ER-3",
        diff: [
          { kind: "removed", text: "High-concentration residuals suggest substrate inhibition; held-out validation is still required." },
          { kind: "added", text: "The substrate-inhibition model has 4.7% held-out relative error, and residuals no longer follow a concentration trend." },
        ],
      },
    },
    {
      id: "skill_bundle",
      label: "Trajectory to Skill",
      shortLabel: "Skill",
      methodLabel: "Trajectory-to-skill",
      description: "Extracts repeatable scientific-analysis steps from session trajectories.",
      steps: [
        {
          sessionId: "sample-session-01",
          evolutionRevision: "ER-1",
          input: "Failed fit trajectory",
          change: "Create a minimal workflow: audit units before fitting.",
          status: "active",
        },
        {
          sessionId: "sample-session-02",
          evolutionRevision: "ER-2",
          input: "Robust fit trajectory",
          change: "Add multi-start fitting, parameter bounds, and stratified residual diagnostics.",
          status: "active",
        },
        {
          sessionId: "sample-session-03",
          evolutionRevision: "ER-3",
          input: "Validated comparison trajectory",
          change: "Add a fixed held-out set, candidate-model comparison, and uncertainty reporting.",
          status: "active",
        },
      ],
      artifact: {
        title: "SKILL.md",
        content: `# Enzyme Kinetics Review

1. Read the column schema and confirm concentration and rate units.
2. Fit in raw response space using at least three reproducible starting points.
3. Check parameter bounds, convergence consistency, and concentration-ordered residuals.
4. Compare substrate-inhibition candidates when high-concentration residuals remain consistently directed.
5. Select models using observations held out before the task begins.
6. Report parameter intervals, held-out error, applicability, and unresolved limitations.`,
        previousRevision: "ER-2",
        currentRevision: "ER-3",
        diff: [
          { kind: "removed", text: "Record candidate mechanisms when structured residuals appear." },
          { kind: "added", text: "Compare candidate models using observations fixed before the task begins and report uncertainty." },
        ],
      },
    },
    {
      id: "agent_system",
      label: "Agent System",
      shortLabel: "Agent system",
      methodLabel: "Agent-system evolution",
      description: "Carries project validation requirements into the next Codex session.",
      steps: [
        {
          sessionId: "sample-session-01",
          evolutionRevision: "ER-1",
          input: "Rejected conclusion",
          change: "Require withholding final parameters when dimension or residual checks fail.",
          status: "active",
        },
        {
          sessionId: "sample-session-02",
          evolutionRevision: "ER-2",
          input: "Structured residual evidence",
          change: "Require explicit comparison of mechanism candidates when the baseline model has structured residuals.",
          status: "active",
        },
        {
          sessionId: "sample-session-03",
          evolutionRevision: "ER-3",
          input: "Held-out validation",
          change: "Require final conclusions to include held-out metrics and uncertainty.",
          status: "active",
        },
      ],
      artifact: {
        title: "AGENTS.md",
        content: `# Project Analysis Requirements

- Confirm units, missing values, and observation range before every fit.
- Do not accept kinetic parameters from a linearized plot or training error alone.
- When residuals are structured, propose falsifiable mechanism candidates first.
- Compare models using observations fixed in advance for holdout.
- Final responses must include parameter intervals, validation metrics, and applicability.
- When any check fails, clearly mark the conclusion as not accepted.`,
        previousRevision: "ER-2",
        currentRevision: "ER-3",
        diff: [
          { kind: "removed", text: "Explicitly compare mechanism candidates when the baseline model has structured residuals." },
          { kind: "added", text: "Final conclusions must include fixed held-out metrics, uncertainty, and applicability." },
        ],
      },
    },
  ],
};

export const PROTEIN_STABILITY_SAMPLE_PROJECT: SampleScientificProject = {
  id: PROTEIN_STABILITY_SAMPLE_PROJECT_ID,
  name: "Protein Stability Evidence Review",
  badge: "Demo",
  summary:
    "Uses DSF and SEC observations to progress from a confounded ranking to a scoped L42F stability conclusion.",
  sourceLabel: "Demo data · 48 DSF curves + 12 SEC summaries",
  executionLabel: "Codex on remote server",
  activeProjectHeadGeneration: 3,
  activeEvolutionRevision: "ER-PS-3",
  sessions: [
    {
      id: "protein-session-01",
      sequence: 1,
      title: "Review the Combined Thermal-Shift Ranking",
      objective: "Compare apparent Tm for wild type and L42F, and determine whether plate, buffer, and replicate structure supports direct ranking.",
      occurredAt: "2026-06-22 09:10",
      duration: "4 min 12 sec",
      outcome: "needs_revision",
      outcomeLabel: "Conclusion not accepted",
      finding: "Construct, plate, and pH are collinear, and absolute fluorescence is not calibrated across plates; the combined thermal-shift ranking cannot support a stability conclusion.",
      pinnedProjectHeadGeneration: 0,
      successorProjectHeadGeneration: 1,
      pinnedEvolutionRevision: "ER-PS-0",
      successorEvolutionRevision: "ER-PS-1",
      contextUsed: [],
      timeline: [
        { id: "ps1-admitted", title: "Task pinned", detail: "Pinned Project Head 0, Evolution Revision ER-PS-0, and the demo input summary.", state: "completed" },
        { id: "ps1-audit", title: "Experimental-design audit", detail: "Found that construct, plate, and buffer conditions cannot be separated in the combined table.", state: "attention" },
        { id: "ps1-curves", title: "Curve quality review", detail: "12 curves show signs of aggregation, and fluorescence scales differ across plates.", state: "attention" },
        { id: "ps1-closed", title: "Failed evidence closed", detail: "Rejected construct ranking and committed Project Head 1 with ER-PS-1.", state: "completed" },
      ],
      trace: [
        { id: "ps1-r1", kind: "reasoning_summary", title: "Analysis summary", summary: "First verify that the design can separate construct effects, then review curve shape; apparent Tm is not automatically stability." },
        { id: "ps1-t1", kind: "tool_call", title: "Read experimental design", tool: "Plate metadata review", summary: "Summarized construct, plate, pH, replicate, and control coverage." },
        { id: "ps1-t2", kind: "tool_call", title: "Review DSF curves", tool: "Curve quality review", summary: "Reviewed baselines, transition regions, and signs of aggregation in 48 DSF curves." },
        { id: "ps1-o1", kind: "tool_result", title: "Result summary", summary: "Confounding and scale differences invalidate the original ranking; no construct is currently recommended." },
      ],
    },
    {
      id: "protein-session-02",
      sequence: 2,
      title: "Normalize Within Plates and Fit Replicates",
      objective: "Normalize within matched pH 7.4 plates, estimate a replicate-aware thermal-shift effect, and determine whether aggregation prevents interpretation.",
      occurredAt: "2026-06-23 10:05",
      duration: "5 min 31 sec",
      outcome: "partial",
      outcomeLabel: "Preliminary result, orthogonal validation needed",
      finding: "L42F has a within-plate ΔTm of 2.8 C, but aggregation appears near the transition; the result is preliminary and cannot support a construct recommendation.",
      pinnedProjectHeadGeneration: 1,
      successorProjectHeadGeneration: 2,
      pinnedEvolutionRevision: "ER-PS-1",
      successorEvolutionRevision: "ER-PS-2",
      contextUsed: ["text_memory", "skill_bundle", "agent_system"],
      timeline: [
        { id: "ps2-admitted", title: "Project Head 1 pinned", detail: "Loaded confounding facts, within-plate analysis skills, and scoped-reporting instructions from ER-PS-1.", state: "completed" },
        { id: "ps2-normalized", title: "Within-plate normalization complete", detail: "Compared constructs and controls only within matched pH 7.4 plates.", state: "completed" },
        { id: "ps2-fit", title: "Replicate fit complete", detail: "The L42F ΔTm estimate is stable, but aggregation flags remain.", state: "attention" },
        { id: "ps2-closed", title: "Preliminary evidence closed", detail: "Required SEC monomer-retention validation and committed Project Head 2 with ER-PS-2.", state: "completed" },
      ],
      trace: [
        { id: "ps2-r1", kind: "reasoning_summary", title: "Analysis summary", summary: "Follow ER-PS-1: normalize within plates, fit with replicate awareness, and review curve shape; any aggregation warning prevents recommendation." },
        { id: "ps2-t1", kind: "tool_call", title: "Normalize within-plate controls", tool: "Plate normalization", summary: "Matched controls by plate and buffer while retaining replicate identity." },
        { id: "ps2-t2", kind: "tool_call", title: "Fit thermal-shift effect", tool: "Replicate-aware fit", summary: "Estimated ΔTm and bootstrap intervals, with curve-quality flags." },
        { id: "ps2-o1", kind: "tool_result", title: "Result summary", summary: "ΔTm is 2.8 C, but signs of aggregation keep the conclusion preliminary; the next session requires an orthogonal assay." },
      ],
    },
    {
      id: "protein-session-03",
      sequence: 3,
      title: "Validate Stability with Orthogonal SEC Evidence",
      objective: "After blinded construct matching, integrate DSF with SEC monomer retention and apply the prespecified rule for L42F.",
      occurredAt: "2026-06-24 11:20",
      duration: "6 min 08 sec",
      outcome: "validated",
      outcomeLabel: "Scoped conclusion validated",
      finding: "Under the tested pH 7.4 condition, L42F has ΔTm of 3.1 C (95% interval 2.4-3.8 C); SEC monomer retention is 92%, versus 87% for wild type. Other buffers and long-term storage remain unvalidated.",
      pinnedProjectHeadGeneration: 2,
      successorProjectHeadGeneration: 3,
      pinnedEvolutionRevision: "ER-PS-2",
      successorEvolutionRevision: "ER-PS-3",
      contextUsed: ["text_memory", "skill_bundle", "agent_system"],
      timeline: [
        { id: "ps3-admitted", title: "Project Head 2 pinned", detail: "Loaded the preliminary effect, orthogonal-validation skills, and reporting scope from ER-PS-2.", state: "completed" },
        { id: "ps3-joined", title: "Blinded assays matched", detail: "Joined DSF replicates and SEC summaries by construct and batch.", state: "completed" },
        { id: "ps3-validated", title: "Prespecified rule passed", detail: "Thermal shift and monomer retention agree in direction, with complete intervals and conditions.", state: "completed" },
        { id: "ps3-closed", title: "Next Project Head ready", detail: "Committed Project Head 3 with ER-PS-3; the next session uses this revision.", state: "completed" },
      ],
      trace: [
        { id: "ps3-r1", kind: "reasoning_summary", title: "Analysis summary", summary: "Do not redefine the acceptance criteria; integrate both assays through the blinded mapping and scope the conclusion to tested pH 7.4 conditions." },
        { id: "ps3-t1", kind: "tool_call", title: "Join orthogonal evidence", tool: "Evidence join", summary: "Joined construct-matched DSF and SEC summaries while retaining batch and replicate identity." },
        { id: "ps3-t2", kind: "tool_call", title: "Check acceptance rule", tool: "Validation checklist", summary: "Checked effect intervals, assay agreement, monomer retention, and applicability." },
        { id: "ps3-o1", kind: "tool_result", title: "Result summary", summary: "L42F is supported at pH 7.4; no conclusion is made for other buffers or long-term storage." },
      ],
    },
  ],
  evolutionTargets: [
    {
      id: "text_memory",
      label: "Text Memory",
      shortLabel: "Memory",
      methodLabel: "Textual memory",
      description: "Preserves experimental-design limits, failed conclusions, preliminary effects, and final scope.",
      steps: [
        { sessionId: "protein-session-01", evolutionRevision: "ER-PS-1", input: "Confounded baseline transcript", change: "Record the plate, pH, and construct collinearity; do not reuse the combined ranking.", status: "active" },
        { sessionId: "protein-session-02", evolutionRevision: "ER-PS-2", input: "Plate-aware fit transcript", change: "Preserve the preliminary 2.8 C effect and aggregation warning, and require SEC validation.", status: "active" },
        { sessionId: "protein-session-03", evolutionRevision: "ER-PS-3", input: "Orthogonal validation transcript", change: "Record the 3.1 C interval, monomer retention, and pH 7.4 scope.", status: "active" },
      ],
      artifact: {
        title: "memory.md",
        content: `# Protein Stability Project Memory

## Validated Facts
- At pH 7.4, L42F has ΔTm of 3.1 C (95% interval 2.4-3.8 C).
- SEC summaries show 92% monomer retention for L42F and 87% for wild type.

## Limits
- Do not combine absolute fluorescence across unmatched plates.
- Other buffers and long-term storage stability remain unvalidated.

## Next Session
- Use Project Head 3 and Evolution Revision ER-PS-3; revalidate intervals and monomer retention after adding batches.`,
        previousRevision: "ER-PS-2",
        currentRevision: "ER-PS-3",
        diff: [
          { kind: "removed", text: "L42F has a within-plate ΔTm of 2.8 C; aggregation is unresolved, so the conclusion is preliminary." },
          { kind: "added", text: "L42F has ΔTm of 3.1 C (95% interval 2.4-3.8 C) and 92% SEC monomer retention." },
        ],
      },
    },
    {
      id: "skill_bundle",
      label: "Trajectory to Skill",
      shortLabel: "Skill",
      methodLabel: "Trajectory-to-skill",
      description: "Extracts a repeatable workflow for within-plate DSF analysis and construct-matched SEC validation.",
      steps: [
        { sessionId: "protein-session-01", evolutionRevision: "ER-PS-1", input: "Rejected ranking trajectory", change: "Create experimental-design audit and within-plate normalization steps.", status: "active" },
        { sessionId: "protein-session-02", evolutionRevision: "ER-PS-2", input: "Replicate-aware fit trajectory", change: "Add replicate fitting, curve-shape review, and aggregation flags.", status: "active" },
        { sessionId: "protein-session-03", evolutionRevision: "ER-PS-3", input: "DSF and SEC synthesis trajectory", change: "Add blinded mapping, assay agreement, and scoped reporting.", status: "active" },
      ],
      artifact: {
        title: "SKILL.md",
        content: `# Protein Stability Evidence Review

1. Audit construct, replicate, plate, buffer, and assay identity.
2. Normalize DSF controls only within matched plates and fit replicate-aware ΔTm.
3. Check for aggregation or non-two-state curves; keep results preliminary when warnings appear.
4. Join only blinded, construct-matched SEC summaries.
5. Require thermal shift and monomer retention to agree in direction.
6. Report the effect, interval, assay agreement, tested conditions, and next falsification experiment.`,
        previousRevision: "ER-PS-2",
        currentRevision: "ER-PS-3",
        diff: [
          { kind: "removed", text: "Require a subsequent orthogonal assay when aggregation is unresolved." },
          { kind: "added", text: "Join blinded SEC summaries and require thermal shift and monomer retention to agree in direction." },
        ],
      },
    },
    {
      id: "agent_system",
      label: "Agent System",
      shortLabel: "Agent system",
      methodLabel: "Agent-system evolution",
      description: "Carries requirements for unconfounded ranking, orthogonal validation, and reporting scope into the next session.",
      steps: [
        { sessionId: "protein-session-01", evolutionRevision: "ER-PS-1", input: "Unsupported conclusion", change: "Do not compare unmatched plates or treat apparent Tm as a stability conclusion.", status: "active" },
        { sessionId: "protein-session-02", evolutionRevision: "ER-PS-2", input: "Aggregation warning", change: "Mark results preliminary and require orthogonal validation when aggregation is flagged.", status: "active" },
        { sessionId: "protein-session-03", evolutionRevision: "ER-PS-3", input: "Scoped validated result", change: "Require conclusions to include intervals, assay agreement, and tested conditions.", status: "active" },
      ],
      artifact: {
        title: "AGENTS.md",
        content: `# Protein Stability Analysis Requirements

- Do not compare absolute fluorescence across unmatched plates or buffers.
- Do not interpret apparent Tm alone as stability or a construct recommendation.
- Stability conclusions require both replicate-aware DSF and construct-matched SEC evidence.
- Final results must include effect intervals, assay agreement, tested conditions, and unvalidated scope.
- The next session uses the separately identified Evolution Revision ER-PS-3 in Project Head 3.`,
        previousRevision: "ER-PS-2",
        currentRevision: "ER-PS-3",
        diff: [
          { kind: "removed", text: "Do not recommend a construct when aggregation is unresolved." },
          { kind: "added", text: "Stability conclusions require both replicate-aware DSF and construct-matched SEC evidence." },
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
