import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { format, parseISO } from "date-fns";
import { Area, AreaChart, CartesianGrid, ReferenceLine, XAxis, YAxis } from "recharts";

import { LinearTab, LinearTabsList } from "@/components/shared/LinearSelect";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
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
import { Tabs, TabsContent } from "@/components/ui/tabs";
import { useTheme } from "@/components/shared/ThemeProvider/theme";
import type {
  AccountActivity,
  AccountActivityMeasure,
  AccountActivitySeries,
  AccountActivityUnit,
} from "@/lib/api/schemas";
import { exactTime } from "@/lib/format";
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
} from "./series";
import {
  MEASURE_GROUPS,
  emptyWindowMessage,
  formatReading,
  formatTick,
  measureLabels,
  unitAxisLabels,
  windowSummary,
} from "./units";

const RANGES: AccountActivityRange[] = ["6h", "24h", "7d"];

/**
 * The account's activity over a window: its shape over time, and its rank.
 *
 * One reading behind two views. The chart says when the account was busy and
 * the breakdown says which app made it busy, and because both read the same
 * response neither can quote a figure the other contradicts.
 */
export function ActivityPanel() {
  const [measure, setMeasure] = useState<AccountActivityMeasure>("containers");
  const [range, setRange] = useState<AccountActivityRange>("24h");
  const activity = useQuery(
    accountActivityQueryOptions({ measure, range, limit: ACTIVITY_SERIES_LIMIT }),
  );

  return (
    <Tabs
      defaultValue="over-time"
      className="panel flex min-h-0 flex-1 flex-col overflow-hidden rounded-md"
    >
      <LinearTabsList ariaLabel="Account metrics views" className="shrink-0 bg-card px-2">
        <LinearTab value="over-time">Usage over time</LinearTab>
        <LinearTab value="breakdown">Breakdown</LinearTab>
      </LinearTabsList>

      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border/80 px-3 py-2">
        <span className="micro-label">Resource</span>
        <Select value={measure} onValueChange={(next) => setMeasure(next as AccountActivityMeasure)}>
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

      <TabsContent value="over-time" className="m-0 min-h-0 flex-1 overflow-hidden p-3">
        {activity.isPending ? (
          <ChartSkeleton />
        ) : activity.isError ? (
          <PanelError message={activity.error.message} layout="centered" />
        ) : (
          <ActivityChart activity={activity.data} measure={measure} range={range} />
        )}
      </TabsContent>

      <TabsContent value="breakdown" className="m-0 min-h-0 flex-1 overflow-auto">
        {activity.isPending ? (
          <BreakdownSkeleton />
        ) : activity.isError ? (
          <PanelError message={activity.error.message} layout="centered" />
        ) : (
          <ActivityBreakdown activity={activity.data} measure={measure} range={range} />
        )}
      </TabsContent>
    </Tabs>
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

type ChartRow = Record<string, string | number>;

function ActivityChart({
  activity,
  measure,
  range,
}: {
  activity: AccountActivity;
  measure: AccountActivityMeasure;
  range: AccountActivityRange;
}) {
  const { theme } = useTheme();
  /* Emptiness is about whether anything was measured, never about whether the
     figures are zero: an account that holds no GPU has an answer to "how many
     GPUs", and it is a flat band along the baseline rather than a blank panel
     telling somebody to go and deploy something. */
  if (activity.series.length === 0) {
    return <EmptyWindow measure={measure} range={range} className="h-full min-h-32" />;
  }

  const labels = activitySeriesLabels(activity.series);
  const spine = activity.series[0]?.buckets ?? [];
  const config: ChartConfig = Object.fromEntries(
    activity.series.map((series, index) => [
      activitySeriesKey(index),
      { label: labels[index], theme: activitySeriesColor(series, index) },
    ]),
  );
  const swatches: Record<string, string> = Object.fromEntries(
    activity.series.map((series, index) => [
      activitySeriesKey(index),
      activitySeriesColor(series, index)[theme],
    ]),
  );
  const peak = Math.max(
    0,
    ...spine.map((_bucket, bucketIndex) =>
      activity.series.reduce(
        (stacked, series) => stacked + (series.buckets[bucketIndex]?.value ?? 0),
        0,
      ),
    ),
  );
  const rows: ChartRow[] = spine.map((bucket, bucketIndex) => {
    const row: ChartRow = {
      label: formatBucket(bucket.timestamp, activity.window_seconds),
      full: exactTime(bucket.timestamp),
    };
    for (const [index, series] of activity.series.entries()) {
      row[activitySeriesKey(index)] = series.buckets[bucketIndex]?.value ?? 0;
    }
    return row;
  });

  return (
    <div className="flex h-full min-h-0 flex-col gap-1.5">
      {/* What the scale counts, stated once where a rotated axis title would go.
          Written flat rather than turned on its side: the drawer gives the plot
          a couple of hundred pixels of height, and a rotated caption spends more
          of its width than the label is worth. Not in the strip's uppercase
          label either — `GiB` is a unit symbol, and upper-casing it prints a
          unit nobody publishes. */}
      <p className="shrink-0 pl-1 text-[11px] font-medium text-muted-foreground">
        {unitAxisLabels[activity.unit]}
      </p>
      <ChartContainer config={config} className="aspect-auto min-h-32 w-full flex-1">
        <AreaChart data={rows} margin={{ top: 4, right: 6, bottom: 0, left: 0 }}>
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
            width={44}
            allowDecimals={activity.unit !== "starts"}
            tickFormatter={(value: number | string) => formatTick(Number(value) || 0, activity.unit)}
            tick={{ fontSize: 10 }}
          />
          <ChartTooltip
            content={
              <ChartTooltipContent
                indicator="dot"
                labelFormatter={(_value, payload) => bucketTooltipLabel(payload)}
                formatter={(value, name, item) => (
                  <TooltipReading
                    name={String(name)}
                    color={swatches[String(item.dataKey ?? "")] ?? "var(--muted-foreground)"}
                    value={Number(value) || 0}
                    unit={activity.unit}
                  />
                )}
              />
            }
          />
          {peak > 0 ? null : (
            /* A window whose bands are all flat spans no range, so the scale has
               nothing to derive ticks from and comes back blank — an axis with
               no numbers on it reads as a chart that failed rather than as a
               resource nothing used. This asks for one unit of headroom, which
               is the only way to set the scale of a stacked series: the axis's
               own `domain` is computed from the stack and ignores what the
               element is given. Invisible, because the fact it states is the
               band on the baseline and not a line of its own. */
            <ReferenceLine y={1} ifOverflow="extendDomain" stroke="transparent" />
          )}
          {activity.series.map((series, index) => (
            <Area
              key={activitySeriesKey(index)}
              /* Stepped for both kinds of reading. A count belongs to a whole
                 interval, and a level is the mean across one — neither says
                 anything about an instant inside it, so a curve between two of
                 them would draw readings nobody took. */
              type="stepAfter"
              stackId="activity"
              dataKey={activitySeriesKey(index)}
              name={labels[index]}
              /* The stroke is the surface, not the series: it draws the hairline
               gap that keeps one band's edge off the next one's fill. */
              stroke="var(--card)"
              strokeWidth={2}
              fill={`var(--color-${activitySeriesKey(index)})`}
              fillOpacity={0.85}
              isAnimationActive={false}
            />
          ))}
        </AreaChart>
      </ChartContainer>
      <ActivityLegend series={activity.series} labels={labels} />
    </div>
  );
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
        <span className="min-w-0 truncate text-muted-foreground">{name}</span>
        <span className="mono shrink-0 font-medium tabular-nums text-foreground">
          {formatReading(value, unit)}
        </span>
      </div>
    </>
  );
}

/**
 * The key, in the order the bands are stacked.
 *
 * Written here rather than taken from the chart library, whose legend sorts its
 * entries by value: a key listed in a different order from the stack it
 * describes makes the reader match colours by eye, which is the one job a
 * legend exists to remove.
 */
function ActivityLegend({
  series,
  labels,
}: {
  series: AccountActivitySeries[];
  labels: string[];
}) {
  const { theme } = useTheme();
  return (
    <ul className="flex shrink-0 flex-wrap items-center justify-center gap-x-4 gap-y-1 px-1 text-[11px] text-muted-foreground">
      {series.map((entry, index) => (
        <li key={activitySeriesKey(index)} className="flex min-w-0 items-center gap-1.5">
          <span
            className="size-2 shrink-0 rounded-[2px]"
            style={{ background: activitySeriesColor(entry, index)[theme] }}
            aria-hidden="true"
          />
          <span className="min-w-0 truncate" title={labels[index]}>
            {labels[index]}
          </span>
        </li>
      ))}
    </ul>
  );
}

function ActivityBreakdown({
  activity,
  measure,
  range,
}: {
  activity: AccountActivity;
  measure: AccountActivityMeasure;
  range: AccountActivityRange;
}) {
  if (activity.series.length === 0) {
    return <EmptyWindow measure={measure} range={range} className="h-full min-h-32" />;
  }

  const labels = activitySeriesLabels(activity.series);
  return (
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
      {activity.series.some((series) => series.kind === "other") ? (
        <p className="px-3 py-2.5 text-[11px] leading-5 text-muted-foreground">
          Only the busiest apps are named. Everything else is summed into Other apps, so the shares
          still add up to the window.
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
  const { theme } = useTheme();
  const share = total > 0 ? series.total / total : 0;
  const color = activitySeriesColor(series, index)[theme];
  return (
    <div
      role="listitem"
      className="flex items-center gap-3 border-b border-border/60 px-3 py-2.5 last:border-b-0"
    >
      <span
        className="size-2 shrink-0 rounded-[2px]"
        style={{ background: color }}
        aria-hidden="true"
      />
      <span className="min-w-0 flex-1 truncate text-[13px] text-foreground" title={label}>
        {label}
      </span>
      {/* Fixed width, not fluid: a two-percent bar stretched across the drawer
          puts its stub and its figure too far apart to be read as one row. */}
      <span className="h-1 w-20 shrink-0 rounded-full bg-muted sm:w-32" aria-hidden="true">
        <span
          className="block h-1 rounded-full"
          style={{
            width: `${Math.max(share * 100, share > 0 ? 4 : 0)}%`,
            background: color,
          }}
        />
      </span>
      <span className="mono w-24 shrink-0 text-right text-[13px] tabular-nums text-foreground">
        {formatReading(series.total, unit)}
      </span>
      <span className="mono w-10 shrink-0 text-right text-[11px] tabular-nums text-muted-foreground">
        {formatShare(share)}
      </span>
    </div>
  );
}

function EmptyWindow({
  measure,
  range,
  className,
}: {
  measure: AccountActivityMeasure;
  range: AccountActivityRange;
  className?: string;
}) {
  return (
    <PanelEmpty
      message={emptyWindowMessage(measure, accountActivityRanges[range].label)}
      detail="Deploy a workload or run a task, and its share of the account shows up here."
      className={className}
    />
  );
}

function ChartSkeleton() {
  return (
    <div className="flex h-full min-h-0 flex-col gap-1.5" aria-hidden="true">
      <Skeleton className="h-2.5 w-10 shrink-0" />
      <Skeleton className="min-h-32 flex-1" />
      <div className="flex shrink-0 justify-center gap-4">
        {[0, 1, 2].map((slot) => (
          <Skeleton key={slot} className="h-2.5 w-20" />
        ))}
      </div>
    </div>
  );
}

function BreakdownSkeleton() {
  return (
    <div aria-hidden="true">
      {[0, 1, 2, 3].map((slot) => (
        <div key={slot} className="space-y-2 border-b border-border/60 px-3 py-2.5 last:border-b-0">
          <Skeleton className="h-3.5 w-32" />
          <Skeleton className="h-1 w-full" />
        </div>
      ))}
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

function formatShare(share: number): string {
  if (share > 0 && share < 0.01) return "<1%";
  return `${Math.round(share * 100)}%`;
}
