import { cn } from "@/lib/utils";
import { stopReasonLabel } from "@/lib/format";

/**
 * Why a terminal container stopped, on its own line.
 *
 * Deliberately not a `Fact`: the grid those sit in truncates, and a third of a
 * drawer clips the longest of these to its first few words — on a phone there
 * is not even a hover to recover it. Renders nothing when there is nothing
 * truthful to say, so a caller can place it unconditionally.
 */
export function StopCause({
  terminationReason,
  status,
  className,
}: {
  terminationReason: string | undefined;
  status: string;
  className?: string;
}) {
  const cause = stopReasonLabel(terminationReason, status);
  if (!cause) return null;
  return (
    <p className={cn("text-sm text-muted-foreground", className)}>
      <span className="micro-label mr-2">Stopped because</span>
      {cause}
    </p>
  );
}
