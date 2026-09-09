import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { countLabel } from "@/lib/format";
import { billingSummaryQueryOptions } from "@/lib/queries/billing";
import { cn } from "@/lib/utils";

// Account limits include containers in workspaces outside this page.
export function AccountCeilingLine() {
  const summary = useQuery(billingSummaryQueryOptions());
  const data = summary.data;
  if (!data) return null;
  if (!data.plan) {
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
