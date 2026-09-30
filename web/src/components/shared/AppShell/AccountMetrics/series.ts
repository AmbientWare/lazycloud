import type { AccountActivitySeries } from "@/lib/api/schemas";

/**
 * How many apps an account activity chart names before the rest are summed.
 *
 * Four, because that is how many of the theme's chart hues stay apart from one
 * another under simulated red/green colour blindness. A fifth would be a band a
 * reader could not tell from its neighbour, which is worse than the honest
 * "Other apps" row the server folds the tail into.
 */
export const ACTIVITY_SERIES_LIMIT = 4;

/**
 * The hue each stack is drawn in.
 *
 * This order keeps adjacent bands apart under simulated red/green colour
 * blindness. The weakest slot is reserved for the smallest app.
 */
const ACTIVITY_COLORS: readonly string[] = [
  "var(--chart-2)",
  "var(--chart-4)",
  "var(--chart-3)",
  "var(--chart-1)",
];

/**
 * Not one of the named apps, so not one of the identity hues either.
 *
 * The folded row is always drawn last, so it must stay clear of every identity
 * hue rather than only the one beside it in the common case.
 */
const FOLDED_COLOR = "var(--muted-foreground)";

export function activitySeriesColor(series: AccountActivitySeries, index: number): string {
  if (series.kind === "other") return FOLDED_COLOR;
  return ACTIVITY_COLORS[index] ?? FOLDED_COLOR;
}

/**
 * What every stack is called, decided across the whole set rather than one row
 * at a time.
 *
 * An account reads several workspaces, and two of them may hold an app of the
 * same name — or work outside an app, which every workspace can have. The
 * server keeps those apart as separate series; a legend that printed the bare
 * name would put one label on two bands and leave the reader matching colours
 * by eye. The workspace is added to exactly the labels that collide, so the
 * common single-workspace account is not made to read a qualifier it has no use
 * for.
 *
 * An app whose name is empty has been deleted since it ran: the work still
 * happened, so the row keeps its place and says the name is gone rather than
 * carrying one invented to fill the space.
 */
export function activitySeriesLabels(series: readonly AccountActivitySeries[]): string[] {
  const bases = series.map(baseLabel);
  const seen = new Map<string, number>();
  for (const base of bases) seen.set(base, (seen.get(base) ?? 0) + 1);
  return bases.map((base, index) => {
    const workspace = series[index]?.workspace_name;
    if ((seen.get(base) ?? 0) < 2 || !workspace) return base;
    return `${base} · ${workspace}`;
  });
}

function baseLabel(series: AccountActivitySeries): string {
  if (series.kind === "other") return "Other apps";
  if (series.kind === "unassigned") return "Outside an app";
  return series.app_name || "Deleted app";
}

/** Stable per-series key; also the suffix of the CSS variable holding its hue. */
export function activitySeriesKey(index: number): string {
  return `series-${index}`;
}

/**
 * Where a name stops being a name and starts being an identifier.
 *
 * Deployed apps are routinely named by a tool rather than a person, and the
 * generated half is the half that tells two of them apart:
 * `function_scaling_09d30198bef5` and `function_scaling_87767f6d27a3` share
 * every character a reader looks at first. Splitting at the separator lets the
 * identifier be set in the data face, where a run of hex is read digit by digit
 * instead of skimmed as a word — the whole name is still printed, in order, and
 * a name that is not built this way comes back whole.
 *
 * Eight hex characters is the shortest run worth treating as an identifier;
 * below that the tail is as likely to be part of the name as not.
 */
export function splitGeneratedName(label: string): { name: string; identifier: string } {
  const match = /^(.+[-_])([0-9a-f]{8,})$/i.exec(label);
  if (!match) return { name: label, identifier: "" };
  return { name: match[1] ?? label, identifier: match[2] ?? "" };
}
