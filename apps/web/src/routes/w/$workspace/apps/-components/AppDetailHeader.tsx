import { StatusChip } from "@/components/shared/StatusChip";
import { countLabel } from "@/components/shared/WorkspacePage/countLabel";
import { PageFacts } from "@/components/shared/WorkspacePage/PageFacts";
import type { App, Deployment } from "@/lib/api/schemas";
import { exactTime, relativeTime } from "@/lib/format";

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
    <PageFacts
      items={[
        countLabel(workloadCount, "workload"),
        countLabel(activeWorkloads, "active version"),
        latestDeployment ? (
          <span title={exactTime(latestDeployment.created_at)}>
            Last deployed{" "}
            <time dateTime={latestDeployment.created_at}>
              {relativeTime(latestDeployment.created_at)}
            </time>
          </span>
        ) : (
          "No deployments yet"
        ),
      ]}
    />
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
