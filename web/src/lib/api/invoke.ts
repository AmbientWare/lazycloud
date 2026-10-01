import { ApiError, api, type Schemas } from "@/lib/api/client";
import type { JsonValue } from "@/lib/api/schemas";
import { parseStubId } from "@/lib/api/views";
import { workspaceName } from "@/lib/api/workspaces";

/**
 * Honest result of firing a deployed invoke URL: the real HTTP status and
 * body, plus the created task id when the backend returned one
 * (a function invoke answers with its task).
 */
export type InvokeResult = {
  status: number;
  ok: boolean;
  durationMs: number;
  bodyText: string;
  json: JsonValue | undefined;
  taskId: string | null;
};

const FUNCTION_INVOKE =
  /^\/v1\/workspaces\/([^/]+)\/apps\/([^/]+)\/functions\/([^/]+)(?:\/versions\/(\d+))?\/invoke$/;

/**
 * Invoke a deployed workload with a JSON payload. A function's invoke URL is
 * this origin's invoke operation, which runs it as a task and answers once it
 * finishes or the server's wait runs out; non-2xx answers resolve so callers
 * show the real error. An endpoint answers on its own host, which takes a
 * workspace token and no cross-origin requests, so the page cannot call it.
 */
export async function invokeDeployment(url: string, body: JsonValue): Promise<InvokeResult> {
  const match = FUNCTION_INVOKE.exec(url);
  if (!match) {
    throw new ApiError(
      403,
      "Forbidden",
      JSON.stringify({
        message:
          "Endpoints take a workspace token, which the dashboard does not hold. Call it with curl or the Python SDK.",
      }),
    );
  }
  const [workspace, app, name] = match.slice(1, 4).map(decodeURIComponent);
  const version = match[4] ? Number(match[4]) : undefined;
  const startedAt = performance.now();
  const { response, data, error } =
    version === undefined
      ? await api.POST("/v1/workspaces/{workspace}/apps/{app}/functions/{function}/invoke", {
          params: { path: { workspace, app, function: name } },
          body: body as Schemas["InvocationBody"],
        })
      : await api.POST(
          "/v1/workspaces/{workspace}/apps/{app}/functions/{function}/versions/{version}/invoke",
          {
            params: { path: { workspace, app, function: name, version } },
            body: body as Schemas["InvocationBody"],
          },
        );
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
