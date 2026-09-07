import { z } from "zod";

import { appSchema, deploymentSchema } from "./apps";
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

export const customerComputeInstanceOptionSchema = z
  .object({
    instance_type: z.string(),
    kind: z.enum(["cpu", "nvidia_gpu"]),
    gpu: z.string().nullable(),
    gpu_count: z.number().int().nonnegative(),
    cpu_millicores: z.number().int().positive(),
    memory_mb: z.number().int().positive(),
  })
  .strict();
export type CustomerComputeInstanceOption = z.infer<typeof customerComputeInstanceOptionSchema>;

export const customerComputeCatalogSchema = z
  .object({
    data: z.array(
      z
        .object({
          provider: z.literal("aws"),
          region: z.string(),
          instances: z.array(customerComputeInstanceOptionSchema),
        })
        .strict(),
    ),
    next: z.string(),
  })
  .strict();

export const poolJoinCommandResponseSchema = z
  .object({
    command: z.string(),
    expires_at: z.string(),
  })
  .strict();

const machineReadinessPhaseSchema = z.enum(["joining", "ready", "blocked", "offline", "revoked"]);

const machinePreflightSeveritySchema = z.enum(["info", "warning", "error"]);
const agentCapacityStateSchema = z.enum(["available", "draining", "preempting", "cordoned"]);

const machinePreflightCheckSchema = z
  .object({
    name: z.string(),
    ok: z.boolean(),
    message: z.string(),
    severity: machinePreflightSeveritySchema,
    remediation: z.string(),
  })
  .strict();

export const unitMachineSchema = z.object({
  id: z.string(),
  cpu: z.number(),
  memory: z.number(),
  pool: z.string(),
  provider_name: z.string(),
  readiness_phase: machineReadinessPhaseSchema,
  readiness_message: z.string(),
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

export const customerComputeInstanceSchema = z
  .object({
    id: z.string(),
    machine_id: z.string().nullable(),
    provider: z.string(),
    region: z.string(),
    instance_type: z.string().nullable(),
    status: z.string(),
    cpu_millicores: z.number().int().nonnegative(),
    memory_mb: z.number().int().nonnegative(),
    gpu: z.string().nullable(),
    gpu_count: z.number().int().nonnegative(),
    bootstrap_phase: z.enum([
      "requested",
      "provisioning",
      "booting",
      "joining",
      "failed",
      "deleting",
    ]),
    service_state: z.enum(["provisioning", "joining", "serving", "degraded", "failed", "deleting"]),
    bootstrap_failure_reason: z
      .enum([
        "agent_download_failed",
        "runtime_install_failed",
        "network_join_failed",
        "provider_identity_failed",
        "agent_enrollment_failed",
        "worker_image_pull_failed",
        "worker_start_failed",
        "worker_readiness_failed",
        "bootstrap_timed_out",
        "service_lost",
        "machine_record_deleted",
        "provider_stopped",
        "provider_terminated",
        "unknown",
      ])
      .nullable(),
    bootstrap_failure_detail: z.string().default(""),
    bootstrap_observed_at: z.string(),
    launch_attempt: z.number().int().positive(),
    booted_template_version: z.string().default(""),
    created_at: z.string(),
  })
  .strict();
export type CustomerComputeInstance = z.infer<typeof customerComputeInstanceSchema>;

export const customerComputeInstanceListSchema = z
  .object({
    data: z.array(customerComputeInstanceSchema),
    next: z.string(),
  })
  .strict();
