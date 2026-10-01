import { api, ok, type Schemas } from "@/lib/api/client";
import { readNdjson } from "@/lib/api/ndjson";

/** One output line; task, container and request logs all carry these fields. */
export type LogLine = Schemas["ContainerLogEntry"];

/** Whose output a log view shows. A request's output is complete once it exists. */
export type LogSource = { task: string } | { container: string } | { request: string };

/** The stored lines a log view opens with. */
export const LOG_HISTORY_LINES = 200;

/** A followed stream sends a keep-alive every 15 seconds; two missed ones is a dead stream. */
export const LOG_FOLLOW_IDLE_MS = 30_000;

const REQUEST_LOG_PAGE = 1000;

function openStream(
  workspace: string,
  source: { task: string } | { container: string },
  query: { after?: number; tail?: number; follow: boolean },
  signal: AbortSignal,
) {
  const options = { parseAs: "stream" as const, signal };
  return ok(
    "task" in source
      ? api.GET("/v1/workspaces/{workspace}/tasks/{task}/logs", {
          ...options,
          params: { path: { workspace, task: source.task }, query },
        })
      : api.GET("/v1/workspaces/{workspace}/containers/{container}/logs", {
          ...options,
          params: { path: { workspace, container: source.container }, query },
        }),
  ) as Promise<ReadableStream<Uint8Array>>;
}

/** The newest `LOG_HISTORY_LINES` stored lines of the source, oldest first. */
export async function readLogHistory(
  workspace: string,
  source: LogSource,
  signal: AbortSignal,
): Promise<LogLine[]> {
  if ("request" in source) return readRequestLogTail(workspace, source.request, signal);
  const lines: LogLine[] = [];
  const body = await openStream(
    workspace,
    source,
    { tail: LOG_HISTORY_LINES, follow: false },
    signal,
  );
  await readNdjson<LogLine>(body, (line) => lines.push(line));
  return lines;
}

/**
 * The request log API reads forward from a cursor, so the newest lines are
 * the last of a walk to its end, keeping only that many on the way.
 */
async function readRequestLogTail(
  workspace: string,
  requestId: string,
  signal: AbortSignal,
): Promise<LogLine[]> {
  let lines: LogLine[] = [];
  let after = 0;
  for (;;) {
    const page = await ok(
      api.GET("/v1/workspaces/{workspace}/requests/{http_request}/logs", {
        params: {
          path: { workspace, http_request: requestId },
          query: { after, limit: REQUEST_LOG_PAGE },
        },
        signal,
      }),
    );
    lines = [...lines, ...page.data].slice(-LOG_HISTORY_LINES);
    const last = page.data.at(-1);
    if (!last || page.data.length < REQUEST_LOG_PAGE) return lines;
    after = last.id;
  }
}

/** Whether a task or container can write no more output. */
async function sourceFinished(
  workspace: string,
  source: { task: string } | { container: string },
  signal: AbortSignal,
): Promise<boolean> {
  if ("task" in source) {
    const task = await ok(
      api.GET("/v1/workspaces/{workspace}/tasks/{task}", {
        params: { path: { workspace, task: source.task } },
        signal,
      }),
    );
    return task.status === "succeeded" || task.status === "failed" || task.status === "cancelled";
  }
  const container = await ok(
    api.GET("/v1/workspaces/{workspace}/containers/{container}", {
      params: { path: { workspace, container: source.container } },
      signal,
    }),
  );
  return container.state === "stopped";
}

/**
 * Follow a task's or container's output from the line after `after`. The
 * server ends a followed stream when the source finishes, and also when it
 * restarts or redeploys, so a clean end resolves "finished" only once the
 * source is terminal and "reconnect" otherwise. A stream silent past the
 * keep-alive interval rejects with `StreamIdleError`.
 */
export async function followLogs(
  workspace: string,
  source: { task: string } | { container: string },
  after: number,
  { signal, onOpen, onLine }: { signal: AbortSignal; onOpen: () => void; onLine: (line: LogLine) => void },
): Promise<"finished" | "reconnect"> {
  const body = await openStream(
    workspace,
    source,
    { after: after || undefined, follow: true },
    signal,
  );
  onOpen();
  await readNdjson<LogLine>(body, onLine, { idleMs: LOG_FOLLOW_IDLE_MS });
  return (await sourceFinished(workspace, source, signal)) ? "finished" : "reconnect";
}
