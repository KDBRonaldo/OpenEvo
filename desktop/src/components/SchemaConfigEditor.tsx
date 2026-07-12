import { Plus, Trash2, X } from "lucide-react";
import { useEffect, useMemo, type CSSProperties, type ReactNode } from "react";
import {
  type EvolutionConfigValidationError,
  type EvolutionConfigValidationResult,
  MAX_ARRAY_ITEMS,
  type OpenEvoArraySchema,
  type OpenEvoConfigSchema,
  type OpenEvoJsonObject,
  type OpenEvoJsonValue,
  type OpenEvoNonNullableSchema,
  type OpenEvoObjectSchema,
  effectiveEvolutionConfig,
  validateEvolutionConfig,
  validateEvolutionConfigOverride,
} from "../api/evolutionConfigSchema";

export interface SchemaConfigEditorProps {
  schema: OpenEvoObjectSchema;
  value: OpenEvoJsonObject;
  defaultValue?: OpenEvoJsonObject;
  onChange: (value: OpenEvoJsonObject) => void;
  onValidationChange?: (result: EvolutionConfigValidationResult) => void;
  disabled?: boolean;
  className?: string;
}

export function SchemaConfigEditor({
  schema,
  value,
  defaultValue,
  onChange,
  onValidationChange,
  disabled = false,
  className,
}: SchemaConfigEditorProps) {
  const validation = useMemo(
    () =>
      defaultValue
        ? validateEvolutionConfigOverride(schema, defaultValue, value)
        : validateEvolutionConfig(schema, value),
    [defaultValue, schema, value],
  );
  const effectiveValue = useMemo(
    () =>
      defaultValue
        ? effectiveEvolutionConfig(schema, defaultValue, value)
        : value,
    [defaultValue, schema, value],
  );

  useEffect(() => {
    onValidationChange?.(validation);
  }, [onValidationChange, validation]);

  return (
    <div
      className={className}
      data-valid={validation.valid ? "true" : "false"}
      style={editorStyle}
    >
      {Object.entries(schema.properties).map(([name, childSchema]) => (
        <SchemaField
          key={name}
          schema={childSchema}
          value={effectiveValue[name]}
          present={Object.hasOwn(effectiveValue, name)}
          overrideValue={value[name]}
          overridden={Object.hasOwn(value, name)}
          path={`config.${name}`}
          label={childSchema.title || humanize(name)}
          errors={validation.errors}
          disabled={disabled}
          onChange={(next) => onChange(withProperty(value, name, next))}
        />
      ))}
    </div>
  );
}

interface SchemaFieldProps {
  schema: OpenEvoConfigSchema;
  value: OpenEvoJsonValue | undefined;
  present: boolean;
  overrideValue: OpenEvoJsonValue | undefined;
  overridden: boolean;
  path: string;
  label: string;
  errors: readonly EvolutionConfigValidationError[];
  disabled: boolean;
  onChange: (value: OpenEvoJsonValue | undefined) => void;
  arrayIndex?: number;
}

function SchemaField(props: SchemaFieldProps) {
  const { schema, present, overridden, value, label, disabled, onChange } = props;
  if (schema.kind === "nullable") {
    return (
      <FieldFrame {...props}>
        <div style={toggleRowStyle}>
          <label style={checkboxLabelStyle}>
            <input
              type="checkbox"
              aria-label={`Include ${label}`}
              checked={present}
              disabled={disabled || (present && !overridden)}
              onChange={(event) =>
                onChange(event.currentTarget.checked ? initialValue(schema.valueSchema) : undefined)
              }
            />
            Include
          </label>
          <label style={checkboxLabelStyle}>
            <input
              type="checkbox"
              aria-label={`Use null for ${label}`}
              checked={present && value === null}
              disabled={disabled || !present}
              onChange={(event) =>
                onChange(event.currentTarget.checked ? null : initialValue(schema.valueSchema))
              }
            />
            Null
          </label>
        </div>
        {present && value !== null ? (
          <FieldControl
            {...props}
            schema={schema.valueSchema}
            present
            value={value}
            showFrame={false}
          />
        ) : null}
      </FieldFrame>
    );
  }
  return <FieldControl {...props} schema={schema} showFrame />;
}

function FieldControl({
  schema,
  value,
  present,
  overrideValue,
  overridden,
  path,
  label,
  errors,
  disabled,
  onChange,
  showFrame,
  arrayIndex,
}: SchemaFieldProps & { schema: OpenEvoNonNullableSchema; showFrame: boolean }) {
  const content = renderControl({
    schema,
    value,
    present,
    overrideValue,
    overridden,
    path,
    label,
    errors,
    disabled,
    onChange,
    arrayIndex,
  });
  return showFrame ? (
    <FieldFrame
      schema={schema}
      value={value}
      present={present}
      overrideValue={overrideValue}
      overridden={overridden}
      path={path}
      label={label}
      errors={errors}
      disabled={disabled}
      onChange={onChange}
      arrayIndex={arrayIndex}
    >
      {content}
    </FieldFrame>
  ) : (
    content
  );
}

function renderControl(props: Omit<SchemaFieldProps, "schema"> & { schema: OpenEvoNonNullableSchema }): ReactNode {
  const { schema, value, present, path, label, errors, disabled, onChange } = props;
  const invalid = errors.some((error) => error.path === path || error.path.startsWith(`${path}[`) || error.path.startsWith(`${path}.`));
  const errorId = fieldErrorId(path);

  if (Object.hasOwn(schema, "const")) {
    return present ? (
      <output aria-label={label} style={constantStyle}>
        {formatOption(schema.const as OpenEvoJsonValue)}
      </output>
    ) : (
      <button
        type="button"
        aria-label={`Set ${label}`}
        disabled={disabled}
        onClick={() => onChange(schema.const as OpenEvoJsonValue)}
        style={commandButtonStyle}
      >
        Set value
      </button>
    );
  }

  if (schema.enum) {
    const selectedIndex = present
      ? schema.enum.findIndex((candidate) => jsonEqual(candidate, value))
      : -1;
    return (
      <select
        aria-label={label}
        aria-invalid={invalid}
        aria-describedby={invalid ? errorId : undefined}
        value={selectedIndex < 0 ? "" : String(selectedIndex)}
        disabled={disabled}
        onChange={(event) => {
          const index = Number(event.currentTarget.value);
          onChange(event.currentTarget.value === "" ? undefined : schema.enum?.[index]);
        }}
        style={inputStyle}
      >
        <option value="">Not set</option>
        {schema.enum.map((option, index) => (
          <option key={index} value={index}>
            {formatOption(option)}
          </option>
        ))}
      </select>
    );
  }

  switch (schema.kind) {
    case "boolean":
      return (
        <input
          type="checkbox"
          aria-label={label}
          aria-invalid={invalid}
          aria-describedby={invalid ? errorId : undefined}
          checked={present && value === true}
          disabled={disabled}
          onChange={(event) => onChange(event.currentTarget.checked)}
        />
      );
    case "number":
    case "integer":
      return (
        <input
          type="number"
          aria-label={label}
          aria-invalid={invalid}
          aria-describedby={invalid ? errorId : undefined}
          value={typeof value === "number" && Number.isFinite(value) ? value : ""}
          min={schema.minimum}
          max={schema.maximum}
          step={schema.kind === "integer" ? 1 : "any"}
          disabled={disabled}
          onChange={(event) => {
            const raw = event.currentTarget.value;
            onChange(raw === "" ? undefined : Number(raw));
          }}
          style={inputStyle}
        />
      );
    case "string": {
      const common = {
        "aria-label": label,
        "aria-invalid": invalid,
        "aria-describedby": invalid ? errorId : undefined,
        value: typeof value === "string" ? value : "",
        disabled,
        onChange: (event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
          onChange(event.currentTarget.value),
        style: inputStyle,
      };
      if (schema.secretRef) {
        return (
          <input
            {...common}
            type="password"
            autoComplete="off"
            spellCheck={false}
          />
        );
      }
      if (schema.description || (schema.maxLength ?? 0) > 160) {
        return <textarea {...common} rows={3} />;
      }
      return <input {...common} type="text" />;
    }
    case "array":
      return <ArrayControl {...props} schema={schema} />;
    case "object":
      return <ObjectControl {...props} schema={schema} />;
  }
}

function ObjectControl({
  schema,
  value,
  overrideValue,
  present,
  label,
  path,
  errors,
  disabled,
  onChange,
}: Omit<SchemaFieldProps, "schema"> & { schema: OpenEvoObjectSchema }) {
  const objectValue = isJsonObject(value) ? value : {};
  const overrideObject = isJsonObject(overrideValue) ? overrideValue : {};
  return (
    <div style={nestedStyle}>
      {!present ? (
        <button
          type="button"
          aria-label={`Set ${label} to empty object`}
          disabled={disabled}
          onClick={() => onChange({})}
          style={commandButtonStyle}
        >
          Set empty object
        </button>
      ) : null}
      {Object.entries(schema.properties).map(([name, childSchema]) => (
        <SchemaField
          key={name}
          schema={childSchema}
          value={objectValue[name]}
          present={Object.hasOwn(objectValue, name)}
          overrideValue={overrideObject[name]}
          overridden={Object.hasOwn(overrideObject, name)}
          path={`${path}.${name}`}
          label={childSchema.title || humanize(name)}
          errors={errors}
          disabled={disabled}
          onChange={(next) => onChange(withProperty(overrideObject, name, next))}
        />
      ))}
    </div>
  );
}

function ArrayControl({
  schema,
  value,
  present,
  path,
  label,
  errors,
  disabled,
  onChange,
}: Omit<SchemaFieldProps, "schema"> & { schema: OpenEvoArraySchema }) {
  const items = Array.isArray(value) ? value : [];
  const canAdd = items.length < (schema.maxItems ?? MAX_ARRAY_ITEMS);
  return (
    <div style={arrayStyle}>
      {!present ? (
        <button
          type="button"
          aria-label={`Set ${label} to empty array`}
          disabled={disabled}
          onClick={() => onChange([])}
          style={commandButtonStyle}
        >
          Set empty array
        </button>
      ) : null}
      {items.map((item, index) => (
        <div key={index} style={arrayItemStyle}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <SchemaField
              schema={schema.items}
              value={item}
              present
              overrideValue={item}
              overridden
              path={`${path}[${index}]`}
              label={`${schema.items.title || singularize(label)} ${index + 1}`}
              errors={errors}
              disabled={disabled}
              onChange={(next) => {
                if (next === undefined) return;
                const updated = [...items];
                updated[index] = next;
                onChange(updated);
              }}
              arrayIndex={index}
            />
          </div>
          <IconButton
            label={`Remove ${label} item ${index + 1}`}
            disabled={disabled || items.length <= (schema.minItems ?? 0)}
            onClick={() => onChange(items.filter((_, itemIndex) => itemIndex !== index))}
          >
            <Trash2 size={16} aria-hidden="true" />
          </IconButton>
        </div>
      ))}
      <button
        type="button"
        aria-label={`Add ${label} item`}
        disabled={disabled || !canAdd}
        onClick={() => onChange([...items, initialValue(schema.items)])}
        style={commandButtonStyle}
      >
        <Plus size={16} aria-hidden="true" />
        Add item
      </button>
    </div>
  );
}

function FieldFrame({
  schema,
  overridden,
  path,
  label,
  errors,
  disabled,
  onChange,
  children,
}: SchemaFieldProps & { children: ReactNode }) {
  const ownErrors = errors.filter(
    (error) => error.path === path || error.path.startsWith(`${path}[`),
  );
  const errorId = fieldErrorId(path);
  return (
    <div data-config-path={path} style={fieldStyle}>
      <div style={labelRowStyle}>
        <span style={labelStyle}>{label}</span>
        {overridden ? (
          <IconButton
            label={`Clear ${label}`}
            disabled={disabled}
            onClick={() => onChange(undefined)}
          >
            <X size={15} aria-hidden="true" />
          </IconButton>
        ) : null}
      </div>
      {schema.description ? <span style={descriptionStyle}>{schema.description}</span> : null}
      {children}
      {ownErrors.length > 0 ? (
        <div id={errorId} role="alert" style={errorStyle}>
          {ownErrors.map((error, index) => (
            <div key={`${error.path}-${index}`}>{error.message}</div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function IconButton({
  label,
  disabled,
  onClick,
  children,
}: {
  label: string;
  disabled: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onClick}
      style={iconButtonStyle}
    >
      {children}
    </button>
  );
}

function withProperty(
  object: OpenEvoJsonObject,
  name: string,
  value: OpenEvoJsonValue | undefined,
): OpenEvoJsonObject {
  if (value === undefined) {
    const { [name]: _removed, ...rest } = object;
    return rest;
  }
  return { ...object, [name]: value };
}

function initialValue(schema: OpenEvoConfigSchema): OpenEvoJsonValue {
  if (Object.hasOwn(schema, "const")) return schema.const as OpenEvoJsonValue;
  if (Object.hasOwn(schema, "default")) return schema.default as OpenEvoJsonValue;
  if (schema.enum?.length) return schema.enum[0];
  switch (schema.kind) {
    case "nullable":
      return null;
    case "object":
      return {};
    case "array":
      return [];
    case "string":
      return "";
    case "boolean":
      return false;
    case "integer":
    case "number":
      return 0;
  }
}

function isJsonObject(value: OpenEvoJsonValue | undefined): value is OpenEvoJsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function formatOption(value: OpenEvoJsonValue): string {
  if (typeof value === "string") return value;
  if (value === null) return "Null";
  return JSON.stringify(value);
}

function jsonEqual(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function humanize(name: string): string {
  const text = name.replace(/[_-]+/g, " ").trim();
  return text ? text[0].toUpperCase() + text.slice(1) : "Value";
}

function singularize(label: string): string {
  return label.endsWith("s") && label.length > 1 ? label.slice(0, -1) : `${label} item`;
}

function fieldErrorId(path: string): string {
  return `schema-config-error-${path.replace(/[^A-Za-z0-9_-]/g, "-")}`;
}

const editorStyle: CSSProperties = { display: "grid", gap: 12, width: "100%" };
const fieldStyle: CSSProperties = { display: "grid", gap: 6, minWidth: 0 };
const nestedStyle: CSSProperties = {
  display: "grid",
  gap: 12,
  borderLeft: "2px solid var(--border, #d8dde5)",
  paddingLeft: 12,
};
const arrayStyle: CSSProperties = { display: "grid", gap: 8 };
const arrayItemStyle: CSSProperties = {
  display: "flex",
  alignItems: "start",
  gap: 8,
  minWidth: 0,
};
const labelRowStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  minHeight: 28,
  gap: 8,
};
const labelStyle: CSSProperties = { fontSize: 13, fontWeight: 600 };
const descriptionStyle: CSSProperties = { fontSize: 12, color: "var(--muted, #5f6875)" };
const toggleRowStyle: CSSProperties = { display: "flex", alignItems: "center", gap: 16 };
const checkboxLabelStyle: CSSProperties = { display: "inline-flex", alignItems: "center", gap: 6, fontSize: 13 };
const inputStyle: CSSProperties = {
  boxSizing: "border-box",
  width: "100%",
  minHeight: 34,
  border: "1px solid var(--border, #c9d0da)",
  borderRadius: 6,
  padding: "6px 8px",
  background: "var(--panel, #fff)",
  color: "inherit",
  font: "inherit",
  letterSpacing: 0,
};
const iconButtonStyle: CSSProperties = {
  display: "inline-grid",
  placeItems: "center",
  flex: "0 0 30px",
  width: 30,
  height: 30,
  border: "1px solid var(--border, #c9d0da)",
  borderRadius: 6,
  background: "transparent",
  color: "inherit",
};
const commandButtonStyle: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  gap: 6,
  width: "fit-content",
  minHeight: 32,
  border: "1px solid var(--border, #c9d0da)",
  borderRadius: 6,
  padding: "5px 9px",
  background: "transparent",
  color: "inherit",
};
const errorStyle: CSSProperties = { color: "var(--danger, #b42318)", fontSize: 12 };
const constantStyle: CSSProperties = {
  boxSizing: "border-box",
  width: "100%",
  minHeight: 34,
  border: "1px solid var(--border, #c9d0da)",
  borderRadius: 6,
  padding: "6px 8px",
  background: "var(--subtle, #f5f7fa)",
  overflowWrap: "anywhere",
};
