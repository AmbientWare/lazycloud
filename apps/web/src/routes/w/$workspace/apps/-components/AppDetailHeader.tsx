import { StatusChip } from "@/components/shared/StatusChip";
import type { App, Deployment } from "@/lib/api/schemas";
import { relativeTime } from "@/lib/format";

import { exactTime, formatCount } from "./app-detail-format";

/**
 * What an app is, under its name.
 *
 * A `description` for `WorkspacePage` rather than a header of its own: the title
 * beside it is the same element every other page renders, and a second copy of
 * it here is the one that drifts.
 */
export function AppDetailFacts({
  latestDeployment,
  workloadCount,
  activeWorkloads,
}: {
  latestDeployment: Deployment | undefined;
  workloadCount: number;
  activeWorkloads: number;
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
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
  );
}

export function AppDetailStatus({
  app,
  latestDeployment,
  activeWorkloads,
}: {
  app: App | undefined;
  latestDeployment: Deployment | undefined;
  activeWorkloads: number;
}) {
  if (!app) return null;
  return (
    <StatusChip
      status={activeWorkloads > 0 ? "deployed" : latestDeployment ? "inactive" : "not deployed"}
      live={activeWorkloads > 0}
    />
  );
}
