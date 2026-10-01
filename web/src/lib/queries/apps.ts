import { infiniteQueryOptions, queryOptions, type QueryClient } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";

import { LIVE_LIST_MAX_PAGES, nextPageCursor, selectPages } from "./infinite-list";
import { workspaceQueryKeys } from "./workspace-keys";

export type App = Schemas["App"];

const APP_PAGE_SIZE = 100;

/** Active and paused apps of the workspace, by name. */
export function appsQueryOptions(workspace: string) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.apps.summaries(workspace),
    initialPageParam: "",
    queryFn: ({ pageParam }) =>
      ok(
        api.GET("/v1/workspaces/{workspace}/apps", {
          params: {
            path: { workspace },
            query: { limit: APP_PAGE_SIZE, cursor: pageParam || undefined },
          },
        }),
      ),
    getNextPageParam: nextPageCursor,
    maxPages: LIVE_LIST_MAX_PAGES,
  });
}

export function selectApps(
  data: { pages: readonly Schemas["AppPage"][] } | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectPages(
    data,
    (page) => page.apps,
    hasNextPage,
    (app) => app.id,
  );
}

/** One app by its id or name. */
export function appQueryOptions(workspace: string, app: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.apps.detail(workspace, app),
    queryFn: () =>
      ok(
        api.GET("/v1/workspaces/{workspace}/apps/{app}", { params: { path: { workspace, app } } }),
      ),
  });
}

export function invalidateAppLists(queryClient: QueryClient, workspace: string) {
  return Promise.all(
    [
      workspaceQueryKeys.apps.root(workspace),
      workspaceQueryKeys.deployments.root(workspace),
      workspaceQueryKeys.containers.root(workspace),
      workspaceQueryKeys.tasks.lists(workspace),
    ].map((queryKey) => queryClient.invalidateQueries({ queryKey })),
  );
}

const appPath = (workspace: string, app: string) => ({ params: { path: { workspace, app } } });

export function pauseAppMutationOptions(workspace: string, app: string) {
  return {
    mutationFn: () =>
      ok(api.POST("/v1/workspaces/{workspace}/apps/{app}/pause", appPath(workspace, app))),
  };
}

export function resumeAppMutationOptions(workspace: string, app: string) {
  return {
    mutationFn: () =>
      ok(api.POST("/v1/workspaces/{workspace}/apps/{app}/resume", appPath(workspace, app))),
  };
}

export function deleteAppMutationOptions(workspace: string, app: string) {
  return {
    mutationFn: () =>
      ok(api.DELETE("/v1/workspaces/{workspace}/apps/{app}", appPath(workspace, app))),
  };
}
