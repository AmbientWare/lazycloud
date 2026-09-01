import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { countLabel } from "@/lib/format";
import { formatCostNanos } from "@/lib/money";
import { billingSummaryQueryOptions } from "@/lib/queries/billing";
import { cn } from "@/lib/utils";

/**
 * Why a container here was refused, when the reason belongs to the account.
 *
 * This page is one workspace's spend; the ceiling and the allowance are the
 * account's, and the containers filling either may be in a workspace nobody is
 * looking at. Without this line the page has no answer to the only billing
 * question somebody asks while reading it. Everything that can be changed —
 * the plan, the card, the invoices — lives in settings, and this links there
 * rather than repeating it.
 *
 * The same query the settings section reads, so it costs no extra request.
 */
export function AccountCeilingLine() {
  const summary = useQuery(billingSummaryQueryOptions());
  const data = summary.data;
  if (!data) return null;
  if (!data.plan) {
    // No subscription is the state in which every container is refused, so this
    // is exactly when the line has to say something.
    return (
      <p className="text-xs text-warning">
        No active plan. Subscribe in account settings to start containers.
      </p>
    );
  }
  const limit = data.entitlements?.max_concurrent_containers ?? 0;
  const atLimit = data.usage.concurrent_containers >= limit;
  const overspent = (data.plan.allowance?.remaining_nanos ?? 0) < 0;

  return (
    <p className={cn("text-xs", atLimit || overspent ? "text-warning" : "text-muted-foreground")}>
      {data.usage.concurrent_containers}/{countLabel(limit, "container")} running or queued
      account-wide
      {overspent && data.plan.allowance
        ? ` · ${formatCostNanos(-data.plan.allowance.remaining_nanos, data.currency)} over included usage this period`
        : null}
      {". "}
      <Link
        to="."
        search={(previous) => ({ ...previous, settings: "general" as const })}
        className="underline underline-offset-2 hover:text-foreground"
      >
        Manage {data.plan.name} plan
      </Link>
    </p>
  );
}
