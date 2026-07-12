import type {
  OpenEvoCapabilityMethodSupport,
  OpenEvoCapabilitySupportState,
  OpenEvoDesktopCapabilities,
  OpenEvoEvolutionTargetCapability,
} from "../api/openevo";
import type {
  OpenEvoEvolutionTargetSelection,
  OpenEvoJsonObject,
} from "./openevoDesktopModel";

export type EvolutionSelections = Record<
  string,
  OpenEvoEvolutionTargetSelection
>;

export type EvolutionChoiceKind =
  | "method"
  | "resolver"
  | "hidden-current"
  | "stale-current"
  | "null-current";

export interface EvolutionConfigChoice {
  id: string | null;
  label: string;
  description: string;
  kind: EvolutionChoiceKind;
  state: OpenEvoCapabilitySupportState;
  reason: string | null;
  current: boolean;
  selectable: boolean;
  defaultConfig: OpenEvoJsonObject | null;
}

export interface EvolutionTargetRow {
  targetId: string;
  displayName: string;
  description: string;
  artifactType: string;
  contextOrder: number;
  enabled: boolean;
  selection: OpenEvoEvolutionTargetSelection | null;
  selectionState: OpenEvoCapabilitySupportState;
  reason: string | null;
  choices: EvolutionConfigChoice[];
  effectiveDefaultChoice: EvolutionConfigChoice | null;
  canEnable: boolean;
}

export interface UnknownEvolutionTargetRow {
  targetId: string;
  displayName: string;
  enabled: boolean;
  selection: OpenEvoEvolutionTargetSelection;
  state: "unavailable";
  reason: string;
}

export interface EvolutionConfigModel {
  targetRows: EvolutionTargetRow[];
  unknownTargetRows: UnknownEvolutionTargetRow[];
}

export function buildEvolutionConfigModel(
  capabilities: OpenEvoDesktopCapabilities,
  selections: EvolutionSelections,
): EvolutionConfigModel {
  const indexedTargets = capabilities.targets.map((target, index) => ({
    target,
    index,
  }));
  indexedTargets.sort(
    (left, right) =>
      left.target.contextOrder - right.target.contextOrder ||
      left.index - right.index,
  );

  const targetRows = indexedTargets.map(({ target }) =>
    buildTargetRow(target, selections[target.targetId] ?? null),
  );
  const knownTargetIds = new Set(
    capabilities.targets.map((target) => target.targetId),
  );
  const unknownTargetRows = Object.entries(selections)
    .filter(([targetId]) => !knownTargetIds.has(targetId))
    .map(([targetId, selection]) => ({
      targetId,
      displayName: displayIdentifier(targetId),
      enabled: selection.enabled,
      selection,
      state: "unavailable" as const,
      reason: `Target "${targetId}" is no longer available in the remote registry. Disable it to repair an enabled configuration.`,
    }));

  return { targetRows, unknownTargetRows };
}

export function setEvolutionTargetEnabled(
  selections: EvolutionSelections,
  row: EvolutionTargetRow | UnknownEvolutionTargetRow,
  enabled: boolean,
): EvolutionSelections {
  const current = selections[row.targetId];
  if (!enabled) {
    if (!current || !current.enabled) {
      return selections;
    }
    return {
      ...selections,
      [row.targetId]: { ...current, enabled: false },
    };
  }

  if (!("contextOrder" in row)) {
    throw new Error(
      `Target "${row.targetId}" cannot be enabled because it is unavailable.`,
    );
  }

  if (current && row.selectionState === "supported") {
    if (current.enabled) {
      return selections;
    }
    return {
      ...selections,
      [row.targetId]: { ...current, enabled: true },
    };
  }

  const effectiveDefault = row.effectiveDefaultChoice;
  if (
    !effectiveDefault ||
    effectiveDefault.id === null ||
    effectiveDefault.defaultConfig === null
  ) {
    throw new Error(
      `Target "${row.targetId}" cannot be enabled without a supported selection.`,
    );
  }
  return {
    ...selections,
    [row.targetId]: {
      enabled: true,
      method: effectiveDefault.id,
      config: copyJsonObject(effectiveDefault.defaultConfig),
    },
  };
}

export function selectEvolutionChoice(
  selections: EvolutionSelections,
  row: EvolutionTargetRow,
  choiceId: string,
): EvolutionSelections {
  const choice = row.choices.find(
    (candidate) => candidate.id === choiceId && candidate.selectable,
  );
  if (!choice || (choice.kind !== "method" && choice.kind !== "resolver")) {
    throw new Error(
      `Evolution choice "${choiceId}" is not selectable for target "${row.targetId}".`,
    );
  }

  const current = selections[row.targetId];
  if (current?.method === choiceId) {
    return selections;
  }
  return {
    ...selections,
    [row.targetId]: {
      enabled: current?.enabled ?? false,
      method: choiceId,
      config:
        choice.kind === "resolver"
          ? {}
          : copyJsonObject(choice.defaultConfig ?? {}),
    },
  };
}

export function resetEvolutionChoiceConfig(
  selections: EvolutionSelections,
  row: EvolutionTargetRow,
): EvolutionSelections {
  const current = selections[row.targetId];
  const choice = row.choices.find(
    (candidate) => candidate.current && candidate.id === current?.method,
  );
  if (
    !current ||
    !choice ||
    (choice.kind !== "method" && choice.kind !== "resolver")
  ) {
    throw new Error(
      `Evolution config for target "${row.targetId}" cannot be reset because its current selection is opaque.`,
    );
  }

  return {
    ...selections,
    [row.targetId]: {
      ...current,
      config:
        choice.kind === "resolver"
          ? {}
          : copyJsonObject(choice.defaultConfig ?? {}),
    },
  };
}

export function evolutionRunBlockReason(
  loading: boolean,
  error: string | null,
  capabilities: OpenEvoDesktopCapabilities | null,
  selections: EvolutionSelections,
): string | null {
  if (loading) {
    return "Remote capabilities are still loading.";
  }
  if (error) {
    return error;
  }
  if (!capabilities) {
    return "Remote capabilities are unavailable.";
  }

  const model = buildEvolutionConfigModel(capabilities, selections);
  const unknownEnabled = model.unknownTargetRows.find((row) => row.enabled);
  if (unknownEnabled) {
    return `enabled target "${unknownEnabled.targetId}" is not available in the remote registry.`;
  }
  const blockedTarget = model.targetRows.find(
    (row) => row.enabled && row.selectionState !== "supported",
  );
  return blockedTarget?.reason ?? null;
}

function buildTargetRow(
  target: OpenEvoEvolutionTargetCapability,
  selection: OpenEvoEvolutionTargetSelection | null,
): EvolutionTargetRow {
  const selectedId = selection?.method ?? null;
  let choices = [
    ...target.methods.map((method) =>
      choiceFromMethod(method, selectedId === method.methodId),
    ),
    ...target.selectionResolvers.map((resolver) =>
      choiceFromResolver(resolver, selectedId === resolver.selectionValue),
    ),
  ];

  const visibleSelected = target.methods.some(
    (method) => method.methodId === selectedId,
  );
  const resolverSelected = target.selectionResolvers.some(
    (resolver) => resolver.selectionValue === selectedId,
  );
  const acceptedSelected = target.acceptedMethods.find(
    (method) => method.methodId === selectedId,
  );
  if (selectedId === null) {
    choices.unshift({
      id: null,
      label: "No method selected",
      description: "This target has no saved method selection.",
      kind: "null-current",
      state: "unavailable",
      reason: `Target "${target.targetId}" has no selected method.`,
      current: true,
      selectable: false,
      defaultConfig: null,
    });
  } else if (!visibleSelected && !resolverSelected && acceptedSelected) {
    choices.unshift({
      id: selectedId,
      label: displayIdentifier(selectedId),
      description: "Saved method accepted by Core but hidden from new selections.",
      kind: "hidden-current",
      state: acceptedSelected.support.overall,
      reason: selectionFailureReason(target, selectedId),
      current: true,
      selectable: false,
      defaultConfig: null,
    });
  } else if (!visibleSelected && !resolverSelected) {
    choices.unshift({
      id: selectedId,
      label: displayIdentifier(selectedId),
      description: "Saved selection no longer present in remote capabilities.",
      kind: "stale-current",
      state: "unavailable",
      reason: selectionFailureReason(target, selectedId),
      current: true,
      selectable: false,
      defaultConfig: null,
    });
  }
  choices = moveCurrentChoiceFirst(choices);

  const selectionState = selectionSupportState(target, selectedId);
  const reason = selectionFailureReason(target, selectedId);
  const effectiveDefaultChoice =
    choices.find(
      (choice) =>
        choice.kind === "method" &&
        choice.id === target.effectiveDefaultMethodId &&
        choice.state === "supported",
    ) ?? null;

  return {
    targetId: target.targetId,
    displayName: target.displayName,
    description: target.description,
    artifactType: target.artifactType,
    contextOrder: target.contextOrder,
    enabled: selection?.enabled ?? false,
    selection,
    selectionState,
    reason,
    choices,
    effectiveDefaultChoice,
    canEnable:
      selectionState === "supported" || effectiveDefaultChoice !== null,
  };
}

function choiceFromMethod(
  method: OpenEvoEvolutionTargetCapability["methods"][number],
  current: boolean,
): EvolutionConfigChoice {
  return {
    id: method.methodId,
    label: method.displayName,
    description: method.description,
    kind: "method",
    state: method.support.overall,
    reason: supportFailureReason(method.support),
    current,
    selectable: method.support.overall === "supported",
    defaultConfig: method.defaultConfig,
  };
}

function choiceFromResolver(
  resolver: OpenEvoEvolutionTargetCapability["selectionResolvers"][number],
  current: boolean,
): EvolutionConfigChoice {
  const state = resolverSupportState(resolver);
  return {
    id: resolver.selectionValue,
    label: resolver.displayName,
    description: resolver.description,
    kind: "resolver",
    state,
    reason: resolverFailureReason(resolver),
    current,
    selectable: state === "supported",
    defaultConfig: null,
  };
}

function selectionSupportState(
  target: OpenEvoEvolutionTargetCapability,
  selectedId: string | null,
): OpenEvoCapabilitySupportState {
  if (selectedId === null) {
    return "unavailable";
  }
  const resolver = target.selectionResolvers.find(
    (candidate) => candidate.selectionValue === selectedId,
  );
  if (resolver) {
    return resolverSupportState(resolver);
  }
  return (
    target.acceptedMethods.find((method) => method.methodId === selectedId)
      ?.support.overall ?? "unavailable"
  );
}

function selectionFailureReason(
  target: OpenEvoEvolutionTargetCapability,
  selectedId: string | null,
): string | null {
  if (selectedId === null) {
    return `Target "${target.targetId}" has no selected method.`;
  }
  const resolver = target.selectionResolvers.find(
    (candidate) => candidate.selectionValue === selectedId,
  );
  if (resolver) {
    const reason = resolverFailureReason(resolver);
    return reason
      ? `Selection resolver "${selectedId}" is ${resolverSupportState(resolver)} for the current remote profile. ${reason}`
      : null;
  }
  const method = target.acceptedMethods.find(
    (candidate) => candidate.methodId === selectedId,
  );
  if (!method) {
    return `Selected method "${selectedId}" is no longer available for target "${target.targetId}" in the remote registry.`;
  }
  const reason = supportFailureReason(method.support);
  if (!reason) {
    return null;
  }
  const availability =
    method.support.overall === "unsupported"
      ? "unsupported by"
      : "unavailable for";
  return `Selected method "${selectedId}" is ${availability} the current remote profile. ${reason}`;
}

function resolverSupportState(
  resolver: OpenEvoEvolutionTargetCapability["selectionResolvers"][number],
): OpenEvoCapabilitySupportState {
  if (
    resolver.resolvedMethods.some(
      (method) => method.support.overall === "unavailable",
    )
  ) {
    return "unavailable";
  }
  if (
    resolver.resolvedMethods.some(
      (method) => method.support.overall === "unsupported",
    )
  ) {
    return "unsupported";
  }
  return "supported";
}

function resolverFailureReason(
  resolver: OpenEvoEvolutionTargetCapability["selectionResolvers"][number],
): string | null {
  const messages = resolver.resolvedMethods.flatMap((method) => {
    const reason = supportFailureReason(method.support);
    return reason ? [reason] : [];
  });
  return unique(messages).join(" ") || null;
}

function supportFailureReason(
  support: OpenEvoCapabilityMethodSupport,
): string | null {
  if (support.overall === "supported") {
    return null;
  }
  const messages = [
    support.execution,
    support.capture,
    support.harness,
    support.runtime,
  ]
    .filter((axis) => axis.state !== "supported")
    .map((axis) => axis.message)
    .filter(Boolean);
  return unique(messages).join(" ") || "This selection is unavailable.";
}

function moveCurrentChoiceFirst(
  choices: EvolutionConfigChoice[],
): EvolutionConfigChoice[] {
  const index = choices.findIndex((choice) => choice.current);
  if (index <= 0) {
    return choices;
  }
  return [
    choices[index]!,
    ...choices.slice(0, index),
    ...choices.slice(index + 1),
  ];
}

function unique(values: string[]): string[] {
  return [...new Set(values)];
}

function displayIdentifier(value: string): string {
  const words = value.replaceAll("_", " ").replaceAll("-", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function copyJsonObject(value: OpenEvoJsonObject): OpenEvoJsonObject {
  return structuredClone(value);
}
