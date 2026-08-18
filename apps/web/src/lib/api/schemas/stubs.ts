import { z } from "zod";

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

const stubRuntimeConfigSchema = z.object({
  cpu: z.union([z.number(), z.string()]).nullish(),
  memory: z.union([z.number(), z.string()]).nullish(),
  gpu: z.string().nullish(),
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

