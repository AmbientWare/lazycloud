import type { UsageBillingLine, UsageBillingOverview } from "@/lib/api/schemas";
import { metricDisplay } from "@/lib/metric-display";

const NANOS_PER_DOLLAR = 1_000_000_000;

export function formatCostNanos(nanos: number, compact = false, currency = "USD"): string {
  const dollars = nanos / NANOS_PER_DOLLAR;
  if (compact) {
    return Intl.NumberFormat(undefined, {
      style: "currency",
      currency,
      currencyDisplay: "narrowSymbol",
      notation: dollars >= 1_000 ? "compact" : "standard",
      maximumFractionDigits: dollars >= 100 ? 0 : dollars >= 1 ? 2 : 3,
    }).format(dollars);
  }
  return Intl.NumberFormat(undefined, {
    style: "currency",
    currency,
    currencyDisplay: "narrowSymbol",
    minimumFractionDigits: 2,
    maximumFractionDigits: dollars > 0 && dollars < 0.01 ? 6 : 2,
  }).format(dollars);
}

export function formatUsageQuantity(line: UsageBillingLine): string {
  if (line.metric === "recorded_compute") return `${line.quantity.toFixed(2)} cents`;
  return metricDisplay(line.metric, line.unit).format(line.quantity);
}

export function billingLabel(report: UsageBillingOverview): string {
  const estimated = report.summary.some((line) => line.cost_basis === "catalog_estimate");
  const recorded = report.summary.some((line) => line.cost_basis !== "catalog_estimate");
  if (recorded && estimated) return "Total cost (recorded + estimated)";
  if (recorded) return "Recorded cost";
  if (estimated) return "Estimated cost";
  return "Total cost";
}
