"use client";

import { useState, useEffect } from "react";
import {
  StyledCard,
  StyledCardContent,
  StyledCardDescription,
  StyledCardHeader,
  StyledCardTitle,
} from "@/components/shared/styled-card";
import { ChartContainer, ChartTooltip } from "@/components/ui/chart";
import { SectionIndicator } from "@/components/shared/section-header";
import type {
  UsageMetrics,
  UsagePeriodInfo,
  AggregatedDailyUsageResponse,
} from "@/interfaces/usage";
import { Bar, BarChart, CartesianGrid, XAxis } from "recharts";
import { formatUsageValue } from "@/lib/format-usage";

interface UsageMetricCardProps {
  label: string;
  value: number;
  unit?: string;
  cost?: number;
  valuePrefix?: string;
  isTotal?: boolean;
}

function UsageMetricCard({
  label,
  value,
  unit,
  cost,
  valuePrefix = "",
  isTotal = false,
}: UsageMetricCardProps) {
  return (
    <div
      className={`bg-muted rounded-lg border p-3 ${
        isTotal
          ? "border-lazycloud/40 from-lazycloud/5 to-muted bg-gradient-to-br shadow-md shadow-lazycloud/10"
          : "border-border/40"
      }`}
    >
      <div className="text-muted-foreground mb-1 text-[10px] font-medium tracking-wide uppercase">
        {label}
      </div>
      {cost !== undefined && !isTotal ? (
        <>
          {/* Cost shown larger but muted */}
          <div className="text-foreground text-xl font-bold">
            ${cost.toFixed(2)}
          </div>
          {/* Usage shown smaller below */}
          <div className="text-muted-foreground mt-0.5 text-xs">
            {formatUsageValue(value)} {unit}
          </div>
        </>
      ) : (
        <div
          className={`font-bold ${isTotal ? "text-lazycloud text-xl" : "text-base"}`}
        >
          {valuePrefix}
          {formatUsageValue(value)}
          {unit && (
            <span className="text-muted-foreground ml-1 text-[10px]">
              {unit}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

interface UsageOverviewWithChartProps {
  usage: UsageMetrics;
  period: UsagePeriodInfo;
  dailyData: AggregatedDailyUsageResponse;
}

export function UsageOverviewWithChart({
  usage,
  period,
  dailyData,
}: UsageOverviewWithChartProps) {
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  const periodStart = new Date(period.start).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
  });
  const periodEnd = new Date(period.end).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });

  const hasUsage =
    usage.cpu_core_hours > 0 ||
    usage.memory_gb_hours > 0 ||
    usage.standard_gb_hours > 0 ||
    usage.shared_gb_hours > 0 ||
    usage.build_minutes > 0 ||
    usage.public_endpoint_hours > 0;

  const rawData = dailyData.daily_usage.map((day) => {
    // Parse ISO 8601 UTC timestamp (e.g., "2025-11-05T00:00:00Z")
    // The backend now sends explicit UTC timestamps, making conversion clearer
    const utcDate = new Date(day.date);
    
    if (isNaN(utcDate.getTime())) {
      // Fallback if date parsing fails
      return {
        date: day.date,
        cost: day.costs?.total_cost ?? 0,
      };
    }
    
    // Add 12 hours to get to noon UTC, which ensures the date stays
    // the same calendar day in most timezones when converted
    const utcDateNoon = new Date(utcDate.getTime() + 12 * 60 * 60 * 1000);
    
    // Format in user's local timezone - toLocaleDateString automatically converts
    // This shows what calendar day the UTC date represents in the user's timezone
    const formattedDate = utcDateNoon.toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
    });
    
    return {
      date: formattedDate,
      cost: day.costs?.total_cost ?? 0,
    };
  });

  // Find max value for color scaling
  const maxValue = Math.max(...rawData.map((d) => d.cost), 1);

  // Map data with colors based on height (LazyCloud blue gradient)
  const chartData = rawData.map((day) => {
    const percentage = day.cost / maxValue;
    // Create gradient from light to full LazyCloud blue
    const opacity = 0.3 + percentage * 0.7; // 30% to 100% opacity
    return {
      ...day,
      fill: `color-mix(in srgb, var(--color-lazycloud) ${opacity * 100}%, transparent)`,
    };
  });

  const chartConfig = {
    cost: {
      label: "Daily Cost",
      color: "var(--color-lazycloud)",
    },
  };

  return (
    <StyledCard className="border-l-lazycloud/60 border-l-4 shadow-md">
      <StyledCardHeader>
        <div className="mb-1 flex items-center gap-2">
          <SectionIndicator size="md" />
          <StyledCardTitle className="text-xl font-semibold">
            Usage Trends
          </StyledCardTitle>
        </div>
        <StyledCardDescription className="text-sm">
          {periodStart} - {periodEnd}
        </StyledCardDescription>
      </StyledCardHeader>
      <StyledCardContent>
        {!hasUsage ? (
          <div className="flex flex-col items-center justify-center py-12 text-center">
            <div className="bg-muted mb-4 rounded-full p-3">
              <svg
                xmlns="http://www.w3.org/2000/svg"
                width="24"
                height="24"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                className="text-muted-foreground"
              >
                <path d="M3 3v18h18" />
                <path d="m19 9-5 5-4-4-3 3" />
              </svg>
            </div>
            <h3 className="mb-2 text-lg font-semibold">No usage data yet</h3>
            <p className="text-muted-foreground max-w-md text-sm">
              Deploy your first application to start tracking usage metrics
              across your workspaces.
            </p>
          </div>
        ) : (
          <div className="space-y-4">
            {/* Chart */}
            {usage.costs && (
              <div className="bg-muted border-border/50 rounded-lg border p-3">
                <h3 className="mb-2 text-sm font-semibold">Daily Cost</h3>
                {mounted ? (
                  <ChartContainer
                    config={chartConfig}
                    className="h-[140px] w-full"
                  >
                    <BarChart accessibilityLayer data={chartData}>
                      <CartesianGrid
                        vertical={false}
                        strokeDasharray="3 3"
                        className="stroke-muted-foreground/20"
                      />
                      <XAxis
                        dataKey="date"
                        tickLine={false}
                        axisLine={false}
                        tickMargin={6}
                        tickFormatter={(value: string) => value}
                        className="text-muted-foreground text-xs"
                      />
                      <ChartTooltip
                        content={({ active, payload, label }) => {
                          if (!active || !payload?.length) return null;
                          const data = payload[0]?.payload as { cost: number };
                          return (
                            <div className="border-border/50 bg-card rounded-lg border px-2 py-1.5 text-xs shadow-xl">
                              <div className="text-muted-foreground mb-0.5 text-[10px]">
                                {label}
                              </div>
                              <div className="text-lazycloud text-lg font-bold">
                                ${data.cost.toFixed(4)}
                              </div>
                            </div>
                          );
                        }}
                      />
                      <Bar
                        dataKey="cost"
                        radius={[4, 4, 0, 0]}
                        className="fill-lazycloud"
                      />
                    </BarChart>
                  </ChartContainer>
                ) : (
                  <div className="bg-muted flex h-[140px] w-full items-center justify-center rounded-lg">
                    <p className="text-muted-foreground text-sm">Loading...</p>
                  </div>
                )}
              </div>
            )}

            {/* Usage and Cost Metrics Cards */}
            <div className="grid grid-cols-2 gap-3 md:grid-cols-6">
              {usage.costs && (
                <UsageMetricCard
                  label="Total"
                  value={usage.costs.total_cost}
                  valuePrefix="$"
                  isTotal
                />
              )}
              <UsageMetricCard
                label="CPU"
                value={usage.cpu_core_hours}
                unit="core-hrs"
                cost={usage.costs?.cpu_cost}
              />
              <UsageMetricCard
                label="Memory"
                value={usage.memory_gb_hours}
                unit="GB-hrs"
                cost={usage.costs?.memory_cost}
              />
              <UsageMetricCard
                label="Storage"
                value={usage.standard_gb_hours + usage.shared_gb_hours}
                unit="GB-hrs"
                cost={
                  usage.costs
                    ? usage.costs.standard_cost + usage.costs.shared_cost
                    : undefined
                }
              />
              <UsageMetricCard
                label="Build Minutes"
                value={usage.build_minutes}
                unit="min"
                cost={usage.costs?.build_cost}
              />
              <UsageMetricCard
                label="Endpoints"
                value={usage.public_endpoint_hours}
                unit="hrs"
                cost={usage.costs?.endpoint_cost}
              />
            </div>
          </div>
        )}
      </StyledCardContent>
    </StyledCard>
  );
}
