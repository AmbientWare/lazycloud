import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { apiRequest } from "@/lib/api/client";
import {
  usageCostListSchema,
  usageCostSeriesSchema,
  type UsageCostBucket,
  type UsageCostGroupKey,
} from "@/lib/api/schemas";

import { accountQueryKeys } from "./workspace-keys";

export type UsageCostWindow = {
  start: string;
  end: string;
};

export type UsageCostScope = {
  groupBy: UsageCostGroupKey;
  appId?: string;
  limit?: number;
};

/**
 * Every workspace this account is invoiced for, over one window.
 *
 * Not workspace-scoped and not keyed by one: the provider invoices an account,
 * so the figure someone opens this page to see spans the workspaces they hold
 * rather than the one the sidebar happens to have selected.
 */
export function accountCostsQueryOptions(window: UsageCostWindow, scope: UsageCostScope) {
  const { groupBy, appId, limit = 50 } = scope;
  return infiniteQueryOptions({
    queryKey: accountQueryKeys.usage.costs({
      start: window.start,
      end: window.end,
      groupBy,
      appId: appId ?? null,
    }),
    initialPageParam: "",
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({
        start: window.start,
        end: window.end,
        group_by: groupBy,
        limit: String(limit),
      });
      if (appId !== undefined) params.set("app_id", appId);
      if (pageParam) params.set("cursor", pageParam);
      return apiRequest(`/api/v1/billing/costs?${params.toString()}`, usageCostListSchema);
    },
    getNextPageParam: (page) => page.next || undefined,
    staleTime: 30_000,
  });
}

/**
 * The same account's spend, cut into the intervals a chart is drawn from.
 *
 * One request for the whole window rather than one per bar: a month is thirty
 * questions with the same answer, and asking them separately is thirty scans of
 * the same index and thirty chances for two of them to land either side of a
 * metering write. Every interval comes back, including the ones nothing ran in,
 * so the chart never has to invent the gaps.
 */
export function accountCostSeriesQueryOptions(window: UsageCostWindow, bucket: UsageCostBucket) {
  return queryOptions({
    queryKey: accountQueryKeys.usage.series({ start: window.start, end: window.end, bucket }),
    queryFn: () => {
      const params = new URLSearchParams({
        start: window.start,
        end: window.end,
        bucket,
      });
      return apiRequest(`/api/v1/billing/cost-series?${params.toString()}`, usageCostSeriesSchema);
    },
    staleTime: 30_000,
  });
}

/** The UTC calendar month an instant falls in, as the window the API takes. */
export function calendarMonthWindow(at: Date): UsageCostWindow {
  const start = new Date(Date.UTC(at.getUTCFullYear(), at.getUTCMonth(), 1));
  const end = new Date(Date.UTC(at.getUTCFullYear(), at.getUTCMonth() + 1, 1));
  return { start: start.toISOString(), end: end.toISOString() };
}
