import { Badge } from "@/components/ui/badge";
import type { MachineLifecycle } from "@/lib/api/schemas";
import { capacityBadge, humanize } from "@/lib/machine-lifecycle";

type Tone = "success" | "warning" | "danger" | "muted";

const tones: Record<MachineLifecycle, Tone> = {
  requested: "warning",
  provisioning: "warning",
  booting: "warning",
  joining: "warning",
  ready: "success",
  draining: "muted",
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
  terminating: "Terminating",
  deleted: "Deleted",
  failed: "Failed",
};

/** The one chip for where a machine is in its life; the message explains the phase. */
export function LifecycleChip({
  lifecycle,
  message,
  failure,
}: {
  lifecycle: MachineLifecycle;
  message: string;
  failure: string | null;
}) {
  const title = lifecycle === "failed" && failure ? `${humanize(failure)}: ${message}` : message;
  return (
    <Badge tone={tones[lifecycle]} className="gap-1.5" title={title}>
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
