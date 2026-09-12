import { infiniteQueryOptions, mutationOptions, queryOptions } from "@tanstack/react-query";

import { apiRequest, postJson, withWorkspace } from "@/lib/api/client";
import {
  containerDetailSchema,
  containerMetricsTimeseriesSchema,
  containerSchema,
  containerWithAppPageSchema,
  type ContainerWithAppPage,
} from "@/lib/api/schemas";

import {
  LIVE_LIST_MAX_PAGES,
  nextListCursor,
  selectInfiniteList,
  type InfiniteListQueryData,
} from "./infinite-list";
import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

export type ContainerListOptions = {
  appId?: string;
  stubIds?: string[];
  statuses?: ContainerListStatus[];
  enabled?: boolean;
};

export type ContainerListStatus = "pending" | "running" | "exited" | "failed" | "stopped";

export function containersQueryOptions(workspaceId: string, options: ContainerListOptions = {}) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.containers.list(workspaceId, {
      appId: options.appId ?? null,
      stubIds: options.stubIds?.join(",") ?? null,
      statuses: options.statuses?.join(",") ?? null,
    }),
    initialPageParam: "",
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
    getNextPageParam: nextListCursor,
    maxPages: LIVE_LIST_MAX_PAGES,
    enabled: options.enabled,
    meta: workspaceLiveQueryMeta(true),
  });
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
