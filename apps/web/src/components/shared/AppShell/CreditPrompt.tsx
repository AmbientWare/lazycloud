import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";

import { Button } from "@/components/ui/button";
import { formatCostNanos } from "@/lib/money";
import { billingSummaryQueryOptions, creditBalanceQueryOptions } from "@/lib/queries/billing";

const LOW_BALANCE_NANOS = 5_000_000_000;

export function CreditPrompt({ onOpenSettings }: { onOpenSettings: () => void }) {
  const summary = useQuery(billingSummaryQueryOptions());
  const balance = useQuery(creditBalanceQueryOptions());
  if (
    !summary.data ||
    summary.data.complimentary_since ||
    !balance.data?.ready ||
    balance.data.balance_nanos > LOW_BALANCE_NANOS
  ) {
    return null;
  }

  return (
    <Button
      type="button"
      onClick={onOpenSettings}
      className="mb-3 h-10 w-full justify-start gap-2 border-brand bg-brand px-3 text-[13px] text-brand-foreground hover:bg-lazycloud-light focus-visible:border-brand focus-visible:ring-brand/40"
    >
      <Plus className="shrink-0" aria-hidden="true" />
      <span className="flex-1 whitespace-nowrap text-left">Add credits</span>
      <span className="shrink-0 border-l border-brand-foreground/20 pl-2.5 tabular-nums">
        {formatCostNanos(balance.data.balance_nanos, summary.data.currency, 2)}
        <span className="sr-only"> available</span>
      </span>
    </Button>
  );
}
