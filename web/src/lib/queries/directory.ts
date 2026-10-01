import type { QueryClient } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";
import { workspaceName } from "@/lib/api/workspaces";

import { workspaceQueryKeys } from "./workspace-keys";

/*
 * Lookups the query layer needs to give components the reference's IDs: the
 * API names apps and workloads where the dashboard's views carry IDs. Each is
 * one cached read of the workspace's live apps or deployed workloads.
 *
 * A lookup reuses the cached read until it is invalidated. A list built from
 * a directory passes `fresh`, so it reads the API every time it refetches and
 * leaves the new read in the cache for the lookups.
 */

type DirectoryRead = { fresh?: boolean };

export type AppDirectory = {
  byId: Map<string, Schemas["App"]>;
  byName: Map<string, Schemas["App"]>;
};

export type WorkloadDirectory = {
  byId: Map<string, Schemas["DeployedWorkload"]>;
  /** Keyed `${app}/${name}`. */
  byName: Map<string, Schemas["DeployedWorkload"]>;
};

async function allPages<T>(
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

export function appDirectory(
  client: QueryClient,
  workspaceId: string,
  { fresh = false }: DirectoryRead = {},
): Promise<AppDirectory> {
  return client.fetchQuery({
    staleTime: fresh ? 0 : Infinity,
    queryKey: [...workspaceQueryKeys.apps.root(workspaceId), "directory"],
    queryFn: async () => {
      const workspace = workspaceName(workspaceId);
      const apps = await allPages(async (cursor) => {
        const page = await ok(
          api.GET("/v1/workspaces/{workspace}/apps", {
            params: { path: { workspace }, query: { limit: 1000, cursor } },
          }),
        );
        return { items: page.apps, next: page.next_cursor };
      });
      return {
        byId: new Map(apps.map((app) => [app.id, app])),
        byName: new Map(apps.map((app) => [app.name, app])),
      };
    },
  });
}

export async function appById(
  client: QueryClient,
  workspaceId: string,
  appId: string,
): Promise<Schemas["App"]> {
  const app = (await appDirectory(client, workspaceId)).byId.get(appId);
  if (app) return app;
  return ok(
    api.GET("/v1/workspaces/{workspace}/apps/{app}", {
      params: { path: { workspace: workspaceName(workspaceId), app: appId } },
    }),
  );
}

export function workloadDirectory(
  client: QueryClient,
  workspaceId: string,
  { fresh = false }: DirectoryRead = {},
): Promise<WorkloadDirectory> {
  return client.fetchQuery({
    staleTime: fresh ? 0 : Infinity,
    queryKey: [...workspaceQueryKeys.deployments.root(workspaceId), "directory"],
    queryFn: async () => {
      const workspace = workspaceName(workspaceId);
      const workloads = await allPages(async (cursor) => {
        const page = await ok(
          api.GET("/v1/workspaces/{workspace}/deployments", {
            params: { path: { workspace }, query: { limit: 1000, cursor } },
          }),
        );
        return { items: page.deployments, next: page.next_cursor };
      });
      return {
        byId: new Map(workloads.map((workload) => [workload.id, workload])),
        byName: new Map(
          workloads.map((workload) => [`${workload.app}/${workload.name}`, workload]),
        ),
      };
    },
  });
}
