import { ApiError, api } from "@/lib/api/client";
import type { JsonValue } from "@/lib/api/schemas";
import { parseStubId } from "@/lib/api/views";
import { workspaceName } from "@/lib/api/workspaces";

/**
 * Honest result of firing a deployed invoke URL: the real HTTP status and
 * body, plus the created task id when the backend returned one
 * (function invokes respond `{task_id: ...}`).
 */
export type InvokeResult = {
  status: number;
  ok: boolean;
  durationMs: number;
  bodyText: string;
  json: JsonValue | undefined;
  taskId: string | null;
};

const FUNCTION_INVOKE = /^\/v1\/workspaces\/([^/]+)\/apps\/([^/]+)\/functions\/([^/]+)\/invoke$/;

/**
 * Invoke a deployed function with a JSON payload. A function is invoked by
 * admitting a task with the payload as its JSON arguments against the active
 * release; non-2xx answers resolve so callers show the real error.
 */
export async function invokeDeployment(url: string, body: JsonValue): Promise<InvokeResult> {
  const match = FUNCTION_INVOKE.exec(url);
  if (!match) {
    throw new ApiError(404, "Not Found", JSON.stringify({ message: "no such operation" }));
  }
  const [workspace, app, name] = match.slice(1).map(decodeURIComponent);
  return submit(workspace, app, name, body);
}

/** Invoke a function by its stub, for a call whose return is a Python object. */
export async function invokeFunctionTask(
  workspaceId: string,
  stubId: string,
  body: JsonValue,
): Promise<InvokeResult> {
  const stub = parseStubId(stubId);
  return submit(workspaceName(workspaceId), stub.app, stub.name, body);
}

async function submit(
  workspace: string,
  app: string,
  name: string,
  body: JsonValue,
): Promise<InvokeResult> {
  const startedAt = performance.now();
  const { response, data, error } = await api.POST(
    "/v1/workspaces/{workspace}/apps/{app}/functions/{function}/tasks",
    {
      params: { path: { workspace, app, function: name } },
      body: { inputs: [{ encoding: "json", value: invocationArguments(body) }] },
    },
  );
  const json = (data ?? error ?? null) as JsonValue;
  return {
    status: response.status,
    ok: response.ok,
    durationMs: performance.now() - startedAt,
    bodyText: JSON.stringify(json),
    json,
    taskId: data?.tasks[0]?.id ?? null,
  };
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
