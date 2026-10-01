import { queryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";

import { workspaceQueryKeys } from "./workspace-keys";

const RESULT_LIMIT = 10;

/** The apps whose name contains the term; an empty term lists the first apps. */
export function appSearchQueryOptions(workspaceName: string, term: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.search(workspaceName, "apps", term),
    queryFn: async (): Promise<Schemas["App"][]> => {
      const page = await ok(
        api.GET("/v1/workspaces/{workspace}/apps", {
          params: {
            path: { workspace: workspaceName },
            query: { limit: RESULT_LIMIT, search: term || undefined },
          },
        }),
      );
      return page.apps;
    },
  });
}

/** Deployed workloads whose app or workload name contains the term. */
export function deploymentSearchQueryOptions(workspaceName: string, term: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.search(workspaceName, "deployments", term),
    queryFn: async (): Promise<Schemas["DeployedWorkload"][]> => {
      const page = await ok(
        api.GET("/v1/workspaces/{workspace}/deployments", {
          params: {
            path: { workspace: workspaceName },
            query: { limit: RESULT_LIMIT, search: term || undefined },
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
