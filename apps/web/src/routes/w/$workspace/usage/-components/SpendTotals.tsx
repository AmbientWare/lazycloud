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
      <div className="flex flex-wrap items-end gap-x-8 gap-y-2">
        <dl>
          <dt className="text-xs text-muted-foreground">Usage cost</dt>
          <dd className="mt-1">
            {error ? (
              <span aria-label="Usage cost unavailable">—</span>
            ) : series ? (
              <span className="content-transition readout text-3xl">
                {formatCostNanos(series.cost_nanos, series.currency)}
              </span>
            ) : (
              <Skeleton className="h-9 w-32" />
            )}
          </dd>
        </dl>
        <dl className="pb-1">
          <dt className="text-xs text-muted-foreground">Covered by subscription credits</dt>
          <dd className="mt-1">
            {error ? (
              <span aria-label="Credit coverage unavailable">—</span>
            ) : series ? (
              <span className="content-transition mono text-sm tabular-nums text-positive">
                {formatCostNanos(series.subscription_credit_nanos, series.currency)}
              </span>
            ) : (
              <Skeleton className="h-5 w-20" />
            )}
          </dd>
        </dl>
      </div>
      <dl aria-label="Usage by category" className="flex flex-wrap gap-x-5 gap-y-2 pb-1 text-xs">
        {billedDimensions.map((dimension) => {
          const totals = series?.data.flatMap((interval) =>
            interval.dimensions.filter((total) => total.dimension === dimension),
          );
          return (
            <div key={dimension} className="flex items-center gap-2">
              <dt className="flex items-center gap-1.5 text-muted-foreground">
                <span
                  aria-hidden="true"
                  className="size-1.5"
                  style={{ background: COST_DIMENSIONS[dimension].color }}
                />
                {COST_DIMENSIONS[dimension].label}
              </dt>
              <dd className="mono tabular-nums">
                {error ? (
                  "—"
                ) : !series ? (
                  <Skeleton className="h-4 w-12" />
                ) : totals?.length ? (
                  formatCostNanos(
                    totals.reduce((sum, total) => sum + total.cost_nanos, 0),
                    series.currency,
                  )
                ) : (
                  "—"
                )}
              </dd>
            </div>
          );
        })}
      </dl>
    </>
  );
}
