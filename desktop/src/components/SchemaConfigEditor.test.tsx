// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  parseEvolutionConfigSchema,
  type OpenEvoJsonObject,
} from "../api/evolutionConfigSchema";
import { SchemaConfigEditor } from "./SchemaConfigEditor";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const schema = parseEvolutionConfigSchema({
  type: "object",
  additionalProperties: false,
  properties: {
    enabled: { type: "boolean", title: "Enabled" },
    mode: { type: "string", title: "Mode", enum: ["fast", "careful"] },
    threshold: { type: "number", title: "Threshold", minimum: 0 },
    notes: { type: "string", title: "Notes", description: "Long form notes" },
    optional_note: {
      anyOf: [{ type: "string" }, { type: "null" }],
      title: "Optional note",
    },
    labels: {
      type: "array",
      title: "Labels",
      maxItems: 2,
      items: { type: "string", title: "Label" },
    },
    nested: {
      type: "object",
      title: "Advanced",
      additionalProperties: false,
      properties: { count: { type: "integer", title: "Count", minimum: 1 } },
    },
    credential_ref: {
      type: "string",
      title: "Credential reference",
      "x-openevo-secret-ref": true,
    },
  },
});

describe("SchemaConfigEditor", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  afterEach(() => {
    document.body.innerHTML = "";
  });

  it("renders stable accessible controls recursively", async () => {
    const onChange = vi.fn();
    const root = await renderEditor(
      {
        enabled: true,
        mode: "careful",
        threshold: 0.5,
        notes: "Details",
        optional_note: null,
        labels: ["one"],
        nested: { count: 2 },
        credential_ref: "openevo-secret:credential-1",
      },
      onChange,
    );

    expect(inputByLabel("Enabled").getAttribute("type")).toBe("checkbox");
    expect(document.querySelector('select[aria-label="Mode"]')).not.toBeNull();
    expect(inputByLabel("Threshold").getAttribute("type")).toBe("number");
    expect(document.querySelector('textarea[aria-label="Notes"]')).not.toBeNull();
    expect(inputByLabel("Use null for Optional note")).toBeInstanceOf(
      HTMLInputElement,
    );
    expect(inputByLabel("Count").getAttribute("type")).toBe("number");
    expect(inputByLabel("Credential reference").getAttribute("autocomplete")).toBe(
      "off",
    );
    expect(buttonByLabel("Add Labels item")).not.toBeNull();
    expect(buttonByLabel("Remove Labels item 1")).not.toBeNull();
    await unmount(root);
  });

  it("emits immutable edits and never applies schema defaults", async () => {
    const onChange = vi.fn();
    const value = { mode: "fast", labels: ["one"] } satisfies OpenEvoJsonObject;
    const root = await renderEditor(value, onChange);

    await change(inputByLabel("Threshold"), "0.75");
    expect(onChange).toHaveBeenLastCalledWith({
      mode: "fast",
      labels: ["one"],
      threshold: 0.75,
    });
    expect(value).toEqual({ mode: "fast", labels: ["one"] });
    expect(onChange.mock.lastCall?.[0]).not.toHaveProperty("enabled");
    await unmount(root);
  });

  it("displays inherited method defaults without materializing them", async () => {
    const onChange = vi.fn();
    const root = await renderEditor(
      {},
      onChange,
      undefined,
      { enabled: true, nested: { count: 2 } },
    );

    expect(inputByLabel("Enabled").checked).toBe(true);
    expect(inputByLabel("Count").value).toBe("2");
    expect(document.querySelector('[aria-label="Clear Enabled"]')).toBeNull();

    await click(inputByLabel("Enabled"));
    expect(onChange).toHaveBeenLastCalledWith({ enabled: false });
    await unmount(root);
  });

  it("supports nullable state and array add/remove without collapsing missing", async () => {
    const onChange = vi.fn();
    const root = await renderEditor({ optional_note: null, labels: ["one"] }, onChange);

    await click(inputByLabel("Use null for Optional note"));
    expect(onChange).toHaveBeenLastCalledWith({ optional_note: "", labels: ["one"] });

    await click(buttonByLabel("Add Labels item"));
    expect(onChange).toHaveBeenLastCalledWith({
      optional_note: null,
      labels: ["one", ""],
    });

    await click(buttonByLabel("Remove Labels item 1"));
    expect(onChange).toHaveBeenLastCalledWith({ optional_note: null, labels: [] });
    await unmount(root);
  });

  it("reports and displays current field validation", async () => {
    const onValidationChange = vi.fn();
    const root = await renderEditor(
      { threshold: -2, credential_ref: "not-a-reference" },
      vi.fn(),
      onValidationChange,
    );

    expect(onValidationChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ valid: false }),
    );
    expect(document.body.textContent).toContain("must be at least 0");
    expect(inputByLabel("Threshold").getAttribute("aria-invalid")).toBe("true");
    expect(document.body.textContent).not.toContain("not-a-reference");
    await unmount(root);
  });

  it("can represent present empty arrays and objects distinctly from missing", async () => {
    const onChange = vi.fn();
    const root = await renderEditor({}, onChange);

    await click(buttonByLabel("Set Labels to empty array"));
    expect(onChange).toHaveBeenLastCalledWith({ labels: [] });

    await click(buttonByLabel("Set Advanced to empty object"));
    expect(onChange).toHaveBeenLastCalledWith({ nested: {} });
    await unmount(root);
  });
});

async function renderEditor(
  value: OpenEvoJsonObject,
  onChange: (value: OpenEvoJsonObject) => void,
  onValidationChange?: Parameters<typeof SchemaConfigEditor>[0]["onValidationChange"],
  defaultValue?: OpenEvoJsonObject,
): Promise<Root> {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <SchemaConfigEditor
        schema={schema}
        value={value}
        defaultValue={defaultValue}
        onChange={onChange}
        onValidationChange={onValidationChange}
      />,
    );
  });
  return root;
}

function inputByLabel(label: string): HTMLInputElement {
  const input = document.querySelector(`[aria-label="${label}"]`);
  if (!(input instanceof HTMLInputElement)) throw new Error(`Missing input ${label}`);
  return input;
}

function buttonByLabel(label: string): HTMLButtonElement {
  const button = document.querySelector(`[aria-label="${label}"]`);
  if (!(button instanceof HTMLButtonElement)) throw new Error(`Missing button ${label}`);
  return button;
}

async function change(input: HTMLInputElement, value: string): Promise<void> {
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )?.set;
    setter?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

async function click(element: HTMLElement): Promise<void> {
  await act(async () => {
    element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
}

async function unmount(root: Root): Promise<void> {
  await act(async () => root.unmount());
}
