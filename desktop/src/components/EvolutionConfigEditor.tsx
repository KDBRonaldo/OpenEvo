import { ChevronUp, RotateCcw, Settings2 } from "lucide-react";
import { useState } from "react";
import type { OpenEvoDesktopCapabilities } from "../api/openevo";
import { validateEvolutionConfigOverride } from "../api/evolutionConfigSchema";
import type {
  OpenEvoEvolutionTargetSelection,
  OpenEvoJsonObject,
} from "../routes/openevoDesktopModel";
import {
  buildEvolutionConfigModel,
  resetEvolutionChoiceConfig,
  selectEvolutionChoice,
  setEvolutionTargetEnabled,
  type EvolutionSelections,
  type EvolutionTargetRow,
} from "../routes/evolutionConfigModel";
import { SchemaConfigEditor } from "./SchemaConfigEditor";

export interface EvolutionConfigIssue {
  targetId: string;
  path: string;
  message: string;
}

export interface EvolutionConfigEditorProps {
  capabilities: OpenEvoDesktopCapabilities | null;
  selections: EvolutionSelections;
  onChange: (selections: EvolutionSelections) => void;
}

export function validateEvolutionDraftConfigs(
  capabilities: OpenEvoDesktopCapabilities | null,
  selections: EvolutionSelections,
): EvolutionConfigIssue[] {
  if (!capabilities) {
    return [];
  }
  const issues: EvolutionConfigIssue[] = [];
  for (const target of capabilities.targets) {
    const selection = selections[target.targetId];
    if (!selection?.enabled) {
      continue;
    }
    const method = target.methods.find(
      (candidate) => candidate.methodId === selection?.method,
    );
    if (!method) {
      continue;
    }
    for (const error of validateEvolutionConfigOverride(
      method.configSchema,
      method.defaultConfig,
      selection.config,
    ).errors) {
      issues.push({
        targetId: target.targetId,
        path: error.path,
        message: error.message,
      });
    }
  }
  return issues;
}

export function EvolutionConfigEditor({
  capabilities,
  selections,
  onChange,
}: EvolutionConfigEditorProps) {
  const [expandedTargets, setExpandedTargets] = useState<Set<string>>(
    () => new Set(),
  );

  if (!capabilities) {
    const enabledSelections = Object.entries(selections).filter(
      ([, selection]) => selection.enabled,
    );
    return (
      <div className="divide-y divide-slate-200 border-y border-slate-200 lg:col-span-4">
        {enabledSelections.map(([targetId]) => (
          <div
            key={targetId}
            data-testid="evolution-target"
            className="flex min-h-12 items-center gap-3 py-2"
          >
            <input
              aria-label={displayIdentifier(targetId)}
              type="checkbox"
              checked
              disabled
            />
            <span className="text-sm font-medium text-slate-700">
              {displayIdentifier(targetId)}
            </span>
            <span className="ml-auto text-xs text-slate-500">Pending</span>
          </div>
        ))}
      </div>
    );
  }

  const model = buildEvolutionConfigModel(capabilities, selections);
  return (
    <div className="divide-y divide-slate-200 border-y border-slate-200 lg:col-span-4">
      {model.targetRows.map((row) => {
        const expanded = expandedTargets.has(row.targetId);
        const selectedMethod = selectedVisibleMethod(capabilities, row);
        const currentChoice = row.choices.find((choice) => choice.current);
        const resettable =
          currentChoice?.kind === "method" || currentChoice?.kind === "resolver";
        const configurable = Boolean(
          selectedMethod &&
            Object.keys(selectedMethod.configSchema.properties).length > 0,
        );
        const configValidation =
          selectedMethod && row.selection && row.enabled
            ? validateEvolutionConfigOverride(
                selectedMethod.configSchema,
                selectedMethod.defaultConfig,
                row.selection.config,
              )
            : null;
        const unavailableChoices = row.choices.filter(
          (choice) =>
            !choice.current &&
            (choice.kind === "method" || choice.kind === "resolver") &&
            choice.state !== "supported",
        );
        const configPanelId = `evolution-config-${encodeURIComponent(row.targetId)}`;
        return (
          <div key={row.targetId} className="py-3" data-target-id={row.targetId}>
            <div className="grid items-center gap-3 md:grid-cols-[minmax(12rem,1fr)_minmax(14rem,1.2fr)_auto]">
              <label
                data-testid="evolution-target"
                className="flex min-w-0 items-start gap-3"
              >
                <input
                  aria-label={row.displayName}
                  className="mt-1"
                  type="checkbox"
                  checked={row.enabled}
                  disabled={!row.enabled && !row.canEnable}
                  onChange={(event) =>
                    onChange(
                      setEvolutionTargetEnabled(
                        selections,
                        row,
                        event.currentTarget.checked,
                      ),
                    )
                  }
                />
                <span className="min-w-0">
                  <span className="block text-sm font-medium text-slate-900">
                    {row.displayName}
                  </span>
                  <span className="block text-xs leading-5 text-slate-500">
                    {row.description}
                  </span>
                </span>
              </label>

              <label className="min-w-0">
                <span className="sr-only">{row.displayName} method</span>
                <select
                  aria-label={`${row.displayName} method`}
                  title={
                    row.choices.find(
                      (choice) => choice.id === row.selection?.method,
                    )?.description
                  }
                  className="h-9 w-full rounded-md border border-slate-200 bg-white px-2 text-sm text-slate-900"
                  value={row.selection?.method ?? ""}
                  onChange={(event) => {
                    const next = selectEvolutionChoice(
                      selections,
                      row,
                      event.currentTarget.value,
                    );
                    onChange(next);
                    setExpandedTargets((current) =>
                      new Set(current).add(row.targetId),
                    );
                  }}
                >
                  {row.choices.map((choice, index) => (
                    <option
                      key={`${choice.kind}-${choice.id ?? "none"}-${index}`}
                      value={choice.id ?? ""}
                      disabled={!choice.selectable}
                    >
                      {choice.label}
                      {choice.state === "supported" ? "" : ` (${choice.state})`}
                    </option>
                  ))}
                </select>
              </label>

              <div className="flex min-w-28 items-center justify-end gap-2">
                <SupportLabel state={row.selectionState} enabled={row.enabled} />
                {resettable ? (
                  <button
                    type="button"
                    aria-label={`Reset ${row.displayName} configuration`}
                    title={`Reset ${row.displayName} configuration`}
                    className="inline-flex h-9 w-9 items-center justify-center rounded-md border border-slate-200 bg-white text-slate-600 hover:bg-slate-50"
                    onClick={() =>
                      onChange(resetEvolutionChoiceConfig(selections, row))
                    }
                  >
                    <RotateCcw size={16} aria-hidden="true" />
                  </button>
                ) : null}
                {configurable ? (
                  <button
                    type="button"
                    aria-label={`Configure ${row.displayName}`}
                    aria-controls={configPanelId}
                    aria-expanded={expanded}
                    title={`Configure ${row.displayName}`}
                    className="inline-flex h-9 w-9 items-center justify-center rounded-md border border-slate-200 bg-white text-slate-600 hover:bg-slate-50 disabled:opacity-50"
                    onClick={() =>
                      setExpandedTargets((current) => {
                        const next = new Set(current);
                        if (next.has(row.targetId)) {
                          next.delete(row.targetId);
                        } else {
                          next.add(row.targetId);
                        }
                        return next;
                      })
                    }
                  >
                    {expanded ? (
                      <ChevronUp size={16} aria-hidden="true" />
                    ) : (
                      <Settings2 size={16} aria-hidden="true" />
                    )}
                  </button>
                ) : null}
              </div>
            </div>

            {row.reason ? (
              <div className="mt-2 text-xs text-rose-700">{row.reason}</div>
            ) : null}
            {configValidation && !configValidation.valid ? (
              <div className="mt-2 text-xs text-rose-700">
                Configuration: {configValidation.errors[0]?.message}
              </div>
            ) : null}
            {unavailableChoices.length > 0 ? (
              <div
                aria-label={`${row.displayName} unavailable methods`}
                className="mt-2 space-y-1 text-xs text-amber-800"
              >
                {unavailableChoices.map((choice) => (
                  <div key={`${choice.kind}-${choice.id}`}>
                    <span className="font-medium">{choice.label}</span>
                    {choice.description ? `: ${choice.description}` : ""}
                    {choice.reason ? ` ${choice.reason}` : ""}
                  </div>
                ))}
              </div>
            ) : null}

            {expanded && selectedMethod && row.selection ? (
              <div
                id={configPanelId}
                className="mt-3 border-l-2 border-slate-200 pl-4"
              >
                <SchemaConfigEditor
                  schema={selectedMethod.configSchema}
                  defaultValue={selectedMethod.defaultConfig}
                  value={row.selection.config}
                  onChange={(config) =>
                    onChange(
                      replaceTargetConfig(
                        selections,
                        row.targetId,
                        config,
                      ),
                    )
                  }
                />
              </div>
            ) : null}
          </div>
        );
      })}

      {model.unknownTargetRows.some((row) => row.enabled) ? (
        <div className="py-3">
          <div className="mb-2 text-xs font-medium uppercase text-slate-500">
            Unavailable saved settings
          </div>
          <div className="space-y-2">
            {model.unknownTargetRows.filter((row) => row.enabled).map((row) => (
              <label
                key={row.targetId}
                data-testid="evolution-target"
                className="flex min-h-9 items-center gap-3 text-sm text-slate-700"
              >
                <input
                  aria-label={row.displayName}
                  type="checkbox"
                  checked={row.enabled}
                  disabled={!row.enabled}
                  onChange={(event) =>
                    onChange(
                      setEvolutionTargetEnabled(
                        selections,
                        row,
                        event.currentTarget.checked,
                      ),
                    )
                  }
                />
                <span>{row.displayName}</span>
                <span className="text-xs text-rose-700">{row.reason}</span>
              </label>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function selectedVisibleMethod(
  capabilities: OpenEvoDesktopCapabilities,
  row: EvolutionTargetRow,
) {
  const target = capabilities.targets.find(
    (candidate) => candidate.targetId === row.targetId,
  );
  return target?.methods.find(
    (method) => method.methodId === row.selection?.method,
  );
}

function replaceTargetConfig(
  selections: EvolutionSelections,
  targetId: string,
  config: OpenEvoJsonObject,
): EvolutionSelections {
  const selection = selections[targetId];
  if (!selection) {
    return selections;
  }
  return {
    ...selections,
    [targetId]: {
      ...selection,
      config,
    } satisfies OpenEvoEvolutionTargetSelection,
  };
}

function SupportLabel({
  state,
  enabled,
}: {
  state: "supported" | "unsupported" | "unavailable";
  enabled: boolean;
}) {
  const tone = {
    supported: "bg-emerald-50 text-emerald-800",
    unsupported: "bg-amber-50 text-amber-800",
    unavailable: "bg-rose-50 text-rose-800",
  }[state];
  const label = state === "supported" ? (enabled ? "Active" : "Ready") : state;
  return (
    <span className={`rounded-full px-2 py-1 text-xs ${tone}`}>{label}</span>
  );
}

function displayIdentifier(value: string): string {
  const words = value.replaceAll("_", " ").replaceAll("-", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}
