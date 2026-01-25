import { hasActiveSubscription } from "@/actions/users";
import { AlertTriangle } from "lucide-react";
import Link from "next/link";
import { Button } from "@/components/ui/button";

export async function SubscriptionBanner() {
  const hasSubscription = await hasActiveSubscription();

  if (hasSubscription) {
    return null;
  }

  return (
    <div className="flex items-center justify-between gap-4 rounded-lg border border-yellow-500/50 bg-yellow-500/10 px-4 py-3">
      <div className="flex items-center gap-3">
        <AlertTriangle className="h-5 w-5 shrink-0 text-yellow-400" />
        <p className="text-sm text-yellow-200">
          <span className="font-semibold">Payment method required.</span>{" "}
          Add a payment method to deploy your applications.
        </p>
      </div>
      <Button asChild size="sm" className="shrink-0 bg-yellow-500 hover:bg-yellow-600 text-black">
        <Link href="/subscribe">Add Payment</Link>
      </Button>
    </div>
  );
}
