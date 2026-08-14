import { useQuery } from "@tanstack/react-query";

import { Panel } from "@/components/shared/Panel";
import { StatusChip } from "@/components/shared/StatusChip";
import { Skeleton } from "@/components/ui/skeleton";
import type { BillingPlan, BillingSummary } from "@/lib/api/schemas";
import { relativeTime } from "@/lib/format";
import { formatCostNanos } from "@/lib/money";
import { billingSummaryQueryOptions } from "@/lib/queries/billing";
import { cn } from "@/lib/utils";

import { PaymentMethodPanel } from "./PaymentMethodPanel";

/**
 * What this account is on, and how much of the period it has spent.
 *
 * The account rather than the workspace: the provider invoices a person, and
 * somebody holding three workspaces holds one payment relationship — so this
 * reads the same in every workspace they switch to.
 */
export function AccountStandingPanel() {
  const summary = useQuery(billingSummaryQueryOptions());

  return (
    <Panel
      title="Account"
      description="What this account is on, and what it has left to spend"
      action={summary.data ? <StandingChip summary={summary.data} /> : null}
    >
      <div className="flex flex-col gap-4 p-4">
        {summary.isPending ? (
          <div className="space-y-2" aria-hidden="true">
            <Skeleton className="h-4 w-56" />
            <Skeleton className="h-2 w-full" />
          </div>
        ) : !summary.data ? (
          <p className="text-sm text-muted-foreground">
            The allowance for this account could not be read.
          </p>
        ) : !summary.data.plan ? (
          <p className="text-sm text-muted-foreground">
            This account is not on a plan, so nothing can be started on it. Subscribe to put it
            back on one.
          </p>
        ) : !summary.data.plan.allowance ? (
          <p className="text-sm text-muted-foreground">
            This period is being renewed. The next allowance opens when the provider confirms it.
          </p>
        ) : (
          <AllowanceMeter
            allowance={summary.data.plan.allowance}
            currency={summary.data.currency}
          />
        )}
        <PaymentMethodPanel
          portalAvailable={summary.data?.portal_available ?? false}
          plan={summary.data?.plan ?? null}
        />
      </div>
    </Panel>
  );
}

const planNames: Record<BillingPlan["id"], string> = { free: "Free", team: "Team" };

function StandingChip({ summary }: { summary: BillingSummary }) {
  if (summary.status === "past_due") {
    return <StatusChip status="past due" />;
  }
  if (!summary.plan) {
    return <StatusChip status="No plan" />;
  }
  return <StatusChip status={planNames[summary.plan.id]} />;
}

function AllowanceMeter({
  allowance,
  currency,
}: {
  allowance: NonNullable<BillingPlan["allowance"]>;
  currency: string;
}) {
  // Spending past the allowance is normal — the overage is billed rather than
  // refused — so the bar fills and the figure beside it goes on counting rather
  // than being clamped.
  const filled = allowance.allowance_nanos
    ? Math.min(100, (allowance.spent_nanos / allowance.allowance_nanos) * 100)
    : 100;
  const overspent = allowance.remaining_nanos < 0;

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <p className="font-mono text-lg">
          {formatCostNanos(allowance.spent_nanos, currency)}
          <span className="text-sm text-muted-foreground">
            {" "}
            of {formatCostNanos(allowance.allowance_nanos, currency)} included
          </span>
        </p>
        <p className="text-xs text-muted-foreground">
          Period ends{" "}
          <time dateTime={allowance.period_ended_at}>
            {relativeTime(allowance.period_ended_at)}
          </time>
        </p>
      </div>
      <div
        role="meter"
        aria-label="Included allowance spent"
        aria-valuemin={0}
        aria-valuemax={allowance.allowance_nanos}
        aria-valuenow={allowance.spent_nanos}
        className="h-2 w-full overflow-hidden rounded-full bg-muted"
      >
        <div
          className={cn("h-full rounded-full", overspent ? "bg-destructive" : "bg-brand")}
          style={{ width: `${filled}%` }}
        />
      </div>
      <p className="text-xs text-muted-foreground">
        {overspent
          ? `${formatCostNanos(-allowance.remaining_nanos, currency)} beyond the included amount, billed on this period's invoice.`
          : `${formatCostNanos(allowance.remaining_nanos, currency)} left in this period.`}
      </p>
    </div>
  );
}
