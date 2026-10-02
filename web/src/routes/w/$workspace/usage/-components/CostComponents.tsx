import type { Schemas } from "@/lib/api/client";
import { formatCostNanos } from "@/lib/money";

import { COST_COMPONENT_LABELS } from "./cost-colors";

export function CostComponents({
  components,
  currency,
}: {
  components: Schemas["UsageCostComponent"][];
  currency: string;
}) {
  return (
    <ul className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
      {components
        .filter((component) => component.quantity > 0)
        .map((component) => (
          <li key={component.component} className="flex items-center gap-1.5">
            {COST_COMPONENT_LABELS[component.component]}
            <span className="mono tabular-nums text-foreground">
              {formatCostNanos(component.cost_nanos, currency)}
            </span>
          </li>
        ))}
    </ul>
  );
}
