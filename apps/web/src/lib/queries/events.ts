import { queryOptions } from "@tanstack/react-query";

import { apiRequest, withWorkspace } from "@/lib/api/client";
import { containerEventSummaryOrNullSchema } from "@/lib/api/schemas";

import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

export function containerEventSummaryQueryOptions(workspaceId: string, containerId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.containers.eventSummary(workspaceId, containerId),
    queryFn: () =>
      apiRequest(
        withWorkspace(`/api/v1/events/containers/${containerId}/summary`, workspaceId),
        containerEventSummaryOrNullSchema,
      ),
    meta: workspaceLiveQueryMeta(false),
    refetchInterval: 15_000,
  });
}
