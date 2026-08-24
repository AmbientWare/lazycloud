import { cn } from "@/lib/utils";

export function ActivitySparkline({
  values,
  failures = [],
  pending = [],
  label,
  className,
}: {
  values: number[];
  failures?: number[];
  /** Tasks still waiting, drawn between failed and successful.

      Without it the bar shows only two states and everything that is not a
      failure is painted as a success, so an app whose tasks are all stuck looks
      like one where they all worked. */
  pending?: number[];
  label: string;
  className?: string;
}) {
  const normalizedValues = values.length > 0 ? values : [0];
  const max = Math.max(...normalizedValues, 1);

  return (
    <div
      className={cn("flex h-12 min-w-32 items-end gap-0.5", className)}
      role="img"
      aria-label={label}
    >
      {normalizedValues.map((value, index) => {
        const failed = Math.min(Math.max(failures[index] ?? 0, 0), value);
        const waiting = Math.min(Math.max(pending[index] ?? 0, 0), Math.max(value - failed, 0));
        const successful = Math.max(value - failed - waiting, 0);
        const height = value === 0 ? 2 : Math.max((value / max) * 100, 8);

        return (
          <span
            key={index}
            data-bucket={index}
            className="flex h-full min-w-0 flex-1 items-end"
            title={`${value} task${value === 1 ? "" : "s"}, ${failed} failed, ${waiting} pending`}
          >
            <span
              className={cn("flex w-full flex-col overflow-hidden", value === 0 && "bg-border")}
              style={{ height: `${height}%` }}
            >
              {failed > 0 ? (
                <span
                  data-series="failures"
                  className="min-h-0 w-full bg-destructive"
                  style={{ flexGrow: failed }}
                />
              ) : null}
              {waiting > 0 ? (
                <span
                  data-series="pending"
                  className="min-h-0 w-full bg-muted-foreground/50"
                  style={{ flexGrow: waiting }}
                />
              ) : null}
              {successful > 0 ? (
                <span
                  data-series="successful"
                  className="min-h-0 w-full bg-positive/75"
                  style={{ flexGrow: successful }}
                />
              ) : null}
            </span>
          </span>
        );
      })}
    </div>
  );
}
