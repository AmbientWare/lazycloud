import { infiniteQueryOptions, mutationOptions, queryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";
import { containerMetricsTimeseriesSchema } from "@/lib/api/schemas";
import { apiRequest, withWorkspace } from "@/lib/api/unserved";

import { LIVE_LIST_MAX_PAGES, nextPageCursor, selectPages } from "./infinite-list";
import { workspaceQueryKeys } from "./workspace-keys";

export type Container = Schemas["Container"];

/** A container that is up and taking work, or finishing it while it drains. */
export function isRunningContainer(container: { state: Container["state"] }): boolean {
  return container.state === "ready" || container.state === "draining";
}

/**
 * The workspace's containers, newest first; with `live`, only those that have
 * not stopped. The API filters by liveness only, so app and workload views
 * narrow the live page themselves.
 */
export function containersQueryOptions(workspace: string, { live = false } = {}) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.containers.list(workspace, { live }),
    initialPageParam: "",
    queryFn: ({ pageParam }) =>
      ok(
        api.GET("/v1/workspaces/{workspace}/containers", {
          params: {
            path: { workspace },
            query: { live, limit: live ? 1000 : 100, cursor: pageParam || undefined },
          },
        }),
      ),
    getNextPageParam: nextPageCursor,
    maxPages: LIVE_LIST_MAX_PAGES,
  });
}

export function selectContainers(
  data: { pages: readonly Schemas["ContainerPage"][] } | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectPages(
    data,
    (page) => page.containers,
    hasNextPage,
    (item) => item.id,
  );
}

export function containerQueryOptions(workspace: string, container: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.containers.detail(workspace, container),
    queryFn: () =>
      ok(
        api.GET("/v1/workspaces/{workspace}/containers/{container}", {
          params: { path: { workspace, container } },
        }),
      ),
  });
}

export function stopContainerMutationOptions(workspace: string, container: string) {
  return mutationOptions({
    mutationFn: () =>
      ok(
        api.POST("/v1/workspaces/{workspace}/containers/{container}/stop", {
          params: { path: { workspace, container } },
        }),
      ),
  });
}

/** CPU, memory and GPU over the container's life; the observability packet serves it. */
export function containerMetricsTimeseriesQueryOptions(
  workspace: string,
  container: string,
  live: boolean,
) {
  return queryOptions({
    queryKey: workspaceQueryKeys.containers.metrics(workspace, container),
    queryFn: () =>
      apiRequest(
        withWorkspace(`/api/v1/metrics/containers/${container}/timeseries`, workspace),
        containerMetricsTimeseriesSchema,
      ),
    refetchInterval: live ? 5_000 : false,
  });
}
