import { infiniteQueryOptions } from "@tanstack/react-query";
import { LIVE_LIST_MAX_PAGES } from "./infinite-list";

import { apiRequest, withWorkspace } from "@/lib/api/client";
import { deploymentListSchema, type Deployment, type DeploymentList } from "@/lib/api/schemas";

import { selectInfiniteList, type InfiniteListQueryData } from "./infinite-list";
import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

export type DeploymentListOptions = {
  appId?: string;
  name?: string;
  kind?: string;
  limit?: number;
};

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
