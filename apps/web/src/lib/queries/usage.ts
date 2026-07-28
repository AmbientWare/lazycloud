import { queryOptions } from "@tanstack/react-query";

import { ApiError, apiRequest, withWorkspace } from "@/lib/api/client";
import {
  usageBillingOverviewSchema,
  usageBillingWorkloadListSchema,
  type UsageBillingPeriod,
} from "@/lib/api/schemas";
import { clearStoredAuthToken, getStoredAuthToken } from "@/lib/auth";

import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

export type UsageWindow = {
  start: string;
  end: string;
};

export type UsageWindowSelection = UsageWindow | { period: UsageBillingPeriod };

export type UsageBillingExport = {
  blob: Blob;
  filename: string;
};

export function usageBillingOverviewQueryOptions(
  workspaceId: string,
  window: UsageWindowSelection,
  bucketSeconds: number,
) {
  return queryOptions({
    queryKey: workspaceQueryKeys.usage.overview(workspaceId, usageWindowKey(window), bucketSeconds),
    queryFn: () => {
      const params = usageWindowSearchParams(window, bucketSeconds);
      return apiRequest(
        withWorkspace(`/api/v1/usage/billing?${params.toString()}`, workspaceId),
        usageBillingOverviewSchema,
      );
    },
    staleTime: 30_000,
    refetchInterval: "period" in window ? 60_000 : false,
    meta: workspaceLiveQueryMeta(true),
  });
}

function usageWindowKey(window: UsageWindowSelection) {
  return "period" in window
    ? { period: window.period, start: null, end: null }
    : { period: null, start: window.start, end: window.end };
}

function usageWindowSearchParams(
  window: UsageWindowSelection,
  bucketSeconds: number,
): URLSearchParams {
  const params = new URLSearchParams({ bucket_seconds: String(bucketSeconds) });
  if ("period" in window) {
    params.set("period", window.period);
  } else {
    params.set("start", window.start);
    params.set("end", window.end);
  }
  return params;
}

export function usageBillingWorkloadsQueryOptions(
  workspaceId: string,
  appId: string,
  window: UsageWindow,
  bucketSeconds: number,
) {
  return queryOptions({
    queryKey: workspaceQueryKeys.usage.workloads(
      workspaceId,
      appId,
      window.start,
      window.end,
      bucketSeconds,
    ),
    queryFn: () => {
      const params = new URLSearchParams({
        app_id: appId,
        start: window.start,
        end: window.end,
        bucket_seconds: String(bucketSeconds),
      });
      return apiRequest(
        withWorkspace(`/api/v1/usage/billing/workloads?${params.toString()}`, workspaceId),
        usageBillingWorkloadListSchema,
      );
    },
    staleTime: 60_000,
    meta: workspaceLiveQueryMeta(true),
  });
}

export async function downloadUsageBillingCsv(
  workspaceId: string,
  window: UsageWindow,
  bucketSeconds: number,
): Promise<UsageBillingExport> {
  const params = new URLSearchParams({
    start: window.start,
    end: window.end,
    bucket_seconds: String(bucketSeconds),
  });
  const headers = new Headers();
  const token = getStoredAuthToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(
    withWorkspace(`/api/v1/usage/billing.csv?${params.toString()}`, workspaceId),
    { headers, credentials: "include" },
  );
  if (!response.ok) {
    const body = await response.text().catch(() => "");
    if (response.status === 401) clearStoredAuthToken();
    throw new ApiError(response.status, response.statusText, body);
  }
  return {
    blob: await response.blob(),
    filename: attachmentFilename(response.headers.get("content-disposition")),
  };
}

function attachmentFilename(contentDisposition: string | null): string {
  const match = contentDisposition?.match(/filename="?([^";]+)"?/i);
  return match?.[1]?.trim() || "usage.csv";
}
