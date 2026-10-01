import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";
import { isTerminalTaskStatus } from "@/lib/format";

import { LIVE_LIST_MAX_PAGES, nextPageCursor, selectPages } from "./infinite-list";
import { workspaceQueryKeys } from "./workspace-keys";

export type Task = Schemas["Task"];
export type TaskStatus = Schemas["TaskStatus"];

export type TaskListOptions = {
  limit?: number;
  app?: string;
  /** Requires `app`. */
  function?: string;
  status?: TaskStatus;
};

function listQuery(workspace: string, options: TaskListOptions, cursor: string) {
  return ok(
    api.GET("/v1/workspaces/{workspace}/tasks", {
      params: {
        path: { workspace },
        query: {
          app: options.app,
          function: options.app ? options.function : undefined,
          status: options.status,
          limit: options.limit ?? 50,
          cursor: cursor || undefined,
        },
      },
    }),
  );
}

function listKey(workspace: string, options: TaskListOptions, mode: "page" | "infinite") {
  return workspaceQueryKeys.tasks.list(workspace, {
    mode,
    limit: options.limit ?? 50,
    app: options.app ?? null,
    function: options.function ?? null,
    status: options.status ?? null,
  });
}

/** The newest tasks, one page. */
export function tasksQueryOptions(workspace: string, options: TaskListOptions = {}) {
  return queryOptions({
    queryKey: listKey(workspace, options, "page"),
    queryFn: () => listQuery(workspace, options, ""),
  });
}

/** Tasks newest first, a page at a time. */
export function tasksInfiniteQueryOptions(workspace: string, options: TaskListOptions = {}) {
  return infiniteQueryOptions({
    queryKey: listKey(workspace, options, "infinite"),
    initialPageParam: "",
    queryFn: ({ pageParam }) => listQuery(workspace, options, pageParam),
    getNextPageParam: nextPageCursor,
    maxPages: LIVE_LIST_MAX_PAGES,
  });
}

export function selectTaskList(
  data: { pages: readonly Schemas["TaskPage"][] } | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectPages(
    data,
    (page) => page.tasks,
    hasNextPage,
    (task) => task.id,
  );
}

/** How long a read of a running task waits for it to finish. */
const FINISH_WAIT_SECONDS = 25;
/** How soon a queued task is read again to see it start. */
const QUEUED_REFRESH_MS = 2_000;

/**
 * One task, kept current while it is open. A running task is read with the
 * API's wait, which answers as soon as it finishes; a queued task is read
 * again shortly, because the wait only ends on a finish and a start is what
 * the drawer is showing next.
 */
export function taskQueryOptions(workspace: string, task: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.tasks.detail(workspace, task),
    queryFn: ({ client, queryKey, signal }) => {
      const known = client.getQueryData<Task>(queryKey);
      const wait = known?.status === "running" ? FINISH_WAIT_SECONDS : 0;
      return ok(
        api.GET("/v1/workspaces/{workspace}/tasks/{task}", {
          params: { path: { workspace, task }, query: { wait_seconds: wait } },
          signal,
        }),
      );
    },
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (!status || isTerminalTaskStatus(status)) return false;
      return status === "running" ? 1_000 : QUEUED_REFRESH_MS;
    },
    retry: false,
  });
}

/** The value a succeeded task returned. */
export function taskResultQueryOptions(workspace: string, task: string, enabled: boolean) {
  return queryOptions({
    queryKey: workspaceQueryKeys.tasks.result(workspace, task),
    queryFn: () =>
      ok(
        api.GET("/v1/workspaces/{workspace}/tasks/{task}/result", {
          params: { path: { workspace, task } },
        }),
      ),
    enabled,
    staleTime: Infinity,
  });
}

const taskPath = (workspace: string, task: string) => ({ params: { path: { workspace, task } } });

export function cancelTask(workspace: string, task: string): Promise<Task> {
  return ok(api.POST("/v1/workspaces/{workspace}/tasks/{task}/cancel", taskPath(workspace, task)));
}

/** Submit the task's input again, as a new task on the release it ran on. */
export function rerunTask(workspace: string, task: string): Promise<Task> {
  return ok(api.POST("/v1/workspaces/{workspace}/tasks/{task}/rerun", taskPath(workspace, task)));
}

/** Admit one task with JSON arguments against the function's active release. */
export async function submitJsonTask(
  workspace: string,
  app: string,
  name: string,
  args: { args: unknown[]; kwargs: Record<string, unknown> },
): Promise<Task> {
  const response = await ok(
    api.POST("/v1/workspaces/{workspace}/apps/{app}/functions/{function}/tasks", {
      params: { path: { workspace, app, function: name } },
      body: { inputs: [{ encoding: "json", value: args }] },
    }),
  );
  return response.tasks[0];
}
