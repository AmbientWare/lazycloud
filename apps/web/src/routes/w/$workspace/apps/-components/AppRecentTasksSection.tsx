import { Link } from "@tanstack/react-router";

import { Panel } from "@/components/shared/Panel";
import { StatusChip } from "@/components/shared/StatusChip";
import { StubKindIcon } from "@/components/shared/StubKindIcon";
import { Skeleton } from "@/components/ui/skeleton";
import type { Task } from "@/lib/api/schemas";
import { durationBetween, relativeTime, startupBetween } from "@/lib/format";

import { exactTime } from "./app-detail-format";

export function AppRecentTasksSection({
  workspaceName,
  appId,
  tasks,
  pending,
  error,
}: {
  workspaceName: string;
  appId: string;
  tasks: Task[] | undefined;
  pending: boolean;
  error: string | undefined;
}) {
  return (
    <div
      role="region"
      aria-labelledby="app-recent-tasks-heading"
      className="min-h-[24rem] lg:h-full lg:min-h-0"
    >
      <Panel
        title={<span id="app-recent-tasks-heading">Recent tasks</span>}
        description="Latest root activity across this app"
        action={<span className="text-[11px] text-muted-foreground">15 most recent</span>}
        className="h-full"
        contentClassName="p-0"
      >
        {error ? (
          <div className="flex min-h-32 items-center justify-center px-4 text-sm text-destructive">
            {error}
          </div>
        ) : pending ? (
          <div className="space-y-2 p-4" aria-hidden="true">
            {Array.from({ length: 4 }, (_, index) => (
              <Skeleton key={index} className="h-14 w-full" />
            ))}
          </div>
        ) : (tasks?.length ?? 0) === 0 ? (
          <div className="flex min-h-32 items-center justify-center text-sm text-muted-foreground">
            No recent tasks
          </div>
        ) : (
          <RecentRunsList tasks={tasks ?? []} workspaceName={workspaceName} appId={appId} />
        )}
      </Panel>
    </div>
  );
}

function RecentRunsList({
  tasks,
  workspaceName,
  appId,
}: {
  tasks: Task[];
  workspaceName: string;
  appId: string;
}) {
  return (
    <div className="divide-y divide-border/80">
      {tasks.map((task) => (
        <div
          key={task.id}
          className="interactive-row grid min-w-0 grid-cols-[minmax(0,1fr)_auto] gap-x-3 gap-y-1.5 px-4 py-3"
        >
          <span className="min-w-0">
            <Link
              to="/w/$workspace/apps/$appId/tasks/$taskId"
              params={{ workspace: workspaceName, appId, taskId: task.id }}
              className="interactive-link block min-w-0 truncate text-sm font-medium text-foreground"
            >
              {task.name}
            </Link>
            <span className="mt-0.5 flex min-w-0 items-center gap-1.5 text-[11px] text-muted-foreground">
              {task.workload ? (
                <>
                  <StubKindIcon kind={task.workload.kind} className="size-3" />
                  <Link
                    to="/w/$workspace/apps/$appId/workloads/$name"
                    params={{ workspace: workspaceName, appId, name: task.workload.name }}
                    className="interactive-link min-w-0 truncate"
                  >
                    {task.workload.name}
                  </Link>
                  {task.deployment ? (
                    <span className="mono shrink-0">v{task.deployment.version}</span>
                  ) : null}
                </>
              ) : (
                <span>Workload unavailable</span>
              )}
            </span>
          </span>
          <StatusChip status={task.status} live={task.status === "running"} />
          <span className="col-span-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] text-muted-foreground">
            <span>
              Requested{" "}
              <time dateTime={task.created_at} title={exactTime(task.created_at)}>
                {relativeTime(task.created_at)}
              </time>
            </span>
            <span aria-hidden="true">·</span>
            <span>Startup {startupBetween(task.created_at, task.started_at) ?? "-"}</span>
            <span aria-hidden="true">·</span>
            <span>Duration {durationBetween(task.started_at, task.finished_at) ?? "-"}</span>
          </span>
        </div>
      ))}
    </div>
  );
}
