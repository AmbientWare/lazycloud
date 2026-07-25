import { z } from "zod";

const concurrencyLimitSchema = z.object({
  id: z.string(),
  workspace_id: z.string(),
  name: z.string(),
  limit: z.number(),
  in_flight: z.number().default(0),
  resource_type: z.string(),
  resource_id: z.string().nullish(),
  available: z.number(),
  saturated: z.boolean(),
  created_at: z.string(),
  updated_at: z.string(),
});
export type ConcurrencyLimit = z.infer<typeof concurrencyLimitSchema>;

export const concurrencyLimitListSchema = z.object({
  limits: z.array(concurrencyLimitSchema).default([]),
});
export type ConcurrencyLimitList = z.infer<typeof concurrencyLimitListSchema>;
