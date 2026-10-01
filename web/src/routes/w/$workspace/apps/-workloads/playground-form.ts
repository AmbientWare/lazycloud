import { z } from "zod";

import type { JsonValue } from "@/lib/api/schemas";
import { jsonValueSchema } from "@/lib/api/schemas/json";

/** Workload kinds the playground can invoke with a JSON payload. */
export const PLAYGROUND_KINDS = new Set(["function", "endpoint"]);

// The callable contract a deploy records. The API leaves `client_contract`
// open; the SDK writes it as python/shared/src/shared/http/client_manifests.py
// describes, and the playground and call examples read it.
const clientParameterSchema = z
  .object({
    name: z.string(),
    json_schema: z.record(jsonValueSchema).nullable().default({}),
    python_type: z.string().default(""),
    required: z.boolean().default(true),
    default: jsonValueSchema.nullish(),
    python_default: z.boolean().default(false),
    parameter_kind: z.string().default("keyword"),
  })
  .refine(
    (parameter) => (parameter.json_schema === null) === Boolean(parameter.python_type),
    "A Python-only parameter must name its type and omit its JSON schema",
  )
  .refine(
    (parameter) => !parameter.python_default || (!parameter.required && parameter.default == null),
    "Python defaults remain in the handler",
  );

const clientContractSchema = z.object({
  operation: z
    .object({
      name: z.string(),
      parameters: z.array(clientParameterSchema).default([]),
      return_schema: z.record(jsonValueSchema).nullable().default({}),
      return_python_type: z.string().default(""),
    })
    .refine(
      (operation) => (operation.return_schema === null) === Boolean(operation.return_python_type),
      "A Python-only return must name its type and omit its JSON schema",
    ),
});

export type ClientContract = z.infer<typeof clientContractSchema>;

/** The release's callable contract, or null when it records none the dashboard can read. */
export function clientContract(value: unknown): ClientContract | null {
  const parsed = clientContractSchema.safeParse(value);
  return parsed.success ? parsed.data : null;
}

const PRIMITIVE_TYPES = ["string", "integer", "number", "boolean"] as const;
type PrimitiveType = (typeof PRIMITIVE_TYPES)[number];

/** Why an HTTP caller cannot use this function: Python-only arguments or return. */
export function pythonOnlyReason(contract: ClientContract | null): string | null {
  if (!contract) return null;
  const reasons = parameterPythonReasons(contract);
  if (contract.operation.return_python_type) {
    reasons.push(`return: ${contract.operation.return_python_type}`);
  }
  return reasons.length ? `Use the Python SDK. ${reasons.join("; ")}.` : null;
}

/**
 * Why the playground cannot build a call. Only the arguments matter here: a
 * Python-only return is requested as a stored Python result and shown from
 * the task, the same way `lazycloud run` shows it.
 */
export function playgroundPythonOnlyReason(contract: ClientContract | null): string | null {
  const reasons = parameterPythonReasons(contract);
  return reasons.length ? `Use the Python SDK. ${reasons.join("; ")}.` : null;
}

export function returnsPythonValue(contract: ClientContract | null): boolean {
  return Boolean(contract?.operation.return_python_type);
}

function parameterPythonReasons(contract: ClientContract | null): string[] {
  const reasons: string[] = [];
  for (const parameter of contract?.operation.parameters ?? []) {
    if (parameter.python_type) reasons.push(`${parameter.name}: ${parameter.python_type}`);
    if (parameter.python_default) reasons.push(`${parameter.name} has a Python default`);
  }
  return reasons;
}

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

const VARIADIC = ["var_positional", "var_keyword"];

/**
 * Typed per-field inputs are only offered when the recorded contract is a
 * flat list of primitives. Anything richer (arrays, objects, unknown shapes),
 * or no contract at all, falls back to the raw JSON editor instead of a fake
 * form.
 */
export function playgroundFields(contract: ClientContract | null): PlaygroundField[] | null {
  if (!contract) return null;
  const fields: PlaygroundField[] = [];
  for (const parameter of contract.operation.parameters) {
    if (VARIADIC.includes(parameter.parameter_kind)) continue;
    const rawType = parameter.json_schema?.type;
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

/**
 * Seed payload for the raw JSON editor: one key per known parameter with its
 * default (or a type-appropriate placeholder), `{}` when nothing is recorded.
 */
export function exampleBody(contract: ClientContract | null): Record<string, JsonValue> {
  const body: Record<string, JsonValue> = {};
  for (const parameter of contract?.operation.parameters ?? []) {
    if (VARIADIC.includes(parameter.parameter_kind)) continue;
    if (parameter.python_type || parameter.python_default) continue;
    if (parameter.default !== null && parameter.default !== undefined) {
      body[parameter.name] = parameter.default;
      continue;
    }
    const type = typeof parameter.json_schema?.type === "string" ? parameter.json_schema.type : "";
    body[parameter.name] = placeholderValue(type);
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
export function shellSingleQuote(value: string): string {
  return `'${value.replaceAll("'", "'\\''")}'`;
}

/**
 * Working curl equivalent of the playground invoke. The bearer token is never
 * embedded; the snippet reads `$LAZYCLOUD_TOKEN` from the environment.
 */
export function curlSnippet(url: string, body?: JsonValue, method = "POST"): string {
  return [
    `curl -X ${method} ${shellSingleQuote(url)}`,
    `  -H "Authorization: Bearer $LAZYCLOUD_TOKEN"`,
    ...(body === undefined
      ? []
      : [
          `  -H 'Content-Type: application/json'`,
          `  -d ${shellSingleQuote(JSON.stringify(body))}`,
        ]),
  ].join(" \\\n");
}

export function pythonSnippet(url: string, body?: JsonValue, method = "POST"): string {
  return [
    "import os",
    "",
    "import requests",
    "",
    `token = os.environ["LAZYCLOUD_TOKEN"]`,
    "",
    "response = requests.request(",
    `    ${JSON.stringify(method)},`,
    `    ${JSON.stringify(url)},`,
    `    headers={"Authorization": f"Bearer {token}"},`,
    ...(body === undefined ? [] : [`    json=${pythonLiteral(body, 4)},`]),
    ")",
    "response.raise_for_status()",
    body === undefined ? "print(response.text)" : "print(response.json())",
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
export function pythonLiteral(value: JsonValue, indent: number): string {
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
