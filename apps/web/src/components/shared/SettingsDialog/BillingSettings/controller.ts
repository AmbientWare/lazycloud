import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import type { BillingSummary } from "@/lib/api/schemas";
import {
  billingSummaryQueryOptions,
  changeBillingPlan,
  openBillingPortal,
  startCardSetup,
} from "@/lib/queries/billing";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";
import {
  planIds,
  publishedPlans,
  type PlanId,
  type PublishedPlan,
} from "@/routes/-marketing/pricingCatalog";

/**
 * What pressing the button beside one plan does.
 *
 * `card` is the whole point of the union. A monthly plan on an account nobody
 * can charge is not a change that fails — it is a card that has to exist first,
 * and the two are different actions rather than one action with a retry.
 */
type PlanOfferAction = "current" | "card" | "switch" | "cancel";

/** One published plan as it is offered to this account. */
export type PlanOffer = PublishedPlan & { id: PlanId; action: PlanOfferAction };

/**
 * Every plan the platform publishes, cheapest first, with what it offers here.
 *
 * The figures and the words are the generated card's — the same source the
 * pricing page compiles in — so a plan added there is offered here without this
 * file being edited, and no price appears twice. Which plan the account is
 * actually on comes from the server, never from the card.
 */
function planOffers(summary: BillingSummary | undefined): readonly PlanOffer[] {
  const currentId = summary?.plan?.id;
  const currentMonthlyNanos = currentId ? publishedPlans[currentId].monthlyNanos : 0;
  const cardOnFile = summary?.payment_method_on_file ?? false;
  return planIds.map((id) => {
    const published = publishedPlans[id];
    return {
      ...published,
      id,
      action: offerAction(published, { id, currentId, currentMonthlyNanos, cardOnFile }),
    };
  });
}

function offerAction(
  published: PublishedPlan,
  {
    id,
    currentId,
    currentMonthlyNanos,
    cardOnFile,
  }: {
    id: PlanId;
    currentId: PlanId | undefined;
    currentMonthlyNanos: number;
    cardOnFile: boolean;
  },
): PlanOfferAction {
  if (id === currentId) return "current";
  // A monthly plan with nobody to charge is a card first and a plan change
  // afterwards, and the two stay separate presses. The server refuses this
  // combination outright, so starting the change here would spend the account's
  // one open-change slot on a request that was never going to work — and
  // returning from the hosted card page must never be what authorises a monthly
  // charge nobody pressed a second time for.
  if (published.monthlyNanos > 0 && !cardOnFile) return "card";
  // Read off the published prices rather than off which plan is which: moving
  // down takes nothing back and stops the next charge, which is a different
  // promise from moving up and is confirmed before it happens.
  return published.monthlyNanos < currentMonthlyNanos ? "cancel" : "switch";
}

export type BillingSettingsController = {
  summary: BillingSummary | undefined;
  isLoading: boolean;
  loadError: Error | null;
  offers: readonly PlanOffer[];
  settling: boolean;
  planOpen: boolean;
  openPlan: () => void;
  closePlan: () => void;
  choose: (offer: PlanOffer) => void;
  changingTo: PlanId | null;
  changeError: Error | null;
  confirmingChangeTo: PlanOffer | null;
  confirmChange: () => void;
  dismissChange: () => void;
  startCard: () => void;
  openPortal: () => void;
  leaving: "card" | "portal" | null;
  busy: boolean;
};

/**
 * The account's plan, and every way out of it.
 *
 * Fetches its own summary rather than taking one as a prop, like every other
 * settings section: the payment relationship belongs to the signed-in person,
 * not to the workspace in the address bar, so there is no workspace to plumb.
 */
export function useBillingSettingsController(): BillingSettingsController {
  const queryClient = useQueryClient();
  const query = useQuery(billingSummaryQueryOptions());
  const [planOpen, setPlanOpen] = useState(false);
  const [confirmingChangeTo, setConfirmingChangeTo] = useState<PlanOffer | null>(null);
  const [leaving, setLeaving] = useState<"card" | "portal" | null>(null);

  const change = useMutation({
    mutationFn: changeBillingPlan,
    onSuccess: (summary) => {
      // The answer is the standing this account now has, so it is written
      // straight into the cache the section reads rather than refetched.
      queryClient.setQueryData(accountQueryKeys.billing(), summary);
      setConfirmingChangeTo(null);
      setPlanOpen(false);
    },
  });

  const settling = query.data?.plan_change_pending ?? false;
  const busy = change.isPending || leaving !== null;

  const go = (which: "card" | "portal") => {
    if (busy) return;
    setLeaving(which);
    void (which === "card" ? startCardSetup() : openBillingPortal()).catch((error: unknown) => {
      // The redirect never happened, so this page is still here to say so. Left
      // silent, the button a cardless account has to press to do anything at all
      // spins and then looks untouched.
      setLeaving(null);
      toast.error(
        which === "card" ? "Could not open the payment page" : "Could not open billing management",
        { description: error instanceof Error ? error.message : undefined },
      );
    });
  };

  const choose = (offer: PlanOffer) => {
    if (busy || settling || offer.action === "current") return;
    if (offer.action === "card") {
      go("card");
      return;
    }
    // Both directions confirm. The move down was already behind one because it
    // gives something up; the move up takes an immediate prorated charge on the
    // card, which is the one a person is entitled to be asked about twice.
    setConfirmingChangeTo(offer);
  };

  return {
    summary: query.data,
    isLoading: query.isPending,
    loadError: query.error,
    offers: planOffers(query.data),
    settling,
    planOpen,
    openPlan: () => setPlanOpen(true),
    closePlan: () => {
      if (change.isPending) return;
      setPlanOpen(false);
      setConfirmingChangeTo(null);
      change.reset();
    },
    choose,
    changingTo: change.isPending ? change.variables : null,
    changeError: change.error,
    confirmingChangeTo,
    confirmChange: () => {
      if (busy || settling || !confirmingChangeTo) return;
      change.mutate(confirmingChangeTo.id);
    },
    dismissChange: () => {
      if (change.isPending) return;
      setConfirmingChangeTo(null);
    },
    startCard: () => go("card"),
    openPortal: () => go("portal"),
    leaving,
    busy,
  };
}
