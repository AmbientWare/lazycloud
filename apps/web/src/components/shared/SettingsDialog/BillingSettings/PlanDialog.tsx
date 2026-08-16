import { Check, CreditCard, LoaderCircle } from "lucide-react";

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
import { relativeTime } from "@/lib/format";
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
            Included compute is issued each period. Usage beyond it is metered at the same rates on
            every plan and billed to the card on file.
          </DialogDescription>
        </DialogHeader>

        <div className="min-h-0 flex-1 overflow-y-auto p-5">
          {controller.settling ? (
            <p className="mb-4 border-l-2 border-warning bg-warning/5 px-3 py-2 text-xs">
              A change of plan for this account is still being settled with the payment provider.
              Nothing else can be changed until it finishes.
            </p>
          ) : null}
          <div className="grid gap-3 sm:grid-cols-2">
            {controller.offers.map((offer) => (
              <PlanCard key={offer.id} offer={offer} controller={controller} />
            ))}
          </div>
          {controller.changeError ? (
            <p
              className="mt-4 border border-destructive/40 bg-destructive/5 p-3 text-xs text-destructive"
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
        "flex flex-col border p-4",
        current ? "border-brand bg-brand/[0.04]" : "border-border bg-card",
      )}
    >
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="text-sm font-medium">{offer.name}</h3>
        <p className="font-mono text-sm">
          {exactDollars(offer.monthlyNanos)}
          <span className="text-xs text-muted-foreground"> / month</span>
        </p>
      </div>
      <p className="mt-1 text-xs leading-5 text-muted-foreground">{offer.summary}</p>
      <ul className="mt-3 space-y-1.5 text-xs leading-5">
        <PlanPoint>{exactDollars(offer.includedNanos)} of compute included each period</PlanPoint>
        <PlanPoint>{offer.maxConcurrentContainers} containers running at once</PlanPoint>
        {offer.terms.map((term) => (
          <PlanPoint key={term}>{term}</PlanPoint>
        ))}
      </ul>
      <div className="mt-4 pt-1">
        {current ? (
          <p className="text-xs font-medium text-brand">Current plan</p>
        ) : (
          <Button
            size="sm"
            variant={offer.action === "cancel" ? "outline" : "default"}
            className={cn("w-full", offer.action === "cancel" && "text-destructive")}
            disabled={controller.busy || controller.settling}
            onClick={() => controller.choose(offer)}
          >
            {pending || (offer.action === "card" && controller.leaving === "card") ? (
              <LoaderCircle className="size-4 animate-spin" />
            ) : offer.action === "card" ? (
              <CreditCard className="size-4" />
            ) : null}
            {offer.action === "card"
              ? `Add a card to switch to ${offer.name}`
              : offer.action === "cancel"
                ? `Cancel subscription and move to ${offer.name}`
                : `Switch to ${offer.name}`}
          </Button>
        )}
      </div>
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
              ? "Your subscription is not ended and your work is not stopped. The monthly price changes."
              : `Switching now charges the rest of this month at the ${offer.name} price, to the card on file.`}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <ul className="space-y-2 text-xs leading-5 text-muted-foreground">
          {/* The period's own figures, never the plan card's. A cardless account
              is on the terms the platform gives away rather than the ones the
              plan publishes, and this is the screen where telling it otherwise
              would be a promise made while asking somebody to commit. */}
          {allowance ? (
            <li>
              You keep the {formatCostNanos(allowance.allowance_nanos, summary?.currency)} included
              for the rest of this period, which ends{" "}
              <time dateTime={allowance.period_ended_at}>
                {relativeTime(allowance.period_ended_at)}
              </time>
              .
            </li>
          ) : null}
          {movingDown ? (
            <>
              <li>
                You will not be charged the monthly subscription again. This period&apos;s
                subscription is not refunded.
              </li>
              <li>
                Moving back to {summary?.plan?.name} before this period ends costs nothing more —
                these days are already paid for.
              </li>
            </>
          ) : (
            <li>Each following month is charged in full at the {offer.name} price.</li>
          )}
          {summary ? (
            <li>
              New containers are limited to {summary.max_concurrent_containers} at once until this
              period ends.
            </li>
          ) : null}
          <li>Your work keeps running and keeps being invoiced.</li>
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
