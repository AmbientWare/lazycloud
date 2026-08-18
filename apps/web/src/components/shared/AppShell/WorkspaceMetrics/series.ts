import type { WorkspaceActivitySeries } from "@/lib/api/schemas";

/**
 * How many apps a workspace activity chart names before the rest are summed.
 *
 * Four, because that is how many of the theme's chart hues stay apart from one
 * another under simulated red/green colour blindness. A fifth would be a band a
 * reader could not tell from its neighbour, which is worse than the honest
 * "Other apps" row the server folds the tail into.
 */
export const ACTIVITY_SERIES_LIMIT = 4;

type SeriesColor = { light: string; dark: string };

/**
 * The hue each stack is drawn in, per theme.
 *
 * The two themes deal the same five `--chart-*` tokens in different orders on
 * purpose. Light's `--chart-4` and `--chart-5` are near-identical ambers and
 * light's `--chart-1` is the strongest ink it has, where dark's strongest is
 * `--chart-2`; taking the tokens in their declared order would put the
 * indistinguishable pair side by side in light and the two weakest bands next
 * to each other in dark. These orders keep every adjacent pair separated in the
 * theme it is drawn in, and the weakest slot is the one the smallest app gets.
 */
const ACTIVITY_COLORS: readonly SeriesColor[] = [
  { light: "var(--chart-2)", dark: "var(--chart-2)" },
  { light: "var(--chart-1)", dark: "var(--chart-4)" },
  { light: "var(--chart-3)", dark: "var(--chart-3)" },
  { light: "var(--chart-5)", dark: "var(--chart-1)" },
];

/** Not one of the named apps, so not one of the identity hues either. */
const FOLDED_COLOR: SeriesColor = {
  light: "var(--muted-foreground)",
  dark: "var(--muted-foreground)",
};

export function activitySeriesColor(series: WorkspaceActivitySeries, index: number): SeriesColor {
  if (series.kind === "other") return FOLDED_COLOR;
  return ACTIVITY_COLORS[index] ?? FOLDED_COLOR;
}

/**
 * What a stack is called.
 *
 * An app whose name is empty has been deleted since it ran: the work still
 * happened, so the row keeps its place and says the name is gone rather than
 * carrying one invented to fill the space.
 */
export function activitySeriesLabel(series: WorkspaceActivitySeries): string {
  if (series.kind === "other") return "Other apps";
  if (series.kind === "unassigned") return "Outside an app";
  return series.app_name || "Deleted app";
}

/** Stable per-series key; also the suffix of the CSS variable holding its hue. */
export function activitySeriesKey(index: number): string {
  return `series-${index}`;
}
