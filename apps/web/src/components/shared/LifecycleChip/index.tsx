import { Badge } from "@/components/ui/badge";
import type { MachineLifecycle } from "@/lib/api/schemas";
import { capacityBadge } from "@/lib/machine-lifecycle";

type Tone = "success" | "warning" | "danger" | "muted";

const tones: Record<MachineLifecycle, Tone> = {
  requested: "warning",
  provisioning: "warning",
  booting: "warning",
  joining: "warning",
  ready: "success",
  draining: "muted",
  stopping: "muted",
  stopped: "muted",
  resuming: "warning",
  terminating: "muted",
  deleted: "muted",
  failed: "danger",
};

const labels: Record<MachineLifecycle, string> = {
  requested: "Requested",
  provisioning: "Provisioning",
  booting: "Booting",
  joining: "Joining",
  ready: "Ready",
  draining: "Draining",
  stopping: "Stopping",
  stopped: "Stopped",
  resuming: "Resuming",
  terminating: "Terminating",
  deleted: "Deleted",
  failed: "Failed",
};

/** The one chip for where a machine is in its life; the row beside it carries the message. */
export function LifecycleChip({ lifecycle }: { lifecycle: MachineLifecycle }) {
  return (
    <Badge tone={tones[lifecycle]} className="gap-1.5">
      {lifecycle === "ready" ? (
        <span className="pulse-live size-1.5 rounded-full bg-current" aria-hidden="true" />
      ) : null}
      {labels[lifecycle]}
    </Badge>
  );
}

export function CapacityBadge(props: Parameters<typeof capacityBadge>[0]) {
  const badge = capacityBadge(props);
  if (!badge) return null;
  return <Badge tone={badge.tone}>{badge.label}</Badge>;
}
