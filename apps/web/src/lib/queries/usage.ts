import { infiniteQueryOptions } from "@tanstack/react-query";

import { apiRequest, withWorkspace } from "@/lib/api/client";
import { usageCostListSchema, type UsageCostGroupKey } from "@/lib/api/schemas";

import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

export type UsageCostWindow = {
  start: string;
  end: string;
};

export type UsageCostScope = {
  groupBy: UsageCostGroupKey;
  appId?: string;
  workloadId?: string;
  limit?: number;
};

/**
 * A workspace's cost over one window, grouped at one level of the product model.
 *
 * Paged rather than fetched whole: rows come back most expensive first, so the
 * first page already answers what somebody opened the page to ask, and the rest
 * continues in its own scroll region.
 */
export function usageCostsQueryOptions(
  workspaceId: string,
  window: UsageCostWindow,
  scope: UsageCostScope,
) {
  const { groupBy, appId, workloadId, limit = 50 } = scope;
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.usage.costs(workspaceId, {
      start: window.start,
      end: window.end,
      groupBy,
      appId: appId ?? null,
      workloadId: workloadId ?? null,
    }),
    initialPageParam: "",
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({
        start: window.start,
        end: window.end,
        group_by: groupBy,
        limit: String(limit),
      });
      if (appId) params.set("app_id", appId);
      if (workloadId) params.set("workload_id", workloadId);
      if (pageParam) params.set("cursor", pageParam);
      return apiRequest(
        withWorkspace(`/api/v1/usage/costs?${params.toString()}`, workspaceId),
        usageCostListSchema,
      );
    },
    getNextPageParam: (page) => page.next || undefined,
    staleTime: 30_000,
    meta: workspaceLiveQueryMeta(true),
  });
}

/**
 * Every workspace this account is invoiced for, over one window.
 *
 * Not workspace-scoped and not keyed by one: the provider invoices an account,
 * so the figure someone opens this page to see spans the workspaces they hold
 * rather than the one the sidebar happens to have selected.
 */
export function accountCostsQueryOptions(window: UsageCostWindow, scope: UsageCostScope) {
  const { groupBy, limit = 50 } = scope;
  return infiniteQueryOptions({
    queryKey: ["account", "costs", { start: window.start, end: window.end, groupBy }] as const,
    initialPageParam: "",
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({
        start: window.start,
        end: window.end,
        group_by: groupBy,
        limit: String(limit),
      });
      if (pageParam) params.set("cursor", pageParam);
      return apiRequest(`/api/v1/billing/costs?${params.toString()}`, usageCostListSchema);
    },
    getNextPageParam: (page) => page.next || undefined,
    staleTime: 30_000,
  });
}

/** The UTC calendar month an instant falls in, as the window the API takes. */
export function calendarMonthWindow(at: Date): UsageCostWindow {
  const start = new Date(Date.UTC(at.getUTCFullYear(), at.getUTCMonth(), 1));
  const end = new Date(Date.UTC(at.getUTCFullYear(), at.getUTCMonth() + 1, 1));
  return { start: start.toISOString(), end: end.toISOString() };
}
