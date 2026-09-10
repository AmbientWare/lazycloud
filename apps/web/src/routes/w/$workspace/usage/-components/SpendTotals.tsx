import { Skeleton } from "@/components/ui/skeleton";
import { billedDimensions, type UsageCostSeries } from "@/lib/api/schemas";
import { formatCostNanos } from "@/lib/money";

import { COST_DIMENSIONS } from "./cost-colors";

export function SpendTotals({
  series,
  error,
}: {
  series: UsageCostSeries | undefined;
  error: Error | null;
}) {
  return (
    <>
      <dl className="col-span-2 col-start-1 row-start-1 min-w-0 sm:col-span-1">
        <dt className="text-xs text-muted-foreground">Total spend</dt>
        <dd className="mt-4 sm:mt-2">
          {error ? (
            <span aria-label="Spend unavailable" className="readout text-3xl sm:text-4xl">
              —
            </span>
          ) : series ? (
            <span className="readout break-all text-3xl leading-none sm:text-4xl">
              {formatCostNanos(series.cost_nanos, series.currency)}
            </span>
          ) : (
            <Skeleton className="h-9 w-36 max-w-full sm:h-10" />
          )}
        </dd>
      </dl>
      <dl
        aria-label="Spend by category"
        className="col-span-2 flex flex-wrap gap-x-8 gap-y-3 sm:gap-x-10 lg:col-span-1 lg:col-start-2 lg:row-start-1 lg:self-end lg:pl-4"
      >
        {billedDimensions.map((dimension) => {
          const totals = series?.data.flatMap((interval) =>
            interval.dimensions.filter((total) => total.dimension === dimension),
          );
          return (
            <div key={dimension}>
              <dt className="text-xs text-muted-foreground">{COST_DIMENSIONS[dimension].label}</dt>
              <dd className="mt-1">
                {error ? (
                  <span aria-label="Spend unavailable" className="text-sm text-muted-foreground">
                    —
                  </span>
                ) : !series ? (
                  <Skeleton className="h-5 w-20" />
                ) : totals?.length ? (
                  <span className="mono text-sm tabular-nums">
                    {formatCostNanos(
                      totals.reduce((sum, total) => sum + total.cost_nanos, 0),
                      series.currency,
                    )}
                  </span>
                ) : (
                  <span aria-label="No metered usage" className="text-sm text-muted-foreground">
                    —
                  </span>
                )}
              </dd>
            </div>
          );
        })}
      </dl>
    </>
  );
}
