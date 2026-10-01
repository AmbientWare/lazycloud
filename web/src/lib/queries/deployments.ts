import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";

import { LIVE_LIST_MAX_PAGES, nextPageCursor, selectPages } from "./infinite-list";
import { workspaceQueryKeys } from "./workspace-keys";

export type DeployedWorkload = Schemas["DeployedWorkload"];

export type DeploymentListOptions = {
  app?: string;
  name?: string;
  limit?: number;
};

/** Deployed workloads, one row per workload with its active version. */
export function deploymentsQueryOptions(workspace: string, options: DeploymentListOptions = {}) {
  const limit = options.limit ?? 100;
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.deployments.list(workspace, {
      limit,
      app: options.app ?? null,
      name: options.name ?? null,
    }),
    initialPageParam: "",
    queryFn: ({ pageParam }) =>
      ok(
        api.GET("/v1/workspaces/{workspace}/deployments", {
          params: {
            path: { workspace },
            query: { app: options.app, name: options.name, limit, cursor: pageParam || undefined },
          },
        }),
      ),
    getNextPageParam: nextPageCursor,
    maxPages: LIVE_LIST_MAX_PAGES,
  });
}

export function selectDeployments(
  data: { pages: readonly Schemas["DeploymentPage"][] } | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectPages(
    data,
    (page) => page.deployments,
    hasNextPage,
    (item) => item.id,
  );
}

/** A deployed function with its active release and schedule. */
export function functionQueryOptions(workspace: string, app: string, name: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.workloads.detail(workspace, app, name),
    queryFn: () =>
      ok(
        api.GET("/v1/workspaces/{workspace}/apps/{app}/functions/{function}", {
          params: { path: { workspace, app, function: name } },
        }),
      ),
  });
}

/** Deployed versions of a workload, newest first. */
export function versionsQueryOptions(workspace: string, deployment: string) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.deployments.versions(workspace, deployment),
    initialPageParam: "",
    queryFn: ({ pageParam }) =>
      ok(
        api.GET("/v1/workspaces/{workspace}/deployments/{deployment}/versions", {
          params: {
            path: { workspace, deployment },
            query: { limit: 100, cursor: pageParam || undefined },
          },
        }),
      ),
    getNextPageParam: nextPageCursor,
  });
}

export function selectVersions(
  data: { pages: readonly Schemas["VersionPage"][] } | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectPages(
    data,
    (page) => page.versions,
    hasNextPage,
    (item) => item.release_id,
  );
}

const deploymentPath = (workspace: string, deployment: string) => ({
  params: { path: { workspace, deployment } },
});

/** Start the workload, optionally making an earlier version active first. */
export function startDeployment(
  workspace: string,
  deployment: string,
  version?: number,
): Promise<DeployedWorkload> {
  return ok(
    api.POST("/v1/workspaces/{workspace}/deployments/{deployment}/start", {
      ...deploymentPath(workspace, deployment),
      body: version === undefined ? {} : { version },
    }),
  );
}

export function stopDeployment(workspace: string, deployment: string): Promise<DeployedWorkload> {
  return ok(
    api.POST(
      "/v1/workspaces/{workspace}/deployments/{deployment}/stop",
      deploymentPath(workspace, deployment),
    ),
  );
}

export function deleteDeployment(workspace: string, deployment: string): Promise<DeployedWorkload> {
  return ok(
    api.DELETE(
      "/v1/workspaces/{workspace}/deployments/{deployment}",
      deploymentPath(workspace, deployment),
    ),
  );
}
