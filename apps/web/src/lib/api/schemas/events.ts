import { z } from "zod";

export const workspaceChangeTopics = [
  "apps",
  "deployments",
  "workloads",
  "tasks",
  "containers",
  "compute.pools",
  "compute.machines",
  "compute.workers",
  "compute.agents",
  "compute.providers",
  "compute.connections",
  "storage.secrets",
  "storage.volumes",
  "usage",
  "settings.concurrency",
] as const;

export const workspaceChangeKinds = ["created", "updated", "deleted"] as const;

export const workspaceChangeEventSchema = z
  .object({
    event_id: z.string().min(1),
    occurred_at: z.string().datetime({ offset: true }),
    workspace_id: z.string().min(1),
    topic: z.enum(workspaceChangeTopics),
    change: z.enum(workspaceChangeKinds),
    resource_id: z.string().min(1),
    app_id: z.string().nullish(),
    deployment_id: z.string().nullish(),
    stub_id: z.string().nullish(),
    task_id: z.string().nullish(),
    root_task_id: z.string().nullish(),
    container_id: z.string().nullish(),
  })
  .strict();

export type WorkspaceChangeTopic = (typeof workspaceChangeTopics)[number];
export type WorkspaceChangeEvent = z.infer<typeof workspaceChangeEventSchema>;
