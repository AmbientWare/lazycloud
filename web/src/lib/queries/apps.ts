import { queryOptions, type QueryClient } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";

import { allPages, listWorkloads, newestFirst } from "./deployments";
import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

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
    meta: workspaceLiveQueryMeta(true),
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
    meta: workspaceLiveQueryMeta(true),
  });
}

/** Hourly buckets the activity charts draw: the current hour and the 23 before it. */
const HOURS_DRAWN = 24;

function activitySince(now = Date.now()): string {
  const hour = 3_600_000;
  return new Date(Math.floor(now / hour) * hour - (HOURS_DRAWN - 1) * hour).toISOString();
}

function taskActivity(workspace: string, app?: string): Promise<Schemas["TaskActivity"]> {
  // The start is computed per fetch and left out of the query key, which
  // would otherwise change on every refetch.
  return ok(
    api.GET("/v1/workspaces/{workspace}/metrics/activity", {
      params: {
        path: { workspace },
        query: { window_seconds: 3600, start: activitySince(), app },
      },
    }),
  );
}

/** The app's tasks per hour and status over the last 24 hours, one series per function. */
export function appActivityQueryOptions(workspace: string, app: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.apps.activity(workspace, app),
    queryFn: () => taskActivity(workspace, app),
    meta: workspaceLiveQueryMeta(true),
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
    meta: workspaceLiveQueryMeta(true),
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
