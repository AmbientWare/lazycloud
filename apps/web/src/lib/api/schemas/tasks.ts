import { z } from "zod";

import { containerSchema } from "./compute";
import { jsonValueSchema } from "./json";
import { taskPendingProgressSchema } from "./task-progress";

// Synced to packages/shared/src/shared/http/tasks.py (TaskResponse, TaskDetailResponse,
// TaskPageResponse, TaskMetricsSummaryResponse, TaskTimeWindowBucketListResponse) and
// packages/shared/src/shared/http/functions.py (FunctionCallGraphNode, FunctionCallGraphResponse).

export const taskStatuses = [
  "pending",
  "running",
  "retry",
  "complete",
  "failed",
  "expired",
  "timeout",
  "cancelled",
] as const;
export type TaskStatusValue = (typeof taskStatuses)[number];

const terminalTaskStatuses: readonly TaskStatusValue[] = [
  "complete",
  "failed",
  "expired",
  "timeout",
  "cancelled",
];

/**
 * The owning resources a task row names.
 *
 * The server sends what a reader sees — a name, a kind, a version — rather than
 * the whole app, workload, and deployment records, which a page of a hundred
 * rows would otherwise repeat a hundred times. Their ids are on the row itself.
 */
const taskAppReferenceSchema = z.object({ name: z.string() });
const taskWorkloadReferenceSchema = z.object({ name: z.string(), kind: z.string() });
const taskDeploymentReferenceSchema = z.object({ name: z.string(), version: z.number() });

export const taskSchema = z.object({
  id: z.string(),
  name: z.string(),
  status: z.string(),
  pending_progress: taskPendingProgressSchema.nullish(),
  workspace_id: z.string().nullish(),
  app_id: z.string().nullish(),
  stub_id: z.string().nullish(),
  deployment_id: z.string().nullish(),
  container_id: z.string().nullish(),
  parent_task_id: z.string().nullish(),
  root_task_id: z.string().nullish(),
  handler: z.string().nullish(),
  attempt_number: z.number().default(0),
  max_attempts: z.number().default(1),
  result: jsonValueSchema.nullish(),
  error: z.string().nullish(),
  exit_code: z.number().nullish(),
  created_at: z.string(),
  started_at: z.string().nullish(),
  finished_at: z.string().nullish(),
  app: taskAppReferenceSchema.nullish(),
  workload: taskWorkloadReferenceSchema.nullish(),
  deployment: taskDeploymentReferenceSchema.nullish(),
  // Only the single-task read (TaskDetailResponse) resolves a container; rows
  // carry `container_id` and nothing more.
  container: containerSchema.nullish(),
  actions: z
    .object({
      can_cancel: z.boolean().default(false),
      can_rerun: z.boolean().default(false),
      can_shell: z.boolean().default(false),
    })
    .default({ can_cancel: false, can_rerun: false, can_shell: false }),
});
export type Task = z.infer<typeof taskSchema>;

export const taskPageSchema = z.object({
  data: z.array(taskSchema).default([]),
  next: z.string().default(""),
});

export const taskMetricsSummarySchema = z.object({
  total: z.number(),
  status_counts: z.record(z.number()).default({}),
  completed: z.number(),
  failed: z.number(),
  cancelled: z.number(),
  failure_rate: z.number().default(0),
  average_runtime_ms: z.number().nullish(),
  runtime_ms_p50: z.number().nullish(),
  runtime_ms_p95: z.number().nullish(),
  runtime_ms_p99: z.number().nullish(),
  startup_ms_p50: z.number().nullish(),
  startup_ms_p95: z.number().nullish(),
});
export type TaskMetricsSummary = z.infer<typeof taskMetricsSummarySchema>;

const taskTimeWindowBucketSchema = z.object({
  timestamp: z.string(),
  count: z.number(),
  status_counts: z.record(z.number()).default({}),
});
export type TaskTimeWindowBucket = z.infer<typeof taskTimeWindowBucketSchema>;

export const taskTimeWindowBucketListSchema = z.object({
  items: z.array(taskTimeWindowBucketSchema).default([]),
});

export function isKnownTaskStatus(status: string): status is TaskStatusValue {
  return (taskStatuses as readonly string[]).includes(status);
}

export function isTerminalTaskStatus(status: string): boolean {
  return (terminalTaskStatuses as readonly string[]).includes(status);
}

const callGraphNodeBaseSchema = z.object({
  task_id: z.string(),
  parent_task_id: z.string().default(""),
  root_task_id: z.string().default(""),
  status: z.string(),
  name: z.string().default(""),
  function_name: z.string().default(""),
  created_at: z.string().nullish(),
  started_at: z.string().nullish(),
  finished_at: z.string().nullish(),
  dependencies: z.array(z.string()).default([]),
});

export type CallGraphNode = z.infer<typeof callGraphNodeBaseSchema> & {
  children: CallGraphNode[];
};

const callGraphNodeSchema: z.ZodType<CallGraphNode, z.ZodTypeDef, unknown> =
  callGraphNodeBaseSchema.extend({
    children: z.lazy(() => z.array(callGraphNodeSchema).default([])),
  });

export const callGraphSchema = z.object({
  root_task_id: z.string().default(""),
  root: callGraphNodeSchema.nullish(),
  nodes: z.array(callGraphNodeSchema).default([]),
});
export type CallGraph = z.infer<typeof callGraphSchema>;
