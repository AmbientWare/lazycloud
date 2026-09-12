import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";

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
    <button
      type="button"
      onClick={onOpenSettings}
      className="mb-3 flex w-full items-center gap-2 rounded-md border border-brand/30 bg-brand/10 px-2.5 py-2.5 text-left text-xs outline-none transition-colors hover:bg-brand/15 focus-visible:ring-2 focus-visible:ring-sidebar-ring"
    >
      <Plus className="size-3.5 shrink-0 text-brand" aria-hidden="true" />
      <span className="min-w-0 flex-1">
        <span className="block font-medium">Add credits</span>
        <span className="mt-0.5 block text-[10px] text-muted-foreground">
          Or set up automatic reload
        </span>
      </span>
      <span className="mono shrink-0 text-muted-foreground">
        {formatCostNanos(balance.data.balance_nanos, summary.data.currency, 2)}
        <span className="sr-only"> available</span>
      </span>
    </button>
  );
}
