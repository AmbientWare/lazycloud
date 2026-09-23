import { z } from "zod";

export const deploymentPlanSchema = z.object({
  app: z.string(),
  app_id: z.string().nullable(),
  snapshot: z.string(),
  prune: z.boolean(),
  data: z.array(
    z.object({
      kind: z.enum(["function", "endpoint", "asgi", "pod", "sandbox", "command"]),
      name: z.string(),
      action: z.enum(["add", "redeploy", "retain", "remove"]),
      versions: z.number().int().nonnegative(),
    }),
  ),
  next: z.string().default(""),
});
export type DeploymentPlan = z.infer<typeof deploymentPlanSchema>;

export const deploymentPruneSchema = z.object({
  operation_id: z.string().uuid(),
  app: z.string(),
  removed_versions: z.number().int().nonnegative(),
  complete: z.boolean(),
});
export type DeploymentPrune = z.infer<typeof deploymentPruneSchema>;
