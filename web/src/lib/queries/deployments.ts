import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";

import { nextListCursor, selectInfiniteList, type InfiniteListQueryData } from "./infinite-list";
import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

/** Every page of a cursor listing, for a list that answers a whole view. */
export async function allPages<T>(
  read: (cursor: string | undefined) => Promise<{ items: T[]; next?: string }>,
): Promise<T[]> {
  const items: T[] = [];
  let cursor: string | undefined;
  do {
    const page = await read(cursor);
    items.push(...page.items);
    cursor = page.next;
  } while (cursor);
  return items;
}

/** Deployed workloads, by app and name, of the workspace or one app. */
export function listDeployments(
  workspace: string,
  app?: string,
): Promise<Schemas["DeployedWorkload"][]> {
  return allPages(async (cursor) => {
    const page = await ok(
      api.GET("/v1/workspaces/{workspace}/deployments", {
        params: { path: { workspace }, query: { app, limit: 1000, cursor } },
      }),
    );
    return { items: page.deployments, next: page.next_cursor };
  });
}

/** Deployed workloads of the workspace, or of one app, newest deploy first. */
export function deploymentsQueryOptions(workspace: string, app?: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.deployments.list(workspace, app ?? null),
    queryFn: async () => newestFirst(await listDeployments(workspace, app)),
    meta: workspaceLiveQueryMeta(true),
  });
}

export function deployedAt(workload: Schemas["DeployedWorkload"]): string {
  return workload.deployed_at ?? workload.created_at;
}

export function newestFirst(
  workloads: Schemas["DeployedWorkload"][],
): Schemas["DeployedWorkload"][] {
  return [...workloads].sort((left, right) => deployedAt(right).localeCompare(deployedAt(left)));
}

/** Whether the workload admits work: started, in a running app, on a deployed version. */
export function workloadRunning(workload: Schemas["DeployedWorkload"]): boolean {
  return workload.state === "active" && workload.app_state !== "paused";
}

/** A workload the page names that its app does not deploy. */
export class WorkloadNotFoundError extends Error {
  constructor(name: string) {
    super(`No deployed workload named ${name} in this app`);
    this.name = "WorkloadNotFoundError";
  }
}

export type Workload = {
  deployment: Schemas["DeployedWorkload"];
  /** The active release: the definition the workload runs. */
  release: Schemas["Release"];
  /** Where an endpoint or ASGI app answers; null for a function. */
  http: Schemas["HttpWorkload"] | null;
  /** A scheduled function's next and last runs. */
  schedule: Schemas["Schedule"] | null;
};

/**
 * One deployed workload by app, kind and name, with its active definition.
 * Every fetch asks the server for exactly that workload; one it does not
 * deploy is a WorkloadNotFoundError.
 */
export function workloadQueryOptions(workspace: string, app: string, kind: string, name: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.deployments.workload(workspace, app, kind, name),
    queryFn: async (): Promise<Workload> => {
      const page = await ok(
        api.GET("/v1/workspaces/{workspace}/deployments", {
          params: { path: { workspace }, query: { app, name, limit: 1 } },
        }),
      );
      const deployment = page.deployments.find(
        (item) => item.app === app && item.name === name && item.kind === kind,
      );
      if (!deployment) throw new WorkloadNotFoundError(name);
      const path = { workspace, app };
      switch (deployment.kind) {
        case "function": {
          const fn = await ok(
            api.GET("/v1/workspaces/{workspace}/apps/{app}/functions/{function}", {
              params: { path: { ...path, function: name } },
            }),
          );
          return { deployment, release: fn.active_release, http: null, schedule: fn.schedule ?? null };
        }
        case "endpoint": {
          const http = await ok(
            api.GET("/v1/workspaces/{workspace}/apps/{app}/endpoints/{endpoint}", {
              params: { path: { ...path, endpoint: name } },
            }),
          );
          return { deployment, release: http.release, http, schedule: null };
        }
        case "asgi": {
          const http = await ok(
            api.GET("/v1/workspaces/{workspace}/apps/{app}/asgi/{endpoint}", {
              params: { path: { ...path, endpoint: name } },
            }),
          );
          return { deployment, release: http.release, http, schedule: null };
        }
      }
    },
    retry: (failures, error) => !(error instanceof WorkloadNotFoundError) && failures < 2,
    meta: workspaceLiveQueryMeta(true),
  });
}

/** The workload's deployed versions, newest first. */
export function versionsInfiniteQueryOptions(workspace: string, deployment: string) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.deployments.versions(workspace, deployment),
    initialPageParam: "",
    queryFn: async ({ pageParam }) => {
      const page = await ok(
        api.GET("/v1/workspaces/{workspace}/deployments/{deployment}/versions", {
          params: {
            path: { workspace, deployment },
            query: { limit: 100, cursor: pageParam || undefined },
          },
        }),
      );
      return { data: page.versions, next: page.next_cursor ?? "" };
    },
    getNextPageParam: nextListCursor,
    meta: workspaceLiveQueryMeta(true),
  });
}

export function selectVersionList(
  data: InfiniteListQueryData<Schemas["Version"]> | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectInfiniteList(data, hasNextPage, (version) => String(version.version));
}

/** Run time percentiles, outcomes and cold starts per hour over the last day. */
export function performanceQueryOptions(workspace: string, deployment: string, windowSeconds = 3600) {
  return queryOptions({
    queryKey: workspaceQueryKeys.deployments.performance(workspace, deployment, windowSeconds),
    queryFn: () =>
      ok(
        api.GET("/v1/workspaces/{workspace}/deployments/{deployment}/performance", {
          params: { path: { workspace, deployment }, query: { window_seconds: windowSeconds } },
        }),
      ),
    meta: workspaceLiveQueryMeta(true),
  });
}

const deploymentPath = (workspace: string, deployment: string) => ({
  params: { path: { workspace, deployment } },
});

/** Starts the workload, on `version` when one is named, which then becomes the active one. */
export function startDeploymentMutationOptions(
  workspace: string,
  deployment: string,
  version?: number,
) {
  return {
    mutationFn: () =>
      ok(
        api.POST("/v1/workspaces/{workspace}/deployments/{deployment}/start", {
          ...deploymentPath(workspace, deployment),
          body: version === undefined ? {} : { version },
        }),
      ),
  };
}

export function stopDeploymentMutationOptions(workspace: string, deployment: string) {
  return {
    mutationFn: () =>
      ok(
        api.POST(
          "/v1/workspaces/{workspace}/deployments/{deployment}/stop",
          deploymentPath(workspace, deployment),
        ),
      ),
  };
}

/** Deletes the workload with every version of it. */
export function deleteDeploymentMutationOptions(workspace: string, deployment: string) {
  return {
    mutationFn: () =>
      ok(
        api.DELETE(
          "/v1/workspaces/{workspace}/deployments/{deployment}",
          deploymentPath(workspace, deployment),
        ),
      ),
  };
}
