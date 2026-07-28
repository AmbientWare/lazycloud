import { queryOptions } from "@tanstack/react-query";

import { apiRequest, withWorkspace } from "@/lib/api/client";
import { cronJobListSchema } from "@/lib/api/schemas";

import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

/** Registered cron jobs (schedule expression, next/last run) for the workspace. */
export function cronJobsQueryOptions(workspaceId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.workloads.cron(workspaceId),
    queryFn: () => apiRequest(withWorkspace("/api/v1/cron-jobs", workspaceId), cronJobListSchema),
    meta: workspaceLiveQueryMeta(true),
  });
}
