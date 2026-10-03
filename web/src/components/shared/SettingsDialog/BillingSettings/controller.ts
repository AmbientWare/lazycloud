import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import type { Schemas } from "@/lib/api/client";
import {
  billingAccountQueryOptions,
  changeBillingPlan,
  openBillingPortal,
  startCardSetup,
} from "@/lib/queries/billing";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";
import { pricingCatalogQueryOptions } from "@/lib/queries/pricing";

type Account = Schemas["BillingAccount"];

type PlanOfferAction = "current" | "card" | "switch" | "downgrade";

export type PlanOffer = Schemas["PublishedPlan"] & { action: PlanOfferAction };

function planOffers(
  account: Account | undefined,
  catalog: Schemas["PricingCatalog"] | undefined,
): readonly PlanOffer[] {
  if (!account || !catalog) return [];
  return catalog.plans.map((published) => ({
    ...published,
    action: offerAction(published, account),
  }));
}

function offerAction(published: Schemas["PublishedPlan"], account: Account): PlanOfferAction {
  const current = account.plan;
  if (published.id === current.id && published.terms_version === current.terms_version) {
    return "current";
  }
  // Returning from card setup must not authorize a subscription charge.
  if (published.monthly_nanos > 0 && !account.payment_method_on_file) return "card";
  return published.monthly_nanos < current.monthly_nanos ? "downgrade" : "switch";
}

export type BillingSettingsController = {
  summary: Account | undefined;
  isLoading: boolean;
  loadError: Error | null;
  offers: readonly PlanOffer[];
  /** The GPU models the platform fleet runs, which an offer's models are read against. */
  offeredGpuTypes: readonly string[];
  settling: boolean;
  cancelScheduledChange: () => void;
  complimentary: boolean;
  planOpen: boolean;
  openPlan: () => void;
  closePlan: () => void;
  choose: (offer: PlanOffer) => void;
  changingTo: Schemas["TermsVersion"] | null;
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
  const query = useQuery(billingAccountQueryOptions());
  const catalog = useQuery(pricingCatalogQueryOptions());
  const [confirmingChangeTo, setConfirmingChangeTo] = useState<PlanOffer | null>(null);
  const [leaving, setLeaving] = useState<"card" | "portal" | null>(null);

  const change = useMutation({
    mutationFn: changeBillingPlan,
    onSuccess: (account) => {
      queryClient.setQueryData(accountQueryKeys.billing(), account);
      setConfirmingChangeTo(null);
      onPlanOpenChange(false);
    },
  });

  const settling = query.data?.plan_change_pending ?? false;
  const complimentary = Boolean(query.data?.complimentary_since);
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
    if (busy || settling || complimentary || offer.action === "current") return;
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
    offeredGpuTypes:
      catalog.data?.gpu_rates.filter((rate) => rate.enabled).map((rate) => rate.gpu_type) ?? [],
    settling,
    cancelScheduledChange: () => {
      const held = query.data?.plan;
      if (busy || settling || complimentary || !held?.scheduled_terms_version) return;
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
      if (busy || settling || complimentary || !confirmingChangeTo) return;
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
