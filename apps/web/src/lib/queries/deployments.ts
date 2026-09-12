import { infiniteQueryOptions } from "@tanstack/react-query";

import { apiRequest, withWorkspace } from "@/lib/api/client";
import { deploymentListSchema, type Deployment } from "@/lib/api/schemas";

import {
  LIVE_LIST_MAX_PAGES,
  nextListCursor,
  selectInfiniteList,
  type InfiniteListQueryData,
} from "./infinite-list";
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
    getNextPageParam: nextListCursor,
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

function deploymentListParams(options: DeploymentListOptions, cursor: string): URLSearchParams {
  const params = new URLSearchParams({ limit: String(options.limit ?? 100) });
  if (cursor) params.set("cursor", cursor);
  if (options.appId) params.set("app_id", options.appId);
  if (options.name) params.set("name", options.name);
  if (options.kind) params.set("kind", options.kind);
  return params;
}
