import { describe, expect, it } from "vitest";
import {
  EvolutionConfigSchemaError,
  effectiveEvolutionConfig,
  parseEvolutionConfigSchema,
  validateEvolutionConfig,
  validateEvolutionConfigOverride,
} from "./evolutionConfigSchema";

const schemaDocument = {
  type: "object",
  additionalProperties: false,
  properties: {
    enabled: { type: "boolean", default: true },
    name: { type: "string", minLength: 2, maxLength: 20 },
    retries: { type: "integer", minimum: 0, maximum: 5 },
    ratio: { type: "number", exclusiveMinimum: 0, maximum: 1 },
    mode: { type: "string", enum: ["fast", "careful"] },
    note: {
      anyOf: [{ type: "string", maxLength: 40 }, { type: "null" }],
      title: "Optional note",
    },
    tags: {
      type: "array",
      items: { type: "string", minLength: 1 },
      minItems: 1,
      maxItems: 3,
    },
    nested: {
      type: "object",
      additionalProperties: false,
      properties: { count: { type: "integer" } },
      required: ["count"],
    },
    api_key_ref: {
      type: "string",
      "x-openevo-secret-ref": true,
    },
  },
  required: ["name", "nested"],
} as const;

describe("parseEvolutionConfigSchema", () => {
  it("parses the complete bounded schema subset without applying defaults", () => {
    const schema = parseEvolutionConfigSchema(schemaDocument);

    expect(schema.type).toBe("object");
    expect(schema.properties.note).toMatchObject({ kind: "nullable" });
    expect(schema.properties.api_key_ref).toMatchObject({
      kind: "string",
      secretRef: true,
    });
    expect(schema.properties.enabled.default).toBe(true);
  });

  it.each([
    [{ ...schemaDocument, patternProperties: {} }, "schema.patternProperties"],
    [
      {
        type: "object",
        additionalProperties: true,
        properties: {},
      },
      "schema.additionalProperties",
    ],
    [
      {
        type: "object",
        additionalProperties: false,
        properties: { value: { anyOf: [{ type: "string" }] } },
      },
      "schema.properties.value.anyOf",
    ],
    [
      {
        type: "object",
        additionalProperties: false,
        properties: { value: { type: "string", minimum: 1 } },
      },
      "schema.properties.value.minimum",
    ],
    [
      {
        type: "object",
        additionalProperties: false,
        properties: { value: { type: "integer", default: 1.5 } },
      },
      "schema.properties.value.default",
    ],
  ])("rejects malformed or unsupported schemas", (document, path) => {
    let error: unknown;
    try {
      parseEvolutionConfigSchema(document);
    } catch (caught) {
      error = caught;
    }
    expect(error).toBeInstanceOf(EvolutionConfigSchemaError);
    expect(error).toMatchObject({ path });
  });

  it.each([
    [Number.NaN, "number must be finite"],
    [Number.POSITIVE_INFINITY, "number must be finite"],
    [Number.MAX_SAFE_INTEGER + 1, "safe integer"],
  ])("rejects lossy numeric schema values", (maximum, message) => {
    expect(() =>
      parseEvolutionConfigSchema({
        type: "object",
        additionalProperties: false,
        properties: { value: { type: "number", maximum } },
      }),
    ).toThrow(message);
  });

  it("rejects unsafe integers recursively in annotations", () => {
    expect(() =>
      parseEvolutionConfigSchema({
        type: "object",
        additionalProperties: false,
        properties: {
          value: {
            type: "object",
            additionalProperties: false,
            properties: {},
            default: { unsafe: Number.MAX_SAFE_INTEGER + 1 },
          },
        },
      }),
    ).toThrow("safe integer");
  });

  it("rejects sensitive values embedded recursively in defaults", () => {
    expect(() =>
      parseEvolutionConfigSchema({
        type: "object",
        additionalProperties: false,
        properties: {
          value: {
            type: "object",
            additionalProperties: false,
            properties: {},
            default: { nested: { password: "do-not-embed" } },
          },
        },
      }),
    ).toThrow("must not contain sensitive field defaults");
  });

  it.each([
    ["enum", [{ nested: { password: "do-not-embed" } }]],
    ["const", { nested: { api_key_ref: "openevo-secret:production" } }],
  ])("rejects sensitive values embedded recursively in %s", (keyword, value) => {
    expect(() =>
      parseEvolutionConfigSchema({
        type: "object",
        additionalProperties: false,
        properties: {
          wrapper: {
            type: "object",
            additionalProperties: false,
            properties: {},
            [keyword]: value,
          },
        },
      }),
    ).toThrow("must not contain sensitive field values");
  });

  it("compares defaults after applying nested schema defaults", () => {
    expect(() =>
      parseEvolutionConfigSchema({
        type: "object",
        additionalProperties: false,
        properties: {
          wrapper: {
            type: "object",
            additionalProperties: false,
            properties: { child: { type: "integer", default: 1 } },
            default: {},
            const: { child: 1 },
          },
        },
      }),
    ).not.toThrow();
  });

  it("counts Unicode code points like Core instead of UTF-16 code units", () => {
    const astralText = "\u{1F9EC}".repeat(3000);
    const unicodeSchema = parseEvolutionConfigSchema({
      type: "object",
      additionalProperties: false,
      properties: {
        value: { type: "string", maxLength: 4096, default: astralText },
      },
    });

    expect(validateEvolutionConfig(unicodeSchema, { value: astralText }).valid).toBe(
      true,
    );
  });

  it("preserves JSON properties named __proto__ without accepting symbol keywords", () => {
    const document = JSON.parse(
      '{"type":"object","additionalProperties":false,"properties":{"__proto__":{"type":"string"}}}',
    );
    const schema = parseEvolutionConfigSchema(document);
    expect(Object.hasOwn(schema.properties, "__proto__")).toBe(true);
    expect(
      validateEvolutionConfig(schema, JSON.parse('{"__proto__":"value"}')).valid,
    ).toBe(true);

    document[Symbol("keyword")] = true;
    expect(() => parseEvolutionConfigSchema(document)).toThrow(
      "schema keyword names must be strings",
    );
  });
});

describe("validateEvolutionConfig", () => {
  const schema = parseEvolutionConfigSchema(schemaDocument);

  it("treats a project config as a recursive partial override", () => {
    expect(validateEvolutionConfig(schema, {})).toEqual({
      valid: true,
      errors: [],
    });
    expect(validateEvolutionConfig(schema, { nested: {} }).valid).toBe(true);
    expect(validateEvolutionConfig(schema, {})).not.toHaveProperty("value");
  });

  it("preserves missing, explicit null, and concrete values", () => {
    expect(validateEvolutionConfig(schema, {}).valid).toBe(true);
    expect(validateEvolutionConfig(schema, { note: null }).valid).toBe(true);
    expect(validateEvolutionConfig(schema, { note: "hello" }).valid).toBe(true);
    expect(validateEvolutionConfig(schema, { name: null })).toMatchObject({
      valid: false,
      errors: [{ path: "config.name" }],
    });
  });

  it("returns field-level errors for every invalid present value", () => {
    const result = validateEvolutionConfig(schema, {
      name: "x",
      retries: 8,
      ratio: 0,
      mode: "unknown",
      tags: [],
      extra: true,
    });

    expect(result.valid).toBe(false);
    expect(result.errors.map((error) => error.path)).toEqual([
      "config.extra",
      "config.name",
      "config.retries",
      "config.ratio",
      "config.mode",
      "config.tags",
    ]);
  });

  it("validates opaque secret references without exposing a secret value", () => {
    expect(
      validateEvolutionConfig(schema, {
        api_key_ref: "openevo-secret:research-api_key.1",
      }).valid,
    ).toBe(true);
    expect(validateEvolutionConfig(schema, { api_key_ref: "plaintext" })).toMatchObject({
      valid: false,
      errors: [{ path: "config.api_key_ref" }],
    });
  });

  it("rejects non-finite and unsafe config numbers recursively", () => {
    const numericSchema = parseEvolutionConfigSchema({
      type: "object",
      additionalProperties: false,
      properties: {
        values: { type: "array", items: { type: "number" } },
      },
    });

    expect(validateEvolutionConfig(numericSchema, { values: [Number.NaN] }).valid).toBe(
      false,
    );
    expect(
      validateEvolutionConfig(numericSchema, {
        values: [Number.MAX_SAFE_INTEGER + 1],
      }).valid,
    ).toBe(false);
  });

  it("deep-merges defaults and requires the final effective config", () => {
    const effectiveSchema = parseEvolutionConfigSchema({
      type: "object",
      additionalProperties: false,
      properties: {
        settings: {
          type: "object",
          additionalProperties: false,
          properties: {
            model: { type: "string" },
            timeout: { type: "integer", minimum: 1 },
            retries: { type: "integer", default: 2 },
          },
          required: ["model", "timeout"],
        },
      },
      required: ["settings"],
    });

    expect(
      validateEvolutionConfigOverride(
        effectiveSchema,
        { settings: { model: "remote", timeout: 30 } },
        { settings: { timeout: 60 } },
      ),
    ).toEqual({ valid: true, errors: [] });
    expect(
      effectiveEvolutionConfig(
        effectiveSchema,
        { settings: { model: "remote", timeout: 30 } },
        { settings: { timeout: 60 } },
      ),
    ).toEqual({ settings: { model: "remote", timeout: 60, retries: 2 } });
    expect(
      validateEvolutionConfigOverride(
        effectiveSchema,
        {},
        { settings: { timeout: 60 } },
      ),
    ).toMatchObject({
      valid: false,
      errors: [{ path: "config.settings.model" }],
    });
  });

  it("rejects non-JSON object instances and symbol properties", () => {
    expect(validateEvolutionConfig(schema, new Date()).valid).toBe(false);
    const value = { name: "valid" } as Record<PropertyKey, unknown>;
    value[Symbol("hidden")] = true;
    expect(validateEvolutionConfig(schema, value).valid).toBe(false);
  });
});
