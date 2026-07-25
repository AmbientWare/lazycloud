import { cn } from "@/lib/utils";

export function ActivitySparkline({
  values,
  failures = [],
  label,
  className,
}: {
  values: number[];
  failures?: number[];
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
        const successful = Math.max(value - failed, 0);
        const height = value === 0 ? 2 : Math.max((value / max) * 100, 8);

        return (
          <span
            key={index}
            data-bucket={index}
            className="flex h-full min-w-0 flex-1 items-end"
            title={`${value} task${value === 1 ? "" : "s"}, ${failed} failed`}
          >
            <span
              className={cn(
                "flex w-full flex-col overflow-hidden",
                value === 0 && "bg-border",
              )}
              style={{ height: `${height}%` }}
            >
              {failed > 0 ? (
                <span
                  data-series="failures"
                  className="min-h-0 w-full bg-destructive"
                  style={{ flexGrow: failed }}
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
