import { ShareBar } from "@/components/shared/ShareBar";
import { shareLabel } from "@/lib/format";
import { formatCostNanos } from "@/lib/money";
import { cn } from "@/lib/utils";

/**
 * The right-hand end of a cost row: the share as a length, the same share in
 * words, and the money.
 *
 * One component across the app list and the workload list beneath it, so a bar
 * of the same width means the same fraction wherever it is read. The bar is
 * dropped on narrow displays rather than squeezed — a stub too short to compare
 * against its neighbours is width spent on nothing — and the figure beside it
 * carries the reading on its own there.
 *
 * `compact` sets the money a step smaller for the workload rows nested inside an
 * app, where a figure louder than the app's own would invert the hierarchy the
 * indentation states.
 */
export function RowFigures({
  share,
  label,
  costNanos,
  currency,
  compact = false,
}: {
  share: number;
  label: string;
  costNanos: number;
  currency: string;
  compact?: boolean;
}) {
  return (
    <>
      <ShareBar share={share} label={label} className="hidden w-24 shrink-0 sm:block lg:w-40" />
      <span className="mono w-10 shrink-0 text-right text-[11px] tabular-nums text-muted-foreground">
        {shareLabel(share)}
      </span>
      <span
        className={cn(
          "readout w-24 shrink-0 text-right text-foreground",
          compact ? "text-xs" : "text-sm",
        )}
      >
        {formatCostNanos(costNanos, currency)}
      </span>
    </>
  );
}
