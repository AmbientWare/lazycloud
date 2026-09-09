import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { countLabel } from "@/lib/format";
import { billingSummaryQueryOptions } from "@/lib/queries/billing";
import { cn } from "@/lib/utils";

/**
 * Why a container here was refused, when the reason belongs to the account.
 *
 * This page is one workspace's spend; concurrency limits belong to the
 * account, and the containers filling them may be in a workspace nobody is
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
  const cpuLimit = data.entitlements?.max_concurrent_cpu_containers ?? 0;
  const gpuLimit = data.entitlements?.max_concurrent_gpus ?? 0;
  const atLimit =
    data.usage.concurrent_cpu_containers >= cpuLimit || data.usage.concurrent_gpus >= gpuLimit;

  return (
    <p className={cn("text-xs", atLimit ? "text-warning" : "text-muted-foreground")}>
      {data.usage.concurrent_cpu_containers}/{countLabel(cpuLimit, "CPU container")} and{" "}
      {data.usage.concurrent_gpus}/{countLabel(gpuLimit, "GPU card")} in use account-wide
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
