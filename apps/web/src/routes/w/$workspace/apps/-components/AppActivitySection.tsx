import { Panel } from "@/components/shared/Panel";
import { Skeleton } from "@/components/ui/skeleton";
import type { TaskTimeWindowBucket } from "@/lib/api/schemas";
import { cn } from "@/lib/utils";

import { ActivitySparkline } from "./ActivitySparkline";
import { appRunActivity } from "./app-activity-buckets";

export function AppActivitySection({
  buckets,
  pending,
  error,
}: {
  buckets: TaskTimeWindowBucket[] | undefined;
  pending: boolean;
  error: string | undefined;
}) {
  const activity = appRunActivity(buckets);

  return (
    <div
      role="region"
      aria-labelledby="app-activity-heading"
      className="min-h-[16rem] lg:h-full lg:min-h-0"
    >
      <Panel
        title={<span id="app-activity-heading">Activity</span>}
        description="Hourly tasks and failures over the last 24 hours"
        action={<ActivityLegend />}
        className="h-full"
        contentClassName="overflow-hidden p-0"
      >
        {error ? (
          <div className="flex h-full min-h-32 items-center justify-center px-4 text-sm text-destructive">
            {error}
          </div>
        ) : pending ? (
          <div className="flex h-full min-h-32 flex-col justify-between gap-3 p-4" aria-hidden="true">
            <div className="flex gap-3">
              <Skeleton className="h-7 w-20" />
              <Skeleton className="h-4 w-16" />
            </div>
            <Skeleton className="h-20 w-full" />
            <Skeleton className="h-3 w-full" />
          </div>
        ) : (
          <div className="flex h-full min-h-0 flex-col px-4 py-2">
            <div className="flex shrink-0 items-baseline gap-2">
              <span className="readout text-2xl leading-none text-foreground">
                {activity.total.toLocaleString()}
              </span>
              <span className="text-xs text-muted-foreground">Tasks</span>
              <span
                className={cn(
                  "ml-auto text-xs",
                  activity.failed > 0 ? "text-destructive" : "text-muted-foreground",
                )}
              >
                {activity.failed.toLocaleString()} failed
              </span>
            </div>
            <ActivitySparkline
              values={activity.tasks}
              failures={activity.failures}
              label="App task and failure activity over the last 24 hours"
              className="mt-2 min-h-8 flex-1"
            />
            <div className="mt-1 flex shrink-0 justify-between text-[10px] text-muted-foreground" aria-hidden="true">
              <span>24h ago</span>
              <span>Now</span>
            </div>
          </div>
        )}
      </Panel>
    </div>
  );
}

function ActivityLegend() {
  return (
    <span className="flex items-center gap-3 text-[10px] text-muted-foreground" aria-hidden="true">
      <span className="flex items-center gap-1.5">
        <span className="size-1.5 bg-positive/75" />
        Successful
      </span>
      <span className="flex items-center gap-1.5">
        <span className="size-1.5 bg-destructive" />
        Failed
      </span>
    </span>
  );
}
