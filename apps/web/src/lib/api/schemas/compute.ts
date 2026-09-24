import { z } from "zod";

import { appSchema, deploymentSchema } from "./apps";
import { awsConnectionPhaseSchema } from "./aws_connections";
import { stubSchema } from "./stubs";

// Synced to shared.http.compute, shared.http.gateway, and
// shared.http.operations. These schemas intentionally omit provider secrets
// and agent installation credentials from list responses.

export const containerSchema = z.object({
  id: z.string(),
  name: z.string(),
  image: z.string(),
  workspace_id: z.string(),
  stub_id: z.string().nullish(),
  app_id: z.string().nullish(),
  machine_id: z.string().nullish(),
  worker_id: z.string().nullish(),
  runtime_machine_id: z.string().default(""),
  runtime_worker_id: z.string().default(""),
  task_id: z.string().nullish(),
  status: z.string(),
  exit_code: z.number().nullish(),
  termination_reason: z.string().default("UNKNOWN"),
  command: z.array(z.string()).default([]),
  cwd: z.string().nullish(),
  ports: z.record(z.number()).default({}),
  created_at: z.string(),
  started_at: z.string().nullish(),
  finished_at: z.string().nullish(),
});
export type Container = z.infer<typeof containerSchema>;

const containerActionCapabilitiesSchema = z.object({
  can_stop: z.boolean().default(false),
  can_shell: z.boolean().default(false),
  can_create_image: z.boolean().default(false),
  can_snapshot_memory: z.boolean().default(false),
});

export const containerDetailSchema = containerSchema.extend({
  app: appSchema.nullish(),
  workload: stubSchema.nullish(),
  deployment: deploymentSchema.nullish(),
  run_name: z.string().nullish(),
  run_status: z.string().nullish(),
  expires_at: z.string().nullish(),
  actions: containerActionCapabilitiesSchema,
});
export type ContainerDetail = z.infer<typeof containerDetailSchema>;

export const containerWithAppPageSchema = z.object({
  data: z
    .array(
      z.object({
        container: containerSchema,
        app_id: z.string().default(""),
      }),
    )
    .default([]),
  next: z.string().default(""),
});
export type ContainerWithAppPage = z.infer<typeof containerWithAppPageSchema>;

export const machineJoinCommandResponseSchema = z
  .object({
    command: z.string(),
    expires_at: z.string(),
  })
  .strict();

/** Synced to shared.compute_fleet.MachineLifecycle. */
export const machineLifecycleSchema = z.enum([
  "requested",
  "provisioning",
  "booting",
  "joining",
  "ready",
  "draining",
  "stopping",
  "stopped",
  "resuming",
  "terminating",
  "deleted",
  "failed",
]);
export type MachineLifecycle = z.infer<typeof machineLifecycleSchema>;

/** Synced to shared.compute_enrollment.MachineBootstrapFailureReason. */
export const machineLifecycleFailureSchema = z.enum([
  "agent_download_failed",
  "runtime_install_failed",
  "network_join_failed",
  "provider_identity_failed",
  "agent_enrollment_failed",
  "worker_image_pull_failed",
  "worker_start_failed",
  "worker_readiness_failed",
  "bootstrap_timed_out",
  "host_preflight_failed",
  "service_lost",
  "machine_record_deleted",
  "provider_stopped",
  "provider_terminated",
  "unknown",
]);

/** Synced to shared.http.compute.MachineResponse, the shape the update route returns. */
export const machineSchema = z.object({
  id: z.string(),
  name: z.string().default(""),
  workspaces: z.array(z.string()).default([]),
  placement: z.string(),
  provider: z.string().default("local"),
  lifecycle: machineLifecycleSchema,
  lifecycle_message: z.string().default(""),
  lifecycle_failure: machineLifecycleFailureSchema.nullable().default(null),
  lifecycle_at: z.string(),
  cpu: z.number().nullish(),
  memory: z.string().nullish(),
  gpu: z.string().nullish(),
  gpu_count: z.number().default(0),
  address: z.string().nullish(),
  labels: z.record(z.string()).default({}),
  created_at: z.string(),
  updated_at: z.string(),
});
export type Machine = z.infer<typeof machineSchema>;

const machinePreflightSeveritySchema = z.enum(["info", "warning", "error"]);
const agentCapacityStateSchema = z.enum([
  "available",
  "draining",
  "preempting",
  "cordoned",
]);

const machinePreflightCheckSchema = z
  .object({
    name: z.string(),
    ok: z.boolean(),
    message: z.string(),
    severity: machinePreflightSeveritySchema,
    remediation: z.string(),
  })
  .strict();

/** Synced to shared.http.compute.UnitMachineResponse. */
export const unitMachineSchema = z.object({
  id: z.string(),
  name: z.string().default(""),
  workspaces: z.array(z.string()).default([]),
  cpu: z.number(),
  memory: z.number(),
  gpu: z.string().default(""),
  gpu_count: z.number().default(0),
  placement: z.string(),
  lifecycle: machineLifecycleSchema,
  lifecycle_message: z.string().default(""),
  lifecycle_failure: machineLifecycleFailureSchema.nullable().default(null),
  lifecycle_at: z.string(),
  connected: z.boolean().default(false),
  schedulable: z.boolean().default(false),
  capacity_state: agentCapacityStateSchema.default("available"),
  capacity_reason: z.string().default(""),
  capacity_observed_at: z.string().nullable().default(null),
  capacity_notice_at: z.string().nullable().default(null),
  preflight_checks: z.array(machinePreflightCheckSchema),
  remediation: z.array(z.string()),
  last_seen_at: z.string().nullable(),
});
export type UnitMachine = z.infer<typeof unitMachineSchema>;

export const unitMachineListSchema = z
  .object({
    data: z.array(unitMachineSchema),
    next: z.string(),
  })
  .strict();

/** Synced to shared.http.compute_policy.ConnectionMachineResponse. */
export const connectionMachineSchema = z
  .object({
    id: z.string(),
    placement: z.string(),
    provider: z.string(),
    region: z.string().default(""),
    availability_zone: z.string().default(""),
    instance_id: z.string().default(""),
    instance_type: z.string().default(""),
    lifecycle: machineLifecycleSchema,
    lifecycle_message: z.string().default(""),
    lifecycle_failure: machineLifecycleFailureSchema.nullable().default(null),
    lifecycle_at: z.string(),
    connected: z.boolean().default(false),
    capacity_state: agentCapacityStateSchema.default("available"),
    capacity_reason: z.string().default(""),
    gpu: z.string().nullable(),
    gpu_count: z.number().int().nonnegative(),
    cpu_millicores: z.number().int().nonnegative(),
    memory_mb: z.number().int().nonnegative(),
    launch_attempt: z.number().int().positive(),
    booted_template_version: z.string().default(""),
    launched_at: z.string().nullable().default(null),
    created_at: z.string(),
  })
  .strict();
export type ConnectionMachine = z.infer<typeof connectionMachineSchema>;

export const connectionMachineListSchema = z
  .object({
    data: z.array(connectionMachineSchema),
    next: z.string(),
  })
  .strict();

/** Synced to shared.http.compute_policy.WorkspaceComputeSummaryResponse. */
export const computeSummarySchema = z
  .object({
    connection: z
      .object({ account_id: z.string(), phase: awsConnectionPhaseSchema })
      .strict()
      .nullable()
      .default(null),
    instances: z
      .object({
        total: z.number().int().nonnegative().default(0),
        ready: z.number().int().nonnegative().default(0),
        pending: z.number().int().nonnegative().default(0),
        degraded: z.number().int().nonnegative().default(0),
      })
      .strict(),
    cost: z
      .object({
        hourly_micros: z.number().int().nonnegative().nullable().default(null),
        daily_micros: z.number().int().nonnegative().nullable().default(null),
        currency: z.literal("USD").default("USD"),
        estimated: z.boolean().default(true),
      })
      .strict(),
    workload_count: z.number().int().nonnegative().default(0),
  })
  .strict();
export type ComputeSummary = z.infer<typeof computeSummarySchema>;
