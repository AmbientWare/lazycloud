import { CreditCard, ExternalLink, LoaderCircle, Sparkles } from "lucide-react";

import { Panel } from "@/components/shared/Panel";
import { StatusChip } from "@/components/shared/StatusChip";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { BillingPlan, BillingSummary } from "@/lib/api/schemas";
import { relativeTime } from "@/lib/format";
import { exactDollars, formatCostNanos } from "@/lib/money";
import { publishedPlans } from "@/routes/-marketing/pricingCatalog";
import { cn } from "@/lib/utils";

import { useBillingSettingsController } from "./controller";
import { PlanDialog } from "./PlanDialog";

/**
 * What this account is on, what it has left to spend, and how to change either.
 *
 * The account rather than the workspace, which is why it lives here and not
 * under `/w/<workspace>/`: the provider invoices a person, and somebody holding
 * three workspaces holds one payment relationship.
 */
export function BillingSettings() {
  const controller = useBillingSettingsController();
  const summary = controller.summary;

  return (
    <>
      <Panel
        title="Plan"
        description="What this account is on, and what it has left to spend"
        action={summary ? <StandingChip summary={summary} /> : null}
      >
        <div className="flex flex-col gap-4 p-4">
          {controller.isLoading ? (
            <div className="space-y-2" aria-hidden="true">
              <Skeleton className="h-4 w-56" />
              <Skeleton className="h-2 w-full" />
              <Skeleton className="h-4 w-72" />
            </div>
          ) : !summary ? (
            <p className="text-sm text-destructive" role="alert">
              {controller.loadError?.message ?? "The plan for this account could not be read."}
            </p>
          ) : (
            <>
              {!summary.plan ? (
                <p className="text-sm text-muted-foreground">
                  This account is not on a plan, so nothing can be started on it. Choose one to put
                  it back on a subscription.
                </p>
              ) : !summary.plan.allowance ? (
                <p className="text-sm text-muted-foreground">
                  This period is being renewed. The next allowance opens when the provider confirms
                  it.
                </p>
              ) : (
                <AllowanceMeter allowance={summary.plan.allowance} currency={summary.currency} />
              )}
              {summary.plan ? (
                <ConcurrencyLine
                  running={summary.live_container_count}
                  limit={summary.max_concurrent_containers}
                />
              ) : null}
              <RetainedTermsLine summary={summary} />
              {controller.settling ? (
                <p className="text-sm text-warning">
                  A change of plan is being settled with the payment provider. It finishes on its
                  own; the plan above is what this account is on until it does.
                </p>
              ) : null}
              <p className="text-sm text-muted-foreground">
                {summary.payment_method_on_file
                  ? "Usage beyond the included amount is invoiced monthly and charged to the card on file."
                  : "Without a card, this account gets a small amount of compute and its work is stopped once that is spent. Adding one raises the included amount immediately and lets usage beyond it be invoiced instead of stopped. Cards are held by our payment provider and never reach LazyCloud."}
              </p>
              <div className="flex flex-wrap gap-2">
                <Button
                  size="sm"
                  // Not disabled while a change is settling. A declined upgrade
                  // holds an intent for hours, and locking the button would trap
                  // the customer on a plan they are trying to leave — the server
                  // refuses a second change with a conflict, which is a sentence
                  // rather than a dead control.
                  disabled={controller.busy}
                  onClick={controller.openPlan}
                >
                  <Sparkles className="size-4" />
                  Manage subscription
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={controller.busy}
                  onClick={controller.startCard}
                >
                  {controller.leaving === "card" ? (
                    <LoaderCircle className="size-4 animate-spin" />
                  ) : (
                    <CreditCard className="size-4" />
                  )}
                  {/* The two states differ in what the button is for, not only in
                      wording: with no card it is the thing that lifts the cap and
                      stops work being killed, and with one saved it is a second
                      card the hosted page will make the default. */}
                  {summary.payment_method_on_file ? "Change payment method" : "Add payment method"}
                </Button>
                {summary.portal_available ? (
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={controller.busy}
                    onClick={controller.openPortal}
                  >
                    {controller.leaving === "portal" ? (
                      <LoaderCircle className="size-4 animate-spin" />
                    ) : (
                      <ExternalLink className="size-4" />
                    )}
                    Manage billing
                  </Button>
                ) : null}
              </div>
              {!summary.payment_method_on_file ? (
                <p className="text-xs text-muted-foreground">
                  A card saved just now can take a few seconds to appear here.
                </p>
              ) : null}
            </>
          )}
        </div>
      </Panel>
      <PlanDialog controller={controller} />
    </>
  );
}

/**
 * How much this account has running against how much it may.
 *
 * Stated as a pair rather than as a ceiling alone, because the ceiling belongs
 * to the account and the containers filling it may be in a workspace nobody is
 * looking at — a limit with no position reads as arbitrary the moment somebody
 * is refused.
 */
function ConcurrencyLine({ running, limit }: { running: number; limit: number }) {
  const atLimit = running >= limit;
  return (
    <p className={cn("text-sm", atLimit ? "text-warning" : "text-muted-foreground")}>
      {running} of {limit} containers running or queued across this account
      {atLimit ? " — nothing new will start until one finishes" : null}
    </p>
  );
}

/**
 * Said only where the period is richer than the plan now on the row.
 *
 * Which is what a move onto cheaper terms leaves behind: the allowance is
 * stamped when the cycle opens and is never reduced inside it, so the customer
 * keeps what they bought and the smaller plan starts at the next cycle. False
 * for an account with no card, whose stamped figure is below every plan's.
 */
function RetainedTermsLine({ summary }: { summary: BillingSummary }) {
  const plan = summary.plan;
  const allowance = plan?.allowance;
  if (!plan || !allowance) return null;
  const published = publishedPlans[plan.id];
  if (allowance.allowance_nanos <= published.includedNanos) return null;
  return (
    <p className="text-sm text-muted-foreground">
      This period keeps the allowance it opened with. The {published.name} plan&apos;s{" "}
      {exactDollars(published.includedNanos)} applies when it ends,{" "}
      <time dateTime={allowance.period_ended_at}>{relativeTime(allowance.period_ended_at)}</time>.
    </p>
  );
}

function StandingChip({ summary }: { summary: BillingSummary }) {
  if (summary.status === "past_due") {
    return <StatusChip status="past due" />;
  }
  if (!summary.plan) {
    return <StatusChip status="No plan" />;
  }
  return <StatusChip status={summary.plan.name} />;
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
      {/* Usage against this period's terms, and worded as nothing more. The
          figures are ours and exact — they read the priced ledger — but what is
          collected is the payment provider's answer, and it carries things this
          platform never models: a balance carried from a period that fell under
          their minimum charge, a proration, tax. Small and bounded, which is why
          the numbers stay; promising them as the invoice total is what has to
          go, because that promise is wrong for exactly the accounts that drift a
          little past the line. The invoice itself is one button away. */}
      <p className="text-xs text-muted-foreground">
        {overspent
          ? `${formatCostNanos(-allowance.remaining_nanos, currency)} of usage beyond the included amount this period. Your invoice is the final amount.`
          : `${formatCostNanos(allowance.remaining_nanos, currency)} of included usage left in this period.`}
      </p>
    </div>
  );
}
