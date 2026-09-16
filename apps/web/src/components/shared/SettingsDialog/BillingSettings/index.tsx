import { LoaderCircle } from "lucide-react";

import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { Panel } from "@/components/shared/Panel";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { BillingSummary } from "@/lib/api/schemas";
import { formatCostNanos } from "@/lib/money";

import { useBillingSettingsController } from "./controller";
import { PlanDialog } from "./PlanDialog";
import { PrepaidCredit } from "./PrepaidCredit";
import { BillingPreferences } from "./BillingPreferences";

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
      <div className="grid shrink-0 gap-3 lg:min-h-0 lg:flex-1 lg:grid-rows-[auto_auto_minmax(0,1fr)]">
        <Panel title="Plan and payment" contentClassName="flex flex-col gap-3 p-3">
          {controller.isLoading ? (
            <div className="space-y-2" aria-hidden="true">
              <Skeleton className="h-4 w-56" />
              <Skeleton className="h-2 w-full" />
              <Skeleton className="h-4 w-72" />
            </div>
          ) : controller.loadError || !summary ? (
            <p className="text-sm text-destructive" role="alert">
              {controller.loadError?.message ?? "Could not load your plan."}
            </p>
          ) : (
            <>
              {complimentary ? (
                <p className="text-sm text-muted-foreground">
                  Usage on this account is tracked but not billed.
                </p>
              ) : !summary.plan ? (
                <p className="text-sm text-muted-foreground">Choose a plan to start workloads.</p>
              ) : null}
              <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
                {complimentary ? null : <SubscriptionTerms summary={summary} />}
                {summary.status === "past_due" ? (
                  <p className="text-sm text-warning">Payment past due</p>
                ) : null}
                <div className="flex flex-wrap gap-2">
                  {complimentary ? null : (
                    <>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={controller.busy || !summary.payment_method_on_file}
                        onClick={controller.openPlan}
                      >
                        Change plan
                      </Button>
                      <Button
                        variant={summary.payment_method_on_file ? "outline" : "default"}
                        size="sm"
                        disabled={controller.busy}
                        onClick={controller.startCard}
                      >
                        {controller.leaving === "card" ? (
                          <LoaderCircle className="size-4 animate-spin" />
                        ) : null}
                        {summary.payment_method_on_file
                          ? "Update payment method"
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
                      ) : null}
                      Invoices
                    </Button>
                  ) : null}
                </div>
              </div>
              {controller.settling ? (
                <p className="text-sm text-warning">
                  Your plan change is processing. The current plan stays active until it finishes.
                </p>
              ) : null}
            </>
          )}
        </Panel>
        {summary && !complimentary ? (
          <>
            <Panel title="Prepaid credit" contentClassName="p-3">
              <PrepaidCredit paymentMethodOnFile={summary.payment_method_on_file} />
            </Panel>
            <Panel title="Spending controls" contentClassName="p-3">
              <BillingPreferences paymentMethodOnFile={summary.payment_method_on_file} />
            </Panel>
          </>
        ) : null}
      </div>
      <PlanDialog controller={controller} />
    </>
  );
}

function SubscriptionTerms({ summary }: { summary: BillingSummary }) {
  const plan = summary.plan;
  if (!plan) return null;
  if (plan.terms_version === null || plan.monthly_nanos === null || plan.included_nanos === null) {
    return (
      <p className="text-sm text-warning">
        Verifying your subscription. Plan changes are unavailable until verification finishes.
      </p>
    );
  }
  return (
    <div className="space-y-1 text-sm">
      <p>
        <span className="font-medium">{plan.name}</span>{" "}
        <span className="text-muted-foreground">
          {formatCostNanos(plan.monthly_nanos)} / month
          {plan.included_nanos > 0 ? ` with ${formatCostNanos(plan.included_nanos)} credit` : ""}
        </span>
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
