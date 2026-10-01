import { useQuery } from "@tanstack/react-query";

import { useLiveNow } from "@/hooks/use-live-now";
import { containerLifecycleQueryOptions } from "@/lib/queries/containers";
import { executionPhaseDomain, executionPhases } from "./phases";
import { LifecycleStrip } from "./LifecycleStrip";

/** Single-strip task lifecycle with proportional phases and stable detail text. */
export function PhaseBar({
  workspace,
  containerId,
  createdAt,
  startedAt,
  finishedAt,
  live,
}: {
  workspace: string;
  containerId: string | undefined;
  createdAt: string;
  startedAt: string | undefined;
  finishedAt: string | undefined;
  live: boolean;
}) {
  const lifecycle = useQuery({
    ...containerLifecycleQueryOptions(workspace, containerId ?? ""),
    enabled: Boolean(containerId),
  });
  const nowMs = useLiveNow(live);

  const times = { created_at: createdAt, started_at: startedAt, finished_at: finishedAt };
  const domain = executionPhaseDomain(times, nowMs);
  const phases = executionPhases(times, lifecycle.data?.stages ?? [], nowMs);
  if (!domain || !phases.length) return null;
  return (
    <div className="px-4 pb-3 pt-1.5">
      <LifecycleStrip phases={phases} domain={domain} />
    </div>
  );
}
