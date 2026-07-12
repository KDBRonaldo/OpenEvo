import { describe, expect, it } from "vitest";
import type {
  OpenEvoCapabilityMethodSupport,
  OpenEvoDesktopCapabilities,
  OpenEvoEvolutionMethodCapability,
  OpenEvoEvolutionTargetCapability,
} from "../api/openevo";
import type {
  OpenEvoEvolutionTargetSelection,
  OpenEvoJsonObject,
} from "./openevoDesktopModel";
import { parseEvolutionConfigSchema } from "../api/evolutionConfigSchema";
import {
  buildEvolutionConfigModel,
  evolutionRunBlockReason,
  resetEvolutionChoiceConfig,
  selectEvolutionChoice,
  setEvolutionTargetEnabled,
} from "./evolutionConfigModel";

describe("evolution configuration model", () => {
  it("orders remote targets by context order and separates every unknown saved target", () => {
    const capabilities = capabilitySet([
      target("target-later", 20, [method("method-later")]),
      target("target-first", 5, [method("method-first")]),
      target("target-tied", 20, [method("method-tied")]),
    ]);
    const selections = {
      "target-later": selection(false, "method-later", { retained: true }),
      "unknown-enabled": selection(true, "old-method", { enabled: true }),
      "unknown-disabled": selection(false, null, { draft: "keep" }),
    };

    const model = buildEvolutionConfigModel(capabilities, selections);

    expect(model.targetRows.map((row) => row.targetId)).toEqual([
      "target-first",
      "target-later",
      "target-tied",
    ]);
    expect(model.unknownTargetRows).toEqual([
      expect.objectContaining({
        targetId: "unknown-enabled",
        enabled: true,
        selection: selections["unknown-enabled"],
      }),
      expect.objectContaining({
        targetId: "unknown-disabled",
        enabled: false,
        selection: selections["unknown-disabled"],
      }),
    ]);
    expect(model.unknownTargetRows[0]?.reason).toContain("remote registry");
    expect(
      setEvolutionTargetEnabled(
        selections,
        model.unknownTargetRows[0]!,
        false,
      ),
    ).toEqual({
      ...selections,
      "unknown-enabled": selection(false, "old-method", { enabled: true }),
    });
    expect(() =>
      setEvolutionTargetEnabled(selections, model.unknownTargetRows[1]!, true),
    ).toThrow("cannot be enabled");
  });

  it("distinguishes visible methods, hidden accepted current methods, resolvers, stale, and null selections", () => {
    const visibleSupported = method("visible-supported");
    const visibleUnsupported = method(
      "visible-unsupported",
      "unsupported",
      "A required executor is missing.",
    );
    const remoteTarget = target("target-choice", 1, [
      visibleSupported,
      visibleUnsupported,
    ]);
    remoteTarget.acceptedMethods.push({
      methodId: "hidden-current",
      implementationIdentityDigest: "hidden-digest",
      support: support("supported"),
    });
    remoteTarget.selectionResolvers = [
      resolver("resolver-supported", "supported"),
      resolver("resolver-unavailable", "unavailable", "History is unavailable."),
    ];

    const supported = buildEvolutionConfigModel(
      capabilitySet([remoteTarget]),
      { "target-choice": selection(true, "visible-supported", {}) },
    ).targetRows[0]!;
    expect(supported.selectionState).toBe("supported");
    expect(supported.reason).toBeNull();
    expect(supported.choices).toEqual([
      expect.objectContaining({
        id: "visible-supported",
        kind: "method",
        selectable: true,
      }),
      expect.objectContaining({
        id: "visible-unsupported",
        kind: "method",
        state: "unsupported",
        selectable: false,
        reason: "A required executor is missing.",
      }),
      expect.objectContaining({
        id: "resolver-supported",
        kind: "resolver",
        selectable: true,
      }),
      expect.objectContaining({
        id: "resolver-unavailable",
        kind: "resolver",
        state: "unavailable",
        selectable: false,
        reason: "History is unavailable.",
      }),
    ]);

    const hidden = buildEvolutionConfigModel(capabilitySet([remoteTarget]), {
      "target-choice": selection(false, "hidden-current", { opaque: 1 }),
    }).targetRows[0]!;
    expect(hidden.choices).toContainEqual(
      expect.objectContaining({
        id: "hidden-current",
        kind: "hidden-current",
        current: true,
        selectable: false,
      }),
    );
    expect(hidden.choices.filter((choice) => choice.id === "hidden-current")).toHaveLength(1);

    const stale = buildEvolutionConfigModel(capabilitySet([remoteTarget]), {
      "target-choice": selection(true, "deleted-method", { stale: true }),
    }).targetRows[0]!;
    expect(stale.selectionState).toBe("unavailable");
    expect(stale.reason).toContain("no longer available");
    expect(stale.choices).toContainEqual(
      expect.objectContaining({
        id: "deleted-method",
        kind: "stale-current",
        current: true,
        selectable: false,
      }),
    );

    const empty = buildEvolutionConfigModel(capabilitySet([remoteTarget]), {
      "target-choice": selection(true, null, { empty: true }),
    }).targetRows[0]!;
    expect(empty.selectionState).toBe("unavailable");
    expect(empty.reason).toContain("no selected method");
    expect(empty.choices[0]).toEqual(
      expect.objectContaining({ id: null, kind: "null-current", current: true }),
    );
  });

  it("derives unsupported resolver state from its resolved methods", () => {
    const remoteTarget = target("target-resolver", 1, [method("method-ok")]);
    remoteTarget.selectionResolvers = [
      resolver("resolver-broken", "unsupported", "One resolved method is blocked."),
    ];

    const row = buildEvolutionConfigModel(capabilitySet([remoteTarget]), {
      "target-resolver": selection(true, "resolver-broken", {}),
    }).targetRows[0]!;

    expect(row.selectionState).toBe("unsupported");
    expect(row.reason).toContain("One resolved method is blocked.");
    expect(row.choices[0]).toEqual(
      expect.objectContaining({
        id: "resolver-broken",
        state: "unsupported",
        selectable: false,
      }),
    );
  });

  it("does not invent an effective method and only enables through a supported default", () => {
    const withoutDefault = target("target-empty", 1, [method("method-visible")]);
    withoutDefault.effectiveDefaultMethodId = null;
    const selections = {
      "target-empty": selection(false, null, { retained: true }),
    };
    const noDefaultModel = buildEvolutionConfigModel(
      capabilitySet([withoutDefault]),
      selections,
    );

    expect(noDefaultModel.targetRows[0]?.effectiveDefaultChoice).toBeNull();
    expect(noDefaultModel.targetRows[0]?.canEnable).toBe(false);
    expect(() =>
      setEvolutionTargetEnabled(selections, noDefaultModel.targetRows[0]!, true),
    ).toThrow("cannot be enabled");
    expect(selections["target-empty"].method).toBeNull();

    const withDefault = target("target-default", 1, [
      method("method-default", "supported", "Supported.", { fresh: 2 }),
    ]);
    const defaultSelections = {
      "target-default": selection(false, null, { obsolete: true }),
    };
    const defaultRow = buildEvolutionConfigModel(
      capabilitySet([withDefault]),
      defaultSelections,
    ).targetRows[0]!;

    expect(setEvolutionTargetEnabled(defaultSelections, defaultRow, true)).toEqual({
      "target-default": selection(true, "method-default", { fresh: 2 }),
    });
  });

  it("preserves config when keeping a selection or disabling and replaces it atomically on change", () => {
    const remoteTarget = target("target-edit", 1, [
      method("method-one", "supported", "Supported.", { one: true }),
      method("method-two", "supported", "Supported.", { two: true }),
    ]);
    remoteTarget.selectionResolvers = [resolver("resolver-choice", "supported")];
    const existing = {
      "target-edit": selection(true, "method-one", { custom: "keep" }),
    };
    const row = buildEvolutionConfigModel(
      capabilitySet([remoteTarget]),
      existing,
    ).targetRows[0]!;

    expect(selectEvolutionChoice(existing, row, "method-one")).toBe(existing);
    expect(selectEvolutionChoice(existing, row, "method-two")).toEqual({
      "target-edit": selection(true, "method-two", { two: true }),
    });
    const resolverSelected = selectEvolutionChoice(existing, row, "resolver-choice");
    expect(resolverSelected).toEqual({
      "target-edit": selection(true, "resolver-choice", {}),
    });
    const resolverRow = buildEvolutionConfigModel(
      capabilitySet([remoteTarget]),
      resolverSelected,
    ).targetRows[0]!;
    resolverSelected["target-edit"].config = { opaque: "preserve" };
    expect(
      selectEvolutionChoice(resolverSelected, resolverRow, "resolver-choice"),
    ).toBe(resolverSelected);

    expect(setEvolutionTargetEnabled(existing, row, false)).toEqual({
      "target-edit": selection(false, "method-one", { custom: "keep" }),
    });
    expect(() => selectEvolutionChoice(existing, row, "missing-choice")).toThrow(
      "not selectable",
    );
  });

  it("resets visible method and resolver config without repairing opaque selections", () => {
    const remoteTarget = target("target-reset", 1, [
      method("method-visible", "supported", "Supported.", {
        nested: { current: true },
      }),
    ]);
    remoteTarget.acceptedMethods.push({
      methodId: "method-hidden",
      implementationIdentityDigest: "hidden-digest",
      support: support("supported"),
    });
    remoteTarget.selectionResolvers = [resolver("resolver-choice", "supported")];
    const capabilities = capabilitySet([remoteTarget]);

    const visibleSelections = {
      "target-reset": selection(true, "method-visible", {
        removed_by_schema: true,
      }),
    };
    const visibleRow = buildEvolutionConfigModel(
      capabilities,
      visibleSelections,
    ).targetRows[0]!;
    const resetVisible = resetEvolutionChoiceConfig(
      visibleSelections,
      visibleRow,
    );
    expect(resetVisible).toEqual({
      "target-reset": selection(true, "method-visible", {
        nested: { current: true },
      }),
    });
    expect(resetVisible["target-reset"].config).not.toBe(
      remoteTarget.methods[0]?.defaultConfig,
    );

    const resolverSelections = {
      "target-reset": selection(false, "resolver-choice", { opaque: true }),
    };
    expect(
      resetEvolutionChoiceConfig(
        resolverSelections,
        buildEvolutionConfigModel(capabilities, resolverSelections).targetRows[0]!,
      ),
    ).toEqual({
      "target-reset": selection(false, "resolver-choice", {}),
    });

    for (const methodId of ["method-hidden", "method-stale", null]) {
      const opaqueSelections = {
        "target-reset": selection(true, methodId, { opaque: true }),
      };
      const opaqueRow = buildEvolutionConfigModel(
        capabilities,
        opaqueSelections,
      ).targetRows[0]!;
      expect(() =>
        resetEvolutionChoiceConfig(opaqueSelections, opaqueRow),
      ).toThrow("cannot be reset");
      expect(opaqueSelections["target-reset"].config).toEqual({ opaque: true });
    }
  });

  it("reports capability and enabled-selection run blockers", () => {
    const remoteTarget = target("target-run", 1, [
      method("method-ok"),
      method("method-bad", "unsupported", "Runtime mismatch."),
    ]);
    const capabilities = capabilitySet([remoteTarget]);

    expect(evolutionRunBlockReason(true, "ignored", null, {})).toContain("loading");
    expect(evolutionRunBlockReason(false, "Remote lookup failed.", null, {})).toBe(
      "Remote lookup failed.",
    );
    expect(evolutionRunBlockReason(false, null, null, {})).toContain("unavailable");
    expect(
      evolutionRunBlockReason(false, null, capabilities, {
        "unknown-target": selection(true, "method-old", {}),
      }),
    ).toContain('enabled target "unknown-target"');
    expect(
      evolutionRunBlockReason(false, null, capabilities, {
        "target-run": selection(true, null, {}),
      }),
    ).toContain("no selected method");
    expect(
      evolutionRunBlockReason(false, null, capabilities, {
        "target-run": selection(true, "method-gone", {}),
      }),
    ).toContain("no longer available");
    expect(
      evolutionRunBlockReason(false, null, capabilities, {
        "target-run": selection(true, "method-bad", {}),
      }),
    ).toContain("Runtime mismatch.");
    expect(
      evolutionRunBlockReason(false, null, capabilities, {
        "target-run": selection(false, "method-bad", {}),
      }),
    ).toBeNull();
  });
});

function capabilitySet(
  targets: OpenEvoEvolutionTargetCapability[],
): OpenEvoDesktopCapabilities {
  return {
    schemaVersion: "1",
    coreVersion: "test",
    registryDigest: "registry",
    evaluatedProfile: {
      executionMode: "subscription",
      captureMode: "transcript",
      harnessId: "synthetic-harness",
      harnessCapabilities: [],
      runtimeCapabilities: [],
    },
    targets,
  };
}

function target(
  targetId: string,
  contextOrder: number,
  methods: OpenEvoEvolutionMethodCapability[],
): OpenEvoEvolutionTargetCapability {
  const configured = methods[0]!;
  return {
    targetId,
    displayName: `Display ${targetId}`,
    description: `Description ${targetId}`,
    artifactType: `artifact-${targetId}`,
    exposure: "desktop",
    maturity: "stable",
    handlerId: `handler-${targetId}`,
    configuredDefaultMethodId: configured.methodId,
    effectiveDefaultMethodId:
      configured.support.overall === "supported" ? configured.methodId : null,
    configuredDefaultSupport: configured.support,
    rendererKind: "markdown",
    rendererContractVersion: "1",
    contributionContractVersion: "1",
    contextOrder,
    implementationIdentityDigest: `target-digest-${targetId}`,
    handlerIdentityDigest: `handler-digest-${targetId}`,
    acceptedMethods: methods.map((item) => ({
      methodId: item.methodId,
      implementationIdentityDigest: item.implementationIdentityDigest,
      support: item.support,
    })),
    selectionResolvers: [],
    methods,
  };
}

function method(
  methodId: string,
  state: "supported" | "unsupported" | "unavailable" = "supported",
  message = "Supported.",
  defaultConfig: OpenEvoJsonObject = {},
): OpenEvoEvolutionMethodCapability {
  return {
    methodId,
    displayName: `Display ${methodId}`,
    description: `Description ${methodId}`,
    exposure: "desktop",
    maturity: "stable",
    executionModes: ["subscription"],
    captureModes: ["transcript"],
    supportedHarnessIds: ["synthetic-harness"],
    harnessRequirements: [],
    runtimeRequirements: [],
    inputBindings: [],
    outputArtifactTypes: [],
    configSchemaJson: "{}",
    defaultConfigJson: JSON.stringify(defaultConfig),
    configSchema: parseEvolutionConfigSchema({
      type: "object",
      properties: {},
      additionalProperties: false,
    }),
    defaultConfig,
    implementationIdentityDigest: `method-digest-${methodId}`,
    support: support(state, message),
  };
}

function resolver(
  selectionValue: string,
  state: "supported" | "unsupported" | "unavailable",
  message = "Supported.",
): OpenEvoEvolutionTargetCapability["selectionResolvers"][number] {
  return {
    selectionValue,
    displayName: `Display ${selectionValue}`,
    description: `Description ${selectionValue}`,
    resolvedMethods: [
      {
        methodId: `resolved-${selectionValue}`,
        implementationIdentityDigest: `resolver-digest-${selectionValue}`,
        support: support(state, message),
      },
    ],
  };
}

function support(
  state: "supported" | "unsupported" | "unavailable",
  message = "Supported.",
): OpenEvoCapabilityMethodSupport {
  const axis = {
    state,
    reasonCode: state === "supported" ? null : `reason-${state}`,
    message,
    missingRequirements: [],
  };
  return {
    overall: state,
    execution: { ...axis },
    capture: {
      state: "supported",
      reasonCode: null,
      message: "Supported.",
      missingRequirements: [],
    },
    harness: {
      state: "supported",
      reasonCode: null,
      message: "Supported.",
      missingRequirements: [],
    },
    runtime: {
      state: "supported",
      reasonCode: null,
      message: "Supported.",
      missingRequirements: [],
    },
  };
}

function selection(
  enabled: boolean,
  methodId: string | null,
  config: OpenEvoJsonObject,
): OpenEvoEvolutionTargetSelection {
  return { enabled, method: methodId, config };
}
