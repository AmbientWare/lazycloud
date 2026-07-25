import { queryOptions } from "@tanstack/react-query";

import { apiRequest, withWorkspace } from "@/lib/api/client";
import { concurrencyLimitListSchema } from "@/lib/api/schemas";

import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

export function concurrencyLimitsQueryOptions(workspaceId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.settings.concurrency(workspaceId),
    queryFn: () =>
      apiRequest(withWorkspace("/api/v1/concurrency-limits", workspaceId), concurrencyLimitListSchema),
    meta: workspaceLiveQueryMeta(true),
  });
}
