import { mutationOptions, queryOptions } from "@tanstack/react-query";

import { api, ok } from "@/lib/api/client";

import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

/** How many of an app's newest sandboxes its page lists. */
const APP_SANDBOX_LIMIT = 50;

export function sandboxesQueryOptions(workspace: string, app: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.sandboxes.list(workspace, app, APP_SANDBOX_LIMIT),
    queryFn: async ({ signal }) => {
      const page = await ok(
        api.GET("/v1/workspaces/{workspace}/sandboxes", {
          params: { path: { workspace }, query: { app, limit: APP_SANDBOX_LIMIT } },
          signal,
        }),
      );
      return page.sandboxes;
    },
    meta: workspaceLiveQueryMeta(true),
  });
}

export function sandboxStatsQueryOptions(workspace: string, app: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.sandboxes.stats(workspace, app),
    queryFn: ({ signal }) =>
      ok(
        api.GET("/v1/workspaces/{workspace}/sandboxes/stats", {
          params: { path: { workspace }, query: { app } },
          signal,
        }),
      ),
    meta: workspaceLiveQueryMeta(true),
  });
}

/** Processes publish no changes, so the list polls while it is shown. */
export function sandboxProcessesQueryOptions(workspace: string, containerId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.sandboxes.processes(workspace, containerId),
    queryFn: async ({ signal }) => {
      const list = await ok(
        api.GET("/v1/workspaces/{workspace}/containers/{container}/processes", {
          params: { path: { workspace, container: containerId } },
          signal,
        }),
      );
      return list.processes;
    },
    refetchInterval: 5_000,
  });
}

/** A port exposed from inside the sandbox publishes no change, so the list polls. */
export function sandboxPortsQueryOptions(workspace: string, containerId: string, enabled: boolean) {
  return queryOptions({
    queryKey: workspaceQueryKeys.sandboxes.ports(workspace, containerId),
    queryFn: async ({ signal }) => {
      const list = await ok(
        api.GET("/v1/workspaces/{workspace}/containers/{container}/ports", {
          params: { path: { workspace, container: containerId } },
          signal,
        }),
      );
      return list.ports;
    },
    enabled,
    refetchInterval: enabled ? 10_000 : false,
  });
}

export function killSandboxProcessMutationOptions(workspace: string, containerId: string) {
  return mutationOptions({
    mutationFn: (process: string) =>
      ok(
        api.POST("/v1/workspaces/{workspace}/containers/{container}/processes/{process}/kill", {
          params: { path: { workspace, container: containerId, process } },
          body: {},
        }),
      ),
  });
}

export function createSandboxImageMutationOptions(workspace: string, containerId: string) {
  return mutationOptions({
    mutationFn: () =>
      ok(
        api.POST("/v1/workspaces/{workspace}/containers/{container}/filesystem-images", {
          params: { path: { workspace, container: containerId } },
        }),
      ),
  });
}

export function snapshotSandboxMemoryMutationOptions(workspace: string, containerId: string) {
  return mutationOptions({
    mutationFn: () =>
      ok(
        api.POST("/v1/workspaces/{workspace}/containers/{container}/snapshots", {
          params: { path: { workspace, container: containerId } },
          body: {},
        }),
      ),
  });
}
