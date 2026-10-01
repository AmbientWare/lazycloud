import type { Schemas } from "@/lib/api/client";
import { cn } from "@/lib/utils";

/** Why a container stopped, in the words its owner needs. A normal stop needs no cause. */
const CAUSES: Record<Schemas["StopReason"], string | null> = {
  stopped: null,
  load_error: "Its code failed to load",
  start_failed: "It failed to start",
  crashed: "Its process exited unexpectedly",
  out_of_memory: "It ran out of memory",
  host_lost: "Its machine was lost",
};

/**
 * Why a stopped container stopped, on its own line.
 *
 * Deliberately not a `Fact`: the grid those sit in truncates, and a third of a
 * drawer clips the longest of these to its first few words — on a phone there
 * is not even a hover to recover it. Renders nothing when there is nothing
 * truthful to say, so a caller can place it unconditionally.
 */
export function StopCause({
  reason,
  message,
  className,
}: {
  /** The API's stop reason; any other value has no cause to show. */
  reason: string | undefined;
  message?: string;
  className?: string;
}) {
  const cause =
    reason && Object.hasOwn(CAUSES, reason) ? CAUSES[reason as Schemas["StopReason"]] : null;
  if (!cause) return null;
  return (
    <p className={cn("text-sm text-muted-foreground", className)}>
      <span className="micro-label mr-2">Stopped because</span>
      {message ? `${cause}: ${message}` : cause}
    </p>
  );
}
