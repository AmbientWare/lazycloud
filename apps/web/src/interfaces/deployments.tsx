import { z } from "zod";

export const DeploymentOverviewSchema = z.object({
  id: z.string(),
  workspace_id: z.string(),
  name: z.string(),
  namespace: z.string(),
  state: z.string(),
  status_message: z.string().nullable(),
  created_at: z.string().nullable(),
  updated_at: z.string().nullable(),
  deployed_at: z.string().nullable(),
  service_count: z.number(),
  volume_count: z.number(),
  network_count: z.number(),
  ready_services: z.number().nullable(),
});

export const ServiceStatusSummarySchema = z.object({
  name: z.string(),
  status: z.string(),
  ready_replicas: z.number(),
  total_replicas: z.number(),
  image: z.string().nullable(),
  ports: z.array(z.string()).nullable(),
  restarts: z.number(),
  endpoint: z.string().nullable(),
});

export const VolumeStatusSummarySchema = z.object({
  name: z.string(),
  status: z.string(),
  storage_type: z.string(),
});

export const DeploymentStatusSchema = z.object({
  deployment_id: z.string(),
  deployment_name: z.string(),
  namespace: z.string(),
  status: z.string(),
  ready: z.boolean(),
  last_checked: z.string(),
  deployed_at: z.string(),
  total_services: z.number(),
  ready_services: z.number(),
  total_replicas: z.number(),
  ready_replicas: z.number(),
  services: z.array(ServiceStatusSummarySchema),
  volumes: z.array(VolumeStatusSummarySchema).nullable(),
  networks: z
    .array(
      z.object({
        name: z.string(),
        status: z.string(),
        driver: z.string().nullable(),
      }),
    )
    .nullable(),
});

export const DeploymentStatusResponseSchema = z.object({
  status: DeploymentStatusSchema,
});

export type DeploymentOverview = z.infer<typeof DeploymentOverviewSchema>;
export type DeploymentStatus = z.infer<typeof DeploymentStatusSchema>;
export type DeploymentStatusResponse = z.infer<
  typeof DeploymentStatusResponseSchema
>;

export interface DeploymentWithStatus extends DeploymentOverview {
  status?: DeploymentStatus;
  isLoading?: boolean;
}
