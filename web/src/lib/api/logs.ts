import type { QueryClient } from "@tanstack/react-query";

import { ApiError, api, type Schemas } from "@/lib/api/client";
import { readNdjson } from "@/lib/api/ndjson";
import type { LogRecord } from "@/lib/api/schemas";
import { parseStubId } from "@/lib/api/views";
import { workloadDirectory } from "@/lib/queries/directory";

export type LogScope = {
  appId?: string;
  stubId?: string;
  taskId?: string;
  containerId?: string;
};

/** A log line as the dashboard's log viewer reads it. */
export function viewLogRecord(entry: Schemas["LogEntry"]): LogRecord {
  return {
    id: String(entry.id),
    cursor: String(entry.id),
    seq_num: entry.id,
    stored_at_ns: 0,
    timestamp: entry.time,
    message: entry.data,
    stream: entry.stream,
    container_id: "",
    stub_id: "",
    stub_type: "",
    task_id: entry.task_id,
    workspace_id: "",
    app_id: "",
    deployment_id: "",
  } as LogRecord;
}

/**
 * Open the log stream of a scope: one task, one container, or a workload by
 * its stub. Lines after `after`, else the last `tail`; with `follow` the
 * stream stays open for new lines.
 */
export async function openLogStream(
  workspace: string,
  workloadId: (app: string, name: string) => Promise<string | undefined>,
  scope: LogScope,
  options: { after?: number; tail?: number; follow: boolean; signal?: AbortSignal },
): Promise<ReadableStream<Uint8Array>> {
  const query = options.after
    ? { after: options.after, follow: options.follow }
    : { tail: options.tail, follow: options.follow };
  const base = { parseAs: "stream" as const, signal: options.signal };
  let result;
  if (scope.taskId) {
    result = await api.GET("/v1/workspaces/{workspace}/tasks/{task}/logs", {
      ...base,
      params: { path: { workspace, task: scope.taskId }, query },
    });
    if (result.response.status === 404) {
      return requestLogStream(workspace, scope.taskId, options.after ?? 0, options.signal);
    }
  } else if (scope.containerId) {
    result = await api.GET("/v1/workspaces/{workspace}/containers/{container}/logs", {
      ...base,
      params: { path: { workspace, container: scope.containerId }, query },
    });
  } else if (scope.stubId) {
    const stub = parseStubId(scope.stubId);
    const deployment = await workloadId(stub.app, stub.name);
    if (!deployment) throw new ApiError(404, "Not Found", JSON.stringify({ message: "not found" }));
    result = await api.GET("/v1/workspaces/{workspace}/deployments/{deployment}/logs", {
      ...base,
      params: { path: { workspace, deployment }, query },
    });
  } else {
    throw new ApiError(404, "Not Found", JSON.stringify({ message: "Choose a task or container" }));
  }
  if (!result.response.ok || !result.data) {
    throw new ApiError(
      result.response.status,
      result.response.statusText,
      JSON.stringify(result.error ?? ""),
    );
  }
  return result.data;
}

/**
 * What an endpoint or ASGI workload wrote while serving one request, as the
 * task log stream's lines. The edge records a request once it ends, so the
 * lines are complete and the stream ends after them.
 */
async function requestLogStream(
  workspace: string,
  requestId: string,
  after: number,
  signal?: AbortSignal,
): Promise<ReadableStream<Uint8Array>> {
  const { response, data, error } = await api.GET(
    "/v1/workspaces/{workspace}/requests/{http_request}/logs",
    { params: { path: { workspace, http_request: requestId }, query: { after } }, signal },
  );
  if (!data) {
    throw new ApiError(response.status, response.statusText, JSON.stringify(error ?? ""));
  }
  const lines = data.data
    .map((line) => JSON.stringify({ ...line, task_id: requestId, attempt: 1 }) + "\n")
    .join("");
  return new Blob([lines]).stream();
}

/** The stored lines of a scope, read to the end of the stream. */
export async function readLogEntries(
  client: QueryClient,
  workspace: string,
  workspaceId: string,
  scope: LogScope,
  options: { tail: number; follow: false; signal?: AbortSignal },
): Promise<Schemas["LogEntry"][]> {
  const body = await openLogStream(
    workspace,
    async (app, name) =>
      (await workloadDirectory(client, workspaceId)).byName.get(`${app}/${name}`)?.id,
    scope,
    options,
  );
  const entries: Schemas["LogEntry"][] = [];
  await readNdjson<Schemas["LogEntry"]>(body, (entry) => entries.push(entry));
  return entries;
}
