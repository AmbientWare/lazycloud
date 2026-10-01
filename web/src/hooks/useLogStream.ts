import { useEffect, useState } from "react";

import { api, apiError, isApiError, type Schemas } from "@/lib/api/client";
import { readNdjson } from "@/lib/api/ndjson";

export type LogEntry = Schemas["LogEntry"];

/** Whose lines to read: one task, every task of a workload, or one container. */
export type LogScope =
  | { kind: "task"; id: string }
  | { kind: "deployment"; id: string }
  | { kind: "container"; id: string };

export type LogStreamStatus = "connecting" | "open" | "reconnecting" | "closed" | "error";

/** The newest stored lines a view starts from. */
export const LOG_TAIL = 1_000;
/** Lines a view keeps; older ones fall off as new ones arrive. */
const MAX_KEPT = 2_000;
const INITIAL_RETRY_MS = 1_000;
const MAX_RETRY_MS = 30_000;

/**
 * Log lines of a scope: the last `LOG_TAIL` stored lines, then, while
 * `follow` is on, every new line as it is written. A dropped connection
 * resumes after the last line it delivered, so nothing repeats or goes
 * missing. A followed task or container stream ends with the task or the
 * container, which leaves the status `closed`.
 */
export function useLogStream(
  workspace: string,
  scope: LogScope,
  follow: boolean,
): { entries: LogEntry[]; status: LogStreamStatus; error: Error | null } {
  const [state, setState] = useState<{
    key: string;
    entries: LogEntry[];
    status: LogStreamStatus;
    error: Error | null;
  }>(() => ({ key: "", entries: [], status: "connecting", error: null }));
  const key = `${workspace}/${scope.kind}/${scope.id}`;
  const current =
    state.key === key ? state : { key, entries: [], status: "connecting" as const, error: null };
  if (current !== state) setState(current);
  const lastId = current.entries.at(-1)?.id ?? 0;

  useEffect(() => {
    const controller = new AbortController();
    let after = lastId;
    let retryMs = INITIAL_RETRY_MS;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const update = (patch: Partial<typeof state>) =>
      setState((previous) => (previous.key === key ? { ...previous, ...patch } : previous));
    const append = (entry: LogEntry) => {
      if (entry.id <= after) return;
      after = entry.id;
      retryMs = INITIAL_RETRY_MS;
      setState((previous) => {
        if (previous.key !== key) return previous;
        const entries = [...previous.entries, entry];
        return {
          ...previous,
          status: "open",
          entries: entries.length > MAX_KEPT ? entries.slice(-MAX_KEPT) : entries,
        };
      });
    };

    const connect = async () => {
      try {
        const body = await openLogStream(workspace, scope, {
          after,
          follow,
          signal: controller.signal,
        });
        update({ status: follow ? "open" : "closed", error: null });
        await readNdjson<LogEntry>(body, append);
        update({ status: "closed" });
      } catch (error) {
        if (controller.signal.aborted) return;
        if (isApiError(error) && error.status < 500) {
          update({ status: "error", error });
          return;
        }
        update({ status: "reconnecting", error: error instanceof Error ? error : null });
        timer = setTimeout(() => void connect(), retryMs);
        retryMs = Math.min(retryMs * 2, MAX_RETRY_MS);
      }
    };
    void connect();
    return () => {
      controller.abort();
      if (timer !== undefined) clearTimeout(timer);
    };
    // `lastId` seeds a resumed connection when `follow` turns back on; it must
    // not reconnect on every line.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, follow]);

  return { entries: current.entries, status: current.status, error: current.error };
}

async function openLogStream(
  workspace: string,
  scope: LogScope,
  { after, follow, signal }: { after: number; follow: boolean; signal: AbortSignal },
): Promise<ReadableStream<Uint8Array>> {
  const query = after > 0 ? { after, follow } : { tail: LOG_TAIL, follow };
  const options = { parseAs: "stream" as const, signal };
  const result =
    scope.kind === "task"
      ? await api.GET("/v1/workspaces/{workspace}/tasks/{task}/logs", {
          ...options,
          params: { path: { workspace, task: scope.id }, query },
        })
      : scope.kind === "deployment"
        ? await api.GET("/v1/workspaces/{workspace}/deployments/{deployment}/logs", {
            ...options,
            params: { path: { workspace, deployment: scope.id }, query },
          })
        : await api.GET("/v1/workspaces/{workspace}/containers/{container}/logs", {
            ...options,
            params: { path: { workspace, container: scope.id }, query },
          });
  if (!result.response.ok || !result.data) {
    throw apiError(result.response.status, result.response.statusText, result.error);
  }
  return result.data;
}
