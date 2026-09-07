import { WorkloadLink } from "@/components/shared/WorkloadLink";

import { StubKindIcon } from "@/components/shared/StubKindIcon";
import type { ResourceWorkloadReference } from "@/lib/api/schemas";

export function ResourceWorkloadLinks({
  workspaceName,
  workloads,
  limit = 2,
}: {
  workspaceName: string;
  workloads: ResourceWorkloadReference[];
  limit?: number;
}) {
  if (workloads.length === 0) {
    return <span className="text-[11px] text-muted-foreground">Not attached to a workload</span>;
  }

  const visible = workloads.slice(0, limit);
  const remaining = workloads.length - visible.length;
  return (
    <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted-foreground">
      <span>Used by</span>
      {visible.map((workload) => (
        <WorkloadLink
          key={`${workload.app_id}:${workload.kind}:${workload.name}`}
          workspaceName={workspaceName}
          appId={workload.app_id}
          name={workload.name}
          kind={workload.kind}
          className="interactive-link inline-flex min-w-0 items-center gap-1 text-foreground"
          title={`${workload.app_name} / ${workload.name}`}
        >
          <StubKindIcon kind={workload.kind} className="size-3 shrink-0" />
          <span className="mono max-w-32 truncate">{workload.name}</span>
        </WorkloadLink>
      ))}
      {remaining > 0 ? <span>+{remaining} more</span> : null}
    </span>
  );
}
