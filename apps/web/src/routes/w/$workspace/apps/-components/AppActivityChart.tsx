import type { TaskActivityBand } from "@/lib/format";
import { cn } from "@/lib/utils";

import {
  ActivitySparkline,
  taskActivityBandLabel,
  taskActivityBandStyle,
} from "./ActivitySparkline";
import type { AppRunActivity } from "./app-activity-buckets";

/** One app's 24 hours, drawn the same way wherever it appears.

    The card and the app view once fed the same bars from different shapes: the
    view resolved every status into a band, the card named only its failures and
    left the rest unattributed. The bars agreed and the colours did not, so the
    same hour was grey in one place and green in the other. Both now pass an
    `AppRunActivity` and this decides the rest.

    Legend and axis are the parts a card has no room for, so they are asked for
    rather than assumed. Everything that carries meaning, the bands, their
    order, their colours and their tooltips, is not optional. */
export function AppActivityChart({
  activity,
  label,
  className,
  chartClassName,
  showLegend = false,
  showAxis = false,
}: {
  activity: AppRunActivity;
  label: string;
  className?: string;
  chartClassName?: string;
  showLegend?: boolean;
  showAxis?: boolean;
}) {
  return (
    <div className={cn("flex min-h-0 flex-col", className)}>
      {showLegend ? <AppActivityLegend activity={activity} className="mb-1 self-end" /> : null}
      <ActivitySparkline
        values={activity.tasks}
        bands={activity.bands}
        label={label}
        className={chartClassName}
      />
      {showAxis ? (
        <div
          className="mt-1 flex shrink-0 justify-between text-[10px] text-muted-foreground"
          aria-hidden="true"
        >
          <span>24h ago</span>
          <span>Now</span>
        </div>
      ) : null}
    </div>
  );
}

export function AppActivityLegend({
  activity,
  className,
}: {
  activity: AppRunActivity;
  className?: string;
}) {
  // "Other" only earns a swatch when something landed there; cancelled work and
  // statuses this build does not know are both rare enough to be noise otherwise.
  const bands: TaskActivityBand[] = ["succeeded", "inFlight", "failed"];
  if (activity.totals.other > 0) bands.push("other");

  return (
    <span
      className={cn("flex items-center gap-3 text-[10px] text-muted-foreground", className)}
      aria-hidden="true"
    >
      {bands.map((band) => (
        <span key={band} className="flex items-center gap-1.5">
          <span className={cn("size-1.5", taskActivityBandStyle[band])} />
          {taskActivityBandLabel[band]}
        </span>
      ))}
    </span>
  );
}
