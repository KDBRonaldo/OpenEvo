import { useMemo } from "react";
import {
  effectiveEvolutionConfig,
  EvolutionConfigSchemaError,
  parseEvolutionConfigSchema,
  validateEvolutionConfigOverride,
  type OpenEvoConfigSchema,
  type OpenEvoJsonObject,
  type OpenEvoJsonValue,
  type OpenEvoObjectSchema,
} from "../api/evolutionConfigSchema";

interface MethodConfigEditorProps {
  readonly schema: OpenEvoJsonObject;
  readonly defaultConfig: OpenEvoJsonObject;
  readonly value: OpenEvoJsonObject;
  readonly disabled: boolean;
  readonly onChange: (value: OpenEvoJsonObject) => void;
}

export function MethodConfigEditor({ schema: rawSchema, defaultConfig, value, disabled, onChange }: MethodConfigEditorProps) {
  const parsed = useMemo(() => parseMethodConfigSchema(rawSchema), [rawSchema]);
  if (!parsed.schema) {
    return <p className="form-error" role="alert">{parsed.error}</p>;
  }
  if (Object.keys(parsed.schema.properties).length === 0) return null;
  const effectiveValue = effectiveEvolutionConfig(parsed.schema, defaultConfig, value);
  return (
    <fieldset className="method-config" disabled={disabled}>
      <legend>Method configuration</legend>
      <ObjectFields schema={parsed.schema} effectiveValue={effectiveValue} overrideValue={value} path="config" onChange={onChange} />
    </fieldset>
  );
}

export function methodConfigErrors(
  rawSchema: OpenEvoJsonObject,
  defaultConfig: OpenEvoJsonObject,
  value: OpenEvoJsonObject,
): readonly string[] {
  const parsed = parseMethodConfigSchema(rawSchema);
  if (!parsed.schema) return [parsed.error ?? "The remote method schema is unavailable."];
  return validateEvolutionConfigOverride(parsed.schema, defaultConfig, value).errors.map((error) => `${error.path}: ${error.message}`);
}

function parseMethodConfigSchema(rawSchema: OpenEvoJsonObject): { schema: OpenEvoObjectSchema | null; error: string | null } {
  try {
    return { schema: parseEvolutionConfigSchema(rawSchema), error: null };
  } catch (error) {
    const detail = error instanceof EvolutionConfigSchemaError ? error.message : "The remote method schema is invalid.";
    return { schema: null, error: `Method configuration cannot be edited: ${detail}` };
  }
}

function ObjectFields({
  schema,
  effectiveValue,
  overrideValue,
  path,
  onChange,
}: {
  schema: OpenEvoObjectSchema;
  effectiveValue: OpenEvoJsonObject;
  overrideValue: OpenEvoJsonObject;
  path: string;
  onChange: (value: OpenEvoJsonObject) => void;
}) {
  return (
    <div className="schema-object-fields">
      {Object.entries(schema.properties).map(([name, child]) => {
        const required = schema.required.includes(name);
        const effectivePresent = Object.hasOwn(effectiveValue, name);
        const overridePresent = Object.hasOwn(overrideValue, name);
        const label = child.title ?? humanize(name);
        const setValue = (next: OpenEvoJsonValue) => onChange({ ...overrideValue, [name]: next });
        const removeValue = () => {
          const next = { ...overrideValue };
          delete next[name];
          onChange(next);
        };
        return (
          <div className="schema-field" key={`${path}.${name}`}>
            {!required ? (
              <label className="schema-optional-toggle">
                <input
                  type="checkbox"
                  checked={overridePresent}
                  onChange={(event) => event.currentTarget.checked
                    ? setValue(effectivePresent ? structuredClone(effectiveValue[name]) : defaultValue(child))
                    : removeValue()}
                />
                <span>Configure {label}</span>
              </label>
            ) : null}
            {required || effectivePresent || overridePresent ? (
              <SchemaField
                schema={child}
                label={label}
                value={effectivePresent ? effectiveValue[name] : defaultValue(child)}
                overrideValue={overridePresent ? overrideValue[name] : undefined}
                path={`${path}.${name}`}
                onChange={setValue}
              />
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function SchemaField({
  schema,
  label,
  value,
  overrideValue,
  path,
  onChange,
}: {
  schema: OpenEvoConfigSchema;
  label: string;
  value: OpenEvoJsonValue | undefined;
  overrideValue: OpenEvoJsonValue | undefined;
  path: string;
  onChange: (value: OpenEvoJsonValue) => void;
}) {
  if (schema.kind === "nullable") {
    const isNull = value === null;
    return (
      <div className="schema-nullable">
        <label className="schema-optional-toggle">
          <input type="checkbox" checked={!isNull} onChange={(event) => onChange(event.currentTarget.checked ? defaultValue(schema.valueSchema) : null)} />
          <span>Set {label}</span>
        </label>
        {!isNull ? <SchemaField schema={schema.valueSchema} label={label} value={value} overrideValue={overrideValue} path={path} onChange={onChange} /> : null}
      </div>
    );
  }
  if (schema.const !== undefined) {
    return <label>{label}<input value={String(schema.const)} readOnly aria-readonly="true" /></label>;
  }
  if (schema.kind === "object") {
    const objectValue = isJsonObject(value) ? value : {};
    const objectOverride = isJsonObject(overrideValue) ? overrideValue : {};
    return (
      <fieldset className="schema-group">
        <legend>{label}</legend>
        {schema.description ? <p>{schema.description}</p> : null}
        <ObjectFields schema={schema} effectiveValue={objectValue} overrideValue={objectOverride} path={path} onChange={onChange} />
      </fieldset>
    );
  }
  if (schema.kind === "array") {
    const displayValue = Array.isArray(value) ? JSON.stringify(value) : typeof value === "string" ? value : "[]";
    return (
      <label>
        {label}
        <textarea
          rows={3}
          value={displayValue}
          aria-describedby={`${path}-help`}
          onChange={(event) => {
            try {
              const parsed: unknown = JSON.parse(event.currentTarget.value);
              onChange(Array.isArray(parsed) ? parsed as OpenEvoJsonValue[] : event.currentTarget.value);
            } catch {
              onChange(event.currentTarget.value);
            }
          }}
        />
        <small id={`${path}-help`}>JSON list{schema.minItems === undefined ? "" : `, at least ${schema.minItems} items`}{schema.maxItems === undefined ? "" : `, at most ${schema.maxItems} items`}.</small>
      </label>
    );
  }
  if (schema.kind === "boolean") {
    return (
      <label className="schema-checkbox">
        <input type="checkbox" checked={value === true} onChange={(event) => onChange(event.currentTarget.checked)} />
        <span>{label}</span>
      </label>
    );
  }
  if (schema.enum) {
    const selectedValue = schema.enum.find((option) => Object.is(option, value));
    return (
      <label>
        {label}
        <select value={enumOptionValue(selectedValue)} onChange={(event) => {
          const selected = schema.enum?.find((option) => enumOptionValue(option) === event.currentTarget.value);
          if (selected !== undefined) onChange(selected);
        }}>
          {schema.enum.map((option) => <option key={JSON.stringify(option)} value={enumOptionValue(option)}>{String(option)}</option>)}
        </select>
        {schema.description ? <small>{schema.description}</small> : null}
      </label>
    );
  }
  if (schema.kind === "string") {
    if (schema.secretRef) {
      return <p className="form-help">{label} is a secure reference and must be configured in the remote workspace.</p>;
    }
    return (
      <label>
        {label}
        <input
          value={typeof value === "string" ? value : ""}
          minLength={schema.minLength}
          maxLength={schema.maxLength}
          onChange={(event) => onChange(event.currentTarget.value)}
        />
        {schema.description ? <small>{schema.description}</small> : null}
      </label>
    );
  }
  const numericValue = typeof value === "number" || typeof value === "string" ? value : "";
  return (
    <label>
      {label}
      <input
        inputMode={schema.kind === "integer" ? "numeric" : "decimal"}
        value={numericValue}
        onChange={(event) => {
          const raw = event.currentTarget.value;
          const parsed = Number(raw);
          onChange(raw !== "" && Number.isFinite(parsed) ? parsed : raw);
        }}
      />
      <small>{numericHelp(schema)}</small>
    </label>
  );
}

function defaultValue(schema: OpenEvoConfigSchema): OpenEvoJsonValue {
  if (schema.default !== undefined) return structuredClone(schema.default);
  if (schema.const !== undefined) return structuredClone(schema.const);
  if (schema.kind === "nullable") return null;
  if (schema.kind === "object") {
    return Object.fromEntries(schema.required.map((name) => [name, defaultValue(schema.properties[name])]));
  }
  if (schema.kind === "array") return [];
  if (schema.kind === "boolean") return false;
  if (schema.kind === "number" || schema.kind === "integer") return schema.minimum ?? 0;
  return schema.enum?.[0] ?? "";
}

function numericHelp(schema: Extract<OpenEvoConfigSchema, { kind: "number" | "integer" }>): string {
  const kind = schema.kind === "integer" ? "Whole number" : "Number";
  const minimum = schema.minimum === undefined ? "" : `, minimum ${schema.minimum}`;
  const maximum = schema.maximum === undefined ? "" : `, maximum ${schema.maximum}`;
  return `${kind}${minimum}${maximum}.`;
}

function humanize(value: string): string {
  const words = value.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function enumOptionValue(value: OpenEvoJsonValue | undefined): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

function isJsonObject(value: OpenEvoJsonValue | undefined): value is OpenEvoJsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
