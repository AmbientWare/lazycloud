import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { Panel } from "@/components/shared/Panel";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import { PageFacts } from "@/components/shared/WorkspacePage/PageFacts";
import { countLabel } from "@/lib/format";
import { formatCostNanos } from "@/lib/money";
import { accountCostSeriesQueryOptions } from "@/lib/queries/usage";
import { useWorkspace } from "@/lib/workspace-context";

import { AccountCeilingLine } from "./-components/AccountCeilingLine";
import { AppCostAccordion } from "./-components/AppCostAccordion";
import { usageRange, usageRangeKeys, type UsageRangeKey } from "./-components/ranges";
import { SpendChart } from "./-components/SpendChart";
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

/**
 * What this account is spending, when it spent it, and which app it went to.
 *
 * Account-wide rather than scoped to the workspace in the sidebar: the provider
 * invoices an account, so somebody running dev, staging and prod wants one
 * figure covering the three. Every row therefore names the workspace it was
 * incurred in.
 *
 * Two regions, one window. The chart answers when, the list answers who, and
 * the range control above both is what keeps a total from sitting beside a
 * shape it does not add up to.
 */
function UsagePage() {
  const { workspaces } = useWorkspace();
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  // Fixed at the range rather than at the clock, so paging through the list and
  // the total above it cannot straddle a boundary crossed mid-session.
  const range = useMemo(() => usageRange(search.range, new Date()), [search.range]);
  const series = useQuery(accountCostSeriesQueryOptions(range.window, range.bucket));

  return (
    <WorkspacePage
      title="Usage"
      description={
        <>
          <PageFacts
            items={[
              series.data ? formatCostNanos(series.data.cost_nanos, series.data.currency) : null,
              countLabel(workspaces.length, "workspace"),
              range.caption,
            ]}
          />
          <AccountCeilingLine />
        </>
      }
      actions={
        <UsageRangeControl
          value={range.key}
          onChange={(next) => void navigate({ search: { range: next }, replace: true })}
        />
      }
      contentClassName="flex min-h-0 flex-col gap-3 overflow-y-auto lg:overflow-hidden"
    >
      <Panel
        title="Spend over time"
        description={`${range.bucket === "hour" ? "Hourly" : "Daily"}, UTC`}
        // The height belongs to the panel rather than to its content: the content
        // is a flex child with a zero basis, so a height set on it contributes
        // nothing to the panel's own size and the chart collapses to a strip.
        className="h-60 shrink-0 sm:h-72"
        contentClassName="overflow-hidden p-3"
      >
        <SpendChart window={range.window} bucket={range.bucket} caption={range.caption} />
      </Panel>
      <Panel
        title="Apps"
        description="Open an app to see its workloads"
        className="min-h-[22rem] flex-1"
        contentClassName="flex min-h-0 flex-col overflow-hidden"
      >
        <AppCostAccordion window={range.window} caption={range.caption} />
      </Panel>
    </WorkspacePage>
  );
}
