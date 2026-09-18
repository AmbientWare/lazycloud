import { getStoredAuthToken } from "@/lib/auth";
import { postJson, withWorkspace } from "@/lib/api/client";
import { functionInvokeResponseSchema, type JsonValue } from "@/lib/api/schemas";

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

/**
 * POST a JSON payload to a deployed invoke URL with the session bearer token.
 * Non-2xx responses resolve (not throw) so callers can surface the backend's
 * real error body; only network failures reject.
 */
export async function invokeDeployment(url: string, body: JsonValue): Promise<InvokeResult> {
  const headers = new Headers({ "Content-Type": "application/json" });
  const token = getStoredAuthToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const startedAt = performance.now();
  const response = await fetch(url, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  const durationMs = performance.now() - startedAt;
  const bodyText = await response.text();

  let json: JsonValue | undefined;
  try {
    json = bodyText ? (JSON.parse(bodyText) as JsonValue) : undefined;
  } catch {
    json = undefined;
  }
  const invocation = functionInvokeResponseSchema.safeParse(json);
  const taskId = invocation.success && invocation.data.task_id ? invocation.data.task_id : null;

  return {
    status: response.status,
    ok: response.ok,
    durationMs,
    bodyText,
    json,
    taskId,
  };
}

/**
 * Invoke a function whose return is a Python object. The deployed invoke URL
 * only answers with JSON, so the call goes through the function API and asks
 * for the result as a stored Python payload; the task then carries its
 * description for the dashboard to show.
 *
 * The body follows the invoke URL's shape: `args`/`kwargs` when present,
 * otherwise the object is the keyword arguments.
 */
export async function invokeFunctionTask(
  workspaceId: string,
  stubId: string,
  body: JsonValue,
): Promise<InvokeResult> {
  const startedAt = performance.now();
  const response = await postJson(
    withWorkspace("/api/v1/functions/invoke", workspaceId),
    functionInvokeResponseSchema,
    {
      stub_id: stubId,
      invocation: {
        version: 1,
        encoding: "json",
        ...invocationArguments(body),
        result_encoding: "cloudpickle",
      },
    },
  );
  return {
    status: 200,
    ok: true,
    durationMs: performance.now() - startedAt,
    bodyText: JSON.stringify(response),
    json: response as JsonValue,
    taskId: response.task_id || null,
  };
}

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
