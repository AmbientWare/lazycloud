"use client";

import { useState, useCallback, useRef, useEffect } from "react";
import { formatUsageValue } from "@/lib/format-usage";
import { formatShortDate } from "@/lib/format-date";
import { cn } from "@/lib/utils";
import { Accordion } from "@/components/ui/accordion";
import {
  StyledAccordionItem,
  StyledAccordionTrigger,
  StyledAccordionContent,
} from "@/components/shared/styled-accordion";
import { StyledCard } from "@/components/shared/styled-card";
import { SectionIndicator } from "@/components/shared/section-header";
import { Spinner } from "@/components/shared/spinner";
import { StatusBadge } from "@/components/shared/status-badge";
import { SectionHeader } from "./usage-cards";
import { getDeploymentCostBreakdown } from "@/actions/usage";
import type {
  UsageMetrics,
  UsagePeriodInfo,
  DeploymentUsageOverview,
  WorkspaceCostBreakdownResponse,
} from "@/interfaces/usage";

interface WorkspaceUsageCardProps {
  workspaceName: string;
  usage: UsageMetrics;
  period: UsagePeriodInfo;
  deployments: DeploymentUsageOverview[];
  startDate: string;
  endDate: string;
  workspaceStatus: "Active" | "Inactive";
}

export function WorkspaceUsageCard({
  workspaceName,
  usage,
  period,
  deployments,
  startDate,
  endDate,
  workspaceStatus,
}: WorkspaceUsageCardProps) {
  const [breakdowns, setBreakdowns] = useState<
    Map<string, WorkspaceCostBreakdownResponse>
  >(new Map());
  const [loadingBreakdowns, setLoadingBreakdowns] = useState<Set<string>>(
    new Set(),
  );

  // Use refs to access latest state without causing callback recreation
  const breakdownsRef = useRef(breakdowns);
  const loadingBreakdownsRef = useRef(loadingBreakdowns);

  useEffect(() => {
    breakdownsRef.current = breakdowns;
  }, [breakdowns]);

  useEffect(() => {
    loadingBreakdownsRef.current = loadingBreakdowns;
  }, [loadingBreakdowns]);

  const handleAccordionChange = useCallback(
    async (value: string | undefined) => {
      if (!value) return;

      const deploymentId = value;
      const deployment = deployments.find(
        (d) => d.deployment_id === deploymentId,
      );

      if (!deployment) return;

      // Skip if breakdown already loaded or is loading
      if (
        breakdownsRef.current.has(deploymentId) ||
        loadingBreakdownsRef.current.has(deploymentId)
      ) {
        return;
      }

      // Fetch breakdown
      setLoadingBreakdowns((prev) => new Set(prev).add(deploymentId));

      try {
        const breakdown = await getDeploymentCostBreakdown(
          deploymentId,
          startDate,
          endDate,
        );

        setBreakdowns((prev) => {
          const next = new Map(prev);
          next.set(deploymentId, breakdown);
          return next;
        });
      } catch (error) {
        console.error(
          `Failed to fetch cost breakdown for deployment ${deploymentId}:`,
          error,
        );
      } finally {
        setLoadingBreakdowns((prev) => {
          const next = new Set(prev);
          next.delete(deploymentId);
          return next;
        });
      }
    },
    [deployments, startDate, endDate],
  );

  const getDeploymentWithBreakdown = useCallback(
    (deployment: DeploymentUsageOverview): DeploymentUsageOverview & {
      cost_breakdown?: WorkspaceCostBreakdownResponse;
    } => {
      const breakdown = breakdowns.get(deployment.deployment_id);
      if (breakdown) {
        return {
          ...deployment,
          cost_breakdown: breakdown,
        };
      }
      return deployment;
    },
    [breakdowns],
  );

  // Calculate number of days in period for averages
  const periodStartDate = new Date(period.start);
  const periodEndDate = new Date(period.end);
  const daysDiff =
    Math.max(
      1,
      Math.ceil(
        (periodEndDate.getTime() - periodStartDate.getTime()) / (1000 * 60 * 60 * 24),
      ),
    ) || 1;

  // Calculate average metrics per day
  const avgCpu = usage.cpu_core_hours / daysDiff;
  const avgMemory = usage.memory_gb_hours / daysDiff;
  const avgStorage = (usage.standard_gb_hours + usage.shared_gb_hours) / daysDiff;
  const avgBuild = usage.build_minutes / daysDiff;
  const avgEndpoints = usage.public_endpoint_hours / daysDiff;

  return (
    <StyledCard className="border-l-lazycloud/40 border-l-2 shadow-md">
      {/* Workspace Header */}
      <div className="p-6 pb-4">
        <div className="flex items-start justify-between">
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-3">
              <h3 className="text-lg font-semibold">{workspaceName}</h3>
              {usage.costs && (
                <span className="text-lazycloud bg-lazycloud/10 rounded-md px-2.5 py-1 text-base font-bold">
                  ${usage.costs.total_cost.toFixed(2)}
                </span>
              )}
            </div>
            <div className="text-muted-foreground flex flex-wrap items-center gap-x-4 gap-y-0.5 text-xs">
              <span>
                CPU:{" "}
                <span className="text-foreground font-semibold">
                  {formatUsageValue(avgCpu)}
                </span>
                /day
              </span>
              <span>
                Memory:{" "}
                <span className="text-foreground font-semibold">
                  {formatUsageValue(avgMemory)}
                </span>
                /day
              </span>
              <span>
                Storage:{" "}
                <span className="text-foreground font-semibold">
                  {formatUsageValue(avgStorage)}
                </span>
                /day
              </span>
              <span>
                Build:{" "}
                <span className="text-foreground font-semibold">
                  {formatUsageValue(avgBuild)}
                </span>
                min/day
              </span>
              <span>
                Endpoints:{" "}
                <span className="text-foreground font-semibold">
                  {formatUsageValue(avgEndpoints)}
                </span>
                hrs/day
              </span>
            </div>
          </div>
          <StatusBadge status={workspaceStatus} />
        </div>
      </div>

      {/* Deployment Breakdown */}
      {deployments.length > 0 && (
        <div className="relative px-6 py-4">
          <div className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-border/20 to-transparent" />
          <div className="mb-4 flex items-center gap-2 text-sm font-semibold">
            <SectionIndicator size="sm" variant="subtle" />
            <span>Deployment Breakdown</span>
            <span className="text-muted-foreground bg-muted rounded px-1.5 py-0.5 text-xs font-normal">
              ({deployments.length})
            </span>
          </div>

          <Accordion
            type="single"
            collapsible
            className="w-full space-y-2"
            onValueChange={handleAccordionChange}
          >
            {deployments.map((deployment) => {
              const deploymentWithBreakdown = getDeploymentWithBreakdown(deployment);
              const isLoading = loadingBreakdowns.has(deployment.deployment_id);
              return (
              <StyledAccordionItem
                key={deployment.deployment_id}
                value={deployment.deployment_id}
              >
                <StyledAccordionTrigger>
                  <div className="flex w-full items-center justify-between pr-4">
                    <div className="flex items-center gap-3">
                      <span className="h-2 w-2 rounded-full bg-lazycloud"></span>
                      <span className="group-data-[state=open]:text-lazycloud text-sm font-semibold transition-colors">
                        {deployment.deployment_name}
                      </span>
                      {deployment.usage.costs && (
                        <span className="text-lazycloud bg-lazycloud/10 rounded-md px-2 py-0.5 text-xs font-bold">
                          ${deployment.usage.costs.total_cost.toFixed(2)}
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-3">
                      {deployment.status === "Active" && deployment.deployed_at && (
                        <span className="text-muted-foreground text-xs">
                          Deployed {formatShortDate(deployment.deployed_at)}
                        </span>
                      )}
                      {deployment.status === "Inactive" && deployment.deleted_at && (
                        <span className="text-muted-foreground text-xs">
                          Destroyed {formatShortDate(deployment.deleted_at)}
                        </span>
                      )}
                      <StatusBadge status={deployment.status} />
                    </div>
                  </div>
                </StyledAccordionTrigger>
                <StyledAccordionContent>
                  {isLoading ? (
                    <div className="flex items-center justify-center">
                      <Spinner size="md" />
                    </div>
                  ) : (
                    <div className="space-y-4">
                        {/* Deployment Cost */}
                        {deploymentWithBreakdown.usage.costs && (
                        <div>
                          <h3 className="text-base font-bold mb-2">
                            Deployment Cost: ${deploymentWithBreakdown.usage.costs.total_cost.toFixed(2)}
                          </h3>
                        </div>
                      )}

                      {/* Usage Breakdown */}
                      <div className="space-y-3">
                        <SectionHeader>Usage Breakdown</SectionHeader>
                        <div className="bg-muted rounded-lg border overflow-hidden">
                          <div className="overflow-x-auto">
                            <table className="w-full text-sm">
                              <thead>
                                <tr className="border-b bg-muted">
                                  <th className="text-left p-3 font-semibold">Metric</th>
                                  <th className="text-right p-3 font-semibold">
                                    Usage
                                    <span className="text-muted-foreground font-normal text-xs ml-1">
                                      (unit-hrs)
                                    </span>
                                  </th>
                                  <th className="text-right p-3 font-semibold">
                                    Cost
                                    <span className="text-muted-foreground font-normal text-xs ml-1">
                                      ($)
                                    </span>
                                  </th>
                                </tr>
                              </thead>
                              <tbody>
                                <tr className="border-b bg-card">
                                  <td className="p-3">
                                    <span className="font-medium">CPU (core)</span>
                                  </td>
                                  <td className="p-3 text-right">
                                    {formatUsageValue(deploymentWithBreakdown.usage.cpu_core_hours)}
                                  </td>
                                  <td className="p-3 text-right">
                                    {deploymentWithBreakdown.usage.costs ? (
                                      <span className="font-semibold">
                                        {deploymentWithBreakdown.usage.costs.cpu_cost.toFixed(2)}
                                      </span>
                                    ) : (
                                      <span className="text-muted-foreground">-</span>
                                    )}
                                  </td>
                                </tr>
                                <tr className="border-b bg-card">
                                  <td className="p-3">
                                    <span className="font-medium">Memory (GB)</span>
                                  </td>
                                  <td className="p-3 text-right">
                                    {formatUsageValue(deploymentWithBreakdown.usage.memory_gb_hours)}
                                  </td>
                                  <td className="p-3 text-right">
                                    {deploymentWithBreakdown.usage.costs ? (
                                      <span className="font-semibold">
                                        {deploymentWithBreakdown.usage.costs.memory_cost.toFixed(2)}
                                      </span>
                                    ) : (
                                      <span className="text-muted-foreground">-</span>
                                    )}
                                  </td>
                                </tr>
                                <tr className="border-b bg-card">
                                  <td className="p-3">
                                    <span className="font-medium">Storage (GB)</span>
                                  </td>
                                  <td className="p-3 text-right">
                                    {formatUsageValue(
                                      deploymentWithBreakdown.usage.standard_gb_hours +
                                        deploymentWithBreakdown.usage.shared_gb_hours
                                    )}
                                  </td>
                                  <td className="p-3 text-right">
                                    {deploymentWithBreakdown.usage.costs ? (
                                      <span className="font-semibold">
                                        {(
                                          deploymentWithBreakdown.usage.costs.standard_cost +
                                          deploymentWithBreakdown.usage.costs.shared_cost
                                        ).toFixed(2)}
                                      </span>
                                    ) : (
                                      <span className="text-muted-foreground">-</span>
                                    )}
                                  </td>
                                </tr>
                                <tr className="border-b bg-card">
                                  <td className="p-3">
                                    <span className="font-medium">Build Minutes</span>
                                  </td>
                                  <td className="p-3 text-right">
                                    {formatUsageValue(deploymentWithBreakdown.usage.build_minutes)}
                                  </td>
                                  <td className="p-3 text-right">
                                    {deploymentWithBreakdown.usage.costs ? (
                                      <span className="font-semibold">
                                        {deploymentWithBreakdown.usage.costs.build_cost.toFixed(2)}
                                      </span>
                                    ) : (
                                      <span className="text-muted-foreground">-</span>
                                    )}
                                  </td>
                                </tr>
                                <tr className="border-b last:border-b-0 bg-card">
                                  <td className="p-3">
                                    <span className="font-medium">Endpoints (hours)</span>
                                  </td>
                                  <td className="p-3 text-right">
                                    {formatUsageValue(deploymentWithBreakdown.usage.public_endpoint_hours)}
                                  </td>
                                  <td className="p-3 text-right">
                                    {deploymentWithBreakdown.usage.costs ? (
                                      <span className="font-semibold">
                                        {deploymentWithBreakdown.usage.costs.endpoint_cost.toFixed(2)}
                                      </span>
                                    ) : (
                                      <span className="text-muted-foreground">-</span>
                                    )}
                                  </td>
                                </tr>
                              </tbody>
                              {deploymentWithBreakdown.usage.costs && (
                                <tfoot>
                                  <tr className="bg-muted border-t-2">
                                    <td className="p-3 font-semibold">Total</td>
                                    <td className="p-3"></td>
                                    <td className="p-3 text-right font-semibold">
                                      {deploymentWithBreakdown.usage.costs.total_cost.toFixed(2)}
                                    </td>
                                  </tr>
                                </tfoot>
                              )}
                            </table>
                          </div>
                        </div>
                      </div>

                      {/* Service Breakdown */}
                      {deploymentWithBreakdown.cost_breakdown && (
                        <div className="space-y-3">
                          <SectionHeader>Service Breakdown</SectionHeader>
                            {deploymentWithBreakdown.cost_breakdown.service_breakdown &&
                            deploymentWithBreakdown.cost_breakdown.service_breakdown.length > 0 ? (
                              <div className="space-y-3">
                                <div className="bg-muted rounded-lg border overflow-hidden">
                                  <div className="overflow-x-auto">
                                    <table className="w-full text-sm">
                                      <thead>
                                        <tr className="border-b bg-muted">
                                          <th className="text-left p-3 font-semibold">Service</th>
                                          <th className="text-right p-3 font-semibold">
                                            CPU Usage
                                            <span className="text-muted-foreground font-normal text-xs ml-1">
                                              (core-hrs)
                                            </span>
                                          </th>
                                          <th className="text-right p-3 font-semibold">
                                            Memory Usage
                                            <span className="text-muted-foreground font-normal text-xs ml-1">
                                              (GB-hrs)
                                            </span>
                                          </th>
                                          <th className="text-right p-3 font-semibold">
                                            Cost
                                            <span className="text-muted-foreground font-normal text-xs ml-1">
                                              ($)
                                            </span>
                                          </th>
                                        </tr>
                                      </thead>
                                      <tbody>
                                        {deploymentWithBreakdown.cost_breakdown.service_breakdown.map(
                                          (service, idx) => (
                                            <tr
                                              key={service.service_name}
                                              className={cn(
                                                "border-b last:border-b-0",
                                                idx % 2 === 0 && "bg-card",
                                              )}
                                            >
                                              <td className="p-3">
                                                <div className="flex flex-col">
                                                  <span className="font-medium">
                                                    {service.service_name}
                                                  </span>
                                                  <span className="text-muted-foreground text-xs mt-0.5">
                                                    {service.percentage_of_total.toFixed(1)}% of deployment cost
                                                  </span>
                                                </div>
                                              </td>
                                              <td className="p-3 text-right">
                                                {service.cpu_core_hours != null ? (
                                                  <span>{service.cpu_core_hours.toFixed(2)}</span>
                                                ) : (
                                                  <span className="text-muted-foreground">-</span>
                                                )}
                                              </td>
                                              <td className="p-3 text-right">
                                                {service.memory_gb_hours != null ? (
                                                  <span>{service.memory_gb_hours.toFixed(2)}</span>
                                                ) : (
                                                  <span className="text-muted-foreground">-</span>
                                                )}
                                              </td>
                                              <td className="p-3 text-right">
                                                <span className="font-semibold">
                                                  {service.total_compute_cost.toFixed(4)}
                                                </span>
                                              </td>
                                            </tr>
                                          ),
                                        )}
                                      </tbody>
                                      <tfoot>
                                        <tr className="bg-muted border-t-2">
                                          <td className="p-3 font-semibold" colSpan={3}>
                                            Total
                                          </td>
                                          <td className="p-3 text-right font-semibold">
                                            {deploymentWithBreakdown.cost_breakdown.service_breakdown
                                              .reduce(
                                                (sum, service) =>
                                                  sum + service.total_compute_cost,
                                                0,
                                              )
                                              .toFixed(4)}
                                          </td>
                                        </tr>
                                      </tfoot>
                                    </table>
                                  </div>
                                </div>
                              </div>
                            ) : (
                              <p className="text-muted-foreground py-4 text-sm italic text-center">
                                No service breakdown available
                              </p>
                            )}
                        </div>
                      )}

                      {/* Volume Breakdown */}
                      {deploymentWithBreakdown.cost_breakdown?.volume_breakdown &&
                        deploymentWithBreakdown.cost_breakdown.volume_breakdown.length > 0 && (
                          <div className="space-y-3">
                            <SectionHeader>Volume Breakdown</SectionHeader>
                            <div className="bg-muted rounded-lg border overflow-hidden">
                              <div className="overflow-x-auto">
                                <table className="w-full text-sm">
                                  <thead>
                                    <tr className="border-b bg-muted">
                                      <th className="text-left p-3 font-semibold">Volume</th>
                                      <th className="text-left p-3 font-semibold">Type</th>
                                      <th className="text-right p-3 font-semibold">
                                        Cost
                                        <span className="text-muted-foreground font-normal text-xs ml-1">
                                          ($)
                                        </span>
                                      </th>
                                    </tr>
                                  </thead>
                                  <tbody>
                                    {deploymentWithBreakdown.cost_breakdown.volume_breakdown.map(
                                      (volume, idx) => (
                                        <tr
                                          key={volume.volume_name}
                                          className={cn(
                                            "border-b last:border-b-0",
                                            idx % 2 === 0 && "bg-muted/10",
                                          )}
                                        >
                                          <td className="p-3">
                                            <span className="font-medium">
                                              {volume.volume_name}
                                            </span>
                                          </td>
                                          <td className="p-3">
                                            <div className="flex flex-col">
                                              <span>
                                                {volume.storage_class === "ebs" ? "Standard" : "Shared"}
                                              </span>
                                              <span className="text-muted-foreground text-xs mt-0.5">
                                                {volume.percentage_of_total.toFixed(1)}% of deployment cost
                                              </span>
                                            </div>
                                          </td>
                                          <td className="p-3 text-right">
                                            <span className="font-semibold">
                                              {volume.storage_cost.toFixed(4)}
                                            </span>
                                          </td>
                                        </tr>
                                      ),
                                    )}
                                  </tbody>
                                  <tfoot>
                                    <tr className="bg-muted border-t-2">
                                      <td className="p-3 font-semibold" colSpan={2}>
                                        Total
                                      </td>
                                      <td className="p-3 text-right font-semibold">
                                        {deploymentWithBreakdown.cost_breakdown.volume_breakdown
                                          .reduce(
                                            (sum, volume) => sum + volume.storage_cost,
                                            0,
                                          )
                                          .toFixed(4)}
                                      </td>
                                    </tr>
                                  </tfoot>
                                </table>
                              </div>
                            </div>
                          </div>
                        )}

                      {/* Totals Comparison */}
                      {deploymentWithBreakdown.cost_breakdown?.service_breakdown &&
                        deploymentWithBreakdown.cost_breakdown.service_breakdown.length > 0 &&
                        deploymentWithBreakdown.cost_breakdown.volume_breakdown &&
                        deploymentWithBreakdown.cost_breakdown.volume_breakdown.length > 0 && (
                          <div className="bg-card rounded-lg border p-3">
                            <div className="flex items-center justify-between text-sm">
                              <span className="text-muted-foreground">Breakdown Total ($):</span>
                              <span className="font-semibold">
                                {(
                                  deploymentWithBreakdown.cost_breakdown.service_breakdown.reduce(
                                    (sum, service) =>
                                      sum + service.total_compute_cost,
                                    0,
                                  ) +
                                  deploymentWithBreakdown.cost_breakdown.volume_breakdown.reduce(
                                    (sum, volume) => sum + volume.storage_cost,
                                    0,
                                  )
                                ).toFixed(4)}
                              </span>
                            </div>
                            <div className="flex items-center justify-between text-xs text-muted-foreground mt-1">
                              <span>Meter Total ($):</span>
                              <span className="font-semibold text-foreground">
                                {deploymentWithBreakdown.cost_breakdown.meter_breakdown.total_cost.toFixed(4)}
                              </span>
                            </div>
                          </div>
                        )}
                      </div>
                    )}
                </StyledAccordionContent>
              </StyledAccordionItem>
            );
            })}
          </Accordion>
        </div>
      )}
    </StyledCard>
  );
}
