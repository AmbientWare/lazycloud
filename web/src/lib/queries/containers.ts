import { infiniteQueryOptions, mutationOptions, queryOptions } from "@tanstack/react-query";

import { ApiError, api, ok, type Schemas } from "@/lib/api/client";
import type { ContainerDetail } from "@/lib/api/schemas";
import { viewActiveDeployment, viewApp, viewContainer, viewStub } from "@/lib/api/views";
import { workspaceName } from "@/lib/api/workspaces";

import { appDirectory, workloadDirectory } from "./directory";
import {
  LIVE_LIST_MAX_PAGES,
  nextListCursor,
  selectInfiniteList,
  type InfiniteListQueryData,
} from "./infinite-list";
import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

export type ContainerFilter = {
  /** Only the containers of this app's workloads. */
  app?: string;
  /** Only this workload's containers; requires `app`. */
  function?: string;
  /** Only containers that have not stopped. */
  live?: boolean;
  enabled?: boolean;
};

export function containersQueryOptions(workspace: string, filter: ContainerFilter = {}) {
  const live = filter.live ?? false;
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.containers.list(workspace, {
      app: filter.app ?? null,
      function: filter.function ?? null,
      live,
    }),
    initialPageParam: "",
    queryFn: async ({ pageParam, signal }) => {
      const page = await ok(
        api.GET("/v1/workspaces/{workspace}/containers", {
          params: {
            path: { workspace },
            query: {
              app: filter.app,
              function: filter.app ? filter.function : undefined,
              live,
              limit: 100,
              cursor: pageParam || undefined,
            },
          },
          signal,
        }),
      );
      return { data: page.containers, next: page.next_cursor ?? "" };
    },
    getNextPageParam: nextListCursor,
    maxPages: LIVE_LIST_MAX_PAGES,
    enabled: filter.enabled,
    meta: workspaceLiveQueryMeta(true),
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
    meta: workspaceLiveQueryMeta(true),
  });
}

/**
 * A container in the reference's detail shape, which the sandbox and pod
 * pages read until the workloads packet rewrites them.
 */
export function containerDetailQueryOptions(workspaceId: string, containerId: string) {
  return queryOptions({
    queryKey: [
      ...workspaceQueryKeys.containers.detail(workspaceId, containerId),
      "reference",
    ] as const,
    queryFn: async ({ client }): Promise<ContainerDetail> => {
      const container = await ok(
        api.GET("/v1/workspaces/{workspace}/containers/{container}", {
          params: { path: { workspace: workspaceName(workspaceId), container: containerId } },
        }),
      );
      const [apps, workloads] = await Promise.all([
        appDirectory(client, workspaceId),
        workloadDirectory(client, workspaceId),
      ]);
      const app = apps.byName.get(container.app);
      const workload = workloads.byName.get(`${container.app}/${container.function}`);
      return {
        ...viewContainer(container, workspaceId, app?.id ?? null),
        app: app ? viewApp(app, workspaceId) : null,
        workload: workload ? viewStub(workload, workspaceId, app?.id ?? "") : null,
        deployment: workload ? viewActiveDeployment(workload, app?.id ?? "") : null,
        run_name: null,
        run_status: null,
        expires_at: null,
        actions: {
          can_stop: container.state !== "stopped",
          can_shell: false,
          can_create_image: false,
          can_snapshot_memory: false,
        },
      };
    },
    meta: workspaceLiveQueryMeta(true),
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
    queryFn: ({ signal }): Promise<Schemas["ContainerLifecycle"] | null> =>
      ok(
        api.GET("/v1/workspaces/{workspace}/containers/{container}/lifecycle", {
          params: { path: { workspace, container: containerId } },
          signal,
        }),
      ).catch((error: unknown) => {
        if (error instanceof ApiError && error.status === 404) return null;
        throw error;
      }),
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
