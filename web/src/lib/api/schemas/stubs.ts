import { z } from "zod";
import { cpuRequestSchema, memoryRequestSchema } from "./resources";
import { productRegionSchema } from "./placement";

// Synced to python/shared/src/shared/http/stubs.py (StubResponse, StubListResponse);
// scoped to the fields the dashboard renders.

const stubRuntimeConfigSchema = z.object({
  region: productRegionSchema.nullish(),
  availability_zone: z.string().default(""),
  preemptible: z.boolean().default(false),
  cpu: cpuRequestSchema.nullish(),
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
