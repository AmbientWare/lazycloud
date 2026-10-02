import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";

import { accountQueryKeys } from "./workspace-keys";

export type UsageCostWindow = {
  start: string;
  end: string;
};

export type UsageCostScope = {
  groupBy: Schemas["UsageCostGroup"];
  appId?: string;
  workspaceId?: string;
  workloadId?: string;
  category?: Schemas["UsageCostCategory"];
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
  const { groupBy, appId, workspaceId, workloadId, category, limit = 50 } = scope;
  return infiniteQueryOptions({
    queryKey: accountQueryKeys.usage.costs({
      start: window.start,
      end: window.end,
      groupBy,
      appId: appId ?? null,
      workspaceId: workspaceId ?? null,
      workloadId: workloadId ?? null,
      category: category ?? null,
    }),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      ok(
        api.GET("/v1/billing/costs", {
          params: {
            query: {
              start: window.start,
              end: window.end,
              group_by: groupBy,
              limit,
              app_id: appId,
              workspace_id: workspaceId,
              workload_id: workloadId,
              category,
              cursor: pageParam,
            },
          },
        }),
      ),
    getNextPageParam: (page) => page.next_cursor,
    staleTime: 30_000,
  });
}

/**
 * The same account's spend, cut into the intervals a chart is drawn from. One
 * request answers the whole window, including the intervals nothing ran in.
 */
export function accountCostSeriesQueryOptions(
  window: UsageCostWindow,
  bucket: Schemas["UsageCostBucket"],
) {
  return queryOptions({
    queryKey: accountQueryKeys.usage.series({ start: window.start, end: window.end, bucket }),
    queryFn: () =>
      ok(
        api.GET("/v1/billing/cost-series", {
          params: { query: { start: window.start, end: window.end, bucket } },
        }),
      ),
    staleTime: 30_000,
  });
}

/** The UTC calendar month an instant falls in, as the window the API takes. */
export function calendarMonthWindow(at: Date): UsageCostWindow {
  const start = new Date(Date.UTC(at.getUTCFullYear(), at.getUTCMonth(), 1));
  const end = new Date(Date.UTC(at.getUTCFullYear(), at.getUTCMonth() + 1, 1));
  return { start: start.toISOString(), end: end.toISOString() };
}
