import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";
import { LIVE_LIST_MAX_PAGES } from "./infinite-list";

import type { QueryClient } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";
import type {
  CallGraph,
  CallGraphNode,
  Task,
  TaskMetricsSummary,
  TaskSummary,
  TaskTimeWindowBucket,
} from "@/lib/api/schemas";
import {
  apiTaskStatus,
  parseDeploymentId,
  parseStubId,
  viewContainer,
  viewRequest,
  viewRequestSummary,
  viewResult,
  viewStatus,
  viewStatusCounts,
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
  const workloads = await workloadDirectory(client, workspaceId);
  let app = options.appId ? (await appById(client, workspaceId, options.appId)).name : undefined;
  let fn: string | undefined;
  let version: number | null = null;
  let kind = options.kind;
  if (options.deploymentId) {
    const parsed = parseDeploymentId(options.deploymentId);
    const workload = workloads.byId.get(parsed.workload);
    app = workload?.app ?? app;
    fn = workload?.name;
    kind = workload?.kind ?? kind;
    version = parsed.version;
  } else if (options.stubIds?.length) {
    const stub = parseStubId(options.stubIds[0]);
    app = stub.app;
    fn = stub.name;
    kind = workloads.byName.get(`${stub.app}/${stub.name}`)?.kind ?? kind;
  }
  // Endpoint and ASGI requests are the edge's request records, not tasks.
  if (kind === "endpoint" || kind === "asgi") {
    return listRequests(client, workspaceId, options, { app, name: fn, kind, version }, cursor);
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

/**
 * One page of endpoint or ASGI requests as task rows. Requests are listed per
 * app, so a workspace-wide list reads the newest page of each app that serves
 * the kind and ends there.
 */
async function listRequests(
  client: QueryClient,
  workspaceId: string,
  options: TaskListOptions,
  target: { app?: string; name?: string; kind: string; version: number | null },
  cursor: string,
): Promise<{ data: TaskSummary[]; next: string }> {
  if (options.status && !["complete", "failed", "cancelled"].includes(options.status)) {
    return { data: [], next: "" };
  }
  const workspace = workspaceName(workspaceId);
  const [apps, workloads] = await Promise.all([
    appDirectory(client, workspaceId),
    workloadDirectory(client, workspaceId),
  ]);
  const limit = options.limit ?? 50;
  const appNames = target.app
    ? [target.app]
    : [
        ...new Set(
          [...workloads.byId.values()]
            .filter((workload) => workload.kind === target.kind)
            .map((workload) => workload.app),
        ),
      ];
  const pages = await Promise.all(
    appNames.map((app) =>
      ok(
        api.GET("/v1/workspaces/{workspace}/apps/{app}/requests", {
          params: {
            path: { workspace, app },
            query: { name: target.name, limit, before: cursor || undefined },
          },
        }),
      ),
    ),
  );
  const search = options.search?.toLowerCase();
  const requests = pages
    .flatMap((page) => page.data)
    .filter((request) => target.app || request.kind === target.kind)
    .filter((request) => target.version === null || request.version === target.version)
    .filter(
      (request) =>
        !search ||
        request.id.startsWith(search) ||
        request.name.toLowerCase().includes(search) ||
        request.path.toLowerCase().includes(search),
    )
    .sort((left, right) => right.started_at.localeCompare(left.started_at))
    .slice(0, limit);
  const data = requests
    .map((request) =>
      viewRequestSummary(
        request,
        workspaceId,
        apps.byName.get(request.app)?.id ?? null,
        workloads.byName.get(`${request.app}/${request.name}`)?.id ?? null,
      ),
    )
    .filter((request) => !options.status || request.status === options.status);
  return { data, next: (target.app && pages[0]?.next) || "" };
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
    // finishes; the change stream refreshes a queued one when it starts.
    queryFn: async ({ client, queryKey, signal }): Promise<Task> => {
      const known = client.getQueryData<Task>(queryKey);
      const read = await api.GET("/v1/workspaces/{workspace}/tasks/{task}", {
        ...taskPath(workspaceId, taskId),
        params: {
          ...taskPath(workspaceId, taskId).params,
          query: { wait_seconds: known?.status === "running" ? 25 : 0 },
        },
        signal,
      });
      // An endpoint row opens its request record, which is not a task.
      if (read.response.status === 404) return requestDetail(client, workspaceId, taskId, signal);
      const task = await ok(Promise.resolve(read));
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
    refetchInterval: (query) => (query.state.data?.status === "running" ? 1_000 : false),
    // Rate-limited drawer reads preserve their explicit Retry-After recovery
    // instead of being retried by an unrelated workspace reconnect.
    retry: false,
    meta: workspaceLiveQueryMeta(true, true, false),
  });
}

async function requestDetail(
  client: QueryClient,
  workspaceId: string,
  requestId: string,
  signal: AbortSignal,
): Promise<Task> {
  const workspace = workspaceName(workspaceId);
  const request = await ok(
    api.GET("/v1/workspaces/{workspace}/requests/{http_request}", {
      params: { path: { workspace, http_request: requestId } },
      signal,
    }),
  );
  const [apps, workloads] = await Promise.all([
    appDirectory(client, workspaceId),
    workloadDirectory(client, workspaceId),
  ]);
  const appId = apps.byName.get(request.app)?.id ?? null;
  const container = request.container_id
    ? await ok(
        api.GET("/v1/workspaces/{workspace}/containers/{container}", {
          params: { path: { workspace, container: request.container_id } },
          signal,
        }),
      ).then((found) => viewContainer(found, workspaceId, appId))
    : null;
  return viewRequest(
    request,
    workspaceId,
    appId,
    workloads.byName.get(`${request.app}/${request.name}`)?.id ?? null,
    container,
  );
}

/**
 * The task's call graph as trees: each task under the task that spawned it,
 * oldest first. A graph past the API's 2,000 tasks ends there.
 */
export function callGraphQueryOptions(workspaceId: string, taskId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.tasks.callGraph(workspaceId, taskId),
    queryFn: async (): Promise<CallGraph> =>
      viewCallGraph(
        await ok(
          api.GET(
            "/v1/workspaces/{workspace}/tasks/{task}/call-graph",
            taskPath(workspaceId, taskId),
          ),
        ),
      ),
    meta: workspaceLiveQueryMeta(true),
  });
}

function viewCallGraph(graph: Schemas["TaskCallGraph"]): CallGraph {
  const nodes = new Map<string, CallGraphNode>();
  const roots: CallGraphNode[] = [];
  for (const node of graph.nodes) {
    const view: CallGraphNode = {
      task_id: node.task_id,
      container_id: node.container_id ?? null,
      parent_task_id: node.parent_task_id ?? "",
      root_task_id: graph.root_task_id,
      status: viewStatus(node.status),
      name: node.function,
      function_name: node.function,
      created_at: node.created_at,
      started_at: node.started_at ?? null,
      finished_at: node.finished_at ?? null,
      dependencies: node.depends_on,
      children: [],
    };
    nodes.set(node.task_id, view);
    // Parents precede their children, so a parent not seen is outside the graph.
    const parent = node.parent_task_id ? nodes.get(node.parent_task_id) : undefined;
    if (parent) parent.children.push(view);
    else roots.push(view);
  }
  return {
    root_task_id: graph.root_task_id,
    root: nodes.get(graph.root_task_id) ?? null,
    nodes: roots,
  };
}

/** The app an aggregate is narrowed to, by the name the API filters on. */
async function appName(
  client: QueryClient,
  workspaceId: string,
  appId: string | undefined,
): Promise<string | undefined> {
  return appId ? (await appById(client, workspaceId, appId)).name : undefined;
}

export function taskMetricsQueryOptions(workspaceId: string, hours = 24, appId?: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.tasks.metrics(workspaceId, hours, appId ?? null),
    queryFn: async ({ client }): Promise<TaskMetricsSummary> => {
      const metrics = await ok(
        api.GET("/v1/workspaces/{workspace}/metrics/tasks", {
          params: {
            path: { workspace: workspaceName(workspaceId) },
            query: {
              start: new Date(Date.now() - hours * 3_600_000).toISOString(),
              app: await appName(client, workspaceId, appId),
            },
          },
        }),
      );
      return {
        total: metrics.total,
        status_counts: viewStatusCounts(metrics.status_counts),
        completed: metrics.status_counts.succeeded,
        failed: metrics.status_counts.failed,
        cancelled: metrics.status_counts.cancelled,
        failure_rate: metrics.failure_rate,
        average_runtime_ms: metrics.average_runtime_ms ?? null,
        runtime_ms_p50: metrics.runtime_ms_p50 ?? null,
        runtime_ms_p95: metrics.runtime_ms_p95 ?? null,
        runtime_ms_p99: metrics.runtime_ms_p99 ?? null,
        startup_ms_p50: metrics.startup_ms_p50 ?? null,
        startup_ms_p95: metrics.startup_ms_p95 ?? null,
      };
    },
    meta: workspaceLiveQueryMeta(true),
  });
}

/** Buckets the activity chart renders; it slices to this and discards the rest. */
const BUCKETS_DRAWN = 24;

/**
 * Tasks submitted per bucket over the `BUCKETS_DRAWN` buckets that end with
 * the current one: one series per app or, for one app, per function.
 */
export function taskActivity(
  workspaceId: string,
  windowSeconds: number,
  app?: string,
): Promise<Schemas["TaskActivity"]> {
  // The span is computed per fetch and left out of the query key: in the key
  // every refetch would be a new key and nothing would be served from cache.
  const windowMs = windowSeconds * 1000;
  const current = Math.floor(Date.now() / windowMs) * windowMs;
  return ok(
    api.GET("/v1/workspaces/{workspace}/metrics/activity", {
      params: {
        path: { workspace: workspaceName(workspaceId) },
        query: {
          window_seconds: windowSeconds,
          start: new Date(current - (BUCKETS_DRAWN - 1) * windowMs).toISOString(),
          app,
        },
      },
    }),
  );
}

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
    queryFn: async ({ client }): Promise<{ items: TaskTimeWindowBucket[] }> => {
      const stub = scope.stubId ? parseStubId(scope.stubId) : undefined;
      const app = stub?.app ?? (await appName(client, workspaceId, scope.appId));
      const activity = await taskActivity(workspaceId, windowSeconds, app);
      const byTime = new Map<string, Schemas["TaskStatusCounts"]>();
      for (const series of activity.series) {
        if (stub && series.function !== stub.name) continue;
        for (const bucket of series.buckets) {
          const sum = byTime.get(bucket.timestamp);
          byTime.set(
            bucket.timestamp,
            sum ? addCounts(sum, bucket.status_counts) : bucket.status_counts,
          );
        }
      }
      return {
        items: [...byTime].map(([timestamp, counts]) => ({
          timestamp,
          count: countTotal(counts),
          status_counts: viewStatusCounts(counts),
        })),
      };
    },
    meta: workspaceLiveQueryMeta(true),
  });
}

function addCounts(
  left: Schemas["TaskStatusCounts"],
  right: Schemas["TaskStatusCounts"],
): Schemas["TaskStatusCounts"] {
  return {
    queued: left.queued + right.queued,
    running: left.running + right.running,
    succeeded: left.succeeded + right.succeeded,
    failed: left.failed + right.failed,
    cancelled: left.cancelled + right.cancelled,
  };
}

export function countTotal(counts: Schemas["TaskStatusCounts"]): number {
  return counts.queued + counts.running + counts.succeeded + counts.failed + counts.cancelled;
}
