export const MAX_SCHEMA_DEPTH = 8;
export const MAX_SCHEMA_NODES = 256;
export const MAX_OBJECT_PROPERTIES = 64;
export const MAX_ENUM_VALUES = 128;
export const MAX_STRING_LENGTH = 4096;
export const MAX_ARRAY_ITEMS = 256;

export type OpenEvoJsonPrimitive = null | boolean | number | string;
export type OpenEvoJsonValue =
  | OpenEvoJsonPrimitive
  | OpenEvoJsonValue[]
  | OpenEvoJsonObject;
export type OpenEvoJsonObject = { [key: string]: OpenEvoJsonValue };

interface SchemaAnnotations {
  readonly title?: string;
  readonly description?: string;
  readonly enum?: readonly OpenEvoJsonValue[];
  readonly const?: OpenEvoJsonValue;
  readonly default?: OpenEvoJsonValue;
}

export interface OpenEvoObjectSchema extends SchemaAnnotations {
  readonly kind: "object";
  readonly type: "object";
  readonly properties: Readonly<Record<string, OpenEvoConfigSchema>>;
  readonly required: readonly string[];
  readonly additionalProperties: false;
}

export interface OpenEvoArraySchema extends SchemaAnnotations {
  readonly kind: "array";
  readonly type: "array";
  readonly items: OpenEvoConfigSchema;
  readonly minItems?: number;
  readonly maxItems?: number;
}

export interface OpenEvoStringSchema extends SchemaAnnotations {
  readonly kind: "string";
  readonly type: "string";
  readonly minLength?: number;
  readonly maxLength?: number;
  readonly secretRef: boolean;
  readonly "x-openevo-secret-ref"?: true;
}

export interface OpenEvoNumberSchema extends SchemaAnnotations {
  readonly kind: "number";
  readonly type: "number";
  readonly minimum?: number;
  readonly maximum?: number;
  readonly exclusiveMinimum?: number;
  readonly exclusiveMaximum?: number;
}

export interface OpenEvoIntegerSchema extends SchemaAnnotations {
  readonly kind: "integer";
  readonly type: "integer";
  readonly minimum?: number;
  readonly maximum?: number;
  readonly exclusiveMinimum?: number;
  readonly exclusiveMaximum?: number;
}

export interface OpenEvoBooleanSchema extends SchemaAnnotations {
  readonly kind: "boolean";
  readonly type: "boolean";
}

export interface OpenEvoNullableSchema extends SchemaAnnotations {
  readonly kind: "nullable";
  readonly anyOf: readonly [OpenEvoNonNullableSchema, { readonly type: "null" }];
  readonly valueSchema: OpenEvoNonNullableSchema;
}

export type OpenEvoNonNullableSchema =
  | OpenEvoObjectSchema
  | OpenEvoArraySchema
  | OpenEvoStringSchema
  | OpenEvoNumberSchema
  | OpenEvoIntegerSchema
  | OpenEvoBooleanSchema;
export type OpenEvoConfigSchema = OpenEvoNonNullableSchema | OpenEvoNullableSchema;

export interface EvolutionConfigValidationError {
  readonly path: string;
  readonly message: string;
}

export interface EvolutionConfigValidationResult {
  readonly valid: boolean;
  readonly errors: readonly EvolutionConfigValidationError[];
}

export class EvolutionConfigSchemaError extends Error {
  readonly path: string;

  constructor(path: string, message: string) {
    super(`${path}: ${message}`);
    this.name = "EvolutionConfigSchemaError";
    this.path = path;
  }
}

const COMMON_KEYWORDS = new Set(["type", "title", "description", "enum", "const", "default"]);
const NULLABLE_KEYWORDS = new Set(["anyOf", "title", "description", "enum", "const", "default"]);
const TYPE_KEYWORDS: Record<string, ReadonlySet<string>> = {
  object: new Set(["properties", "required", "additionalProperties"]),
  array: new Set(["items", "minItems", "maxItems"]),
  string: new Set(["minLength", "maxLength", "x-openevo-secret-ref"]),
  number: new Set(["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"]),
  integer: new Set(["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"]),
  boolean: new Set(),
};
const SUPPORTED_TYPES = new Set(Object.keys(TYPE_KEYWORDS));
const SECRET_REFERENCE = /^openevo-secret:[A-Za-z0-9_.-]{1,128}$/;

interface ParseState {
  nodes: number;
  active: Set<object>;
}

export function parseEvolutionConfigSchema(input: unknown): OpenEvoObjectSchema {
  const schema = parseSchema(input, "schema", 1, { nodes: 0, active: new Set() });
  if (schema.kind !== "object") {
    fail("schema.type", "root schema must have type=object");
  }
  return schema;
}

export function validateEvolutionConfig(
  schema: OpenEvoObjectSchema,
  value: unknown,
): EvolutionConfigValidationResult {
  const errors: EvolutionConfigValidationError[] = [];
  validateValue(schema, value, "config", errors, {
    partial: true,
    applyDefaults: false,
  });
  return { valid: errors.length === 0, errors };
}

export function validateEvolutionConfigOverride(
  schema: OpenEvoObjectSchema,
  defaultConfig: OpenEvoJsonObject,
  override: OpenEvoJsonObject,
): EvolutionConfigValidationResult {
  const errors: EvolutionConfigValidationError[] = [];
  validateValue(
    schema,
    mergeJsonValue(defaultConfig, override),
    "config",
    errors,
    { partial: false, applyDefaults: true },
  );
  return { valid: errors.length === 0, errors };
}

export function effectiveEvolutionConfig(
  schema: OpenEvoObjectSchema,
  defaultConfig: OpenEvoJsonObject,
  override: OpenEvoJsonObject,
): OpenEvoJsonObject {
  return valueWithSchemaDefaults(
    schema,
    mergeJsonValue(defaultConfig, override),
  ) as OpenEvoJsonObject;
}

export function isOpenEvoSecretReference(value: string): boolean {
  return SECRET_REFERENCE.test(value);
}

function parseSchema(
  input: unknown,
  path: string,
  depth: number,
  state: ParseState,
): OpenEvoConfigSchema {
  const raw = expectRecord(input, path, "schema node must be an object");
  if (depth > MAX_SCHEMA_DEPTH) fail(path, `schema exceeds maximum depth ${MAX_SCHEMA_DEPTH}`);
  state.nodes += 1;
  if (state.nodes > MAX_SCHEMA_NODES) {
    fail(path, `schema exceeds maximum node count ${MAX_SCHEMA_NODES}`);
  }
  if (state.active.has(raw)) fail(path, "recursive schemas are forbidden");
  state.active.add(raw);

  try {
    if (Reflect.ownKeys(raw).some((key) => typeof key !== "string")) {
      fail(path, "schema keyword names must be strings");
    }
    if (Object.hasOwn(raw, "anyOf")) {
      rejectKeywords(raw, NULLABLE_KEYWORDS, path);
      const branches = raw.anyOf;
      if (!Array.isArray(branches) || branches.length !== 2) {
        fail(`${path}.anyOf`, "nullable anyOf must contain exactly two schemas");
      }
      const nullIndexes = branches.flatMap((branch, index) =>
        isExactNullSchema(branch) ? [index] : [],
      );
      if (nullIndexes.length !== 1) {
        fail(`${path}.anyOf`, "nullable anyOf must contain exactly one type=null schema");
      }
      state.nodes += 1;
      if (state.nodes > MAX_SCHEMA_NODES) {
        fail(`${path}.anyOf[${nullIndexes[0]}]`, `schema exceeds maximum node count ${MAX_SCHEMA_NODES}`);
      }
      if (depth + 1 > MAX_SCHEMA_DEPTH) {
        fail(`${path}.anyOf[${nullIndexes[0]}]`, `schema exceeds maximum depth ${MAX_SCHEMA_DEPTH}`);
      }
      const valueIndex = nullIndexes[0] === 0 ? 1 : 0;
      const valueRaw = branches[valueIndex];
      if (isRecord(valueRaw) && Object.hasOwn(valueRaw, "anyOf")) {
        fail(`${path}.anyOf[${valueIndex}].anyOf`, "nested nullable anyOf is forbidden");
      }
      const valueSchema = parseSchema(
        valueRaw,
        `${path}.anyOf[${valueIndex}]`,
        depth + 1,
        state,
      );
      if (valueSchema.kind === "nullable") {
        fail(`${path}.anyOf[${valueIndex}].anyOf`, "nested nullable anyOf is forbidden");
      }
      const annotations = parseAnnotations(raw, path);
      const parsed: OpenEvoNullableSchema = {
        kind: "nullable",
        anyOf: [valueSchema, { type: "null" }],
        valueSchema,
        ...annotations,
      };
      validateSchemaAnnotations(parsed, path);
      return parsed;
    }

    if (typeof raw.type !== "string" || !SUPPORTED_TYPES.has(raw.type)) {
      fail(`${path}.type`, "must be one supported scalar, object, or array type");
    }
    rejectKeywords(raw, unionSets(COMMON_KEYWORDS, TYPE_KEYWORDS[raw.type]), path);
    const annotations = parseAnnotations(raw, path);
    let parsed: OpenEvoNonNullableSchema;

    switch (raw.type) {
      case "object":
        parsed = parseObjectSchema(raw, path, depth, state, annotations);
        break;
      case "array":
        parsed = parseArraySchema(raw, path, depth, state, annotations);
        break;
      case "string":
        parsed = parseStringSchema(raw, path, annotations);
        break;
      case "number":
      case "integer":
        parsed = parseNumericSchema(raw, path, annotations, raw.type);
        break;
      case "boolean":
        parsed = { kind: "boolean", type: "boolean", ...annotations };
        break;
      default:
        fail(`${path}.type`, "unsupported schema type");
    }
    validateSchemaAnnotations(parsed, path);
    return parsed;
  } finally {
    state.active.delete(raw);
  }
}

function parseObjectSchema(
  raw: Record<string, unknown>,
  path: string,
  depth: number,
  state: ParseState,
  annotations: SchemaAnnotations,
): OpenEvoObjectSchema {
  if (raw.additionalProperties !== false) {
    fail(`${path}.additionalProperties`, "must be present and false");
  }
  if (!Object.hasOwn(raw, "properties")) {
    fail(`${path}.properties`, "must be present for closed objects");
  }
  const propertiesRaw = expectRecord(raw.properties, `${path}.properties`, "must be an object");
  if (Reflect.ownKeys(propertiesRaw).some((key) => typeof key !== "string")) {
    fail(`${path}.properties`, "property names must be strings");
  }
  const entries = Object.entries(propertiesRaw);
  if (entries.length > MAX_OBJECT_PROPERTIES) {
    fail(`${path}.properties`, `exceeds maximum property count ${MAX_OBJECT_PROPERTIES}`);
  }
  const propertyEntries: [string, OpenEvoConfigSchema][] = [];
  for (const [name, child] of entries) {
    if (codePointLength(name) > MAX_STRING_LENGTH) fail(`${path}.properties`, "property name is too long");
    const childPath = `${path}.properties.${name}`;
    const parsedChild = parseSchema(child, childPath, depth + 1, state);
    enforceSensitiveProperty(name, parsedChild, childPath);
    propertyEntries.push([name, parsedChild]);
  }
  const properties = Object.fromEntries(propertyEntries);

  const requiredRaw = raw.required ?? [];
  if (!Array.isArray(requiredRaw)) fail(`${path}.required`, "must be an array");
  if (requiredRaw.length > MAX_OBJECT_PROPERTIES) fail(`${path}.required`, "contains too many entries");
  const required: string[] = [];
  for (let index = 0; index < requiredRaw.length; index += 1) {
    const name = requiredRaw[index];
    if (typeof name !== "string") fail(`${path}.required[${index}]`, "must be a property name");
    if (!Object.hasOwn(properties, name)) fail(`${path}.required[${index}]`, "must name a declared property");
    if (required.includes(name)) fail(`${path}.required[${index}]`, "must not duplicate a property name");
    required.push(name);
  }
  return {
    kind: "object",
    type: "object",
    properties,
    required,
    additionalProperties: false,
    ...annotations,
  };
}

function parseArraySchema(
  raw: Record<string, unknown>,
  path: string,
  depth: number,
  state: ParseState,
  annotations: SchemaAnnotations,
): OpenEvoArraySchema {
  if (!Object.hasOwn(raw, "items")) fail(`${path}.items`, "must be present for bounded arrays");
  const minItems = parseSizeBound(raw, path, "minItems", MAX_ARRAY_ITEMS);
  const maxItems = parseSizeBound(raw, path, "maxItems", MAX_ARRAY_ITEMS);
  if (minItems !== undefined && maxItems !== undefined && minItems > maxItems) {
    fail(`${path}.minItems`, "must not exceed maxItems");
  }
  return {
    kind: "array",
    type: "array",
    items: parseSchema(raw.items, `${path}.items`, depth + 1, state),
    ...(minItems === undefined ? {} : { minItems }),
    ...(maxItems === undefined ? {} : { maxItems }),
    ...annotations,
  };
}

function parseStringSchema(
  raw: Record<string, unknown>,
  path: string,
  annotations: SchemaAnnotations,
): OpenEvoStringSchema {
  const minLength = parseSizeBound(raw, path, "minLength", MAX_STRING_LENGTH);
  const maxLength = parseSizeBound(raw, path, "maxLength", MAX_STRING_LENGTH);
  if (minLength !== undefined && maxLength !== undefined && minLength > maxLength) {
    fail(`${path}.minLength`, "must not exceed maxLength");
  }
  if (Object.hasOwn(raw, "x-openevo-secret-ref") && raw["x-openevo-secret-ref"] !== true) {
    fail(`${path}.x-openevo-secret-ref`, "must be true when present");
  }
  const secretRef = raw["x-openevo-secret-ref"] === true;
  return {
    kind: "string",
    type: "string",
    secretRef,
    ...(secretRef ? { "x-openevo-secret-ref": true as const } : {}),
    ...(minLength === undefined ? {} : { minLength }),
    ...(maxLength === undefined ? {} : { maxLength }),
    ...annotations,
  };
}

function parseNumericSchema(
  raw: Record<string, unknown>,
  path: string,
  annotations: SchemaAnnotations,
  type: "number" | "integer",
): OpenEvoNumberSchema | OpenEvoIntegerSchema {
  const bounds: Record<string, number> = {};
  for (const keyword of ["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"] as const) {
    if (Object.hasOwn(raw, keyword)) bounds[keyword] = expectFiniteNumber(raw[keyword], `${path}.${keyword}`);
  }
  const lowers = [bounds.minimum, bounds.exclusiveMinimum].filter((value): value is number => value !== undefined);
  const uppers = [bounds.maximum, bounds.exclusiveMaximum].filter((value): value is number => value !== undefined);
  if (lowers.length > 0 && uppers.length > 0) {
    const lower = Math.max(...lowers);
    const upper = Math.min(...uppers);
    const lowerExclusive = bounds.exclusiveMinimum === lower;
    const upperExclusive = bounds.exclusiveMaximum === upper;
    if (lower > upper || (lower === upper && (lowerExclusive || upperExclusive))) {
      fail(path, "numeric bounds define an empty range");
    }
  }
  return { kind: type, type, ...bounds, ...annotations } as OpenEvoNumberSchema | OpenEvoIntegerSchema;
}

function parseAnnotations(raw: Record<string, unknown>, path: string): SchemaAnnotations {
  const annotations: {
    title?: string;
    description?: string;
    enum?: readonly OpenEvoJsonValue[];
    const?: OpenEvoJsonValue;
    default?: OpenEvoJsonValue;
  } = {};
  for (const keyword of ["title", "description"] as const) {
    if (!Object.hasOwn(raw, keyword)) continue;
    if (typeof raw[keyword] !== "string") fail(`${path}.${keyword}`, "must be a string");
    if (codePointLength(raw[keyword]) > MAX_STRING_LENGTH) {
      fail(`${path}.${keyword}`, `exceeds ${MAX_STRING_LENGTH} characters`);
    }
    annotations[keyword] = raw[keyword];
  }
  if (Object.hasOwn(raw, "enum")) {
    if (!Array.isArray(raw.enum) || raw.enum.length === 0) fail(`${path}.enum`, "must be a non-empty array");
    if (raw.enum.length > MAX_ENUM_VALUES) fail(`${path}.enum`, `exceeds maximum enum value count ${MAX_ENUM_VALUES}`);
    if (containsSensitiveValue(raw.enum)) {
      fail(`${path}.enum`, "must not contain sensitive field values");
    }
    annotations.enum = raw.enum.map((value, index) => checkedJsonValue(value, `${path}.enum[${index}]`));
  }
  if (Object.hasOwn(raw, "const")) {
    if (containsSensitiveValue(raw.const)) {
      fail(`${path}.const`, "must not contain sensitive field values");
    }
    annotations.const = checkedJsonValue(raw.const, `${path}.const`);
  }
  if (Object.hasOwn(raw, "default")) {
    if (containsSensitiveValue(raw.default)) {
      fail(`${path}.default`, "must not contain sensitive field defaults");
    }
    annotations.default = checkedJsonValue(raw.default, `${path}.default`);
  }
  return annotations;
}

function validateSchemaAnnotations(schema: OpenEvoConfigSchema, path: string): void {
  if (schema.enum) {
    schema.enum.forEach((value, index) => {
      const errors: EvolutionConfigValidationError[] = [];
      validateValue(schema, value, `${path}.enum[${index}]`, errors, { partial: false, applyDefaults: false });
      if (errors.length > 0) fail(`${path}.enum[${index}]`, "does not satisfy its schema");
    });
  }
  if (Object.hasOwn(schema, "const")) {
    const errors: EvolutionConfigValidationError[] = [];
    validateValue(schema, schema.const, `${path}.const`, errors, { partial: false, applyDefaults: false });
    if (errors.length > 0) fail(`${path}.const`, "does not satisfy its schema");
  }
  if (Object.hasOwn(schema, "default")) {
    const errors: EvolutionConfigValidationError[] = [];
    validateValue(schema, schema.default, `${path}.default`, errors, { partial: false, applyDefaults: true });
    if (errors.length > 0) fail(`${path}.default`, "does not satisfy its schema");
  }
}

interface ValueOptions {
  partial: boolean;
  applyDefaults: boolean;
}

function validateValue(
  schema: OpenEvoConfigSchema,
  value: unknown,
  path: string,
  errors: EvolutionConfigValidationError[],
  options: ValueOptions,
): void {
  if (schema.kind === "nullable") {
    if (value !== null) validateValue(schema.valueSchema, value, path, errors, options);
  } else {
    validateTypedValue(schema, value, path, errors, options);
  }
  const comparable = options.applyDefaults
    ? valueWithSchemaDefaults(schema, value)
    : value;
  if (schema.enum && !schema.enum.some((allowed) => jsonEqual(comparable, allowed))) {
    addError(errors, path, "must match an allowed enum value");
  }
  if (Object.hasOwn(schema, "const") && !jsonEqual(comparable, schema.const)) {
    addError(errors, path, "must match the constant value");
  }
}

function validateTypedValue(
  schema: OpenEvoNonNullableSchema,
  value: unknown,
  path: string,
  errors: EvolutionConfigValidationError[],
  options: ValueOptions,
): void {
  switch (schema.kind) {
    case "object": {
      if (!isRecord(value)) return addError(errors, path, "must be an object");
      if (Reflect.ownKeys(value).some((key) => typeof key !== "string")) {
        addError(errors, path, "object property names must be strings");
      }
      const entries = Object.entries(value);
      if (entries.length > MAX_OBJECT_PROPERTIES) addError(errors, path, `object exceeds ${MAX_OBJECT_PROPERTIES} properties`);
      for (const [name] of entries) {
        if (codePointLength(name) > MAX_STRING_LENGTH) addError(errors, path, `property name exceeds ${MAX_STRING_LENGTH} characters`);
        if (!Object.hasOwn(schema.properties, name)) addError(errors, `${path}.${name}`, "unknown property");
      }
      for (const [name, childSchema] of Object.entries(schema.properties)) {
        if (Object.hasOwn(value, name)) {
          validateValue(childSchema, value[name], `${path}.${name}`, errors, options);
        } else if (options.applyDefaults && Object.hasOwn(childSchema, "default")) {
          validateValue(childSchema, childSchema.default, `${path}.${name}`, errors, options);
        } else if (!options.partial && schema.required.includes(name)) {
          addError(errors, `${path}.${name}`, "required property is missing");
        }
      }
      return;
    }
    case "array":
      if (!Array.isArray(value)) return addError(errors, path, "must be an array");
      if (value.length > MAX_ARRAY_ITEMS) addError(errors, path, `array exceeds ${MAX_ARRAY_ITEMS} items`);
      if (value.length < (schema.minItems ?? 0)) addError(errors, path, `must contain at least ${schema.minItems} items`);
      if (value.length > (schema.maxItems ?? MAX_ARRAY_ITEMS)) addError(errors, path, `must contain at most ${schema.maxItems} items`);
      value.forEach((item, index) => validateValue(schema.items, item, `${path}[${index}]`, errors, options));
      return;
    case "string":
      if (typeof value !== "string") return addError(errors, path, "must be a string");
      if (codePointLength(value) > MAX_STRING_LENGTH) addError(errors, path, `string exceeds ${MAX_STRING_LENGTH} characters`);
      if (codePointLength(value) < (schema.minLength ?? 0)) addError(errors, path, `must contain at least ${schema.minLength} characters`);
      if (codePointLength(value) > (schema.maxLength ?? MAX_STRING_LENGTH)) addError(errors, path, `must contain at most ${schema.maxLength} characters`);
      if (schema.secretRef && !isOpenEvoSecretReference(value)) addError(errors, path, "must be an opaque OpenEvo secret reference");
      return;
    case "boolean":
      if (typeof value !== "boolean") addError(errors, path, "must be a boolean");
      return;
    case "integer":
      if (typeof value !== "number" || !Number.isInteger(value)) return addError(errors, path, "must be an integer, not a boolean");
      if (!Number.isSafeInteger(value)) return addError(errors, path, "integer must be within the JavaScript safe integer range");
      validateNumericBounds(schema, value, path, errors);
      return;
    case "number":
      if (typeof value !== "number" || !Number.isFinite(value)) return addError(errors, path, "must be a finite number, not a boolean");
      if (Number.isInteger(value) && !Number.isSafeInteger(value)) return addError(errors, path, "integer must be within the JavaScript safe integer range");
      validateNumericBounds(schema, value, path, errors);
  }
}

function validateNumericBounds(
  schema: OpenEvoNumberSchema | OpenEvoIntegerSchema,
  value: number,
  path: string,
  errors: EvolutionConfigValidationError[],
): void {
  if (schema.minimum !== undefined && value < schema.minimum) addError(errors, path, `must be at least ${schema.minimum}`);
  if (schema.maximum !== undefined && value > schema.maximum) addError(errors, path, `must be at most ${schema.maximum}`);
  if (schema.exclusiveMinimum !== undefined && value <= schema.exclusiveMinimum) addError(errors, path, `must be greater than ${schema.exclusiveMinimum}`);
  if (schema.exclusiveMaximum !== undefined && value >= schema.exclusiveMaximum) addError(errors, path, `must be less than ${schema.exclusiveMaximum}`);
}

function mergeJsonValue(
  base: OpenEvoJsonValue,
  override: OpenEvoJsonValue,
): OpenEvoJsonValue {
  if (isRecord(base) && isRecord(override)) {
    const merged = new Map<string, OpenEvoJsonValue>();
    for (const [key, value] of Object.entries(base)) {
      merged.set(key, cloneJsonValue(value as OpenEvoJsonValue));
    }
    for (const [key, value] of Object.entries(override)) {
      const existing = merged.get(key);
      merged.set(
        key,
        existing === undefined
          ? cloneJsonValue(value as OpenEvoJsonValue)
          : mergeJsonValue(existing, value as OpenEvoJsonValue),
      );
    }
    return Object.fromEntries(merged);
  }
  return cloneJsonValue(override);
}

function cloneJsonValue(value: OpenEvoJsonValue): OpenEvoJsonValue {
  if (Array.isArray(value)) {
    return value.map(cloneJsonValue);
  }
  if (isRecord(value)) {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        key,
        cloneJsonValue(item as OpenEvoJsonValue),
      ]),
    );
  }
  return value;
}

function valueWithSchemaDefaults(
  schema: OpenEvoConfigSchema,
  value: unknown,
): unknown {
  if (schema.kind === "nullable") {
    return value === null
      ? null
      : valueWithSchemaDefaults(schema.valueSchema, value);
  }
  if (schema.kind === "object" && isRecord(value)) {
    const entries: Array<[string, unknown]> = [];
    for (const [name, childSchema] of Object.entries(schema.properties)) {
      if (Object.hasOwn(value, name)) {
        entries.push([
          name,
          valueWithSchemaDefaults(childSchema, value[name]),
        ]);
      } else if (Object.hasOwn(childSchema, "default")) {
        entries.push([
          name,
          valueWithSchemaDefaults(childSchema, childSchema.default),
        ]);
      }
    }
    return Object.fromEntries(entries);
  }
  if (schema.kind === "array" && Array.isArray(value)) {
    return value.map((item) => valueWithSchemaDefaults(schema.items, item));
  }
  return value;
}

function checkedJsonValue(value: unknown, path: string, active = new Set<object>()): OpenEvoJsonValue {
  if (value === null || typeof value === "boolean") return value;
  if (typeof value === "string") {
    if (codePointLength(value) > MAX_STRING_LENGTH) fail(path, `string exceeds ${MAX_STRING_LENGTH} characters`);
    return value;
  }
  if (typeof value === "number") {
    expectFiniteNumber(value, path);
    return value;
  }
  if (Array.isArray(value)) {
    if (value.length > MAX_ARRAY_ITEMS) fail(path, `array exceeds ${MAX_ARRAY_ITEMS} items`);
    if (active.has(value)) fail(path, "recursive values are forbidden");
    active.add(value);
    try {
      return value.map((item, index) => checkedJsonValue(item, `${path}[${index}]`, active));
    } finally {
      active.delete(value);
    }
  }
  if (isRecord(value)) {
    const entries = Object.entries(value);
    if (entries.length > MAX_OBJECT_PROPERTIES) fail(path, `object exceeds ${MAX_OBJECT_PROPERTIES} properties`);
    if (active.has(value)) fail(path, "recursive values are forbidden");
    active.add(value);
    try {
      return Object.fromEntries(entries.map(([key, item]) => {
        if (codePointLength(key) > MAX_STRING_LENGTH) fail(path, `property name exceeds ${MAX_STRING_LENGTH} characters`);
        return [key, checkedJsonValue(item, `${path}.${key}`, active)];
      }));
    } finally {
      active.delete(value);
    }
  }
  fail(path, "value must use JSON-compatible types");
}

function expectFiniteNumber(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) fail(path, "number must be finite, not a boolean");
  if (Number.isInteger(value) && !Number.isSafeInteger(value)) {
    fail(path, "integer must be within the JavaScript safe integer range");
  }
  return value;
}

function parseSizeBound(
  raw: Record<string, unknown>,
  path: string,
  keyword: string,
  maximum: number,
): number | undefined {
  if (!Object.hasOwn(raw, keyword)) return undefined;
  const value = raw[keyword];
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
    fail(`${path}.${keyword}`, "must be a non-negative safe integer");
  }
  if (value > maximum) fail(`${path}.${keyword}`, `must not exceed ${maximum}`);
  return value;
}

function enforceSensitiveProperty(name: string, schema: OpenEvoConfigSchema, path: string): void {
  if (!isSensitiveName(name) && !(schema.kind === "string" && schema.secretRef)) return;
  for (const keyword of ["default", "enum", "const"] as const) {
    if (Object.hasOwn(schema, keyword)) fail(`${path}.${keyword}`, "sensitive properties must not embed values");
  }
  if (!isSensitiveName(name) || !name.toLowerCase().endsWith("_ref")) {
    fail(path, "sensitive properties must be opaque *_ref fields");
  }
  if (schema.kind !== "string") fail(`${path}.type`, "secret references must be strings");
  if (!schema.secretRef) fail(`${path}.x-openevo-secret-ref`, "secret reference fields must opt into Core validation");
}

function isSensitiveName(name: string): boolean {
  const snake = name.replace(/([a-z0-9])([A-Z])/g, "$1_$2").toLowerCase();
  const parts = snake.split(/[^a-z0-9]+/).filter(Boolean);
  const values = new Set(parts);
  if (["secret", "password", "credential", "authorization"].some((part) => values.has(part))) return true;
  if (values.has("apikey") || (values.has("api") && values.has("key"))) return true;
  if (values.has("token") && (parts.length === 1 || values.has("ref") || ["access", "auth", "bearer", "refresh", "session"].some((part) => values.has(part)))) return true;
  return values.has("key") && ["access", "private", "secret", "client", "signing", "encryption", "ssh"].some((part) => values.has(part));
}

function containsSensitiveValue(value: unknown, active = new Set<object>()): boolean {
  if (Array.isArray(value)) {
    if (active.has(value)) return false;
    active.add(value);
    try {
      return value.some((item) => containsSensitiveValue(item, active));
    } finally {
      active.delete(value);
    }
  }
  if (!isRecord(value)) return false;
  if (active.has(value)) return false;
  active.add(value);
  try {
    return Object.entries(value).some(
      ([key, item]) => isSensitiveName(key) || containsSensitiveValue(item, active),
    );
  } finally {
    active.delete(value);
  }
}

function rejectKeywords(raw: Record<string, unknown>, allowed: ReadonlySet<string>, path: string): void {
  for (const keyword of Object.keys(raw)) {
    if (!allowed.has(keyword)) fail(`${path}.${keyword}`, "unsupported schema keyword");
  }
}

function unionSets(left: ReadonlySet<string>, right: ReadonlySet<string>): Set<string> {
  return new Set([...left, ...right]);
}

function expectRecord(value: unknown, path: string, message: string): Record<string, unknown> {
  if (!isRecord(value)) fail(path, message);
  return value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function isExactNullSchema(value: unknown): boolean {
  return isRecord(value) && Object.keys(value).length === 1 && value.type === "null";
}

function jsonEqual(left: unknown, right: unknown): boolean {
  if (typeof left === "number" && typeof right === "number") return left === right;
  if (typeof left !== typeof right || left === null || right === null) return left === right;
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left) && Array.isArray(right) && left.length === right.length && left.every((item, index) => jsonEqual(item, right[index]));
  }
  if (isRecord(left) && isRecord(right)) {
    const leftKeys = Object.keys(left);
    const rightKeys = Object.keys(right);
    return leftKeys.length === rightKeys.length && leftKeys.every((key) => Object.hasOwn(right, key) && jsonEqual(left[key], right[key]));
  }
  return left === right;
}

function addError(errors: EvolutionConfigValidationError[], path: string, message: string): void {
  errors.push({ path, message });
}

function codePointLength(value: string): number {
  return Array.from(value).length;
}

function fail(path: string, message: string): never {
  throw new EvolutionConfigSchemaError(path, message);
}
