import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";
import { LIVE_LIST_MAX_PAGES } from "./infinite-list";

import { apiRequest, postJson, withWorkspace } from "@/lib/api/client";
import {
  callGraphSchema,
  taskMetricsSummarySchema,
  taskPageSchema,
  taskSchema,
  taskTimeWindowBucketListSchema,
  type Task,
} from "@/lib/api/schemas";

import { selectInfiniteList, type InfiniteListQueryData } from "./infinite-list";
import {
  workspaceLiveQueryMeta,
  workspaceQueryKeys,
  type TaskListKeyParts,
} from "./workspace-keys";

/** Re-submit a finished task with the same payload as a new task. */
export function rerunTask(workspaceId: string, taskId: string): Promise<Task> {
  return postJson(withWorkspace(`/api/v1/tasks/${taskId}/rerun`, workspaceId), taskSchema);
}

export type TaskListOptions = {
  limit?: number;
  status?: string;
  deploymentId?: string;
  appId?: string;
  stubIds?: string[];
  kind?: string;
  createdAfter?: string;
  createdBefore?: string;
  createdWithinSeconds?: number;
  search?: string;
  rootOnly?: boolean;
  live?: boolean;
};

export function tasksQueryOptions(workspaceId: string, options: TaskListOptions = {}) {
  const params = taskListParams(options);

  return queryOptions({
    queryKey: taskListKey(workspaceId, options, "page"),
    queryFn: () =>
      apiRequest(withWorkspace(`/api/v1/tasks?${params.toString()}`, workspaceId), taskPageSchema),
    meta: workspaceLiveQueryMeta(true, options.live !== false),
  });
}

export function tasksInfiniteQueryOptions(workspaceId: string, options: TaskListOptions = {}) {
  return infiniteQueryOptions({
    queryKey: taskListKey(workspaceId, options, "infinite"),
    initialPageParam: "",
    queryFn: ({ pageParam }) => {
      const params = taskListParams(options, pageParam);
      return apiRequest(
        withWorkspace(`/api/v1/tasks?${params.toString()}`, workspaceId),
        taskPageSchema,
      );
    },
    getNextPageParam: (page) => page.next || undefined,
    // A live list is a view of what is happening now, not an archive. Without a
    // bound, every change event refetches every page the list has ever loaded:
    // one app view walked twenty pages of a hundred containers and re-walked
    // them on each event, which is most of what made the dashboard slow.
    maxPages: LIVE_LIST_MAX_PAGES,
    meta: workspaceLiveQueryMeta(true, options.live !== false),
  });
}

export function selectTaskList(
  data: InfiniteListQueryData<Task> | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectInfiniteList(data, hasNextPage, (task) => task.id);
}

function taskListParams(options: TaskListOptions, cursor = ""): URLSearchParams {
  const params = new URLSearchParams();
  params.set("limit", String(options.limit ?? 50));
  if (cursor) params.set("cursor", cursor);
  if (options.status) params.set("status", options.status);
  if (options.deploymentId) params.set("deployment_id", options.deploymentId);
  if (options.appId) params.set("app_id", options.appId);
  for (const stubId of options.stubIds ?? []) params.append("stub_id", stubId);
  if (options.kind) params.set("kind", options.kind);
  if (options.createdAfter) params.set("created_after", options.createdAfter);
  if (options.createdBefore) params.set("created_before", options.createdBefore);
  if (options.createdWithinSeconds) {
    params.set("created_within_seconds", String(options.createdWithinSeconds));
  }
  if (options.search) params.set("q", options.search);
  if (options.rootOnly) params.set("root_only", "true");
  return params;
}

function taskListKey(
  workspaceId: string,
  options: TaskListOptions,
  mode: TaskListKeyParts["mode"],
) {
  const parts: TaskListKeyParts = {
    mode,
    limit: options.limit ?? 50,
    status: options.status ?? null,
    deploymentId: options.deploymentId ?? null,
    appId: options.appId ?? null,
    stubIds: options.stubIds?.join(",") ?? null,
    kind: options.kind ?? null,
    createdAfter: options.createdAfter ?? null,
    createdBefore: options.createdBefore ?? null,
    createdWithinSeconds: options.createdWithinSeconds ?? null,
    search: options.search ?? null,
    rootOnly: options.rootOnly ?? false,
  };
  return workspaceQueryKeys.tasks.list(workspaceId, parts);
}

export function taskQueryOptions(workspaceId: string, taskId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.tasks.detail(workspaceId, taskId),
    queryFn: () => apiRequest(withWorkspace(`/api/v1/tasks/${taskId}`, workspaceId), taskSchema),
    // Rate-limited drawer reads preserve their explicit Retry-After recovery
    // instead of being retried by an unrelated workspace reconnect.
    retry: false,
    meta: workspaceLiveQueryMeta(true, true, false),
  });
}

export function callGraphQueryOptions(workspaceId: string, taskId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.tasks.callGraph(workspaceId, taskId),
    queryFn: () =>
      apiRequest(withWorkspace(`/api/v1/tasks/${taskId}/call-graph`, workspaceId), callGraphSchema),
    meta: workspaceLiveQueryMeta(true),
  });
}

export function taskMetricsQueryOptions(workspaceId: string, hours = 24, appId?: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.tasks.metrics(workspaceId, hours, appId ?? null),
    queryFn: () => {
      const endedAt = Math.floor(Date.now() / 1000);
      const startedAt = endedAt - hours * 3600;
      const appParam = appId ? `&app_id=${encodeURIComponent(appId)}` : "";
      return apiRequest(
        withWorkspace(
          `/api/v1/tasks/metrics?started_at=${startedAt}&ended_at=${endedAt}${appParam}`,
          workspaceId,
        ),
        taskMetricsSummarySchema,
      );
    },
    meta: workspaceLiveQueryMeta(true),
  });
}

/** Buckets the activity chart renders; it slices to this and discards the rest. */
const BUCKETS_DRAWN = 24;

export function taskBucketsQueryOptions(
  workspaceId: string,
  windowSeconds = 3600,
  scope: { appId?: string; stubId?: string } = {},
) {
  return queryOptions({
    queryKey: workspaceQueryKeys.tasks.buckets(
      workspaceId,
      windowSeconds,
      scope.appId ?? null,
      scope.stubId ?? null,
    ),
    queryFn: () => {
      // The span is computed per fetch and deliberately left out of the query
      // key: in the key every refetch would be a new key and nothing would ever
      // be served from cache. Asking for the buckets the chart draws is the
      // point of sending it at all, since without a span the server reads the
      // workspace's whole task history to answer for one day of it.
      const endedAt = Math.floor(Date.now() / 1000);
      const params = new URLSearchParams();
      params.set("window_seconds", String(windowSeconds));
      params.set("started_at", String(endedAt - windowSeconds * BUCKETS_DRAWN));
      params.set("ended_at", String(endedAt));
      if (scope.appId) params.set("app_id", scope.appId);
      if (scope.stubId) params.set("stub_id", scope.stubId);
      return apiRequest(
        withWorkspace(`/api/v1/tasks/aggregate-by-time-window?${params.toString()}`, workspaceId),
        taskTimeWindowBucketListSchema,
      );
    },
    meta: workspaceLiveQueryMeta(true),
  });
}
