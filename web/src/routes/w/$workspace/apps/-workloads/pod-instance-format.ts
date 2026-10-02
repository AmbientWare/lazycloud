import type { Schemas } from "@/lib/api/client";
import { durationBetween } from "@/lib/format";

export function podInstancePlacement(container: Schemas["Container"]): string {
  if (container.host) return `Machine ${container.host}`;
  return container.state === "pending" ? "Scheduling" : "Not reported";
}

export function podInstanceUptime(container: Schemas["Container"], now = Date.now()): string {
  if (!container.ready_at) return "-";
  const started = Date.parse(container.ready_at);
  const finished = container.stopped_at ? Date.parse(container.stopped_at) : now;
  if (Number.isNaN(started) || Number.isNaN(finished) || finished < started) return "-";
  const elapsed = finished - started;
  const days = Math.floor(elapsed / 86_400_000);
  if (days === 0) return durationBetween(container.ready_at, container.stopped_at, now) ?? "-";
  const hours = Math.floor((elapsed % 86_400_000) / 3_600_000);
  return `${days}d ${hours}h`;
}
