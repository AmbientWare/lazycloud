import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CreditCard, ExternalLink, LoaderCircle, Sparkles } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import type { BillingPlan } from "@/lib/api/schemas";
import { openBillingPortal, startCardSetup, subscribeToPlan } from "@/lib/queries/billing";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";

/**
 * Where somebody goes to start paying, to move plan, or to change how they pay.
 *
 * The two hosted buttons leave for the payment provider. Nothing about a card is
 * entered, stored, or displayed here — the provider hosts all of it, which is the
 * whole reason this platform never handles card data.
 *
 * Upgrading is the one action that completes here, because nothing about it is a
 * card: it moves the subscription the account already holds onto the Team price,
 * and the answer is the new standing.
 *
 * The management button appears only once there is something at the provider to
 * manage. Offering it earlier would send somebody to a page with nobody on it.
 */
export function PaymentMethodPanel({
  portalAvailable,
  plan,
}: {
  portalAvailable: boolean;
  plan: BillingPlan | null;
}) {
  const onTeam = plan?.id === "team";
  const [leaving, setLeaving] = useState<"setup" | "portal" | null>(null);
  const queryClient = useQueryClient();

  const subscribe = useMutation({
    mutationFn: subscribeToPlan,
    onSuccess: (summary) => {
      // The response is the standing this account now has, so it is written
      // straight into the cache the panel above reads rather than refetched.
      queryClient.setQueryData(accountQueryKeys.billing(), summary);
      toast.success("Moved to the Team plan");
    },
    onError: (error: unknown) => {
      // The commonest refusal is an account with no working card, which the
      // provider says in words worth passing through unaltered.
      toast.error("Could not move to Team", {
        description: error instanceof Error ? error.message : undefined,
      });
    },
  });

  const go = async (which: "setup" | "portal") => {
    setLeaving(which);
    try {
      await (which === "setup" ? startCardSetup() : openBillingPortal());
    } catch (error) {
      // The redirect never happened, so this page is still here to say so.
      setLeaving(null);
      toast.error(
        which === "setup" ? "Could not open the payment page" : "Could not open billing management",
        { description: error instanceof Error ? error.message : undefined },
      );
    }
  };

  const busy = leaving !== null || subscribe.isPending;

  return (
    <div className="flex flex-col gap-3 border-t border-border/80 pt-4">
      <p className="text-sm text-muted-foreground">
        {onTeam
          ? "Usage beyond the included amount is invoiced monthly and charged to the card on file."
          : "Usage beyond the included amount is invoiced monthly, so add a card for it to be charged to. Cards are held by our payment provider and never reach LazyCloud."}
      </p>
      <div className="flex flex-wrap gap-2">
        {onTeam ? null : (
          <Button size="sm" disabled={busy} onClick={() => subscribe.mutate()}>
            {subscribe.isPending ? (
              <LoaderCircle className="size-4 animate-spin" />
            ) : (
              <Sparkles className="size-4" />
            )}
            Subscribe to Team
          </Button>
        )}
        <Button variant="outline" size="sm" disabled={busy} onClick={() => void go("setup")}>
          {leaving === "setup" ? (
            <LoaderCircle className="size-4 animate-spin" />
          ) : (
            <CreditCard className="size-4" />
          )}
          {/* One label in both states: nothing here is told whether a card is
              already saved, and the hosted page saves one and makes it the
              default either way. Replacing one is what the management page is
              for. */}
          Add payment method
        </Button>
        {portalAvailable ? (
          <Button variant="outline" size="sm" disabled={busy} onClick={() => void go("portal")}>
            {leaving === "portal" ? (
              <LoaderCircle className="size-4 animate-spin" />
            ) : (
              <ExternalLink className="size-4" />
            )}
            Manage billing
          </Button>
        ) : null}
      </div>
    </div>
  );
}
