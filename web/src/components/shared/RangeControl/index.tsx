import { cn } from "@/lib/utils";

/**
 * The span every figure in a region is read over.
 *
 * One control for the whole region rather than one per chart: the charts and
 * the totals answer the same question at different resolutions, and two windows
 * would put a total beside a shape that does not add up to it.
 *
 * Pressed buttons rather than a tab strip, because this steers what every chart
 * is asking for rather than switching between them — a tablist here would
 * promise panels to move between that do not exist.
 */
export function RangeControl<Key extends string>({
  options,
  value,
  onChange,
}: {
  options: readonly { key: Key; label: string }[];
  value: Key;
  onChange: (range: Key) => void;
}) {
  return (
    <div
      role="group"
      aria-label="Range"
      className="flex items-center gap-0.5 rounded-md border border-input bg-card p-0.5"
    >
      {options.map(({ key, label }) => {
        const selected = key === value;
        return (
          <button
            key={key}
            type="button"
            aria-pressed={selected}
            onClick={() => onChange(key)}
            className={cn(
              "h-6 rounded-xs px-2 text-xs whitespace-nowrap outline-none transition-colors",
              "focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset",
              selected
                ? "bg-accent font-medium text-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {label}
          </button>
        );
      })}
    </div>
  );
}
