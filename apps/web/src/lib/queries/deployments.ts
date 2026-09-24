import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { apiRequest, postJson, withWorkspace } from "@/lib/api/client";
import {
  deploymentListSchema,
  devboxSchema,
  type Deployment,
  type DevboxPhase,
} from "@/lib/api/schemas";

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

/**
 * A devbox's status, from the endpoint cheap enough to poll.
 *
 * Connections and the idle deadline change without a workspace event, so it
 * refreshes on an interval: quickly while the devbox is changing state, slowly
 * once it has settled, and not at all while the tab is hidden.
 */
export function devboxQueryOptions(workspaceId: string, deploymentId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.deployments.devbox(workspaceId, deploymentId),
    queryFn: () =>
      apiRequest(
        withWorkspace(
          `/api/v1/deployments/${encodeURIComponent(deploymentId)}/devbox`,
          workspaceId,
        ),
        devboxSchema,
      ),
    refetchInterval: (query) =>
      query.state.data && SETTLED_PHASES.has(query.state.data.phase)
        ? SETTLED_REFRESH_MS
        : CHANGING_REFRESH_MS,
    meta: workspaceLiveQueryMeta(false),
  });
}

/** Boot a stopped devbox; the server answers once it has a container. */
export function startDevboxMutationOptions(workspaceId: string, deploymentId: string) {
  return {
    mutationFn: () =>
      postJson(
        withWorkspace(
          `/api/v1/deployments/${encodeURIComponent(deploymentId)}/devbox/start`,
          workspaceId,
        ),
        devboxSchema,
      ),
  };
}

/** Stop a devbox's container now; its deployment stays on. */
export function stopDevboxMutationOptions(workspaceId: string, deploymentId: string) {
  return {
    mutationFn: () =>
      postJson(
        withWorkspace(
          `/api/v1/deployments/${encodeURIComponent(deploymentId)}/devbox/stop`,
          workspaceId,
        ),
        devboxSchema,
      ),
  };
}

const SETTLED_PHASES = new Set<DevboxPhase>(["running", "stopped", "failed"]);
const SETTLED_REFRESH_MS = 15_000;
const CHANGING_REFRESH_MS = 3_000;

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
