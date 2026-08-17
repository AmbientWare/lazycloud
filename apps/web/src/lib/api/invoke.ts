import { getStoredAuthToken } from "@/lib/auth";
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
