import { z } from 'zod'

// Resource schemas
export const ResourceRequirementsSchema = z.object({
  cpu: z.string().nullable().optional(),
  memory: z.string().nullable().optional(),
})

export const ResourcesSchema = z.object({
  limits: ResourceRequirementsSchema.nullable().optional(),
  requests: ResourceRequirementsSchema.nullable().optional(),
})

export const CurrentUsageSchema = z.object({
  cpu: z.string().nullable().optional(),
  memory: z.string().nullable().optional(),
})

// Health check schemas
export const HttpGetProbeSchema = z.object({
  path: z.string(),
  port: z.number(),
  scheme: z.string().nullable().optional(),
})

export const TcpSocketProbeSchema = z.object({
  port: z.number(),
})

export const ExecProbeSchema = z.object({
  command: z.array(z.string()),
})

export const ProbeConfigSchema = z.object({
  httpGet: HttpGetProbeSchema.nullable().optional(),
  tcpSocket: TcpSocketProbeSchema.nullable().optional(),
  exec: ExecProbeSchema.nullable().optional(),
  initialDelaySeconds: z.number().nullable().optional(),
  timeoutSeconds: z.number().nullable().optional(),
  periodSeconds: z.number().nullable().optional(),
  successThreshold: z.number().nullable().optional(),
  failureThreshold: z.number().nullable().optional(),
})

export const HealthCheckValuesSchema = z.object({
  enabled: z.boolean().optional(),
  livenessProbe: ProbeConfigSchema.nullable().optional(),
  readinessProbe: ProbeConfigSchema.nullable().optional(),
})

// HPA schemas
export const HPAMetricSchema = z.object({
  type: z.string(),
  resource: z.record(z.string(), z.any()),
})

export const HPAValuesSchema = z.object({
  enabled: z.boolean().optional(),
  minReplicas: z.number().optional(),
  maxReplicas: z.number().optional(),
  metrics: z.array(HPAMetricSchema).optional(),
})

// Pod status schema
export const PodStatusSchema = z.object({
  name: z.string(),
  phase: z.string(),
  ready_containers: z.number(),
  total_containers: z.number(),
  restart_count: z.number(),
  age: z.string(),
  node: z.string(),
  ip: z.string().nullable().optional(),
  cpu_usage: z.string().nullable().optional(),
  memory_usage: z.string().nullable().optional(),
  reason: z.string().nullable().optional(),
  message: z.string().nullable().optional(),
})

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
})

export const ServiceStatusSummarySchema = z.object({
  name: z.string(),
  status: z.string(),
  ready_replicas: z.number(),
  total_replicas: z.number(),
  image: z.string().nullable(),
  ports: z.array(z.string()).nullable(),
  restarts: z.number(),
  endpoint: z.string().nullable(),
  // Custom domain fields
  custom_domain: z.string().nullable().optional(),
  domain_status: z.string().nullable().optional(),
  cname_target: z.string().nullable().optional(),
  // Detailed fields for service details view
  resources: ResourcesSchema.nullable().optional(),
  current_usage: CurrentUsageSchema.nullable().optional(),
  healthcheck: HealthCheckValuesSchema.nullable().optional(),
  hpa: HPAValuesSchema.nullable().optional(),
  pods: z.array(PodStatusSchema).nullable().optional(),
})

export const VolumeStatusSummarySchema = z.object({
  name: z.string(),
  status: z.string(),
  storage_type: z.string(),
  size: z.string().nullable().optional(),
})

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
})

export const DeploymentStatusResponseSchema = z.object({
  status: DeploymentStatusSchema,
})

export type DeploymentOverview = z.infer<typeof DeploymentOverviewSchema>
export type DeploymentStatus = z.infer<typeof DeploymentStatusSchema>
export type DeploymentStatusResponse = z.infer<
  typeof DeploymentStatusResponseSchema
>
export type ServiceStatusSummary = z.infer<typeof ServiceStatusSummarySchema>
export type Resources = z.infer<typeof ResourcesSchema>
export type CurrentUsage = z.infer<typeof CurrentUsageSchema>
export type HealthCheckValues = z.infer<typeof HealthCheckValuesSchema>
export type ProbeConfig = z.infer<typeof ProbeConfigSchema>
export type HPAValues = z.infer<typeof HPAValuesSchema>
export type PodStatus = z.infer<typeof PodStatusSchema>

export interface DeploymentWithStatus extends DeploymentOverview {
  status?: DeploymentStatus
  isLoading?: boolean
}
