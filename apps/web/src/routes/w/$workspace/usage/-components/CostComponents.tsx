import type { UsageCostComponent } from "@/lib/api/schemas";
import { formatCostNanos } from "@/lib/money";

import { COST_COMPONENT_LABELS, COST_DIMENSIONS } from "./cost-colors";

export function CostComponents({
  components,
  currency,
}: {
  components: UsageCostComponent[];
  currency: string;
}) {
  return (
    <ul className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
      {components
        .filter((component) => component.quantity > 0)
        .map((component) => (
          <li key={component.component} className="flex items-center gap-1.5">
            <span
              aria-hidden="true"
              className="size-2 shrink-0 rounded-[2px]"
              style={{ backgroundColor: COST_DIMENSIONS[component.dimension].color }}
            />
            {COST_COMPONENT_LABELS[component.component]}
            <span className="mono tabular-nums text-foreground">
              {formatCostNanos(component.cost_nanos, currency)}
            </span>
          </li>
        ))}
    </ul>
  );
}
