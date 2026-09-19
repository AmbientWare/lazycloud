import type { MachineLifecycle } from "@/lib/api/schemas";

type Tone = "success" | "warning" | "danger" | "muted";

/** What the capacity badge should say beside the lifecycle, or nothing when the host is fine. */
export function capacityBadge({
  lifecycle,
  connected,
  capacityState,
}: {
  lifecycle: MachineLifecycle;
  connected: boolean;
  capacityState: "available" | "at_risk" | "draining" | "preempting" | "cordoned";
}): { label: string; tone: Tone } | null {
  if (lifecycle === "ready" && !connected) return { label: "Offline", tone: "danger" };
  switch (capacityState) {
    case "available":
      return null;
    case "at_risk":
      return { label: "At risk", tone: "warning" };
    case "preempting":
      return { label: "Preempting", tone: "danger" };
    case "draining":
      return { label: "Draining", tone: "muted" };
    case "cordoned":
      return { label: "Cordoned", tone: "muted" };
  }
}

export function humanize(value: string): string {
  const spaced = value.replaceAll("_", " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
