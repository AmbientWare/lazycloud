import { Link } from "@tanstack/react-router";
import { ChevronRight } from "lucide-react";

import { StubKindIcon } from "@/components/shared/StubKindIcon";
import type { ContainerDetail } from "@/lib/api/schemas";

export function ContainerLineage({
  record,
  workspaceName,
}: {
  record: ContainerDetail;
  workspaceName: string;
}) {
  if (!record.app && !record.workload && !record.task_id) return null;
  return (
    <nav
      aria-label="Container lineage"
      className="mt-1.5 flex flex-wrap items-center gap-1 text-xs"
    >
      {record.app ? (
        <Link
          to="/w/$workspace/apps/$appId"
          params={{ workspace: workspaceName, appId: record.app.id }}
          className="text-brand hover:underline"
        >
          {record.app.name}
        </Link>
      ) : null}
      {record.app && record.workload ? (
        <ChevronRight className="size-3 text-muted-foreground" />
      ) : null}
      {record.app && record.workload ? (
        <Link
          to="/w/$workspace/apps/$appId/workloads/$name"
          params={{ workspace: workspaceName, appId: record.app.id, name: record.workload.name }}
          className="inline-flex items-center gap-1.5 text-brand hover:underline"
        >
          <StubKindIcon kind={record.workload.kind} className="size-3.5" />
          {record.workload.name}
        </Link>
      ) : record.workload ? (
        <span className="inline-flex items-center gap-1.5 text-muted-foreground">
          <StubKindIcon kind={record.workload.kind} className="size-3.5" />
          {record.workload.name}
        </span>
      ) : null}
      {(record.workload || record.app) && record.task_id ? (
        <ChevronRight className="size-3 text-muted-foreground" />
      ) : null}
      {record.task_id ? (
        <Link
          to="/w/$workspace/tasks/$taskId"
          params={{ workspace: workspaceName, taskId: record.task_id }}
          className="text-brand hover:underline"
        >
          {record.run_name ? `Task: ${record.run_name}` : "Task"}
        </Link>
      ) : null}
    </nav>
  );
}
