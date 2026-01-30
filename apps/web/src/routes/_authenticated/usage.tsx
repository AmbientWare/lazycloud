import { useState } from 'react'
import { createFileRoute } from '@tanstack/react-router'
import { Info } from 'lucide-react'
import { DateRangePicker } from './-components/usage/date-range-picker'
import { UsageOverviewWithChart } from './-components/usage/usage-overview-with-chart'
import { WorkspaceUsageCard } from './-components/usage/workspace-usage'
import { UsagePageSkeleton } from './-components/usage/usage-skeletons'
import { StyledTooltip } from '@/components/shared/styled-tooltip'
import { SectionDivider } from '@/components/shared/section-divider'
import { SectionHeader } from '@/components/shared/section-header'
import { Spinner } from '@/components/shared/spinner'
import { dateToUrlString, getDefaultDateRange } from '@/lib/format-date'
import {
  getAggregatedUsage,
  getAggregatedDailyUsage,
} from '@/server/functions/usage'
import { getBillingCycle } from '@/server/functions/billing'
import type {
  AggregatedUsageResponse,
  AggregatedDailyUsageResponse,
} from '@/interfaces/usage'

export const Route = createFileRoute('/_authenticated/usage')({
  // Show skeleton during navigation while data loads
  pendingComponent: UsagePagePending,
  pendingMs: 0, // Show immediately on navigation
  pendingMinMs: 200, // Keep showing for at least 200ms to avoid flash
  loader: async () => {
    // Fetch billing cycle to determine date range
    let billingCycle: { start: Date; end: Date } | null = null
    let startDateISO: string
    let endDateISO: string
    let presetId: string

    try {
      const cycle = await getBillingCycle()
      const start = new Date(cycle.current_period_start)
      const end = new Date(cycle.current_period_end)
      billingCycle = { start, end }
      startDateISO = dateToUrlString(start, false)
      endDateISO = dateToUrlString(end, true)
      presetId = 'billing-cycle'
    } catch {
      // Fall back to calendar month if billing cycle fails
      const defaults = getDefaultDateRange()
      startDateISO = defaults.start
      endDateISO = defaults.end
      presetId = 'this-month'
    }

    // Fetch usage data with fallback on error
    let aggregatedUsage: AggregatedUsageResponse
    let aggregatedDailyUsage: AggregatedDailyUsageResponse

    try {
      ;[aggregatedUsage, aggregatedDailyUsage] = await Promise.all([
        getAggregatedUsage({
          data: { startDate: startDateISO, endDate: endDateISO },
        }),
        getAggregatedDailyUsage({
          data: { startDate: startDateISO, endDate: endDateISO, timezone: 'UTC' },
        }),
      ])
    } catch (error) {
      console.error('Failed to fetch usage data:', error)
      // Return empty defaults so page still renders
      aggregatedUsage = {
        period: { start: startDateISO, end: endDateISO },
        usage: {
          cpu_core_hours: 0,
          memory_gb_hours: 0,
          build_minutes: 0,
          storage_gb_months: 0,
        },
        workspace_count: 0,
        record_count: 0,
        workspaces: [],
      }
      aggregatedDailyUsage = {
        period: { start: startDateISO, end: endDateISO },
        daily_usage: [],
        workspace_count: 0,
      }
    }

    return {
      billingCycle,
      startDateISO,
      endDateISO,
      presetId,
      aggregatedUsage,
      aggregatedDailyUsage,
    }
  },
  component: UsagePage,
})

function UsagePagePending() {
  return (
    <>
      <div className="flex flex-col gap-4 pb-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-3xl font-bold tracking-tight">
              Usage & Billing
            </h1>
          </div>
          <p className="mt-1.5 text-sm text-muted-foreground">
            Monitor your resource consumption and costs
          </p>
        </div>
      </div>
      <UsagePageSkeleton />
    </>
  )
}

function UsagePage() {
  const loaderData = Route.useLoaderData()

  const [startDateISO, setStartDateISO] = useState<string>(
    loaderData.startDateISO,
  )
  const [endDateISO, setEndDateISO] = useState<string>(loaderData.endDateISO)
  const [presetId, setPresetId] = useState<string>(loaderData.presetId)
  const [aggregatedUsage, setAggregatedUsage] =
    useState<AggregatedUsageResponse>(loaderData.aggregatedUsage)
  const [aggregatedDailyUsage, setAggregatedDailyUsage] =
    useState<AggregatedDailyUsageResponse>(loaderData.aggregatedDailyUsage)
  const [isLoading, setIsLoading] = useState(false)
  const [timezone] = useState<string>(() => {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone
    } catch {
      return 'UTC'
    }
  })

  // Fetch usage data when dates change (user interaction)
  const fetchUsageData = async (start: string, end: string) => {
    setIsLoading(true)
    try {
      const [usage, dailyUsage] = await Promise.all([
        getAggregatedUsage({ data: { startDate: start, endDate: end } }),
        getAggregatedDailyUsage({
          data: { startDate: start, endDate: end, timezone },
        }),
      ])
      setAggregatedUsage(usage)
      setAggregatedDailyUsage(dailyUsage)
    } catch (error) {
      console.error('Failed to fetch usage data:', error)
    } finally {
      setIsLoading(false)
    }
  }

  const handleDateChange = (
    start: string,
    end: string,
    newPresetId?: string,
  ) => {
    setStartDateISO(start)
    setEndDateISO(end)
    if (newPresetId) {
      setPresetId(newPresetId)
    } else {
      setPresetId('')
    }
    void fetchUsageData(start, end)
  }

  const hasAnyUsage = aggregatedUsage.workspaces.some(
    (workspace) =>
      workspace.usage.cpu_core_hours > 0 ||
      workspace.usage.memory_gb_hours > 0 ||
      workspace.usage.build_minutes > 0,
  )

  return (
    <>
      <div className="flex flex-col gap-4 pb-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-3xl font-bold tracking-tight">
              Usage & Billing
            </h1>
            <StyledTooltip
              content={
                <p className="text-sm">
                  Costs shown are estimates based on resource usage. Actual
                  billing is calculated and tracked on your invoices.
                </p>
              }
              className="max-w-xs"
            >
              <Info className="size-5 cursor-help text-muted-foreground" />
            </StyledTooltip>
          </div>
          <p className="mt-1.5 text-sm text-muted-foreground">
            Monitor your resource consumption and costs
          </p>
        </div>
        <div className="flex items-center gap-2">
          {isLoading && <Spinner size="sm" />}
          <DateRangePicker
            startDate={startDateISO}
            endDate={endDateISO}
            presetId={presetId}
            billingCycle={loaderData.billingCycle}
            onDateChange={handleDateChange}
          />
        </div>
      </div>

      <SectionDivider spacing="md">
        <UsageOverviewWithChart
          usage={aggregatedUsage.usage}
          period={aggregatedUsage.period}
          dailyData={aggregatedDailyUsage}
        />
      </SectionDivider>

      <SectionDivider spacing="lg">
        {hasAnyUsage && aggregatedUsage.workspaces.length > 0 ? (
          <div className="space-y-5">
            <SectionHeader
              title="Workspace Breakdown"
              description="Detailed usage metrics by workspace"
              indicatorSize="lg"
              titleSize="xl"
            />
            <div className="w-full space-y-6">
              {aggregatedUsage.workspaces.map((workspace) => (
                <WorkspaceUsageCard
                  key={`${workspace.workspace_id}-${startDateISO}-${endDateISO}`}
                  workspaceName={workspace.workspace_name}
                  usage={workspace.usage}
                  period={aggregatedUsage.period}
                  deployments={workspace.deployments}
                  startDate={startDateISO}
                  endDate={endDateISO}
                />
              ))}
            </div>
          </div>
        ) : null}
      </SectionDivider>
    </>
  )
}
