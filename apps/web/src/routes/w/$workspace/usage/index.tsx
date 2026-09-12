import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { Panel } from "@/components/shared/Panel";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import { accountCostSeriesQueryOptions } from "@/lib/queries/usage";

import { UsageCostBreakdown } from "./-components/UsageCostBreakdown";
import { usageRange, usageRangeKeys, type UsageRangeKey } from "./-components/ranges";
import { SpendChart } from "./-components/SpendChart";
import { SpendTotals } from "./-components/SpendTotals";
import { UsageRangeControl } from "./-components/UsageRangeControl";

type UsageSearch = {
  range: UsageRangeKey;
};

export const Route = createFileRoute("/w/$workspace/usage/")({
  validateSearch: (search: Record<string, unknown>): UsageSearch => ({
    range: usageRangeKeys.includes(search.range as UsageRangeKey)
      ? (search.range as UsageRangeKey)
      : "month",
  }),
  component: UsagePage,
  errorComponent: RouteErrorFallback,
});

function UsagePage() {
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  // Keep the window fixed while paging so every section reads the same interval.
  const range = useMemo(() => usageRange(search.range, new Date()), [search.range]);
  const series = useQuery(accountCostSeriesQueryOptions(range.window, range.bucket));

  return (
    <WorkspacePage
      title="Usage"
      actions={
        <UsageRangeControl
          value={range.key}
          onChange={(next) => void navigate({ search: { range: next }, replace: true })}
        />
      }
      contentClassName="flex min-h-0 flex-col gap-3 overflow-y-auto lg:overflow-hidden"
    >
      <section aria-label="Spend" className="panel shrink-0 overflow-hidden rounded-md">
        <header className="grid grid-cols-1 items-start gap-x-4 gap-y-5 px-4 py-5 sm:px-5 lg:grid-cols-[minmax(10rem,auto)_minmax(0,1fr)]">
          <SpendTotals series={series.data} error={series.error} />
        </header>
        <div className="h-48 px-3 pb-3 sm:h-56">
          <SpendChart window={range.window} bucket={range.bucket} caption={range.caption} />
        </div>
      </section>
      <Panel
        title="Usage breakdown"
        className="min-h-[22rem] flex-1 lg:min-h-0"
        contentClassName="flex min-h-0 flex-col overflow-hidden"
      >
        <UsageCostBreakdown window={range.window} caption={range.caption} />
      </Panel>
    </WorkspacePage>
  );
}
