import { infiniteQueryOptions, mutationOptions, queryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";

import {
  LIVE_LIST_MAX_PAGES,
  nextListCursor,
  selectInfiniteList,
  type InfiniteListQueryData,
} from "./infinite-list";
import {
  criticalLiveQueryMeta,
  type WorkloadRef,
  workspaceLiveQueryMeta,
  workspaceQueryKeys,
} from "./workspace-keys";

/** The containers of the workload's releases, newest first; `live` keeps those not stopped. */
export function containersQueryOptions(
  workspace: string,
  workload: WorkloadRef & { id: string },
  { live = false, enabled }: { live?: boolean; enabled?: boolean } = {},
) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.containers.list(workspace, workload, live),
    initialPageParam: "",
    queryFn: async ({ pageParam, signal }) => {
      const page = await ok(
        api.GET("/v1/workspaces/{workspace}/containers", {
          params: {
            path: { workspace },
            query: { workload_id: workload.id, live, limit: 100, cursor: pageParam || undefined },
          },
          signal,
        }),
      );
      return { data: page.containers, next: page.next_cursor ?? "" };
    },
    getNextPageParam: nextListCursor,
    maxPages: LIVE_LIST_MAX_PAGES,
    enabled,
    meta: criticalLiveQueryMeta,
  });
}

export function selectContainerList(
  data: InfiniteListQueryData<Schemas["Container"]> | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectInfiniteList(data, hasNextPage, (container) => container.id);
}

export function containerQueryOptions(workspace: string, containerId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.containers.detail(workspace, containerId),
    queryFn: ({ signal }) =>
      ok(
        api.GET("/v1/workspaces/{workspace}/containers/{container}", {
          params: { path: { workspace, container: containerId } },
          signal,
        }),
      ),
    meta: criticalLiveQueryMeta,
  });
}

export function stopContainerMutationOptions(workspace: string, containerId: string) {
  return mutationOptions({
    mutationFn: () =>
      ok(
        api.POST("/v1/workspaces/{workspace}/containers/{container}/stop", {
          params: { path: { workspace, container: containerId } },
        }),
      ),
  });
}

/** The container's last hour of life, at the finest step the API keeps. */
export function containerMetricsQueryOptions(
  workspace: string,
  containerId: string,
  live: boolean,
) {
  return queryOptions({
    queryKey: workspaceQueryKeys.containers.metrics(workspace, containerId),
    queryFn: ({ signal }) =>
      ok(
        api.GET("/v1/workspaces/{workspace}/containers/{container}/metrics", {
          params: { path: { workspace, container: containerId } },
          signal,
        }),
      ),
    // Hosts sample every 5 seconds; samples are not change events.
    refetchInterval: live ? 5_000 : false,
  });
}

/**
 * When the container waited for a host, each start stage and the drain. A
 * stage changes with the container's state, so the change stream refreshes
 * this and nothing polls it. A container without a record answers null.
 */
export function containerLifecycleQueryOptions(workspace: string, containerId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.containers.lifecycle(workspace, containerId),
    queryFn: async ({ signal }): Promise<Schemas["ContainerLifecycle"] | null> => {
      const page = await ok(
        api.POST("/v1/workspaces/{workspace}/containers/lifecycles", {
          params: { path: { workspace } },
          body: { container_ids: [containerId] },
          signal,
        }),
      );
      return page.lifecycles[0] ?? null;
    },
    meta: workspaceLiveQueryMeta(false),
  });
}

/** The lifecycles of a call graph's containers, up to 200 a call. */
export function containerLifecyclesQueryOptions(
  workspace: string,
  rootTaskId: string,
  containerIds: string[],
  live: boolean,
) {
  const ids = [...new Set(containerIds)].sort();
  return queryOptions({
    queryKey: [...workspaceQueryKeys.tasks.callGraph(workspace, rootTaskId), "lifecycle", ids],
    queryFn: async ({ signal }): Promise<Schemas["ContainerLifecycle"][]> => {
      const batches: string[][] = [];
      for (let index = 0; index < ids.length; index += 200) {
        batches.push(ids.slice(index, index + 200));
      }
      const pages = await Promise.all(
        batches.map((container_ids) =>
          ok(
            api.POST("/v1/workspaces/{workspace}/containers/lifecycles", {
              params: { path: { workspace } },
              body: { container_ids },
              signal,
            }),
          ),
        ),
      );
      return pages.flatMap((page) => page.lifecycles);
    },
    enabled: ids.length > 0,
    meta: workspaceLiveQueryMeta(false, live),
  });
}
