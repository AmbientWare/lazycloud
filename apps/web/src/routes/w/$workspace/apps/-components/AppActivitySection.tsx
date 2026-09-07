import { Panel } from "@/components/shared/Panel";
import { Skeleton } from "@/components/ui/skeleton";
import type { TaskTimeWindowBucket } from "@/lib/api/schemas";
import { countLabel } from "@/lib/format";
import { cn } from "@/lib/utils";

import { AppActivityChart, AppActivityLegend } from "./AppActivityChart";
import { appRunActivity } from "./app-activity-buckets";

export function AppActivitySection({
  buckets,
  runningContainers,
  pending,
  error,
}: {
  buckets: TaskTimeWindowBucket[] | undefined;
  runningContainers: number;
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
        description="Hourly tasks by outcome over the last 24 hours"
        action={<AppActivityLegend activity={activity} />}
        className="h-full"
        contentClassName="overflow-hidden p-0"
      >
        {error ? (
          <div className="flex h-full min-h-32 items-center justify-center px-4 text-sm text-destructive">
            {error}
          </div>
        ) : pending ? (
          <div
            className="flex h-full min-h-32 flex-col justify-between gap-3 p-4"
            aria-hidden="true"
          >
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
                  "text-xs",
                  activity.totals.failed > 0 ? "text-destructive" : "text-muted-foreground",
                )}
              >
                {activity.totals.failed.toLocaleString()} failed
              </span>
              <span
                className={cn(
                  "text-xs",
                  activity.totals.inFlight > 0 ? "text-warning" : "text-muted-foreground",
                )}
              >
                {activity.totals.inFlight.toLocaleString()} pending
              </span>
              {/* Counted now, not over the window the figures beside it cover —
                  labelled "running" rather than given the same 24-hour framing. */}
              <span className="ml-auto shrink-0 text-xs text-muted-foreground">
                {countLabel(runningContainers, "container")} running
              </span>
            </div>
            <AppActivityChart
              activity={activity}
              label="App task activity by outcome over the last 24 hours"
              className="mt-2 flex-1"
              chartClassName="min-h-8 flex-1"
              showAxis
            />
          </div>
        )}
      </Panel>
    </div>
  );
}
