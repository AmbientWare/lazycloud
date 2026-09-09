import { CreditCard, ExternalLink, LoaderCircle, Sparkles } from "lucide-react";

import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { Panel } from "@/components/shared/Panel";
import { StatusChip } from "@/components/shared/StatusChip";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { BillingSummary } from "@/lib/api/schemas";
import { usagePhrase } from "@/lib/entitlements";
import { exactDollars } from "@/lib/money";
import { cn } from "@/lib/utils";

import { useBillingSettingsController } from "./controller";
import { PlanDialog } from "./PlanDialog";
import { PrepaidCredit } from "./PrepaidCredit";
import { UsageBudget } from "./UsageBudget";
import { AutomaticReload } from "./AutomaticReload";

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
              ) : summary.plan.included_nanos === 0 ? (
                <p className="text-sm text-muted-foreground">This plan has no recurring credit.</p>
              ) : null}
              {summary.plan || complimentary ? (
                <div className="flex flex-col gap-1">
                  <ConcurrencyLine
                    running={summary.usage.concurrent_cpu_containers}
                    limit={summary.entitlements?.max_concurrent_cpu_containers ?? 0}
                    noun="CPU container"
                    state="running or queued"
                    atLimitNote="Stop a container or raise your plan limit before starting another."
                  />
                  <ConcurrencyLine
                    running={summary.usage.concurrent_gpus}
                    limit={summary.entitlements?.max_concurrent_gpus ?? 0}
                    noun="GPU card"
                    state="in use"
                    atLimitNote="Release GPU capacity or raise your plan limit before starting more."
                  />
                </div>
              ) : null}
              <EntitlementUsage summary={summary} />
              {complimentary ? null : <SubscriptionTerms summary={summary} />}
              {controller.settling ? (
                <p className="text-sm text-warning">
                  Your plan change is processing. The current plan stays active until it finishes.
                </p>
              ) : null}
              {complimentary ? null : (
                <p className="text-sm text-muted-foreground">
                  Usage consumes your prepaid credit. Add credit before your balance runs out to
                  keep workloads running.
                </p>
              )}
              <div className="flex flex-wrap gap-2">
                {complimentary ? null : (
                  <>
                    <Button size="sm" disabled={controller.busy} onClick={controller.openPlan}>
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
      {summary && !complimentary ? <PrepaidCredit /> : null}
      {summary && !complimentary ? <AutomaticReload /> : null}
      {summary && !complimentary ? <UsageBudget /> : null}
      <PlanDialog controller={controller} />
    </>
  );
}

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

function SubscriptionTerms({ summary }: { summary: BillingSummary }) {
  const plan = summary.plan;
  if (!plan) return null;
  if (plan.terms_version === null || plan.monthly_nanos === null || plan.included_nanos === null) {
    return (
      <p className="text-sm text-warning">
        Your subscription terms are being verified. Plan changes are paused until verification
        finishes.
      </p>
    );
  }
  return (
    <div className="space-y-1 text-sm text-muted-foreground">
      <p>
        Your {plan.name} subscription costs {exactDollars(plan.monthly_nanos)} per month
        {plan.included_nanos > 0
          ? ` and includes ${exactDollars(plan.included_nanos)} of usage credit`
          : ""}
        .
      </p>
      {plan.scheduled_change_at ? (
        <p>
          A plan change is scheduled for <LiveRelativeTime value={plan.scheduled_change_at} />. Your
          current benefits remain active until then.
        </p>
      ) : null}
    </div>
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
