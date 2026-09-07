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
import { exactDollars, formatCostNanos } from "@/lib/money";
import { cn } from "@/lib/utils";

import type { BillingSettingsController, PlanOffer } from "./controller";

/**
 * Every plan the platform publishes, and the one way onto each of them.
 *
 * Mounted only while open, which is what drops the confirmation and the last
 * refusal without anything having to clear them. Its open state is deliberately
 * not in the URL: a customer sent to the payment provider from inside here comes
 * back to the settings dialog with this closed, so returning from a hosted page
 * can never be what completes a plan change.
 */
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
            Each plan includes monthly compute. Additional usage is billed at the same rates.
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
          ) : null}
          <div className="grid items-stretch gap-4 sm:grid-cols-2">
            {controller.offers.map((offer) => (
              <PlanCard key={offer.id} offer={offer} controller={controller} />
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
  const pending = controller.changingTo === offer.id;

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
        <PlanPoint>{exactDollars(offer.included_nanos)} compute included each month</PlanPoint>
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
            variant={offer.action === "cancel" ? "outline" : "default"}
            className={cn("w-full", offer.action === "cancel" && "text-destructive")}
            disabled={controller.busy || controller.settling || controller.complimentary}
            onClick={() => controller.choose(offer)}
          >
            {pending || (offer.action === "card" && controller.leaving === "card") ? (
              <LoaderCircle className="size-4 animate-spin" />
            ) : offer.action === "card" ? (
              <CreditCard className="size-4" />
            ) : null}
            {offer.action === "card"
              ? `Add payment method for ${offer.name}`
              : offer.action === "cancel"
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

/**
 * What moving down actually does, in the figures this account holds.
 *
 * Every line is derived: the allowance and the date are the server's answer for
 * this period, and the terms taking over are the published card's. The
 * asymmetry is stated rather than smoothed over — the allowance is the one this
 * period opened with and keeps, while how much may run at once is read live and
 * drops straight away.
 */
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
  const allowance = summary?.plan?.allowance ?? null;
  const movingDown = offer.action === "cancel";

  return (
    <AlertDialog open onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Move to the {offer.name} plan?</AlertDialogTitle>
          <AlertDialogDescription>
            {movingDown
              ? "Your workloads keep running. Only the monthly price changes."
              : `The card on file will be charged at the ${offer.name} rate for the rest of this month.`}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <ul className="space-y-2 text-xs leading-5 text-muted-foreground">
          {/* The period's own figures, never the plan card's. A cardless account
              is on the terms the platform gives away rather than the ones the
              plan publishes, and this is the screen where telling it otherwise
              would be a promise made while asking somebody to commit. */}
          {allowance ? (
            <li>
              You keep {formatCostNanos(allowance.allowance_nanos, summary?.currency)} of included
              compute through <LiveRelativeTime value={allowance.period_ended_at} />.
            </li>
          ) : null}
          {movingDown ? (
            <>
              <li>
                You won&apos;t be charged another monthly fee. This period isn&apos;t refunded.
              </li>
              <li>
                Moving back to {summary?.plan?.name} before this period ends won&apos;t add another
                charge.
              </li>
            </>
          ) : (
            <li>Future months are billed at the full {offer.name} price.</li>
          )}
          {summary ? (
            <li>
              Your limits change immediately to{" "}
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
