import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";
import type {
  UsageCostBucket,
  UsageCostCategory,
  UsageCostGroupKey,
  UsageCostList,
  UsageCostRow,
  UsageCostSeries,
} from "@/lib/api/schemas";

import { accountQueryKeys } from "./workspace-keys";

export type UsageCostWindow = {
  start: string;
  end: string;
};

export type UsageCostScope = {
  groupBy: UsageCostGroupKey;
  appId?: string;
  workspaceId?: string;
  category?: UsageCostCategory;
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
  const { groupBy, appId, workspaceId, category, limit = 50 } = scope;
  return infiniteQueryOptions({
    queryKey: accountQueryKeys.usage.costs({
      start: window.start,
      end: window.end,
      groupBy,
      appId: appId ?? null,
      workspaceId: workspaceId ?? null,
      category: category ?? null,
    }),
    initialPageParam: "",
    queryFn: async ({ pageParam }) =>
      viewCostPage(
        await ok(
          api.GET("/v1/billing/costs", {
            params: {
              query: {
                start: window.start,
                end: window.end,
                group_by: groupBy,
                limit,
                // An empty app ID is the breakdown's "no app"; the unattributed
                // category already narrows to that.
                app_id: appId || undefined,
                workspace_id: workspaceId || undefined,
                category,
                cursor: pageParam || undefined,
              },
            },
          }),
        ),
      ),
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
    queryFn: async () =>
      viewCostSeries(
        await ok(
          api.GET("/v1/billing/cost-series", {
            params: { query: { start: window.start, end: window.end, bucket } },
          }),
        ),
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

/* The API omits the names and IDs a row has none of; the views read them as empty. */

function viewCostRow(row: Schemas["UsageCostRow"]): UsageCostRow {
  return {
    workspace_id: row.workspace_id,
    workspace_name: row.workspace_name ?? "",
    app_id: row.app_id ?? "",
    app_name: row.app_name ?? "",
    workload_id: row.workload_id ?? "",
    workload_name: row.workload_name ?? "",
    workload_kind: row.workload_kind ?? "",
    task_id: row.task_id ?? "",
    disk_id: row.disk_id ?? "",
    disk_name: row.disk_name ?? "",
    category: row.category ?? "",
    cost_nanos: row.cost_nanos,
    components: row.components,
  };
}

function viewCostPage(page: Schemas["UsageCostPage"]): UsageCostList {
  return {
    workspace_id: "",
    start: page.start,
    end: page.end,
    currency: page.currency,
    group_by: page.group_by,
    cost_nanos: page.cost_nanos,
    data: page.rows.map(viewCostRow),
    next: page.next_cursor ?? "",
  };
}

function viewCostSeries(series: Schemas["UsageCostSeries"]): UsageCostSeries {
  return {
    start: series.start,
    end: series.end,
    currency: series.currency,
    bucket: series.bucket,
    cost_nanos: series.cost_nanos,
    subscription_credit_nanos: series.subscription_credit_nanos,
    data: series.intervals,
  };
}
