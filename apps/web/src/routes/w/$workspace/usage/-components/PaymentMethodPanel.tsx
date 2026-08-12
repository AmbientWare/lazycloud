import { useState } from "react";
import { CreditCard, ExternalLink, LoaderCircle } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { openBillingPortal, startCardSetup } from "@/lib/queries/billing";

/**
 * Where somebody goes to start paying, or to change how they already do.
 *
 * On the usage page rather than a settings screen of its own: this is where a
 * person looks at what they owe, and being told an amount is the moment they
 * want to do something about it.
 *
 * Both buttons leave for the payment provider. Nothing about a card is entered,
 * stored, or displayed here — the provider hosts all of it, which is the whole
 * reason this platform never handles card data.
 */
export function PaymentMethodPanel() {
  const [leaving, setLeaving] = useState<"setup" | "portal" | null>(null);

  const go = async (which: "setup" | "portal") => {
    setLeaving(which);
    try {
      await (which === "setup" ? startCardSetup() : openBillingPortal());
    } catch (error) {
      // The redirect never happened, so this page is still here to say so.
      setLeaving(null);
      toast.error(
        which === "setup"
          ? "Could not open the payment page"
          : "Could not open billing management",
        { description: error instanceof Error ? error.message : undefined },
      );
    }
  };

  return (
    <div className="flex flex-col gap-3">
      <p className="text-muted-foreground text-sm">
        Usage is invoiced monthly and charged to the card on file. Cards are held by
        our payment provider and never reach LazyCloud.
      </p>
      <div className="flex flex-wrap gap-2">
        <Button
          variant="default"
          size="sm"
          disabled={leaving !== null}
          onClick={() => void go("setup")}
        >
          {leaving === "setup" ? (
            <LoaderCircle className="size-4 animate-spin" />
          ) : (
            <CreditCard className="size-4" />
          )}
          Add payment method
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={leaving !== null}
          onClick={() => void go("portal")}
        >
          {leaving === "portal" ? (
            <LoaderCircle className="size-4 animate-spin" />
          ) : (
            <ExternalLink className="size-4" />
          )}
          Manage billing
        </Button>
      </div>
    </div>
  );
}
