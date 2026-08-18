import { cn } from "@/lib/utils";

import { usageRangeKeys, usageRangeLabel, type UsageRangeKey } from "./ranges";

/**
 * The span every figure on the page is read over.
 *
 * One control for the whole page rather than one per region: the chart and the
 * list answer the same question at different resolutions, and two windows would
 * put a total beside a shape that does not add up to it.
 *
 * Pressed buttons rather than a tab strip, because this steers what every region
 * is asking for rather than switching between them — a tablist here would
 * promise panels to move between that do not exist.
 */
export function UsageRangeControl({
  value,
  onChange,
}: {
  value: UsageRangeKey;
  onChange: (range: UsageRangeKey) => void;
}) {
  return (
    <div
      role="group"
      aria-label="Range"
      className="flex items-center gap-0.5 rounded-md border border-input bg-card p-0.5"
    >
      {usageRangeKeys.map((key) => {
        const selected = key === value;
        return (
          <button
            key={key}
            type="button"
            aria-pressed={selected}
            onClick={() => onChange(key)}
            className={cn(
              "h-6 rounded-[3px] px-2 text-xs whitespace-nowrap outline-none transition-colors",
              "focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset",
              selected
                ? "bg-accent font-medium text-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {usageRangeLabel(key)}
          </button>
        );
      })}
    </div>
  );
}
