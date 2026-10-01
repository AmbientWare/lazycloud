import { Link } from "@tanstack/react-router";

import { StubKindIcon } from "@/components/shared/StubKindIcon";
import type { Schemas } from "@/lib/api/client";

export function ResourceWorkloadLinks({
  workspaceName,
  workloads,
  limit = 2,
}: {
  workspaceName: string;
  workloads: Schemas["WorkloadRef"][];
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
        <Link
          key={`${workload.app}:${workload.kind}:${workload.name}`}
          to="/w/$workspace/apps/$app/workloads/$kind/$name"
          params={{
            workspace: workspaceName,
            app: workload.app,
            kind: workload.kind,
            name: workload.name,
          }}
          className="interactive-link inline-flex min-w-0 items-center gap-1 text-foreground"
          title={`${workload.app} / ${workload.name}`}
        >
          <StubKindIcon kind={workload.kind} className="size-3 shrink-0" />
          <span className="mono max-w-32 truncate">{workload.name}</span>
        </Link>
      ))}
      {remaining > 0 ? <span>+{remaining} more</span> : null}
    </span>
  );
}
