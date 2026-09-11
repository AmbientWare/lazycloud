import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { format, parseISO } from "date-fns";
import { Bar, BarChart, CartesianGrid, ReferenceLine, XAxis, YAxis } from "recharts";
import type { BarShapeProps } from "recharts";

import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
import { ShareBar } from "@/components/shared/ShareBar";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import type {
  AccountActivity,
  AccountActivityMeasure,
  AccountActivitySeries,
  AccountActivityUnit,
} from "@/lib/api/schemas";
import { exactTime, shareLabel } from "@/lib/format";
import {
  accountActivityQueryOptions,
  accountActivityRanges,
  type AccountActivityRange,
} from "@/lib/queries/account-metrics";

import {
  ACTIVITY_SERIES_LIMIT,
  activitySeriesColor,
  activitySeriesKey,
  activitySeriesLabels,
  splitGeneratedName,
} from "./series";
import {
  MEASURE_GROUPS,
  emptyWindowMessage,
  flatWindowNote,
  formatReading,
  formatTick,
  measureLabels,
  unitAxisLabels,
  windowSummary,
} from "./units";

const RANGES: AccountActivityRange[] = ["6h", "24h", "7d"];

/** Wide and short, the way a clock is read; shorter still where the drawer is the whole screen. */
const PLOT_HEIGHT = "h-44 sm:h-56";

/**
 * The account's activity over a window: its shape over time, and its rank.
 *
 * The plot and the list read one response, so neither can quote a figure the
 * other contradicts. The list sits under the plot rather than behind a tab
 * because it is the plot's key as well as its table — a band is only identified
 * once something names it, and a reading is only checkable once something
 * prints it — and because a name a tool generated needs a whole line to be told
 * from its neighbour, which a row of swatches under a chart does not have.
 */
export function ActivityPanel() {
  const [measure, setMeasure] = useState<AccountActivityMeasure>("containers");
  /* Six hours cut into quarter-hours, not a day cut into hours. The drawer is
     opened to ask whether the account is healthy now, and accounts on this
     platform work in bursts: a burst that fills two thirds of a six-hour window
     occupies four hours of a day, so the wider default spent most of the plot
     proving that nothing had happened. The day and the week are one control
     away, and the strip above already reads the day. */
  const [range, setRange] = useState<AccountActivityRange>("6h");
  const activity = useQuery(
    accountActivityQueryOptions({ measure, range, limit: ACTIVITY_SERIES_LIMIT }),
  );

  return (
    <section
      aria-label="Account activity"
      className="panel flex min-h-0 flex-1 flex-col overflow-hidden rounded-md"
    >
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border/80 px-3 py-2">
        <span className="micro-label">Resource</span>
        <Select
          value={measure}
          onValueChange={(next) => setMeasure(next as AccountActivityMeasure)}
        >
          <SelectTrigger size="sm" className="h-7 text-xs" aria-label="Resource">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {MEASURE_GROUPS.map((group) => (
              <SelectGroup key={group.label}>
                <SelectLabel className="micro-label px-2 py-1">{group.label}</SelectLabel>
                {group.measures.map((option) => (
                  <SelectItem key={option} value={option} className="text-xs">
                    {measureLabels[option]}
                  </SelectItem>
                ))}
              </SelectGroup>
            ))}
          </SelectContent>
        </Select>
        <Select value={range} onValueChange={(next) => setRange(next as AccountActivityRange)}>
          <SelectTrigger size="sm" className="h-7 text-xs" aria-label="Time range">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {RANGES.map((option) => (
              <SelectItem key={option} value={option} className="text-xs">
                {accountActivityRanges[option].label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <WindowSummary activity={activity.data} />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {activity.isPending ? (
          <ActivitySkeleton />
        ) : activity.isError ? (
          <div className="flex h-full min-h-40 items-center justify-center">
            <PanelError message={activity.error.message} />
          </div>
        ) : (
          <ActivityReading activity={activity.data} measure={measure} range={range} />
        )}
      </div>
    </section>
  );
}

/** What the window came to, with the figure in the data face and nothing else. */
function WindowSummary({ activity }: { activity: AccountActivity | undefined }) {
  if (!activity) return <p className="ml-auto" />;
  const { reading, caption } = windowSummary(activity.total, activity.unit);
  return (
    <p className="ml-auto text-[11px] text-muted-foreground">
      <span className="mono tabular-nums text-foreground">{reading}</span> {caption}
    </p>
  );
}

function ActivityReading({
  activity,
  measure,
  range,
}: {
  activity: AccountActivity;
  measure: AccountActivityMeasure;
  range: AccountActivityRange;
}) {
  const labels = useMemo(() => activitySeriesLabels(activity.series), [activity.series]);

  /* Emptiness is about whether anything was measured, never about whether the
     figures are zero: an account that holds no GPU has an answer to "how many
     GPUs", and the plot draws it against a real scale rather than blanking and
     telling somebody to go and deploy something. */
  if (activity.series.length === 0) {
    return (
      <PanelEmpty
        message={emptyWindowMessage(measure, accountActivityRanges[range].label)}
        className="h-full min-h-40 py-8"
      />
    );
  }

  return (
    <>
      <ActivityChart activity={activity} measure={measure} labels={labels} />
      <ActivityBreakdown activity={activity} labels={labels} />
    </>
  );
}

type ChartRow = Record<string, string | number>;

function ActivityChart({
  activity,
  measure,
  labels,
}: {
  activity: AccountActivity;
  measure: AccountActivityMeasure;
  labels: string[];
}) {
  const series = activity.series;
  const windowSeconds = activity.window_seconds;
  const keys = useMemo(() => series.map((_entry, index) => activitySeriesKey(index)), [series]);
  const config = useMemo<ChartConfig>(
    () =>
      Object.fromEntries(
        series.map((entry, index) => [
          activitySeriesKey(index),
          { label: labels[index], color: activitySeriesColor(entry, index) },
        ]),
      ),
    [series, labels],
  );
  const rows = useMemo<ChartRow[]>(() => {
    const spine = series[0]?.buckets ?? [];
    return spine.map((bucket, bucketIndex) => {
      const row: ChartRow = {
        label: formatBucket(bucket.timestamp, windowSeconds),
        full: exactTime(bucket.timestamp),
      };
      for (const [index, entry] of series.entries()) {
        row[activitySeriesKey(index)] = entry.buckets[bucketIndex]?.value ?? 0;
      }
      return row;
    });
  }, [series, windowSeconds]);
  const peak = useMemo(
    () =>
      Math.max(
        0,
        ...rows.map((row) => keys.reduce((stacked, key) => stacked + Number(row[key] ?? 0), 0)),
      ),
    [rows, keys],
  );
  /* One shape per band, held across renders: recharts compares this prop by
     identity, and a fresh closure on every render redraws every column of every
     band whenever anything above this changes. */
  const shapes = useMemo(() => keys.map((key) => activitySegment(key, keys)), [keys]);

  return (
    <div className="px-3 pb-2 pt-3">
      {/* What the scale counts, stated once where a rotated axis title would go.
          Written flat rather than turned on its side: the drawer gives the plot
          a couple of hundred pixels of height, and a rotated caption spends more
          of its width than the label is worth. Not in the strip's uppercase
          label either — `GiB` is a unit symbol, and upper-casing it prints a
          unit nobody publishes. */}
      <p className="pl-1 text-[11px] font-medium text-muted-foreground">
        {unitAxisLabels[activity.unit]}
      </p>
      <div className="relative">
        <ChartContainer config={config} className={`aspect-auto w-full ${PLOT_HEIGHT}`}>
          <BarChart
            data={rows}
            margin={{ top: 6, right: 6, bottom: 0, left: 0 }}
            barCategoryGap="18%"
          >
            <CartesianGrid stroke="var(--border)" vertical={false} />
            <XAxis
              dataKey="label"
              stroke="var(--muted-foreground)"
              tickLine={false}
              axisLine={false}
              minTickGap={28}
              tickMargin={6}
              tick={{ fontSize: 10 }}
            />
            <YAxis
              stroke="var(--muted-foreground)"
              tickLine={false}
              axisLine={false}
              width={46}
              allowDecimals={activity.unit !== "starts"}
              tickFormatter={(value: number | string) =>
                formatTick(Number(value) || 0, activity.unit)
              }
              tick={{ fontSize: 10 }}
            />
            <ChartTooltip
              cursor={{ fill: "var(--accent)", opacity: 0.5 }}
              content={
                <ChartTooltipContent
                  indicator="dot"
                  labelFormatter={(_value, payload) => bucketTooltipLabel(payload)}
                  formatter={(value, name, item) => (
                    <TooltipReading
                      name={String(name)}
                      color={item.color ?? "var(--muted-foreground)"}
                      value={Number(value) || 0}
                      unit={activity.unit}
                    />
                  )}
                />
              }
            />
            {peak > 0 ? null : (
              /* A window whose readings are all zero spans no range, so the
                 scale has nothing to derive ticks from and comes back blank —
                 an axis with no numbers on it reads as a chart that failed
                 rather than as a resource nothing used. This asks for one unit
                 of headroom, which is the only way to set the scale of a
                 stacked series: the axis's own `domain` is computed from the
                 stack and ignores what the element is given. Invisible, because
                 the fact it states is the empty plot and not a line of its
                 own. */
              <ReferenceLine y={1} ifOverflow="extendDomain" stroke="transparent" />
            )}
            {series.map((_entry, index) => (
              <Bar
                key={keys[index]}
                /* A column, because a reading belongs to a whole interval and
                   says nothing about an instant inside it: a count is what
                   happened between two boundaries and a level is the mean
                   across them. The column's width is that interval, stated by
                   the mark itself. A curve or a step would have to draw the
                   space between two intervals as though something were measured
                   there, and on the windows this account actually produces —
                   where four buckets in twenty carry everything — a stepped
                   fill degenerates into isolated slabs that read as a broken
                   chart rather than as a quiet one. */
                stackId="activity"
                dataKey={keys[index]}
                name={labels[index]}
                fill={`var(--color-${keys[index]})`}
                maxBarSize={20}
                shape={shapes[index]}
                isAnimationActive={false}
              />
            ))}
          </BarChart>
        </ChartContainer>
        {peak > 0 ? null : (
          <p className="pointer-events-none absolute inset-0 flex items-center justify-center pl-10 text-[11px] text-muted-foreground">
            {flatWindowNote(measure)}
          </p>
        )}
      </div>
    </div>
  );
}

/** The surface showing between two bands of one column; `barCategoryGap` does the same between columns. */
const STACK_GAP = 2;
/** The rounded end of a column, at the only corner that is a reading. */
const CAP_RADIUS = 3;

/**
 * One band of one interval's column.
 *
 * Drawn by hand for the two pieces of negative space a stack needs. The gap
 * between bands is surface showing through rather than a stroke around the
 * fill, so nothing but data carries ink; it is taken off the top of every band
 * that has another above it, which leaves the column's overall height — the
 * figure the axis is read against — exactly where the scale puts it. Only that
 * top is rounded, because only the top of the column is a reading; every
 * boundary below it is a join. A band too short to give the gap away keeps its
 * full height instead, since a hairline reading rounded to nothing is the one
 * thing this must not draw.
 */
function activitySegment(seriesKey: string, keys: readonly string[]) {
  return ({ x, y, width, height, fill, payload }: BarShapeProps) => {
    if (!(height > 0) || !(width > 0)) return null;
    const row = (payload ?? {}) as ChartRow;
    const topmost = keys.filter((key) => Number(row[key] ?? 0) > 0).at(-1);
    const capped = topmost === seriesKey;
    const gap = capped || height <= STACK_GAP + 1 ? 0 : STACK_GAP;
    const top = y + gap;
    const drawn = height - gap;
    if (!capped) {
      return <rect x={x} y={top} width={width} height={drawn} fill={fill} />;
    }
    const radius = Math.min(CAP_RADIUS, width / 2, drawn);
    return (
      <path
        fill={fill}
        d={`M${x},${top + drawn}L${x},${top + radius}Q${x},${top} ${x + radius},${top}L${x + width - radius},${top}Q${x + width},${top} ${x + width},${top + radius}L${x + width},${top + drawn}Z`}
      />
    );
  };
}

/** One band's share of the interval under the cursor, in the unit it is read in. */
function TooltipReading({
  name,
  color,
  value,
  unit,
}: {
  name: string;
  color: string;
  value: number;
  unit: AccountActivityUnit;
}) {
  return (
    <>
      <span
        className="size-2.5 shrink-0 rounded-[2px]"
        style={{ background: color }}
        aria-hidden="true"
      />
      <div className="flex flex-1 items-center justify-between gap-6 leading-none">
        <SeriesLabel label={name} className="min-w-0 truncate text-muted-foreground" />
        <span className="mono shrink-0 font-medium tabular-nums text-foreground">
          {formatReading(value, unit)}
        </span>
      </div>
    </>
  );
}

/**
 * An app's name, with the part a tool generated set in the data face.
 *
 * `function_scaling_09d30198bef5` and `function_scaling_87767f6d27a3` are the
 * same word to anyone skimming, and the half that separates them is the half
 * that looks like noise. Setting it in the monospace face gives the run of hex
 * even spacing and its own texture, so the eye stops there and reads it instead
 * of sliding off; nothing is shortened, elided or invented, and a name no tool
 * generated is left exactly as it was.
 */
function SeriesLabel({ label, className }: { label: string; className?: string }) {
  const { name, identifier } = splitGeneratedName(label);
  return (
    <span className={className} title={label}>
      {name}
      {identifier ? <span className="mono">{identifier}</span> : null}
    </span>
  );
}

/**
 * The key, and the totals it keys.
 *
 * In the order the bands are stacked rather than by size, and written here
 * rather than taken from the chart library, whose legend sorts its entries by
 * value: a key listed in a different order from the stack it describes makes
 * the reader match colours by eye, which is the one job a key exists to remove.
 *
 * Every figure the plot encodes as a length is printed here as a number, which
 * is what keeps the reading available to somebody who cannot separate two of
 * the bands by colour, or reach a tooltip at all.
 */
function ActivityBreakdown({ activity, labels }: { activity: AccountActivity; labels: string[] }) {
  return (
    <div className="border-t border-border/80">
      <p className="micro-label px-3 pb-1 pt-2">By app</p>
      <div role="list" aria-label="Activity by app">
        {activity.series.map((series, index) => (
          <BreakdownRow
            key={activitySeriesKey(index)}
            series={series}
            label={labels[index] ?? ""}
            index={index}
            total={activity.total}
            unit={activity.unit}
          />
        ))}
      </div>
      {activity.series.some((series) => series.kind === "other") ? (
        <p className="px-3 py-2.5 text-[11px] leading-5 text-muted-foreground">
          Other apps combines apps with less activity.
        </p>
      ) : null}
    </div>
  );
}

function BreakdownRow({
  series,
  label,
  index,
  total,
  unit,
}: {
  series: AccountActivitySeries;
  label: string;
  index: number;
  total: number;
  unit: AccountActivityUnit;
}) {
  const share = total > 0 ? series.total / total : 0;
  const color = activitySeriesColor(series, index);
  return (
    <div role="listitem" className="flex items-center gap-3 border-t border-border/60 px-3 py-2">
      <span
        className="size-2 shrink-0 rounded-[2px]"
        style={{ background: color }}
        aria-hidden="true"
      />
      <SeriesLabel label={label} className="min-w-0 flex-1 truncate text-[13px] text-foreground" />
      {/* Fixed width, not fluid: a two-percent bar stretched across the drawer
          puts its stub and its figure too far apart to be read as one row. Gone
          entirely where the drawer is the whole screen — it draws the share the
          figure beside it already states, and the width it costs is the width
          that decides whether two apps named by the same tool can be told
          apart. */}
      <ShareBar share={share} color={color} className="hidden w-32 shrink-0 sm:block" />
      <span className="mono w-24 shrink-0 text-right text-[13px] tabular-nums text-foreground">
        {formatReading(series.total, unit)}
      </span>
      <span className="mono w-10 shrink-0 text-right text-[11px] tabular-nums text-muted-foreground">
        {shareLabel(share)}
      </span>
    </div>
  );
}

function ActivitySkeleton() {
  return (
    <div aria-hidden="true">
      <div className="px-3 pb-2 pt-3">
        <Skeleton className="h-2.5 w-10" />
        <Skeleton className={`mt-1.5 w-full ${PLOT_HEIGHT}`} />
      </div>
      <div className="border-t border-border/80">
        <div className="px-3 pb-1 pt-2">
          <Skeleton className="h-2.5 w-12" />
        </div>
        {[0, 1, 2, 3].map((slot) => (
          <div key={slot} className="flex items-center gap-3 border-t border-border/60 px-3 py-2">
            <Skeleton className="size-2 shrink-0 rounded-[2px]" />
            <Skeleton className="h-3.5 flex-1" />
            <Skeleton className="hidden h-1.5 w-32 shrink-0 sm:block" />
            <Skeleton className="h-3.5 w-24 shrink-0" />
          </div>
        ))}
      </div>
    </div>
  );
}

/**
 * Intervals wider than an hour span days, so their ticks carry the day too;
 * anything narrower is read within one, where the clock time is enough.
 */
function formatBucket(timestamp: string, windowSeconds: number): string {
  try {
    return format(parseISO(timestamp), windowSeconds > 3600 ? "EEE HH:mm" : "HH:mm");
  } catch {
    return timestamp;
  }
}

function bucketTooltipLabel(payload: readonly { payload?: unknown }[] | undefined): string {
  const row = payload?.[0]?.payload;
  if (row && typeof row === "object" && "full" in row && typeof row.full === "string") {
    return row.full;
  }
  return "";
}
