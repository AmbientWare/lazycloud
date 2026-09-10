import { LoaderCircle } from "lucide-react";

import { LiveRelativeTime } from "@/components/shared/LiveTime";
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
      <div className="space-y-4">
        <div className="flex flex-col gap-2">
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
                        disabled={controller.busy || !summary.payment_method_on_file}
                        onClick={controller.openPlan}
                      >
                        Change plan
                      </Button>
                      <Button
                        variant={summary.payment_method_on_file ? "outline" : "default"}
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
              {!complimentary && !summary.payment_method_on_file ? (
                <p className="text-xs text-muted-foreground">
                  Add a payment method to buy credit, enable automatic reload, or change plans.
                </p>
              ) : null}
              {controller.settling ? (
                <p className="text-sm text-warning">
                  Your plan change is processing. The current plan stays active until it finishes.
                </p>
              ) : null}
            </>
          )}
        </div>
        {summary && !complimentary ? (
          <>
            <PrepaidCredit paymentMethodOnFile={summary.payment_method_on_file} />
            <BillingPreferences paymentMethodOnFile={summary.payment_method_on_file} />
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
        Your subscription terms are being verified. Plan changes are paused until verification
        finishes.
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
