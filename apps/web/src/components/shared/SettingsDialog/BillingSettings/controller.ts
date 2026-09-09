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

type PlanOfferAction = "current" | "card" | "switch" | "downgrade" | "unverified";

export type PlanOffer = PublishedPlan & { action: PlanOfferAction };

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
  // Returning from card setup must not authorize a subscription charge.
  if (published.monthly_nanos > 0 && !summary.payment_method_on_file) return "card";
  return current?.monthly_nanos != null && published.monthly_nanos < current.monthly_nanos
    ? "downgrade"
    : "switch";
}

function heldTermsUnverified(summary: BillingSummary | undefined): boolean {
  const held = summary?.plan;
  return Boolean(
    held &&
    (held.terms_version === null || held.monthly_nanos === null || held.included_nanos === null),
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
