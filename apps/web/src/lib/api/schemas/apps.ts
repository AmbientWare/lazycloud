import { z } from "zod";
import { cpuRequestSchema, memoryRequestSchema } from "./resources";
import { productRegionSchema } from "./placement";

import { diskStatuses } from "./storage";
import { stubSchema } from "./stubs";

export const appSchema = z.object({
  id: z.string(),
  workspace_id: z.string(),
  stub_id: z.string().nullish(),
  name: z.string(),
  version: z.number(),
  public: z.boolean(),
  active: z.boolean(),
  deleted_at: z.string().nullish(),
  created_at: z.string(),
  updated_at: z.string(),
  actions: z
    .object({
      can_pause: z.boolean().default(false),
      can_resume: z.boolean().default(false),
      can_delete: z.boolean().default(false),
    })
    .default({ can_pause: false, can_resume: false, can_delete: false }),
});
export type App = z.infer<typeof appSchema>;

export const podRoles = ["service", "devbox"] as const;
export type PodRole = (typeof podRoles)[number];

export const deploymentSchema = z.object({
  id: z.string(),
  name: z.string(),
  kind: z.string(),
  // Set for pods only; the server resolves it, so the browser never infers it.
  role: z.enum(podRoles).nullish(),
  app_id: z.string().nullish(),
  stub_id: z.string().nullish(),
  version: z.number(),
  spec: z
    .object({
      resources: z
        .object({
          cpu: cpuRequestSchema.nullish(),
          region: productRegionSchema.nullish(),
          availability_zone: z.string().default(""),
          memory: memoryRequestSchema.nullish(),
          disk: z.string().nullish(),
          gpu: z.array(z.string()).default([]),
          gpu_count: z.number().default(0),
          timeout_seconds: z.number().nullish(),
          concurrency: z.number().default(1),
          keep_warm: z.number().nullish(),
          preemptible: z.boolean().default(false),
        })
        .default({
          gpu: [],
          gpu_count: 0,
          concurrency: 1,
          availability_zone: "",
          preemptible: false,
        }),
      route: z.string().nullish(),
      methods: z.array(z.string()).default([]),
      cron: z.string().nullish(),
      command: z.array(z.string()).default([]),
      ports: z.record(z.number()).default({}),
      machine: z.string().default(""),
    })
    .default({
      resources: { gpu: [], gpu_count: 0, concurrency: 1 },
      methods: [],
      command: [],
      ports: {},
      machine: "",
    }),
  active: z.boolean(),
  deleted_at: z.string().nullish(),
  created_at: z.string(),
  updated_at: z.string(),
  actions: z
    .object({
      can_start: z.boolean().default(false),
      can_stop: z.boolean().default(false),
      can_delete: z.boolean().default(false),
      can_scale: z.boolean().default(false),
    })
    .default({ can_start: false, can_stop: false, can_delete: false, can_scale: false }),
  scaling: z
    .object({
      min_replicas: z.number().int().nonnegative(),
      max_replicas: z.number().int().nonnegative(),
    })
    .nullish(),
});
export type Deployment = z.infer<typeof deploymentSchema>;

export const devboxStates = ["running", "starting", "stopped"] as const;
export const devboxPhases = [
  "stopped",
  "queued",
  "pulling_image",
  "restoring_disk",
  "starting",
  "running",
  "stopping",
  "failed",
] as const;
export type DevboxPhase = (typeof devboxPhases)[number];

// Synced to packages/shared/src/shared/http/deployments.py (DevboxResponse).
export const devboxSchema = z.object({
  ssh_command: z.string(),
  ssh_host: z.string(),
  state: z.enum(devboxStates),
  phase: z.enum(devboxPhases),
  phase_reason: z.string().default(""),
  container_id: z.string().nullable(),
  open_connections: z.number().int().nonnegative(),
  idle_deadline: z.string().nullable(),
  disk: z
    .object({
      name: z.string(),
      size_bytes: z.number(),
      stored_bytes: z.number(),
      generation: z.number().int().nonnegative(),
      status: z.enum(diskStatuses),
    })
    .nullable(),
});
export type Devbox = z.infer<typeof devboxSchema>;

export const deploymentListSchema = z.object({
  data: z.array(deploymentSchema).default([]),
  next: z.string().default(""),
});
export type DeploymentList = z.infer<typeof deploymentListSchema>;

// Synced to packages/shared/src/shared/http/deployments.py (DeploymentUrlResponse);
// scoped to the invoke URL the dashboard consumes.
export const deploymentUrlSchema = z.object({
  url: z.string(),
});
export type DeploymentUrl = z.infer<typeof deploymentUrlSchema>;

const appSummarySchema = z.object({
  app: appSchema,
  latest_workload: stubSchema.nullish(),
  latest_deployment: deploymentSchema.nullish(),
  workload_kinds: z.record(z.number()).default({}),
  workload_count: z.number().default(0),
  active_versions: z.number().default(0),
  running_containers: z.number().default(0),
  runs_24h: z.number().default(0),
  failed_runs_24h: z.number().default(0),
  pending_runs_24h: z.number().default(0),
  succeeded_runs_24h: z.number().default(0),
  activity_24h: z.array(z.number()).default([]),
  failures_24h: z.array(z.number()).default([]),
  pending_24h: z.array(z.number()).default([]),
  succeeded_24h: z.array(z.number()).default([]),
  last_deployed_at: z.string().nullish(),
});
export type AppSummary = z.infer<typeof appSummarySchema>;

export const appSummaryListSchema = z.object({
  items: z.array(appSummarySchema).default([]),
});
