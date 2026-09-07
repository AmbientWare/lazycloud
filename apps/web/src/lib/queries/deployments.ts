import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";
import { LIVE_LIST_MAX_PAGES } from "./infinite-list";

import { apiRequest, withWorkspace } from "@/lib/api/client";
import {
  deploymentListSchema,
  workloadPageSchema,
  type Deployment,
  type DeploymentKind,
  type DeploymentList,
} from "@/lib/api/schemas";

import { selectInfiniteList, type InfiniteListQueryData } from "./infinite-list";
import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

export type DeploymentListOptions = {
  appId?: string;
  name?: string;
  kind?: DeploymentKind;
  limit?: number;
};

export function workloadsInfiniteQueryOptions(workspaceId: string, appId: string, kind?: string) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.deployments.workloads(workspaceId, appId, undefined, kind),
    initialPageParam: "",
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({ limit: "50" });
      if (pageParam) params.set("cursor", pageParam);
      if (kind) params.set("kind", kind);
      return apiRequest(
        withWorkspace(`/api/v1/apps/${encodeURIComponent(appId)}/workloads?${params}`, workspaceId),
        workloadPageSchema,
      );
    },
    getNextPageParam: (page) => page.next || undefined,
    maxPages: LIVE_LIST_MAX_PAGES,
    meta: workspaceLiveQueryMeta(true),
  });
}

export function workloadQueryOptions(
  workspaceId: string,
  appId: string,
  name: string,
  kind: DeploymentKind,
) {
  return queryOptions({
    queryKey: workspaceQueryKeys.deployments.workloads(workspaceId, appId, name, kind),
    queryFn: async () => {
      const params = new URLSearchParams({ name, kind, limit: "1" });
      const page = await apiRequest(
        withWorkspace(`/api/v1/apps/${encodeURIComponent(appId)}/workloads?${params}`, workspaceId),
        workloadPageSchema,
      );
      return page.data[0] ?? null;
    },
    meta: workspaceLiveQueryMeta(true),
  });
}

export function deploymentsInfiniteQueryOptions(
  workspaceId: string,
  options: DeploymentListOptions = {},
) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.deployments.list(workspaceId, {
      limit: options.limit ?? 100,
      appId: options.appId ?? null,
      name: options.name ?? null,
      kind: options.kind ?? null,
    }),
    initialPageParam: "",
    queryFn: ({ pageParam }) => {
      const params = deploymentListParams(options, pageParam);
      return apiRequest(
        withWorkspace(`/api/v1/deployments?${params.toString()}`, workspaceId),
        deploymentListSchema,
      );
    },
    getNextPageParam: nextDeploymentCursor,
    maxPages: LIVE_LIST_MAX_PAGES,
    meta: workspaceLiveQueryMeta(true),
  });
}

export function selectDeploymentList(
  data: InfiniteListQueryData<Deployment> | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectInfiniteList(data, hasNextPage, (deployment) => deployment.id);
}

export function nextDeploymentCursor(
  lastPage: DeploymentList,
  pages: DeploymentList[],
): string | undefined {
  if (!lastPage.next) return undefined;
  const cursorAlreadySeen = pages.slice(0, -1).some((page) => page.next === lastPage.next);
  return cursorAlreadySeen ? undefined : lastPage.next;
}

function deploymentListParams(options: DeploymentListOptions, cursor: string): URLSearchParams {
  const params = new URLSearchParams({ limit: String(options.limit ?? 100) });
  if (cursor) params.set("cursor", cursor);
  if (options.appId) params.set("app_id", options.appId);
  if (options.name) params.set("name", options.name);
  if (options.kind) params.set("kind", options.kind);
  return params;
}
