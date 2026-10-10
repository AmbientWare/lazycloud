import { LiveRelativeTime } from "@/components/shared/LiveTime";
import type { Schemas } from "@/lib/api/client";

const FAILURE_LABELS: Record<Schemas["DiskOperation"], string> = {
  publish: "Saving while running failed",
  release: "Saving after stop failed",
};

/** Why the holder's last save of a disk failed, shown until a later save succeeds. */
export function DiskFailure({ failure }: { failure: Schemas["DiskFailure"] }) {
  return (
    <p className="mt-2 min-w-0 break-words text-xs text-destructive">
      {FAILURE_LABELS[failure.operation]} <LiveRelativeTime value={failure.failed_at} />:{" "}
      {failure.message}
    </p>
  );
}
