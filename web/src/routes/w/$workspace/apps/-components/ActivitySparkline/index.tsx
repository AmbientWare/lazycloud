import type { TaskActivityBand } from "@/lib/format";
import { cn } from "@/lib/utils";

/** Top to bottom within a bar: failures sit at the cap, finished work at the base. */
const drawOrder: readonly TaskActivityBand[] = ["failed", "inFlight", "other", "succeeded"];

export const taskActivityBandStyle: Record<TaskActivityBand, string> = {
  failed: "bg-destructive",
  inFlight: "bg-warning",
  other: "bg-muted-foreground/40",
  succeeded: "bg-positive/75",
};

export const taskActivityBandLabel: Record<TaskActivityBand, string> = {
  failed: "Failed",
  inFlight: "Pending",
  other: "Other",
  succeeded: "Succeeded",
};

export type ActivityBands = Partial<Record<TaskActivityBand, number[]>>;

export function ActivitySparkline({
  values,
  bands,
  label,
  className,
}: {
  values: number[];
  /** Per-hour counts for each band, indexed alongside `values`.

      Whatever a bar's total exceeds the bands supplied for it is drawn as
      `other`, so a caller that can only name its failures says so instead of
      having the rest painted as success. */
  bands?: ActivityBands;
  label: string;
  className?: string;
}) {
  const normalizedValues = values.length > 0 ? values : [0];
  const max = Math.max(...normalizedValues, 1);

  return (
    <div
      className={cn("flex h-12 min-w-32 items-end gap-0.5", className)}
      role="img"
      aria-label={label}
    >
      {normalizedValues.map((value, index) => {
        const segments = barSegments(value, index, bands);
        const height = value === 0 ? 2 : Math.max((value / max) * 100, 8);

        return (
          <span
            key={index}
            data-bucket={index}
            className="flex h-full min-w-0 flex-1 items-end"
            title={barTitle(value, segments)}
          >
            <span
              className={cn("flex w-full flex-col overflow-hidden", value === 0 && "bg-border")}
              style={{ height: `${height}%` }}
            >
              {drawOrder.map((band) =>
                segments[band] > 0 ? (
                  <span
                    key={band}
                    data-series={band}
                    className={cn("min-h-0 w-full", taskActivityBandStyle[band])}
                    style={{ flexGrow: segments[band] }}
                  />
                ) : null,
              )}
            </span>
          </span>
        );
      })}
    </div>
  );
}

function barSegments(
  value: number,
  index: number,
  bands: ActivityBands | undefined,
): Record<TaskActivityBand, number> {
  const segments: Record<TaskActivityBand, number> = {
    failed: 0,
    inFlight: 0,
    other: 0,
    succeeded: 0,
  };
  let named = 0;
  for (const band of drawOrder) {
    const amount = Math.max(bands?.[band]?.[index] ?? 0, 0);
    const bounded = Math.min(amount, Math.max(value - named, 0));
    segments[band] = bounded;
    named += bounded;
  }
  segments.other += Math.max(value - named, 0);
  return segments;
}

function barTitle(value: number, segments: Record<TaskActivityBand, number>): string {
  const total = `${value} task${value === 1 ? "" : "s"}`;
  const parts = drawOrder
    .filter((band) => segments[band] > 0)
    .map((band) => `${segments[band]} ${taskActivityBandLabel[band].toLowerCase()}`);
  return parts.length > 0 ? `${total}: ${parts.join(", ")}` : total;
}
