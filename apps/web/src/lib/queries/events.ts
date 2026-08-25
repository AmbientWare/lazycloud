import { queryOptions } from "@tanstack/react-query";

import { apiRequest, withWorkspace } from "@/lib/api/client";
import { containerEventSummaryOrNullSchema } from "@/lib/api/schemas";

import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

/**
 * How often a container's lifecycle summary is re-read.
 *
 * Most of what lands in a container's event log — pod, shell, gateway, and
 * supervision events — is written without the container record itself changing,
 * and it is the record that has a change topic. A change event refreshes this
 * when one happens; between them, asking is the only way the phases fill in.
 */
const LIFECYCLE_SUMMARY_POLL_INTERVAL_MS = 15_000;

export function containerEventSummaryQueryOptions(workspaceId: string, containerId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.containers.eventSummary(workspaceId, containerId),
    queryFn: () =>
      apiRequest(
        withWorkspace(`/api/v1/events/containers/${containerId}/summary`, workspaceId),
        containerEventSummaryOrNullSchema,
      ),
    meta: workspaceLiveQueryMeta(false),
    refetchInterval: LIFECYCLE_SUMMARY_POLL_INTERVAL_MS,
  });
}
