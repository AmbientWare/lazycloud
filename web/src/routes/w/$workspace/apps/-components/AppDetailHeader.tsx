import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { PageFacts } from "@/components/shared/WorkspacePage/PageFacts";
import { countLabel } from "@/lib/format";
import type { DeployedWorkload } from "@/lib/queries/deployments";

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
  latestDeployment: DeployedWorkload | undefined;
  workloadCount: number;
  activeWorkloads: number;
}) {
  return (
    <PageFacts
      items={[
        countLabel(workloadCount, "workload"),
        countLabel(activeWorkloads, "active version"),
        latestDeployment ? (
          <span>
            Last deployed{" "}
            <LiveRelativeTime value={latestDeployment.deployed_at ?? latestDeployment.created_at} />
          </span>
        ) : (
          "No deployments yet"
        ),
      ]}
    />
  );
}
