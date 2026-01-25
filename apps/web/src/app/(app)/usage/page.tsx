"use client";

import { useState, useEffect } from "react";
import { startOfMonth } from "date-fns";
import { DateRangePicker } from "./_components/date-range-picker";
import { StyledTooltip } from "@/components/shared/styled-tooltip";
import { Info } from "lucide-react";
import {
  getAggregatedUsage,
  getAggregatedDailyUsage,
} from "@/actions/usage";
import { getBillingCycle } from "@/actions/billing";
import { UsageOverviewWithChart } from "./_components/usage-overview-with-chart";
import { WorkspaceUsageCard } from "./_components/workspace-usage";
import { SectionDivider } from "@/components/shared/section-divider";
import { SectionHeader } from "@/components/shared/section-header";
import { OverviewSkeleton, WorkspaceBreakdownSkeleton } from "./_components/usage-skeletons";
import { Spinner } from "@/components/shared/spinner";
import type {
  AggregatedUsageResponse,
  AggregatedDailyUsageResponse,
} from "@/interfaces/usage";

// Convert local date to UTC ISO string (matches DateRangePicker's dateToUrlString logic)
const dateToUrlString = (date: Date, isEnd: boolean): string => {
  const year = date.getFullYear();
  const month = date.getMonth();
  const day = date.getDate();

  if (isEnd) {
    // End of local calendar day
    const localEndOfDay = new Date(year, month, day, 23, 59, 59, 999);
    return localEndOfDay.toISOString();
  } else {
    // Start of local calendar day
    const localMidnight = new Date(year, month, day, 0, 0, 0);
    return localMidnight.toISOString();
  }
};

const getDefaultISO = (): { start: string; end: string } => {
  const from = startOfMonth(new Date());
  const to = new Date();
  return {
    start: dateToUrlString(from, false),
    end: dateToUrlString(to, true),
  };
};

export default function UsagePage() {
  const defaultDates = getDefaultISO();
  const [startDateISO, setStartDateISO] = useState<string>(defaultDates.start);
  const [endDateISO, setEndDateISO] = useState<string>(defaultDates.end);
  const [presetId, setPresetId] = useState<string>("this-month");
  const [billingCycle, setBillingCycle] = useState<{ start: Date; end: Date } | null>(null);
  const [billingCycleLoaded, setBillingCycleLoaded] = useState(false);
  const [timezone] = useState<string>(() => {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone;
    } catch {
      return "UTC";
    }
  });
  const [aggregatedUsage, setAggregatedUsage] = useState<AggregatedUsageResponse | null>(null);
  const [aggregatedDailyUsage, setAggregatedDailyUsage] = useState<AggregatedDailyUsageResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  // Fetch billing cycle on mount and set as default dates
  useEffect(() => {
    const fetchBillingCycle = async () => {
      try {
        const cycle = await getBillingCycle();
        const start = new Date(cycle.current_period_start);
        const end = new Date(cycle.current_period_end);
        setBillingCycle({ start, end });
        // Set billing cycle as default dates
        setStartDateISO(dateToUrlString(start, false));
        setEndDateISO(dateToUrlString(end, true));
        setPresetId("billing-cycle");
      } catch {
        // If billing cycle fetch fails, keep calendar month defaults
      } finally {
        setBillingCycleLoaded(true);
      }
    };
    void fetchBillingCycle();
  }, []);

  // Fetch usage data when dates or timezone change (only after billing cycle loaded)
  useEffect(() => {
    if (!billingCycleLoaded) return;

    const fetchData = async () => {
      setIsLoading(true);
      try {
        const [usage, dailyUsage] = await Promise.all([
          getAggregatedUsage(startDateISO, endDateISO),
          getAggregatedDailyUsage(startDateISO, endDateISO, timezone),
        ]);
        setAggregatedUsage(usage);
        setAggregatedDailyUsage(dailyUsage);
      } catch (error) {
        console.error("Failed to fetch usage data:", error);
      } finally {
        setIsLoading(false);
      }
    };

    void fetchData();
  }, [startDateISO, endDateISO, timezone, billingCycleLoaded]);

  const handleDateChange = (start: string, end: string, newPresetId?: string) => {
    setStartDateISO(start);
    setEndDateISO(end);
    if (newPresetId) {
      setPresetId(newPresetId);
    } else {
      setPresetId("");
    }
  };

  const hasAnyUsage = aggregatedUsage?.workspaces.some(
    (workspace) =>
      workspace.usage.cpu_core_hours > 0 ||
      workspace.usage.memory_gb_hours > 0 ||
      workspace.usage.standard_gb_hours > 0 ||
      workspace.usage.shared_gb_hours > 0,
  ) ?? false;

  return (
    <>
      <div className="flex flex-col gap-4 pb-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-3xl font-bold tracking-tight">Usage & Billing</h1>
            <StyledTooltip
              content={
                <p className="text-sm">
                  Costs shown are estimates based on resource usage. Actual billing is
                  calculated and tracked on your invoices.
                </p>
              }
              className="max-w-xs"
            >
              <Info className="text-muted-foreground h-5 w-5 cursor-help" />
            </StyledTooltip>
          </div>
          <p className="text-muted-foreground mt-1.5 text-sm">
            Monitor your resource consumption and costs
          </p>
        </div>
        <div className="flex items-center gap-2">
          {isLoading && aggregatedUsage && aggregatedDailyUsage && (
            <Spinner size="sm" />
          )}
          <DateRangePicker
            startDate={startDateISO}
            endDate={endDateISO}
            presetId={presetId}
            billingCycle={billingCycle}
            onDateChange={handleDateChange}
          />
        </div>
      </div>

      {isLoading && !aggregatedUsage && !aggregatedDailyUsage ? (
        <>
          <OverviewSkeleton />
          <SectionDivider spacing="lg">
            <div className="space-y-5">
              <SectionHeader
                title="Workspace Breakdown"
                description="Detailed usage metrics by workspace"
                indicatorSize="lg"
                titleSize="xl"
              />
              <WorkspaceBreakdownSkeleton />
            </div>
          </SectionDivider>
        </>
      ) : aggregatedUsage && aggregatedDailyUsage ? (
        <>
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
      ) : null}
    </>
  );
}
