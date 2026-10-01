import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";

import { Button } from "@/components/ui/button";
import { formatCostNanos } from "@/lib/money";
import { billingAccountQueryOptions } from "@/lib/queries/billing";

const LOW_BALANCE_NANOS = 5_000_000_000;

export function CreditPrompt({ onOpenSettings }: { onOpenSettings: () => void }) {
  const account = useQuery(billingAccountQueryOptions({ balance: true })).data;
  if (!account || account.complimentary_since || account.balance_nanos > LOW_BALANCE_NANOS) {
    return null;
  }

  return (
    <Button
      type="button"
      onClick={onOpenSettings}
      className="mb-3 h-10 w-full justify-start gap-2 px-3 text-[13px]"
    >
      <Plus className="shrink-0" aria-hidden="true" />
      <span className="flex-1 whitespace-nowrap text-left">Add credits</span>
      <span className="shrink-0 border-l border-brand-foreground/20 pl-2.5 tabular-nums">
        {formatCostNanos(account.balance_nanos, account.currency, 2)}
        <span className="sr-only"> available</span>
      </span>
    </Button>
  );
}
