import { z } from "zod";
import { cpuRequestSchema, memoryRequestSchema } from "./resources";
import { productRegionSchema } from "./placement";

// Synced to packages/shared/src/shared/http/stubs.py (StubResponse, StubListResponse);
// scoped to the fields the dashboard renders.

export const stubKinds = [
  "function",
  "endpoint",
  "asgi",
  "pod",
  "shell",
  "sandbox",
  "command",
] as const;
export type StubKind = (typeof stubKinds)[number];

/**
 * The kinds a person deploys and can therefore filter by.
 *
 * Narrower than `stubKinds`, which mirrors the wire enum and has to accept
 * everything the server may send. `shell` and `command` are how the platform
 * runs something on a workload's behalf, not workloads anyone declares —
 * offering them as filters lists two options that can only ever return nothing.
 */
export const workloadKinds = ["function", "endpoint", "asgi", "pod", "sandbox"] as const;
export type WorkloadKind = (typeof workloadKinds)[number];

const stubRuntimeConfigSchema = z.object({
  region: productRegionSchema.nullish(),
  cpu: z.union([cpuRequestSchema, z.string()]).nullish(),
  memory: memoryRequestSchema.nullish(),
  gpu: z.array(z.string()).default([]),
  gpu_count: z.number().nullish(),
  keep_warm: z.number().nullish(),
  timeout_seconds: z.number().nullish(),
  concurrency: z.number().nullish(),
});

export const stubSchema = z.object({
  id: z.string(),
  workspace_id: z.string(),
  name: z.string(),
  kind: z.string(),
  handler: z.string().nullish(),
  deployment_id: z.string().nullish(),
  app_id: z.string().nullish(),
  public: z.boolean().default(false),
  config: z.object({ runtime: stubRuntimeConfigSchema.nullish() }).default({ runtime: null }),
  created_at: z.string(),
  updated_at: z.string(),
});
export type Stub = z.infer<typeof stubSchema>;

export const stubListSchema = z.object({
  stubs: z.array(stubSchema).default([]),
});
