import { z } from "zod";

import { appSchema, deploymentSchema } from "./apps";
import { stubSchema } from "./stubs";

// The reference's container, which the sandbox and pod pages read until the
// workloads packet rewrites them.
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
