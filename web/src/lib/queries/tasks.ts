import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";
import { LIVE_LIST_MAX_PAGES } from "./infinite-list";

import type { QueryClient } from "@tanstack/react-query";

import { api, apiRequest, ok, withWorkspace, type Schemas } from "@/lib/api/client";
import {
  callGraphSchema,
  taskMetricsSummarySchema,
  taskTimeWindowBucketListSchema,
  type Task,
  type TaskSummary,
} from "@/lib/api/schemas";
import {
  apiTaskStatus,
  parseDeploymentId,
  parseStubId,
  viewContainer,
  viewResult,
  viewTask,
  viewTaskSummary,
} from "@/lib/api/views";
import { workspaceName } from "@/lib/api/workspaces";

import { appById, appDirectory, workloadDirectory } from "./directory";

import { selectInfiniteList, type InfiniteListQueryData } from "./infinite-list";
import {
  workspaceLiveQueryMeta,
  workspaceQueryKeys,
  type TaskListKeyParts,
} from "./workspace-keys";

const taskPath = (workspaceId: string, taskId: string) => ({
  params: { path: { workspace: workspaceName(workspaceId), task: taskId } },
});

/** A task in the dashboard's view, with the IDs the API names by app and function. */
async function viewTaskRow(
  client: QueryClient,
  workspaceId: string,
  task: Schemas["Task"],
): Promise<TaskSummary> {
  const [apps, workloads] = await Promise.all([
    appDirectory(client, workspaceId),
    workloadDirectory(client, workspaceId),
  ]);
  return viewTaskSummary(
    task,
    workspaceId,
    apps.byName.get(task.app)?.id ?? null,
    workloads.byName.get(`${task.app}/${task.function}`)?.id ?? null,
  );
}

/** Re-submit a finished task with the same payload as a new task. */
export async function rerunTask(workspaceId: string, taskId: string): Promise<Task> {
  const task = await ok(
    api.POST("/v1/workspaces/{workspace}/tasks/{task}/rerun", taskPath(workspaceId, taskId)),
  );
  return { ...viewTaskSummary(task, workspaceId, null, null), command: [], args: [], kwargs: {} };
}

/** Cancel a queued or running task. */
export async function cancelTask(workspaceId: string, taskId: string): Promise<Task> {
  const task = await ok(
    api.POST("/v1/workspaces/{workspace}/tasks/{task}/cancel", taskPath(workspaceId, taskId)),
  );
  return { ...viewTaskSummary(task, workspaceId, null, null), command: [], args: [], kwargs: {} };
}

/**
 * One page of tasks. The API filters by app, function and status; a version,
 * a root-only view and a search narrow the page here.
 */
async function listTasks(
  client: QueryClient,
  workspaceId: string,
  options: TaskListOptions,
  cursor: string,
): Promise<{ data: TaskSummary[]; next: string }> {
  const workspace = workspaceName(workspaceId);
  let app = options.appId ? (await appById(client, workspaceId, options.appId)).name : undefined;
  let fn: string | undefined;
  let version: number | null = null;
  if (options.deploymentId) {
    const parsed = parseDeploymentId(options.deploymentId);
    const workload = (await workloadDirectory(client, workspaceId)).byId.get(parsed.workload);
    app = workload?.app ?? app;
    fn = workload?.name;
    version = parsed.version;
  } else if (options.stubIds?.length) {
    const stub = parseStubId(options.stubIds[0]);
    app = stub.app;
    fn = stub.name;
  }
  const page = await ok(
    api.GET("/v1/workspaces/{workspace}/tasks", {
      params: {
        path: { workspace },
        query: {
          app,
          function: app ? fn : undefined,
          status: apiTaskStatus(options.status),
          limit: options.limit ?? 50,
          cursor: cursor || undefined,
        },
      },
    }),
  );
  const search = options.search?.toLowerCase();
  const tasks = page.tasks
    .filter((task) => version === null || task.version === version)
    .filter((task) => !options.rootOnly || !task.parent_task_id)
    .filter(
      (task) =>
        !search || task.id.startsWith(search) || task.function.toLowerCase().includes(search),
    );
  const data = await Promise.all(tasks.map((task) => viewTaskRow(client, workspaceId, task)));
  return {
    data: data.filter((task) => !options.status || task.status === options.status),
    next: page.next_cursor ?? "",
  };
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
  return queryOptions({
    queryKey: taskListKey(workspaceId, options, "page"),
    queryFn: ({ client }) => listTasks(client, workspaceId, options, ""),
    meta: workspaceLiveQueryMeta(true, options.live !== false),
  });
}

export function tasksInfiniteQueryOptions(workspaceId: string, options: TaskListOptions = {}) {
  return infiniteQueryOptions({
    queryKey: taskListKey(workspaceId, options, "infinite"),
    initialPageParam: "",
    queryFn: ({ pageParam, client }) => listTasks(client, workspaceId, options, pageParam),
    getNextPageParam: (page) => page.next || undefined,
    maxPages: LIVE_LIST_MAX_PAGES,
    meta: workspaceLiveQueryMeta(true, options.live !== false),
  });
}

export function selectTaskList(
  data: InfiniteListQueryData<TaskSummary> | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectInfiniteList(data, hasNextPage, (task) => task.id);
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
    // A running task is read with the API's wait, which answers as soon as it
    // finishes; a queued one is read again shortly to see it start.
    queryFn: async ({ client, queryKey, signal }): Promise<Task> => {
      const known = client.getQueryData<Task>(queryKey);
      const task = await ok(
        api.GET("/v1/workspaces/{workspace}/tasks/{task}", {
          ...taskPath(workspaceId, taskId),
          params: {
            ...taskPath(workspaceId, taskId).params,
            query: { wait_seconds: known?.status === "running" ? 25 : 0 },
          },
          signal,
        }),
      );
      const [apps, workloads] = await Promise.all([
        appDirectory(client, workspaceId),
        workloadDirectory(client, workspaceId),
      ]);
      const appId = apps.byName.get(task.app)?.id ?? null;
      const [result, container] = await Promise.all([
        task.status === "succeeded"
          ? ok(
              api.GET(
                "/v1/workspaces/{workspace}/tasks/{task}/result",
                taskPath(workspaceId, taskId),
              ),
            ).then(viewResult)
          : null,
        task.container_id
          ? ok(
              api.GET("/v1/workspaces/{workspace}/containers/{container}", {
                params: {
                  path: { workspace: workspaceName(workspaceId), container: task.container_id },
                },
              }),
            ).then((found) => viewContainer(found, workspaceId, appId))
          : null,
      ]);
      return viewTask(
        task,
        workspaceId,
        appId,
        workloads.byName.get(`${task.app}/${task.function}`)?.id ?? null,
        { result, container },
      );
    },
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (status === "running") return 1_000;
      return status === "pending" || status === "retry" ? 2_000 : false;
    },
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
