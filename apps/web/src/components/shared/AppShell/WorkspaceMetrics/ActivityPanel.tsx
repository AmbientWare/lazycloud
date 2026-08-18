import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { format, parseISO } from "date-fns";
import { Area, AreaChart, CartesianGrid, XAxis, YAxis } from "recharts";

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
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent } from "@/components/ui/tabs";
import { useTheme } from "@/components/shared/ThemeProvider/theme";
import type {
  WorkspaceActivity,
  WorkspaceActivityMeasure,
  WorkspaceActivitySeries,
} from "@/lib/api/schemas";
import { exactTime } from "@/lib/format";
import {
  workspaceActivityMeasureLabels,
  workspaceActivityQueryOptions,
  workspaceActivityRanges,
  type WorkspaceActivityRange,
} from "@/lib/queries/workspace-metrics";

import {
  ACTIVITY_SERIES_LIMIT,
  activitySeriesColor,
  activitySeriesKey,
  activitySeriesLabel,
} from "./series";

const MEASURES: WorkspaceActivityMeasure[] = ["containers", "tasks"];
const RANGES: WorkspaceActivityRange[] = ["6h", "24h", "7d"];

/**
 * The workspace's starts over a window: their shape over time, and their rank.
 *
 * One reading behind two views. The chart says when the workspace was busy and
 * the breakdown says which app made it busy, and because both read the same
 * response neither can quote a figure the other contradicts.
 */
export function ActivityPanel({ workspaceId }: { workspaceId: string }) {
  const [measure, setMeasure] = useState<WorkspaceActivityMeasure>("containers");
  const [range, setRange] = useState<WorkspaceActivityRange>("24h");
  const activity = useQuery(
    workspaceActivityQueryOptions(workspaceId, {
      measure,
      range,
      limit: ACTIVITY_SERIES_LIMIT,
    }),
  );

  return (
    <Tabs
      defaultValue="over-time"
      className="panel flex min-h-0 flex-1 flex-col overflow-hidden rounded-md"
    >
      <LinearTabsList ariaLabel="Workspace metrics views" className="shrink-0 bg-card px-2">
        <LinearTab value="over-time">Usage over time</LinearTab>
        <LinearTab value="breakdown">Breakdown</LinearTab>
      </LinearTabsList>

      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border/80 px-3 py-2">
        <Select
          value={measure}
          onValueChange={(next) => setMeasure(next as WorkspaceActivityMeasure)}
        >
          <SelectTrigger size="sm" className="h-7 text-xs" aria-label="Resource">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {MEASURES.map((option) => (
              <SelectItem key={option} value={option} className="text-xs">
                {workspaceActivityMeasureLabels[option]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={range} onValueChange={(next) => setRange(next as WorkspaceActivityRange)}>
          <SelectTrigger size="sm" className="h-7 text-xs" aria-label="Time range">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {RANGES.map((option) => (
              <SelectItem key={option} value={option} className="text-xs">
                {workspaceActivityRanges[option].label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <p className="ml-auto text-[11px] text-muted-foreground">
          {activity.data ? (
            <>
              <span className="mono tabular-nums text-foreground">
                {activity.data.total.toLocaleString()}
              </span>{" "}
              in this window
            </>
          ) : null}
        </p>
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

type ChartRow = Record<string, string | number>;

function ActivityChart({
  activity,
  measure,
  range,
}: {
  activity: WorkspaceActivity;
  measure: WorkspaceActivityMeasure;
  range: WorkspaceActivityRange;
}) {
  if (activity.total === 0) {
    return <EmptyWindow measure={measure} range={range} className="h-full min-h-32" />;
  }

  const spine = activity.series[0]?.buckets ?? [];
  const config: ChartConfig = Object.fromEntries(
    activity.series.map((series, index) => [
      activitySeriesKey(index),
      { label: activitySeriesLabel(series), theme: activitySeriesColor(series, index) },
    ]),
  );
  const rows: ChartRow[] = spine.map((bucket, bucketIndex) => {
    const row: ChartRow = {
      label: formatBucket(bucket.timestamp, activity.window_seconds),
      full: exactTime(bucket.timestamp),
    };
    for (const [index, series] of activity.series.entries()) {
      row[activitySeriesKey(index)] = series.buckets[bucketIndex]?.count ?? 0;
    }
    return row;
  });

  return (
    <div className="flex h-full min-h-0 flex-col gap-2">
      <ChartContainer config={config} className="aspect-auto min-h-32 w-full flex-1">
        <AreaChart data={rows} margin={{ top: 6, right: 6, bottom: 0, left: 0 }}>
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
            width={38}
            allowDecimals={false}
            tick={{ fontSize: 10 }}
          />
          <ChartTooltip
            content={
              <ChartTooltipContent
                indicator="dot"
                labelFormatter={(_value, payload) => bucketTooltipLabel(payload)}
              />
            }
          />
          {activity.series.map((series, index) => (
            <Area
              key={activitySeriesKey(index)}
              /* Stepped, not curved: each reading counts a whole interval, so the
               band holds its height across that interval. A smoothed curve would
               draw counts between intervals that were never taken. */
              type="stepAfter"
              stackId="activity"
              dataKey={activitySeriesKey(index)}
              name={activitySeriesLabel(series)}
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
      <ActivityLegend series={activity.series} />
    </div>
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
function ActivityLegend({ series }: { series: WorkspaceActivitySeries[] }) {
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
          <span className="min-w-0 truncate" title={activitySeriesLabel(entry)}>
            {activitySeriesLabel(entry)}
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
  activity: WorkspaceActivity;
  measure: WorkspaceActivityMeasure;
  range: WorkspaceActivityRange;
}) {
  if (activity.total === 0) {
    return <EmptyWindow measure={measure} range={range} className="h-full min-h-32" />;
  }

  return (
    <div role="list" aria-label="Activity by app">
      {activity.series.map((series, index) => (
        <BreakdownRow
          key={activitySeriesKey(index)}
          series={series}
          index={index}
          total={activity.total}
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
  index,
  total,
}: {
  series: WorkspaceActivitySeries;
  index: number;
  total: number;
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
      <span className="min-w-0 flex-1 truncate text-[13px] text-foreground">
        {activitySeriesLabel(series)}
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
      <span className="mono w-12 shrink-0 text-right text-[13px] tabular-nums text-foreground">
        {series.total.toLocaleString()}
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
  measure: WorkspaceActivityMeasure;
  range: WorkspaceActivityRange;
  className?: string;
}) {
  return (
    <PanelEmpty
      message={`No ${measure} started in the last ${workspaceActivityRanges[range].label}`}
      detail="Deploy a workload or run a task, and its share of the workspace shows up here."
      className={className}
    />
  );
}

function ChartSkeleton() {
  return (
    <div className="flex h-full min-h-0 flex-col gap-2" aria-hidden="true">
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
