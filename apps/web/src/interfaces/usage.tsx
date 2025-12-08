import { z } from "zod";

export const UsagePeriodInfoSchema = z.object({
  start: z.string(),
  end: z.string(),
});

export const UsageMetricsSchema = z.object({
  cpu_core_hours: z.number(),
  memory_gb_hours: z.number(),
  standard_gb_hours: z.number(),
  shared_gb_hours: z.number(),
  build_minutes: z.number(),
  public_endpoint_hours: z.number(),
  costs: z
    .object({
      cpu_cost: z.number(),
      memory_cost: z.number(),
      standard_cost: z.number(),
      shared_cost: z.number(),
      build_cost: z.number(),
      endpoint_cost: z.number(),
      total_cost: z.number(),
    })
    .optional(),
});


export const DailyUsageDataSchema = z.object({
  date: z.string(),
  cpu_core_hours: z.number(),
  memory_gb_hours: z.number(),
  standard_gb_hours: z.number(),
  shared_gb_hours: z.number(),
  build_minutes: z.number(),
  public_endpoint_hours: z.number(),
  costs: z
    .object({
      cpu_cost: z.number(),
      memory_cost: z.number(),
      standard_cost: z.number(),
      shared_cost: z.number(),
      build_cost: z.number(),
      endpoint_cost: z.number(),
      total_cost: z.number(),
    })
    .optional(),
});

export const DailyUsageResponseSchema = z.object({
  workspace_id: z.string(),
  period: UsagePeriodInfoSchema,
  daily_usage: z.array(DailyUsageDataSchema),
});

export const DeploymentUsageOverviewSchema = z.object({
  deployment_id: z.string(),
  deployment_name: z.string(),
  usage: UsageMetricsSchema,
  status: z.enum(["Active", "Inactive"]),
  deployed_at: z.string().nullable().optional(),
  deleted_at: z.string().nullable().optional(),
});

export const WorkspaceUsageSummarySchema = z.object({
  workspace_id: z.string(),
  workspace_name: z.string(),
  workspace_status: z.enum(["Active", "Inactive"]),
  usage: UsageMetricsSchema,
  record_count: z.number(),
  deployments: z.array(DeploymentUsageOverviewSchema),
});

export const AggregatedUsageResponseSchema = z.object({
  period: UsagePeriodInfoSchema,
  usage: UsageMetricsSchema,
  workspace_count: z.number(),
  record_count: z.number(),
  workspaces: z.array(WorkspaceUsageSummarySchema),
});

export const AggregatedDailyUsageResponseSchema = z.object({
  period: UsagePeriodInfoSchema,
  daily_usage: z.array(DailyUsageDataSchema),
  workspace_count: z.number(),
});

export type UsagePeriodInfo = z.infer<typeof UsagePeriodInfoSchema>;
export type UsageMetrics = z.infer<typeof UsageMetricsSchema>;
export type DailyUsageData = z.infer<typeof DailyUsageDataSchema>;
export type AggregatedUsageResponse = z.infer<
  typeof AggregatedUsageResponseSchema
>;
export type AggregatedDailyUsageResponse = z.infer<
  typeof AggregatedDailyUsageResponseSchema
>;

// Cost breakdown schemas

export const MeterCostBreakdownSchema = z.object({
  cpu_cost: z.number(),
  memory_cost: z.number(),
  standard_cost: z.number(),
  shared_cost: z.number(),
  build_cost: z.number(),
  endpoint_cost: z.number(),
  total_cost: z.number(),
});

export const ServiceCostBreakdownSchema = z.object({
  service_name: z.string(),
  cpu_core_hours: z.number().nullable().optional(),
  memory_gb_hours: z.number().nullable().optional(),
  cpu_cost: z.number(),
  memory_cost: z.number(),
  total_compute_cost: z.number(),
  percentage_of_total: z.number(),
});

export const VolumeCostBreakdownSchema = z.object({
  volume_name: z.string(),
  storage_class: z.string(),
  storage_cost: z.number(),
  percentage_of_total: z.number(),
});

export const WorkspaceCostBreakdownResponseSchema = z.object({
  workspace_id: z.string(),
  period: UsagePeriodInfoSchema,
  meter_breakdown: MeterCostBreakdownSchema,
  service_breakdown: z.array(ServiceCostBreakdownSchema),
  volume_breakdown: z.array(VolumeCostBreakdownSchema),
  is_estimated: z.boolean(),
});

export type ServiceCostBreakdown = z.infer<typeof ServiceCostBreakdownSchema>;
export type VolumeCostBreakdown = z.infer<typeof VolumeCostBreakdownSchema>;
export type WorkspaceCostBreakdownResponse = z.infer<
  typeof WorkspaceCostBreakdownResponseSchema
>;

export type DeploymentUsageOverview = z.infer<
  typeof DeploymentUsageOverviewSchema
>;

// Meter pricing schema
export const MeterPricingResponseSchema = z.object({
  cpu_usage: z.number(),
  memory_usage: z.number(),
  standard_storage: z.number(),
  shared_storage: z.number(),
  build_minutes: z.number(),
  public_endpoints: z.number(),
});

export type MeterPricingResponse = z.infer<typeof MeterPricingResponseSchema>;
