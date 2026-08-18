import type { DeploymentManifest, JsonValue } from "@/lib/api/schemas";

/** Deployment kinds the playground can invoke with a JSON payload. */
export const PLAYGROUND_KINDS = new Set(["function", "endpoint"]);

const PRIMITIVE_TYPES = ["string", "integer", "number", "boolean"] as const;
type PrimitiveType = (typeof PRIMITIVE_TYPES)[number];

export type PlaygroundField = {
  name: string;
  type: PrimitiveType;
  required: boolean;
  /** Serialized default shown as the input placeholder ("" when none). */
  defaultText: string;
};

function primitiveType(value: string | undefined): PrimitiveType | null {
  return (PRIMITIVE_TYPES as readonly string[]).includes(value ?? "")
    ? (value as PrimitiveType)
    : null;
}

/**
 * Typed per-field inputs are only offered when the recorded schema is a flat
 * object of primitives; anything richer (arrays, objects, files, unknown
 * shapes) falls back to the raw JSON editor instead of a fake form.
 *
 * The client contract (what `lazycloud client get` generates typed clients from)
 * is preferred; the SDK `inputs` Schema metadata is the fallback.
 */
export function playgroundFields(manifest: DeploymentManifest): PlaygroundField[] | null {
  const contract = manifest.client_contract;
  if (contract) {
    const parameters = contract.operation.parameters.filter(
      (parameter) => !["var_positional", "var_keyword"].includes(parameter.parameter_kind),
    );
    if (parameters.length === 0) return [];
    const fields: PlaygroundField[] = [];
    for (const parameter of parameters) {
      const rawType = parameter.json_schema.type;
      const type = primitiveType(typeof rawType === "string" ? rawType : undefined);
      if (!type) return null;
      fields.push({
        name: parameter.name,
        type,
        required: parameter.required,
        defaultText:
          parameter.default === null || parameter.default === undefined
            ? ""
            : JSON.stringify(parameter.default),
      });
    }
    return fields;
  }

  const entries = Object.entries(manifest.inputs.fields);
  if (entries.length === 0) return null;
  const fields: PlaygroundField[] = [];
  for (const [name, field] of entries) {
    const type = primitiveType(field.type);
    if (!type) return null;
    fields.push({ name, type, required: true, defaultText: "" });
  }
  return fields;
}

/**
 * Seed payload for the raw JSON editor: one key per known parameter with its
 * default (or a type-appropriate placeholder), `{}` when nothing is recorded.
 */
export function exampleBody(manifest: DeploymentManifest): Record<string, JsonValue> {
  const body: Record<string, JsonValue> = {};
  const contract = manifest.client_contract;
  if (contract) {
    for (const parameter of contract.operation.parameters) {
      if (["var_positional", "var_keyword"].includes(parameter.parameter_kind)) continue;
      if (parameter.default !== null && parameter.default !== undefined) {
        body[parameter.name] = parameter.default;
        continue;
      }
      const type = typeof parameter.json_schema.type === "string" ? parameter.json_schema.type : "";
      body[parameter.name] = placeholderValue(type);
    }
    return body;
  }
  for (const [name, field] of Object.entries(manifest.inputs.fields)) {
    body[name] = placeholderValue(field.type);
  }
  return body;
}

function placeholderValue(type: string): JsonValue {
  switch (type) {
    case "string":
      return "";
    case "integer":
    case "number":
      return 0;
    case "boolean":
      return false;
    case "array":
      return [];
    case "object":
    case "json":
      return {};
    default:
      return null;
  }
}

export type BuildBodyResult =
  { body: Record<string, JsonValue>; error?: undefined } | { body?: undefined; error: string };

/**
 * Parse typed field inputs into the kwargs JSON body. Empty optional fields
 * are omitted; empty required fields are an error.
 */
export function buildBody(
  fields: PlaygroundField[],
  values: Record<string, string>,
): BuildBodyResult {
  const body: Record<string, JsonValue> = {};
  for (const field of fields) {
    const raw = (values[field.name] ?? "").trim();
    if (raw === "") {
      if (field.required) return { error: `${field.name} is required` };
      continue;
    }
    switch (field.type) {
      case "string":
        body[field.name] = values[field.name] ?? "";
        break;
      case "integer": {
        if (!/^-?\d+$/.test(raw)) return { error: `${field.name} must be an integer` };
        body[field.name] = Number(raw);
        break;
      }
      case "number": {
        const parsed = Number(raw);
        if (Number.isNaN(parsed)) return { error: `${field.name} must be a number` };
        body[field.name] = parsed;
        break;
      }
      case "boolean": {
        if (raw !== "true" && raw !== "false") {
          return { error: `${field.name} must be true or false` };
        }
        body[field.name] = raw === "true";
        break;
      }
    }
  }
  return { body };
}

/** POSIX shell single-quoting: close, escaped quote, reopen. */
function shellSingleQuote(value: string): string {
  return `'${value.replaceAll("'", "'\\''")}'`;
}

/**
 * Working curl equivalent of the playground invoke. The bearer token is never
 * embedded; the snippet reads `$LAZYCLOUD_TOKEN` from the environment.
 */
export function curlSnippet(url: string, body: JsonValue): string {
  return [
    `curl -X POST ${shellSingleQuote(url)} \\`,
    `  -H "Authorization: Bearer $LAZYCLOUD_TOKEN" \\`,
    `  -H 'Content-Type: application/json' \\`,
    `  -d ${shellSingleQuote(JSON.stringify(body))}`,
  ].join("\n");
}

/**
 * Working Python (requests) equivalent of the playground invoke.
 *
 * The token is bound before the call rather than read inline: the inline form
 * put the longest line in the snippet at 71 columns, which is what the panel
 * had to scroll sideways to show, and a named binding is what a reader would
 * have written anyway.
 */
export function pythonSnippet(url: string, body: JsonValue): string {
  return [
    "import os",
    "",
    "import requests",
    "",
    `token = os.environ["LAZYCLOUD_TOKEN"]`,
    "",
    "response = requests.post(",
    `    ${JSON.stringify(url)},`,
    `    headers={"Authorization": f"Bearer {token}"},`,
    `    json=${pythonLiteral(body, 4)},`,
    ")",
    "response.raise_for_status()",
    "print(response.json())",
  ].join("\n");
}

/**
 * Longest single-line Python literal the snippet renders inline. Past it the
 * literal is expanded one entry per line: a payload with a dozen arguments is
 * one unreadable line otherwise, and the snippet column is what has to hold it.
 */
const PYTHON_LITERAL_WIDTH = 58;

/**
 * Render a JSON value as a Python literal (True/False/None differ from JSON),
 * expanded across lines once the one-line form outruns the snippet column.
 */
function pythonLiteral(value: JsonValue, indent: number): string {
  const flat = flatPythonLiteral(value);
  if (value === null || typeof value !== "object") return flat;
  if (indent + flat.length <= PYTHON_LITERAL_WIDTH) return flat;

  const entries = Array.isArray(value)
    ? value.map((item) => pythonLiteral(item, indent + 4))
    : Object.entries(value).map(
        ([key, item]) => `${JSON.stringify(key)}: ${pythonLiteral(item, indent + 4)}`,
      );
  const [open, close] = Array.isArray(value) ? ["[", "]"] : ["{", "}"];
  const inner = entries.map((entry) => `${" ".repeat(indent + 4)}${entry},`).join("\n");
  return `${open}\n${inner}\n${" ".repeat(indent)}${close}`;
}

/** The one-line form, which is what the width is measured against. */
function flatPythonLiteral(value: JsonValue): string {
  if (value === null) return "None";
  if (typeof value === "boolean") return value ? "True" : "False";
  if (typeof value === "number") return JSON.stringify(value);
  if (typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) {
    if (value.length === 0) return "[]";
    return `[${value.map((item) => flatPythonLiteral(item)).join(", ")}]`;
  }
  const entries = Object.entries(value);
  if (entries.length === 0) return "{}";
  const inner = entries
    .map(([key, item]) => `${JSON.stringify(key)}: ${flatPythonLiteral(item)}`)
    .join(", ");
  return `{${inner}}`;
}
