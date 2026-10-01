import { queryOptions } from "@tanstack/react-query";

import { api, ApiError, ok, type Schemas } from "@/lib/api/client";

import { workspaceQueryKeys } from "./workspace-keys";

const RESULT_LIMIT = 10;
const APP_NAME = /^[a-z][a-z0-9_]{0,62}$/;
const WORKLOAD_NAME = /^[A-Za-z_][A-Za-z0-9_-]{0,62}$/;

/**
 * The apps a search term finds. An empty term lists the first apps; otherwise the
 * API matches the app by its exact name, the only app lookup it offers.
 */
export function appSearchQueryOptions(workspaceName: string, term: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.search(workspaceName, "apps", term),
    queryFn: async (): Promise<Schemas["App"][]> => {
      const path = { workspace: workspaceName };
      if (!term) {
        const page = await ok(
          api.GET("/v1/workspaces/{workspace}/apps", {
            params: { path, query: { limit: RESULT_LIMIT } },
          }),
        );
        return page.apps;
      }
      if (!APP_NAME.test(term)) return [];
      try {
        return [
          await ok(
            api.GET("/v1/workspaces/{workspace}/apps/{app}", {
              params: { path: { ...path, app: term } },
            }),
          ),
        ];
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return [];
        throw error;
      }
    },
  });
}

/** Deployed workloads a search term finds: the first ones, or those with exactly that name. */
export function deploymentSearchQueryOptions(workspaceName: string, term: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.search(workspaceName, "deployments", term),
    queryFn: async (): Promise<Schemas["DeployedWorkload"][]> => {
      if (term && !WORKLOAD_NAME.test(term)) return [];
      const page = await ok(
        api.GET("/v1/workspaces/{workspace}/deployments", {
          params: {
            path: { workspace: workspaceName },
            query: { limit: RESULT_LIMIT, name: term || undefined },
          },
        }),
      );
      return page.deployments;
    },
  });
}

/** Tasks whose id starts with the term or whose function name contains it. */
export function taskSearchQueryOptions(workspaceName: string, term: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.search(workspaceName, "tasks", term),
    queryFn: async (): Promise<Schemas["Task"][]> => {
      const page = await ok(
        api.GET("/v1/workspaces/{workspace}/tasks", {
          params: {
            path: { workspace: workspaceName },
            query: { limit: RESULT_LIMIT, search: term },
          },
        }),
      );
      return page.tasks;
    },
  });
}
