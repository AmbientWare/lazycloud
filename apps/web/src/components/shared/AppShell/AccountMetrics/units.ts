import type { AccountActivityMeasure, AccountActivityUnit } from "@/lib/api/schemas";

/**
 * The resources one selector offers, in two groups.
 *
 * Grouped rather than listed flat because the list holds two different
 * questions: a start is an event the account asked for, and a resource is
 * capacity it held. They answer in different units and nothing sums one to the
 * other, so the control says which is which at the moment somebody picks.
 */
export const MEASURE_GROUPS: readonly {
  label: string;
  measures: readonly AccountActivityMeasure[];
}[] = [
  { label: "Started", measures: ["containers", "tasks"] },
  { label: "Held", measures: ["cpu", "memory", "gpu"] },
];

export const measureLabels = {
  containers: "Containers",
  tasks: "Tasks",
  cpu: "CPU cores",
  memory: "Memory",
  gpu: "GPUs",
} as const satisfies Record<AccountActivityMeasure, string>;

/** What the vertical scale counts, written once above it rather than on every tick. */
export const unitAxisLabels = {
  starts: "starts",
  cores: "cores",
  gibibytes: "GiB",
  gpus: "GPUs",
} as const satisfies Record<AccountActivityUnit, string>;

/**
 * A figure at the precision its unit is read at.
 *
 * A start is a whole thing and never carries a decimal. A level is a fraction of
 * capacity, and a hundredth of a core printed as `0` is the one thing this must
 * not say: the row beside it claims three percent of the account, and a reader
 * given both concludes the panel is broken. Under the last digit it can print,
 * the figure says so rather than rounding to the answer a resource nothing used
 * would give.
 */
export function formatQuantity(value: number, unit: AccountActivityUnit): string {
  if (unit === "starts") return Math.round(value).toLocaleString();
  if (value <= 0) return "0";
  if (value < 0.01) return "<0.01";
  const digits = value >= 100 ? 0 : value >= 10 ? 1 : 2;
  return value.toLocaleString(undefined, { maximumFractionDigits: digits });
}

/**
 * The same figure on an axis, where every tick is a place on a scale.
 *
 * No `<` form here: a tick states where the scale is, and a scale that stops
 * short of its own smallest step cannot be read against the bands drawn on it.
 */
export function formatTick(value: number, unit: AccountActivityUnit): string {
  if (unit === "starts") return Math.round(value).toLocaleString();
  const digits = value >= 100 ? 0 : value >= 10 ? 1 : value >= 1 ? 2 : 4;
  return value.toLocaleString(undefined, { maximumFractionDigits: digits });
}

/** The same figure with its unit attached, for a tooltip row and a breakdown row. */
export function formatReading(value: number, unit: AccountActivityUnit): string {
  const quantity = formatQuantity(value, unit);
  return unit === "starts" ? quantity : `${quantity} ${unitAxisLabels[unit]}`;
}

/**
 * What the window came to, said the way the measure is read.
 *
 * A count totals the window; a level is the level held across it, so the two
 * cannot share a sentence without one of them claiming to be the other. Split
 * into the figure and the words around it, because only the figure is a figure
 * — a whole sentence set in the data face reads as output rather than as prose.
 */
export function windowSummary(
  total: number,
  unit: AccountActivityUnit,
): { reading: string; caption: string } {
  return unit === "starts"
    ? { reading: formatQuantity(total, unit), caption: "started in this window" }
    : { reading: formatReading(total, unit), caption: "held on average" };
}

/** What a window nothing was measured in says, in the measure's own words. */
export function emptyWindowMessage(measure: AccountActivityMeasure, rangeLabel: string): string {
  const started = measure === "containers" || measure === "tasks";
  return started
    ? `No ${measureLabels[measure].toLowerCase()} started in the last ${rangeLabel}`
    : `Nothing was metered in the last ${rangeLabel}`;
}
