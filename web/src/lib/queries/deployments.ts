import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { ApiError, api, ok, type Schemas } from "@/lib/api/client";

import { nextListCursor, selectInfiniteList, type InfiniteListQueryData } from "./infinite-list";
import { workspaceLiveQueryMeta, workspaceQueryKeys, type WorkloadRef } from "./workspace-keys";

/** Every page of a cursor listing, for a list that answers a whole view. */
export async function allPages<T>(
  read: (cursor: string | undefined) => Promise<{ items: T[]; next?: string }>,
): Promise<T[]> {
  const items: T[] = [];
  let cursor: string | undefined;
  do {
    const page = await read(cursor);
    items.push(...page.items);
    cursor = page.next;
  } while (cursor);
  return items;
}

/** Deployed workloads, by app and name, of the workspace or one app. */
export function listWorkloads(workspace: string, app?: string): Promise<Schemas["Workload"][]> {
  return allPages(async (cursor) => {
    const page = await ok(
      api.GET("/v1/workspaces/{workspace}/workloads", {
        params: { path: { workspace }, query: { app, limit: 1000, cursor } },
      }),
    );
    return { items: page.workloads, next: page.next_cursor };
  });
}

/** Deployed workloads of the workspace, or of one app, newest deploy first. */
export function workloadsQueryOptions(workspace: string, app?: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.workloads.list(workspace, app ?? null),
    queryFn: async () => newestFirst(await listWorkloads(workspace, app)),
    meta: workspaceLiveQueryMeta(true),
  });
}

export function deployedAt(workload: Schemas["Workload"]): string {
  return workload.deployed_at ?? workload.created_at;
}

export function newestFirst(workloads: Schemas["Workload"][]): Schemas["Workload"][] {
  return [...workloads].sort((left, right) => deployedAt(right).localeCompare(deployedAt(left)));
}

/** Whether the workload admits work: started, in a running app, on a deployed version. */
export function workloadRunning(workload: Schemas["Workload"]): boolean {
  return workload.state === "active" && workload.app_state !== "paused";
}

/** Whether the app deploys no live workload of that kind and name. */
export function workloadNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

const workloadPath = (workspace: string, { app, kind, name }: WorkloadRef) => ({
  workspace,
  app,
  kind,
  name,
});

/** One deployed workload, the definition its active version runs and where it answers. */
export function workloadQueryOptions(workspace: string, workload: WorkloadRef) {
  return queryOptions({
    queryKey: workspaceQueryKeys.workloads.detail(workspace, workload),
    queryFn: ({ signal }) =>
      ok(
        api.GET("/v1/workspaces/{workspace}/apps/{app}/workloads/{kind}/{name}", {
          params: { path: workloadPath(workspace, workload) },
          signal,
        }),
      ),
    retry: (failures, error) => !workloadNotFound(error) && failures < 2,
    meta: workspaceLiveQueryMeta(true),
  });
}

/**
 * Where the workload answers callers: an endpoint or ASGI app on its own
 * host, a function on this origin's invoke operation.
 */
export function invokeUrl(workspace: string, { workload, http }: Schemas["WorkloadDetail"]) {
  if (http) return http.url;
  const [ws, app, name] = [workspace, workload.app, workload.name].map(encodeURIComponent);
  return `${window.location.origin}/v1/workspaces/${ws}/apps/${app}/workloads/function/${name}/invoke`;
}

/** The workload's deployed versions, newest first. */
export function versionsInfiniteQueryOptions(workspace: string, workload: WorkloadRef) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.workloads.versions(workspace, workload),
    initialPageParam: "",
    queryFn: async ({ pageParam }) => {
      const page = await ok(
        api.GET("/v1/workspaces/{workspace}/apps/{app}/workloads/{kind}/{name}/versions", {
          params: {
            path: workloadPath(workspace, workload),
            query: { limit: 100, cursor: pageParam || undefined },
          },
        }),
      );
      return { data: page.versions, next: page.next_cursor ?? "" };
    },
    getNextPageParam: nextListCursor,
    meta: workspaceLiveQueryMeta(true),
  });
}

export function selectVersionList(
  data: InfiniteListQueryData<Schemas["Version"]> | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectInfiniteList(data, hasNextPage, (version) => String(version.version));
}

/** Run time percentiles, outcomes and cold starts per hour over the last day. */
export function performanceQueryOptions(
  workspace: string,
  workload: WorkloadRef,
  windowSeconds = 3600,
) {
  return queryOptions({
    queryKey: workspaceQueryKeys.workloads.performance(workspace, workload, windowSeconds),
    queryFn: () =>
      ok(
        api.GET("/v1/workspaces/{workspace}/apps/{app}/workloads/{kind}/{name}/performance", {
          params: {
            path: workloadPath(workspace, workload),
            query: { window_seconds: windowSeconds },
          },
        }),
      ),
    meta: workspaceLiveQueryMeta(true),
  });
}

/** Starts the workload, on `version` when one is named, which then becomes the active one. */
export function startWorkloadMutationOptions(
  workspace: string,
  workload: WorkloadRef,
  version?: number,
) {
  return {
    mutationFn: () =>
      ok(
        api.POST("/v1/workspaces/{workspace}/apps/{app}/workloads/{kind}/{name}/start", {
          params: { path: workloadPath(workspace, workload) },
          body: version === undefined ? {} : { version },
        }),
      ),
  };
}

export function stopWorkloadMutationOptions(workspace: string, workload: WorkloadRef) {
  return {
    mutationFn: () =>
      ok(
        api.POST("/v1/workspaces/{workspace}/apps/{app}/workloads/{kind}/{name}/stop", {
          params: { path: workloadPath(workspace, workload) },
        }),
      ),
  };
}

/** Deletes the workload with every version of it. */
export function deleteWorkloadMutationOptions(workspace: string, workload: WorkloadRef) {
  return {
    mutationFn: () =>
      ok(
        api.DELETE("/v1/workspaces/{workspace}/apps/{app}/workloads/{kind}/{name}", {
          params: { path: workloadPath(workspace, workload) },
        }),
      ),
  };
}

/** Holds a pod at a number of containers until the next scale. */
export function scaleWorkloadMutationOptions(workspace: string, workload: WorkloadRef) {
  return {
    mutationFn: (containers: number) =>
      ok(
        api.POST("/v1/workspaces/{workspace}/apps/{app}/workloads/{kind}/{name}/scale", {
          params: { path: workloadPath(workspace, workload) },
          body: { containers },
        }),
      ),
  };
}

const SETTLED_DEVBOX_PHASES = new Set<Schemas["DevboxPhase"]>(["running", "stopped", "failed"]);
const SETTLED_DEVBOX_REFRESH_MS = 15_000;
const CHANGING_DEVBOX_REFRESH_MS = 3_000;

/** A devbox is always a pod, so its app and name address it. */
type DevboxRef = Pick<WorkloadRef, "app" | "name">;

const devboxPath = (workspace: string, { app, name }: DevboxRef) => ({ workspace, app, name });

/**
 * A devbox's status. Connections and the idle deadline change without a
 * workspace change, so it refreshes on an interval: quickly while the devbox
 * is changing state or the caller waits for a change it asked for, slowly
 * once it has settled.
 */
export function devboxQueryOptions(
  workspace: string,
  devbox: DevboxRef,
  { awaitingChange = false }: { awaitingChange?: boolean } = {},
) {
  return queryOptions({
    queryKey: workspaceQueryKeys.workloads.devbox(workspace, devbox),
    queryFn: ({ signal }) =>
      ok(
        api.GET("/v1/workspaces/{workspace}/apps/{app}/workloads/pod/{name}/devbox", {
          params: { path: devboxPath(workspace, devbox) },
          signal,
        }),
      ),
    refetchInterval: (query) =>
      !awaitingChange && query.state.data && SETTLED_DEVBOX_PHASES.has(query.state.data.phase)
        ? SETTLED_DEVBOX_REFRESH_MS
        : CHANGING_DEVBOX_REFRESH_MS,
    meta: workspaceLiveQueryMeta(false),
  });
}

/** Starts the devbox now; the server answers with its status without waiting. */
export function startDevboxMutationOptions(workspace: string, devbox: DevboxRef) {
  return {
    mutationFn: () =>
      ok(
        api.POST("/v1/workspaces/{workspace}/apps/{app}/workloads/pod/{name}/devbox/start", {
          params: { path: devboxPath(workspace, devbox) },
        }),
      ),
  };
}

/** Stops the devbox's container now; its workload stays on. */
export function stopDevboxMutationOptions(workspace: string, devbox: DevboxRef) {
  return {
    mutationFn: () =>
      ok(
        api.POST("/v1/workspaces/{workspace}/apps/{app}/workloads/pod/{name}/devbox/stop", {
          params: { path: devboxPath(workspace, devbox) },
        }),
      ),
  };
}
