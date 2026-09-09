import { Check, CreditCard, LoaderCircle } from "lucide-react";

import { LiveRelativeTime } from "@/components/shared/LiveTime";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { BillingSummary } from "@/lib/api/schemas";
import { gpuModelsPhrase, limitPhrase, memberLimitPhrase } from "@/lib/entitlements";
import { countLabel } from "@/lib/format";
import { exactDollars } from "@/lib/money";
import { cn } from "@/lib/utils";

import type { BillingSettingsController, PlanOffer } from "./controller";

// Keep confirmation out of the URL so a checkout return cannot authorize a change.
export function PlanDialog({ controller }: { controller: BillingSettingsController }) {
  return (
    <Dialog
      open={controller.planOpen}
      onOpenChange={(next) => (next ? undefined : controller.closePlan())}
    >
      {controller.planOpen ? <PlanDialogBody controller={controller} /> : null}
    </Dialog>
  );
}

function PlanDialogBody({ controller }: { controller: BillingSettingsController }) {
  const { summary, confirmingChangeTo } = controller;

  return (
    <>
      <DialogContent className="flex max-h-[min(46rem,calc(100svh-2rem))] max-w-3xl flex-col gap-0 overflow-hidden p-0 sm:max-w-3xl">
        <DialogHeader className="shrink-0 border-b border-border bg-muted/20 px-5 py-4 pr-12 text-left">
          <DialogTitle className="text-base">Subscription</DialogTitle>
          <DialogDescription>
            Paid plans include monthly usage credit. Usage rates are the same across plans.
          </DialogDescription>
        </DialogHeader>

        <div className="min-h-0 flex-1 overflow-y-auto p-5">
          {controller.complimentary ? (
            <p className="mb-4 rounded-sm border-l-2 border-border bg-muted/30 px-3 py-2 text-xs">
              Usage on this account is tracked but not billed, so there is no plan to change.
            </p>
          ) : controller.settling ? (
            <p className="mb-4 rounded-sm border-l-2 border-warning bg-warning/5 px-3 py-2 text-xs">
              Your plan change is processing. You can make another change when it finishes.
            </p>
          ) : controller.termsUnverified ? (
            <p className="mb-4 rounded-sm border-l-2 border-warning bg-warning/5 px-3 py-2 text-xs">
              Your current subscription terms are being verified. Plan changes will be available
              when verification finishes.
            </p>
          ) : null}
          {summary?.plan?.scheduled_change_at ? (
            <div className="mb-4 space-y-2 rounded-sm border border-border p-3 text-xs">
              <p>
                Your scheduled plan change takes effect{" "}
                <LiveRelativeTime value={summary.plan.scheduled_change_at} />. Your current price
                and benefits remain active until then.
              </p>
              <Button
                size="sm"
                variant="outline"
                disabled={
                  controller.busy ||
                  controller.settling ||
                  controller.termsUnverified ||
                  controller.complimentary
                }
                onClick={controller.cancelScheduledChange}
              >
                Keep current plan
              </Button>
            </div>
          ) : null}
          <div className="grid items-stretch gap-4 sm:grid-cols-3">
            {controller.offers.map((offer) => (
              <PlanCard key={offer.terms_version} offer={offer} controller={controller} />
            ))}
          </div>
          {controller.changeError ? (
            <p
              className="mt-4 rounded-sm border border-destructive/40 bg-destructive/5 p-3 text-xs text-destructive"
              role="alert"
            >
              {controller.changeError.message}
            </p>
          ) : null}
        </div>
      </DialogContent>

      {confirmingChangeTo ? (
        <ChangeConfirmation
          offer={confirmingChangeTo}
          summary={summary}
          pending={controller.changingTo !== null}
          onConfirm={controller.confirmChange}
          onOpenChange={(next) => (next ? undefined : controller.dismissChange())}
        />
      ) : null}
    </>
  );
}

function PlanCard({
  offer,
  controller,
}: {
  offer: PlanOffer;
  controller: BillingSettingsController;
}) {
  const current = offer.action === "current";
  const pending = controller.changingTo === offer.terms_version;

  return (
    <section
      className={cn(
        "flex h-full flex-col rounded-md border bg-card p-5",
        current ? "border-brand/70 bg-brand/[0.05]" : "border-border",
      )}
    >
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-base font-semibold">{offer.name}</h3>
        {current ? <span className="text-xs font-medium text-brand">Current</span> : null}
      </div>
      <p className="mt-3 font-mono text-2xl font-semibold tracking-tight">
        {exactDollars(offer.monthly_nanos)}
        <span className="ml-1 font-sans text-xs font-normal text-muted-foreground">/ month</span>
      </p>
      <p className="mt-2 text-xs leading-5 text-muted-foreground">{offer.summary}</p>
      <ul className="mt-4 flex-1 space-y-2 border-t border-border/80 pt-4 text-xs leading-5">
        <PlanPoint>
          {offer.included_nanos > 0
            ? `${exactDollars(offer.included_nanos)} usage credit each month`
            : "Pay for usage with prepaid credit"}
        </PlanPoint>
        <PlanPoint>
          {countLabel(offer.entitlements.max_concurrent_cpu_containers, "CPU container")} running at
          once
        </PlanPoint>
        <PlanPoint>
          {countLabel(offer.entitlements.max_concurrent_gpus, "GPU card")} held at once
        </PlanPoint>
        <PlanPoint>{gpuModelsPhrase(offer.entitlements.gpu_types)}</PlanPoint>
        <PlanPoint>{limitPhrase(offer.entitlements.max_workspaces, "workspace")}</PlanPoint>
        <PlanPoint>{memberLimitPhrase(offer.entitlements.max_members)}</PlanPoint>
        {offer.entitlements.connected_cloud ? (
          <PlanPoint>Connected cloud accounts</PlanPoint>
        ) : null}
        {offer.entitlements.custom_domains ? <PlanPoint>Custom domains</PlanPoint> : null}
        {offer.entitlements.self_hosted ? <PlanPoint>Self-hosted compute</PlanPoint> : null}
        <PlanPoint>{offer.entitlements.log_retention_days}-day log retention</PlanPoint>
        {offer.terms.map((term) => (
          <PlanPoint key={term}>{term}</PlanPoint>
        ))}
      </ul>
      {!current ? (
        <div className="mt-5 border-t border-border/80 pt-4">
          <Button
            size="sm"
            variant={offer.action === "downgrade" ? "outline" : "default"}
            className="w-full"
            disabled={
              controller.busy ||
              controller.settling ||
              controller.complimentary ||
              offer.action === "unverified"
            }
            onClick={() => controller.choose(offer)}
          >
            {pending || (offer.action === "card" && controller.leaving === "card") ? (
              <LoaderCircle className="size-4 animate-spin" />
            ) : offer.action === "card" ? (
              <CreditCard className="size-4" />
            ) : null}
            {offer.action === "card"
              ? `Add payment method for ${offer.name}`
              : offer.action === "downgrade"
                ? `Move to ${offer.name}`
                : `Switch to ${offer.name}`}
          </Button>
        </div>
      ) : null}
    </section>
  );
}

function PlanPoint({ children }: { children: React.ReactNode }) {
  return (
    <li className="flex gap-2">
      <Check className="mt-0.5 size-3.5 shrink-0 text-positive" aria-hidden="true" />
      <span className="min-w-0">{children}</span>
    </li>
  );
}

function ChangeConfirmation({
  offer,
  summary,
  pending,
  onConfirm,
  onOpenChange,
}: {
  offer: PlanOffer;
  summary: BillingSummary | undefined;
  pending: boolean;
  onConfirm: () => void;
  onOpenChange: (open: boolean) => void;
}) {
  const movingDown = offer.action === "downgrade";

  return (
    <AlertDialog open onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Move to the {offer.name} plan?</AlertDialogTitle>
          <AlertDialogDescription>
            {movingDown
              ? "The change takes effect at your next renewal. Your current benefits stay active until then."
              : offer.monthly_nanos > 0
                ? "Your payment method will be charged the prorated difference for the rest of this billing period."
                : "Your subscription will switch to these terms without a monthly charge."}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <ul className="space-y-2 text-xs leading-5 text-muted-foreground">
          {movingDown ? (
            <>
              <li>
                Your next monthly price will be {exactDollars(offer.monthly_nanos)}. This period
                isn&apos;t refunded.
              </li>
              <li>You can cancel the scheduled change before renewal.</li>
              <li>Existing credits keep their original expiry dates.</li>
            </>
          ) : (
            <li>
              Future months cost {exactDollars(offer.monthly_nanos)} and include{" "}
              {exactDollars(offer.included_nanos)} of usage credit.
            </li>
          )}
          {summary ? (
            <li>
              Your limits change {movingDown ? "at renewal" : "after payment succeeds"} to{" "}
              {countLabel(offer.entitlements.max_concurrent_cpu_containers, "CPU container")} and{" "}
              {countLabel(offer.entitlements.max_concurrent_gpus, "GPU card")} at once.
            </li>
          ) : null}
          <li>Your workloads keep running and usage remains billable.</li>
        </ul>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>Keep my plan</AlertDialogCancel>
          <Button
            variant={movingDown ? "destructive" : "default"}
            disabled={pending}
            onClick={onConfirm}
          >
            {pending ? <LoaderCircle className="size-4 animate-spin" /> : null}
            Move to {offer.name}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
