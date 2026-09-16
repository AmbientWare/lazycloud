import { useQuery } from "@tanstack/react-query";

import { useLiveNow } from "@/hooks/use-live-now";
import { executionPhaseDomain, executionPhases } from "./phases";
import { isTerminalTaskStatus, type Task } from "@/lib/api/schemas";
import { containerEventSummaryQueryOptions } from "@/lib/queries/events";
import { LifecycleStrip } from "./LifecycleStrip";

/** Single-strip task lifecycle with proportional phases and stable detail text. */
export function PhaseBar({ workspaceId, task }: { workspaceId: string; task: Task }) {
  const summary = useQuery({
    ...containerEventSummaryQueryOptions(workspaceId, task.container_id ?? ""),
    enabled: Boolean(task.container_id),
  });

  const live = !isTerminalTaskStatus(task.status);
  const nowMs = useLiveNow(live);

  const domain = executionPhaseDomain(task, nowMs);
  const phases = executionPhases(task, summary.data?.lifecycle ?? [], nowMs);
  if (!domain || !phases.length) return null;
  return (
    <div className="px-4 pb-3 pt-1.5">
      <LifecycleStrip phases={phases} domain={domain} />
    </div>
  );
}
