import { z } from "zod";

export const computePlacementTargetSchema = z.enum(["managed", "aws"]);
export type ComputePlacementTarget = z.infer<typeof computePlacementTargetSchema>;

export const resolvedComputePlacementSchema = z
  .object({
    target: computePlacementTargetSchema,
    source: z.enum(["workspace_default", "workload_override", "attached_pool"]),
    provider: z.string(),
    region: z.string(),
  })
  .strict();
export type ResolvedComputePlacement = z.infer<typeof resolvedComputePlacementSchema>;
