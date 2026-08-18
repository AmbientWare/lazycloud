import { cn } from "@/lib/utils";

/**
 * The smallest stub a non-zero share draws.
 *
 * A share that rounds to nothing still has to be visible: a row that cost
 * something and shows an empty track would say it cost nothing. The tracks this
 * is drawn on run from `w-24` to `w-40`, and three percent of the narrowest is
 * the thinnest fill that survives rounding to whole device pixels — two percent
 * disappears there, four percent reads as a real quantity on the widest.
 */
const MINIMUM_FILL_PERCENT = 3;

export type ShareBarTone = "brand" | "capacity";

/**
 * One share of a whole, as a filled length against a quiet track.
 *
 * The figures beside these bars are fractions of a cent, or counts against a
 * ceiling — scales where a column of numbers has nothing standing out in it.
 * Length is what is legible there, and one component is what keeps the same
 * share from drawing three widths in three places.
 *
 * `tone="capacity"` is for the one kind of share that has a bound rather than a
 * peer: it warms as the reading approaches the limit. The colour is always a
 * second reading of a fact stated in words nearby, never the only one.
 *
 * `color` keys the fill to something the row already identifies by colour — a
 * series swatch — where a single accent would make the bar the one part of the
 * row that does not say which series it belongs to.
 */
export function ShareBar({
  share,
  label,
  tone = "brand",
  color,
  className,
}: {
  share: number;
  /** Omitted where the figure printed beside the bar already states the share. */
  label?: string;
  tone?: ShareBarTone;
  color?: string;
  className?: string;
}) {
  const bounded = Number.isFinite(share) ? Math.min(Math.max(share, 0), 1) : 0;
  const width = bounded === 0 ? 0 : Math.max(bounded * 100, MINIMUM_FILL_PERCENT);

  return (
    <span
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      className={cn("block h-1.5 overflow-hidden rounded-full bg-border", className)}
    >
      <span
        className={cn(
          "block h-full rounded-full",
          color ? undefined : tone === "capacity" ? capacityFill(bounded) : "bg-brand",
        )}
        style={{ width: `${width}%`, background: color }}
      />
    </span>
  );
}

function capacityFill(share: number): string {
  if (share >= 1) return "bg-destructive";
  if (share >= 0.8) return "bg-warning";
  return "bg-foreground/45";
}
