import { StatusChip } from "@/components/shared/StatusChip";
import { Skeleton } from "@/components/ui/skeleton";
import type { App, Deployment } from "@/lib/api/schemas";
import { relativeTime } from "@/lib/format";

import { exactTime, formatCount } from "./app-detail-format";

export function AppDetailHeader({
  app,
  latestDeployment,
  workloadCount,
  activeWorkloads,
  actions,
}: {
  app: App | undefined;
  latestDeployment: Deployment | undefined;
  workloadCount: number;
  activeWorkloads: number;
  actions?: ReactNode;
}) {
  return (
    <header className="panel shrink-0 rounded-md bg-card px-4 py-3">
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          {app ? (
            <h1 className="truncate text-xl font-semibold text-foreground">{app.name}</h1>
          ) : (
            <Skeleton className="h-6 w-48" aria-hidden="true" />
          )}
          <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
            <span>{formatCount(workloadCount, "workload")}</span>
            <span aria-hidden="true">·</span>
            <span>{formatCount(activeWorkloads, "active version")}</span>
            <span aria-hidden="true">·</span>
            {latestDeployment ? (
              <span title={exactTime(latestDeployment.created_at)}>
                Last deployed{" "}
                <time dateTime={latestDeployment.created_at}>
                  {relativeTime(latestDeployment.created_at)}
                </time>
              </span>
            ) : (
              <span>No deployments yet</span>
            )}
          </div>
        </div>
        <div className="flex min-w-0 flex-wrap items-center justify-end gap-2">
          <StatusChip
            status={
              activeWorkloads > 0 ? "deployed" : latestDeployment ? "inactive" : "not deployed"
            }
            live={activeWorkloads > 0}
          />
          {actions}
        </div>
      </div>
    </header>
  );
}
import type { ReactNode } from "react";
