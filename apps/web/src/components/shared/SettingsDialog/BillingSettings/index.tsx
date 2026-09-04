import { CreditCard, ExternalLink, LoaderCircle, Sparkles } from "lucide-react";

import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { Panel } from "@/components/shared/Panel";
import { StatusChip } from "@/components/shared/StatusChip";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { BillingPlan, BillingSummary } from "@/lib/api/schemas";
import { usagePhrase } from "@/lib/entitlements";
import { exactDollars, formatCostNanos } from "@/lib/money";
import { cn } from "@/lib/utils";

import { useBillingSettingsController, type PlanOffer } from "./controller";
import { PlanDialog } from "./PlanDialog";

/**
 * What this account is on, what it has left to spend, and how to change either.
 *
 * The account rather than the workspace, which is why it lives here and not
 * under `/w/<workspace>/`: the provider invoices a person, and somebody holding
 * three workspaces holds one payment relationship.
 */
export function BillingSettings({
  planOpen,
  onPlanOpenChange,
}: {
  planOpen: boolean;
  onPlanOpenChange: (open: boolean) => void;
}) {
  const controller = useBillingSettingsController({ planOpen, onPlanOpenChange });
  const summary = controller.summary;
  const complimentary = controller.complimentary;

  return (
    <>
      <Panel title="Plan" action={summary ? <StandingChip summary={summary} /> : null}>
        <div className="flex flex-col gap-4 p-4">
          {controller.isLoading ? (
            <div className="space-y-2" aria-hidden="true">
              <Skeleton className="h-4 w-56" />
              <Skeleton className="h-2 w-full" />
              <Skeleton className="h-4 w-72" />
            </div>
          ) : controller.loadError || !summary ? (
            <p className="text-sm text-destructive" role="alert">
              {controller.loadError?.message ?? "The plan for this account could not be read."}
            </p>
          ) : (
            <>
              {complimentary ? (
                <p className="text-sm text-muted-foreground">
                  Usage on this account is tracked but not billed.
                </p>
              ) : !summary.plan ? (
                <p className="text-sm text-muted-foreground">Choose a plan to start workloads.</p>
              ) : !summary.plan.allowance ? (
                <p className="text-sm text-muted-foreground">
                  Your next usage allowance will appear when renewal finishes.
                </p>
              ) : (
                <AllowanceMeter allowance={summary.plan.allowance} currency={summary.currency} />
              )}
              {summary.plan || complimentary ? (
                <div className="flex flex-col gap-1">
                  <ConcurrencyLine
                    running={summary.usage.concurrent_cpu_containers}
                    limit={summary.entitlements?.max_concurrent_cpu_containers ?? 0}
                    noun="CPU container"
                    state="running or queued"
                    atLimitNote="New containers will start when capacity is available."
                  />
                  <ConcurrencyLine
                    running={summary.usage.concurrent_gpus}
                    limit={summary.entitlements?.max_concurrent_gpus ?? 0}
                    noun="GPU card"
                    state="in use"
                    atLimitNote="New GPU containers will start when cards are free."
                  />
                </div>
              ) : null}
              <EntitlementUsage summary={summary} />
              {complimentary ? null : (
                <RetainedTermsLine summary={summary} offers={controller.offers} />
              )}
              {controller.settling ? (
                <p className="text-sm text-warning">
                  Your plan change is processing. The current plan stays active until it finishes.
                </p>
              ) : null}
              {complimentary ? null : (
                <p className="text-sm text-muted-foreground">
                  {summary.payment_method_on_file
                    ? "Usage beyond the included amount is invoiced monthly and charged to the card on file."
                    : "Add a payment method to increase included compute and bill overages instead of stopping workloads. Payment details are stored by our payment provider, not LazyCloud."}
                </p>
              )}
              <div className="flex flex-wrap gap-2">
                {complimentary ? null : (
                  <>
                    <Button
                      size="sm"
                      // Not disabled while a change is settling. A declined upgrade
                      // holds an intent for hours, and locking the button would trap
                      // the customer on a plan they are trying to leave. The server
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
                      {summary.payment_method_on_file
                        ? "Change payment method"
                        : "Add payment method"}
                    </Button>
                  </>
                )}
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
              {!summary.payment_method_on_file && !complimentary ? (
                <p className="text-xs text-muted-foreground">
                  New payment methods may take a few seconds to appear.
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
 * How much of one pool this account is holding against how much it may.
 *
 * One line per pool, because the two are bounded separately: a container counts
 * against the CPU ceiling or, if it asks for cards, against the GPU one by the
 * number of cards. A single combined figure would leave an account refused a GPU
 * looking at a container count with plenty of room in it.
 */
function ConcurrencyLine({
  running,
  limit,
  noun,
  state,
  atLimitNote,
}: {
  running: number;
  limit: number;
  noun: string;
  state: string;
  atLimitNote: string;
}) {
  const atLimit = running >= limit;
  return (
    <p className={cn("text-sm", atLimit ? "text-warning" : "text-muted-foreground")}>
      {usagePhrase(running, limit, noun)} {state}
      {atLimit ? `. ${atLimitNote}` : null}
    </p>
  );
}

/**
 * The two ceilings that are not compute: how many workspaces and how many people.
 *
 * Both are counted across the account rather than per workspace, which is the
 * scope a payer is billed in. Somebody holding three workspaces reads one
 * figure here rather than adding up their own.
 */
function EntitlementUsage({ summary }: { summary: BillingSummary }) {
  const entitlements = summary.entitlements;
  if (!entitlements) return null;
  return (
    <p className="text-sm text-muted-foreground">
      {usagePhrase(summary.usage.workspaces, entitlements.max_workspaces, "workspace")} ·{" "}
      {usagePhrase(summary.usage.members, entitlements.max_members, "member")}
    </p>
  );
}

/**
 * Said only where the period is richer than the plan now on the row.
 *
 * Which is what a move onto cheaper terms leaves behind: the allowance is
 * stamped when the cycle opens and is never reduced inside it, so the customer
 * keeps what they bought and the smaller plan starts at the next cycle. Absent
 * for an account with no card, whose stamped figure is below every plan's.
 */
function RetainedTermsLine({
  summary,
  offers,
}: {
  summary: BillingSummary;
  offers: readonly PlanOffer[];
}) {
  const plan = summary.plan;
  const allowance = plan?.allowance;
  if (!plan || !allowance) return null;
  const published = offers.find((offer) => offer.id === plan.id);
  if (!published || allowance.allowance_nanos <= published.included_nanos) return null;
  return (
    <p className="text-sm text-muted-foreground">
      This period keeps its current allowance. The {published.name} plan&apos;s{" "}
      {exactDollars(published.included_nanos)} allowance starts{" "}
      <LiveRelativeTime value={allowance.period_ended_at} />.
    </p>
  );
}

function StandingChip({ summary }: { summary: BillingSummary }) {
  if (summary.complimentary_since) {
    return <StatusChip status="Complimentary" />;
  }
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
          Ends <LiveRelativeTime value={allowance.period_ended_at} />
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
          ? `${formatCostNanos(-allowance.remaining_nanos, currency)} over the included amount this period. Your invoice shows the final total.`
          : `${formatCostNanos(allowance.remaining_nanos, currency)} of included usage remaining.`}
      </p>
    </div>
  );
}
