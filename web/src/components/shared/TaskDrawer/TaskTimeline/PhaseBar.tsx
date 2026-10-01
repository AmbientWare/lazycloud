import { useQuery } from "@tanstack/react-query";

import { useLiveNow } from "@/hooks/use-live-now";
import { isTerminalTaskStatus } from "@/lib/format";
import { containerQueryOptions } from "@/lib/queries/containers";
import type { Task } from "@/lib/queries/tasks";

import { LifecycleStrip } from "./LifecycleStrip";
import { executionPhaseDomain, executionPhases, preparationWindows } from "./phases";

/** Single-strip task lifecycle with proportional phases and stable detail text. */
export function PhaseBar({ workspace, task }: { workspace: string; task: Task }) {
  const container = useQuery({
    ...containerQueryOptions(workspace, task.container_id ?? ""),
    enabled: Boolean(task.container_id),
  });

  const live = !isTerminalTaskStatus(task.status);
  const nowMs = useLiveNow(live);

  const domain = executionPhaseDomain(task, nowMs);
  const phases = executionPhases(task, preparationWindows(container.data), nowMs);
  if (!domain || !phases.length) return null;
  return (
    <div className="px-4 pb-3 pt-1.5">
      <LifecycleStrip phases={phases} domain={domain} />
    </div>
  );
}
