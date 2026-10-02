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
export function workloadSearchQueryOptions(workspaceName: string, term: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.search(workspaceName, "workloads", term),
    queryFn: async (): Promise<Schemas["Workload"][]> => {
      const page = await ok(
        api.GET("/v1/workspaces/{workspace}/workloads", {
          params: {
            path: { workspace: workspaceName },
            query: { limit: RESULT_LIMIT, search: term || undefined },
          },
        }),
      );
      return page.workloads;
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

/** Sandboxes whose name, app name or container id contains the term. */
export function sandboxSearchQueryOptions(workspaceName: string, term: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.search(workspaceName, "sandboxes", term),
    queryFn: async (): Promise<Schemas["Sandbox"][]> => {
      const page = await ok(
        api.GET("/v1/workspaces/{workspace}/sandboxes", {
          params: {
            path: { workspace: workspaceName },
            query: { limit: RESULT_LIMIT, search: term || undefined },
          },
        }),
      );
      return page.sandboxes;
    },
  });
}
