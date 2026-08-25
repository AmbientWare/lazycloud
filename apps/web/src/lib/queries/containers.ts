import { infiniteQueryOptions, mutationOptions, queryOptions } from "@tanstack/react-query";
import { LIVE_LIST_MAX_PAGES } from "./infinite-list";

import { apiRequest, postJson, withWorkspace } from "@/lib/api/client";
import {
  containerDetailSchema,
  containerMetricsTimeseriesSchema,
  containerSchema,
  containerWithAppPageSchema,
  type ContainerWithAppPage,
} from "@/lib/api/schemas";

import { selectInfiniteList, type InfiniteListQueryData } from "./infinite-list";
import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

export type ContainerListOptions = {
  appId?: string;
  stubIds?: string[];
  statuses?: ContainerListStatus[];
  enabled?: boolean;
};

export type ContainerListStatus = "pending" | "running" | "exited" | "failed" | "stopped";

const INITIAL_CONTAINER_CURSOR: string = "";

export function containersQueryOptions(workspaceId: string, options: ContainerListOptions = {}) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.containers.list(workspaceId, {
      appId: options.appId ?? null,
      stubIds: options.stubIds?.join(",") ?? null,
      statuses: options.statuses?.join(",") ?? null,
    }),
    initialPageParam: INITIAL_CONTAINER_CURSOR,
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({ limit: "100" });
      if (pageParam) params.set("cursor", pageParam);
      if (options.appId) params.set("app_id", options.appId);
      for (const stubId of options.stubIds ?? []) params.append("stub_id", stubId);
      for (const status of options.statuses ?? []) params.append("status", status);
      return apiRequest(
        withWorkspace(`/api/v1/containers?${params.toString()}`, workspaceId),
        containerWithAppPageSchema,
      );
    },
    getNextPageParam: nextContainerCursor,
    // A live list is a view of what is happening now, not an archive. Without a
    // bound, every change event refetches every page the list has ever loaded:
    // one app view walked twenty pages of a hundred containers and re-walked
    // them on each event, which is most of what made the dashboard slow.
    maxPages: LIVE_LIST_MAX_PAGES,
    enabled: options.enabled,
    meta: workspaceLiveQueryMeta(true),
  });
}

export function nextContainerCursor(
  lastPage: ContainerWithAppPage,
  pages: ContainerWithAppPage[],
): string | undefined {
  if (!lastPage.next) return undefined;
  const cursorAlreadySeen = pages.slice(0, -1).some((page) => page.next === lastPage.next);
  return cursorAlreadySeen ? undefined : lastPage.next;
}

export function selectContainerList(
  data: InfiniteListQueryData<ContainerWithAppPage["data"][number]> | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectInfiniteList(data, hasNextPage, (item) => item.container.id);
}

export function containerQueryOptions(workspaceId: string, containerId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.containers.detail(workspaceId, containerId),
    queryFn: () =>
      apiRequest(
        withWorkspace(`/api/v1/containers/${containerId}`, workspaceId),
        containerDetailSchema,
      ),
    meta: workspaceLiveQueryMeta(true),
  });
}

export function stopContainerMutationOptions(workspaceId: string, containerId: string) {
  return mutationOptions({
    mutationFn: () =>
      postJson(
        withWorkspace(`/api/v1/containers/${encodeURIComponent(containerId)}/stop`, workspaceId),
        containerSchema,
        {},
      ),
  });
}

export function containerMetricsTimeseriesQueryOptions(
  workspaceId: string,
  containerId: string,
  live: boolean,
) {
  return queryOptions({
    queryKey: workspaceQueryKeys.containers.metrics(workspaceId, containerId),
    queryFn: () =>
      apiRequest(
        withWorkspace(`/api/v1/metrics/containers/${containerId}/timeseries`, workspaceId),
        containerMetricsTimeseriesSchema,
      ),
    refetchInterval: live ? 5_000 : false,
  });
}
