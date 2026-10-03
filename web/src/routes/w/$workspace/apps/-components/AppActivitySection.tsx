import { Panel } from "@/components/shared/Panel";
import type { Schemas } from "@/lib/api/client";
import { countLabel } from "@/lib/format";
import { cn } from "@/lib/utils";

import { AppActivityChart, AppActivityLegend } from "./AppActivityChart";
import { appRunActivity } from "./app-activity-buckets";

/**
 * The app's tasks per hour over the last day. The chart keeps its size while
 * the read is pending.
 */
export function AppActivitySection({
  series,
  runningContainers,
  pending,
  error,
}: {
  series: Schemas["ActivitySeries"][] | undefined;
  runningContainers: number | undefined;
  pending: boolean;
  error: string | undefined;
}) {
  const activity = appRunActivity(series);
  const count = (value: number) => (pending ? "—" : value.toLocaleString());

  return (
    <div
      role="region"
      aria-labelledby="app-activity-heading"
      aria-busy={pending}
      className="min-h-[16rem] lg:h-full lg:min-h-0"
    >
      <Panel
        title={<span id="app-activity-heading">Activity</span>}
        action={<AppActivityLegend activity={activity} />}
        className="h-full"
        contentClassName="overflow-hidden p-0"
      >
        {error ? (
          <div className="flex h-full min-h-32 items-center justify-center px-4 text-sm text-destructive">
            {error}
          </div>
        ) : (
          <div className="flex h-full min-h-0 flex-col px-4 py-2">
            <div className="flex shrink-0 items-baseline gap-2">
              <span className="readout text-2xl leading-none text-foreground">
                {count(activity.total)}
              </span>
              <span className="text-xs text-muted-foreground">Tasks</span>
              <span
                className={cn(
                  "text-xs",
                  activity.totals.failed > 0 ? "text-destructive" : "text-muted-foreground",
                )}
              >
                {count(activity.totals.failed)} failed
              </span>
              <span
                className={cn(
                  "text-xs",
                  activity.totals.inFlight > 0 ? "text-warning" : "text-muted-foreground",
                )}
              >
                {count(activity.totals.inFlight)} pending
              </span>
              {/* Counted now, not over the window the figures beside it cover —
                  labelled "running" rather than given the same 24-hour framing. */}
              {runningContainers === undefined ? null : (
                <span className="ml-auto shrink-0 text-xs text-muted-foreground">
                  {countLabel(runningContainers, "container")} running
                </span>
              )}
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
