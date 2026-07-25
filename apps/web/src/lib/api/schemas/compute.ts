import { z } from "zod";

import { appSchema, deploymentSchema } from "./apps";
import { computePlacementTargetSchema } from "./compute_placement";
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

const awsInstanceTypeSchema = z
  .string()
  .min(1)
  .refine((value) => value.trim().length > 0, "Instance type cannot be empty");
const awsDefaultInstanceTypeSchema = z
  .string()
  .min(1)
  .max(64)
  .refine((value) => value.trim().length > 0, "Instance type cannot be empty");

const awsWorkspaceComputePolicySchema = z
  .object({
    default_region: z.string(),
    default_instance_type: awsDefaultInstanceTypeSchema,
    initial_cpu_workers: z.number().int().min(0).max(100),
    min_cpu_workers: z.number().int().min(0).max(100),
    max_cpu_instances: z.number().int().min(0).max(100),
    max_gpu_instances: z.number().int().min(0).max(100),
    min_free_cpu_millicores: z.number().int().min(0),
    min_free_memory_mib: z.number().int().min(0),
    allowed_regions: z.array(z.string()).min(1),
    allowed_instance_types: z
      .array(awsInstanceTypeSchema)
      .refine(
        (instanceTypes) => new Set(instanceTypes).size === instanceTypes.length,
        "Allowed instance types must be unique",
      ),
    idle_timeout_seconds: z.number().int().min(60).max(86_400),
    root_volume_gib: z.number().int().min(50).max(2048),
  })
  .strict()
  .superRefine((policy, context) => {
    if (
      policy.min_cpu_workers > policy.initial_cpu_workers ||
      policy.initial_cpu_workers > policy.max_cpu_instances
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "CPU worker capacity must satisfy min <= initial <= max",
        path: ["initial_cpu_workers"],
      });
    }
    if (
      policy.allowed_instance_types.length > 0 &&
      !policy.allowed_instance_types.includes(policy.default_instance_type)
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Default instance type must be allowed",
        path: ["default_instance_type"],
      });
    }
  });

export const workspaceComputePolicySchema = z
  .object({
    revision: z.number().int().positive(),
    default_placement: computePlacementTargetSchema,
    aws: awsWorkspaceComputePolicySchema,
    created_at: z.string(),
    updated_at: z.string(),
  })
  .strict();
export type WorkspaceComputePolicy = z.infer<typeof workspaceComputePolicySchema>;

export const workspaceComputePolicyUpdateRequestSchema = z
  .object({
    expected_revision: z.number().int().positive(),
    default_placement: computePlacementTargetSchema,
    aws: awsWorkspaceComputePolicySchema,
  })
  .strict();
export type WorkspaceComputePolicyUpdateRequest = z.infer<
  typeof workspaceComputePolicyUpdateRequestSchema
>;

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

export const poolJoinCommandResponseSchema = z.object({
  command: z.string(),
  expires_at: z.string(),
}).strict();

const machineReadinessPhaseSchema = z.enum([
  "joining",
  "ready",
  "blocked",
  "offline",
  "revoked",
]);

const machinePreflightSeveritySchema = z.enum(["info", "warning", "error"]);
const agentCapacityStateSchema = z.enum(["available", "preempting", "cordoned"]);

const machinePreflightCheckSchema = z
  .object({
    name: z.string(),
    ok: z.boolean(),
    message: z.string(),
    severity: machinePreflightSeveritySchema,
    remediation: z.string(),
  })
  .strict();

export const poolMachineSchema = z.object({
  id: z.string(),
  cpu: z.number(),
  memory: z.number(),
  pool_name: z.string(),
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
export type PoolMachine = z.infer<typeof poolMachineSchema>;

export const poolMachineListSchema = z
  .object({
    data: z.array(poolMachineSchema),
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
      "ready",
      "failed",
      "deleting",
    ]),
    bootstrap_failure_reason: z
      .enum([
        "agent_download_failed",
        "runtime_install_failed",
        "network_join_failed",
        "provider_identity_failed",
        "agent_enrollment_failed",
        "worker_start_failed",
        "worker_readiness_failed",
        "bootstrap_timed_out",
        "provider_stopped",
        "provider_terminated",
        "unknown",
      ])
      .nullable(),
    bootstrap_observed_at: z.string(),
    launch_attempt: z.number().int().positive(),
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

const workerContainerSchema = z.object({
  container_id: z.string(),
  workspace_id: z.string().default(""),
  stub_id: z.string().default(""),
  status: z.string().default(""),
  scheduled_at: z.string().nullish(),
  started_at: z.string().nullish(),
});

export const workerSchema = z.object({
  id: z.string(),
  status: z.string(),
  pool_name: z.string(),
  machine_id: z.string().default(""),
  gpu: z.string().default(""),
  runtime: z.string().default(""),
  total_cpu: z.number().default(0),
  total_memory: z.number().default(0),
  total_gpu_count: z.number().default(0),
  free_cpu: z.number().default(0),
  free_memory: z.number().default(0),
  free_gpu_count: z.number().default(0),
  resource_version: z.number().default(0),
  requires_pool_selector: z.boolean().default(false),
  preemptible: z.boolean().default(false),
  created_at: z.string().nullish(),
  updated_at: z.string().nullish(),
  active_containers: z.array(workerContainerSchema).default([]),
});
export type Worker = z.infer<typeof workerSchema>;

export const workerListSchema = z.object({
  workers: z.array(workerSchema).default([]),
});
