import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import type {
  BillingTermsVersion,
  BillingSummary,
  PricingCatalog,
  PublishedPlan,
} from "@/lib/api/schemas";
import {
  billingSummaryQueryOptions,
  changeBillingPlan,
  openBillingPortal,
  startCardSetup,
} from "@/lib/queries/billing";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";
import { pricingCatalogQueryOptions } from "@/lib/queries/pricing";

/**
 * What pressing the button beside one plan does.
 *
 * `card` is the whole point of the union. A monthly plan on an account nobody
 * can charge is not a change that fails — it is a card that has to exist first,
 * and the two are different actions rather than one action with a retry.
 */
type PlanOfferAction = "current" | "card" | "switch" | "downgrade" | "unverified";

/** One published plan as it is offered to this account. */
export type PlanOffer = PublishedPlan & { action: PlanOfferAction };

/**
 * Every plan the platform publishes, cheapest first, with what it offers here.
 *
 * The figures and words come from the pricing endpoint, so a plan added there is
 * offered here without this file being edited. The account's current plan comes
 * from its billing summary.
 */
function planOffers(
  summary: BillingSummary | undefined,
  catalog: PricingCatalog | undefined,
): readonly PlanOffer[] {
  if (!catalog) return [];
  return catalog.plans.map((published) => ({
    ...published,
    action: offerAction(published, summary),
  }));
}

function offerAction(
  published: PublishedPlan,
  summary: BillingSummary | undefined,
): PlanOfferAction {
  if (!summary || heldTermsUnverified(summary)) return "unverified";
  const current = summary.plan;
  if (published.id === current?.id && published.terms_version === current.terms_version) {
    return "current";
  }
  // A monthly plan with nobody to charge is a card first and a plan change
  // afterwards, and the two stay separate presses. The server refuses this
  // combination outright, so starting the change here would spend the account's
  // one open-change slot on a request that was never going to work — and
  // returning from the hosted card page must never be what authorises a monthly
  // charge nobody pressed a second time for.
  if (published.monthly_nanos > 0 && !summary.payment_method_on_file) return "card";
  return current?.monthly_nanos != null && published.monthly_nanos < current.monthly_nanos
    ? "downgrade"
    : "switch";
}

function heldTermsUnverified(summary: BillingSummary | undefined): boolean {
  const held = summary?.plan;
  return Boolean(
    held &&
    (held.terms_version === null ||
      held.monthly_nanos === null ||
      held.included_nanos === null ||
      held.credit_scope === null),
  );
}

export type BillingSettingsController = {
  summary: BillingSummary | undefined;
  isLoading: boolean;
  loadError: Error | null;
  offers: readonly PlanOffer[];
  settling: boolean;
  termsUnverified: boolean;
  cancelScheduledChange: () => void;
  /** An administrator has waived this account's bill; nothing here is for sale to it. */
  complimentary: boolean;
  planOpen: boolean;
  openPlan: () => void;
  closePlan: () => void;
  choose: (offer: PlanOffer) => void;
  changingTo: BillingTermsVersion | null;
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
export function useBillingSettingsController({
  planOpen,
  onPlanOpenChange,
}: {
  planOpen: boolean;
  onPlanOpenChange: (open: boolean) => void;
}): BillingSettingsController {
  const queryClient = useQueryClient();
  const query = useQuery(billingSummaryQueryOptions());
  const catalog = useQuery(pricingCatalogQueryOptions());
  const [confirmingChangeTo, setConfirmingChangeTo] = useState<PlanOffer | null>(null);
  const [leaving, setLeaving] = useState<"card" | "portal" | null>(null);

  const change = useMutation({
    mutationFn: changeBillingPlan,
    onSuccess: (summary) => {
      // The answer is the standing this account now has, so it is written
      // straight into the cache the section reads rather than refetched.
      queryClient.setQueryData(accountQueryKeys.billing(), summary);
      setConfirmingChangeTo(null);
      onPlanOpenChange(false);
    },
  });

  const settling = query.data?.plan_change_pending ?? false;
  const complimentary = Boolean(query.data?.complimentary_since);
  const termsUnverified = heldTermsUnverified(query.data);
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
    if (
      busy ||
      settling ||
      complimentary ||
      offer.action === "current" ||
      offer.action === "unverified"
    )
      return;
    if (offer.action === "card") {
      go("card");
      return;
    }
    setConfirmingChangeTo(offer);
  };

  return {
    summary: query.data,
    isLoading: query.isPending || catalog.isPending,
    loadError: query.error ?? catalog.error,
    offers: planOffers(query.data, catalog.data),
    settling,
    termsUnverified,
    cancelScheduledChange: () => {
      const held = query.data?.plan;
      if (
        busy ||
        settling ||
        complimentary ||
        termsUnverified ||
        !held?.terms_version ||
        !held.scheduled_terms_version
      )
        return;
      change.mutate({ plan: held.id, terms_version: held.terms_version });
    },
    complimentary,
    planOpen,
    openPlan: () => onPlanOpenChange(true),
    closePlan: () => {
      if (change.isPending) return;
      onPlanOpenChange(false);
      setConfirmingChangeTo(null);
      change.reset();
    },
    choose,
    changingTo: change.isPending ? change.variables.terms_version : null,
    changeError: change.error,
    confirmingChangeTo,
    confirmChange: () => {
      if (busy || settling || complimentary || termsUnverified || !confirmingChangeTo) return;
      change.mutate({
        plan: confirmingChangeTo.id,
        terms_version: confirmingChangeTo.terms_version,
      });
    },
    dismissChange: () => {
      if (change.isPending) return;
      setConfirmingChangeTo(null);
    },
    startCard: () => {
      if (complimentary) return;
      go("card");
    },
    openPortal: () => go("portal"),
    leaving,
    busy,
  };
}
