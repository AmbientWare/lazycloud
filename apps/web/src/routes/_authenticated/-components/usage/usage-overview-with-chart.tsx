import { useState, useEffect } from 'react'
import {
  StyledCard,
  StyledCardContent,
  StyledCardDescription,
  StyledCardHeader,
  StyledCardTitle,
} from '@/components/shared/styled-card'
import { ChartContainer, ChartTooltip } from '@/components/ui/chart'
import { SectionIndicator } from '@/components/shared/section-header'
import type {
  UsageMetrics,
  UsagePeriodInfo,
  AggregatedDailyUsageResponse,
} from '@/interfaces/usage'
import { Bar, BarChart, CartesianGrid, XAxis } from 'recharts'
import { formatUsageValue } from '@/lib/format-usage'

interface UsageMetricCardProps {
  label: string
  value: number
  unit?: string
  cost?: number
  valuePrefix?: string
  isTotal?: boolean
}

function UsageMetricCard({
  label,
  value,
  unit,
  cost,
  valuePrefix = '',
  isTotal = false,
}: UsageMetricCardProps) {
  return (
    <div
      className={`rounded-lg border bg-muted p-3 ${
        isTotal
          ? 'border-lazycloud/40 bg-gradient-to-br from-lazycloud/5 to-muted shadow-md shadow-lazycloud/10'
          : 'border-border/40'
      }`}
    >
      <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      {cost !== undefined && !isTotal ? (
        <>
          {/* Cost shown larger but muted */}
          <div className="text-xl font-bold text-foreground">
            ${cost.toFixed(2)}
          </div>
          {/* Usage shown smaller below */}
          <div className="mt-0.5 text-xs text-muted-foreground">
            {formatUsageValue(value)} {unit}
          </div>
        </>
      ) : (
        <div
          className={`font-bold ${isTotal ? 'text-xl text-lazycloud' : 'text-base'}`}
        >
          {valuePrefix}
          {formatUsageValue(value)}
          {unit && (
            <span className="ml-1 text-[10px] text-muted-foreground">
              {unit}
            </span>
          )}
        </div>
      )}
    </div>
  )
}

interface UsageOverviewWithChartProps {
  usage: UsageMetrics
  period: UsagePeriodInfo
  dailyData: AggregatedDailyUsageResponse
}

export function UsageOverviewWithChart({
  usage,
  period,
  dailyData,
}: UsageOverviewWithChartProps) {
  const [mounted, setMounted] = useState(false)

  useEffect(() => {
    setMounted(true)
  }, [])

  const periodStart = new Date(period.start).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
  })
  const periodEnd = new Date(period.end).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  })

  const hasUsage =
    usage.cpu_core_hours > 0 ||
    usage.memory_gb_hours > 0 ||
    usage.build_minutes > 0 ||
    usage.storage_gb_months > 0

  const rawData = dailyData.daily_usage.map((day) => {
    // Parse ISO 8601 UTC timestamp (e.g., "2025-11-05T00:00:00Z")
    const utcDate = new Date(day.date)

    if (isNaN(utcDate.getTime())) {
      // Fallback if date parsing fails
      return {
        date: day.date,
        cost: day.costs?.total_cost ?? 0,
      }
    }

    // Add 12 hours to get to noon UTC, which ensures the date stays
    // the same calendar day in most timezones when converted
    const utcDateNoon = new Date(utcDate.getTime() + 12 * 60 * 60 * 1000)

    // Format in user's local timezone
    const formattedDate = utcDateNoon.toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
    })

    return {
      date: formattedDate,
      cost: day.costs?.total_cost ?? 0,
    }
  })

  // Find max value for color scaling
  const maxValue = Math.max(...rawData.map((d) => d.cost), 1)

  // Map data with colors based on height (LazyCloud blue gradient)
  const chartData = rawData.map((day) => {
    const percentage = day.cost / maxValue
    // Create gradient from light to full LazyCloud blue
    const opacity = 0.3 + percentage * 0.7 // 30% to 100% opacity
    return {
      ...day,
      fill: `color-mix(in srgb, var(--color-lazycloud) ${opacity * 100}%, transparent)`,
    }
  })

  const chartConfig = {
    cost: {
      label: 'Daily Cost',
      color: 'var(--color-lazycloud)',
    },
  }

  return (
    <StyledCard className="border-l-4 border-l-lazycloud/60 shadow-md">
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
            <div className="mb-4 rounded-full bg-muted p-3">
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
            <p className="max-w-md text-sm text-muted-foreground">
              Deploy your first application to start tracking usage metrics
              across your workspaces.
            </p>
          </div>
        ) : (
          <div className="space-y-4">
            {/* Chart */}
            {usage.costs && (
              <div className="rounded-lg border border-border/50 bg-muted p-3">
                <h3 className="mb-2 text-sm font-semibold">Daily Cost</h3>
                {mounted ? (
                  <ChartContainer
                    config={chartConfig}
                    className="h-[120px] w-full sm:h-[140px]"
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
                        className="text-xs text-muted-foreground"
                      />
                      <ChartTooltip
                        content={({ active, payload, label }) => {
                          if (!active || !payload?.length) return null
                          const data = payload[0]?.payload as { cost: number }
                          return (
                            <div className="rounded-lg border border-border/50 bg-card px-2 py-1.5 text-xs shadow-xl">
                              <div className="mb-0.5 text-[10px] text-muted-foreground">
                                {label}
                              </div>
                              <div className="text-lg font-bold text-lazycloud">
                                ${data.cost.toFixed(4)}
                              </div>
                            </div>
                          )
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
                  <div className="flex h-[120px] w-full items-center justify-center rounded-lg bg-muted sm:h-[140px]">
                    <p className="text-sm text-muted-foreground">Loading...</p>
                  </div>
                )}
              </div>
            )}

            {/* Usage and Cost Metrics Cards */}
            <div className="grid grid-cols-2 gap-2 sm:gap-3 md:grid-cols-5">
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
                label="Build"
                value={usage.build_minutes}
                unit="minutes"
                cost={usage.costs?.build_cost}
              />
              <UsageMetricCard
                label="Storage"
                value={usage.storage_gb_months}
                unit="GB-mo"
                cost={usage.costs?.storage_cost}
              />
            </div>
          </div>
        )}
      </StyledCardContent>
    </StyledCard>
  )
}
