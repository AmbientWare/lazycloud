import { queryOptions, type QueryClient } from "@tanstack/react-query";

import { activityStart } from "@/lib/activity-window";
import { api, ok, type Schemas } from "@/lib/api/client";

import { listWorkloads, newestFirst } from "./deployments";
import { allPages } from "./infinite-list";
import { criticalLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

function listApps(workspace: string): Promise<Schemas["App"][]> {
  return allPages(async (cursor) => {
    const page = await ok(
      api.GET("/v1/workspaces/{workspace}/apps", {
        params: { path: { workspace }, query: { limit: 1000, cursor } },
      }),
    );
    return { items: page.apps, next: page.next_cursor };
  });
}

/** The workspace's live apps by name. */
export function appsQueryOptions(workspace: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.apps.list(workspace),
    queryFn: () => listApps(workspace),
    meta: criticalLiveQueryMeta,
  });
}

export function appQueryOptions(workspace: string, app: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.apps.detail(workspace, app),
    queryFn: () =>
      ok(
        api.GET("/v1/workspaces/{workspace}/apps/{app}", {
          params: { path: { workspace, app } },
        }),
      ),
    meta: criticalLiveQueryMeta,
  });
}

function taskActivity(workspace: string, app?: string): Promise<Schemas["TaskActivity"]> {
  return ok(
    api.GET("/v1/workspaces/{workspace}/metrics/activity", {
      params: {
        path: { workspace },
        query: { window_seconds: 3600, start: activityStart(), app },
      },
    }),
  );
}

/** The app's tasks per hour and status over the last 24 hours, one series per function. */
export function appActivityQueryOptions(workspace: string, app: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.apps.activity(workspace, app),
    queryFn: () => taskActivity(workspace, app),
    meta: criticalLiveQueryMeta,
  });
}

export type AppSummary = {
  app: Schemas["App"];
  /** Deployed workloads, newest deploy first. */
  workloads: Schemas["Workload"][];
  activity: Schemas["ActivitySeries"] | undefined;
};

/**
 * Every app with what its card shows: the apps, the workspace's deployed
 * workloads and one read of the last 24 hours of task activity per app.
 */
export function appSummariesQueryOptions(workspace: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.apps.summaries(workspace),
    queryFn: async (): Promise<AppSummary[]> => {
      const [apps, workloads, activity] = await Promise.all([
        listApps(workspace),
        listWorkloads(workspace),
        taskActivity(workspace),
      ]);
      const series = new Map(activity.series.map((item) => [item.app, item]));
      return apps.map((app) => ({
        app,
        workloads: newestFirst(workloads.filter((workload) => workload.app === app.name)),
        activity: series.get(app.name),
      }));
    },
    meta: criticalLiveQueryMeta,
  });
}

export function invalidateAppLists(queryClient: QueryClient, workspace: string) {
  return Promise.all(
    [workspaceQueryKeys.apps.root(workspace), workspaceQueryKeys.workloads.root(workspace)].map(
      (queryKey) => queryClient.invalidateQueries({ queryKey }),
    ),
  );
}

const appPath = (workspace: string, app: string) => ({ params: { path: { workspace, app } } });

export function setAppPausedMutationOptions(workspace: string, app: string, paused: boolean) {
  return {
    mutationFn: () =>
      ok(
        api.PATCH("/v1/workspaces/{workspace}/apps/{app}", {
          ...appPath(workspace, app),
          body: { paused },
        }),
      ),
  };
}

export function deleteAppMutationOptions(workspace: string, app: string) {
  return {
    mutationFn: () =>
      ok(api.DELETE("/v1/workspaces/{workspace}/apps/{app}", appPath(workspace, app))),
  };
}
