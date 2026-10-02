import { api, type Schemas } from "@/lib/api/client";
import type { JsonValue } from "@/lib/json";

/**
 * Honest result of a playground call: the real HTTP status and body, plus the
 * created task id when the call ran as a task. Non-2xx answers resolve so
 * callers show the server's error.
 */
export type InvokeResult = {
  status: number;
  ok: boolean;
  durationMs: number;
  bodyText: string;
  json: JsonValue | undefined;
  taskId: string | null;
};

type Answer = { response: Response; data?: { task: { id: string } }; error?: unknown };

function invokeResult(startedAt: number, { response, data, error }: Answer): InvokeResult {
  const json = (data ?? error ?? null) as JsonValue;
  return {
    status: response.status,
    ok: response.ok,
    durationMs: performance.now() - startedAt,
    bodyText: JSON.stringify(json),
    json,
    taskId: data?.task.id ?? null,
  };
}

/**
 * Run a function's active release with JSON arguments. The server runs it as
 * a task and answers once it finishes or its wait runs out.
 */
export async function invokeFunction(
  workspace: string,
  app: string,
  name: string,
  body: JsonValue,
): Promise<InvokeResult> {
  const startedAt = performance.now();
  return invokeResult(
    startedAt,
    await api.POST("/v1/workspaces/{workspace}/apps/{app}/workloads/function/{name}/invoke", {
      params: { path: { workspace, app, name } },
      body: body as Schemas["InvocationBody"],
    }),
  );
}

/**
 * Submit a function call as a task, for a call whose return is a Python
 * object: the task stores the result and the playground shows it from there.
 */
export async function invokeFunctionTask(
  workspace: string,
  app: string,
  name: string,
  body: JsonValue,
): Promise<InvokeResult> {
  const startedAt = performance.now();
  const { response, data, error } = await api.POST(
    "/v1/workspaces/{workspace}/apps/{app}/workloads/function/{name}/tasks",
    {
      params: { path: { workspace, app, name } },
      body: { inputs: [{ encoding: "json", value: invocationArguments(body) }] },
    },
  );
  const task = data?.tasks[0];
  return invokeResult(startedAt, { response, data: task ? { task } : undefined, error });
}

/**
 * Call an endpoint at its path on this origin, which the session cookie
 * authenticates. `path` is the endpoint's invoke path from its describe read
 * with the request's route appended. A GET or HEAD carries no body, so an
 * object payload becomes its query string, as the endpoint reads it.
 */
export async function invokeHttp(
  path: string,
  method: string,
  body: JsonValue,
): Promise<InvokeResult> {
  const startedAt = performance.now();
  const bodiless = method === "GET" || method === "HEAD";
  const response = await fetch(bodiless ? withQuery(path, body) : path, {
    method,
    headers: bodiless ? undefined : { "Content-Type": "application/json" },
    body: bodiless ? undefined : JSON.stringify(body),
  });
  const bodyText = await response.text();
  let json: JsonValue | undefined;
  try {
    json = bodyText ? (JSON.parse(bodyText) as JsonValue) : undefined;
  } catch {
    json = undefined;
  }
  return {
    status: response.status,
    ok: response.ok,
    durationMs: performance.now() - startedAt,
    bodyText,
    json,
    taskId: null,
  };
}

function withQuery(path: string, body: JsonValue): string {
  if (body === null || typeof body !== "object" || Array.isArray(body)) return path;
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(body)) {
    if (value === null || value === undefined) continue;
    query.set(key, typeof value === "string" ? value : JSON.stringify(value));
  }
  const text = query.toString();
  return text ? `${path}${path.includes("?") ? "&" : "?"}${text}` : path;
}

/** The body's `args` and `kwargs` when it names them; otherwise the body is the keyword arguments. */
function invocationArguments(body: JsonValue): {
  args: JsonValue[];
  kwargs: Record<string, JsonValue>;
} {
  if (body === null || typeof body !== "object" || Array.isArray(body)) {
    return { args: [], kwargs: {} };
  }
  const { args, kwargs, ...rest } = body;
  return {
    args: Array.isArray(args) ? args : [],
    kwargs: kwargs !== null && typeof kwargs === "object" && !Array.isArray(kwargs) ? kwargs : rest,
  };
}
