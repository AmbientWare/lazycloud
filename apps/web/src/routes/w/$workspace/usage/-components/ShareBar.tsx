import { cn } from "@/lib/utils";

/**
 * One row's share of the total above it.
 *
 * The page's figures are fractions of a cent, where a column of currency reads
 * as a column of $0.00 and nothing stands out. Share is what is legible at that
 * scale, and it is the same encoding the chart above uses for time — one filled
 * length against a quiet track, read the same way in both places.
 *
 * A share that rounds to nothing still draws: a row that cost something and
 * shows an empty track would say it cost nothing.
 */
export function ShareBar({
  share,
  label,
  className,
}: {
  share: number;
  label: string;
  className?: string;
}) {
  const bounded = Number.isFinite(share) ? Math.min(Math.max(share, 0), 1) : 0;
  const width = bounded === 0 ? 0 : Math.max(bounded * 100, 2);

  return (
    <span
      role="img"
      aria-label={label}
      className={cn("block h-1.5 overflow-hidden rounded-full bg-border", className)}
    >
      <span className="block h-full rounded-full bg-brand" style={{ width: `${width}%` }} />
    </span>
  );
}

/** The same fraction in words, for the figure beside the bar. */
export function shareLabel(share: number): string {
  if (!Number.isFinite(share) || share <= 0) return "0%";
  const percent = share * 100;
  if (percent < 1) return "<1%";
  return `${Math.round(percent)}%`;
}
