import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";
import type { operations } from "@/lib/api/generated/openapi";

import { LIVE_LIST_MAX_PAGES, nextListCursor, selectInfiniteList } from "./infinite-list";
import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

/** A row of a task list: a task, or an endpoint or ASGI request record. */
export type TaskRow = Schemas["Task"] | Schemas["HttpRequest"];

export function isRequest(row: TaskRow): row is Schemas["HttpRequest"] {
  return "method" in row;
}

/** Endpoint and ASGI workloads serve request records, not tasks. */
export function servesRequests(kind: string): boolean {
  return kind === "endpoint" || kind === "asgi";
}

export function taskFinished(status: Schemas["TaskStatus"]): boolean {
  return status === "succeeded" || status === "failed" || status === "cancelled";
}

/**
 * A request in the task status words. The edge records a request once it
 * ends: failed when the workload answered with a server error, cancelled when
 * the caller left first (499).
 */
export function requestStatus(request: Schemas["HttpRequest"]): Schemas["TaskStatus"] {
  if (request.status === 499) return "cancelled";
  return request.status >= 500 ? "failed" : "succeeded";
}

/** A task failure as the text a person reads: the traceback, else the exception. */
export function failureText(failure: Schemas["TaskFailure"]): string {
  if (failure.traceback) return failure.traceback.trimEnd();
  return failure.type ? `${failure.type}: ${failure.message}` : failure.message;
}

/** The row's status, name and times, read from whichever record it is. */
export function rowFacts(row: TaskRow) {
  if (!isRequest(row)) {
    return {
      name: row.function,
      kind: "function" as Schemas["WorkloadKind"],
      status: row.status,
      createdAt: row.created_at,
      startedAt: row.started_at,
      finishedAt: row.finished_at,
    };
  }
  return {
    name: row.name,
    kind: row.kind,
    status: requestStatus(row),
    createdAt: row.started_at,
    startedAt: row.started_at,
    finishedAt: new Date(Date.parse(row.started_at) + row.duration_ms).toISOString(),
  };
}

const taskPath = (workspace: string, task: string) => ({
  params: { path: { workspace, task } },
});

/** Re-submit a finished task with the same payload as a new task. */
export function rerunTask(workspace: string, taskId: string): Promise<Schemas["Task"]> {
  return ok(api.POST("/v1/workspaces/{workspace}/tasks/{task}/rerun", taskPath(workspace, taskId)));
}

/** Cancel a queued or running task. */
export function cancelTask(workspace: string, taskId: string): Promise<Schemas["Task"]> {
  return ok(
    api.POST("/v1/workspaces/{workspace}/tasks/{task}/cancel", taskPath(workspace, taskId)),
  );
}

type TaskListQuery = NonNullable<operations["listTasks"]["parameters"]["query"]>;

export type TaskFilter = Omit<TaskListQuery, "limit" | "cursor">;

const TASK_PAGE_SIZE = 50;

async function listTasks(
  workspace: string,
  filter: TaskFilter,
  limit: number,
  cursor: string,
  signal: AbortSignal,
): Promise<{ data: Schemas["Task"][]; next: string }> {
  // A function narrows an app, and a version narrows a function.
  const fn = filter.app ? filter.function : undefined;
  const query: TaskListQuery = {
    ...filter,
    function: fn,
    version: fn ? filter.version : undefined,
    search: filter.search?.slice(0, 100) || undefined,
    root_only: filter.root_only || undefined,
    limit,
    cursor: cursor || undefined,
  };
  const page = await ok(
    api.GET("/v1/workspaces/{workspace}/tasks", {
      params: { path: { workspace }, query },
      signal,
    }),
  );
  return { data: page.tasks, next: page.next_cursor ?? "" };
}

function taskListKey(
  workspace: string,
  filter: TaskFilter,
  limit: number,
  mode: "page" | "infinite",
) {
  return workspaceQueryKeys.tasks.list(workspace, {
    mode,
    limit,
    app: filter.app ?? null,
    function: filter.function ?? null,
    status: filter.status ?? null,
    version: filter.version ?? null,
    search: filter.search || null,
    rootOnly: filter.root_only ?? false,
  });
}

/** The newest page of tasks the filter selects. */
export function tasksQueryOptions(
  workspace: string,
  filter: TaskFilter = {},
  limit = TASK_PAGE_SIZE,
) {
  return queryOptions({
    queryKey: taskListKey(workspace, filter, limit, "page"),
    queryFn: async ({ signal }) => (await listTasks(workspace, filter, limit, "", signal)).data,
    meta: workspaceLiveQueryMeta(true),
  });
}

export function tasksInfiniteQueryOptions(
  workspace: string,
  filter: TaskFilter = {},
  limit = TASK_PAGE_SIZE,
) {
  return infiniteQueryOptions({
    queryKey: taskListKey(workspace, filter, limit, "infinite"),
    initialPageParam: "",
    queryFn: ({ pageParam, signal }) => listTasks(workspace, filter, limit, pageParam, signal),
    getNextPageParam: nextListCursor,
    maxPages: LIVE_LIST_MAX_PAGES,
    meta: workspaceLiveQueryMeta(true),
  });
}

export function selectTaskList(
  data: { pages: readonly { data: readonly Schemas["Task"][]; next: string }[] } | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectInfiniteList(data, hasNextPage, (task) => task.id);
}

/**
 * The newest requests one endpoint or ASGI workload served. The change stream
 * carries no request records, and the edge writes one about a second after
 * the request ends, so the list is reread while it is shown.
 */
export function requestsQueryOptions(workspace: string, app: string, name: string, limit = 50) {
  return queryOptions({
    queryKey: workspaceQueryKeys.tasks.requests(workspace, app, name, limit),
    queryFn: async ({ signal }) =>
      (
        await ok(
          api.GET("/v1/workspaces/{workspace}/apps/{app}/requests", {
            params: { path: { workspace, app }, query: { name, limit } },
            signal,
          }),
        )
      ).data,
    refetchInterval: 10_000,
    meta: workspaceLiveQueryMeta(true),
  });
}

export type TaskDetail = {
  task: Schemas["Task"];
  result: Schemas["Payload"] | null;
};

export function taskQueryOptions(workspace: string, taskId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.tasks.detail(workspace, taskId),
    // A running task is read with the API's wait, which answers as soon as it
    // finishes; the change stream refreshes a queued one when it starts.
    queryFn: async ({ client, queryKey, signal }): Promise<TaskDetail> => {
      const known = client.getQueryData<TaskDetail>(queryKey);
      const task = await ok(
        api.GET("/v1/workspaces/{workspace}/tasks/{task}", {
          params: {
            path: { workspace, task: taskId },
            query: { wait_seconds: known?.task.status === "running" ? 25 : 0 },
          },
          signal,
        }),
      );
      const result =
        task.status === "succeeded"
          ? await ok(
              api.GET("/v1/workspaces/{workspace}/tasks/{task}/result", {
                ...taskPath(workspace, taskId),
                signal,
              }),
            )
          : null;
      return { task, result };
    },
    refetchInterval: (query) => (query.state.data?.task.status === "running" ? 1_000 : false),
    // Rate-limited drawer reads preserve their explicit Retry-After recovery
    // instead of being retried by an unrelated workspace reconnect.
    retry: false,
    meta: workspaceLiveQueryMeta(true, true, false),
  });
}

/** An endpoint or ASGI request record, which is complete once it exists. */
export function requestQueryOptions(workspace: string, requestId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.tasks.request(workspace, requestId),
    queryFn: ({ signal }) =>
      ok(
        api.GET("/v1/workspaces/{workspace}/requests/{http_request}", {
          params: { path: { workspace, http_request: requestId } },
          signal,
        }),
      ),
    retry: false,
    meta: workspaceLiveQueryMeta(false),
  });
}

export type CallGraphNode = Schemas["CallGraphNode"] & { children: CallGraphNode[] };

/**
 * The task's call graph as trees: each task under the task that spawned it,
 * oldest first. A graph past the API's 2,000 tasks ends there.
 */
export function callGraphQueryOptions(workspace: string, rootTaskId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.tasks.callGraph(workspace, rootTaskId),
    queryFn: async ({ signal }): Promise<CallGraphNode[]> => {
      const graph = await ok(
        api.GET("/v1/workspaces/{workspace}/tasks/{task}/call-graph", {
          ...taskPath(workspace, rootTaskId),
          signal,
        }),
      );
      const nodes = new Map<string, CallGraphNode>();
      const roots: CallGraphNode[] = [];
      for (const node of graph.nodes) {
        const tree: CallGraphNode = { ...node, children: [] };
        nodes.set(node.task_id, tree);
        // Parents precede their children, so a parent not seen is outside the graph.
        const parent = node.parent_task_id ? nodes.get(node.parent_task_id) : undefined;
        if (parent) parent.children.push(tree);
        else roots.push(tree);
      }
      return roots;
    },
    meta: workspaceLiveQueryMeta(true),
  });
}

export function taskMetricsQueryOptions(workspace: string, hours = 24, app?: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.tasks.metrics(workspace, hours, app ?? null),
    queryFn: ({ signal }) =>
      ok(
        api.GET("/v1/workspaces/{workspace}/metrics/tasks", {
          params: {
            path: { workspace },
            query: { start: new Date(Date.now() - hours * 3_600_000).toISOString(), app },
          },
          signal,
        }),
      ),
    meta: workspaceLiveQueryMeta(true),
  });
}
