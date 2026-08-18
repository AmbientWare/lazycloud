import { queryOptions } from "@tanstack/react-query";

import { apiRequest, withWorkspace } from "@/lib/api/client";
import {
  workspaceActivitySchema,
  workspaceContainerCountsSchema,
  type WorkspaceActivityMeasure,
} from "@/lib/api/schemas";

import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

/**
 * What this workspace is holding right now, by live container status.
 *
 * Workspace-scoped, unlike the concurrency ceiling on the billing summary: the
 * ceiling counts every workspace the account owns, so the two answer different
 * questions and neither stands in for the other.
 */
export function workspaceContainerCountsQueryOptions(workspaceId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.metrics.containerCounts(workspaceId),
    queryFn: () =>
      apiRequest(
        withWorkspace("/api/v1/metrics/workspace/containers", workspaceId),
        workspaceContainerCountsSchema,
      ),
    meta: workspaceLiveQueryMeta(true),
  });
}

export type WorkspaceActivityRange = "6h" | "24h" | "7d";

/**
 * The spans a workspace's activity is read over, and how finely each is cut.
 *
 * Each pairing cuts its span into roughly two dozen intervals — the server
 * aligns both ends to interval boundaries, so a span can gain one — which is
 * what keeps a band legible at every span rather than a smear at the widest.
 */
export const workspaceActivityRanges = {
  "6h": { label: "6 hours", spanSeconds: 6 * 3600, windowSeconds: 900 },
  "24h": { label: "24 hours", spanSeconds: 24 * 3600, windowSeconds: 3600 },
  "7d": { label: "7 days", spanSeconds: 7 * 24 * 3600, windowSeconds: 6 * 3600 },
} as const satisfies Record<
  WorkspaceActivityRange,
  { label: string; spanSeconds: number; windowSeconds: number }
>;

export const workspaceActivityMeasureLabels = {
  containers: "Containers started",
  tasks: "Tasks started",
} as const satisfies Record<WorkspaceActivityMeasure, string>;

/**
 * A workspace's starts over a window, split by the app they belong to.
 *
 * `limit` is how many apps come back named; everything past it arrives summed
 * as one row, so the stacks still total the window without the browser ever
 * holding a series it was not given.
 *
 * The window is computed when the request is made rather than baked into the
 * key, matching the task metrics summary: keying on the instant would mint a
 * fresh cache entry on every render and never reuse one.
 */
export function workspaceActivityQueryOptions(
  workspaceId: string,
  options: { measure: WorkspaceActivityMeasure; range: WorkspaceActivityRange; limit: number },
) {
  const { measure, range, limit } = options;
  const { spanSeconds, windowSeconds } = workspaceActivityRanges[range];
  return queryOptions({
    queryKey: workspaceQueryKeys.metrics.activity(workspaceId, { measure, range, limit }),
    queryFn: () => {
      const params = new URLSearchParams({
        measure,
        window_seconds: String(windowSeconds),
        start: new Date(Date.now() - spanSeconds * 1000).toISOString(),
        limit: String(limit),
      });
      return apiRequest(
        withWorkspace(`/api/v1/metrics/workspace/activity?${params.toString()}`, workspaceId),
        workspaceActivitySchema,
      );
    },
    staleTime: 30_000,
    meta: workspaceLiveQueryMeta(true),
  });
}
