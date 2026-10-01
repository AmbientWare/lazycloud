import { infiniteQueryOptions } from "@tanstack/react-query";

import { workspaceName } from "@/lib/api/workspaces";
import { readLogEntries, viewLogRecord } from "@/lib/api/logs";
import type { LogQuery, LogRecord } from "@/lib/api/schemas";

import { selectInfiniteList, type InfiniteListQueryData } from "./infinite-list";
import { workspaceQueryKeys } from "./workspace-keys";

export type LogScope = {
  appId?: string;
  stubId?: string;
  taskId?: string;
  containerId?: string;
};

const LOG_PAGE_SIZE = 200;

export function logScopeParams(scope: LogScope): URLSearchParams {
  const params = new URLSearchParams();
  if (scope.appId) params.set("app_id", scope.appId);
  if (scope.stubId) params.set("stub_id", scope.stubId);
  if (scope.taskId) params.set("task_id", scope.taskId);
  if (scope.containerId) params.set("container_id", scope.containerId);
  return params;
}

export function logHistoryQueryOptions(workspaceId: string, scope: LogScope) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.logs.history(workspaceId, {
      appId: scope.appId ?? null,
      stubId: scope.stubId ?? null,
      taskId: scope.taskId ?? null,
      containerId: scope.containerId ?? null,
    }),
    // The newest lines of the scope. The API reads forward from a cursor, so
    // there is no older page to load.
    queryFn: async ({ signal, client }): Promise<LogQuery> => {
      const entries = await readLogEntries(client, workspaceName(workspaceId), workspaceId, scope, {
        tail: LOG_PAGE_SIZE,
        follow: false,
        signal,
      });
      return { data: entries.map(viewLogRecord), next: "" };
    },
    initialPageParam: "",
    getNextPageParam: (lastPage) => lastPage.next || undefined,
  });
}

export function selectLogHistory(
  data: InfiniteListQueryData<LogRecord> | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectInfiniteList(data, hasNextPage, logRecordKey);
}

function logRecordKey(record: LogRecord): string {
  return record.id || `${record.timestamp}-${record.seq_num}-${record.message}`;
}
