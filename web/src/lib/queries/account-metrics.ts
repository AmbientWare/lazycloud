import { queryOptions } from "@tanstack/react-query";

import { api, ok } from "@/lib/api/client";
import type {
  AccountActivity,
  AccountActivityMeasure,
  AccountContainerCounts,
} from "@/lib/api/schemas";

import { accountQueryKeys, workspaceLiveQueryMeta } from "./workspace-keys";

/**
 * What this account is holding right now, by live container status.
 *
 * Keyed off the account rather than the workspace, and answered that way by the
 * server: the concurrency ceiling it is read against is a term of a plan, and a
 * plan belongs to a payer, so a figure covering one workspace would be compared
 * with a limit it is not counted for.
 */
export function accountContainerCountsQueryOptions() {
  return queryOptions({
    queryKey: accountQueryKeys.metrics.containerCounts(),
    queryFn: async (): Promise<AccountContainerCounts> => {
      const metrics = await ok(api.GET("/v1/me/metrics"));
      return { pending: metrics.containers.pending, running: metrics.containers.running };
    },
    meta: workspaceLiveQueryMeta(true),
  });
}

export type AccountActivityRange = "6h" | "24h" | "7d";

/**
 * The spans an account's activity is read over, and how finely each is cut.
 *
 * Each pairing cuts its span into roughly two dozen intervals — the server
 * aligns both ends to interval boundaries, so a span can gain one — which is
 * what keeps a band legible at every span rather than a smear at the widest.
 */
export const accountActivityRanges = {
  "6h": { label: "6 hours", spanSeconds: 6 * 3600, windowSeconds: 900 },
  "24h": { label: "24 hours", spanSeconds: 24 * 3600, windowSeconds: 3600 },
  "7d": { label: "7 days", spanSeconds: 7 * 24 * 3600, windowSeconds: 6 * 3600 },
} as const satisfies Record<
  AccountActivityRange,
  { label: string; spanSeconds: number; windowSeconds: number }
>;

/**
 * An account's activity over a window, split by the app it belongs to.
 *
 * `limit` is how many apps come back named; everything past it arrives summed
 * as one row, so the stacks still total the window without the browser ever
 * holding a series it was not given.
 *
 * The window is computed when the request is made rather than baked into the
 * key, matching the task metrics summary: keying on the instant would mint a
 * fresh cache entry on every render and never reuse one.
 */
export function accountActivityQueryOptions(options: {
  measure: AccountActivityMeasure;
  range: AccountActivityRange;
  limit: number;
}) {
  const { measure, range, limit } = options;
  const { spanSeconds, windowSeconds } = accountActivityRanges[range];
  return queryOptions({
    queryKey: accountQueryKeys.metrics.activity({ measure, range, limit }),
    queryFn: async (): Promise<AccountActivity> => {
      const activity = await ok(
        api.GET("/v1/me/activity", {
          params: {
            query: {
              measure,
              window_seconds: windowSeconds,
              start: new Date(Date.now() - spanSeconds * 1000).toISOString(),
              limit,
            },
          },
        }),
      );
      return {
        ...activity,
        series: activity.series.map((series) => ({
          kind: series.kind,
          // The API names a series' workspace; its labels read only the name.
          workspace_id: "",
          workspace_name: series.workspace ?? "",
          app_id: series.app_id ?? "",
          app_name: series.app ?? "",
          total: series.total,
          buckets: series.buckets,
        })),
      };
    },
    staleTime: 30_000,
    meta: workspaceLiveQueryMeta(true),
  });
}
