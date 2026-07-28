import { infiniteQueryOptions } from "@tanstack/react-query";

import { apiRequest, withWorkspace } from "@/lib/api/client";
import { logQuerySchema, type LogRecord } from "@/lib/api/schemas";

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
    queryFn: ({ pageParam }) => {
      const params = logScopeParams(scope);
      params.set("limit", String(LOG_PAGE_SIZE));
      params.set("page", String(pageParam));
      return apiRequest(
        withWorkspace(`/api/v1/logs?${params.toString()}`, workspaceId),
        logQuerySchema,
      );
    },
    initialPageParam: 0,
    getNextPageParam: (lastPage) => {
      if (!lastPage.next) return undefined;
      const nextPage = Number(lastPage.next);
      return Number.isInteger(nextPage) && nextPage >= 0 ? nextPage : undefined;
    },
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
