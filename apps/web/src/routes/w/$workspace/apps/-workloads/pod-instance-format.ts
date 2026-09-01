import type { Container } from "@/lib/api/schemas";
import { durationBetween } from "@/lib/format";

export function podInstancePlacement(container: Container): string {
  const machineId = container.runtime_machine_id || container.machine_id;
  const workerId = container.runtime_worker_id || container.worker_id;
  if (machineId) return `Machine ${machineId}`;
  if (workerId) return `Worker ${workerId}`;
  return container.status === "pending" ? "Scheduling" : "Not reported";
}

export function podInstanceUptime(container: Container, now = Date.now()): string {
  if (!container.started_at) return "-";
  const started = Date.parse(container.started_at);
  const finished = container.finished_at ? Date.parse(container.finished_at) : now;
  if (Number.isNaN(started) || Number.isNaN(finished) || finished < started) return "-";
  const elapsed = finished - started;
  const days = Math.floor(elapsed / 86_400_000);
  if (days === 0) return durationBetween(container.started_at, container.finished_at, now) ?? "-";
  const hours = Math.floor((elapsed % 86_400_000) / 3_600_000);
  return `${days}d ${hours}h`;
}
