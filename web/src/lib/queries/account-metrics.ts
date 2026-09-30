import { queryOptions } from "@tanstack/react-query";

import { apiRequest } from "@/lib/api/client";
import {
  accountActivitySchema,
  accountContainerCountsSchema,
  type AccountActivityMeasure,
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
    queryFn: () => apiRequest("/api/v1/metrics/account/containers", accountContainerCountsSchema),
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
    queryFn: () => {
      const params = new URLSearchParams({
        measure,
        window_seconds: String(windowSeconds),
        start: new Date(Date.now() - spanSeconds * 1000).toISOString(),
        limit: String(limit),
      });
      return apiRequest(
        `/api/v1/metrics/account/activity?${params.toString()}`,
        accountActivitySchema,
      );
    },
    staleTime: 30_000,
    meta: workspaceLiveQueryMeta(true),
  });
}
