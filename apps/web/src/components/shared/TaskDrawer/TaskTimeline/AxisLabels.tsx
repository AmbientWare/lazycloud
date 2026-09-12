import { cn } from "@/lib/utils";

import type { AxisTick } from "./timeline";

export function AxisLabels({ ticks, className }: { ticks: AxisTick[]; className: string }) {
  return ticks.map((tick, index) => (
    <span
      key={tick.timestampMs}
      className={cn(
        "absolute text-[10px] tabular-nums text-muted-foreground",
        className,
        index > 0 && (index === ticks.length - 1 ? "-translate-x-full" : "-translate-x-1/2"),
      )}
      style={{ left: `${tick.leftPct}%` }}
      title={new Date(tick.timestampMs).toLocaleString()}
    >
      {tick.label}
    </span>
  ));
}
